"""쿠버네티스 Job 디스패치의 얇은 클라이언트 (2026-11-05, 웹 파드의 ``SBMIG_WEB_MODE=k8s-job``).

웹 파드는 업로드마다 Job 하나를 만들고(``81-web-job.template.yaml`` 의 틀에 실행별 env 를 채워), 상태를 지켜보고(Pending →
Running → Succeeded / Failed), 멈출 때 지운다. 그 셋이 전부라서 ``kubernetes`` 패키지 대신 ``requests`` 로 클러스터 안 API 를
직접 부른다 — 폐쇄망에 새 wheel 을 들일 일이 없고, 서비스 계정의 토큰·CA(``/var/run/secrets/kubernetes.io/serviceaccount``)로
인증한다(권한은 deploy/k8s/45-web-rbac.yaml). 로그 스트림·zip 전달·결과 회수는 공유 PVC 가 대신하므로 여기 없다.

A thin in-cluster client for the three calls the Job dispatch needs — create a Job from the template, read its state
(Job status + its pod's phase / container exit code), delete it — over `requests` with the service-account token and CA.
Logs, the zip and the results travel over the shared PVC, not over this client."""
from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

log = logging.getLogger("sbmig.web.k8s")

MODE_ENV = "SBMIG_WEB_MODE"                 # in-process (default) | k8s-job
NAMESPACE_ENV = "SBMIG_K8S_NAMESPACE"
TEMPLATE_ENV = "SBMIG_JOB_TEMPLATE"
POLL_ENV = "SBMIG_JOB_POLL_S"
STOP_GRACE_ENV = "SBMIG_JOB_STOP_GRACE_S"
DEFAULT_TEMPLATE = "deploy/k8s/81-web-job.template.yaml"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MODES = ("in-process", "k8s-job")
TERMINAL_PHASES = ("Succeeded", "Failed", "Missing")


class K8sError(RuntimeError):
    """클러스터 API 가 거부했거나 닿지 않을 때(상태 코드와 본문 앞부분을 담는다)."""


@dataclass
class JobState:
    """Job 하나의 지금 상태: ``phase`` (Creating | Pending | Running | Succeeded | Failed | Missing), 파드 이름, 컨테이너
    종료 코드(끝났을 때), 사람이 읽을 메시지(ImagePullBackOff 같은 대기 이유, Job 조건)."""
    phase: str
    pod: str | None = None
    exit_code: int | None = None
    message: str = ""

    @property
    def finished(self) -> bool:
        return self.phase in TERMINAL_PHASES


def job_name(run_id: str) -> str:
    """실행 id → Job 이름(``sbmig-<run id>``, RFC 1123: 소문자·숫자·``-``, 63자 이하).

    `sbmig-<run id>` made a valid RFC 1123 name (lower-case, `-`, at most 63 characters)."""
    base = re.sub(r"[^a-z0-9-]+", "-", f"sbmig-{run_id}".lower()).strip("-")
    return (base[:63].rstrip("-")) or "sbmig-run"


def render_job(template: str | Path | dict, run_id: str, env: dict[str, str], namespace: str | None = None) -> dict:
    """틀(yaml 경로 또는 dict)에 실행 하나를 채운다: ``metadata.name`` = job_name, ``generateName`` 제거, 라벨
    ``sbmig-run-id``, namespace(주어지면), 첫 컨테이너의 env 에 ``env`` 를 같은 이름은 바꾸고 없는 것은 덧붙인다. 틀은 손대지 않는다.

    Fill the template for one run: name, labels, namespace, and the first container's env (same names replaced)."""
    data = yaml.safe_load(Path(template).read_text(encoding="utf-8")) if not isinstance(template, dict) else template
    if not isinstance(data, dict) or data.get("kind") != "Job":
        raise K8sError(f"job template is not a Job manifest: {template}")
    job = copy.deepcopy(data)
    meta = job.setdefault("metadata", {})
    meta["name"] = job_name(run_id)
    meta.pop("generateName", None)
    if namespace:
        meta["namespace"] = namespace
    labels = meta.setdefault("labels", {})
    labels["sbmig-run-id"] = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id)[:63]
    containers = job.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    if not containers:
        raise K8sError("job template has no container")
    pod_labels = job["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})
    pod_labels["sbmig-run-id"] = labels["sbmig-run-id"]
    items = [e for e in (containers[0].get("env") or []) if isinstance(e, dict) and e.get("name") not in env]
    items += [{"name": k, "value": str(v)} for k, v in env.items()]
    containers[0]["env"] = items
    return job


class K8sClient:
    """클러스터 안 API 클라이언트. ``in_cluster()`` 가 서비스 계정 파일과 ``KUBERNETES_SERVICE_HOST`` 로 만든다; 테스트는
    ``session`` 에 가짜를 넣는다.

    In-cluster API client (`in_cluster()`); tests pass a fake `session`."""

    def __init__(self, base_url: str, token: str, namespace: str, verify: str | bool = True,
                 session: Any = None, timeout_s: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.verify = verify
        self.timeout_s = timeout_s
        self.session = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                         "Content-Type": "application/json"}

    @classmethod
    def in_cluster(cls, namespace: str | None = None, session: Any = None) -> K8sClient:
        """파드 안의 기본값: 토큰·CA·namespace 는 서비스 계정 디렉터리, 주소는 ``KUBERNETES_SERVICE_HOST`` / ``_PORT``.
        클러스터 밖이면 K8sError."""
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        token_file, ca_file, ns_file = SA_DIR / "token", SA_DIR / "ca.crt", SA_DIR / "namespace"
        if not host or not token_file.is_file():
            raise K8sError("not inside a Kubernetes cluster (no KUBERNETES_SERVICE_HOST / service-account token) — "
                           "SBMIG_WEB_MODE=k8s-job needs the web pod to run in the cluster")
        ns = (namespace or os.environ.get(NAMESPACE_ENV, "") or
              (ns_file.read_text(encoding="utf-8").strip() if ns_file.is_file() else "")).strip() or "default"
        return cls(f"https://{host}:{port}", token_file.read_text(encoding="utf-8").strip(), ns,
                   verify=str(ca_file) if ca_file.is_file() else True, session=session)

    # ---- http
    def _req(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
        try:
            r = self.session.request(method, self.base_url + path, headers=self._headers, verify=self.verify,
                                     timeout=self.timeout_s, **kw)
        except requests.RequestException as e:
            raise K8sError(f"{method} {path}: {type(e).__name__}: {e}") from e
        if r.status_code not in ok:
            raise K8sError(f"{method} {path}: HTTP {r.status_code}: {(r.text or '')[:300]}")
        return r

    def _jobs(self) -> str:
        return f"/apis/batch/v1/namespaces/{self.namespace}/jobs"

    # ---- the three calls
    def create_job(self, manifest: dict) -> str:
        """Job 을 만들고 이름을 돌려준다(이미 있으면 409 → K8sError)."""
        r = self._req("POST", self._jobs(), json=manifest)
        name = ((r.json() or {}).get("metadata") or {}).get("name") or manifest["metadata"]["name"]
        log.info("k8s: job %s created in %s", name, self.namespace)
        return str(name)

    def job_state(self, name: str) -> JobState:
        """Job + 그 파드의 상태 하나로: 없음 → Missing; status.succeeded / failed → Succeeded / Failed(컨테이너 종료 코드 포함);
        아니면 파드 phase(Pending / Running; 대기 이유를 메시지로), 파드가 아직 없으면 Creating."""
        r = self._req("GET", f"{self._jobs()}/{name}", ok=(200, 404))
        if r.status_code == 404:
            return JobState("Missing", message="job not found (deleted?)")
        status = (r.json() or {}).get("status") or {}
        pods = self._req("GET", f"/api/v1/namespaces/{self.namespace}/pods",
                         params={"labelSelector": f"job-name={name}"}).json().get("items") or []
        pods.sort(key=lambda p: ((p.get("metadata") or {}).get("creationTimestamp") or ""), reverse=True)
        pod = pods[0] if pods else None
        pod_name = ((pod or {}).get("metadata") or {}).get("name")
        pstatus = (pod or {}).get("status") or {}
        exit_code, message = None, ""
        for cs in pstatus.get("containerStatuses") or []:
            state = cs.get("state") or {}
            if "terminated" in state:
                exit_code = state["terminated"].get("exitCode")
                message = state["terminated"].get("reason") or message
            elif "waiting" in state:
                w = state["waiting"]
                message = " ".join(x for x in (w.get("reason"), w.get("message")) if x)[:300]
        if (status.get("succeeded") or 0) > 0:
            return JobState("Succeeded", pod_name, exit_code if exit_code is not None else 0, message)
        if (status.get("failed") or 0) > 0:
            cond = next((c for c in status.get("conditions") or [] if c.get("type") == "Failed"), {})
            why = " ".join(x for x in (cond.get("reason"), cond.get("message")) if x)[:300]
            return JobState("Failed", pod_name, exit_code, why or message or "job failed")
        if pod is None:
            return JobState("Creating", None, None, "no pod yet")
        phase = pstatus.get("phase") or "Pending"
        if phase in ("Succeeded", "Failed"):          # the pod ended, the Job status lags a moment behind
            return JobState(phase, pod_name, exit_code, message)
        return JobState(phase, pod_name, None, message)

    def delete_job(self, name: str) -> None:
        """Job 과 그 파드를 지운다(propagationPolicy Foreground). 이미 없으면 조용히."""
        self._req("DELETE", f"{self._jobs()}/{name}", ok=(200, 202, 404),
                  json={"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground"})
        log.info("k8s: job %s deleted", name)
