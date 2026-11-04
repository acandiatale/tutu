#!/usr/bin/env bash
# run-job.sh — 빌드 Job 파드의 생애 전체 (인프라 모델 2026-11-05: 요청마다 Job 하나, emptyDir 작업 공간, 끝나면 종료):
#   1. 소스: SBMIG_SOURCE_GIT_URL@REF 를 git clone (GIT_TOKEN), 또는 SBMIG_SOURCE_ZIP_URL 의 zip, 또는 마운트된 /workspace/in
#   2. KB: SBMIG_KB_GIT_URL 을 /workspace/kb 로 clone (파일 KB 는 파드보다 오래 살아야 한다; 실행이 순차라 push 가 충돌하지 않는다)
#   3. sbmig run <소스> --config <SBMIG_CONFIG 의 yml 들> --config <경로 yml: kb.dir, record_dir> --out /workspace/out
#   4. push: 이관된 프로젝트(실행 디렉터리의 git 저장소, 브랜치 sbmig/<run id>)를 소스 저장소로;
#      report.md / errors.md / sbmig.log / journal 을 SBMIG_RESULT_GIT_URL(브랜치 <run id>)로, 없으면 RESULT_PUSH_CMD(인자 $1 = 실행 디렉터리),
#      그것도 없으면 파드 로그에만 출력; KB 를 그 저장소로 다시 push
#   5. 종료 코드: 보고서가 있으면 0 (sbmig exit 0 또는 2 — 보고서가 곧 결과), 3 = sbmig 중단(preflight / ADF / 내부 오류),
#      4 = 결과를 밖으로 못 보냄; SBMIG_FAIL_JOB_ON_BUILD_FAIL=1 이면 빌드 실패도 Job 실패(2)
# 비밀값(GIT_TOKEN, LLM_*)은 환경에서만 읽고 URL 에 메모리 안에서만 붙인다 — 로그와 push 되는 파일에 남지 않는다.
# SBMIG_PYTHON(기본 python), SBMIG_WORKSPACE(기본 /workspace)는 이미지 밖에서 모의 실행할 때 쓴다.
set -uo pipefail
WS="${SBMIG_WORKSPACE:-/workspace}"
OUT="$WS/out"; SRC="$WS/src"; KB="$WS/kb"
mkdir -p "$OUT" "$SRC"
log() { printf '[run-job] %s\n' "$*"; }
die() { log "ERROR: $*"; exit "${2:-1}"; }

# --- https git 인증: 토큰은 메모리 안의 URL 에만, 파일에는 쓰지 않는다
auth_url() {   # auth_url <https url> → user:token 이 붙은 url (GIT_USER 기본 "oauth2")
  local url="$1"
  if [ -n "${GIT_TOKEN:-}" ] && [[ "$url" == https://* ]]; then
    printf 'https://%s:%s@%s' "${GIT_USER:-oauth2}" "$GIT_TOKEN" "${url#https://}"
  else
    printf '%s' "$url"
  fi
}
export GIT_TERMINAL_PROMPT=0

# --- 1. 소스
if [ -n "${SBMIG_SOURCE_GIT_URL:-}" ]; then
  log "source: git clone ${SBMIG_SOURCE_GIT_URL} @ ${SBMIG_SOURCE_GIT_REF:-HEAD}"
  git clone --quiet --depth 1 ${SBMIG_SOURCE_GIT_REF:+--branch "$SBMIG_SOURCE_GIT_REF"} "$(auth_url "$SBMIG_SOURCE_GIT_URL")" "$SRC/project" \
    || die "소스 git clone 실패" 3
  rm -rf "$SRC/project/.git"                       # sbmig 이 복사본에 자기 저장소를 만든다(브랜치 sbmig/<run id>)
  INPUT="$SRC/project"
elif [ -n "${SBMIG_SOURCE_ZIP_URL:-}" ]; then
  log "source: download ${SBMIG_SOURCE_ZIP_URL}"
  curl -fsSL ${GIT_TOKEN:+-H "Authorization: Bearer $GIT_TOKEN"} -o "$SRC/project.zip" "$SBMIG_SOURCE_ZIP_URL" || die "zip 다운로드 실패" 3
  INPUT="$SRC/project.zip"
elif [ -d "$WS/in" ] && [ -n "$(ls -A "$WS/in" 2>/dev/null)" ]; then
  INPUT="$(find "$WS/in" -maxdepth 1 -mindepth 1 | head -1)"
  log "source: mounted $INPUT"
else
  die "소스가 없다: SBMIG_SOURCE_GIT_URL 또는 SBMIG_SOURCE_ZIP_URL 을 주거나 /workspace/in 을 마운트할 것" 3
fi

# --- 2. KB
PATHS_YML="$WS/paths.yml"
if [ -n "${SBMIG_KB_GIT_URL:-}" ]; then
  log "kb: git clone ${SBMIG_KB_GIT_URL}"
  git clone --quiet "$(auth_url "$SBMIG_KB_GIT_URL")" "$KB" || die "KB 저장소 git clone 실패" 3
  KB_DIR="$KB"
else
  KB_DIR="$WS/kb-local"; mkdir -p "$KB_DIR"
  log "kb: SBMIG_KB_GIT_URL 없음 — 이 실행의 KB 는 보존되지 않는다(다음 프로젝트가 배우지 못함)"
fi
printf 'kb:\n  dir: %s\nllm:\n  record_dir: %s\n' "$KB_DIR" "$OUT/llm-records" > "$PATHS_YML"

# --- 3. 실행
CFG_OPTS=()
for c in ${SBMIG_CONFIG:-}; do CFG_OPTS+=(--config "$c"); done        # yml 경로 하나 이상, 공백으로 구분
CFG_OPTS+=(--config "$PATHS_YML")
log "sbmig run $INPUT ${CFG_OPTS[*]} --out $OUT"
cd /app
"${SBMIG_PYTHON:-python}" -m sbmig.cli run "$INPUT" "${CFG_OPTS[@]}" --out "$OUT"
RC=$?
RUN_DIR="$(ls -dt "$OUT"/*/ 2>/dev/null | head -1)"; RUN_DIR="${RUN_DIR%/}"
[ -n "$RUN_DIR" ] || die "$OUT 아래에 실행 디렉터리가 없다 (sbmig exit $RC)" "$RC"
RUN_ID="$(basename "$RUN_DIR")"
log "sbmig exit $RC — run dir $RUN_DIR"
grep -h 'SBMIG-RESULT' "$RUN_DIR/sbmig.log" 2>/dev/null | tail -1 || true

# --- 4. 결과 push
push_fail=0
if [ -n "${SBMIG_SOURCE_GIT_URL:-}" ] && [ -d "$RUN_DIR/project/.git" ]; then
  BR="${SBMIG_RESULT_BRANCH_PREFIX:-sbmig/}${RUN_ID}"
  log "push: 이관된 프로젝트 → ${SBMIG_SOURCE_GIT_URL} 브랜치 $BR"
  ( cd "$RUN_DIR/project" && git push --quiet "$(auth_url "$SBMIG_SOURCE_GIT_URL")" "HEAD:refs/heads/$BR" ) || { log "WARN: 프로젝트 push 실패"; push_fail=1; }
fi
if [ -n "${SBMIG_RESULT_GIT_URL:-}" ]; then
  RES="$WS/result"; mkdir -p "$RES"
  for f in report.md report.json errors.md errors.jsonl journal.jsonl sbmig.log plan.json profile.json; do
    [ -f "$RUN_DIR/$f" ] && cp "$RUN_DIR/$f" "$RES/"
  done
  [ -d "$RUN_DIR/logs" ] && cp -r "$RUN_DIR/logs" "$RES/logs"
  log "push: 보고서 / 로그 → ${SBMIG_RESULT_GIT_URL} 브랜치 $RUN_ID"
  ( cd "$RES" && git init -q && git checkout -q -b "$RUN_ID" && git add -A && git commit -q -m "sbmig $RUN_ID" \
    && git push --quiet "$(auth_url "$SBMIG_RESULT_GIT_URL")" "HEAD:refs/heads/$RUN_ID" ) || { log "WARN: 결과 push 실패"; push_fail=1; }
elif [ -n "${RESULT_PUSH_CMD:-}" ]; then
  log "push: RESULT_PUSH_CMD $RUN_DIR"
  bash -c "$RESULT_PUSH_CMD" _ "$RUN_DIR" || { log "WARN: RESULT_PUSH_CMD 실패"; push_fail=1; }
else
  log "결과 저장소 설정 없음 — report.md 를 출력한다 (파드 로그가 유일한 사본)"; sed -n '1,120p' "$RUN_DIR/report.md" 2>/dev/null || true
fi
if [ -n "${SBMIG_KB_GIT_URL:-}" ]; then
  ( cd "$KB" && git add -A && { git diff --cached --quiet || git commit -q -m "sbmig $RUN_ID"; } \
    && git push --quiet "$(auth_url "$SBMIG_KB_GIT_URL")" HEAD ) || { log "WARN: KB push 실패 (다른 실행이 먼저 push 했나? 실행은 순차여야 한다)"; push_fail=1; }
fi

# --- 5. 종료
if [ "$RC" -eq 3 ]; then exit 3; fi                                   # 중단(preflight / ADF / 내부 오류): Job 실패
if [ "$push_fail" -ne 0 ]; then exit 4; fi                             # 실행은 됐지만 결과가 파드를 떠나지 못함
if [ "$RC" -eq 2 ] && [ "${SBMIG_FAIL_JOB_ON_BUILD_FAIL:-0}" = "1" ]; then exit 2; fi
exit 0
