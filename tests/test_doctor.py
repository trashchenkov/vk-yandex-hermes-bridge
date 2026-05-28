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


def test_run_doctor_reports_missing_required_config(monkeypatch, tmp_path):
    worker = load_worker()
    for name in [
        "QUEUE_URL",
        "VK_GROUP_TOKEN",
        "VK_OWNER_ID",
        "VK_ALLOWED_USERS",
        "HERMES_API_KEY",
        "API_SERVER_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)

    report = worker.run_doctor(
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
        check_network=False,
    )

    by_name = {check["name"]: check for check in report["checks"]}
    assert report["ok"] is False
    assert by_name["QUEUE_URL"]["status"] == "fail"
    assert by_name["VK_GROUP_TOKEN"]["status"] == "fail"
    assert by_name["OWNER_ALLOWLIST"]["status"] == "fail"
    assert by_name["HERMES_API_KEY"]["status"] == "fail"
    assert by_name["STATE_DBS"]["status"] == "ok"


def test_run_doctor_passes_required_config_and_state_dbs(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("QUEUE_URL", "https://queue.example/test")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "key")
    monkeypatch.setenv("HERMES_API_BASE", "http://127.0.0.1:8642")

    report = worker.run_doctor(
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
        check_network=False,
    )

    by_name = {check["name"]: check for check in report["checks"]}
    assert report["ok"] is True
    assert by_name["QUEUE_URL"]["status"] == "ok"
    assert by_name["VK_GROUP_TOKEN"]["status"] == "ok"
    assert by_name["OWNER_ALLOWLIST"]["status"] == "ok"
    assert by_name["HERMES_API_KEY"]["status"] == "ok"
    assert by_name["HERMES_API_BASE"]["status"] == "ok"
    assert by_name["STATE_DBS"]["status"] == "ok"


def test_run_doctor_redacts_secrets_and_includes_remediation_hints(monkeypatch, tmp_path):
    worker = load_worker()
    secret = "vk1.super-secret-token-value"
    monkeypatch.setenv("QUEUE_URL", "https://queue.example/test")
    monkeypatch.setenv("VK_GROUP_TOKEN", secret)
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "hermes-secret-key")

    report = worker.run_doctor(
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
        check_network=False,
    )
    text = worker.format_doctor_report(report)

    assert secret not in text
    assert "hermes-secret-key" not in text
    assert "***" in text
    assert all(check.get("hint") for check in report["checks"] if check["status"] in {"fail", "warn"})


def test_vk_allow_all_users_is_failure_outside_explicit_test_mode(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("QUEUE_URL", "https://queue.example/test")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "key")
    monkeypatch.setenv("VK_ALLOW_ALL_USERS", "true")
    monkeypatch.delenv("VK_BRIDGE_TEST_MODE", raising=False)

    report = worker.run_doctor(
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
        check_network=False,
    )

    by_name = {check["name"]: check for check in report["checks"]}
    assert report["ok"] is False
    assert by_name["VK_ALLOW_ALL_USERS"]["status"] == "fail"
    assert "public" in by_name["VK_ALLOW_ALL_USERS"]["hint"].lower()


def test_format_doctor_report_is_human_readable():
    worker = load_worker()
    text = worker.format_doctor_report({
        "ok": False,
        "checks": [
            {"name": "A", "status": "ok", "detail": "ready"},
            {"name": "B", "status": "fail", "detail": "missing", "hint": "set B"},
        ],
    })

    assert "Doctor: FAIL" in text
    assert "[ok] A: ready" in text
    assert "[fail] B: missing" in text
    assert "hint: set B" in text
