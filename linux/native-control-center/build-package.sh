#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VERSION="${1:-$(sed -nE 's/^version = "([^"]+)"/\1/p' "$SOURCE_ROOT/pyproject.toml" | head -n 1)}"

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
git -C "$SOURCE_ROOT" archive --format=tar HEAD -- \
  src configs migrations linux web/dist \
  pyproject.toml requirements.runtime.lock alembic.ini README.md LICENSE | \
  tar -xf - -C "$PACKAGE_ROOT/app"

cp "$SCRIPT_DIR/README.md" "$PACKAGE_ROOT/README.md"
cat >"$PACKAGE_ROOT/install.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$PACKAGE_DIR/app/linux/native-control-center/ashare-native-linux.sh" install "$@"
EOF
chmod 0755 "$PACKAGE_ROOT/install.sh"

mkdir -p "$DIST_DIR"
rm -f "$ARCHIVE" "$CHECKSUM"
tar -C "$STAGE_DIR" -czf "$ARCHIVE" "$PACKAGE_NAME"
(cd "$DIST_DIR" && sha256sum "$(basename "$ARCHIVE")" >"$(basename "$CHECKSUM")")

printf '%s\n' "$ARCHIVE"
printf '%s\n' "$CHECKSUM"
