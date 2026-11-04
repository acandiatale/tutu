#!/usr/bin/env bash
# submit-job.sh — 인프라 모델(가이드 2026-11-05)의 Agent 쪽: 요청 하나에 대해 Job 템플릿을 채우고, 권한과 서버 측 문법을 확인하고,
# Job 을 만들고(generateName → create), 로그를 따라가며, 결과 줄을 보여 준다.
# 사용: KUBECONFIG=/etc/kube/config submit-job.sh <template.yaml> <namespace> SBMIG_SOURCE_GIT_URL=… [SBMIG_SOURCE_GIT_REF=…] [--no-follow]
# kubeconfig 계정에 필요한 권한: jobs create/get/delete, pods get/list, pods/log get (namespace 범위).
set -euo pipefail
TPL="${1:?template yaml}"; NS="${2:?namespace}"; shift 2
FOLLOW=1; ASSIGN=()
for a in "$@"; do case "$a" in --no-follow) FOLLOW=0 ;; *=*) ASSIGN+=("$a") ;; *) echo "모르는 인자 $a" >&2; exit 2 ;; esac; done
export KUBECONFIG="${KUBECONFIG:-/etc/kube/config}"
for kv in "${ASSIGN[@]}"; do export "$kv"; done
# 요청 값을 템플릿에 채운다 (SBMIG_* env 항목의 value 만 바꾼다; 환경에 없는 것은 템플릿 값 그대로)
FILLED="$(mktemp)"; trap 'rm -f "$FILLED"' EXIT
python3 - "$TPL" > "$FILLED" <<'PY'
import os, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
def sub(m):
    name = m.group(1); val = os.environ.get(name)
    return m.group(0) if val is None else f'{{name: {name}, value: "{val}"}}'
print(re.sub(r'\{name: (SBMIG_[A-Z_]+), value: "[^"]*"\}', sub, text), end="")
PY
echo "== 권한"; kubectl -n "$NS" auth can-i create jobs.batch
echo "== 서버 측 검증"; kubectl -n "$NS" create --dry-run=server -f "$FILLED" -o name
echo "== 생성"; JOB="$(kubectl -n "$NS" create -f "$FILLED" -o name)"; echo "$JOB"
JOBNAME="${JOB#job.batch/}"
if [ "$FOLLOW" = 1 ]; then
  echo "== 파드 대기"; kubectl -n "$NS" wait --for=condition=Ready "pod" -l "job-name=$JOBNAME" --timeout=600s >/dev/null 2>&1 || true
  echo "== 로그"; kubectl -n "$NS" logs -f "job/$JOBNAME" -c build || true
  echo "== 결과 (succeeded/failed)"; kubectl -n "$NS" get "job/$JOBNAME" -o jsonpath='{.status.succeeded}/{.status.failed}{"\n"}'
  kubectl -n "$NS" logs "job/$JOBNAME" -c build 2>/dev/null | grep -h 'SBMIG-RESULT' | tail -1 || true
fi
echo "끝난 뒤 정리: kubectl -n $NS delete job $JOBNAME"
