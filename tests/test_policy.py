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


def test_resolve_role_prefers_blocked_over_owner_and_trusted(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_ALLOWED_USERS", "1,2")
    monkeypatch.setenv("VK_TRUSTED_USERS", "2,3")
    monkeypatch.setenv("VK_BLOCKED_USERS", "1,3")

    assert worker.resolve_role(worker.normalize_vk_message(vk_event(1))) == "blocked"
    assert worker.resolve_role(worker.normalize_vk_message(vk_event(2))) == "trusted"
    assert worker.resolve_role(worker.normalize_vk_message(vk_event(3))) == "blocked"
    assert worker.resolve_role(worker.normalize_vk_message(vk_event(4))) == "public"


def test_policy_decision_routes_owner_command_only_for_owner(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")

    owner_decision = worker.decide_policy(worker.normalize_vk_message(vk_event(1, "!trace vk-1")))
    public_decision = worker.decide_policy(worker.normalize_vk_message(vk_event(9, "!trace vk-1")))

    assert owner_decision == {
        "role": "owner",
        "action": "owner_command",
        "hermes_allowed": False,
        "reason": "owner_command",
        "command": "trace",
        "command_args": ["vk-1"],
    }
    assert public_decision["role"] == "public"
    assert public_decision["action"] == "deny"
    assert public_decision["hermes_allowed"] is False


def test_policy_decision_defaults_public_and_blocked_to_no_hermes(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_TRUSTED_USERS", "2")
    monkeypatch.setenv("VK_BLOCKED_USERS", "3")

    owner = worker.decide_policy(worker.normalize_vk_message(vk_event(1)))
    trusted = worker.decide_policy(worker.normalize_vk_message(vk_event(2)))
    public = worker.decide_policy(worker.normalize_vk_message(vk_event(4)))
    blocked = worker.decide_policy(worker.normalize_vk_message(vk_event(3)))

    assert owner["action"] == "reply"
    assert owner["hermes_allowed"] is True
    assert trusted["role"] == "trusted"
    assert trusted["action"] == "reply"
    assert trusted["hermes_allowed"] is True
    assert public["role"] == "public"
    assert public["action"] == "deny"
    assert public["hermes_allowed"] is False
    assert public["reason"] == "public_default_deny"
    assert blocked["role"] == "blocked"
    assert blocked["action"] == "deny"
    assert blocked["hermes_allowed"] is False


def test_public_handoff_is_explicit_policy_without_hermes(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_PUBLIC_HANDOFF", "true")

    decision = worker.decide_policy(worker.normalize_vk_message(vk_event(4, "important public question")))

    assert decision == {
        "role": "public",
        "action": "handoff",
        "hermes_allowed": False,
        "reason": "public_handoff",
    }


def test_fake_event_exposes_full_policy_decision(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "254662087")
    monkeypatch.delenv("VK_PUBLIC_HANDOFF", raising=False)
    fixture = ROOT / "fixtures" / "vk" / "message_new_owner.json"

    result = worker.run_fake_event(fixture, fake_hermes_answer="answer", dedup_path=tmp_path / "dedup.sqlite3")

    assert result["policy"] == {
        "role": "owner",
        "action": "reply",
        "hermes_allowed": True,
        "reason": "allowed_user",
    }
