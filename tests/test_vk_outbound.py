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


def test_build_vk_outbound_messages_keeps_short_message_single_chunk():
    worker = load_worker()

    outbound = worker.build_vk_outbound_messages("254662087", "коротко", trace_id="vk-test")

    assert outbound == [
        {"peer_id": "254662087", "message": "коротко", "random_id": worker.stable_random_id("vk-test", 0)}
    ]


def test_build_vk_outbound_messages_numbers_split_chunks_and_stabilizes_random_ids():
    worker = load_worker()
    long_text = "A" * (worker.VK_MAX_MESSAGE_CHARS + 100)

    first = worker.build_vk_outbound_messages("254662087", long_text, trace_id="vk-stable")
    second = worker.build_vk_outbound_messages("254662087", long_text, trace_id="vk-stable")

    assert len(first) == 2
    assert first == second
    assert first[0]["message"].startswith("[1/2]\n")
    assert first[1]["message"].startswith("[2/2]\n")
    assert len(first[0]["message"]) <= worker.VK_MAX_MESSAGE_CHARS
    assert len(first[1]["message"]) <= worker.VK_MAX_MESSAGE_CHARS
    assert first[0]["random_id"] != first[1]["random_id"]


def test_build_vk_outbound_messages_uses_different_random_ids_for_different_traces():
    worker = load_worker()

    first = worker.build_vk_outbound_messages("254662087", "same", trace_id="vk-one")
    second = worker.build_vk_outbound_messages("254662087", "same", trace_id="vk-two")

    assert first[0]["random_id"] != second[0]["random_id"]
