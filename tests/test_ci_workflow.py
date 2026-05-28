from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists_and_runs_required_checks():
    text = WORKFLOW.read_text(encoding="utf-8")

    required = [
        "pull_request",
        "push",
        "node --check yandex-vk-hermes-function/index.js",
        "npm ci --prefix yandex-vk-hermes-function",
        "python -m py_compile vm-worker/vk_hermes_worker.py",
        "python -m pytest -q",
        "bash -n scripts/setup-hermes-api-server.sh",
        "bash -n scripts/install-vk-hermes-worker-service.sh",
        "bash -n scripts/build-yandex-function-zip.sh",
    ]
    for phrase in required:
        assert phrase in text


def test_ci_workflow_does_not_reference_runtime_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")

    forbidden = [
        "VK_GROUP_TOKEN",
        "VK_SECRET",
        "HERMES_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "QUEUE_URL",
    ]
    for phrase in forbidden:
        assert phrase not in text
