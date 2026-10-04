#!/usr/bin/env bash
# TokenTier installer (macOS + Linux). Thin wrapper: all logic lives in bin/tokentier.
#   ./install.sh --dry-run      preview every change, write nothing
#   ./install.sh                install globally (~/.claude), asks once before writing
#   ./install.sh --help         all options
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "TokenTier needs python3 (3.9 or newer), but python3 was not found on PATH." >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "TokenTier needs Python 3.9 or newer; found $(python3 -V 2>&1)." >&2
  exit 1
fi
exec python3 "$HERE/bin/tokentier" install "$@"
