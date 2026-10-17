#!/usr/bin/env bash
# Wrapper for scripts/e2e.py: activates the repo venv (if present) and forwards all arguments.
#   scripts/e2e.sh --only boot2 --stop-after spring
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$REPO/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO/.venv/bin/activate"
fi
export JAVA21_HOME="${JAVA21_HOME:-/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home}"
cd "$REPO"
exec python scripts/e2e.py "$@"
