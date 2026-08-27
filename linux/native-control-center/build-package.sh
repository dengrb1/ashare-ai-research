#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VERSION="${1:-$(sed -nE 's/^version = "([^"]+)"/\1/p' "$SOURCE_ROOT/pyproject.toml" | head -n 1)}"
PACKAGE_SOURCE="${ASHARE_PACKAGE_SOURCE:-HEAD}"

if [[ -z "$VERSION" ]]; then
  echo "Unable to determine the application version." >&2
  exit 1
fi

DIST_DIR="$SCRIPT_DIR/dist"
PACKAGE_NAME="AshareAI-Linux-Native-Beta-v${VERSION}"
ARCHIVE="$DIST_DIR/${PACKAGE_NAME}.tar.gz"
CHECKSUM="$ARCHIVE.sha256"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ashare-ai-linux-package.XXXXXX")"

cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

PACKAGE_ROOT="$STAGE_DIR/$PACKAGE_NAME"
mkdir -p "$PACKAGE_ROOT/app"

# Package only the files the native runtime loads.  This excludes local state as
# well as development-only tooling, tests, and repository automation metadata.
if [[ "$PACKAGE_SOURCE" == "HEAD" ]]; then
  git -C "$SOURCE_ROOT" archive --format=tar HEAD -- \
    src configs migrations linux web/dist \
    pyproject.toml requirements.runtime.lock alembic.ini README.md LICENSE | \
    tar -xf - -C "$PACKAGE_ROOT/app"
else
  copy_path() {
    local relative="$1"
    local source="$SOURCE_ROOT/$relative"
    local destination="$PACKAGE_ROOT/app/$relative"
    if [[ -d "$source" ]]; then
      mkdir -p "$destination"
      cp -a "$source/." "$destination/"
    else
      mkdir -p "$(dirname -- "$destination")"
      cp -a "$source" "$destination"
    fi
  }
  for path in src configs migrations linux web/dist pyproject.toml requirements.runtime.lock alembic.ini README.md LICENSE; do
    copy_path "$path"
  done
fi

chmod 0755 "$PACKAGE_ROOT/app/linux/native-control-center/ashare-native-console.sh"

cp "$SCRIPT_DIR/README.md" "$PACKAGE_ROOT/README.md"
cat >"$PACKAGE_ROOT/install.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONTROLLER="$PACKAGE_DIR/app/linux/native-control-center/ashare-native-linux.sh"
GUI="$PACKAGE_DIR/app/linux/native-control-center/native_control_center.py"
ROOT="${ASHARE_NATIVE_ROOT:-$HOME/.local/share/ashare-ai/runtime}"
CONTROLLER_ARGS=(install --root "$ROOT" --source-root "$PACKAGE_DIR/app")
GUI_ARGS=()
for argument in "$@"; do
  if [[ "$argument" == "--help" || "$argument" == "-h" ]]; then
    exec "$CONTROLLER" install --help
  elif [[ "$argument" == "--no-autostart" ]]; then
    GUI_ARGS+=("$argument")
  else
    CONTROLLER_ARGS+=("$argument")
  fi
done
for ((index = 1; index <= $#; index++)); do
  if [[ "${!index}" == "--root" && $((index + 1)) -le $# ]]; then
    next=$((index + 1))
    ROOT="${!next}"
  fi
done
"$CONTROLLER" "${CONTROLLER_ARGS[@]}"
PYTHON="python3"
if [[ -x "$ROOT/venv/bin/python" ]]; then
  PYTHON="$ROOT/venv/bin/python"
fi
exec "$PYTHON" "$GUI" --controller "$CONTROLLER" --source-root "$PACKAGE_DIR/app" --root "$ROOT" --install-desktop "${GUI_ARGS[@]}"
EOF
chmod 0755 "$PACKAGE_ROOT/install.sh"

mkdir -p "$DIST_DIR"
rm -f "$ARCHIVE" "$CHECKSUM"
tar -C "$STAGE_DIR" -czf "$ARCHIVE" "$PACKAGE_NAME"
(cd "$DIST_DIR" && sha256sum "$(basename "$ARCHIVE")" >"$(basename "$CHECKSUM")")

printf '%s\n' "$ARCHIVE"
printf '%s\n' "$CHECKSUM"
