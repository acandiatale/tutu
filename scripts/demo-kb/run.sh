#!/usr/bin/env bash
# KB learning-loop demo: two migrations of the same error; the second one must not need the LLM.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
export JAVA21_HOME="${JAVA21_HOME:-/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home}"
FIXTURE="${1:-fixtures/projects/boot3}"
# Optional: a real LLM profile appended LAST (overrides the replayed fake), e.g.
#   LLM_API_KEY=sk-ant-... scripts/demo-kb/run.sh fixtures/projects/boot3 config/llm-anthropic.yml
LLM_CFG="${2:-}"
rm -rf out/demo-kb && mkdir -p out/demo-kb
CFG=(--config config/artifacts.local-e2e.yml --config scripts/demo-kb/config.yml)
if [[ -n "$LLM_CFG" ]]; then CFG+=(--config "$LLM_CFG"); fi
CFG+=(--out out/demo-kb/runs)

echo "### run 1 — the LLM (replayed) fixes @JsonComponent; the case is harvested into the KB"
python -m sbmig.cli run "$FIXTURE" "${CFG[@]}" --run-id kb-run-1 > out/demo-kb/run1.log 2>&1 || true
echo "### run 2 — fresh copy of the same project; the KB must fix it without the LLM"
python -m sbmig.cli run "$FIXTURE" "${CFG[@]}" --run-id kb-run-2 > out/demo-kb/run2.log 2>&1 || true

python - <<'PY'
import json
from pathlib import Path
for rid in ("kb-run-1", "kb-run-2"):
    rep = json.loads(Path(f"out/demo-kb/runs/{rid}/report.json").read_text())
    atts = [(a["phase"], a["decision"]["kind"], a["decision"].get("recipe_ids") or a["decision"].get("kb_case_ids") or a.get("model"), a["outcome"])
            for ph in rep["phases"] for a in ph["attempts"]]
    h = rep.get("harvest") or {}
    print(f"{rid}: success={rep['success']} llm_calls={rep['llm_usage']['calls']} attempts={atts} "
          f"harvest(cases={h.get('cases_created')}, candidates={h.get('candidates_created')}, credited={h.get('recipes_credited')})")
PY
echo "### knowledge base after the two runs"
python -m sbmig.cli kb --config config/artifacts.local-e2e.yml --config scripts/demo-kb/config.yml stats
python -m sbmig.cli kb --config config/artifacts.local-e2e.yml --config scripts/demo-kb/config.yml list
