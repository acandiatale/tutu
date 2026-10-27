#!/usr/bin/env bash
# Fleet demo (v2, offline): ONE agent migrates six fixture projects sequentially with one shared, fresh KB:
#   pure-java, boot2, boot3, spring5-xml, boot3-again, java6-legacy
# Every project replays its own LLM answers (fixtures/projects/<name>/sbmig.e2e.yml → replay/, the same files
# scripts/e2e.py uses); the fleet overrides each project's kb.dir with the shared demo KB. What the later projects show:
#   * boot3 — the LLM writes the @JsonComponent → @JacksonComponent fix script (case 8dc5ebcf6d), stored as v1;
#     boot3-again — a fresh copy of boot3 under out/ (artifactId renamed, so the KB counts it as another project) —
#     runs the stored v1 first (`reuse`, 0 fix LLM calls)
#   * pure-java — the LLM writes the JAXB fix script (case 7a52b1cea8: add jakarta.xml.bind-api + jaxb-runtime);
#     java6-legacy hits the same case and reuses it (its own replay has no answer for it)
#   * each run still makes one LLM call for the structure review (not a fix)
# Both scripts end with successes in 2 projects → their catalog cases become `active` (kb.auto_promote_after_projects 2).
# Then the script attempts per project, `sbmig kb scripts list`, `sbmig fleet status` and learning.md "Fix scripts".
#   scripts/fleet-demo.sh            # needs mvn + JDK 21 (JAVA21_HOME) like scripts/e2e.sh; writes only out/fleet-demo/
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
export JAVA21_HOME="${JAVA21_HOME:-/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home}"
DEMO="$REPO/out/fleet-demo"
rm -rf "$DEMO" && mkdir -p "$DEMO"
P="$REPO/fixtures/projects"

# boot3-again: a fresh copy of boot3 = "another project with the same error" (another root artifactId)
cp -R "$P/boot3" "$DEMO/boot3-again"
rm -rf "$DEMO/boot3-again/target"
sed -i.bak -e 's#<artifactId>order-api</artifactId>#<artifactId>order-api-again</artifactId>#' \
           -e 's#<name>order-api</name>#<name>order-api-again</name>#' "$DEMO/boot3-again/pom.xml"
rm -f "$DEMO/boot3-again/pom.xml.bak"

cat > "$DEMO/fleet-demo.yml" <<YML
# fleet-wide: a fresh KB for this demo only (the fleet sets it for every project, whatever the project configs say)
kb:
  dir: $DEMO/kb
# fleet level only (preflight; no project runs on it): every project config below sets its replayed LLM
llm:
  provider: fake
YML

cat > "$DEMO/manifest.yml" <<YML
projects:
  - {name: pure-java,    input: $P/pure-java,     configs: [$P/pure-java/sbmig.e2e.yml]}
  - {name: boot2,        input: $P/boot2,         configs: [$P/boot2/sbmig.e2e.yml]}
  - {name: boot3,        input: $P/boot3,         configs: [$P/boot3/sbmig.e2e.yml]}
  - {name: spring5-xml,  input: $P/spring5-xml,   configs: [$P/spring5-xml/sbmig.e2e.yml]}
  - {name: boot3-again,  input: $DEMO/boot3-again, configs: [$P/boot3/sbmig.e2e.yml]}
  - {name: java6-legacy, input: $P/java6-legacy,  configs: [$P/java6-legacy/sbmig.e2e.yml]}
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
echo "### fix-script attempts per project (report.json phases[].attempts[])"
python - "$DEMO/runs" <<'PY'
import json
import sys
from pathlib import Path

runs = Path(sys.argv[1])
state = json.loads((runs / "fleet" / "state.json").read_text(encoding="utf-8"))
print(f"{'project':<14} {'llm':>3} {'review':>6}  attempts (case vN mode → outcome)")
for p in state.get("projects") or []:
    f = runs / str(p.get("run_id") or "-") / "report.json"
    if not f.is_file():
        print(f"{p.get('name'):<14} -    -       no report ({p.get('status')})")
        continue
    rep = json.loads(f.read_text(encoding="utf-8"))
    rv = rep.get("structure_review") or {}
    atts = [a for ph in rep.get("phases") or [] for a in ph.get("attempts") or [] if a.get("script_mode")]
    cells = [f"{a.get('script_case_id')} v{a.get('script_version')} {a['script_mode']} → {a.get('outcome')}" for a in atts]
    print(f"{p.get('name'):<14} {rep.get('llm_usage', {}).get('calls', 0):>3} "
          f"{(rv.get('llm_usage') or {}).get('calls', 0):>6}  {'; '.join(cells) or '-'}")
PY

echo
echo "### sbmig kb scripts list"
python -m sbmig.cli kb "${CFG[@]}" scripts list
echo
echo "### sbmig fleet status"
python -m sbmig.cli fleet status "$DEMO/runs"
echo
echo "### learning.md — Fix scripts"
sed -n '/^## Fix scripts/,$p' "$DEMO/runs/fleet/learning.md"
echo
echo "summary: out/fleet-demo/runs/fleet/summary.md   learning: out/fleet-demo/runs/fleet/learning.md"
