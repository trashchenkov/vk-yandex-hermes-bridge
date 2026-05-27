#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUNCTION_DIR="${FUNCTION_DIR:-$(cd "$SCRIPT_DIR/../yandex-vk-hermes-function" && pwd)}"
OUT="${OUT:-/tmp/vk-hermes-function.zip}"

cd "$FUNCTION_DIR"
npm install --omit=dev
rm -f "$OUT"
python3 - "$FUNCTION_DIR" "$OUT" <<'PY'
from pathlib import Path
import sys
import zipfile
root = Path(sys.argv[1])
out = Path(sys.argv[2])
include = [root / 'index.js', root / 'package.json', root / 'package-lock.json', root / 'node_modules']
with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    for item in include:
        if not item.exists():
            continue
        if item.is_file():
            zf.write(item, item.relative_to(root))
        else:
            for path in item.rglob('*'):
                if path.is_file():
                    zf.write(path, path.relative_to(root))
PY

echo "$OUT"
