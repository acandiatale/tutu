#!/usr/bin/env bash
# KB demo (v2): the same project migrated twice with ONE demo KB — the second run reuses the fix script the first run
# stored, without a fix LLM call.
#   run 1: the build fails on a case only an LLM can fix (boot3: Boot 4 renamed @JsonComponent to @JacksonComponent,
#          case 8dc5ebcf6d) → the replayed LLM writes a fix script → stored as <kb>/scripts/<case>/v1.py → BUILD SUCCESS
#   run 2: a fresh copy of the same project, same error → the stored v1 runs first (mode `reuse`, 0 fix LLM calls)
# The structure review is one LLM call in EVERY run (it is not a fix), so run 1 = 2 calls, run 2 = 1 call.
# The script ends with 2 successes in 1 project (the same root artifactId twice counts once), so its catalog case stays
# `candidate`: `kb.auto_promote_after_projects` (2) counts distinct projects — scripts/fleet-demo.sh shows the promotion.
#   scripts/demo-kb/run.sh                                   # boot3, replayed LLM (fixtures/projects/boot3/replay)
#   scripts/demo-kb/run.sh fixtures/projects/<fixture>       # another fixture with its own replay/ and sbmig.e2e.yml
#   LLM_API_KEY=... scripts/demo-kb/run.sh fixtures/projects/boot3 config/llm-anthropic.yml   # a real LLM (appended last)
# Needs mvn + JDK 21 (JAVA21_HOME) like scripts/e2e.sh. Writes only under out/demo-kb/ (runs/, kb/, run1.log, run2.log).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
export JAVA21_HOME="${JAVA21_HOME:-/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home}"
FIXTURE="${1:-fixtures/projects/boot3}"
FIXTURE="${FIXTURE%/}"
LLM_CFG="${2:-}"
DEMO=out/demo-kb
rm -rf "$DEMO" && mkdir -p "$DEMO"

CFG=(--config config/artifacts.local-e2e.yml)
# the fixture's own e2e override (artifact swaps and the like); its kb.dir / replay are overridden by the demo config
if [[ -f "$FIXTURE/sbmig.e2e.yml" ]]; then CFG+=(--config "$FIXTURE/sbmig.e2e.yml"); fi
CFG+=(--config scripts/demo-kb/config.yml)
if [[ "$(cd "$FIXTURE" && pwd)" != "$REPO/fixtures/projects/boot3" && -d "$FIXTURE/replay" ]]; then
  printf 'llm:\n  base_url: "replay:%s/replay"\n' "$FIXTURE" > "$DEMO/replay.yml"
  CFG+=(--config "$DEMO/replay.yml")
fi
if [[ -n "$LLM_CFG" ]]; then CFG+=(--config "$LLM_CFG"); fi
KB_CFG=("${CFG[@]}")
CFG+=(--out "$DEMO/runs")

echo "### run 1 — the LLM (replayed) writes a fix script; it is stored in the demo KB ($DEMO/kb)"
python -m sbmig.cli run "$FIXTURE" "${CFG[@]}" --run-id kb-run-1 > "$DEMO/run1.log" 2>&1 || true
echo "### run 2 — a fresh copy of the same project; the stored script must fix it without a fix LLM call"
python -m sbmig.cli run "$FIXTURE" "${CFG[@]}" --run-id kb-run-2 > "$DEMO/run2.log" 2>&1 || true

python - "$DEMO/runs" <<'PY'
import json
import sys
from pathlib import Path

runs = Path(sys.argv[1])
for rid in ("kb-run-1", "kb-run-2"):
    f = runs / rid / "report.json"
    if not f.is_file():
        print(f"{rid}: no report.json (see out/demo-kb/{'run1' if rid.endswith('1') else 'run2'}.log)")
        continue
    rep = json.loads(f.read_text(encoding="utf-8"))
    rv = rep.get("structure_review") or {}
    rv_calls = (rv.get("llm_usage") or {}).get("calls", 0)
    atts = [a for ph in rep.get("phases") or [] for a in ph.get("attempts") or [] if a.get("script_mode")]
    modes = {}
    for a in atts:
        modes[a["script_mode"]] = modes.get(a["script_mode"], 0) + 1
    calls = rep.get("llm_usage", {}).get("calls", 0)
    print(f"{rid}: success={rep.get('success')} llm_calls={calls} (structure review {rv_calls}, verdict "
          f"{rv.get('verdict') or '-'}; fix scripts {calls - rv_calls}) script attempts by mode: "
          f"{', '.join(f'{k} {v}' for k, v in sorted(modes.items())) or 'none'}")
    for a in atts:
        print(f"    {a.get('phase')} it{a.get('iteration')}: case {a.get('script_case_id')} v{a.get('script_version')} "
              f"{a['script_mode']:<7} → {a.get('outcome')} (LLM calls {(a.get('llm_usage') or {}).get('calls', 0)})")
PY
echo
echo "### sbmig kb scripts list (the demo KB)"
python -m sbmig.cli kb "${KB_CFG[@]}" scripts list
echo
echo "### sbmig kb stats (the demo KB)"
python -m sbmig.cli kb "${KB_CFG[@]}" stats
echo
echo "reports: $DEMO/runs/kb-run-1/report.md, $DEMO/runs/kb-run-2/report.md   script: sbmig kb --kb-dir $DEMO/kb scripts show <case>"
