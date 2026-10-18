#!/usr/bin/env bash
# Wrapper for scripts/e2e.py: activates the repo venv (if present), installs the simulated in-house libraries
# (fixtures/libs/install.sh — needed by the boot2-javax-lib* scenarios; pass --skip-libs to skip) and forwards
# all arguments. Exit code is non-zero when any fixture does not meet its expected.json "expect" block.
#   scripts/e2e.sh                                   # all fixtures, happy path + failure scenarios
#   scripts/e2e.sh --only boot2-javax-lib,missing-artifact
#   scripts/e2e.sh --expect-only                     # print the expectations only
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$REPO/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO/.venv/bin/activate"
fi
export JAVA21_HOME="${JAVA21_HOME:-/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home}"
cd "$REPO"
# the recipe catalog must agree with the config sets / reactive rules / base recipes before anything runs
python -m sbmig.cli recipes check --config config/artifacts.local-e2e.yml
exec python scripts/e2e.py --install-libs "$@"
