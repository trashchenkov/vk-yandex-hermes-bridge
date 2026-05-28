from __future__ import annotations

import importlib.util
import json
import logging
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


def test_run_health_reports_worker_queue_hermes_vk_and_state(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("QUEUE_URL", "https://queue.example/test")
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-super-secret-token")
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "hermes-secret-key")
    monkeypatch.setenv("HERMES_API_BASE", "http://127.0.0.1:8642")

    report = worker.run_health(
        mode="queue",
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
    )

    by_name = {component["name"]: component for component in report["components"]}
    assert report["ok"] is True
    assert by_name["worker"]["status"] == "ok"
    assert by_name["queue"]["status"] == "ok"
    assert by_name["hermes"]["status"] == "ok"
    assert by_name["vk"]["status"] == "ok"
    assert by_name["state_dbs"]["status"] == "ok"
    assert "vk-super-secret-token" not in worker.format_health_report(report)
    assert "hermes-secret-key" not in worker.format_health_report(report)


def test_run_health_long_poll_skips_queue_but_requires_vk_group(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.delenv("QUEUE_URL", raising=False)
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "key")

    report = worker.run_health(
        mode="long_poll",
        dedup_db=tmp_path / "dedup.sqlite3",
        trace_db=tmp_path / "trace.sqlite3",
        review_db=tmp_path / "review.sqlite3",
    )

    by_name = {component["name"]: component for component in report["components"]}
    assert report["ok"] is True
    assert by_name["queue"]["status"] == "skip"
    assert by_name["vk"]["detail"] == "token/group configured"


def test_health_cli_prints_report_and_exit_code(monkeypatch, tmp_path, capsys):
    worker = load_worker()
    monkeypatch.setenv("QUEUE_URL", "https://queue.example/test")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vk_hermes_worker.py",
            "--health",
            "--dedup-db",
            str(tmp_path / "dedup.sqlite3"),
            "--trace-db",
            str(tmp_path / "trace.sqlite3"),
            "--review-db",
            str(tmp_path / "review.sqlite3"),
        ],
        raising=False,
    )

    assert worker.main() == 0
    output = capsys.readouterr().out
    assert "Health: OK" in output
    assert "[ok] queue:" in output
    assert "[ok] hermes:" in output
    assert "[ok] vk:" in output


def test_json_log_formatter_emits_parseable_redacted_event():
    worker = load_worker()
    record = logging.LogRecord(
        name="vk_hermes_worker",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="policy decision token=super-secret access_key=nested-secret",
        args=(),
        exc_info=None,
    )

    line = worker.JsonLogFormatter().format(record)
    data = json.loads(line)

    assert data["level"] == "INFO"
    assert data["logger"] == "vk_hermes_worker"
    assert "policy decision" in data["message"]
    assert "super-secret" not in line
    assert "nested-secret" not in line
    assert "[redacted]" in line
