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


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, data, timeout))
        assert url.endswith("/method/groups.getLongPollServer")
        assert data["group_id"] == "123"
        assert data["access_token"] == "token"
        return FakeResponse({"response": {"key": "lp-key", "server": "https://lp.example", "ts": "10"}})

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, params, timeout))
        assert url == "https://lp.example"
        assert params["act"] == "a_check"
        assert params["key"] == "lp-key"
        assert params["ts"] == "10"
        return FakeResponse({
            "ts": "11",
            "updates": [
                {
                    "type": "message_new",
                    "object": {
                        "message": {
                            "id": 501,
                            "peer_id": 254662087,
                            "from_id": 254662087,
                            "text": "long poll hello",
                            "attachments": [],
                        }
                    },
                },
                {"type": "message_typing_state", "object": {}},
            ],
        })


def test_long_poll_once_fetches_server_and_uses_processing_pipeline(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")
    processed = []
    monkeypatch.setattr(worker, "process_payload", lambda payload, dedup, trace_store=None, review_store=None: processed.append(payload))

    state = worker.run_long_poll_once(
        FakeSession(),
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        trace_store=worker.TraceStore(tmp_path / "trace.sqlite3"),
        review_store=worker.ReviewStore(tmp_path / "review.sqlite3"),
    )

    assert state["ts"] == "11"
    assert state["server"] == "https://lp.example"
    assert state["processed"] == 1
    assert processed == [
        {
            "type": "message_new",
            "group_id": 123,
            "event_id": "lp-501",
            "object": {
                "message": {
                    "id": 501,
                    "peer_id": 254662087,
                    "from_id": 254662087,
                    "text": "long poll hello",
                    "attachments": [],
                }
            },
        }
    ]


def test_long_poll_refreshes_server_on_failed_key(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_GROUP_ID", "123")
    monkeypatch.setenv("VK_GROUP_TOKEN", "token")

    class FailedKeySession(FakeSession):
        def get(self, url, params=None, timeout=None):
            self.gets.append((url, params, timeout))
            return FakeResponse({"failed": 2})

    state = worker.run_long_poll_once(
        FailedKeySession(),
        worker.DedupStore(tmp_path / "dedup.sqlite3"),
        state={"key": "old", "server": "https://old.example", "ts": "9"},
    )

    assert state["processed"] == 0
    assert state["refresh"] is True
    assert state["key"] == "lp-key"
    assert state["ts"] == "10"


def test_long_poll_cli_once_uses_same_state_stores(monkeypatch, tmp_path):
    worker = load_worker()
    calls = []
    monkeypatch.setattr(worker, "run_long_poll_loop", lambda **kwargs: calls.append(kwargs) or 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vk_hermes_worker.py",
            "--long-poll",
            "--once",
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
    assert len(calls) == 1
    assert calls[0]["once"] is True
    assert str(calls[0]["dedup_path"]).endswith("dedup.sqlite3")
    assert str(calls[0]["trace_path"]).endswith("trace.sqlite3")
    assert str(calls[0]["review_path"]).endswith("review.sqlite3")
