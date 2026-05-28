from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "docs" / "release-deploy-checklist.md"


def read_checklist() -> str:
    return CHECKLIST.read_text(encoding="utf-8")


def test_release_deploy_checklist_exists_with_mvp_scope():
    text = read_checklist()

    assert text.startswith("# MVP release/deploy checklist")
    assert "private-by-default" in text
    assert "VK Callback API → Yandex Cloud Function → Yandex Message Queue → VM worker → Hermes API → VK" in text


def test_release_deploy_checklist_covers_required_env_and_modes():
    text = read_checklist()

    for name in [
        "VK_OWNER_ID",
        "VK_GROUP_TOKEN",
        "HERMES_API_BASE",
        "QUEUE_URL",
        "DEDUP_DB",
        "TRACE_DB",
        "REVIEW_DB",
        "POISON_DB",
        "RATE_LIMIT_DB",
    ]:
        assert name in text

    assert "Queue mode" in text
    assert "Long Poll mode" in text
    assert "python vm-worker/vk_hermes_worker.py --doctor" in text
    assert "python vm-worker/vk_hermes_worker.py --health" in text
    assert "make check" in text


def test_release_deploy_checklist_covers_yandex_worker_and_rollback():
    text = read_checklist()

    for phrase in [
        "scripts/deploy-yandex-function.sh",
        "scripts/install-vk-hermes-worker-service.sh",
        "docker compose config",
        "systemctl restart vk-hermes-worker",
        "systemctl stop vk-hermes-worker",
        "git checkout",
        "VK_EMERGENCY_LOCKDOWN=true",
        "!trace",
        "!pending",
        "!poison",
    ]:
        assert phrase in text


def test_readme_links_release_deploy_checklist():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docs/release-deploy-checklist.md" in readme
