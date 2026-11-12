"""웹 파드가 쿠버네티스 Job을 만들고 관리할 때 쓰는 가벼운 API 클라이언트 모듈.
웹 파드의 ``SBMIG_WEB_MODE=k8s-job``에서 쓴다.
업로드마다 ``81-web-job.template.yaml`` 템플릿에 실행별 환경 변수를
채워 Job을 만들고, 상태를 확인하고, 중지할 때 지운다.
- 필요한 호출이 몇 가지뿐이므로 ``kubernetes`` 패키지 없이 ``requests``로 API를 직접 호출한다.
  폐쇄망에 새 패키지를 들이지 않기 위해서다
- 인증은 서비스 계정 토큰과 CA 또는 ``KUBECONFIG``로 한다. 필요한 권한은 deploy/k8s/45-web-rbac.yaml에 있다
- 로그, zip 전달, 결과 회수는 공유 PVC로 하므로 이 모듈에는 없다

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
STATUS_GRACE_ENV = "SBMIG_JOB_STATUS_GRACE_S"   # Job이 끝난 뒤 job-status.json이 보일 때까지 기다리는 시간(초, 기본 90)
EXIT_GRACE_ENV = "SBMIG_JOB_EXIT_GRACE_S"       # 결과 파일을 받은 뒤 파드 종료를 기다리는 초(기본 120), 넘기면 Job을 지운다
KUBECONFIG_ENV = "KUBECONFIG"              # Agent 파드에 Secret으로 마운트한 kubeconfig(/etc/kube/config)
DEFAULT_TEMPLATE = "deploy/k8s/81-web-job.template.yaml"
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MODES = ("in-process", "k8s-job", "local-job")    # local-job: the Job-mode flow with a local subprocess as the "pod" (development)
TERMINAL_PHASES = ("Succeeded", "Failed", "Missing")
TEMPLATE_INVALID = "MIGRATION_TEMPLATE_INVALID"   # 시작 검증에서 거부할 때 메시지 앞에 붙는 값으로 배포팀이 로그에서 찾는다
# Worker에 있으면 안 되는 환경 변수다.
# LLM 원문 기록은 공유 볼륨에 소스를 남기고, 빌드 파드는 웹 API를 호출하지 않으므로 웹 토큰이 필요 없다
FORBIDDEN_WORKER_ENV = ("LLM_RECORD_DIR", "SBMIG_WEB_TOKEN", "WEB_API_TOKEN")
# 시작할 때 확인하는 권한 (verb, resource, group) 목록이며 deploy/k8s/45-web-rbac.yaml과 같아야 한다
REQUIRED_PERMISSIONS = tuple([(v, "jobs", "batch") for v in ("create", "get", "list", "watch", "delete")]
                             + [(v, "pods", "") for v in ("get", "list", "watch")])


def cluster_host_present() -> bool:
    """환경 변수 ``KUBERNETES_SERVICE_HOST``가 있는지 확인한다.
    있으면 파드 안에서 실행 중이라는 뜻이며 토큰이 있는지와는 상관없다."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def in_cluster_available() -> bool:
    """이 프로세스가 클러스터 안 파드에서 실행 중인지 확인한다(``KUBERNETES_SERVICE_HOST``와 서비스 계정
    토큰). 개발 PC에서는 False다.
    이 값이 False이고 ``kubeconfig_path()``도 없으면 ``k8s-job`` 모드가 ``local-job``으로 바뀐다.

    Are we a pod inside a cluster (`KUBERNETES_SERVICE_HOST` + the service-account token)? False on a developer PC."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and (SA_DIR / "token").is_file()


def kubeconfig_path() -> Path | None:
    """``KUBECONFIG``가 가리키는 파일이 있으면 그 경로를 반환한다.
    여러 경로(os.pathsep)가 있으면 처음 존재하는 파일을 반환한다.
    ``~/.kube/config``는 개발 PC의 다른 클러스터일 수 있으므로 자동으로 쓰지 않는다.

    The file `KUBECONFIG` points at, when it exists; nothing implicit (`~/.kube/config` is never picked up on its own)."""
    raw = (os.environ.get(KUBECONFIG_ENV) or "").strip()
    for part in raw.split(os.pathsep) if raw else []:
        p = Path(part.strip()).expanduser()
        if part.strip() and p.is_file():
            return p
    return None


def cluster_access_available() -> bool:
    """쿠버네티스 API를 호출할 수단이 있는지 확인한다.
    파드 안의 서비스 계정 토큰이나 ``KUBECONFIG`` 파일 중 하나가 있으면 된다.

    A service-account token inside a pod, or an explicit kubeconfig: either lets `k8s-job` create real Jobs."""
    return in_cluster_available() or kubeconfig_path() is not None


class K8sError(RuntimeError):
    """클러스터 API가 요청을 거부했거나 연결되지 않을 때 던지는 예외 클래스.
    상태 코드와 응답 본문 앞부분을 담는다."""


@dataclass
class JobState:
    """Job 하나의 현재 상태를 담는 데이터 클래스.
    ``phase``(Creating | Pending | Running | Succeeded | Failed | Missing), 파드 이름, 컨테이너 종료 코드,
    사람이 읽을 메시지(ImagePullBackOff 같은 대기 이유, Job 조건)를 담는다."""
    phase: str
    pod: str | None = None
    exit_code: int | None = None
    message: str = ""
    # 이어받거나 지우기 전에 소유 라벨(sbmig-owner / sbmig-run-id)을 확인한다. None은 라벨을 알 수 없는
    # 클라이언트(로컬 서브프로세스, 테스트용 가짜 구현)이고, 그때만 이름으로 판단한다
    # 빈 dict는 라벨이 없는 Job이므로 우리 것이 아니다
    labels: dict[str, str] | None = None
    # Job이 Succeeded / Failed여도 아직 실행 중이거나 종료 중인 파드 수다. activeDeadlineSeconds로 Failed가
    # 된 Job도 종료 유예 동안 파드의 Maven이 계속 파일을 쓸 수 있으므로 0이 되어야 끝난 것으로 본다
    active_pods: int = 0

    @property
    def finished(self) -> bool:
        """Job이 완전히 끝났는지 확인한다. 종료 상태(Succeeded / Failed / Missing)이고 실행 중이거나 종료
        중인 파드가 없어야 하며, 슬롯을 다른 실행에 넘겨도 되는지 판단할 때 쓴다.

        Ended: a terminal phase AND no pod of it still running or terminating (may its slot be handed on)."""
        return self.phase in TERMINAL_PHASES and not self.active_pods


def job_name(run_id: str) -> str:
    """실행 id로 Job 이름(``sbmig-<run id>``)을 만든다.
    RFC 1123에 맞게 소문자, 숫자, ``-``만 쓰고 63자 이하로 자른다.

    `sbmig-<run id>` made a valid RFC 1123 name (lower-case, `-`, at most 63 characters)."""
    base = re.sub(r"[^a-z0-9-]+", "-", f"sbmig-{run_id}".lower()).strip("-")
    return (base[:63].rstrip("-")) or "sbmig-run"


def render_job(template: str | Path | dict, run_id: str, env: dict[str, str], namespace: str | None = None,
               owner: str | None = None) -> dict:
    """Job 템플릿(yaml 경로 또는 dict)에 실행 하나의 값을 채운 매니페스트를 만든다.
    원본 템플릿은 바꾸지 않는다.
    - ``metadata.name``을 job_name으로 정하고 ``generateName``은 제거한다
    - 라벨 ``sbmig-run-id``와, ``owner``가 있으면 ``sbmig-owner``를 Job과 파드 템플릿에 붙인다
    - namespace를 넣고, 첫 컨테이너의 env에 ``env``를 합친다. 같은 이름은 값을 바꾸고 없는 이름은 추가한다

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
    # 라벨 값 규칙(영숫자, ``-_.``, 63자 이하)에 맞춘다.
    # run id와 owner 토큰은 이미 맞지만 틀린 값 때문에 API가 422로 실패하지 않게 한다
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value))[:63]


def _invalid(reason: str) -> K8sError:
    return K8sError(f"{TEMPLATE_INVALID}: {reason}")


def validate_worker_template(template: str | Path | dict, out_dir: Path | None) -> dict:
    """Worker Job 템플릿이 규칙에 맞는지 시작할 때 검사한다.
    위반하면 ``K8sError("MIGRATION_TEMPLATE_INVALID: <이유>")``를 던지고, 통과하면 읽은 dict를 반환한다.
    Job 매니페스트인지, 컨테이너가 하나인지, ``backoffLimit`` 0, ``restartPolicy`` Never, ``envFrom`` 없음,
    ``out_dir``를 포함하는 쓰기 가능한 PVC 마운트 하나, 비밀 이름 env에 직접 쓴 값이 없는지 등을 확인한다.
    ``out_dir``가 None이면 테스트 클라이언트로 보고 마운트 규칙만 건너뛴다.

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
    """쿠버네티스 클러스터 API를 호출하는 클라이언트 클래스. ``connect()``, ``in_cluster()``,
    ``from_kubeconfig()``로 만들고, 테스트는 ``session``에 가짜 구현을 넣는다.
    여러 Job을 감시하는 스레드가 함께 쓰는데 ``requests.Session``은 스레드 안전하지 않으므로, 세션을 넣지
    않았으면 스레드마다 하나씩 만든다.

    In-cluster API client (`in_cluster()`); tests pass a fake `session`, used as is. Without one each thread gets its own
    `requests.Session` (several watcher threads share the client; headers / verify / cert travel with every request)."""

    def __init__(self, base_url: str, token: str, namespace: str, verify: str | bool = True,
                 session: Any = None, timeout_s: float = 15.0, cert: tuple[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.namespace = namespace
        self.verify = verify
        self.cert = cert                        # kubeconfig 사용자가 클라이언트 인증서를 쓸 때의 (client.crt, client.key)
        self.timeout_s = timeout_s
        self._injected_session = session       # None → one requests.Session per thread (the `session` property)
        self._local = threading.local()
        self.source = ""                        # 접속 정보를 어디서 읽었는지 사람이 읽을 수 있게 남긴다
        self._tmp: Any = None                   # kubeconfig의 base64 인증서를 풀어 둔 임시 폴더이며 클라이언트와 수명이 같다
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    @classmethod
    def in_cluster(cls, namespace: str | None = None, session: Any = None) -> K8sClient:
        """파드 안의 기본 접속 정보로 클라이언트를 만든다. 토큰, CA, namespace는 서비스 계정 폴더에서,
        주소는 ``KUBERNETES_SERVICE_HOST`` / ``_PORT``에서 읽는다.
        클러스터 밖이면 K8sError를 던진다."""
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
        """kubeconfig 파일로 클라이언트를 만든다.
        ``current-context``의 cluster와 user(token, 클라이언트 인증서, username + password)를 읽는다.
        외부 명령을 실행하지 않으므로 ``exec`` / ``auth-provider`` 사용자는 K8sError를 던진다.
        namespace는 인자, ``SBMIG_K8S_NAMESPACE``, context의 namespace, default 순으로 정한다.

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
            # ``*-data``는 base64 PEM이므로 임시 파일(0600)로 풀고, ``*``는 경로이므로 상대 경로면
            # kubeconfig 폴더를 기준으로 본다
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
        """접속할 수 있는 방법으로 클라이언트를 만든다. ``KUBECONFIG`` 파일이 있으면 그것을, 없으면 파드 안
        서비스 계정을 쓰고, 둘 다 없으면 K8sError를 던진다.

        Explicit kubeconfig first, else the in-cluster service account."""
        if kubeconfig_path() is not None:
            return cls.from_kubeconfig(namespace=namespace, session=session)
        return cls.in_cluster(namespace=namespace, session=session)

    @property
    def session(self) -> Any:
        """이 스레드가 쓸 HTTP 세션을 반환한다. 넣어 준 세션이 있으면 그것을, 없으면 처음 호출할 때 만든
        스레드 전용 ``requests.Session``을 반환한다.

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

    @staticmethod
    def _body(r: Any, what: str) -> dict:
        # 2xx인데 본문이 JSON 객체가 아니면(앞단 프록시의 HTML 응답 등) API 문제로 보고 K8sError를 던진다
        try:
            body = r.json()
        except Exception as e:  # requests' JSONDecodeError, a fake's ValueError …
            raise K8sError(f"{what}: HTTP {getattr(r, 'status_code', '?')}: the answer is not JSON ({type(e).__name__})") from e
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise K8sError(f"{what}: HTTP {getattr(r, 'status_code', '?')}: the answer is not a JSON object ({type(body).__name__})")
        return body

    # ---- the three calls
    def create_job(self, manifest: dict) -> str:
        """Job을 만들고 그 이름을 반환한다. 같은 이름의 Job이 이미 있으면 상태 409로 K8sError를 던진다.
        2xx인데 본문을 읽을 수 없으면 생성은 성공한 것이므로 경고를 남기고 템플릿의 이름을 반환한다.

        Create the Job and return its name; a 2xx whose body cannot be read still created it (the manifest's name, a warning)."""
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
        """Job과 파드의 상태를 조회해 하나의 JobState로 합친다. Job이 없으면 Missing, status.succeeded /
        failed가 있으면 Succeeded / Failed, 아니면 파드의 phase이고 파드가 없으면 Creating이다.
        종료 상태여도 실행 중이거나 종료 중인 파드 수를 ``active_pods``에 담는다.
        응답 형식이 이상하면 K8sError를 던진다.

        One state of the Job and its pod; `active_pods` counts pods still running / terminating even when the Job is
        terminal. A body that is not JSON or has an odd shape raises K8sError (an API problem, never another exception)."""
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
        # job_state가 받은 응답을 해석한다. 형식이 이상하면 호출한 쪽이 K8sError로 바꾼다
        status = body.get("status") or {}
        labels = {str(k): str(v) for k, v in (((body.get("metadata") or {}).get("labels")) or {}).items()}
        pods = list(pods_body.get("items") or [])
        # 종료 중인 파드는 deletionTimestamp가 있어도 phase가 Running이므로 Succeeded / Failed가 아닌 파드를 모두 센다
        # Job의 status.active / status.terminating도 함께 보고 더 큰 값을 쓴다
        live = sum(1 for p in pods if ((p.get("status") or {}).get("phase") or "Pending") not in ("Succeeded", "Failed"))
        active_pods = max(live, int(status.get("active") or 0), int(status.get("terminating") or 0))
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
            return JobState("Succeeded", pod_name, exit_code if exit_code is not None else 0, message, labels, active_pods)
        if (status.get("failed") or 0) > 0:
            cond = next((c for c in status.get("conditions") or [] if c.get("type") == "Failed"), {})
            why = " ".join(x for x in (cond.get("reason"), cond.get("message")) if x)[:300]
            return JobState("Failed", pod_name, exit_code, why or message or "job failed", labels, active_pods)
        if pod is None:
            return JobState("Creating", None, None, "no pod yet", labels)
        phase = pstatus.get("phase") or "Pending"
        if phase in ("Succeeded", "Failed"):          # the pod ended, the Job status lags a moment behind
            return JobState(phase, pod_name, exit_code, message, labels, live)
        return JobState(phase, pod_name, None, message, labels)

    def delete_job(self, name: str) -> None:
        """Job과 그 파드를 지운다(propagationPolicy Foreground). 이미 없으면 무시한다."""
        self._req("DELETE", f"{self._jobs()}/{name}", ok=(200, 202, 404),
                  json={"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground"})
        log.info("k8s: job %s deleted", name)

    def can_i(self, verb: str, resource: str, group: str = "") -> bool:
        """이 클라이언트의 사용자가 namespace 안에서 ``resource``에 ``verb`` 작업을 할 수 있는지
        SelfSubjectAccessReview로 확인한다. 시작할 때 권한을 점검하는 데 쓴다.
        API가 거부하거나 연결되지 않으면 K8sError를 던진다.

        One SelfSubjectAccessReview: may this client's user `verb` `resource` (API `group`) in its namespace?"""
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": {"namespace": self.namespace, "verb": verb, "resource": resource, "group": group}}}
        r = self._req("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", json=body)
        st = self._body(r, "POST selfsubjectaccessreviews").get("status") or {}
        if not isinstance(st, dict):
            raise K8sError(f"POST selfsubjectaccessreviews: unexpected answer (status is {type(st).__name__})")
        return bool(st.get("allowed"))
