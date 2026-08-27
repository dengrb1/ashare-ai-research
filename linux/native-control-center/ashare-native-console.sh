#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ASHARE_NATIVE_ROOT:-}"
if [[ -z "$ROOT" && -f "$SCRIPT_DIR/runtime-root.txt" ]]; then
  ROOT="$(<"$SCRIPT_DIR/runtime-root.txt")"
fi
if [[ -z "$ROOT" ]]; then
  ROOT="$HOME/.local/share/ashare-ai/runtime"
fi

for ((index = 1; index <= $#; index++)); do
  if [[ "${!index}" == "--root" && $((index + 1)) -le $# ]]; then
    next=$((index + 1))
    ROOT="${!next}"
  fi
done

PYTHON="python3"
if [[ -x "$ROOT/venv/bin/python" ]]; then
  PYTHON="$ROOT/venv/bin/python"
fi
exec "$PYTHON" "$SCRIPT_DIR/native_control_center.py" "$@"
