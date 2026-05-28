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


def load_dotenv(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(errors="replace").splitlines():
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


def allowed_vk_users() -> set[str]:
    return env_id_set("VK_ALLOWED_USERS")


def owner_vk_users() -> set[str]:
    # VK_ALLOWED_USERS is the legacy owner allowlist; once VK_OWNER_ID is set, it is authoritative.
    explicit = env_id_set("VK_OWNER_ID")
    return explicit or allowed_vk_users()


def trusted_vk_users() -> set[str]:
    return env_id_set("VK_TRUSTED_USERS")


def blocked_vk_users() -> set[str]:
    return env_id_set("VK_BLOCKED_USERS")


def resolve_role(vk: dict[str, Any]) -> str:
    sender = str(vk.get("from_id") or "")
    if sender in blocked_vk_users():
        return "blocked"
    if sender in owner_vk_users():
        return "owner"
    if sender in trusted_vk_users() or truthy_env("VK_ALLOW_ALL_USERS"):
        return "trusted"
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


def decide_policy(vk: dict[str, Any]) -> dict[str, Any]:
    role = resolve_role(vk)
    text = str(vk.get("text") or "")
    command = parse_owner_command(text)
    if role == "blocked":
        return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "blocked_user"}
    if command:
        if role == "owner":
            return {"role": role, "action": "owner_command", "hermes_allowed": False, "reason": "owner_command", **command}
        return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "owner_command_requires_owner", **command}
    if role in {"owner", "trusted"}:
        return {"role": role, "action": "reply", "hermes_allowed": True, "reason": "allowed_user"}
    if truthy_env("VK_PUBLIC_HANDOFF"):
        return {"role": role, "action": "handoff", "hermes_allowed": False, "reason": "public_handoff"}
    return {"role": role, "action": "deny", "hermes_allowed": False, "reason": "public_default_deny"}


def is_authorized(vk: dict[str, Any]) -> bool:
    """Return True when the VK sender may reach Hermes tools."""
    return bool(decide_policy(vk).get("hermes_allowed"))


def unauthorized_reply_text() -> str:
    return env("VK_UNAUTHORIZED_REPLY", "Бот приватный. Доступ к Hermes Agent ограничен.").strip()


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
            reply = unauthorized_reply_text()
            if reply:
                reply_vk(vk["peer_id"], reply, trace_id=trace_id)
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


def sqs_client():
    return boto3.client(
        "sqs",
        endpoint_url=env("QUEUE_ENDPOINT", "https://message-queue.api.cloud.yandex.net"),
        region_name=env("AWS_REGION", "ru-central1"),
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


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

    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=default_env, help="bridge .env path")
    parser.add_argument("--hermes-env", default="/root/.hermes/.env", help="Hermes .env path for API_SERVER_KEY fallback")
    parser.add_argument("--once", action="store_true", help="process one poll cycle and exit")
    parser.add_argument("--dedup-db", help="SQLite dedup store path")
    parser.add_argument("--trace-db", help="SQLite trace store path")
    parser.add_argument("--review-db", help="SQLite review inbox path")
    parser.add_argument("--fake-event", help="process a saved VK event fixture without VK/Yandex/Hermes secrets")
    parser.add_argument("--fake-hermes-answer", default="Fake Hermes response.", help="assistant text used by --fake-event")
    parser.add_argument("--doctor", action="store_true", help="check required config and local state stores")
    parser.add_argument("--doctor-network", action="store_true", help="also check Hermes API /health")
    args = parser.parse_args()

    load_dotenv(args.hermes_env)
    load_dotenv(args.env)

    logging.basicConfig(
        level=getattr(logging, env("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.fake_event:
        result = run_fake_event(
            args.fake_event,
            fake_hermes_answer=args.fake_hermes_answer,
            dedup_path=args.dedup_db or ":memory:",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.doctor:
        report = run_doctor(
            dedup_db=args.dedup_db or env("DEDUP_DB", default_dedup),
            trace_db=args.trace_db or env("TRACE_DB", default_trace),
            review_db=args.review_db or env("REVIEW_DB", default_review),
            check_network=args.doctor_network,
        )
        print(format_doctor_report(report))
        return 0 if report["ok"] else 2

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
