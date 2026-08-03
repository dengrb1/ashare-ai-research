#!/usr/bin/env bash
set -euo pipefail

COMMAND="status"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$SCRIPT_DIR/runtime"
if [[ -f "$SCRIPT_DIR/../../pyproject.toml" && -d "$SCRIPT_DIR/../../src" ]]; then
  DEFAULT_ROOT="$HOME/.local/share/ashare-ai/runtime"
fi
ROOT="${ASHARE_NATIVE_ROOT:-$DEFAULT_ROOT}"
if [[ -z "${ASHARE_NATIVE_ROOT:-}" && -f "$SCRIPT_DIR/runtime-root.txt" ]]; then
  ROOT="$(<"$SCRIPT_DIR/runtime-root.txt")"
fi
SOURCE_ROOT=""
JSON=0
RESEARCH_MODE="SERIAL"
RESEARCH_WORKERS=0
WATCHDOG_INTERVAL=10

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|start|stop|restart|repair|doctor|status)
      COMMAND="$1"
      shift
      ;;
    --root|-r)
      ROOT="$2"
      shift 2
      ;;
    --source-root|-s)
      SOURCE_ROOT="$2"
      shift 2
      ;;
    --json|-j)
      JSON=1
      shift
      ;;
    --research-mode)
      RESEARCH_MODE="$2"
      shift 2
      ;;
    --research-workers)
      RESEARCH_WORKERS="$2"
      shift 2
      ;;
    --watchdog-interval)
      WATCHDOG_INTERVAL="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'EOF'
AshareAI Linux native controller

Commands:
  install, start, stop, restart, repair, status, doctor

Options:
  --root <path>
  --source-root <path>
  --json
  --research-mode SERIAL|DUAL
  --research-workers 0..2
  --watchdog-interval <seconds>
EOF
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

ROOT="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$ROOT")"
mkdir -p "$ROOT"/{config,state,logs,data,downloads,deps,web}
PYTHON_CONTROLLER="$SCRIPT_DIR/native_controller.py"
if [[ -f "$PYTHON_CONTROLLER" ]]; then
  controller_args=("$COMMAND" "--root" "$ROOT")
  if [[ -n "$SOURCE_ROOT" ]]; then controller_args+=("--source-root" "$SOURCE_ROOT"); fi
  if [[ "$JSON" -eq 1 ]]; then controller_args+=("--json"); fi
  if [[ "$COMMAND" == "status" ]]; then controller_args+=("--fast"); fi
  if [[ "$COMMAND" == "install" || "$COMMAND" == "start" || "$COMMAND" == "restart" ]]; then
    controller_args+=("--research-mode" "$RESEARCH_MODE" "--research-workers" "$RESEARCH_WORKERS" "--watchdog-interval" "$WATCHDOG_INTERVAL")
  fi
  exec python3 "$PYTHON_CONTROLLER" "${controller_args[@]}"
fi
DESIRED_STATE_PATH="$ROOT/state/desired-state.json"
WATCHDOG_LOG="$ROOT/logs/watchdog.log"

read_desired_state() {
  if [[ -f "$DESIRED_STATE_PATH" ]]; then
    python3 - "$DESIRED_STATE_PATH" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("desired_state", "STOPPED"))
except Exception:
    print("STOPPED")
PY
  else
    echo "STOPPED"
  fi
}

write_desired_state() {
  python3 - "$DESIRED_STATE_PATH" "$1" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"desired_state": sys.argv[2]}, ensure_ascii=False), encoding="utf-8")
PY
}

status_json() {
  local desired
  desired="$(read_desired_state)"
  python3 - "$desired" <<'PY'
import datetime as dt
import json
import sys
desired = sys.argv[1]
payload = {
    "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "scope": "LINUX_NATIVE_PROCESS_GROUP",
    "total_working_set_bytes": 0,
    "total_working_set_mib": 0,
    "services": [],
    "desired_state": desired,
    "runtime_healthy": False,
    "ports": {"postgres": 55432, "redis": 56379, "api": 58000, "searxng": 58080},
    "watchdog_task": {"registered": False, "state": "Missing", "task_name": "ashare-ai-native-watchdog"},
    "watchdog": None,
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
}

doctor_json() {
  python3 - "$ROOT" "$SOURCE_ROOT" <<'PY'
import json
import os
import sys
root, source = sys.argv[1], sys.argv[2]
checks = [
    {"check": "runtime-root", "status": "PASS", "detail": root},
    {"check": "source-root", "status": "PASS" if source and os.path.isdir(source) else "WARN", "detail": source or "not supplied"},
    {"check": "docker-processes", "status": "PASS", "detail": "linux native controller does not start Docker"},
    {"check": "dependency-lock", "status": "WARN", "detail": "linux lock file is pending"},
]
print(json.dumps(checks, ensure_ascii=False, indent=2))
PY
}

case "$COMMAND" in
  status)
    if [[ "$JSON" -eq 1 ]]; then status_json; else echo "Linux native runtime is $(read_desired_state)"; fi
    ;;
  doctor)
    if [[ "$JSON" -eq 1 ]]; then doctor_json; else echo "Linux native controller scaffold is present at $ROOT"; fi
    ;;
  stop)
    write_desired_state "STOPPED"
    echo "$(date -u +%FT%TZ) [INFO] linux native stop requested" >> "$WATCHDOG_LOG"
    echo "Linux native runtime desired state is STOPPED"
    ;;
  install|start|restart|repair)
    echo "$(date -u +%FT%TZ) [WARN] linux native $COMMAND requested before dependency implementation" >> "$WATCHDOG_LOG"
    echo "Linux native $COMMAND requires the linux dependency lock and process implementation described in README.md" >&2
    exit 3
    ;;
esac
