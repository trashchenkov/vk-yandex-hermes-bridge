from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "vm-worker" / "vk_hermes_worker.py"
FUNCTION_DIR = ROOT / "yandex-vk-hermes-function"


def load_worker():
    spec = importlib.util.spec_from_file_location("vk_hermes_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_mode_uses_safe_vk_chunk_limit():
    source = (FUNCTION_DIR / "index.js").read_text(encoding="utf-8")

    assert "const VK_MAX_MESSAGE_CHARS = 4000;" in source


def test_direct_mode_splits_long_cyrillic_message():
    script = r"""
const chunks = require('./index.js')._test.splitForVk('Я'.repeat(9000));
console.log(JSON.stringify(chunks));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=FUNCTION_DIR,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    chunks = json.loads(completed.stdout)

    assert len(chunks) >= 3
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks) == "Я" * 9000


def test_direct_mode_does_not_split_astral_unicode_surrogate_pair():
    script = r"""
const text = 'A' + '😀'.repeat(2000);
const chunks = require('./index.js')._test.splitForVk(text);
const encodedChunks = chunks.map((chunk) => new URLSearchParams({ message: chunk }).get('message'));
console.log(JSON.stringify(encodedChunks));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=FUNCTION_DIR,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    chunks = json.loads(completed.stdout)

    assert "".join(chunks) == "A" + "😀" * 2000
    assert all("\ufffd" not in chunk for chunk in chunks)


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


def test_build_vk_outbound_messages_stays_within_safe_vk_limit():
    worker = load_worker()
    text = "Я" * 9000

    outbound = worker.build_vk_outbound_messages("254662087", text, trace_id="vk-api-limit")

    assert len(outbound) >= 3
    assert all(len(item["message"]) <= 4000 for item in outbound)
    assert "".join(item["message"].split("\n", 1)[1] for item in outbound) == text


def test_build_vk_outbound_messages_preserves_astral_unicode():
    worker = load_worker()
    text = "😀" * 4500

    outbound = worker.build_vk_outbound_messages("254662087", text, trace_id="vk-emoji")

    assert all(len(item["message"]) <= 4000 for item in outbound)
    assert "".join(item["message"].split("\n", 1)[1] for item in outbound) == text


def test_build_vk_outbound_messages_splits_one_character_over_limit():
    worker = load_worker()
    text = "я" * (worker.VK_MAX_MESSAGE_CHARS + 1)

    outbound = worker.build_vk_outbound_messages("254662087", text, trace_id="vk-boundary")

    assert len(outbound) == 2
    assert outbound[0]["message"].startswith("[1/2]\n")
    assert outbound[1]["message"].startswith("[2/2]\n")


def test_build_vk_outbound_messages_keeps_exact_limit_single():
    worker = load_worker()
    text = "Ж" * worker.VK_MAX_MESSAGE_CHARS

    outbound = worker.build_vk_outbound_messages("254662087", text, trace_id="vk-exact-boundary")

    assert len(outbound) == 1
    assert outbound[0]["message"] == text


def test_reply_vk_sends_every_chunk_sequentially(monkeypatch):
    worker = load_worker()
    sent: list[tuple[str, str, int]] = []

    def fake_send(peer_id, message, *, random_id, attachment=None):
        if len(message) > 4000:
            raise RuntimeError("VK error 914: message is too long")
        sent.append((peer_id, message, random_id))

    monkeypatch.setattr(worker, "send_vk_message", fake_send)
    text = "кириллица " * 1500
    expected = worker.build_vk_outbound_messages("254662087", text, trace_id="vk-ordered")

    worker.reply_vk("254662087", text, trace_id="vk-ordered")

    assert sent == [
        (item["peer_id"], item["message"], item["random_id"])
        for item in expected
    ]


def test_reply_vk_sends_chunks_in_order_and_stops_after_failure(monkeypatch):
    worker = load_worker()
    attempted: list[str] = []

    def fake_send(peer_id, message, **kwargs):
        attempted.append(message)
        if len(attempted) == 2:
            raise RuntimeError("second chunk failed")

    monkeypatch.setattr(worker, "send_vk_message", fake_send)
    text = "абзац\n\n" + ("длинный текст " * 1000)

    try:
        worker.reply_vk("254662087", text, trace_id="vk-partial-failure")
    except RuntimeError as exc:
        assert str(exc) == "second chunk failed"
    else:
        raise AssertionError("reply_vk must propagate an intermediate send failure")

    assert attempted[0].startswith("[1/")
    assert attempted[1].startswith("[2/")
    assert len(attempted) == 2
