from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vk_event(from_id: int, text: str = "hello") -> dict:
    return {
        "type": "message_new",
        "event_id": f"event-{from_id}-{text}",
        "object": {
            "message": {
                "id": from_id,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [],
            }
        },
    }


class FakeResponse:
    ok = True
    status_code = 200
    text = "{}"

    def json(self):
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "public answer"}]}]}


def test_public_hermes_reply_uses_public_profile_session_and_key(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("HERMES_API_KEY", "private-key")
    monkeypatch.setenv("HERMES_PUBLIC_API_KEY", "public-key")
    monkeypatch.setenv("HERMES_PUBLIC_MODEL", "public-safe-model")
    monkeypatch.setenv("HERMES_PUBLIC_PROFILE", "vk-public")
    monkeypatch.setenv("HERMES_PUBLIC_SESSION_PREFIX", "vk-public")
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {"owner": {"ids": ["1"]}},
        "rules": {"public": {"action": "reply", "hermes_allowed": True, "reason": "public_profile_reply"}},
    }))
    captured: dict = {}

    def fake_post(url, headers, data, timeout):
        captured.update({"url": url, "headers": headers, "payload": json.loads(data.decode("utf-8")), "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(worker.requests, "post", fake_post)

    answer = worker.call_hermes(worker.normalize_vk_message(vk_event(9, "public question")))

    assert answer == "public answer"
    assert captured["headers"]["authorization"] == "Bearer public-key"
    assert captured["headers"]["x-hermes-profile"] == "vk-public"
    assert captured["headers"]["x-hermes-session-key"] == "vk-public:9"
    assert captured["payload"]["model"] == "public-safe-model"
    assert captured["payload"]["conversation"] == "vk-public:9"


def test_owner_hermes_reply_keeps_private_profile(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "private-key")
    monkeypatch.setenv("HERMES_MODEL", "private-model")
    monkeypatch.setenv("HERMES_PUBLIC_API_KEY", "public-key")
    monkeypatch.setenv("HERMES_PUBLIC_PROFILE", "vk-public")
    captured: dict = {}

    def fake_post(url, headers, data, timeout):
        captured.update({"headers": headers, "payload": json.loads(data.decode("utf-8"))})
        return FakeResponse()

    monkeypatch.setattr(worker.requests, "post", fake_post)

    worker.call_hermes(worker.normalize_vk_message(vk_event(1, "owner question")))

    assert captured["headers"]["authorization"] == "Bearer private-key"
    assert "x-hermes-profile" not in captured["headers"]
    assert captured["headers"]["x-hermes-session-key"] == "vk:1"
    assert captured["payload"]["model"] == "private-model"


def test_doctor_warns_when_public_reply_uses_private_profile(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("QUEUE_URL", "https://queue.example")
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    monkeypatch.setenv("HERMES_API_KEY", "private-key")
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {"owner": {"ids": ["1"]}},
        "rules": {"public": {"action": "reply", "hermes_allowed": True, "reason": "public_profile_reply"}},
    }))

    report = worker.run_doctor(
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
    )

    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["PUBLIC_HERMES_PROFILE"]["status"] == "warn"
    assert "private profile" in by_name["PUBLIC_HERMES_PROFILE"]["detail"]


def test_doctor_accepts_separate_public_profile(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("QUEUE_URL", "https://queue.example")
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    monkeypatch.setenv("HERMES_API_KEY", "private-key")
    monkeypatch.setenv("HERMES_PUBLIC_PROFILE", "vk-public")
    monkeypatch.setenv("HERMES_PUBLIC_API_KEY", "public-key")
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {"owner": {"ids": ["1"]}},
        "rules": {"public": {"action": "reply", "hermes_allowed": True, "reason": "public_profile_reply"}},
    }))

    report = worker.run_doctor(
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
    )

    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["PUBLIC_HERMES_PROFILE"]["status"] == "ok"
    assert "vk-public" in by_name["PUBLIC_HERMES_PROFILE"]["detail"]
