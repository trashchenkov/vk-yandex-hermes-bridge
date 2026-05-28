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


def vk_event(from_id: int, text: str = "hello") -> dict:
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


def test_public_faq_search_answers_with_source_citation(monkeypatch, tmp_path):
    worker = load_worker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "materials.md").write_text("# Hermes VK bridge\n\nHermes bridge keeps private tools isolated and uses VK public FAQ mode for safe answers.\n", encoding="utf-8")
    monkeypatch.setenv("VK_PUBLIC_FAQ_PATHS", str(docs))

    result = worker.answer_public_faq("Как Hermes bridge изолирует private tools?")

    assert result is not None
    assert "private tools isolated" in result["answer"]
    assert result["sources"] == [str(docs / "materials.md")]
    assert "Sources:" in result["text"]
    assert str(docs / "materials.md") in result["text"]


def test_public_faq_search_returns_none_without_reliable_source(monkeypatch, tmp_path):
    worker = load_worker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "materials.md").write_text("# Other\n\nOnly unrelated deployment notes.\n", encoding="utf-8")
    monkeypatch.setenv("VK_PUBLIC_FAQ_PATHS", str(docs))

    assert worker.answer_public_faq("Как устроен RAG по VK постам?") is None


def test_public_faq_policy_replies_without_calling_hermes(monkeypatch, tmp_path):
    worker = load_worker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "faq.md").write_text("# FAQ\n\nVK FAQ mode answers public questions only from cited community materials.\n", encoding="utf-8")
    monkeypatch.setenv("VK_PUBLIC_FAQ_PATHS", str(docs))
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {"owner": {"ids": ["1"]}},
        "rules": {"public": {"action": "public_faq", "hermes_allowed": False, "reason": "public_faq"}},
    }))

    outbound: list[dict] = []
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, trace_id=None: outbound.append({"peer_id": peer_id, "text": text, "trace_id": trace_id}))
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(AssertionError("Hermes must not be called for public FAQ")))

    worker.process_payload(vk_event(9, "Что делает VK FAQ mode?"), worker.DedupStore(":memory:"), trace_store=worker.TraceStore(":memory:"), review_store=worker.ReviewStore(":memory:"))

    assert len(outbound) == 1
    assert outbound[0]["peer_id"] == "9"
    assert "cited community materials" in outbound[0]["text"]
    assert "Sources:" in outbound[0]["text"]


def test_public_faq_miss_hands_off_without_hallucinating(monkeypatch, tmp_path):
    worker = load_worker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "faq.md").write_text("# FAQ\n\nOnly known answer about Hermes.\n", encoding="utf-8")
    monkeypatch.setenv("VK_PUBLIC_FAQ_PATHS", str(docs))
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {"owner": {"ids": ["1"]}},
        "rules": {"public": {"action": "public_faq", "hermes_allowed": False, "reason": "public_faq"}},
    }))
    monkeypatch.setenv("VK_UNAUTHORIZED_MODE", "ignore")

    outbound: list[dict] = []
    review = worker.ReviewStore(":memory:")
    monkeypatch.setattr(worker, "reply_vk", lambda peer_id, text, trace_id=None: outbound.append({"peer_id": peer_id, "text": text}))
    monkeypatch.setattr(worker, "call_hermes", lambda vk: (_ for _ in ()).throw(AssertionError("Hermes must not be called for public FAQ miss")))

    worker.process_payload(vk_event(9, "Расскажи про неизвестную тему микросетей"), worker.DedupStore(":memory:"), trace_store=worker.TraceStore(":memory:"), review_store=review)

    assert outbound == []
    pending = review.list_pending()
    assert len(pending) == 1
    assert pending[0]["kind"] == "public_question"
    assert pending[0]["text"] == "Расскажи про неизвестную тему микросетей"
