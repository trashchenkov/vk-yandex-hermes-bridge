#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT="${OUT:-/tmp/vk-hermes-function.zip}"
FUNCTION_NAME="${FUNCTION_NAME:-vk-hermes-callback}"
SERVICE_ACCOUNT_ID="${SERVICE_ACCOUNT_ID:-}"
RUNTIME="${RUNTIME:-nodejs20}"
ENTRYPOINT="${ENTRYPOINT:-index.handler}"
MEMORY="${MEMORY:-128m}"
EXECUTION_TIMEOUT="${EXECUTION_TIMEOUT:-30s}"
DRY_RUN="${DRY_RUN:-0}"
SKIP_BUILD=0

usage() {
  cat <<'EOF'
Usage: scripts/deploy-yandex-function.sh [--skip-build]

Builds the VK Callback Cloud Function artifact and creates a new Yandex Cloud
Function version with env vars supplied from the current environment or .env.

Safe defaults:
  DRY_RUN=1 prints the yc command shape and callback URL without network calls.
  --skip-build reuses OUT, default /tmp/vk-hermes-function.zip.

Required for real deploy:
  yc CLI authenticated and configured
  SERVICE_ACCOUNT_ID
  QUEUE_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
  QUEUE_ENDPOINT, VK_GROUP_ID, VK_CONFIRMATION_TOKEN, VK_SECRET
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build)
      SKIP_BUILD=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

required_env=(
  QUEUE_URL
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_REGION
  QUEUE_ENDPOINT
  VK_GROUP_ID
  VK_CONFIRMATION_TOKEN
  VK_SECRET
)

missing=()
if [[ -z "$SERVICE_ACCOUNT_ID" ]]; then
  missing+=(SERVICE_ACCOUNT_ID)
fi
for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    missing+=("$name")
  fi
done

if [[ "$DRY_RUN" != "1" && ${#missing[@]} -gt 0 ]]; then
  echo "Missing required env vars: ${missing[*]}" >&2
  exit 2
fi

if [[ "$SKIP_BUILD" != "1" ]]; then
  # Keep this path visible for docs/tests: scripts/build-yandex-function-zip.sh
  OUT="$OUT" "$SCRIPT_DIR/build-yandex-function-zip.sh" >/dev/null
fi

env_arg="BRIDGE_MODE=queue"
for name in "${required_env[@]}"; do
  env_arg+=",$name=${!name:-}"
done

VK_CALLBACK_URL="${VK_CALLBACK_URL:-https://functions.yandexcloud.net/${FUNCTION_ID:-<function-id>}}"
# Secret values are read by expansion such as ${VK_SECRET:-} and ${AWS_SECRET_ACCESS_KEY:-}; dry-run prints redacted placeholders only.

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY RUN: would create Yandex Cloud Function version"
  echo "yc serverless function version create --function-name $FUNCTION_NAME --runtime $RUNTIME --entrypoint $ENTRYPOINT --memory $MEMORY --execution-timeout $EXECUTION_TIMEOUT --source-path $OUT --service-account-id ${SERVICE_ACCOUNT_ID:-<SERVICE_ACCOUNT_ID>} --environment BRIDGE_MODE=queue,QUEUE_URL=***,AWS_ACCESS_KEY_ID=***,AWS_SECRET_ACCESS_KEY=***,AWS_REGION=***,QUEUE_ENDPOINT=***,VK_GROUP_ID=***,VK_CONFIRMATION_TOKEN=***,VK_SECRET=***"
  echo "VK Callback URL: $VK_CALLBACK_URL"
  exit 0
fi

if ! command -v yc >/dev/null 2>&1; then
  echo "yc CLI is required for real deploy. Install/configure yc or run DRY_RUN=1." >&2
  exit 2
fi

yc serverless function version create \
  --function-name "$FUNCTION_NAME" \
  --runtime "$RUNTIME" \
  --entrypoint "$ENTRYPOINT" \
  --memory "$MEMORY" \
  --execution-timeout "$EXECUTION_TIMEOUT" \
  --source-path "$OUT" \
  --service-account-id "$SERVICE_ACCOUNT_ID" \
  --environment "$env_arg"

echo "VK Callback URL: $VK_CALLBACK_URL"
