"""일괄 실행을 시작하기 전에 디스크, Maven, Java, settings.xml, 저장소, LLM이 정상인지 점검하는 모듈.

The runner (:mod:`sbmig.fleet.runner`) calls :func:`run_preflight` once before the first project and again
before it restarts after waiting out an infrastructure outage. A failed check carries the infrastructure
kind (``disk | maven | jdk | repository | llm``) and ``transient``; a configuration error (no Maven, wrong
Java version, key rejected) does not pass by waiting, so the runner stops at once. The lines are ASCII so
they read the same on a cp949 console.

One short ASCII line per check, so the operator can retype the failing one:

    preflight: disk       ok   212.4 GB free on D:\\sbmig\\out (min 5)
    preflight: maven      ok   Apache Maven 3.9.9 (…)
    preflight: java       ok   21 (Java version: 21.0.4)
    preflight: settings   ok   C:\\m2\\settings.nexus.xml (mirror external:* -> http://nexus.local/repository/public/)
    preflight: repository ok   http://nexus.local/repository/public/ HTTP 200
    preflight: llm        ok   gpt-6-luna HTTP 200 1.3s

The kind is the ``infra:<kind>`` flag of the runs. ``transient``: an outage, a full disk; not transient: no
Maven, Java 17, settings file missing, LLM key missing / rejected.

The repository check is one GET to the settings.xml mirror URL the way Maven would go: no environment proxies
(Maven ignores HTTP_PROXY; a ``<proxy>`` of the settings is used), any HTTP answer below 500 counts as
reachable (401 / 404 included), and a TLS handshake Python cannot verify still means the server is up (Maven
trusts the JDK truststore, Python does not know the company CA). The LLM check is
``sbmig.fix.company.probe_llm`` (the ``sbmig llm-probe`` call), skipped for the fake provider and for
``loop.max_llm_calls: -1`` (0 means no limit).
"""
from __future__ import annotations

import fnmatch
import logging
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from sbmig.core.config import Config
from sbmig.fix.company import ascii_safe

log = logging.getLogger("sbmig.fleet")

Echo = Callable[[str], None]
REPOSITORY_TIMEOUT_S = 10.0
_JAVA_RE = re.compile(r"Java version:\s*(\d+)(?:\.(\d+))?")


@dataclass
class Check:
    """점검 하나의 결과를 담는 데이터 클래스.
    실패하면 인프라 종류(``kind``)와 기다리면 풀릴 수 있는지(``transient``)를 담는다.
    ``skipped``는 점검하지 않았다는 뜻이다. 실패로 보지 않는다."""
    name: str                              # disk | maven | java | settings | repository | llm
    ok: bool
    detail: str = ""
    kind: str = ""                         # 실패했을 때의 인프라 종류: disk | maven | jdk | repository | llm
    transient: bool = False                # 장애처럼 저절로 풀릴 수 있으면 True, 설정 오류면 False
    skipped: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """점검이 실패했는지 반환한다. 건너뛴 점검은 실패가 아니다."""
        return not self.ok and not self.skipped

    def line(self) -> str:
        """``preflight: <이름> ok|FAIL|skip <설명>`` 형식의 한 줄을 만든다.
        cp949 콘솔에서도 읽히게 ASCII만 쓴다. 설명은 160자에서 자른다."""
        state = "skip" if self.skipped else ("ok" if self.ok else "FAIL")
        return f"preflight: {self.name:<10} {state:<4} {ascii_safe(self.detail, 160)}"


def _existing(path: Path) -> Path:
    """경로가 아직 없으면 존재하는 가장 가까운 상위 폴더를 반환한다.
    그 폴더로 같은 드라이브의 여유 공간을 잰다."""
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def check_disk(path: str | Path, min_free_gb: float) -> Check:
    """일괄 실행의 ``--out``(``path``)이 있는 드라이브에 여유 공간이 ``fleet.min_free_gb`` 이상인지 점검한다.
    설정값이 0이면 점검하지 않는다. 공간을 잴 수 없어도 건너뛴다.
    공간 부족은 파일을 지우면 풀리므로 ``transient``로 본다."""
    if not min_free_gb or min_free_gb <= 0:
        return Check("disk", True, "no check (fleet.min_free_gb: 0)", skipped=True)
    try:
        free = shutil.disk_usage(_existing(Path(path))).free / 1024 ** 3
    except OSError as e:
        return Check("disk", True, f"free space unknown: {e}", skipped=True)
    detail = f"{free:.1f} GB free on {Path(path)} (min {min_free_gb:g})"
    return Check("disk", free >= min_free_gb, detail, kind="disk", transient=True, data={"free_gb": free})


def java_major(text: str) -> Optional[int]:
    """``mvn -v`` 출력의 ``Java version:``에서 메이저 번호를 꺼낸다. ``1.8`` 같은 이전 표기는 8로 읽는다.

    21 from `Java version: 21.0.4`, 8 from `Java version: 1.8.0_402`."""
    m = _JAVA_RE.search(text or "")
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def check_maven_java(cfg: Config, maven: Optional[Callable[[Config], Any]] = None) -> list[Check]:
    """``mvn -v``를 한 번 실행해서 Maven이 실행되는지 점검한다.
    Maven이 쓰는 Java 메이저 버전이 ``build.require_java_major``와 같은지도 점검한다.
    둘 다 설정 오류라서 실패해도 ``transient``가 아니다.

    Maven is ``build.mvn`` with ``build.java_home``. ``build.require_java_major`` 0 skips the Java check."""
    if maven is None:
        from sbmig.build.mvn import maven_available as maven
    try:
        res = maven(cfg)
    except Exception as e:  # noqa: BLE001 - 점검은 결과로 알리고 예외를 던지지 않는다
        res = (False, f"{type(e).__name__}: {e}")
    ok, detail = (bool(res[0]), str(res[1] or "")) if isinstance(res, tuple) else (bool(res), "")
    if not ok:
        return [Check("maven", False, detail or f"'{cfg.build.mvn}' is not runnable", kind="maven"),
                Check("java", True, "not checked (Maven not runnable)", skipped=True)]
    maven_line = detail.split(";")[0].strip()
    out = [Check("maven", True, maven_line or "runnable")]
    want = int(getattr(cfg.build, "require_java_major", 0) or 0)
    got = java_major(detail)
    java_text = next((p.strip() for p in detail.split(";") if "Java version" in p), "")
    if want <= 0:
        out.append(Check("java", True, f"{got or '?'} (no check: build.require_java_major 0)", skipped=True))
    elif got is None:
        out.append(Check("java", True, "Java version not shown by mvn -v", skipped=True))
    elif got != want:
        out.append(Check("java", False, f"{got} but build.require_java_major is {want} ({java_text}); set "
                                        f"build.java_home / JAVA_HOME to a JDK {want}", kind="jdk"))
    else:
        out.append(Check("java", True, f"{got} ({java_text})"))
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == name]


def _child(e: ET.Element, name: str) -> str:
    return next(((c.text or "").strip() for c in e if _local(c.tag) == name), "")


def read_settings(path: Path) -> dict[str, Any]:
    """settings.xml에서 미러(``id``, ``mirrorOf``, ``url``)와 활성화된 프록시를 읽는다.
    URL이 없는 미러와 host가 없는 프록시는 뺀다.

    Proxy keys: ``protocol`` (default http), ``host``, ``port``, ``nonProxyHosts``."""
    root = ET.parse(path).getroot()
    mirrors = [{"id": _child(m, "id"), "mirrorOf": _child(m, "mirrorOf"), "url": _child(m, "url")}
               for m in _find_all(root, "mirror")]
    proxies = [{"protocol": _child(p, "protocol") or "http", "host": _child(p, "host"), "port": _child(p, "port"),
                "nonProxyHosts": _child(p, "nonProxyHosts")}
               for p in _find_all(root, "proxy") if (_child(p, "active") or "true").lower() != "false"]
    return {"mirrors": [m for m in mirrors if m["url"]], "proxies": [p for p in proxies if p["host"]]}


def _catch_all(mirrors: list[dict]) -> Optional[dict]:
    """빌드가 실제로 거치는 미러를 고른다. ``*``나 ``external:*``를 받는 미러가 없으면 첫 미러를 반환한다."""
    for m in mirrors:
        parts = {x.strip() for x in m["mirrorOf"].split(",")}
        if parts & {"*", "external:*", "external:http:*"}:
            return m
    return mirrors[0] if mirrors else None


def check_settings(cfg: Config) -> Check:
    """``build.settings_file``이 있고 읽히는지 점검한다. 빌드가 거치는 미러도 알려 준다(RUNBOOK §0.1).
    설정되지 않았거나 ``${…}``가 치환되지 않았으면 건너뛴다.
    파일이 없거나 깨졌으면 설정 오류(kind ``repository``)로 본다."""
    sf = (cfg.build.settings_file or "").strip()
    if not sf or (sf.startswith("${") and sf.endswith("}")):
        return Check("settings", True, "build.settings_file not set (Maven's own settings; the poms' repositories "
                                       "are contacted directly)", skipped=True)
    path = Path(sf).expanduser().absolute()            # Maven이 `-s`로 받는 경로와 같다(sbmig.build.mvn.base_command)
    if not path.is_file():
        return Check("settings", False, f"build.settings_file not found: {path}", kind="repository")
    try:
        info = read_settings(path)
    except (ET.ParseError, OSError) as e:
        return Check("settings", False, f"{path} is not a readable settings.xml: {e}", kind="repository")
    mirror = _catch_all(info["mirrors"])
    if mirror is None:
        return Check("settings", True, f"{path} (no <mirror>: the poms' repositories are contacted directly)",
                     data=info)
    return Check("settings", True, f"{path} (mirror {mirror['mirrorOf'] or '?'} -> {mirror['url']})",
                 data={**info, "mirror": mirror})


def _proxies_for(url: str, proxies: list[dict]) -> dict[str, str]:
    """settings.xml의 프록시 중 이 URL에 사용할 프록시를 고른다.
    Maven과 같은 규칙으로 ``nonProxyHosts``에 걸리는 프록시는 건너뛴다."""
    host = urlparse(url).hostname or ""
    for p in proxies:
        if any(fnmatch.fnmatch(host, pat.strip()) for pat in (p.get("nonProxyHosts") or "").split("|") if pat.strip()):
            continue
        target = f"http://{p['host']}:{p['port'] or 80}"
        return {"http": target, "https": target}
    return {}


def _http_get(url: str, proxies: dict[str, str], timeout: float) -> int:
    """URL에 GET을 한 번 보내고 HTTP 상태 코드를 반환한다.
    셸의 HTTP_PROXY / NO_PROXY는 쓰지 않는다. Maven과 다른 경로로 접속하면 점검 결과가 틀린다.
    본문은 받지 않는다."""
    import requests

    with requests.Session() as s:
        s.trust_env = False
        resp = s.get(url, proxies=proxies or None, timeout=timeout, allow_redirects=False, stream=True)
        try:
            return resp.status_code
        finally:
            resp.close()


def check_repository(settings: Check, *, http_get: Optional[Callable[[str, dict, float], int]] = None,
                     timeout: float = REPOSITORY_TIMEOUT_S) -> Check:
    """settings 미러 URL에 GET을 한 번 보내 저장소가 살아 있는지 점검한다.
    Nexus 장애면 빌드를 실패시키는 대신 일괄 실행을 기다리게 한다.
    500 미만 응답(401, 404 포함)은 살아 있다고 본다.
    TLS 검증이 실패해도 살아 있다고 본다. 파이썬이 사내 CA를 모를 뿐 서버는 응답했다.
    연결 실패와 5xx는 ``transient``로 본다."""
    mirror = settings.data.get("mirror") if settings.ok else None
    url = (mirror or {}).get("url", "")
    if not url or not url.lower().startswith(("http://", "https://")):
        return Check("repository", True, "no http(s) mirror URL in build.settings_file to check", skipped=True)
    get = http_get or _http_get
    try:
        status = get(url, _proxies_for(url, settings.data.get("proxies") or []), timeout)
    except Exception as e:  # noqa: BLE001 - 아래에서 종류를 나눈다
        from sbmig.fix.company import transport_cause
        name = type(e).__name__
        if "SSL" in name or "Certificate" in name:     # TLS 핸드셰이크에 응답했으면 서버는 살아 있다
            return Check("repository", True, f"{url} answered (TLS certificate not verifiable by Python: "
                                             f"{ascii_safe(transport_cause(e), 80)})")
        return Check("repository", False, f"{url} unreachable: cause={transport_cause(e)}", kind="repository",
                     transient=True)
    if status >= 500:
        return Check("repository", False, f"{url} HTTP {status}", kind="repository", transient=True)
    return Check("repository", True, f"{url} HTTP {status}")


def check_llm(cfg: Config, probe: Optional[Callable[[Config], Any]] = None) -> Check:
    """작은 LLM 호출을 한 번 보내 점검한다(``sbmig llm-probe``와 같은 요청).
    가짜 LLM(``fake``)이거나 ``loop.max_llm_calls: -1``이면 건너뛴다.
    키나 모델 거부는 설정 오류, 게이트웨이 장애는 ``transient``로 본다.
    probe가 예외를 던지면 ``transient`` 실패로 기록한다."""
    provider = str(cfg.llm.provider)
    if provider in ("fake",):
        return Check("llm", True, f"not checked (llm.provider {provider})", skipped=True)
    if int(getattr(cfg.loop, "max_llm_calls", 0) or 0) < 0:          # -1이면 LLM을 호출하지 않는다
        return Check("llm", True, "not checked (loop.max_llm_calls -1: no LLM calls)", skipped=True)
    if probe is None:
        from sbmig.fix.company import probe_llm as probe
    try:
        res = probe(cfg)
    except Exception as e:  # noqa: BLE001 - probe 예외로 일괄 실행을 멈추지 않는다
        return Check("llm", False, f"probe failed: {type(e).__name__}: {e}", kind="llm", transient=True)
    if res.ok:
        return Check("llm", True, f"{res.model} HTTP {res.status if res.status is not None else '-'} "
                                  f"{res.latency_s:.1f}s")
    return Check("llm", False, f"{res.kind} {res.problem}".strip(), kind="llm", transient=bool(res.transient),
                 data={"hints": list(res.hints)})


def run_preflight(cfg: Config, out_dir: str | Path, *, echo: Optional[Echo] = None,
                  maven: Optional[Callable[[Config], Any]] = None, probe: Optional[Callable[[Config], Any]] = None,
                  http_get: Optional[Callable[[str, dict, float], int]] = None) -> list[Check]:
    """모든 점검을 disk, maven, java, settings, repository, llm 순서로 실행한다.
    하나가 실패해도 끝까지 실행한다.
    ``echo``가 있으면 줄이 나올 때마다 넘긴다. 없으면 로그에만 기록한다."""
    checks: list[Check] = []

    def add(c: Check) -> None:
        checks.append(c)
        if echo is not None:
            echo(c.line())
        else:
            log.log(logging.WARNING if c.failed else logging.INFO, "%s", c.line())

    add(check_disk(out_dir, float(getattr(cfg.fleet, "min_free_gb", 0) or 0)))
    for c in check_maven_java(cfg, maven):
        add(c)
    settings = check_settings(cfg)
    add(settings)
    add(check_repository(settings, http_get=http_get))
    add(check_llm(cfg, probe))
    return checks


def failed(checks: list[Check]) -> list[Check]:
    """실패한 점검만 반환한다. 건너뛴 점검은 제외한다."""
    return [c for c in checks if c.failed]
