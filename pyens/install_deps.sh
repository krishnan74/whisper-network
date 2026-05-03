#!/usr/bin/env bash
# Install Python deps for pyens (works around macOS loading stale /usr/lib/libexpat.1.dylib).
#
# From repo root:
#   ./pyens/install_deps.sh
# From pyens folder:
#   ./install_deps.sh
#
# Optional: PYTHON=/opt/homebrew/bin/python3.11 ./install_deps.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ="$HERE/requirements.txt"

fix_expat_dyld_for_macos() {
  if [[ "$(uname)" != "Darwin" ]]; then
    return 0
  fi
  for d in /opt/homebrew/opt/expat/lib /usr/local/opt/expat/lib; do
    if [[ -d "$d" ]]; then
      export DYLD_LIBRARY_PATH="$d${DYLD_LIBRARY_PATH:+:${DYLD_LIBRARY_PATH}}"
      return 0
    fi
  done
  echo "brew install expat   # needed on macOS so Python's xml/parsers/expat loads" >&2
  exit 1
}

fix_expat_dyld_for_macos

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3.11 &>/dev/null; then PY="$(command -v python3.11)"
  elif command -v python3.12 &>/dev/null; then PY="$(command -v python3.12)"
  elif command -v python3 &>/dev/null; then PY="$(command -v python3)"
  else echo "No python3 on PATH"; exit 1;
  fi
fi

"$PY" -c "import xml.parsers.expat" >/dev/null 2>&1 || {
  echo "Python still fails on pyexpat. Run: brew install expat
Then re-run this script (it sets DYLD_LIBRARY_PATH to Homebrew expat)." >&2
  exit 1
}

echo "Using: $PY"
"$PY" -m ensurepip --upgrade 2>/dev/null || true
exec "$PY" -m pip install -r "$REQ"
