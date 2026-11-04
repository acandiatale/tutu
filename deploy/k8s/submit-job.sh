#!/usr/bin/env bash
# submit-job.sh — the agent side of the infra model (guide 2026-11-05): fill the Job template for one request, check the
# permission and the server-side validity, create the Job (generateName → create), follow its log, report the result.
# Usage: KUBECONFIG=/etc/kube/config submit-job.sh <template.yaml> <namespace> SBMIG_SOURCE_GIT_URL=… [SBMIG_SOURCE_GIT_REF=…] [--no-follow]
# Requires in the kubeconfig's account: jobs create/get/delete, pods get/list, pods/log get (namespace-scoped).
set -euo pipefail
TPL="${1:?template yaml}"; NS="${2:?namespace}"; shift 2
FOLLOW=1; ASSIGN=()
for a in "$@"; do case "$a" in --no-follow) FOLLOW=0 ;; *=*) ASSIGN+=("$a") ;; *) echo "unknown arg $a" >&2; exit 2 ;; esac; done
export KUBECONFIG="${KUBECONFIG:-/etc/kube/config}"
for kv in "${ASSIGN[@]}"; do export "$kv"; done
# fill the request values into the template (only the SBMIG_SOURCE_* / SBMIG_RESULT_* / SBMIG_KB_* placeholders are replaced)
FILLED="$(mktemp)"; trap 'rm -f "$FILLED"' EXIT
python3 - "$TPL" > "$FILLED" <<'PY'
import os, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
def sub(m):
    name = m.group(1); val = os.environ.get(name)
    return m.group(0) if val is None else f'{{name: {name}, value: "{val}"}}'
# `- {name: SBMIG_SOURCE_GIT_URL, value: "CHANGE_ME_…"}` → the environment's value when set
print(re.sub(r'\{name: (SBMIG_[A-Z_]+), value: "[^"]*"\}', sub, text), end="")
PY
echo "== permission"; kubectl -n "$NS" auth can-i create jobs.batch
echo "== server-side validation"; kubectl -n "$NS" create --dry-run=server -f "$FILLED" -o name
echo "== create"; JOB="$(kubectl -n "$NS" create -f "$FILLED" -o name)"; echo "$JOB"
JOBNAME="${JOB#job.batch/}"
if [ "$FOLLOW" = 1 ]; then
  echo "== waiting for the pod"; kubectl -n "$NS" wait --for=condition=Ready "pod" -l "job-name=$JOBNAME" --timeout=600s >/dev/null 2>&1 || true
  echo "== log"; kubectl -n "$NS" logs -f "job/$JOBNAME" -c build || true
  echo "== result"; kubectl -n "$NS" get "job/$JOBNAME" -o jsonpath='{.status.succeeded}/{.status.failed}{"\n"}'
  kubectl -n "$NS" logs "job/$JOBNAME" -c build 2>/dev/null | grep -h 'SBMIG-RESULT' | tail -1 || true
fi
echo "cleanup when done: kubectl -n $NS delete job $JOBNAME"
