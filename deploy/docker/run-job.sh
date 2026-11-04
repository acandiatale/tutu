#!/usr/bin/env bash
# run-job.sh — the build Job's whole life (infra model 2026-11-05: one Job per request, emptyDir workspace, exit when done):
#   1. the source: git clone SBMIG_SOURCE_GIT_URL@REF (GIT_TOKEN), or a zip from SBMIG_SOURCE_ZIP_URL, or /workspace/in (mounted)
#   2. the KB: git clone SBMIG_KB_GIT_URL to /workspace/kb (the file KB must outlive the pod; sequential runs → push works)
#   3. sbmig run <source> --config <each yml in SBMIG_CONFIG> --config <paths yml: kb.dir, record_dir> --out /workspace/out
#   4. push: the migrated project (the run dir's git repo, branch sbmig/<run id>) to the source repository;
#      report.md / errors.md / sbmig.log / SBMIG-RESULT to SBMIG_RESULT_GIT_URL (branch <run id>) when set, else RESULT_PUSH_CMD
#      when set (gets the run dir as $1), else they stay in the pod's log only (printed); the KB back to its repository
#   5. exit 0 when a report exists (sbmig exit 0 or 2 — the report IS the result), non-zero on infrastructure failures /
#      sbmig exit 3; SBMIG_FAIL_JOB_ON_BUILD_FAIL=1 makes a failed build fail the Job too
# Secrets come from the environment (GIT_TOKEN, LLM_*) and never reach the log or the pushed files.
# SBMIG_PYTHON (default python) and SBMIG_WORKSPACE (default /workspace) exist for a dry run outside the image.
set -uo pipefail
WS="${SBMIG_WORKSPACE:-/workspace}"
OUT="$WS/out"; SRC="$WS/src"; KB="$WS/kb"
mkdir -p "$OUT" "$SRC"
log() { printf '[run-job] %s\n' "$*"; }
die() { log "ERROR: $*"; exit "${2:-1}"; }

# --- credentials for git over https: a token in the URL only in memory, never written
auth_url() {   # auth_url <https url> → url with user:token (GIT_USER default "oauth2")
  local url="$1"
  if [ -n "${GIT_TOKEN:-}" ] && [[ "$url" == https://* ]]; then
    printf 'https://%s:%s@%s' "${GIT_USER:-oauth2}" "$GIT_TOKEN" "${url#https://}"
  else
    printf '%s' "$url"
  fi
}
export GIT_TERMINAL_PROMPT=0

# --- 1. the source
if [ -n "${SBMIG_SOURCE_GIT_URL:-}" ]; then
  log "source: git clone ${SBMIG_SOURCE_GIT_URL} @ ${SBMIG_SOURCE_GIT_REF:-HEAD}"
  git clone --quiet --depth 1 ${SBMIG_SOURCE_GIT_REF:+--branch "$SBMIG_SOURCE_GIT_REF"} "$(auth_url "$SBMIG_SOURCE_GIT_URL")" "$SRC/project" \
    || die "git clone of the source failed" 3
  rm -rf "$SRC/project/.git"                       # sbmig makes its own repository of the copy (branch sbmig/<run id>)
  INPUT="$SRC/project"
elif [ -n "${SBMIG_SOURCE_ZIP_URL:-}" ]; then
  log "source: download ${SBMIG_SOURCE_ZIP_URL}"
  curl -fsSL ${GIT_TOKEN:+-H "Authorization: Bearer $GIT_TOKEN"} -o "$SRC/project.zip" "$SBMIG_SOURCE_ZIP_URL" || die "download failed" 3
  INPUT="$SRC/project.zip"
elif [ -d "$WS/in" ] && [ -n "$(ls -A "$WS/in" 2>/dev/null)" ]; then
  INPUT="$(find "$WS/in" -maxdepth 1 -mindepth 1 | head -1)"
  log "source: mounted $INPUT"
else
  die "no source: set SBMIG_SOURCE_GIT_URL or SBMIG_SOURCE_ZIP_URL, or mount /workspace/in" 3
fi

# --- 2. the KB
PATHS_YML="$WS/paths.yml"
if [ -n "${SBMIG_KB_GIT_URL:-}" ]; then
  log "kb: git clone ${SBMIG_KB_GIT_URL}"
  git clone --quiet "$(auth_url "$SBMIG_KB_GIT_URL")" "$KB" || die "git clone of the KB repository failed" 3
  KB_DIR="$KB"
else
  KB_DIR="$WS/kb-local"; mkdir -p "$KB_DIR"
  log "kb: no SBMIG_KB_GIT_URL — the KB of this run is NOT kept (nothing learns across projects)"
fi
printf 'kb:\n  dir: %s\nllm:\n  record_dir: %s\n' "$KB_DIR" "$OUT/llm-records" > "$PATHS_YML"

# --- 3. the run
CFG_OPTS=()
for c in ${SBMIG_CONFIG:-}; do CFG_OPTS+=(--config "$c"); done        # one or more yml paths, space-separated
CFG_OPTS+=(--config "$PATHS_YML")
log "sbmig run $INPUT ${CFG_OPTS[*]} --out $OUT"
cd /app
"${SBMIG_PYTHON:-python}" -m sbmig.cli run "$INPUT" "${CFG_OPTS[@]}" --out "$OUT"
RC=$?
RUN_DIR="$(ls -dt "$OUT"/*/ 2>/dev/null | head -1)"; RUN_DIR="${RUN_DIR%/}"
[ -n "$RUN_DIR" ] || die "no run directory under $OUT (sbmig exit $RC)" "$RC"
RUN_ID="$(basename "$RUN_DIR")"
log "sbmig exit $RC — run dir $RUN_DIR"
grep -h 'SBMIG-RESULT' "$RUN_DIR/sbmig.log" 2>/dev/null | tail -1 || true

# --- 4. push the results
push_fail=0
if [ -n "${SBMIG_SOURCE_GIT_URL:-}" ] && [ -d "$RUN_DIR/project/.git" ]; then
  BR="${SBMIG_RESULT_BRANCH_PREFIX:-sbmig/}${RUN_ID}"
  log "push: the migrated project → ${SBMIG_SOURCE_GIT_URL} branch $BR"
  ( cd "$RUN_DIR/project" && git push --quiet "$(auth_url "$SBMIG_SOURCE_GIT_URL")" "HEAD:refs/heads/$BR" ) || { log "WARN: project push failed"; push_fail=1; }
fi
if [ -n "${SBMIG_RESULT_GIT_URL:-}" ]; then
  RES="$WS/result"; mkdir -p "$RES"
  for f in report.md report.json errors.md errors.jsonl journal.jsonl sbmig.log plan.json profile.json; do
    [ -f "$RUN_DIR/$f" ] && cp "$RUN_DIR/$f" "$RES/"
  done
  [ -d "$RUN_DIR/logs" ] && cp -r "$RUN_DIR/logs" "$RES/logs"
  log "push: report / logs → ${SBMIG_RESULT_GIT_URL} branch $RUN_ID"
  ( cd "$RES" && git init -q && git checkout -q -b "$RUN_ID" && git add -A && git commit -q -m "sbmig $RUN_ID" \
    && git push --quiet "$(auth_url "$SBMIG_RESULT_GIT_URL")" "HEAD:refs/heads/$RUN_ID" ) || { log "WARN: result push failed"; push_fail=1; }
elif [ -n "${RESULT_PUSH_CMD:-}" ]; then
  log "push: RESULT_PUSH_CMD $RUN_DIR"
  bash -c "$RESULT_PUSH_CMD" _ "$RUN_DIR" || { log "WARN: RESULT_PUSH_CMD failed"; push_fail=1; }
else
  log "no result sink configured — report.md follows (the pod log is the only copy)"; sed -n '1,120p' "$RUN_DIR/report.md" 2>/dev/null || true
fi
if [ -n "${SBMIG_KB_GIT_URL:-}" ]; then
  ( cd "$KB" && git add -A && { git diff --cached --quiet || git commit -q -m "sbmig $RUN_ID"; } \
    && git push --quiet "$(auth_url "$SBMIG_KB_GIT_URL")" HEAD ) || { log "WARN: KB push failed (another run pushed first? runs must be sequential)"; push_fail=1; }
fi

# --- 5. exit
if [ "$RC" -eq 3 ]; then exit 3; fi                                   # aborted (preflight / ADF / internal): the Job fails
if [ "$push_fail" -ne 0 ]; then exit 4; fi                             # the run worked, the results did not leave the pod
if [ "$RC" -eq 2 ] && [ "${SBMIG_FAIL_JOB_ON_BUILD_FAIL:-0}" = "1" ]; then exit 2; fi
exit 0
