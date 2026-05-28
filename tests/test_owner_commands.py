from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"
OWNER_COMMANDS = ["!trace vk-abc", "!pending", "!approve 42", "!reject 42"]


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vk_event(from_id: int, text: str) -> dict:
    return {
        "type": "message_new",
        "event_id": f"cmd-{from_id}-{text}",
        "object": {
            "message": {
                "id": abs(hash((from_id, text))) % 100000,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": [],
            }
        },
    }


@pytest.mark.parametrize("text", OWNER_COMMANDS)
def test_known_owner_commands_are_routed_for_owner_with_command_metadata(monkeypatch, text):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    vk = worker.normalize_vk_message(vk_event(1, text))

    decision = worker.decide_policy(vk)

    command, *args = text[1:].split()
    assert decision["role"] == "owner"
    assert decision["action"] == "owner_command"
    assert decision["hermes_allowed"] is False
    assert decision["command"] == command
    assert decision["command_args"] == args


@pytest.mark.parametrize("text", OWNER_COMMANDS)
def test_known_owner_commands_from_public_are_denied_without_hermes(monkeypatch, tmp_path, text):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    payload = vk_event(9, text)
    hermes_calls = []
    sent = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: hermes_calls.append(vk) or "should not happen")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))

    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert hermes_calls == []
    assert len(sent) == 1
    assert sent[0][0] == "9"
    assert "приватный" in sent[0][1].lower()


def test_owner_command_is_acknowledged_without_calling_hermes(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    payload = vk_event(1, "!pending")
    hermes_calls = []
    sent = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: hermes_calls.append(vk) or "should not happen")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, message, **kwargs: sent.append((peer_id, message, kwargs)))

    worker.process_payload(payload, worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert hermes_calls == []
    assert sent == [("1", "Review store is not configured.", {"trace_id": worker.trace_id_for_payload(payload)})]
