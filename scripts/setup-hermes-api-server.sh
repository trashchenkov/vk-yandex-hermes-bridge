#!/usr/bin/env bash
set -euo pipefail

HERMES_ENV="${HERMES_ENV:-/root/.hermes/.env}"
API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
API_SERVER_PORT="${API_SERVER_PORT:-8642}"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run on the Hermes VM as root, or set HERMES_ENV to a writable .env path." >&2
  exit 1
fi

if [[ -z "${API_SERVER_KEY:-}" ]]; then
  API_SERVER_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
fi

install -m 700 -d "$(dirname "$HERMES_ENV")"
touch "$HERMES_ENV"
chmod 600 "$HERMES_ENV"
cp "$HERMES_ENV" "$HERMES_ENV.bak.$(date +%Y%m%d-%H%M%S)"

python3 - "$HERMES_ENV" "$API_SERVER_HOST" "$API_SERVER_PORT" "$API_SERVER_KEY" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
host, port, key = sys.argv[2:5]
updates = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": host,
    "API_SERVER_PORT": port,
    "API_SERVER_KEY": key,
}
lines = path.read_text(errors="replace").splitlines()
out = []
seen = set()
for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        name = stripped.split("=", 1)[0].strip()
        if name in updates:
            out.append(f"{name}={updates[name]}")
            seen.add(name)
            continue
    out.append(line)
for name, value in updates.items():
    if name not in seen:
        out.append(f"{name}={value}")
path.write_text("\n".join(out).rstrip() + "\n")
PY

echo "Hermes API Server env prepared in $HERMES_ENV"
echo "API_SERVER_HOST=$API_SERVER_HOST"
echo "API_SERVER_PORT=$API_SERVER_PORT"
echo "API_SERVER_KEY=<generated-or-updated; see $HERMES_ENV>"
echo
echo "Restart when ready: systemctl restart hermes-gateway.service"
echo "Smoke test: curl -H \"Authorization: Bearer <API_SERVER_KEY>\" http://127.0.0.1:${API_SERVER_PORT}/v1/models"
