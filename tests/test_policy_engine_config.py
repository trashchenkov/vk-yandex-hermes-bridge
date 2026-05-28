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


def vk_event(from_id: int, text: str = "hello", peer_id: int | None = None) -> dict:
    return {
        "type": "message_new",
        "event_id": f"event-{from_id}-{peer_id or from_id}-{text}",
        "object": {
            "message": {
                "id": from_id,
                "peer_id": peer_id or from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [],
            }
        },
    }


def test_policy_engine_uses_env_json_roles_and_role_rules(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {
            "owner": {"ids": ["10"]},
            "trusted": {"ids": ["20"]},
            "blocked": {"ids": ["30"]},
        },
        "rules": {
            "trusted": {"action": "handoff", "hermes_allowed": False, "reason": "trusted_review_required"},
            "public": {"action": "deny", "hermes_allowed": False, "reason": "configured_public_deny"},
        },
    }))
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_TRUSTED_USERS", "2")

    owner = worker.decide_policy(worker.normalize_vk_message(vk_event(10)))
    legacy_owner = worker.decide_policy(worker.normalize_vk_message(vk_event(1)))
    trusted = worker.decide_policy(worker.normalize_vk_message(vk_event(20)))
    blocked = worker.decide_policy(worker.normalize_vk_message(vk_event(30)))

    assert owner["role"] == "owner"
    assert owner["action"] == "reply"
    assert owner["hermes_allowed"] is True
    assert legacy_owner["role"] == "public"
    assert legacy_owner["reason"] == "configured_public_deny"
    assert trusted == {
        "role": "trusted",
        "action": "handoff",
        "hermes_allowed": False,
        "reason": "trusted_review_required",
    }
    assert blocked["role"] == "blocked"
    assert blocked["action"] == "deny"
    assert blocked["hermes_allowed"] is False


def test_policy_engine_can_load_file_config(monkeypatch, tmp_path):
    worker = load_worker()
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "roles": {"owner": {"ids": ["42"]}},
        "rules": {"public": {"action": "handoff", "hermes_allowed": False, "reason": "file_public_handoff"}},
    }))
    monkeypatch.setenv("VK_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("VK_OWNER_ID", "1")

    assert worker.resolve_role(worker.normalize_vk_message(vk_event(42))) == "owner"
    public = worker.decide_policy(worker.normalize_vk_message(vk_event(7)))

    assert public == {
        "role": "public",
        "action": "handoff",
        "hermes_allowed": False,
        "reason": "file_public_handoff",
    }


def test_policy_engine_group_chat_requires_mention(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {"owner": {"ids": ["1"]}},
        "group_chats": {"require_mention": True, "mentions": ["гермес", "@club123"]},
    }))

    no_mention = worker.decide_policy(worker.normalize_vk_message(vk_event(9, "привет", peer_id=2000000001)))
    with_mention = worker.decide_policy(worker.normalize_vk_message(vk_event(9, "Гермес, помоги", peer_id=2000000001)))

    assert no_mention == {
        "role": "group_chat",
        "action": "deny",
        "hermes_allowed": False,
        "reason": "group_mention_required",
    }
    assert with_mention["role"] == "group_chat"
    assert with_mention["action"] == "deny"
    assert with_mention["reason"] == "group_default_deny"
