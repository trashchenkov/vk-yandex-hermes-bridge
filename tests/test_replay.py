from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"
FIXTURES = ROOT / "fixtures" / "vk"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_fixture_set_covers_core_local_emulator_cases():
    expected = {
        "confirmation.json",
        "message_new_owner.json",
        "message_new_unknown.json",
        "duplicate_event.json",
        "hermes_timeout.json",
        "vk_send_failure.json",
    }
    assert expected <= {path.name for path in FIXTURES.glob("*.json")}


def test_run_replay_reports_policy_outbound_duplicate_and_simulated_failures(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.delenv("VK_ALLOW_ALL_USERS", raising=False)

    report = worker.run_replay(
        [
            FIXTURES / "message_new_owner.json",
            FIXTURES / "message_new_unknown.json",
            FIXTURES / "duplicate_event.json",
            FIXTURES / "hermes_timeout.json",
            FIXTURES / "vk_send_failure.json",
        ],
        fake_hermes_answer="replay answer",
        dedup_path=tmp_path / "dedup.sqlite3",
    )

    assert report["ok"] is True
    cases = {Path(item["fixture"]).name: item for item in report["results"]}
    assert cases["message_new_owner.json"]["policy_decision"] == "reply"
    assert cases["message_new_owner.json"]["hermes_called"] is True
    assert cases["message_new_owner.json"]["outbound_messages"][-1]["message"] == "replay answer"
    assert cases["message_new_unknown.json"]["policy_decision"] == "deny"
    assert cases["message_new_unknown.json"]["hermes_called"] is False
    assert cases["duplicate_event.json"]["duplicate_skipped"] is True
    assert cases["hermes_timeout.json"]["status"] == "error"
    assert "simulated Hermes timeout" in cases["hermes_timeout.json"]["error"]
    assert cases["vk_send_failure.json"]["status"] == "error"
    assert "simulated VK send failure" in cases["vk_send_failure.json"]["error"]


def test_replay_cli_prints_human_report(monkeypatch, tmp_path, capsys):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vk_hermes_worker.py",
            "--replay",
            str(FIXTURES / "message_new_owner.json"),
            str(FIXTURES / "duplicate_event.json"),
            "--fake-hermes-answer",
            "cli replay answer",
            "--dedup-db",
            str(tmp_path / "dedup.sqlite3"),
        ],
        raising=False,
    )

    assert worker.main() == 0
    output = capsys.readouterr().out
    assert "Replay: OK" in output
    assert "message_new_owner.json" in output
    assert "decision=reply" in output
    assert "duplicate_skipped=True" in output
    assert "final_outbound=" in output
