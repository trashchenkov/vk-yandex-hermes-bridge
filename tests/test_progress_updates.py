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


def vk_event(from_id: int = 1, text: str = "run tools") -> dict:
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


class FakeResponse:
    def __init__(self, payload: dict | None = None, *, ok: bool = True, status_code: int = 200, text: str | None = None):
        self.payload = payload or {}
        self.ok = ok
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(self.payload)

    def json(self):
        return self.payload


def test_sse_parser_handles_event_and_data_blocks():
    worker = load_worker()
    chunks = [
        b": keepalive\n\n",
        b"event: tool_progress\n",
        b"data: {\"tool\": \"terminal\", \"preview\": \"pytest -q\"}\n\n",
        b"data: {\"status\": \"completed\"}\n\n",
    ]

    events = list(worker.parse_sse_events(chunks))

    assert events == [
        {"event": "tool_progress", "tool": "terminal", "preview": "pytest -q"},
        {"status": "completed"},
    ]


def test_sse_parser_flushes_empty_byte_chunks_from_requests_iter_lines():
    worker = load_worker()
    chunks = [
        b'data: {"event":"tool.started","tool":"terminal","preview":"date"}',
        b"",
        b'data: {"event":"tool.completed","tool":"terminal","error":false}',
        b"",
    ]

    events = list(worker.parse_sse_events(chunks))

    assert events == [
        {"event": "tool.started", "tool": "terminal", "preview": "date"},
        {"event": "tool.completed", "tool": "terminal", "error": False},
    ]


def test_formats_tool_progress_with_redaction_and_truncation():
    worker = load_worker()
    event = {
        "event": "tool_progress",
        "tool": "terminal",
        "preview": "export HERMES_API_KEY=super-secret-value && " + "x" * 200,
    }

    line = worker.format_progress_event(event)

    assert line.startswith("💻 terminal: ")
    assert "super-secret-value" not in line
    assert "[redacted]" in line
    assert len(line) <= 130
    assert line.endswith('…"')


def test_progress_sink_buffers_and_rate_limits_messages(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_PROGRESS_FLUSH_INTERVAL_MS", "1000")
    monkeypatch.setenv("VK_PROGRESS_MAX_LINES", "10")
    sent: list[tuple[str, str]] = []
    now = [100.0]

    sink = worker.VkProgressSink("123", "trace-1", send_func=lambda peer_id, text, trace_id=None: sent.append((peer_id, text)), clock=lambda: now[0])
    sink.add_line("📚 skill_view: \"hermes-agent\"")
    sink.add_line("💻 terminal: \"pytest -q\"")
    sink.flush(force=False)
    assert sent == []

    now[0] = 101.1
    sink.flush(force=False)

    assert len(sent) == 1
    assert sent[0][0] == "123"
    assert sent[0][1].startswith("Работаю...")
    assert "skill_view" in sent[0][1]
    assert "terminal" in sent[0][1]


def test_extract_hermes_text_accepts_run_status_output_string():
    worker = load_worker()

    assert worker.extract_hermes_text({"object": "hermes.run", "status": "completed", "output": "final answer"}) == "final answer"


def test_call_hermes_with_progress_uses_runs_events_and_final_result(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("HERMES_API_KEY", "key")
    monkeypatch.setenv("VK_PROGRESS_ENABLED", "true")
    monkeypatch.setenv("VK_PROGRESS_FLUSH_INTERVAL_MS", "0")
    sent: list[str] = []
    calls: list[tuple[str, str]] = []

    class FakeStreamResponse(FakeResponse):
        def iter_lines(self, decode_unicode=False):
            lines = [
                "event: tool_progress",
                'data: {"tool":"terminal","preview":"pytest -q"}',
                "",
            ]
            for line in lines:
                yield line if decode_unicode else line.encode("utf-8")

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append(("POST", url))
        assert url.endswith("/v1/runs")
        return FakeResponse({"id": "run-1"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        calls.append(("GET", url))
        if url.endswith("/v1/runs/run-1/events"):
            assert stream is True
            return FakeStreamResponse()
        if url.endswith("/v1/runs/run-1"):
            return FakeResponse({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "final answer"}]}]})
        raise AssertionError(url)

    monkeypatch.setattr(worker.requests, "post", fake_post)
    monkeypatch.setattr(worker.requests, "get", fake_get)

    answer = worker.call_hermes_with_progress(
        worker.normalize_vk_message(vk_event()),
        send_progress=lambda peer_id, text, trace_id=None: sent.append(text),
        trace_id="trace-1",
    )

    assert answer == "final answer"
    assert any("terminal" in message for message in sent)
    assert calls == [
        ("POST", "http://127.0.0.1:8642/v1/runs"),
        ("GET", "http://127.0.0.1:8642/v1/runs/run-1/events"),
        ("GET", "http://127.0.0.1:8642/v1/runs/run-1"),
    ]


def test_process_payload_uses_progress_path_when_enabled(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_PROGRESS_ENABLED", "true")
    progress_calls = []
    replies = []

    monkeypatch.setattr(worker, "call_hermes_with_progress", lambda vk, send_progress, trace_id: progress_calls.append((vk, send_progress, trace_id)) or "answer")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, trace_id=None: replies.append((peer_id, text, trace_id)))

    worker.process_payload(vk_event(), worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert len(progress_calls) == 1
    assert replies[-1][1] == "answer"


def test_process_payload_falls_back_to_responses_when_progress_disabled(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_PROGRESS_ENABLED", "false")
    calls = []
    monkeypatch.setattr(worker, "call_hermes", lambda vk: calls.append(vk) or "answer")
    monkeypatch.setattr(worker, "call_hermes_with_progress", lambda vk, send_progress, trace_id: (_ for _ in ()).throw(AssertionError("progress disabled")))
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, trace_id=None: None)

    worker.process_payload(vk_event(), worker.DedupStore(tmp_path / "dedup.sqlite3"))

    assert len(calls) == 1
