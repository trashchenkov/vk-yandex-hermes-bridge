from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-yandex-function.sh"
README = ROOT / "README.md"
MAKEFILE = ROOT / "Makefile"


def test_deploy_helper_exists_and_has_safe_shell_contract():
    text = SCRIPT.read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "DRY_RUN" in text
    assert "scripts/build-yandex-function-zip.sh" in text
    assert "yc serverless function version create" in text
    assert "VK_CALLBACK_URL" in text
    assert "VK_SECRET" not in text or "${VK_SECRET" in text
    assert "AWS_SECRET_ACCESS_KEY" not in text or "${AWS_SECRET_ACCESS_KEY" in text


def test_deploy_helper_is_executable_and_shell_syntax_is_valid():
    assert SCRIPT.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(SCRIPT)], cwd=ROOT, check=True)


def test_deploy_helper_dry_run_does_not_require_yc_or_network():
    env = os.environ.copy()
    env.update(
        {
            "DRY_RUN": "1",
            "FUNCTION_NAME": "vk-hermes-callback",
            "SERVICE_ACCOUNT_ID": "sa-test",
            "OUT": "/tmp/vk-hermes-test.zip",
        }
    )
    result = subprocess.run(
        [str(SCRIPT), "--skip-build"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "DRY RUN" in result.stdout
    assert "yc serverless function version create" in result.stdout
    assert "vk-hermes-callback" in result.stdout
    assert "VK Callback URL:" in result.stdout


def test_readme_and_makefile_document_deploy_helper():
    readme = README.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "scripts/deploy-yandex-function.sh" in readme
    assert "make deploy-function-dry-run" in readme
    assert ".PHONY: check deploy-function-dry-run" in makefile or "deploy-function-dry-run" in makefile
