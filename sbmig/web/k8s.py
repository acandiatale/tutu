"""쿠버네티스 Job 디스패치의 얇은 클라이언트 (2026-11-05, 웹 파드의 ``SBMIG_WEB_MODE=k8s-job``).

웹 파드는 업로드마다 Job 하나를 만들고(``81-web-job.template.yaml`` 의 틀에 실행별 env 를 채워), 상태를 지켜보고(Pending →
Running → Succeeded / Failed), 멈출 때 지운다. 그 셋이 전부라서 ``kubernetes`` 패키지 대신 ``requests`` 로 클러스터 안 API 를
직접 부른다 — 폐쇄망에 새 wheel 을 들일 일이 없고, 서비스 계정의 토큰·CA(``/var/run/secrets/kubernetes.io/serviceaccount``)로
인증한다(권한은 deploy/k8s/45-web-rbac.yaml). 로그 스트림·zip 전달·결과 회수는 공유 PVC 가 대신하므로 여기 없다.

A thin in-cluster client for the three calls the Job dispatch needs — create a Job from the template, read its state
(Job status + its pod's phase / container exit code), delete it — over `requests` with the service-account token and CA.
Logs, the zip and the results travel over the shared PVC, not over this client."""
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

MODE_ENV = "SBMIG_WEB_MODE"                 # in-process (default) | k8s-job
NAMESPACE_ENV = "SBMIG_K8S_NAMESPACE"
TEMPLATE_ENV = "SBMIG_JOB_TEMPLATE"
POLL_ENV = "SBMIG_JOB_POLL_S"
STOP_GRACE_ENV = "SBMIG_JOB_STOP_GRACE_S"
STATUS_GRACE_ENV = "SBMIG_JOB_STATUS_GRACE_S"   # Job 이 끝난 뒤 Worker 의 job-status.json 이 보일 때까지 기다리는 초(NFS 속성 캐시 — 기본 90)
EXIT_GRACE_ENV = "SBMIG_JOB_EXIT_GRACE_S"       # 결과 파일을 받은 뒤 파드가 스스로 끝나기를 기다리는 초(넘기면 그 Job 을 지운다 — 기본 120)
KUBECONFIG_ENV = "KUBECONFIG"              # 인프라팀 모델(2026-11-09): Agent 파드에 Secret 으로 마운트한 kubeconfig(/etc/kube/config)
DEFAULT_TEMPLATE = "deploy/k8s/81-web-job.template.yaml"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MODES = ("in-process", "k8s-job", "local-job")    # local-job: the Job-mode flow with a local subprocess as the "pod" (development)
TERMINAL_PHASES = ("Succeeded", "Failed", "Missing")
TEMPLATE_INVALID = "MIGRATION_TEMPLATE_INVALID"   # 시작 검증이 거부할 때 메시지의 첫 말(배포팀이 로그에서 찾는 값)
# Worker 에 있으면 안 되는 env: 원문 LLM 기록(공유 볼륨에 소스가 남는다)과 웹 토큰(빌드 파드가 웹 API 를 부를 일이 없다)
FORBIDDEN_WORKER_ENV = ("LLM_RECORD_DIR", "SBMIG_WEB_TOKEN", "WEB_API_TOKEN")
# 시작 때 확인하는 권한(deploy/k8s/45-web-rbac.yaml 과 같은 목록): (verb, resource, group)
REQUIRED_PERMISSIONS = tuple([(v, "jobs", "batch") for v in ("create", "get", "list", "watch", "delete")]
                             + [(v, "pods", "") for v in ("get", "list", "watch")])


def cluster_host_present() -> bool:
    """``KUBERNETES_SERVICE_HOST`` 가 있나 — 파드 안이라는 뜻(토큰은 별개)."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def in_cluster_available() -> bool:
    """이 프로세스가 클러스터 안 파드인가(``KUBERNETES_SERVICE_HOST`` + 서비스 계정 토큰). 개발 PC 에서는 False —
    ``kubeconfig_path()`` 도 없으면 ``k8s-job`` 모드가 자동으로 ``local-job`` 으로 내려가는 기준.

    Are we a pod inside a cluster (`KUBERNETES_SERVICE_HOST` + the service-account token)? False on a developer PC."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and (SA_DIR / "token").is_file()


def kubeconfig_path() -> Path | None:
    """``KUBECONFIG`` 가 가리키는 파일이 있으면 그 경로(2026-11-09, 인프라팀 모델: Agent 파드에 kubeconfig Secret 을 ``/etc/kube/config`` 로
    마운트하고 ``KUBECONFIG`` 를 준다). 명시된 것만 본다 — ``~/.kube/config`` 는 개발 PC 의 다른 클러스터일 수 있어 저절로 쓰지 않는다.
    여러 경로(os.pathsep)면 첫 번째 있는 파일.

    The file `KUBECONFIG` points at, when it exists; nothing implicit (`~/.kube/config` is never picked up on its own)."""
    raw = (os.environ.get(KUBECONFIG_ENV) or "").strip()
    for part in raw.split(os.pathsep) if raw else []:
        p = Path(part.strip()).expanduser()
        if part.strip() and p.is_file():
            return p
    return None


def cluster_access_available() -> bool:
    """쿠버네티스 API 를 부를 수단이 있나 — 파드 안의 서비스 계정 토큰이거나 ``KUBECONFIG`` 파일. ``K8sClient.connect()`` 가 그 순서의
    반대로(kubeconfig 가 명시됐으면 그것부터) 고른다.

    A service-account token inside a pod, or an explicit kubeconfig: either lets `k8s-job` create real Jobs."""
    return in_cluster_available() or kubeconfig_path() is not None


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
    # Job 의 metadata.labels: 이어받거나 지우기 전에 소유(sbmig-owner / sbmig-run-id)를 확인한다. **None = 이 클라이언트는 라벨을 알려 주지
    # 못한다**(로컬 서브프로세스, 테스트용 가짜) — 그때만 이름을 믿는다. dict 는 API 가 답한 라벨 그대로이고, 비어 있으면(라벨 없는 Job)
    # 우리 것이 아니다(리뷰 2026-11-10: 빈 dict 를 "모름" 으로 읽어 라벨 없는 Job 을 자기 것으로 다뤘다)
    labels: dict[str, str] | None = None

    @property
    def finished(self) -> bool:
        return self.phase in TERMINAL_PHASES


def job_name(run_id: str) -> str:
    """실행 id → Job 이름(``sbmig-<run id>``, RFC 1123: 소문자·숫자·``-``, 63자 이하).

    `sbmig-<run id>` made a valid RFC 1123 name (lower-case, `-`, at most 63 characters)."""
    base = re.sub(r"[^a-z0-9-]+", "-", f"sbmig-{run_id}".lower()).strip("-")
    return (base[:63].rstrip("-")) or "sbmig-run"


def render_job(template: str | Path | dict, run_id: str, env: dict[str, str], namespace: str | None = None,
               owner: str | None = None) -> dict:
    """틀(yaml 경로 또는 dict)에 실행 하나를 채운다: ``metadata.name`` = job_name, ``generateName`` 제거, 라벨
    ``sbmig-run-id``(와 ``owner`` 가 주어지면 ``sbmig-owner`` — Job 과 파드 틀 둘 다), namespace(주어지면), 첫 컨테이너의 env 에 ``env`` 를
    같은 이름은 바꾸고 없는 것은 덧붙인다. 틀은 손대지 않는다.

    Fill the template for one run: name, labels (run id, and the per-request owner token when given — on the Job and its
    pod template), namespace, and the first container's env (same names replaced)."""
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
    # 라벨 값 규칙(영숫자·``-_.``, 63자 이하) — run id 와 owner 토큰은 이미 맞지만 틀린 값이 API 에서 422 로 죽지 않게
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value))[:63]


def _invalid(reason: str) -> K8sError:
    return K8sError(f"{TEMPLATE_INVALID}: {reason}")


def validate_worker_template(template: str | Path | dict, out_dir: Path | None) -> dict:
    """Worker Job 틀을 시작 때 검사한다(배포팀 명세, v2.1z-ac — 회사 Main 의 worker_template 과 같은 규칙). 어기면
    ``K8sError("MIGRATION_TEMPLATE_INVALID: <이유>")``: 읽을 수 없음 / ``batch/v1`` ``Job`` 아님; 컨테이너가 정확히 하나가 아니거나
    initContainers·ephemeralContainers 있음; ``backoffLimit``(기본 0) ≠ 0, ``parallelism`` / ``completions``(기본 1) ≠ 1,
    ``restartPolicy``(기본 Never) ≠ Never; 컨테이너에 name / image 없음; ``envFrom`` 있음; ``out_dir`` 를 품는 마운트가 쓰기 가능한 PVC
    전체(readOnly·subPath 없음) 하나가 아니거나 ``out_dir`` 아래 마운트가 있음; ``LLM_RECORD_DIR`` / ``SBMIG_WEB_TOKEN`` / ``WEB_API_TOKEN``
    env, 또는 이름에 TOKEN / PASSWORD / SECRET / API_KEY 가 든 env(설정 덮어쓰기 ``SBMIG_<섹션>__<키>`` 제외)에 literal ``value``. ``out_dir`` 가 None 이면(테스트가 주입한 클라이언트
    — 출력 폴더가 파드 경로가 아니다) 마운트 규칙만 건너뛴다. 통과하면 읽은 dict 를 돌려준다(Main 은 그것으로 Job 을 만든다).

    Validate the Worker Job template at startup; raises `K8sError("MIGRATION_TEMPLATE_INVALID: …")`, returns the parsed dict.
    `out_dir=None` skips the mount rules only (an injected test client: the output dir is not a pod path)."""
    if isinstance(template, dict):
        data: Any = template
    else:
        try:
            data = yaml.safe_load(Path(template).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise _invalid(f"job template {template} is not readable ({type(e).__name__}: {e}) — mount the ConfigMap "
                           f"sbmig-job-template at /templates (deploy/README.md §3) or point {TEMPLATE_ENV} at the filled template") from e
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
    return data


class K8sClient:
    """클러스터 안 API 클라이언트. ``in_cluster()`` 가 서비스 계정 파일과 ``KUBERNETES_SERVICE_HOST`` 로 만든다; 테스트는
    ``session`` 에 가짜를 넣는다. v2.1z-ae: 여러 Job 을 동시에 지켜보는 스레드들이 한 클라이언트를 함께 쓴다 — ``requests.Session``
    은 스레드 사이에서 안전하다고 약속하지 않으므로, 넣어 준 세션이 없으면 **스레드마다** 세션을 하나씩 만든다(헤더·verify·cert 는
    요청마다 넘기므로 동작은 같다). 넣어 준 세션은 그대로 쓴다.

    In-cluster API client (`in_cluster()`); tests pass a fake `session`, used as is. Without one each thread gets its own
    `requests.Session` (several watcher threads share the client; headers / verify / cert travel with every request)."""

    def __init__(self, base_url: str, token: str, namespace: str, verify: str | bool = True,
                 session: Any = None, timeout_s: float = 15.0, cert: tuple[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.verify = verify
        self.cert = cert                        # (client.crt, client.key) — kubeconfig 의 클라이언트 인증서 사용자일 때
        self.timeout_s = timeout_s
        self._injected_session = session       # None → one requests.Session per thread (the `session` property)
        self._local = threading.local()
        self.source = ""                        # 사람이 읽을 출처: "in-cluster service account" / "kubeconfig <경로> (context <이름>)"
        self._tmp: Any = None                   # kubeconfig 의 base64 인증서를 풀어 둔 임시 폴더 — 클라이언트와 같이 산다
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

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
        c = cls(f"https://{host}:{port}", token_file.read_text(encoding="utf-8").strip(), ns,
                verify=str(ca_file) if ca_file.is_file() else True, session=session)
        c.source = "in-cluster service account"
        return c

    @classmethod
    def from_kubeconfig(cls, path: str | Path | None = None, namespace: str | None = None, session: Any = None) -> K8sClient:
        """kubeconfig 파일로 만든다(2026-11-09, 인프라팀 모델: Secret ``agent-kubeconfig`` → ``/etc/kube/config``, env ``KUBECONFIG``).
        ``current-context`` 의 cluster(server, certificate-authority[-data], insecure-skip-tls-verify)와 user(token / tokenFile,
        client-certificate[-data] + client-key[-data], username + password)를 읽는다. base64 ``*-data`` 는 임시 폴더에 0600 파일로 풀어
        requests 에 경로로 준다; 상대 경로는 kubeconfig 파일이 있는 폴더 기준(kubectl 과 같다). ``exec`` / ``auth-provider`` 사용자는 못
        쓴다(외부 명령을 부르지 않는다 — 토큰이나 인증서가 든 kubeconfig 를 달라고 K8sError). namespace: 인자 → ``SBMIG_K8S_NAMESPACE`` →
        context 의 namespace → default.

        Build the client from a kubeconfig (current-context's cluster + user; token, client certificate or basic auth; `exec` refused)."""
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
            # ``*-data`` 는 base64 PEM → 임시 파일(0600); ``*`` 는 경로(상대면 kubeconfig 폴더 기준)
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
        """``KUBECONFIG`` 파일이 있으면 그것으로(명시된 쪽이 이긴다), 없으면 파드 안 서비스 계정으로. 둘 다 없으면 K8sError
        (``in_cluster`` 의 메시지).

        Explicit kubeconfig first, else the in-cluster service account."""
        if kubeconfig_path() is not None:
            return cls.from_kubeconfig(namespace=namespace, session=session)
        return cls.in_cluster(namespace=namespace, session=session)

    @property
    def session(self) -> Any:
        """이 스레드가 쓸 세션: 넣어 준 것이 있으면 그것, 없으면 이 스레드 전용 ``requests.Session`` (처음 부를 때 만든다).

        The injected session, else this thread's own `requests.Session` (created on first use)."""
        if self._injected_session is not None:
            return self._injected_session
        s = getattr(self._local, "session", None)
        if s is None:
            s = self._local.session = requests.Session()
        return s

    @session.setter
    def session(self, value: Any) -> None:
        self._injected_session = value

    # ---- http
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
            return JobState("Missing", message="job not found (deleted?)", labels={})
        body = r.json() or {}
        status = body.get("status") or {}
        labels = {str(k): str(v) for k, v in (((body.get("metadata") or {}).get("labels")) or {}).items()}
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
            return JobState("Succeeded", pod_name, exit_code if exit_code is not None else 0, message, labels)
        if (status.get("failed") or 0) > 0:
            cond = next((c for c in status.get("conditions") or [] if c.get("type") == "Failed"), {})
            why = " ".join(x for x in (cond.get("reason"), cond.get("message")) if x)[:300]
            return JobState("Failed", pod_name, exit_code, why or message or "job failed", labels)
        if pod is None:
            return JobState("Creating", None, None, "no pod yet", labels)
        phase = pstatus.get("phase") or "Pending"
        if phase in ("Succeeded", "Failed"):          # the pod ended, the Job status lags a moment behind
            return JobState(phase, pod_name, exit_code, message, labels)
        return JobState(phase, pod_name, None, message, labels)

    def delete_job(self, name: str) -> None:
        """Job 과 그 파드를 지운다(propagationPolicy Foreground). 이미 없으면 조용히."""
        self._req("DELETE", f"{self._jobs()}/{name}", ok=(200, 202, 404),
                  json={"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground"})
        log.info("k8s: job %s deleted", name)

    def can_i(self, verb: str, resource: str, group: str = "") -> bool:
        """이 클라이언트의 사용자가 namespace 안에서 ``verb`` ``resource`` 를 할 수 있나 — SelfSubjectAccessReview 한 번(시작 때 권한
        자가 점검; 누구나 부를 수 있는 API 라 따로 권한이 필요 없다). API 가 거부·불통이면 K8sError.

        One SelfSubjectAccessReview: may this client's user `verb` `resource` (API `group`) in its namespace?"""
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": {"namespace": self.namespace, "verb": verb, "resource": resource, "group": group}}}
        r = self._req("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", json=body)
        return bool(((r.json() or {}).get("status") or {}).get("allowed"))
