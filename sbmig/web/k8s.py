"""웹 파드가 쿠버네티스 Job을 만들고 상태를 보고 지울 때 쓰는 API 클라이언트 모듈.
``SBMIG_WEB_MODE=k8s-job``일 때 업로드마다 ``81-web-job.template.yaml``에 실행 값을 채워 Job을 만든다.

Plain ``requests``, no ``kubernetes`` package: the calls are few, and the closed network should not take a
new package. Authentication is the pod's service-account token or the ``KUBECONFIG`` file; the permissions
needed are in ``deploy/k8s/45-web-rbac.yaml``. Logs, the zip and the result files go through the shared PVC,
not through this module.

Calls: create a Job from the template, read its state (Job status + its pod's phase / container exit code), delete
it; plus pod events (why a pod does not start) and SelfSubjectAccessReview (the startup permission check).
The caller is sbmig.web.runs.RunManager."""
from __future__ import annotations

import base64
import copy
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

from .contract import OWNER_LABEL, RUN_LABEL, is_secret_name

log = logging.getLogger("sbmig.web.k8s")

MODE_ENV = "SBMIG_WEB_MODE"                 # in-process(기본값), k8s-job, local-job 중 하나
NAMESPACE_ENV = "SBMIG_K8S_NAMESPACE"
TEMPLATE_ENV = "SBMIG_JOB_TEMPLATE"
POLL_ENV = "SBMIG_JOB_POLL_S"
STOP_GRACE_ENV = "SBMIG_JOB_STOP_GRACE_S"
STATUS_GRACE_ENV = "SBMIG_JOB_STATUS_GRACE_S"   # Job이 끝난 뒤 job-status.json이 보일 때까지 기다리는 초, 기본 90
EXIT_GRACE_ENV = "SBMIG_JOB_EXIT_GRACE_S"       # 결과 파일이 생긴 뒤 파드 종료를 기다리는 초, 기본 120. 넘기면 Job을 지운다
KUBECONFIG_ENV = "KUBECONFIG"              # 웹 파드에 Secret으로 마운트한 kubeconfig 경로, 회사 배포에서는 /etc/kube/config
DEFAULT_TEMPLATE = "deploy/k8s/81-web-job.template.yaml"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MODES = ("in-process", "k8s-job", "local-job")    # local-job은 파드 대신 로컬 서브프로세스로 Job 모드를 실행하는 개발용 모드
TERMINAL_PHASES = ("Succeeded", "Failed", "Missing")
NOT_STARTED_PHASES = ("Creating", "Pending")   # 파드가 없거나 컨테이너가 아직 시작하지 않은 상태
PENDING_NOTICE_ENV = "SBMIG_JOB_PENDING_NOTICE_S"   # 파드가 이 초 동안 시작하지 못하면 이유를 찾아 화면에 알린다. 기본 300, 0이면 끈다
TEMPLATE_INVALID = "MIGRATION_TEMPLATE_INVALID"   # 시작 검증에서 거부할 때 메시지 앞에 붙인다. 배포팀이 로그에서 이 값으로 찾는다
PLACEHOLDER = "CHANGE_ME"   # 저장소의 81 템플릿에 남아 있는 플레이스홀더. 채우지 않은 템플릿으로 만든 Job은 이미지를 받지 못한다
# Worker에서는 금지하는 환경 변수. LLM 원문 기록은 공유 볼륨에 소스를 남기고, 빌드 파드는 웹 API를 호출하지 않는다
FORBIDDEN_WORKER_ENV = ("LLM_RECORD_DIR", "SBMIG_WEB_TOKEN", "WEB_API_TOKEN")
# 시작할 때 확인하는 (verb, resource, group) 권한 목록. ``deploy/k8s/45-web-rbac.yaml``과 같아야 한다
REQUIRED_PERMISSIONS = tuple([(v, "jobs", "batch") for v in ("create", "get", "list", "watch", "delete")]
                             + [(v, "pods", "") for v in ("get", "list", "watch")])


def cluster_host_present() -> bool:
    """파드 안에서 실행 중인지 ``KUBERNETES_SERVICE_HOST``만 보고 확인한다. 서비스 계정 토큰은 보지 않는다.

    RunManager uses it to refuse k8s-job inside a cluster without a usable account instead of degrading to
    local-job."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def in_cluster_available() -> bool:
    """서비스 계정 토큰이 있는 클러스터 안 파드에서 실행 중인지 확인한다. 개발 PC에서는 False다.
    False이고 ``kubeconfig_path()``도 None이면 RunManager가 ``k8s-job``을 ``local-job``으로 바꾼다."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and (SA_DIR / "token").is_file()


def kubeconfig_path() -> Path | None:
    """``KUBECONFIG``의 경로 중 처음 존재하는 파일을 반환한다. 없으면 None을 반환한다.
    ``~/.kube/config``는 쓰지 않는다. 개발 PC에서는 다른 클러스터를 가리킬 수 있다.

    Several paths are split on ``os.pathsep``."""
    raw = (os.environ.get(KUBECONFIG_ENV) or "").strip()
    for part in raw.split(os.pathsep) if raw else []:
        p = Path(part.strip()).expanduser()
        if part.strip() and p.is_file():
            return p
    return None


def cluster_access_available() -> bool:
    """쿠버네티스 API를 호출할 수 있는지 확인한다. 서비스 계정 토큰이나 ``KUBECONFIG`` 파일 중 하나만 있으면 된다."""
    return in_cluster_available() or kubeconfig_path() is not None


class K8sError(RuntimeError):
    """클러스터 API가 요청을 거부했거나 연결되지 않을 때 던지는 예외 클래스.
    메시지에 HTTP 상태 코드와 응답 본문 앞부분이 들어간다."""


@dataclass
class JobState:
    """Job 하나의 상태를 담는 클래스.
    ``phase``는 Creating, Pending, Running, Succeeded, Failed, Missing 중 하나다.

    ``message`` is for people: a waiting reason such as ImagePullBackOff, or the Job's Failed condition."""
    phase: str
    pod: str | None = None
    exit_code: int | None = None
    message: str = ""
    # 이어받거나 지우기 전에 확인하는 소유 라벨. 빈 dict는 라벨이 없는 남의 Job이다
    # None: a client that cannot know labels (local subprocess, test fake); only then the Job name decides.
    labels: dict[str, str] | None = None
    # 아직 실행 중이거나 종료 중인 파드 수. Job이 끝난 상태여도 이 값이 0이 되어야 끝난 것으로 본다
    # A Job failed by activeDeadlineSeconds still has a pod whose Maven writes files during the termination grace.
    active_pods: int = 0
    # 파드가 시작하지 못한 이유. 컨테이너 waiting 상태와 PodScheduled=False 조건에서 읽는다
    waiting_reason: str = ""
    waiting_message: str = ""
    sched_reason: str = ""
    sched_message: str = ""

    @property
    def started(self) -> bool:
        """파드의 컨테이너가 한 번이라도 시작했는지 확인한다. Creating이나 Pending이면 False다."""
        return self.phase not in NOT_STARTED_PHASES

    @property
    def finished(self) -> bool:
        """Job이 완전히 끝나 슬롯을 다른 실행에 넘겨도 되는지 확인한다.
        종료 상태이고 실행 중이거나 종료 중인 파드가 하나도 없어야 True다."""
        return self.phase in TERMINAL_PHASES and not self.active_pods


# 파드가 시작하지 못한 이유를 종류(family)별로 나누고, 종류마다 화면에 보일 한국어 문장을 둔다
_IGNORED_WAITING = ("ContainerCreating", "PodInitializing")
_IMAGE_WAITING = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull", "RegistryUnavailable",
                  "ImageInspectError")
_WARNING_REASONS = ("FailedMount", "FailedAttachVolume", "FailedCreatePodSandBox", "Failed", "FailedScheduling")
POD_WAIT_KO = {
    "mount": "볼륨을 붙이지 못하고 있습니다. 인프라 담당자에게 문의해 주세요.",
    "multiattach": "볼륨이 다른 노드에 붙어 있어 이 노드에서 쓸 수 없습니다. 인프라 담당자에게 문의해 주세요.",
    "image": "이미지를 받지 못하고 있습니다.",
    "pulling": "이미지를 받는 중입니다. 이미지가 크면 몇 분 걸립니다.",
    "unschedulable": "파드를 띄울 노드가 없습니다. 자원이 부족할 수 있습니다.",
    "sandbox": "노드에서 파드 네트워크를 준비하지 못하고 있습니다. 인프라 담당자에게 문의해 주세요.",
    "config": "파드 설정을 읽지 못하고 있습니다. ConfigMap과 Secret을 확인해 주세요.",
    "unknown": "이유는 kubectl describe {kind} {name}의 Events에서 볼 수 있습니다.",
}


def _event_family(reason: str, message: str) -> str:
    # 경고 이벤트의 reason과 message로 이유 종류를 정한다
    if "Multi-Attach" in message:
        return "multiattach"
    if reason in ("FailedMount", "FailedAttachVolume"):
        return "mount"
    if reason == "FailedCreatePodSandBox":
        return "sandbox"
    if reason == "FailedScheduling":
        return "unschedulable"
    if reason == "Failed" and re.search(r"(?i)image|pull", message):
        return "image"
    return "unknown"


def pod_wait_reason(st: JobState, events: list[dict[str, str]] | None = None) -> tuple[str, str, str, str]:
    """시작하지 못한 파드의 이유를 ``(family, reason, message, source)``로 반환한다.
    이유를 찾지 못하면 family는 ``unknown``이다.

    Looked up in this order: (1) a container `waiting.reason` other than ContainerCreating / PodInitializing, (2) the
    `PodScheduled=False` condition, (3) the newest Warning event of the known reasons, else the newest Normal
    `Pulling`. `source` is waiting | condition | event | none."""
    wr = st.waiting_reason
    if wr and wr not in _IGNORED_WAITING:
        family = "image" if wr in _IMAGE_WAITING else ("config" if wr == "CreateContainerConfigError" else "unknown")
        return family, wr, st.waiting_message, "waiting"
    if st.sched_reason or st.sched_message:
        return "unschedulable", st.sched_reason or "Unschedulable", st.sched_message, "condition"
    evs = list(events or [])
    warn = next((e for e in evs if e.get("type") == "Warning" and e.get("reason") in _WARNING_REASONS), None)
    if warn is not None:
        return _event_family(warn["reason"], warn.get("message", "")), warn["reason"], warn.get("message", ""), "event"
    pulling = next((e for e in evs if e.get("type") == "Normal" and e.get("reason") == "Pulling"), None)
    if pulling is not None:
        return "pulling", "Pulling", pulling.get("message", ""), "event"
    return "unknown", "", "", "none"


def pod_wait_hint_ko(family: str, seconds: float, pod: str | None, job: str) -> str:
    """파드가 기다린 시간과 이유 종류로 화면에 보일 안내 문장 두 개를 만든다.

    Minutes are whole, at least 1; an unknown family points at ``kubectl describe`` of the pod (or the Job)."""
    minutes = max(1, int(seconds // 60))
    tail = POD_WAIT_KO.get(family) or POD_WAIT_KO["unknown"]
    if "{kind}" in tail:
        tail = tail.format(kind="pod" if pod else "job", name=pod or job)
    return f"빌드 파드가 아직 시작되지 못했습니다({minutes}분째). {tail}"


# 실행 id 끝의 시각과 난수. 같은 초에 시작한 두 실행은 이 난수로만 구분된다
_RUN_ID_TAIL = re.compile(r"-\d{8}-\d{6}-[0-9a-f]{6}$")


def job_name(run_id: str, limit: int = 63) -> str:
    """실행 id로 Job 이름 ``sbmig-<run id>``를 만든다.
    소문자, 숫자, ``-``만 쓴다. ``limit``자를 넘으면 중간을 잘라 낸다. 끝의 시각과 난수는 남긴다.

    RFC 1123. The `-YYYYmmdd-HHMMSS-<hex 6>` tail must survive the cut: two runs of one project started in the same
    second differ only in the random part, and a project name of up to 30 characters leads the run id (v2.1z-be)."""
    base = re.sub(r"[^a-z0-9-]+", "-", f"sbmig-{run_id}".lower()).strip("-")
    if len(base) <= limit:
        return base or "sbmig-run"
    m = _RUN_ID_TAIL.search(base)
    tail = m.group(0) if m and len(m.group(0)) < limit else ""
    return (base[:limit - len(tail)].rstrip("-") + tail) or "sbmig-run"


def render_job(template: str | Path | dict, run_id: str, env: dict[str, str], namespace: str | None = None,
               owner: str | None = None) -> dict:
    """Job 템플릿에 실행 하나의 값을 채운 매니페스트를 만든다. 원본 템플릿은 바꾸지 않는다.
    템플릿이 Job이 아니거나 컨테이너가 없으면 K8sError를 던진다.

    * ``metadata.name`` = job_name(run_id); ``generateName`` is dropped.
    * Labels ``sbmig-run-id`` and, with ``owner``, ``sbmig-owner`` (the per-request token) on the Job AND its pod
      template: RunManager adopts or deletes only a Job carrying both.
    * ``env`` is merged into the first container's env: same names replaced, new names appended."""
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
    labels[RUN_LABEL] = _label_value(run_id)
    if owner:
        labels[OWNER_LABEL] = _label_value(owner)
    containers = job.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    if not containers:
        raise K8sError("job template has no container")
    pod_labels = job["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})
    pod_labels[RUN_LABEL] = labels[RUN_LABEL]
    if owner:
        pod_labels[OWNER_LABEL] = labels[OWNER_LABEL]
    items = [e for e in (containers[0].get("env") or []) if isinstance(e, dict) and e.get("name") not in env]
    items += [{"name": k, "value": str(v)} for k, v in env.items()]
    containers[0]["env"] = items
    return job


def _label_value(value: str) -> str:
    # run id와 owner 토큰은 원래 라벨 값 규칙에 맞는다. 틀린 값이 와도 API가 422로 거부하지 않게 다듬는다
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value))[:63]


def _invalid(reason: str) -> K8sError:
    return K8sError(f"{TEMPLATE_INVALID}: {reason}")


# 템플릿을 읽지 못하거나 채우지 않았을 때 운영자에게 보여 주는 조치 안내
TEMPLATE_HINT = (f"the web pod reads the image's copy {DEFAULT_TEMPLATE} unless {TEMPLATE_ENV} names another file: fill "
                 f"{DEFAULT_TEMPLATE} in the checkout the image is built from, build and push the image and restart the web "
                 f"pod, or point {TEMPLATE_ENV} at a filled copy (deploy/README.md §3)")


def _placeholders(node: Any, path: str = "") -> list[str]:
    # 플레이스홀더가 남은 값의 위치를 찾는다. 주석은 YAML을 읽을 때 빠져서 세지 않는다
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _placeholders(v, f"{path}.{k}" if path else str(k))]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _placeholders(v, f"{path}[{i}]")]
    if isinstance(node, str) and PLACEHOLDER in node:
        return [f"{path}={node}"]
    return []


def validate_worker_template(template: str | Path | dict, out_dir: Path | None, *, allow_placeholders: bool = False) -> dict:
    """웹 파드가 시작할 때 Worker Job 템플릿을 검사한다. 통과하면 읽은 dict를 반환한다.
    규칙에 어긋나면 ``K8sError("MIGRATION_TEMPLATE_INVALID: <이유>")``를 던진다.

    Rules:
    * a ``batch/v1`` Job with exactly one container, no initContainers / ephemeralContainers;
    * ``backoffLimit`` 0, ``parallelism`` / ``completions`` 1, ``restartPolicy`` Never;
    * the container has a name and an image, no ``envFrom`` (one env entry per variable), no FORBIDDEN_WORKER_ENV name,
      no literal value for a secret-looking name (``contract.is_secret_name``: secretKeyRef only);
    * exactly one mount holds ``out_dir``: the whole, writable persistentVolumeClaim, no mount below it;
    * no value containing ``CHANGE_ME`` (comments are not parsed, so they do not count).
    ``out_dir=None`` skips the mount rules (an injected test client: the output dir is not a pod path);
    ``allow_placeholders=True`` skips the CHANGE_ME rule (an injected test client renders the unfilled template)."""
    if isinstance(template, dict):
        data: Any = template
    else:
        try:
            data = yaml.safe_load(Path(template).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise _invalid(f"job template {template} is not readable ({type(e).__name__}: {e}) — {TEMPLATE_HINT}") from e
    if not isinstance(data, dict) or data.get("apiVersion") != "batch/v1" or data.get("kind") != "Job":
        where = "" if isinstance(template, dict) else f" {template}"
        raise _invalid(f"the job template{where} is not a batch/v1 Job")
    spec = data.get("spec") or {}
    pspec = ((spec.get("template") or {}).get("spec")) or {}
    containers = pspec.get("containers") or []
    if not isinstance(containers, list) or len(containers) != 1:
        raise _invalid(f"the Worker pod must have exactly one container, the template has {len(containers) if isinstance(containers, list) else 0}")
    for extra in ("initContainers", "ephemeralContainers"):
        if pspec.get(extra):
            raise _invalid(f"{extra} are not allowed in the Worker pod")
    if int(spec.get("backoffLimit", 0) or 0) != 0:
        raise _invalid("spec.backoffLimit must be 0 (a migration is never retried blindly)")
    for key in ("parallelism", "completions"):
        if int(spec.get(key) if spec.get(key) is not None else 1) != 1:
            raise _invalid(f"spec.{key} must be 1")
    if (pspec.get("restartPolicy") or "Never") != "Never":
        raise _invalid("spec.template.spec.restartPolicy must be Never")
    c = containers[0] if isinstance(containers[0], dict) else {}
    if not c.get("name") or not c.get("image"):
        raise _invalid("the Worker container needs a name and an image")
    if c.get("envFrom"):
        raise _invalid("envFrom is not allowed — Worker environment references must be explicit (one env entry per variable)")
    for e in c.get("env") or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "")
        if name in FORBIDDEN_WORKER_ENV:
            raise _invalid(f"env {name} is not allowed in the Worker")
        if is_secret_name(name) and "value" in e:
            raise _invalid(f"env {name} has a literal value — credentials require valueFrom bindings (secretKeyRef)")
    if out_dir is not None:
        out = Path(out_dir)
        volumes = {v.get("name"): v for v in pspec.get("volumes") or [] if isinstance(v, dict)}
        mounts = [m for m in c.get("volumeMounts") or [] if isinstance(m, dict) and m.get("mountPath")]
        holding = [m for m in mounts if out.is_relative_to(Path(str(m["mountPath"])))]
        if len(holding) != 1:
            raise _invalid(f"exactly one volume mount must contain the output directory {out}, found {len(holding)}")
        m = holding[0]
        if "persistentVolumeClaim" not in (volumes.get(m.get("name")) or {}):
            raise _invalid(f"the mount {m['mountPath']} holding {out} must be a persistentVolumeClaim volume (shared with the web pod)")
        if m.get("readOnly") or m.get("subPath") or m.get("subPathExpr"):
            raise _invalid(f"the mount {m['mountPath']} holding {out} must be the whole claim, writable (no readOnly / subPath)")
        below = [str(x["mountPath"]) for x in mounts if x is not m and Path(str(x["mountPath"])).is_relative_to(out)]
        if below:
            raise _invalid(f"no mount may sit under the output directory {out}: {', '.join(below)}")
    left = [] if allow_placeholders else _placeholders(data)
    if left:
        where = "" if isinstance(template, dict) else f" {template}"
        raise _invalid(f"the job template{where} still has the repository placeholder {PLACEHOLDER} ({'; '.join(left)}) — "
                       f"{TEMPLATE_HINT}")
    return data


class K8sClient:
    """쿠버네티스 API를 호출하는 클라이언트 클래스. 보통 ``connect()``로 만든다.
    여러 Job 감시 스레드가 함께 쓴다. 세션을 넣어 주지 않으면 스레드마다 ``requests.Session``을 따로 만든다.

    requests.Session is not thread-safe; headers / verify / cert travel with every request, not with the session.
    Tests pass a fake ``session``, used as is by every thread."""

    def __init__(self, base_url: str, token: str, namespace: str, verify: str | bool = True,
                 session: Any = None, timeout_s: float = 15.0, cert: tuple[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.verify = verify
        self.cert = cert                        # kubeconfig 사용자가 클라이언트 인증서를 쓸 때의 (crt, key) 경로
        self.timeout_s = timeout_s
        self._injected_session = session       # None이면 ``session`` 속성이 스레드마다 세션을 만든다
        self._local = threading.local()
        self.source = ""                        # 접속 정보를 읽은 곳. 로그에 남긴다
        self._tmp: Any = None                   # kubeconfig 인증서를 풀어 둔 임시 폴더. 클라이언트가 살아 있는 동안 유지한다
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        # 이벤트 조회가 403이면 False가 되고 이 프로세스에서는 다시 조회하지 않는다
        self.events_readable = True
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    @classmethod
    def in_cluster(cls, namespace: str | None = None, session: Any = None) -> K8sClient:
        """파드의 서비스 계정으로 클라이언트를 만든다. 클러스터 밖이면 K8sError를 던진다.

        Token, CA and namespace from SA_DIR, address from KUBERNETES_SERVICE_HOST / _PORT. Namespace order: the
        argument, SBMIG_K8S_NAMESPACE, the service account's namespace file, ``default``."""
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        token_file, ca_file, ns_file = SA_DIR / "token", SA_DIR / "ca.crt", SA_DIR / "namespace"
        if not host or not token_file.is_file():
            raise K8sError("not inside a Kubernetes cluster (no KUBERNETES_SERVICE_HOST / service-account token) — "
                           "SBMIG_WEB_MODE=k8s-job needs the web pod to run in the cluster")
        ns = (namespace or os.environ.get(NAMESPACE_ENV, "") or
              (ns_file.read_text(encoding="utf-8").strip() if ns_file.is_file() else "")).strip() or "default"
        c = cls(f"https://{host}:{port}", token_file.read_text(encoding="utf-8").strip(), ns,
                verify=str(ca_file) if ca_file.is_file() else True, session=session)
        c.source = "in-cluster service account"
        return c

    @classmethod
    def from_kubeconfig(cls, path: str | Path | None = None, namespace: str | None = None, session: Any = None) -> K8sClient:
        """kubeconfig 파일의 ``current-context``로 클라이언트를 만든다.
        ``exec``나 ``auth-provider``로 인증하는 사용자면 K8sError를 던진다. 외부 명령은 실행하지 않는다.

        User auth, first found: token / tokenFile, client certificate (``*-data`` written to a 0600 temp dir that lives
        with the client), username + password (Basic). Namespace order: the argument, SBMIG_K8S_NAMESPACE, the
        context's, ``default``. ``insecure-skip-tls-verify`` turns verification off."""
        p = Path(path) if path else kubeconfig_path()
        if p is None or not Path(p).is_file():
            raise K8sError(f"kubeconfig not found: {p or (KUBECONFIG_ENV + ' is not set')}")
        p = Path(p)
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise K8sError(f"kubeconfig {p}: {type(e).__name__}: {e}") from e

        def named(kind: str, name: Any, inner: str) -> dict:
            for it in data.get(kind) or []:
                if isinstance(it, dict) and it.get("name") == name:
                    return it.get(inner) or {}
            return {}

        ctx_name = data.get("current-context") or next((c.get("name") for c in data.get("contexts") or [] if isinstance(c, dict)), None)
        ctx = named("contexts", ctx_name, "context")
        cluster, user = named("clusters", ctx.get("cluster"), "cluster"), named("users", ctx.get("user"), "user")
        server = str(cluster.get("server") or "").strip()
        if not server:
            raise K8sError(f"kubeconfig {p}: context {ctx_name!r} names no cluster server")
        tmp = tempfile.TemporaryDirectory(prefix="sbmig-kube-")

        def materialize(b64: Any, file: Any, name: str) -> str | None:
            # ``*-data``는 base64 PEM이다. 임시 파일로 푼다. 상대 경로 값은 kubeconfig 폴더 기준으로 본다
            if b64:
                out = Path(tmp.name) / name
                out.write_bytes(base64.b64decode(str(b64)))
                out.chmod(0o600)
                return str(out)
            if file:
                f = Path(str(file)).expanduser()
                return str(f if f.is_absolute() else (p.parent / f))
            return None

        verify: str | bool = True
        if cluster.get("insecure-skip-tls-verify"):
            verify = False
        else:
            ca = materialize(cluster.get("certificate-authority-data"), cluster.get("certificate-authority"), "ca.crt")
            if ca:
                verify = ca
        token = str(user.get("token") or "").strip()
        if not token and user.get("tokenFile"):
            tf = Path(str(user["tokenFile"])).expanduser()
            tf = tf if tf.is_absolute() else p.parent / tf
            token = tf.read_text(encoding="utf-8").strip() if tf.is_file() else ""
        crt = materialize(user.get("client-certificate-data"), user.get("client-certificate"), "client.crt")
        key = materialize(user.get("client-key-data"), user.get("client-key"), "client.key")
        cert = (crt, key) if crt and key else None
        basic = ""
        if not token and cert is None:
            if user.get("exec") or user.get("auth-provider"):
                raise K8sError(f"kubeconfig {p}: user {ctx.get('user')!r} authenticates through exec / auth-provider — sbmig runs no "
                               "external command; give the web pod a kubeconfig with a token or a client certificate")
            if user.get("username") and user.get("password") is not None:
                basic = base64.b64encode(f"{user['username']}:{user['password']}".encode()).decode("ascii")
            else:
                raise K8sError(f"kubeconfig {p}: user {ctx.get('user')!r} has no token, client certificate or password")
        ns = (namespace or os.environ.get(NAMESPACE_ENV, "") or str(ctx.get("namespace") or "")).strip() or "default"
        c = cls(server, token, ns, verify=verify, session=session, cert=cert)
        if basic:
            c._headers["Authorization"] = f"Basic {basic}"
        c._tmp = tmp
        c.source = f"kubeconfig {p} (context {ctx_name})"
        log.info("k8s: %s → %s, namespace %s", c.source, server, ns)
        return c

    @classmethod
    def connect(cls, namespace: str | None = None, session: Any = None) -> K8sClient:
        """``KUBECONFIG`` 파일이 있으면 그것으로, 없으면 파드의 서비스 계정으로 클라이언트를 만든다.
        둘 다 없으면 K8sError를 던진다."""
        if kubeconfig_path() is not None:
            return cls.from_kubeconfig(namespace=namespace, session=session)
        return cls.in_cluster(namespace=namespace, session=session)

    @property
    def session(self) -> Any:
        """이 스레드가 쓸 HTTP 세션을 반환한다. 넣어 준 세션이 없으면 스레드마다 처음 쓸 때 만든다."""
        if self._injected_session is not None:
            return self._injected_session
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = requests.Session()
        return s

    @session.setter
    def session(self, value: Any) -> None:
        self._injected_session = value

    # ---- HTTP 호출
    def _req(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
        if self.cert:
            kw.setdefault("cert", self.cert)
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

    @staticmethod
    def _body(r: Any, what: str) -> dict:
        # 2xx인데 본문이 JSON 객체가 아니면 API 문제로 보고 K8sError를 던진다. 앞단 프록시의 HTML 응답 같은 경우다
        try:
            body = r.json()
        except Exception as e:  # requests의 JSONDecodeError와 가짜 구현의 ValueError를 함께 잡는다
            raise K8sError(f"{what}: HTTP {getattr(r, 'status_code', '?')}: the answer is not JSON ({type(e).__name__})") from e
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise K8sError(f"{what}: HTTP {getattr(r, 'status_code', '?')}: the answer is not a JSON object ({type(body).__name__})")
        return body

    # ---- Job 생성, 조회, 삭제와 권한 확인
    def create_job(self, manifest: dict) -> str:
        """Job을 만들고 이름을 반환한다. 같은 이름의 Job이 있으면 409로 K8sError를 던진다.
        2xx인데 본문을 읽을 수 없으면 만들어진 것으로 본다. 경고를 남기고 매니페스트의 이름을 반환한다."""
        r = self._req("POST", self._jobs(), json=manifest)
        try:
            name = ((self._body(r, "POST jobs").get("metadata") or {}).get("name")) or manifest["metadata"]["name"]
        except (K8sError, AttributeError) as e:
            log.warning("k8s: job %s created but the answer is unreadable (%s) — using the manifest's name",
                        manifest["metadata"]["name"], e)
            name = manifest["metadata"]["name"]
        log.info("k8s: job %s created in %s", name, self.namespace)
        return str(name)

    def job_state(self, name: str) -> JobState:
        """Job과 파드 상태를 조회해 JobState 하나로 합친다.
        응답이 JSON이 아니거나 형식이 이상하면 K8sError를 던진다.

        Phase: Missing when the Job answers 404; Succeeded / Failed from ``status.succeeded`` / ``failed``; else the
        newest pod's phase, Creating without a pod. ``active_pods`` counts pods still running / terminating even when
        the Job is terminal. Only K8sError escapes, never another exception."""
        r = self._req("GET", f"{self._jobs()}/{name}", ok=(200, 404))
        if r.status_code == 404:
            return JobState("Missing", message="job not found (deleted?)", labels={})
        body = self._body(r, f"GET job {name}")
        pods_r = self._req("GET", f"/api/v1/namespaces/{self.namespace}/pods", params={"labelSelector": f"job-name={name}"})
        pods_body = self._body(pods_r, f"GET pods of job {name}")
        try:
            return self._job_state_of(body, pods_body)
        except (AttributeError, KeyError, TypeError, ValueError, IndexError) as e:
            raise K8sError(f"GET job {name}: unexpected answer ({type(e).__name__}: {e})") from e

    @staticmethod
    def _job_state_of(body: dict, pods_body: dict) -> JobState:
        # 형식이 이상한 응답에서 나는 예외는 job_state가 K8sError로 바꾼다
        status = body.get("status") or {}
        labels = {str(k): str(v) for k, v in (((body.get("metadata") or {}).get("labels")) or {}).items()}
        pods = list(pods_body.get("items") or [])
        # 종료 중인 파드도 phase는 Running이다. 끝나지 않은 파드를 모두 세고, Job의 active, terminating 중 큰 값을 쓴다
        live = sum(1 for p in pods if ((p.get("status") or {}).get("phase") or "Pending") not in ("Succeeded", "Failed"))
        active_pods = max(live, int(status.get("active") or 0), int(status.get("terminating") or 0))
        pods.sort(key=lambda p: ((p.get("metadata") or {}).get("creationTimestamp") or ""), reverse=True)
        pod = pods[0] if pods else None
        pod_name = ((pod or {}).get("metadata") or {}).get("name")
        pstatus = (pod or {}).get("status") or {}
        exit_code, message = None, ""
        waiting_reason = waiting_message = ""
        for cs in pstatus.get("containerStatuses") or []:
            state = cs.get("state") or {}
            if "terminated" in state:
                exit_code = state["terminated"].get("exitCode")
                message = state["terminated"].get("reason") or message
            elif "waiting" in state:
                w = state["waiting"] or {}
                message = " ".join(x for x in (w.get("reason"), w.get("message")) if x)[:300]
                waiting_reason, waiting_message = str(w.get("reason") or ""), str(w.get("message") or "")[:300]
        sched = next((c for c in pstatus.get("conditions") or []
                      if isinstance(c, dict) and c.get("type") == "PodScheduled" and str(c.get("status")) == "False"), {})
        sched_reason, sched_message = str(sched.get("reason") or ""), str(sched.get("message") or "")[:300]
        if (status.get("succeeded") or 0) > 0:
            return JobState("Succeeded", pod_name, exit_code if exit_code is not None else 0, message, labels, active_pods)
        if (status.get("failed") or 0) > 0:
            cond = next((c for c in status.get("conditions") or [] if c.get("type") == "Failed"), {})
            why = " ".join(x for x in (cond.get("reason"), cond.get("message")) if x)[:300]
            return JobState("Failed", pod_name, exit_code, why or message or "job failed", labels, active_pods)
        if pod is None:
            return JobState("Creating", None, None, "no pod yet", labels)
        phase = pstatus.get("phase") or "Pending"
        if phase in ("Succeeded", "Failed"):          # 파드는 끝났지만 Job 상태가 아직 바뀌지 않은 경우
            return JobState(phase, pod_name, exit_code, message, labels, live)
        return JobState(phase, pod_name, None, message, labels, waiting_reason=waiting_reason,
                        waiting_message=waiting_message, sched_reason=sched_reason, sched_message=sched_message)

    def pod_events(self, pod_name: str) -> list[dict[str, str]]:
        """파드 이벤트를 최신순으로 반환한다. 항목마다 ``type``, ``reason``, ``message``, ``time``이 있다.
        403이거나 응답 형식이 이상하면 빈 목록을 반환한다. 이 프로세스에서는 다시 조회하지 않는다.
        예외를 던지지 않는다.

        Events need their own RBAC rule, hence the 403 case. A transport error or another HTTP status gives [] this
        time only."""
        if not self.events_readable or not pod_name:
            return []
        try:
            r = self._req("GET", f"/api/v1/namespaces/{self.namespace}/events", ok=(200, 403),
                          params={"fieldSelector": f"involvedObject.kind=Pod,involvedObject.name={pod_name}"})
        except K8sError as e:
            log.debug("k8s: events of pod %s not read this time (%s)", pod_name, e)
            return []
        if r.status_code == 403:
            self.events_readable = False
            log.info("k8s: pod events are not readable with this account (HTTP 403) — not asked again")
            return []
        try:
            items = self._body(r, "GET events").get("items") or []
            if not isinstance(items, list):
                raise TypeError("items is not a list")
            out = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                meta = it.get("metadata") or {}
                when = (it.get("lastTimestamp") or it.get("eventTime") or it.get("firstTimestamp")
                        or meta.get("creationTimestamp") or "")
                out.append({"type": str(it.get("type") or ""), "reason": str(it.get("reason") or ""),
                            "message": str(it.get("message") or ""), "time": str(when)})
        except (K8sError, AttributeError, TypeError, ValueError) as e:
            self.events_readable = False
            log.info("k8s: the events answer is not usable (%s) — pod events are not asked again", e)
            return []
        out.sort(key=lambda e: e["time"], reverse=True)
        return out

    def delete_job(self, name: str) -> None:
        """Job과 그 파드를 지운다. Job이 이미 없으면 그대로 넘어간다.

        ``propagationPolicy: Foreground``; 404 counts as deleted."""
        self._req("DELETE", f"{self._jobs()}/{name}", ok=(200, 202, 404),
                  json={"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground"})
        log.info("k8s: job %s deleted", name)

    def can_i(self, verb: str, resource: str, group: str = "") -> bool:
        """이 클라이언트의 사용자에게 namespace 안에서 ``resource``를 ``verb``할 권한이 있는지 확인한다.
        API가 거부하거나 연결되지 않으면 K8sError를 던진다.

        One SelfSubjectAccessReview; the startup check asks every entry of REQUIRED_PERMISSIONS."""
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": {"namespace": self.namespace, "verb": verb, "resource": resource, "group": group}}}
        r = self._req("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", json=body)
        st = self._body(r, "POST selfsubjectaccessreviews").get("status") or {}
        if not isinstance(st, dict):
            raise K8sError(f"POST selfsubjectaccessreviews: unexpected answer (status is {type(st).__name__})")
        return bool(st.get("allowed"))
