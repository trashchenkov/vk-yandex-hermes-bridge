from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


SECRET_ENV_NAMES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "VK_GROUP_TOKEN",
    "VK_SECRET",
    "VK_CONFIRMATION_TOKEN",
    "HERMES_API_KEY",
    "HERMES_PUBLIC_API_KEY",
    "API_SERVER_KEY",
}


def test_compose_runs_worker_with_env_file_and_state_volume():
    compose_path = ROOT / "docker-compose.yml"
    assert compose_path.exists()
    compose = yaml.safe_load(compose_path.read_text())

    worker = compose["services"]["vk-hermes-worker"]
    assert worker["build"]["dockerfile"] == "Dockerfile.worker"
    assert ".env" in worker.get("env_file", [])
    assert worker.get("restart") == "unless-stopped"
    assert "vk-hermes-state:/app/state" in worker.get("volumes", [])
    assert worker["command"][:2] == ["python", "vm-worker/vk_hermes_worker.py"]
    assert "vk-hermes-state" in compose.get("volumes", {})


def test_compose_does_not_inline_secrets():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    worker = compose["services"]["vk-hermes-worker"]
    env = worker.get("environment", {}) or {}
    assert not (SECRET_ENV_NAMES & set(env)), "secrets must come from .env, not inline compose environment"
    text = (ROOT / "docker-compose.yml").read_text()
    assert "sk-" not in text.lower()
    assert "secret" not in re.sub(r"AWS_SECRET_ACCESS_KEY|VK_SECRET", "", text)


def test_worker_dockerfile_installs_runtime_deps_without_copying_secrets():
    dockerfile = (ROOT / "Dockerfile.worker").read_text()
    assert "FROM python:" in dockerfile
    assert "requirements-vm-worker.txt" in dockerfile
    assert "pip install" in dockerfile
    assert "vm-worker/" in dockerfile
    forbidden = ["COPY .env", "ADD .env", "ENV HERMES_API_KEY", "ENV VK_GROUP_TOKEN", "ENV AWS_SECRET_ACCESS_KEY"]
    for snippet in forbidden:
        assert snippet not in dockerfile


def test_dockerignore_excludes_secret_and_runtime_state():
    entries = set((ROOT / ".dockerignore").read_text().splitlines())
    assert ".env" in entries
    assert "state/" in entries
    assert ".git/" in entries
    assert "__pycache__/" in entries


def test_worker_requirements_are_minimal_runtime_deps():
    reqs = (ROOT / "requirements-vm-worker.txt").read_text().splitlines()
    normalized = {re.split(r"[<>=!~]", line, 1)[0].strip() for line in reqs if line.strip() and not line.startswith("#")}
    assert {"boto3", "requests"}.issubset(normalized)
    assert "pytest" not in normalized


def test_readme_documents_docker_compose_tradeoffs():
    readme = (ROOT / "README.md").read_text()
    assert "docker compose" in readme.lower()
    assert "systemd" in readme.lower()
    assert "state volume" in readme.lower()
    assert "does not bake secrets" in readme.lower()
