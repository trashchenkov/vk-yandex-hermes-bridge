#!/usr/bin/env python3
"""VM-side worker for VK -> Yandex Message Queue -> local Hermes -> VK.

The worker keeps Hermes private: it polls Yandex Message Queue outbound from the
VM, calls the Hermes API Server on 127.0.0.1, sends the answer to VK, then
acknowledges the queue message.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.config import Config

LOG = logging.getLogger("vk_hermes_worker")
VK_MAX_MESSAGE_CHARS = 9000


def redact_secrets(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)(access_key\s*=\s*)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)\b([A-Z0-9_]*(?:token|secret|key|password|memory)[A-Z0-9_]*)(\s*[=:]\s*)\S+", r"\1\2[redacted]", text)
    text = re.sub(r"\b[A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD|MEMORY|ACCESS_KEY)[A-Z0-9_]*\b", "[redacted]", text)
    return text


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def load_dotenv(path: str | Path) -> None:
    p = Path(path)
    try:
        if not p.exists():
            return
        lines = p.read_text(errors="replace").splitlines()
    except OSError as exc:
        LOG.warning("skip dotenv %s: %s", p, exc)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, fallback: str = "") -> str:
    return os.environ.get(name) or fallback


def int_env(name: str, fallback: int) -> int:
    try:
        return int(env(name, str(fallback)))
    except ValueError:
        return fallback


def normalize_vk_message(payload: dict[str, Any]) -> dict[str, Any]:
    obj = payload.get("object") or {}
    message = obj.get("message") or obj
    peer_id = str(message.get("peer_id") or message.get("user_id") or message.get("from_id") or "")
    from_id = str(message.get("from_id") or message.get("user_id") or peer_id or "")
    text = str(message.get("text") or message.get("body") or "").strip()
    message_id = str(message.get("id") or message.get("conversation_message_id") or payload.get("event_id") or "")
    attachments = message.get("attachments") if isinstance(message.get("attachments"), list) else []
    return {
        "message": message,
        "peer_id": peer_id,
        "from_id": from_id,
        "text": text,
        "message_id": message_id,
        "attachments": attachments,
    }


def truthy_env(name: str) -> bool:
    return env(name).strip().lower() in {"1", "true", "yes", "on"}


def env_id_set(name: str) -> set[str]:
    raw = env(name)
    return {item.strip() for item in raw.split(",") if item.strip()}


def _load_policy_json() -> dict[str, Any]:
    raw = env("VK_POLICY_JSON").strip()
    if raw:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as exc:
            LOG.warning("invalid VK_POLICY_JSON: %s", exc)
            return {}
    policy_file = env("VK_POLICY_FILE").strip()
    if policy_file:
        try:
            data = json.loads(Path(policy_file).read_text(errors="replace"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("invalid VK_POLICY_FILE %s: %s", policy_file, exc)
            return {}
    return {}


def policy_config() -> dict[str, Any]:
    return _load_policy_json()


def _policy_role_ids(config: dict[str, Any], role: str) -> set[str]:
    roles = config.get("roles") if isinstance(config.get("roles"), dict) else {}
    spec = roles.get(role) if isinstance(roles.get(role), dict) else {}
    ids = spec.get("ids") or []
    return {str(item).strip() for item in ids if str(item).strip()}


def _is_group_peer(peer_id: str) -> bool:
    try:
        return int(peer_id) >= 2_000_000_000
    except ValueError:
        return False


def allowed_vk_users() -> set[str]:
    return env_id_set("VK_ALLOWED_USERS")


def owner_vk_users() -> set[str]:
    config = policy_config()
    if config:
        return _policy_role_ids(config, "owner")
    # VK_ALLOWED_USERS is the legacy owner allowlist; once VK_OWNER_ID is set, it is authoritative.
    explicit = env_id_set("VK_OWNER_ID")
    return explicit or allowed_vk_users()


def trusted_vk_users() -> set[str]:
    config = policy_config()
    if config:
        return _policy_role_ids(config, "trusted")
    return env_id_set("VK_TRUSTED_USERS")


def blocked_vk_users() -> set[str]:
    config = policy_config()
    if config:
        return _policy_role_ids(config, "blocked")
    return env_id_set("VK_BLOCKED_USERS")


def resolve_role(vk: dict[str, Any]) -> str:
    config = policy_config()
    sender = str(vk.get("from_id") or "")
    if sender in blocked_vk_users():
        return "blocked"
    if sender in owner_vk_users():
        return "owner"
    if sender in trusted_vk_users() or (not config and truthy_env("VK_ALLOW_ALL_USERS")):
        return "trusted"
    if config and _is_group_peer(str(vk.get("peer_id") or "")):
        return "group_chat"
    return "public"


OWNER_COMMANDS = {"trace", "pending", "approve", "reject", "reply"}


def parse_owner_command(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    parts = stripped[1:].split()
    if not parts:
        return None
    command = parts[0].lower()
    if command not in OWNER_COMMANDS:
        return None
    return {"command": command, "command_args": parts[1:]}


def is_owner_command(text: str) -> bool:
    return parse_owner_command(text) is not None


def emergency_lockdown_enabled() -> bool:
    return truthy_env("VK_EMERGENCY_LOCKDOWN") or truthy_env("VK_LOCKDOWN")


def _configured_rule(config: dict[str, Any], role: str) -> dict[str, Any] | None:
    rules = config.get("rules") if isinstance(config.get("rules"), dict) else {}
    rule = rules.get(role)
    if not isinstance(rule, dict):
        return None
    action = str(rule.get("action") or "deny")
    return {
        "role": role,
        "action": action,
        "hermes_allowed": bool(rule.get("hermes_allowed", action == "reply")),
        "reason": str(rule.get("reason") or f"configured_{role}_{action}"),
    }


def _group_mention_required_decision(config: dict[str, Any], vk: dict[str, Any]) -> dict[str, Any] | None:
    group_cfg = config.get("group_chats") if isinstance(config.get("group_chats"), dict) else {}
    if not group_cfg.get("require_mention"):
        return None
    mentions = [str(item).lower() for item in (group_cfg.get("mentions") or []) if str(item).strip()]
    text = str(vk.get("text") or "").lower()
    if mentions and not any(mention in text for mention in mentions):
        return {"role": "group_chat", "action": "deny", "hermes_allowed": False, "reason": "group_mention_required"}
    return None


def decide_policy(vk: dict[str, Any]) -> dict[str, Any]:
    config = policy_config()
    role = resolve_role(vk)
    text = str(vk.get("text") or "")
    command = parse_owner_command(text)
    if role == "blocked":
        return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "blocked_user"}
    if emergency_lockdown_enabled() and role != "owner":
        return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "emergency_lockdown"}
    if role == "group_chat":
        mention_decision = _group_mention_required_decision(config, vk)
        if mention_decision:
            return mention_decision
    if command:
        if role == "owner":
            return {"role": role, "action": "owner_command", "hermes_allowed": False, "reason": "owner_command", **command}
        return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "owner_command_requires_owner", **command}
    configured = _configured_rule(config, role) if config else None
    if configured:
        return configured
    if role in {"owner", "trusted"}:
        return {"role": role, "action": "reply", "hermes_allowed": True, "reason": "allowed_user"}
    if role == "group_chat":
        return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "group_default_deny"}
    if truthy_env("VK_PUBLIC_HANDOFF"):
        return {"role": role, "action": "handoff", "hermes_allowed": False, "reason": "public_handoff"}
    return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "public_default_deny"}


def is_authorized(vk: dict[str, Any]) -> bool:
    """Return True when the VK sender may reach Hermes tools."""
    return bool(decide_policy(vk).get("hermes_allowed"))


def unauthorized_reply_text() -> str:
    return env("VK_UNAUTHORIZED_REPLY", "Бот приватный. Доступ к Hermes Agent ограничен.").strip()


def unauthorized_mode() -> str:
    mode = env("VK_UNAUTHORIZED_MODE", "reply").strip().lower()
    return mode if mode in {"ignore", "reply", "notify_owner", "reply_and_notify"} else "reply"


def owner_notification_peer_id() -> str:
    explicit = env("VK_OWNER_PEER_ID").strip()
    if explicit:
        return explicit
    owners = sorted(owner_vk_users())
    return owners[0] if owners else ""


def redact_notification_text(text: str) -> str:
    return redact_secrets(text)


def redact_for_logs(value: Any) -> Any:
    if isinstance(value, str):
        return redact_notification_text(value)
    if isinstance(value, dict):
        return {str(k): redact_for_logs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_for_logs(item) for item in value]
    return value
def configure_logging() -> None:
    level = getattr(logging, env("LOG_LEVEL", "INFO").upper(), logging.INFO)
    if env("LOG_FORMAT").strip().lower() == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        logging.basicConfig(level=level, handlers=[handler], force=True)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )


def format_owner_unauthorized_notification(vk: dict[str, Any], trace_id: str, decision: dict[str, Any]) -> str:
    preview = redact_notification_text(str(vk.get("text") or "").replace("\n", " "))[:240]
    attachment_types = ",".join([str(a.get("type")) for a in vk.get("attachments", []) if isinstance(a, dict) and a.get("type")]) or "none"
    return "\n".join([
        "Unauthorized VK message",
        f"trace={trace_id}",
        f"from={vk.get('from_id', '')}",
        f"peer={vk.get('peer_id', '')}",
        f"role={decision.get('role', '')}",
        f"decision={decision.get('action', '')}",
        f"reason={decision.get('reason', '')}",
        f"date={vk.get('message', {}).get('date') or ''}",
        f"attachments={attachment_types}",
        f"text={preview}",
    ])


def notify_owner_about_unauthorized(vk: dict[str, Any], trace_id: str, decision: dict[str, Any]) -> bool:
    peer_id = owner_notification_peer_id()
    if not peer_id:
        LOG.warning("owner notification skipped trace_id=%s reason=missing_owner_peer_id", trace_id)
        return False
    reply_vk(peer_id, format_owner_unauthorized_notification(vk, trace_id, decision), trace_id=trace_id)
    return True


def shadow_mode_enabled() -> bool:
    return truthy_env("VK_SHADOW_MODE")


def format_owner_shadow_notification(vk: dict[str, Any], trace_id: str, decision: dict[str, Any], answer: str) -> str:
    preview = redact_notification_text(str(answer).replace("\n", " "))[:500]
    return "\n".join([
        "Shadow VK answer",
        f"trace={trace_id}",
        f"from={vk.get('from_id', '')}",
        f"peer={vk.get('peer_id', '')}",
        f"role={decision.get('role', '')}",
        f"decision={decision.get('action', '')}",
        f"reason={decision.get('reason', '')}",
        f"proposed={preview}",
    ])


def notify_owner_about_shadow(vk: dict[str, Any], trace_id: str, decision: dict[str, Any], answer: str) -> bool:
    peer_id = owner_notification_peer_id()
    if not peer_id:
        LOG.warning("shadow notification skipped trace_id=%s reason=missing_owner_peer_id", trace_id)
        return False
    reply_vk(peer_id, format_owner_shadow_notification(vk, trace_id, decision, answer), trace_id=trace_id)
    return True


def is_help_command(text: str) -> bool:
    return text.strip().lower() in {"начать", "/start", "помощь", "/help"}


def help_text() -> str:
    return "\n".join([
        "Привет! Я VK-канал связи с Hermes Agent.",
        "",
        "Напиши обычное сообщение — я передам его агенту и верну ответ сюда.",
        "Команды: /help, помощь, /start, начать.",
    ])


def build_hermes_input(vk: dict[str, Any]) -> str:
    attachments = vk["attachments"]
    attachment_summary = ""
    if attachments:
        types = ", ".join([str(a.get("type")) for a in attachments if isinstance(a, dict) and a.get("type")])
        attachment_summary = f"\n\n[VK attachments: {types}]"
    return f"{vk['text'] or '[empty VK message]'}{attachment_summary}"


def hermes_instructions(vk: dict[str, Any]) -> str:
    return "\n".join([
        "Ты отвечаешь пользователю через VK community messages.",
        "Пиши на русском, кратко и по делу, если пользователь не просит подробно.",
        "Не используй Telegram MarkdownV2; VK поддерживает обычный текст и ссылки.",
        f"VK peer_id: {vk['peer_id']}; VK from_id: {vk['from_id']}.",
    ])


def extract_hermes_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    output = data.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"} and content.get("text"):
                    parts.append(str(content["text"]))
        if parts:
            return "\n".join(parts).strip()
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        if msg.get("content"):
            return str(msg["content"]).strip()
    return ""


def call_hermes(vk: dict[str, Any]) -> str:
    base = env("HERMES_API_BASE", "http://127.0.0.1:8642").rstrip("/")
    key = env("HERMES_API_KEY") or env("API_SERVER_KEY")
    if not key:
        raise RuntimeError("HERMES_API_KEY or API_SERVER_KEY is required")
    payload = {
        "model": env("HERMES_MODEL", "hermes-agent"),
        "input": build_hermes_input(vk),
        "instructions": hermes_instructions(vk),
        "conversation": f"vk:{vk['peer_id']}",
        "store": True,
    }
    res = requests.post(
        f"{base}/v1/responses",
        headers={
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "x-hermes-session-key": f"vk:{vk['peer_id']}",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=int_env("HERMES_TIMEOUT_MS", 120000) / 1000,
    )
    text = res.text
    try:
        data = res.json()
    except Exception:
        data = {"raw": text}
    if not res.ok:
        raise RuntimeError(f"Hermes API HTTP {res.status_code}: {text[:500]}")
    answer = extract_hermes_text(data)
    if not answer:
        raise RuntimeError("Hermes API returned no assistant text")
    return answer


def _split_text_for_vk(text: str, max_chars: int) -> list[str]:
    remaining = text.strip() or "Готово."
    chunks: list[str] = []
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def split_for_vk(text: str) -> list[str]:
    prefix = env("VK_REPLY_PREFIX", "")
    return _split_text_for_vk(f"{prefix}{text or ''}", VK_MAX_MESSAGE_CHARS)


def stable_random_id(trace_id: str, chunk_index: int) -> int:
    raw = f"{trace_id}:{chunk_index}".encode("utf-8")
    # VK accepts signed 32-bit integers except zero; keep it positive and stable.
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) % 2_147_483_647 or 1


def build_vk_outbound_messages(peer_id: str, text: str, *, trace_id: str) -> list[dict[str, Any]]:
    prefix = env("VK_REPLY_PREFIX", "")
    base_text = f"{prefix}{text or ''}".strip() or "Готово."
    chunks = _split_text_for_vk(base_text, VK_MAX_MESSAGE_CHARS)
    if len(chunks) > 1:
        numbered: list[str] = []
        total = len(_split_text_for_vk(base_text, VK_MAX_MESSAGE_CHARS - 16))
        chunks = _split_text_for_vk(base_text, VK_MAX_MESSAGE_CHARS - len(f"[{total}/{total}]\n"))
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            numbered.append(f"[{index}/{total}]\n{chunk}")
        chunks = numbered
    return [
        {
            "peer_id": str(peer_id),
            "message": chunk,
            "random_id": stable_random_id(trace_id, index),
        }
        for index, chunk in enumerate(chunks)
    ]


def send_vk_message(peer_id: str, message: str, *, random_id: int | None = None) -> None:
    token = env("VK_GROUP_TOKEN")
    if not token:
        raise RuntimeError("VK_GROUP_TOKEN is required")
    data = {
        "access_token": token,
        "v": env("VK_API_VERSION", "5.199"),
        "peer_id": str(peer_id),
        "random_id": str(random_id if random_id is not None else random.randint(1, 2_147_483_647)),
        "message": message,
    }
    res = requests.post("https://api.vk.com/method/messages.send", data=data, timeout=30)
    payload = res.json()
    if not res.ok or payload.get("error"):
        raise RuntimeError(f"VK messages.send failed: HTTP {res.status_code} {str(payload.get('error') or payload)[:500]}")


def reply_vk(peer_id: str, text: str, *, trace_id: str | None = None) -> None:
    actual_trace_id = trace_id or f"vk-send-{hashlib.sha256(f'{peer_id}:{text}'.encode('utf-8')).hexdigest()[:16]}"
    for outbound in build_vk_outbound_messages(peer_id, text, trace_id=actual_trace_id):
        send_vk_message(outbound["peer_id"], outbound["message"], random_id=outbound["random_id"])


def event_fingerprint(payload: dict[str, Any]) -> str:
    vk = normalize_vk_message(payload)
    raw = "|".join([
        str(payload.get("event_id") or ""),
        str(payload.get("group_id") or ""),
        vk["peer_id"],
        vk["message_id"],
        vk["text"],
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DedupStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS processed (key TEXT PRIMARY KEY, created_at REAL NOT NULL)")
        self.db.commit()

    def seen(self, key: str) -> bool:
        row = self.db.execute("SELECT 1 FROM processed WHERE key = ?", (key,)).fetchone()
        return row is not None

    def mark(self, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO processed (key, created_at) VALUES (?, ?)", (key, time.time()))
        self.db.commit()

    def cleanup(self, max_age_days: int = 14) -> None:
        cutoff = time.time() - max_age_days * 86400
        self.db.execute("DELETE FROM processed WHERE created_at < ?", (cutoff,))
        self.db.commit()


class TraceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS trace_records ("
            "trace_id TEXT PRIMARY KEY, created_at REAL NOT NULL, record_json TEXT NOT NULL)"
        )
        self.db.commit()

    def put(self, record: dict[str, Any]) -> None:
        payload = dict(record)
        payload.setdefault("created_at", time.time())
        self.db.execute(
            "INSERT OR REPLACE INTO trace_records (trace_id, created_at, record_json) VALUES (?, ?, ?)",
            (payload["trace_id"], float(payload["created_at"]), json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        self.db.commit()

    def get(self, trace_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT record_json FROM trace_records WHERE trace_id = ?", (trace_id,)).fetchone()
        if not row:
            return None
        return json.loads(row[0])


class ReviewStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS review_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "status TEXT NOT NULL, kind TEXT NOT NULL, trace_id TEXT NOT NULL, peer_id TEXT NOT NULL, "
            "from_id TEXT NOT NULL, text TEXT NOT NULL, item_json TEXT NOT NULL)"
        )
        self.db.commit()

    def create_item(
        self,
        *,
        kind: str,
        trace_id: str,
        peer_id: str,
        from_id: str,
        text: str,
        status: str = "pending",
    ) -> dict[str, Any]:
        now = time.time()
        item = {
            "id": 0,
            "created_at": now,
            "updated_at": now,
            "status": status,
            "kind": kind,
            "trace_id": trace_id,
            "peer_id": str(peer_id),
            "from_id": str(from_id),
            "text": text,
        }
        cur = self.db.execute(
            "INSERT INTO review_items (created_at, updated_at, status, kind, trace_id, peer_id, from_id, text, item_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, status, kind, trace_id, str(peer_id), str(from_id), text, "{}"),
        )
        item["id"] = int(cur.lastrowid)
        self.db.execute("UPDATE review_items SET item_json = ? WHERE id = ?", (json.dumps(item, ensure_ascii=False, sort_keys=True), item["id"]))
        self.db.commit()
        return item

    def _row_to_item(self, row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return json.loads(row[-1])

    def get(self, item_id: int | str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT item_json FROM review_items WHERE id = ?", (int(item_id),)).fetchone()
        return self._row_to_item(row)

    def list_pending(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT item_json FROM review_items WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_status(self, item_id: int | str, status: str) -> dict[str, Any]:
        item = self.get(item_id)
        if not item:
            raise KeyError(f"review item #{item_id} not found")
        item["status"] = status
        item["updated_at"] = time.time()
        self.db.execute(
            "UPDATE review_items SET status = ?, updated_at = ?, item_json = ? WHERE id = ?",
            (status, item["updated_at"], json.dumps(item, ensure_ascii=False, sort_keys=True), int(item_id)),
        )
        self.db.commit()
        return item


def role_for_vk(vk: dict[str, Any]) -> str:
    return resolve_role(vk)


def trace_id_for_payload(payload: dict[str, Any]) -> str:
    return f"vk-{event_fingerprint(payload)[:16]}"


def raw_event_hash(payload: dict[str, Any]) -> str:
    """Return a stable hash of the raw inbound event without storing raw data."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summarize_attachments(attachments: list[Any]) -> list[dict[str, str]]:
    """Return a safe attachment summary without nested VK access keys/tokens."""
    summary: list[dict[str, str]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_type = attachment.get("type")
        if attachment_type:
            summary.append({"type": str(attachment_type)})
    return summary


def build_event_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the internal traceable event envelope used by fake and real paths."""
    vk = normalize_vk_message(payload)
    message = vk["message"]
    attachments = summarize_attachments(vk["attachments"])
    return {
        "trace_id": trace_id_for_payload(payload),
        "event_type": str(payload.get("type") or ""),
        "message_timestamp": message.get("date") or payload.get("date"),
        "vk": {
            "group_id": str(payload.get("group_id") or ""),
            "event_id": str(payload.get("event_id") or ""),
            "peer_id": vk["peer_id"],
            "from_id": vk["from_id"],
            "message_id": vk["message_id"],
            "conversation_message_id": str(message.get("conversation_message_id") or ""),
        },
        "text": vk["text"],
        "attachment_types": [item["type"] for item in attachments],
        "attachments": attachments,
        "raw_event_sha256": raw_event_hash(payload),
    }


def new_trace_record(envelope: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": envelope["trace_id"],
        "envelope": envelope,
        "role": str(policy.get("role") or ""),
        "decision": str(policy.get("action") or ""),
        "reason": str(policy.get("reason") or ""),
        "hermes_status": "not_called",
        "vk_status": "not_sent",
        "error": "",
    }


def save_trace(trace_store: TraceStore | None, record: dict[str, Any] | None) -> None:
    if trace_store and record:
        trace_store.put(record)


def format_trace_record(record: dict[str, Any]) -> str:
    return " ".join([
        f"Trace {record['trace_id']}:",
        f"role={record.get('role', '')}",
        f"decision={record.get('decision', '')}",
        f"hermes={record.get('hermes_status', '')}",
        f"vk={record.get('vk_status', '')}",
        f"error={record.get('error', '')}" if record.get("error") else "error=none",
    ])


def format_pending_items(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No pending review items."
    lines = ["Pending review items:"]
    for item in items:
        text = str(item.get("text") or "").replace("\n", " ")[:80]
        lines.append(f"#{item['id']} {item.get('kind', '')} trace={item.get('trace_id', '')} from={item.get('from_id', '')}: {text}")
    return "\n".join(lines)


def handle_owner_command(
    vk: dict[str, Any],
    decision: dict[str, Any],
    trace_store: TraceStore | None,
    review_store: ReviewStore | None = None,
) -> str:
    command = str(decision.get("command") or "unknown")
    args = [str(arg) for arg in decision.get("command_args") or []]
    if command == "trace":
        if not args:
            return "Usage: !trace <trace_id>"
        if not trace_store:
            return "Trace store is not configured."
        record = trace_store.get(args[0])
        if not record:
            return f"Trace {args[0]} not found."
        return format_trace_record(record)
    if command == "pending":
        if not review_store:
            return "Review store is not configured."
        return format_pending_items(review_store.list_pending())
    if command in {"approve", "reject"}:
        if not args:
            return f"Usage: !{command} <review_id>"
        if not review_store:
            return "Review store is not configured."
        try:
            item = review_store.update_status(args[0], "approved" if command == "approve" else "rejected")
        except (KeyError, ValueError):
            return f"Review item #{args[0]} not found."
        return f"Review item #{item['id']} {item['status']}."
    if command == "reply":
        if len(args) < 2:
            return "Usage: !reply <review_id> <text>"
        if not review_store:
            return "Review store is not configured."
        item_id = args[0]
        reply_text = " ".join(args[1:]).strip()
        try:
            item = review_store.get(item_id)
        except ValueError:
            item = None
        if not item:
            return f"Review item #{item_id} not found."
        reply_vk(str(item["peer_id"]), reply_text)
        updated = review_store.update_status(item_id, "replied")
        return f"Manual reply sent for review item #{updated['id']}."
    return f"Owner command !{command} accepted, but it is not implemented yet."


def process_payload(
    payload: dict[str, Any],
    dedup: DedupStore,
    trace_store: TraceStore | None = None,
    review_store: ReviewStore | None = None,
) -> None:
    vk = normalize_vk_message(payload)
    trace_id = trace_id_for_payload(payload)
    if not vk["peer_id"]:
        LOG.info("skip payload without peer_id trace_id=%s", trace_id)
        return
    if vk["message"].get("out"):
        LOG.info("skip outgoing VK message trace_id=%s", trace_id)
        return

    key = event_fingerprint(payload)
    if dedup.seen(key):
        LOG.info("skip duplicate event %s trace_id=%s", key[:12], trace_id)
        return

    decision = decide_policy(vk)
    role = str(decision["role"])
    action = str(decision["action"])
    envelope = build_event_envelope(payload)
    trace_record = new_trace_record(envelope, decision)
    log_level = logging.WARNING if action in {"deny", "handoff"} else logging.INFO
    LOG.log(
        log_level,
        "policy decision trace_id=%s role=%s decision=%s reason=%s from_id=%s peer_id=%s",
        trace_id,
        role,
        action,
        decision.get("reason", ""),
        vk["from_id"],
        vk["peer_id"],
    )

    try:
        if action in {"deny", "handoff"}:
            if action == "handoff" and review_store:
                review_store.create_item(
                    kind="public_question",
                    trace_id=trace_id,
                    peer_id=vk["peer_id"],
                    from_id=vk["from_id"],
                    text=vk["text"],
                )
            mode = unauthorized_mode()
            sent_any = False
            if mode in {"reply", "reply_and_notify"}:
                reply = unauthorized_reply_text()
                if reply:
                    reply_vk(vk["peer_id"], reply, trace_id=trace_id)
                    sent_any = True
            if mode in {"notify_owner", "reply_and_notify"}:
                sent_any = notify_owner_about_unauthorized(vk, trace_id, decision) or sent_any
            if sent_any:
                trace_record["vk_status"] = "sent"
            save_trace(trace_store, trace_record)
            dedup.mark(key)
            return

        if action == "owner_command":
            reply_vk(vk["peer_id"], handle_owner_command(vk, decision, trace_store, review_store=review_store), trace_id=trace_id)
            trace_record["vk_status"] = "sent"
            save_trace(trace_store, trace_record)
            dedup.mark(key)
            return

        if is_help_command(vk["text"]):
            reply_vk(vk["peer_id"], help_text(), trace_id=trace_id)
            trace_record["vk_status"] = "sent"
        else:
            try:
                answer = call_hermes(vk)
                trace_record["hermes_status"] = "ok"
            except Exception as exc:
                trace_record["hermes_status"] = "error"
                trace_record["error"] = str(exc)[:500]
                save_trace(trace_store, trace_record)
                raise
            if shadow_mode_enabled():
                notify_owner_about_shadow(vk, trace_id, decision, answer)
                trace_record["vk_status"] = "shadow_not_sent"
            else:
                reply_vk(vk["peer_id"], answer, trace_id=trace_id)
                trace_record["vk_status"] = "sent"
        save_trace(trace_store, trace_record)
        dedup.mark(key)
    except Exception as exc:
        if not trace_record.get("error"):
            trace_record["error"] = str(exc)[:500]
        if trace_record.get("vk_status") == "not_sent" and trace_record.get("hermes_status") != "error":
            trace_record["vk_status"] = "error"
        save_trace(trace_store, trace_record)
        raise


def run_fake_event(
    fixture_path: str | Path,
    *,
    fake_hermes_answer: str = "Fake Hermes response.",
    dedup_path: str | Path = ":memory:",
) -> dict[str, Any]:
    payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    vk = normalize_vk_message(payload)
    outbound: list[dict[str, Any]] = []
    hermes_called = False

    def fake_call_hermes(_: dict[str, Any]) -> str:
        nonlocal hermes_called
        hermes_called = True
        return fake_hermes_answer

    def fake_reply_vk(peer_id: str, text: str, *, trace_id: str | None = None) -> None:
        actual_trace_id = trace_id or trace_id_for_payload(payload)
        outbound.extend(build_vk_outbound_messages(peer_id, text, trace_id=actual_trace_id))

    original_call_hermes = globals()["call_hermes"]
    original_reply_vk = globals()["reply_vk"]
    globals()["call_hermes"] = fake_call_hermes
    globals()["reply_vk"] = fake_reply_vk
    try:
        dedup = DedupStore(dedup_path)
        process_payload(payload, dedup)
    finally:
        globals()["call_hermes"] = original_call_hermes
        globals()["reply_vk"] = original_reply_vk

    if not vk["peer_id"]:
        policy = {"role": role_for_vk(vk), "action": "skip", "hermes_allowed": False, "reason": "missing_peer_id"}
    elif vk["message"].get("out"):
        policy = {"role": role_for_vk(vk), "action": "skip", "hermes_allowed": False, "reason": "outgoing_message"}
    else:
        policy = decide_policy(vk)

    envelope = build_event_envelope(payload)
    return {
        "trace_id": envelope["trace_id"],
        "envelope": envelope,
        "role": policy["role"],
        "policy_decision": policy["action"],
        "policy": policy,
        "hermes_called": hermes_called,
        "outbound_messages": outbound,
    }


def run_replay_fixture(
    fixture_path: str | Path,
    *,
    fake_hermes_answer: str = "Fake Hermes response.",
    dedup_path: str | Path = ":memory:",
) -> dict[str, Any]:
    fixture = Path(fixture_path)
    try:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"fixture": str(fixture), "status": "error", "ok": False, "error": f"failed to read fixture: {exc}"}

    simulate = str((payload.get("_replay") or {}).get("simulate") or "ok") if isinstance(payload, dict) else "ok"
    vk = normalize_vk_message(payload) if isinstance(payload, dict) else {"peer_id": "", "message": {}, "attachments": [], "text": ""}
    outbound: list[dict[str, Any]] = []
    hermes_called = False
    error = ""

    def fake_call_hermes(_: dict[str, Any]) -> str:
        nonlocal hermes_called
        hermes_called = True
        if simulate == "hermes_timeout":
            raise TimeoutError("simulated Hermes timeout")
        return fake_hermes_answer

    def fake_reply_vk(peer_id: str, text: str, *, trace_id: str | None = None) -> None:
        if simulate == "vk_send_failure":
            raise RuntimeError("simulated VK send failure")
        actual_trace_id = trace_id or (trace_id_for_payload(payload) if isinstance(payload, dict) else "replay")
        outbound.extend(build_vk_outbound_messages(peer_id, text, trace_id=actual_trace_id))

    original_call_hermes = globals()["call_hermes"]
    original_reply_vk = globals()["reply_vk"]
    globals()["call_hermes"] = fake_call_hermes
    globals()["reply_vk"] = fake_reply_vk
    trace_store = TraceStore(":memory:")
    duplicate_skipped = False
    try:
        if isinstance(payload, dict):
            dedup = DedupStore(dedup_path)
            try:
                process_payload(payload, dedup, trace_store=trace_store)
                if simulate == "duplicate":
                    first_count = len(outbound)
                    process_payload(payload, dedup, trace_store=trace_store)
                    duplicate_skipped = len(outbound) == first_count
            except Exception as exc:
                error = str(exc)[:500]
        else:
            error = "fixture root is not a JSON object"
    finally:
        globals()["call_hermes"] = original_call_hermes
        globals()["reply_vk"] = original_reply_vk

    if isinstance(payload, dict):
        if not vk["peer_id"]:
            policy = {"role": role_for_vk(vk), "action": "skip", "hermes_allowed": False, "reason": "missing_peer_id"}
        elif vk["message"].get("out"):
            policy = {"role": role_for_vk(vk), "action": "skip", "hermes_allowed": False, "reason": "outgoing_message"}
        else:
            policy = decide_policy(vk)
        envelope = build_event_envelope(payload)
        trace_id = envelope["trace_id"]
    else:
        policy = {"role": "public", "action": "skip", "hermes_allowed": False, "reason": "invalid_fixture"}
        envelope = {}
        trace_id = ""

    return {
        "fixture": str(fixture),
        "status": "error" if error else "ok",
        "ok": True,
        "error": error,
        "trace_id": trace_id,
        "envelope": envelope,
        "role": policy["role"],
        "policy_decision": policy["action"],
        "policy": policy,
        "hermes_called": hermes_called,
        "outbound_messages": outbound,
        "duplicate_skipped": duplicate_skipped,
        "simulate": simulate,
    }


def run_replay(
    fixture_paths: list[str | Path],
    *,
    fake_hermes_answer: str = "Fake Hermes response.",
    dedup_path: str | Path = ":memory:",
) -> dict[str, Any]:
    results = [
        run_replay_fixture(path, fake_hermes_answer=fake_hermes_answer, dedup_path=dedup_path)
        for path in fixture_paths
    ]
    return {"ok": all(result.get("ok") for result in results), "results": results}


def format_replay_report(report: dict[str, Any]) -> str:
    lines = [f"Replay: {'OK' if report.get('ok') else 'FAIL'}"]
    for result in report.get("results") or []:
        fixture = Path(str(result.get("fixture") or "")).name
        line = " ".join([
            f"[{result.get('status')}] {fixture}",
            f"decision={result.get('policy_decision')}",
            f"role={result.get('role')}",
            f"hermes_called={result.get('hermes_called')}",
            f"outbound={len(result.get('outbound_messages') or [])}",
            f"duplicate_skipped={result.get('duplicate_skipped')}",
            f"trace={result.get('trace_id')}",
        ])
        if result.get("error"):
            line = f"{line} error={result.get('error')}"
        outbound = (result.get("outbound_messages") or [])[-1] if result.get("outbound_messages") else None
        if outbound:
            line = f"{line} final_outbound={json.dumps(outbound, ensure_ascii=False, separators=(',', ':'))}"
        lines.append(line)
    return "\n".join(lines)


def smoke_check(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    check: dict[str, Any] = {"name": name, "status": "ok" if ok else "fail", "detail": detail}
    check.update(extra)
    return check


def run_smoke(
    *,
    fixture_dir: str | Path,
    state_dir: str | Path,
    fake_hermes_answer: str = "Smoke fake Hermes response.",
) -> dict[str, Any]:
    fixture_root = Path(fixture_dir)
    state_root = Path(state_dir)
    state_root.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    cases = [
        ("owner_fake_e2e", fixture_root / "message_new_owner.json", "reply", True),
        ("public_fake_e2e", fixture_root / "message_new_unknown.json", "deny", False),
    ]
    for name, fixture, expected_decision, expected_hermes_called in cases:
        if not fixture.exists():
            checks.append(smoke_check(name, False, f"missing fixture {fixture}"))
            continue
        try:
            result = run_fake_event(
                fixture,
                fake_hermes_answer=fake_hermes_answer,
                dedup_path=":memory:",
            )
            ok = (
                result["policy_decision"] == expected_decision
                and bool(result["outbound_messages"])
                and result["hermes_called"] is expected_hermes_called
            )
            detail = f"decision={result['policy_decision']} outbound={len(result['outbound_messages'])} hermes_called={result['hermes_called']}"
            final_outbound = result["outbound_messages"][-1] if result["outbound_messages"] else None
            checks.append(smoke_check(
                name,
                ok,
                detail,
                trace_id=result["trace_id"],
                outbound_count=len(result["outbound_messages"]),
                hermes_called=result["hermes_called"],
                final_outbound_payload=final_outbound,
            ))
        except Exception as exc:
            checks.append(smoke_check(name, False, str(exc)))
    return {"ok": all(check["status"] == "ok" for check in checks), "checks": checks}


def format_smoke_report(report: dict[str, Any]) -> str:
    lines = [f"Smoke: {'OK' if report.get('ok') else 'FAIL'}"]
    for check in report.get("checks") or []:
        line = f"[{check.get('status')}] {check.get('name')}: {check.get('detail')}"
        if check.get("trace_id"):
            line = f"{line} trace={check.get('trace_id')}"
        if check.get("final_outbound_payload"):
            outbound_json = json.dumps(check["final_outbound_payload"], ensure_ascii=False, separators=(",", ":"))
            line = f"{line} final_outbound={outbound_json}"
        lines.append(line)
    return "\n".join(lines)


def doctor_check(name: str, ok: bool, detail: str, hint: str = "", status: str | None = None) -> dict[str, str]:
    check = {"name": name, "status": status or ("ok" if ok else "fail"), "detail": detail}
    if hint:
        check["hint"] = hint
    return check


def _redacted_configured(value: str) -> str:
    return "configured ***" if value else "missing"


def run_doctor(
    *,
    dedup_db: str | Path,
    trace_db: str | Path,
    review_db: str | Path,
    check_network: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    checks.append(doctor_check(
        "QUEUE_URL",
        bool(env("QUEUE_URL")),
        "configured" if env("QUEUE_URL") else "missing QUEUE_URL",
        "Set QUEUE_URL to the Yandex Message Queue URL used by the worker.",
    ))
    checks.append(doctor_check(
        "VK_GROUP_TOKEN",
        bool(env("VK_GROUP_TOKEN")),
        _redacted_configured(env("VK_GROUP_TOKEN")),
        "Set VK_GROUP_TOKEN from VK community settings; never commit it.",
    ))
    has_owner = bool(owner_vk_users())
    checks.append(doctor_check(
        "OWNER_ALLOWLIST",
        has_owner,
        "configured" if has_owner else "missing VK_OWNER_ID or VK_ALLOWED_USERS",
        "Set VK_OWNER_ID to the maintainer VK user id before enabling the bridge.",
    ))
    has_hermes_key = bool(env("HERMES_API_KEY") or env("API_SERVER_KEY"))
    checks.append(doctor_check(
        "HERMES_API_KEY",
        has_hermes_key,
        _redacted_configured(env("HERMES_API_KEY") or env("API_SERVER_KEY")),
        "Set HERMES_API_KEY or API_SERVER_KEY for the local Hermes API server.",
    ))
    if truthy_env("VK_ALLOW_ALL_USERS"):
        allow_all_ok = truthy_env("VK_BRIDGE_TEST_MODE")
        checks.append(doctor_check(
            "VK_ALLOW_ALL_USERS",
            allow_all_ok,
            "enabled in explicit test mode" if allow_all_ok else "enabled outside explicit test mode",
            "Disable VK_ALLOW_ALL_USERS for public/semi-public communities; use VK_PUBLIC_HANDOFF instead.",
        ))
    else:
        checks.append(doctor_check("VK_ALLOW_ALL_USERS", True, "disabled"))
    if emergency_lockdown_enabled():
        checks.append(doctor_check("EMERGENCY_LOCKDOWN", True, "enabled: non-owner traffic is forced to deny"))
    else:
        checks.append(doctor_check("EMERGENCY_LOCKDOWN", True, "disabled"))
    base = env("HERMES_API_BASE", "http://127.0.0.1:8642").rstrip("/")
    if check_network:
        try:
            res = requests.get(f"{base}/health", timeout=3)
            checks.append(doctor_check(
                "HERMES_API_BASE",
                res.ok,
                f"GET /health HTTP {res.status_code}",
                "Start Hermes API server or fix HERMES_API_BASE.",
            ))
        except Exception as exc:
            checks.append(doctor_check(
                "HERMES_API_BASE",
                False,
                f"unreachable: {exc}",
                "Start Hermes API server and verify HERMES_API_BASE points to it.",
            ))
    else:
        checks.append(doctor_check("HERMES_API_BASE", bool(base), base or "missing", "Set HERMES_API_BASE, default is http://127.0.0.1:8642."))
    try:
        DedupStore(dedup_db)
        TraceStore(trace_db)
        ReviewStore(review_db)
        checks.append(doctor_check("STATE_DBS", True, "dedup/trace/review stores opened"))
    except Exception as exc:
        checks.append(doctor_check("STATE_DBS", False, str(exc), "Ensure state directory exists and is writable by the worker user."))
    return {"ok": all(check["status"] == "ok" for check in checks), "checks": checks}


def format_doctor_report(report: dict[str, Any]) -> str:
    lines = [f"Doctor: {'OK' if report.get('ok') else 'FAIL'}"]
    for check in report.get("checks") or []:
        line = f"[{check.get('status')}] {check.get('name')}: {check.get('detail')}"
        if check.get("hint") and check.get("status") in {"fail", "warn"}:
            line = f"{line} | hint: {check.get('hint')}"
        lines.append(line)
    return "\n".join(lines)



def health_component(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    component: dict[str, Any] = {"name": name, "status": status, "detail": redact_notification_text(str(detail))}
    component.update(extra)
    return component


def run_health(
    *,
    mode: str = "queue",
    dedup_db: str | Path,
    trace_db: str | Path,
    review_db: str | Path,
    check_network: bool = False,
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    normalized_mode = "long_poll" if mode in {"long_poll", "long-poll"} else "queue"
    components.append(health_component("worker", "ok", f"mode={normalized_mode}"))

    if normalized_mode == "queue":
        components.append(health_component(
            "queue",
            "ok" if env("QUEUE_URL") else "fail",
            "configured" if env("QUEUE_URL") else "missing QUEUE_URL",
        ))
    else:
        components.append(health_component("queue", "skip", "not used in long_poll mode"))

    has_hermes_key = bool(env("HERMES_API_KEY") or env("API_SERVER_KEY"))
    hermes_base = env("HERMES_API_BASE", "http://127.0.0.1:8642")
    components.append(health_component(
        "hermes",
        "ok" if has_hermes_key and hermes_base else "fail",
        f"base={hermes_base}; auth={'configured' if has_hermes_key else 'missing'}",
    ))

    has_vk_token = bool(env("VK_GROUP_TOKEN"))
    has_vk_group = bool(env("VK_GROUP_ID")) if normalized_mode == "long_poll" else True
    components.append(health_component(
        "vk",
        "ok" if has_vk_token and has_vk_group else "fail",
        "token/group configured" if has_vk_token and has_vk_group else "missing VK_GROUP_TOKEN or VK_GROUP_ID",
    ))

    has_owner = bool(owner_vk_users())
    components.append(health_component(
        "policy",
        "ok" if has_owner else "fail",
        "owner allowlist configured" if has_owner else "missing VK_OWNER_ID or VK_ALLOWED_USERS",
    ))

    try:
        DedupStore(dedup_db)
        TraceStore(trace_db)
        ReviewStore(review_db)
        components.append(health_component("state_dbs", "ok", "dedup/trace/review stores opened"))
    except Exception as exc:
        components.append(health_component("state_dbs", "fail", str(exc)))

    ok = all(component["status"] in {"ok", "skip"} for component in components)
    return {"ok": ok, "mode": normalized_mode, "components": components}


def format_health_report(report: dict[str, Any]) -> str:
    lines = [f"Health: {'OK' if report.get('ok') else 'FAIL'}"]
    for component in report.get("components") or []:
        lines.append(f"[{component.get('status')}] {component.get('name')}: {component.get('detail')}")
    return "\n".join(lines)


def sqs_client():
    return boto3.client(
        "sqs",
        endpoint_url=env("QUEUE_ENDPOINT", "https://message-queue.api.cloud.yandex.net"),
        region_name=env("AWS_REGION", "ru-central1"),
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def vk_api_version() -> str:
    return env("VK_API_VERSION", "5.199")


def vk_long_poll_server(session: Any) -> dict[str, str]:
    group_id = env("VK_GROUP_ID")
    token = env("VK_GROUP_TOKEN")
    if not group_id:
        raise RuntimeError("VK_GROUP_ID is required for Long Poll mode")
    if not token:
        raise RuntimeError("VK_GROUP_TOKEN is required for Long Poll mode")
    res = session.post(
        "https://api.vk.com/method/groups.getLongPollServer",
        data={"group_id": group_id, "access_token": token, "v": vk_api_version()},
        timeout=int_env("VK_API_TIMEOUT_SECONDS", 10),
    )
    if hasattr(res, "raise_for_status"):
        res.raise_for_status()
    data = res.json()
    if data.get("error"):
        raise RuntimeError(f"VK Long Poll server error: {data['error']}")
    server = data.get("response") or {}
    missing = [name for name in ("key", "server", "ts") if not server.get(name)]
    if missing:
        raise RuntimeError(f"VK Long Poll server response missing: {', '.join(missing)}")
    return {"key": str(server["key"]), "server": str(server["server"]), "ts": str(server["ts"])}


def vk_long_poll_update_payload(update: dict[str, Any]) -> dict[str, Any] | None:
    if update.get("type") != "message_new":
        return None
    obj = update.get("object") or {}
    message = obj.get("message") or obj
    message_id = message.get("id") or message.get("conversation_message_id") or "unknown"
    try:
        group_id: int | str = int(env("VK_GROUP_ID"))
    except ValueError:
        group_id = env("VK_GROUP_ID")
    return {
        "type": "message_new",
        "group_id": group_id,
        "event_id": f"lp-{message_id}",
        "object": obj,
    }


def run_long_poll_once(
    session: Any,
    dedup: DedupStore,
    *,
    state: dict[str, Any] | None = None,
    trace_store: TraceStore | None = None,
    review_store: ReviewStore | None = None,
) -> dict[str, Any]:
    current = dict(state or {})
    if not current.get("key") or not current.get("server") or not current.get("ts"):
        current.update(vk_long_poll_server(session))
    wait = int_env("VK_LONG_POLL_WAIT_SECONDS", 25)
    res = session.get(
        current["server"],
        params={"act": "a_check", "key": current["key"], "ts": current["ts"], "wait": wait},
        timeout=wait + int_env("VK_API_TIMEOUT_SECONDS", 10),
    )
    if hasattr(res, "raise_for_status"):
        res.raise_for_status()
    data = res.json()
    if data.get("failed"):
        current.update(vk_long_poll_server(session))
        current["processed"] = 0
        current["refresh"] = True
        return current
    if data.get("ts"):
        current["ts"] = str(data["ts"])
    processed = 0
    for update in data.get("updates") or []:
        if not isinstance(update, dict):
            continue
        payload = vk_long_poll_update_payload(update)
        if not payload:
            continue
        process_payload(payload, dedup, trace_store=trace_store, review_store=review_store)
        processed += 1
    current["processed"] = processed
    current["refresh"] = False
    return current


def run_long_poll_loop(
    *,
    dedup_path: str | Path,
    trace_path: str | Path,
    review_path: str | Path,
    once: bool = False,
    session: Any | None = None,
) -> int:
    dedup = DedupStore(dedup_path)
    trace_store = TraceStore(trace_path)
    review_store = ReviewStore(review_path)
    session = session or requests.Session()
    state: dict[str, Any] | None = None
    cycles = 0
    while True:
        state = run_long_poll_once(session, dedup, state=state, trace_store=trace_store, review_store=review_store)
        cycles += 1
        LOG.info("long poll cycle processed=%s ts=%s refresh=%s", state.get("processed"), state.get("ts"), state.get("refresh"))
        if once:
            return cycles


def run_once(
    client: Any,
    queue_url: str,
    dedup: DedupStore,
    trace_store: TraceStore | None = None,
    review_store: ReviewStore | None = None,
) -> int:
    res = client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=int_env("QUEUE_MAX_MESSAGES", 1),
        WaitTimeSeconds=int_env("QUEUE_WAIT_TIME_SECONDS", 20),
        VisibilityTimeout=int_env("QUEUE_VISIBILITY_TIMEOUT", 300),
    )
    messages = res.get("Messages") or []
    for msg in messages:
        receipt = msg["ReceiptHandle"]
        body = json.loads(msg.get("Body") or "{}")
        payload = body.get("payload") if isinstance(body, dict) and "payload" in body else body
        if not isinstance(payload, dict):
            LOG.warning("invalid queue message body: %r", body)
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            continue
        try:
            process_payload(payload, dedup, trace_store=trace_store, review_store=review_store)
        except Exception:
            LOG.exception("processing failed; leaving message for retry")
            continue
        client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
    return len(messages)


def main() -> int:
    default_env = str(Path(__file__).resolve().parents[1] / ".env")
    default_state = Path(__file__).resolve().parents[1] / "state"
    default_dedup = str(default_state / "vk-worker-dedup.sqlite3")
    default_trace = str(default_state / "vk-worker-trace.sqlite3")
    default_review = str(default_state / "vk-worker-review.sqlite3")
    default_fixture_dir = str(Path(__file__).resolve().parents[1] / "fixtures" / "vk")

    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=default_env, help="bridge .env path")
    parser.add_argument("--hermes-env", default="/root/.hermes/.env", help="Hermes .env path for API_SERVER_KEY fallback")
    parser.add_argument("--once", action="store_true", help="process one poll cycle and exit")
    parser.add_argument("--long-poll", action="store_true", help="use VK Long Poll directly instead of Yandex Message Queue")
    parser.add_argument("--dedup-db", help="SQLite dedup store path")
    parser.add_argument("--trace-db", help="SQLite trace store path")
    parser.add_argument("--review-db", help="SQLite review inbox path")
    parser.add_argument("--fake-event", help="process a saved VK event fixture without VK/Yandex/Hermes secrets")
    parser.add_argument("--replay", nargs="+", help="replay one or more saved VK event fixtures with fake Hermes/VK sends")
    parser.add_argument("--fake-hermes-answer", default="Fake Hermes response.", help="assistant text used by --fake-event/--replay")
    parser.add_argument("--doctor", action="store_true", help="check required config and local state stores")
    parser.add_argument("--doctor-network", action="store_true", help="also check Hermes API /health")
    parser.add_argument("--health", action="store_true", help="print worker health/status summary")
    parser.add_argument("--health-network", action="store_true", help="also check Hermes API /health for --health")
    parser.add_argument("--smoke", action="store_true", help="run fake owner/public E2E smoke checks")
    parser.add_argument("--fixture-dir", default=default_fixture_dir, help="VK fixture directory for --smoke")
    parser.add_argument("--state-dir", default=str(default_state), help="state directory for --smoke temporary stores")
    args = parser.parse_args()

    load_dotenv(args.hermes_env)
    load_dotenv(args.env)

    configure_logging()

    if args.fake_event:
        result = run_fake_event(
            args.fake_event,
            fake_hermes_answer=args.fake_hermes_answer,
            dedup_path=args.dedup_db or ":memory:",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.replay:
        report = run_replay(
            args.replay,
            fake_hermes_answer=args.fake_hermes_answer,
            dedup_path=args.dedup_db or ":memory:",
        )
        print(format_replay_report(report))
        return 0 if report["ok"] else 2

    if args.doctor:
        report = run_doctor(
            dedup_db=args.dedup_db or env("DEDUP_DB", default_dedup),
            trace_db=args.trace_db or env("TRACE_DB", default_trace),
            review_db=args.review_db or env("REVIEW_DB", default_review),
            check_network=args.doctor_network,
        )
        print(format_doctor_report(report))
        return 0 if report["ok"] else 2

    if args.health:
        report = run_health(
            mode="long_poll" if args.long_poll else "queue",
            dedup_db=args.dedup_db or env("DEDUP_DB", default_dedup),
            trace_db=args.trace_db or env("TRACE_DB", default_trace),
            review_db=args.review_db or env("REVIEW_DB", default_review),
            check_network=args.health_network,
        )
        print(format_health_report(report))
        return 0 if report["ok"] else 2

    if args.smoke:
        report = run_smoke(
            fixture_dir=args.fixture_dir,
            state_dir=args.state_dir,
            fake_hermes_answer=args.fake_hermes_answer,
        )
        print(format_smoke_report(report))
        return 0 if report["ok"] else 2

    if args.long_poll:
        return run_long_poll_loop(
            dedup_path=args.dedup_db or env("DEDUP_DB", default_dedup),
            trace_path=args.trace_db or env("TRACE_DB", default_trace),
            review_path=args.review_db or env("REVIEW_DB", default_review),
            once=args.once,
        ) and 0

    queue_url = env("QUEUE_URL")
    if not queue_url:
        raise SystemExit("QUEUE_URL is required")

    dedup = DedupStore(args.dedup_db or env("DEDUP_DB", default_dedup))
    trace_store = TraceStore(args.trace_db or env("TRACE_DB", default_trace))
    review_store = ReviewStore(args.review_db or env("REVIEW_DB", default_review))
    dedup.cleanup()
    client = sqs_client()

    while True:
        run_once(client, queue_url, dedup, trace_store=trace_store, review_store=review_store)
        if args.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())


