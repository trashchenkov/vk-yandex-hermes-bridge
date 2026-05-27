#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${BRIDGE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON_BIN:-python3}"
VENV="$ROOT/.venv"
ENV_FILE="$ROOT/.env"
SERVICE=/etc/systemd/system/vk-hermes-worker.service

install -m 700 -d "$ROOT/state"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$ROOT/vm-worker/requirements.txt"
chmod +x "$ROOT/vm-worker/vk_hermes_worker.py"

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 /dev/null "$ENV_FILE"
  cat > "$ENV_FILE" <<'EOF'
# Fill these after creating Yandex Message Queue + SA static key.
QUEUE_URL=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=ru-central1
QUEUE_ENDPOINT=https://message-queue.api.cloud.yandex.net

# VK token for sending replies from the VM worker.
VK_GROUP_TOKEN=
VK_API_VERSION=5.199

# Production access control. Set your own VK user id here.
VK_ALLOWED_USERS=
VK_ALLOW_ALL_USERS=false
VK_UNAUTHORIZED_REPLY=Бот приватный. Доступ к Hermes Agent ограничен.

# Local Hermes API Server on this VM.
HERMES_API_BASE=http://127.0.0.1:8642
HERMES_MODEL=hermes-agent
HERMES_TIMEOUT_MS=120000
EOF
fi
chmod 600 "$ENV_FILE"

cat > "$SERVICE" <<EOF
[Unit]
Description=VK -> Yandex Message Queue -> Hermes worker
After=network-online.target hermes-gateway.service
Wants=network-online.target
Requires=hermes-gateway.service

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/python $ROOT/vm-worker/vk_hermes_worker.py --env $ENV_FILE --hermes-env /root/.hermes/.env
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vk-hermes-worker.service

echo "Installed vk-hermes-worker.service"
echo "Edit $ENV_FILE, then run: systemctl restart vk-hermes-worker.service"
