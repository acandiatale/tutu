#!/usr/bin/env bash
# Fleet demo (offline): ONE agent migrates several fixture projects sequentially with one shared, fresh KB.
#   pure-java, boot2, boot3, spring5-xml, boot3-again
# boot3 runs with the replayed LLM of scripts/demo-kb (its project recipe for @JsonComponent is disabled there),
# so the LLM fixes @JsonComponent once → the case is labeled (annotation-replace/type-rename) → a recipe candidate
# is COMPILED from the labels (no LLM) → "boot3-again" (a fresh copy) gets it in its plan, needs 0 LLM calls,
# credits it → auto-promoted to active. Then `sbmig kb learning` and `sbmig fleet status` are printed.
#   scripts/fleet-demo.sh            # needs mvn + JDK 21 (JAVA21_HOME) like scripts/e2e.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
export JAVA21_HOME="${JAVA21_HOME:-/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home}"
DEMO="$REPO/out/fleet-demo"
rm -rf "$DEMO" && mkdir -p "$DEMO"

cat > "$DEMO/fleet-demo.yml" <<YML
# fresh KB for this demo only; promote a candidate after success in 1 other project
kb:
  dir: $DEMO/kb
  auto_promote_after_projects: 1
YML

P="$REPO/fixtures/projects"
cat > "$DEMO/manifest.yml" <<YML
projects:
  - {name: pure-java,   input: $P/pure-java}
  - {name: boot2,       input: $P/boot2}
  - {name: boot3,       input: $P/boot3, configs: [$REPO/scripts/demo-kb/config.yml]}
  - {name: spring5-xml, input: $P/spring5-xml}
  - {name: boot3-again, input: $P/boot3, configs: [$REPO/scripts/demo-kb/config.yml]}
YML

CFG=(--config config/artifacts.local-e2e.yml --config "$DEMO/fleet-demo.yml")
echo "### sbmig fleet run (log: out/fleet-demo/fleet.log)"
set +e
python -m sbmig.cli fleet run "$DEMO/manifest.yml" "${CFG[@]}" --out "$DEMO/runs" > "$DEMO/fleet.log" 2>&1
RC=$?
set -e
grep -E '^\[[0-9]+/[0-9]+\]' "$DEMO/fleet.log" || tail -20 "$DEMO/fleet.log"
echo "fleet exit code: $RC (0 = all success, 2 = some incomplete, 3 = stopped on abort)"

echo
echo "### sbmig kb learning"
python -m sbmig.cli kb "${CFG[@]}" learning
echo
echo "### sbmig kb symbols"
python -m sbmig.cli kb "${CFG[@]}" symbols
echo
echo "### sbmig kb list"
python -m sbmig.cli kb "${CFG[@]}" list
echo
echo "### sbmig fleet status"
python -m sbmig.cli fleet status "$DEMO/runs"
echo
echo "summary: out/fleet-demo/runs/fleet/summary.md   learning: out/fleet-demo/runs/fleet/learning.md"
