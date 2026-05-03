#!/usr/bin/env bash
# macOS often loads /usr/lib/libexpat.1.dylib, which breaks Homebrew Python’s pyexpat
# (pip cannot start). Prefer Homebrew’s expat library on DYLD_LIBRARY_PATH.
#
# Usage (from repo root ens-test):
#   ./pyens/run.sh python -m pyens info

set -euo pipefail

if [[ "$(uname)" == "Darwin" ]]; then
  if [[ -d "/opt/homebrew/opt/expat/lib" ]]; then
    export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib:${DYLD_LIBRARY_PATH:-}"
  elif [[ -d "/usr/local/opt/expat/lib" ]]; then
    export DYLD_LIBRARY_PATH="/usr/local/opt/expat/lib:${DYLD_LIBRARY_PATH:-}"
  else
    echo "Install expat so pyexpat works: brew install expat" >&2
  fi
fi

exec "$@"
