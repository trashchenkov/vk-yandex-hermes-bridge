#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Python syntax"
python -m py_compile vm-worker/vk_hermes_worker.py

echo "==> Python tests"
python -m pytest -q

echo "==> Node syntax"
node --check yandex-vk-hermes-function/index.js

echo "==> Shell syntax"
bash -n scripts/setup-hermes-api-server.sh
bash -n scripts/install-vk-hermes-worker-service.sh
bash -n scripts/build-yandex-function-zip.sh
bash -n scripts/deploy-yandex-function.sh

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "==> Docker Compose config"
  docker compose config >/dev/null
else
  echo "==> Docker Compose config skipped: docker compose is not available"
fi

echo "==> OK"
