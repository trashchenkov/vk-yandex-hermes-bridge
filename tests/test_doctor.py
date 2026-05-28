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


def test_format_doctor_report_is_human_readable():
    worker = load_worker()
    text = worker.format_doctor_report({
        "ok": False,
        "checks": [
            {"name": "A", "status": "ok", "detail": "ready"},
            {"name": "B", "status": "fail", "detail": "missing"},
        ],
    })

    assert "Doctor: FAIL" in text
    assert "[ok] A: ready" in text
    assert "[fail] B: missing" in text
