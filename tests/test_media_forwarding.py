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


def test_parse_outbound_media_keeps_text_and_allowed_local_files(monkeypatch, tmp_path):
    worker = load_worker()
    image = tmp_path / "plot.png"
    image.write_bytes(b"png")
    blocked = tmp_path / "malware.exe"
    blocked.write_bytes(b"exe")
    missing = tmp_path / "missing.pdf"
    monkeypatch.setenv("VK_MEDIA_MAX_BYTES", "100")

    parts = worker.parse_outbound_media_reply(f"ответ\nMEDIA:{image}\nMEDIA:{blocked}\nMEDIA:{missing}")

    assert parts["message"] == "ответ"
    assert parts["media_paths"] == [image]
    assert parts["warnings"] == [
        "MEDIA not attached: malware.exe unsupported_ext",
        "MEDIA not attached: missing.pdf missing_file",
    ]


def test_parse_outbound_media_rejects_oversized_files(monkeypatch, tmp_path):
    worker = load_worker()
    doc = tmp_path / "large.pdf"
    doc.write_bytes(b"x" * 10)
    monkeypatch.setenv("VK_MEDIA_MAX_BYTES", "5")

    parts = worker.parse_outbound_media_reply(f"MEDIA:{doc}")

    assert parts["message"] == ""
    assert parts["media_paths"] == []
    assert parts["warnings"] == ["MEDIA not attached: large.pdf too_large"]


def test_reply_vk_uploads_media_and_sends_attachment(monkeypatch, tmp_path):
    worker = load_worker()
    image = tmp_path / "plot.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    calls: list[dict] = []

    class FakeResponse:
        ok = True
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append({"url": url, "data": data, "files": files, "timeout": timeout})
        if url.endswith("photos.getMessagesUploadServer"):
            return FakeResponse({"response": {"upload_url": "https://upload.example/photo"}})
        if url == "https://upload.example/photo":
            return FakeResponse({"server": 1, "photo": "[]", "hash": "h"})
        if url.endswith("photos.saveMessagesPhoto"):
            return FakeResponse({"response": [{"owner_id": 10, "id": 20, "access_key": "secret"}]})
        if url.endswith("messages.send"):
            return FakeResponse({"response": 1})
        raise AssertionError(url)

    monkeypatch.setattr(worker.requests, "post", fake_post)

    worker.reply_vk("123", f"готово\nMEDIA:{image}", trace_id="media-trace")

    send = calls[-1]
    assert send["url"].endswith("messages.send")
    assert send["data"]["message"] == "готово"
    assert send["data"]["attachment"] == "photo10_20_secret"


def test_reply_vk_downloads_media_url_before_upload(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    calls: list[dict] = []

    class FakeResponse:
        ok = True
        status_code = 200
        content = b"pdf-bytes"

        def __init__(self, payload=None):
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_get(url, timeout=None):
        calls.append({"method": "get", "url": url, "timeout": timeout})
        return FakeResponse()

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append({"method": "post", "url": url, "data": data, "files": files, "timeout": timeout})
        if url.endswith("docs.getMessagesUploadServer"):
            return FakeResponse({"response": {"upload_url": "https://upload.example/doc"}})
        if url == "https://upload.example/doc":
            assert files and "file" in files
            return FakeResponse({"file": "uploaded-token"})
        if url.endswith("docs.save"):
            return FakeResponse({"response": {"doc": {"owner_id": 7, "id": 8}}})
        if url.endswith("messages.send"):
            return FakeResponse({"response": 1})
        raise AssertionError(url)

    monkeypatch.setattr(worker.requests, "get", fake_get)
    monkeypatch.setattr(worker.requests, "post", fake_post)

    worker.reply_vk("123", "см. файл\nMEDIA:https://files.example/report.pdf", trace_id="media-url")

    assert calls[0] == {"method": "get", "url": "https://files.example/report.pdf", "timeout": 60}
    assert calls[-1]["data"]["message"] == "см. файл"
    assert calls[-1]["data"]["attachment"] == "doc7_8"


def test_parse_outbound_media_accepts_allowed_remote_urls_and_rejects_bad_ext(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_MEDIA_ALLOWED_EXTS", "png,pdf")

    parts = worker.parse_outbound_media_reply("ответ\nMEDIA:https://cdn.example/plot.png\nMEDIA:https://cdn.example/run.exe")

    assert parts["message"] == "ответ"
    assert parts["media_paths"] == []
    assert parts["media_urls"] == ["https://cdn.example/plot.png"]
    assert parts["warnings"] == ["MEDIA not attached: run.exe unsupported_ext"]


def test_reply_vk_downloads_remote_media_then_uploads(monkeypatch, tmp_path):
    worker = load_worker()
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    monkeypatch.setenv("VK_MEDIA_MAX_BYTES", "100")
    calls: list[dict] = []

    class FakeResponse:
        ok = True
        status_code = 200

        def __init__(self, payload=None, content=b""):
            self._payload = payload or {}
            self.content = content

        def json(self):
            return self._payload

    def fake_get(url, timeout=None):
        calls.append({"method": "GET", "url": url, "timeout": timeout})
        assert url == "https://cdn.example/plot.png?token=secret"
        return FakeResponse(content=b"png")

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append({"method": "POST", "url": url, "data": data, "files": files, "timeout": timeout})
        if url.endswith("photos.getMessagesUploadServer"):
            return FakeResponse({"response": {"upload_url": "https://upload.example/photo"}})
        if url == "https://upload.example/photo":
            assert files["photo"][0] == "plot.png"
            return FakeResponse({"server": 1, "photo": "[]", "hash": "h"})
        if url.endswith("photos.saveMessagesPhoto"):
            return FakeResponse({"response": [{"owner_id": 10, "id": 20}]})
        if url.endswith("messages.send"):
            return FakeResponse({"response": 1})
        raise AssertionError(url)

    monkeypatch.setattr(worker.requests, "get", fake_get)
    monkeypatch.setattr(worker.requests, "post", fake_post)

    worker.reply_vk("123", "готово\nMEDIA:https://cdn.example/plot.png?token=secret", trace_id="remote-media")

    send = calls[-1]
    assert send["url"].endswith("messages.send")
    assert send["data"]["message"] == "готово"
    assert send["data"]["attachment"] == "photo10_20"


def test_reply_vk_degrades_when_remote_media_download_is_too_large(monkeypatch):
    worker = load_worker()
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    monkeypatch.setenv("VK_MEDIA_MAX_BYTES", "2")
    sent: list[dict] = []

    class FakeResponse:
        ok = True
        status_code = 200
        content = b"toolarge"

        def json(self):
            return {"response": 1}

    monkeypatch.setattr(worker.requests, "get", lambda url, timeout=None: FakeResponse())
    monkeypatch.setattr(worker.requests, "post", lambda url, data=None, files=None, timeout=None: sent.append(data) or FakeResponse())

    worker.reply_vk("123", "ответ\nMEDIA:https://cdn.example/plot.png", trace_id="remote-too-large")

    assert len(sent) == 1
    assert sent[0]["message"] == "ответ\n\nMEDIA not attached: plot.png download_failed"
    assert "attachment" not in sent[0]


def test_reply_vk_degrades_to_warning_when_media_upload_fails(monkeypatch, tmp_path):
    worker = load_worker()
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"pdf")
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    sent: list[dict] = []

    class FakeResponse:
        ok = True
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, data=None, files=None, timeout=None):
        if url.endswith("docs.getMessagesUploadServer"):
            return FakeResponse({"response": {}})
        if url.endswith("messages.send"):
            sent.append(data)
            return FakeResponse({"response": 1})
        raise AssertionError(url)

    monkeypatch.setattr(worker.requests, "post", fake_post)

    worker.reply_vk("123", f"ответ\nMEDIA:{doc}", trace_id="media-fail")

    assert len(sent) == 1
    assert sent[0]["message"] == "ответ\n\nMEDIA not attached: report.pdf upload_failed"
    assert "attachment" not in sent[0]
