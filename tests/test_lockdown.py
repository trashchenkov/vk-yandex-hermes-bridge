from __future__ import annotations

import importlib.util
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
        "object": {"message": {"id": from_id, "peer_id": from_id, "from_id": from_id, "text": text, "attachments": []}},
    }


def test_lockdown_forces_non_owner_deny_even_when_trusted_or_handoff(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_TRUSTED_USERS", "2")
    monkeypatch.setenv("VK_PUBLIC_HANDOFF", "true")
    monkeypatch.setenv("VK_EMERGENCY_LOCKDOWN", "true")

    trusted = worker.decide_policy(worker.normalize_vk_message(vk_event(2, "trusted question")))
    public = worker.decide_policy(worker.normalize_vk_message(vk_event(3, "public question")))

    assert trusted == {"role": "trusted", "action": "deny", "hermes_allowed": False, "reason": "emergency_lockdown"}
    assert public == {"role": "public", "action": "deny", "hermes_allowed": False, "reason": "emergency_lockdown"}


def test_lockdown_keeps_owner_reply_and_owner_commands_available(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_EMERGENCY_LOCKDOWN", "true")

    owner_reply = worker.decide_policy(worker.normalize_vk_message(vk_event(1, "status")))
    owner_command = worker.decide_policy(worker.normalize_vk_message(vk_event(1, "!trace vk-1")))

    assert owner_reply["action"] == "reply"
    assert owner_reply["hermes_allowed"] is True
    assert owner_command["action"] == "owner_command"
    assert owner_command["command"] == "trace"


def test_lockdown_status_is_visible_in_doctor(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("QUEUE_URL", "https://queue.example/test")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "key")
    monkeypatch.setenv("VK_EMERGENCY_LOCKDOWN", "true")

    report = worker.run_doctor(
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
        check_network=False,
    )

    by_name = {check["name"]: check for check in report["checks"]}
    assert report["ok"] is True
    assert by_name["EMERGENCY_LOCKDOWN"] == {
        "name": "EMERGENCY_LOCKDOWN",
        "status": "ok",
        "detail": "enabled: non-owner traffic is forced to deny",
    }
