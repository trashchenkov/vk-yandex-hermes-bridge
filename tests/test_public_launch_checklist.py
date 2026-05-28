from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "docs" / "public-launch-checklist.md"
README = ROOT / "README.md"


def test_public_launch_checklist_exists_and_documents_required_gates():
    text = CHECKLIST.read_text(encoding="utf-8")

    required_phrases = [
        "Public auto-reply is disabled by default",
        "VK_PUBLIC_HANDOFF=true",
        "VK_ALLOW_ALL_USERS=false",
        "python3 -m pytest -q",
        "--smoke",
        "--doctor",
        "redteam",
        "audit",
        "trace",
        "rate limit",
        "source grounding",
        "emergency lockdown",
    ]
    for phrase in required_phrases:
        assert phrase in text


def test_readme_links_public_launch_checklist():
    readme = README.read_text(encoding="utf-8")

    assert "docs/public-launch-checklist.md" in readme
