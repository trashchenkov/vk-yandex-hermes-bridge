from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_fake_event_owner_uses_fake_hermes_and_fake_vk_send(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.delenv("VK_ALLOW_ALL_USERS", raising=False)

    fixture = ROOT / "fixtures" / "vk" / "message_new_owner.json"
    result = worker.run_fake_event(fixture, fake_hermes_answer="fake Hermes answer", dedup_path=tmp_path / "dedup.sqlite3")

    assert result["trace_id"]
    assert result["role"] == "owner"
    assert result["policy_decision"] == "reply"
    assert result["hermes_called"] is True
    assert result["outbound_messages"] == [
        {"peer_id": "254662087", "message": "fake Hermes answer"}
    ]


def test_run_fake_event_unknown_user_is_denied_without_fake_hermes(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.delenv("VK_ALLOW_ALL_USERS", raising=False)

    fixture = ROOT / "fixtures" / "vk" / "message_new_unknown.json"
    result = worker.run_fake_event(fixture, fake_hermes_answer="should not be used", dedup_path=tmp_path / "dedup.sqlite3")

    assert result["trace_id"]
    assert result["role"] == "public"
    assert result["policy_decision"] == "deny"
    assert result["hermes_called"] is False
    assert result["outbound_messages"] == [
        {"peer_id": "111111111", "message": "Бот приватный. Доступ к Hermes Agent ограничен."}
    ]


def test_fake_event_cli_prints_json_result(monkeypatch, tmp_path, capsys):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vk_hermes_worker.py",
            "--fake-event",
            str(ROOT / "fixtures" / "vk" / "message_new_owner.json"),
            "--fake-hermes-answer",
            "cli fake answer",
            "--dedup-db",
            str(tmp_path / "dedup.sqlite3"),
        ],
        raising=False,
    )

    assert worker.main() == 0
    output = capsys.readouterr().out
    assert '"policy_decision": "reply"' in output
    assert '"message": "cli fake answer"' in output
