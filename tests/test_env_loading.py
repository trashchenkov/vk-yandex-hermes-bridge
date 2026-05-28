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


def test_load_dotenv_ignores_inaccessible_default_path(monkeypatch):
    worker = load_worker()

    class InaccessiblePath:
        def __init__(self, path):
            self.path = path

        def exists(self):
            raise PermissionError("permission denied")

    monkeypatch.setattr(worker, "Path", InaccessiblePath)

    worker.load_dotenv("/root/.hermes/.env")
