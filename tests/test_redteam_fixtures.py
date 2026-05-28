from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"
REDTEAM_FIXTURES = sorted((ROOT / "fixtures" / "vk").glob("redteam_*.json"))


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fixture", REDTEAM_FIXTURES, ids=lambda p: p.stem)
def test_unknown_redteam_fixture_is_denied_without_hermes(monkeypatch, tmp_path, fixture):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "254662087")
    monkeypatch.delenv("VK_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("VK_PUBLIC_HANDOFF", raising=False)

    result = worker.run_fake_event(fixture, fake_hermes_answer="must not be used", dedup_path=tmp_path / f"{fixture.stem}.sqlite3")

    assert result["trace_id"].startswith("vk-")
    assert result["role"] == "public"
    assert result["policy_decision"] in {"deny", "handoff"}
    assert result["policy"]["reason"] == "public_default_deny"
    assert result["hermes_called"] is False
    assert result["outbound_messages"]


@pytest.mark.parametrize("fixture", REDTEAM_FIXTURES, ids=lambda p: p.stem)
def test_unknown_redteam_fixture_handoff_still_does_not_call_hermes(monkeypatch, tmp_path, fixture):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "254662087")
    monkeypatch.setenv("VK_PUBLIC_HANDOFF", "true")
    monkeypatch.delenv("VK_ALLOW_ALL_USERS", raising=False)

    result = worker.run_fake_event(fixture, fake_hermes_answer="must not be used", dedup_path=tmp_path / f"handoff-{fixture.stem}.sqlite3")

    assert result["role"] == "public"
    assert result["policy_decision"] == "handoff"
    assert result["policy"]["reason"] == "public_handoff"
    assert result["hermes_called"] is False
    assert result["outbound_messages"]


def test_redteam_fixture_set_covers_required_cases():
    names = {path.stem.removeprefix("redteam_") for path in REDTEAM_FIXTURES}
    assert names == {
        "impersonate_owner",
        "prompt_injection",
        "secrets_request",
        "shell_command",
        "cross_channel",
        "wall_publish",
    }
