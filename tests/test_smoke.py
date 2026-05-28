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


def test_run_smoke_executes_owner_and_public_fake_e2e(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.delenv("VK_ALLOW_ALL_USERS", raising=False)

    report = worker.run_smoke(
        fixture_dir=FIXTURES,
        state_dir=tmp_path,
        fake_hermes_answer="smoke fake answer",
    )

    assert report["ok"] is True
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["owner_fake_e2e"]["status"] == "ok"
    assert by_name["public_fake_e2e"]["status"] == "ok"
    assert by_name["owner_fake_e2e"]["trace_id"].startswith("vk-")
    assert by_name["owner_fake_e2e"]["outbound_count"] == 1
    assert by_name["owner_fake_e2e"]["final_outbound_payload"] == {
        "peer_id": "254662087",
        "message": "smoke fake answer",
        "random_id": worker.stable_random_id(by_name["owner_fake_e2e"]["trace_id"], 0),
    }
    assert by_name["public_fake_e2e"]["hermes_called"] is False


def test_run_smoke_is_repeatable_with_same_state_dir(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.delenv("VK_ALLOW_ALL_USERS", raising=False)

    first = worker.run_smoke(fixture_dir=FIXTURES, state_dir=tmp_path, fake_hermes_answer="first")
    second = worker.run_smoke(fixture_dir=FIXTURES, state_dir=tmp_path, fake_hermes_answer="second")

    assert first["ok"] is True
    assert second["ok"] is True
    assert {check["status"] for check in second["checks"]} == {"ok"}


def test_format_smoke_report_is_human_readable():
    worker = load_worker()
    text = worker.format_smoke_report({
        "ok": False,
        "checks": [
            {"name": "owner_fake_e2e", "status": "ok", "detail": "reply path works", "trace_id": "vk-1", "final_outbound_payload": {"peer_id": "1", "message": "ok", "random_id": 7}},
            {"name": "public_fake_e2e", "status": "fail", "detail": "unexpected Hermes call"},
        ],
    })

    assert "Smoke: FAIL" in text
    assert "[ok] owner_fake_e2e: reply path works trace=vk-1" in text
    assert 'final_outbound={"peer_id":"1","message":"ok","random_id":7}' in text
    assert "[fail] public_fake_e2e: unexpected Hermes call" in text


def test_smoke_cli_prints_report_and_returns_zero(monkeypatch, tmp_path, capsys):
    worker = load_worker()
    monkeypatch.setenv("VK_ALLOWED_USERS", "254662087")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vk_hermes_worker.py",
            "--smoke",
            "--fixture-dir",
            str(FIXTURES),
            "--state-dir",
            str(tmp_path),
            "--fake-hermes-answer",
            "cli smoke answer",
        ],
        raising=False,
    )

    assert worker.main() == 0
    output = capsys.readouterr().out
    assert "Smoke: OK" in output
    assert "owner_fake_e2e" in output
    assert "public_fake_e2e" in output
