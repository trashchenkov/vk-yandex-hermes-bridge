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


def vk_event(from_id: int, attachments: list[dict], text: str = "посмотри файл") -> dict:
    return {
        "type": "message_new",
        "event_id": f"media-{from_id}",
        "object": {
            "message": {
                "id": from_id,
                "peer_id": from_id,
                "from_id": from_id,
                "text": text,
                "attachments": attachments,
            }
        },
    }


def test_trusted_photo_and_small_pdf_are_forwarded_to_hermes_input(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_TRUSTED_USERS", "2")
    monkeypatch.setenv("VK_MEDIA_MAX_BYTES", "1000")
    payload = vk_event(2, [
        {
            "type": "photo",
            "photo": {
                "sizes": [
                    {"type": "s", "url": "https://example.test/s.jpg", "width": 75, "height": 75},
                    {"type": "x", "url": "https://example.test/x.jpg", "width": 604, "height": 403},
                ]
            },
        },
        {
            "type": "doc",
            "doc": {
                "title": "manual.pdf",
                "url": "https://example.test/manual.pdf?access_key=secret",
                "size": 900,
                "ext": "pdf",
            },
        },
    ])
    vk = worker.normalize_vk_message(payload)

    rendered = worker.build_hermes_input(vk)

    assert "photo image forwarded" in rendered
    assert "https://example.test/x.jpg" in rendered
    assert "doc manual.pdf forwarded" in rendered
    assert "access_key=secret" not in rendered
    assert "access_key=[redacted]" in rendered


def test_large_or_unsupported_docs_degrade_without_url(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_OWNER_ID", "1")
    monkeypatch.setenv("VK_MEDIA_MAX_BYTES", "1000")
    vk = worker.normalize_vk_message(vk_event(1, [
        {"type": "doc", "doc": {"title": "huge.pdf", "url": "https://example.test/huge.pdf", "size": 5000, "ext": "pdf"}},
        {"type": "audio", "audio": {"url": "https://example.test/audio.mp3"}},
    ]))

    rendered = worker.build_hermes_input(vk)

    assert "huge.pdf not forwarded: too_large" in rendered
    assert "audio attachment not forwarded: unsupported_type" in rendered
    assert "https://example.test/huge.pdf" not in rendered
    assert "https://example.test/audio.mp3" not in rendered


def test_public_attachments_are_not_forwarded_even_when_public_hermes_enabled(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_POLICY_JSON", json.dumps({
        "roles": {"owner": {"ids": ["1"]}},
        "rules": {"public": {"action": "reply", "hermes_allowed": True, "reason": "public_profile_reply"}},
    }))
    vk = worker.normalize_vk_message(vk_event(9, [
        {"type": "doc", "doc": {"title": "public.pdf", "url": "https://example.test/public.pdf", "size": 10, "ext": "pdf"}},
    ]))

    rendered = worker.build_hermes_input(vk)

    assert "public.pdf not forwarded: untrusted_role" in rendered
    assert "https://example.test/public.pdf" not in rendered
