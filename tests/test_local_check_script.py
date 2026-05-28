from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "scripts" / "check.sh"
MAKEFILE = ROOT / "Makefile"


def test_check_script_exists_and_runs_core_quality_gates():
    text = CHECK_SCRIPT.read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "python -m py_compile vm-worker/vk_hermes_worker.py" in text
    assert "python -m pytest -q" in text
    assert "node --check yandex-vk-hermes-function/index.js" in text
    assert "bash -n scripts/setup-hermes-api-server.sh" in text
    assert "bash -n scripts/install-vk-hermes-worker-service.sh" in text
    assert "bash -n scripts/build-yandex-function-zip.sh" in text
    assert "bash -n scripts/deploy-yandex-function.sh" in text
    assert "docker compose config" in text
    assert "npm ci" not in text, "local check should not require network dependency install"


def test_check_script_is_executable_and_shell_syntax_is_valid():
    mode = CHECK_SCRIPT.stat().st_mode
    assert mode & 0o111, "scripts/check.sh must be executable"
    subprocess.run(["bash", "-n", str(CHECK_SCRIPT)], cwd=ROOT, check=True)


def test_readme_documents_make_check():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "make check" in readme
    assert "scripts/check.sh" in readme or "local checks" in readme


def test_makefile_exposes_check_target():
    text = MAKEFILE.read_text(encoding="utf-8")
    assert ".PHONY: check" in text
    assert "check:" in text
    assert "./scripts/check.sh" in text
