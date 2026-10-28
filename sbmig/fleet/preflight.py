"""일괄 실행 사전 점검(round 10): 프로젝트 ~500 개를 시작하기 전에 인프라가 살아 있는지 본다.
흐름상 위치: 러너가 첫 프로젝트 전에 한 번(``fleet.preflight``), 그리고 인프라 대기 뒤 재개하기 전마다
:func:`run_preflight` 를 부른다(:mod:`sbmig.fleet.runner`). 점검 순서: 디스크 → Maven → Java → settings.xml →
저장소(Nexus 미러) → LLM. 결과는 점검마다 ASCII 한 줄(``preflight: <점검> ok|FAIL|skip …``) — cp949 콘솔에서
운영자가 그대로 옮겨 적을 수 있게. 실패에는 인프라 종류(``disk | maven | jdk | repository | llm``)와 저절로
풀릴 수 있는지(``transient``: 장애·디스크 부족)를 단다. 설정 오류(Maven 없음, Java 17, 키 거부 등)는 기다려도
안 풀리므로 러너가 바로 멈춘다. 저장소 점검은 Maven 처럼 셸의 프록시 환경 변수를 쓰지 않는다.

Fleet preflight (round 10): is the infrastructure there before the next ~500 projects start?

Checked once before the first project (``fleet.preflight``) and again whenever the fleet resumes after an
infrastructure pause (``fleet.infra_*``, see :mod:`sbmig.fleet.runner`). One short ASCII line per check, so the
operator can retype the failing one:

    preflight: disk       ok   212.4 GB free on D:\\sbmig\\out (min 5)
    preflight: maven      ok   Apache Maven 3.9.9 (…)
    preflight: java       ok   21 (Java version: 21.0.4)
    preflight: settings   ok   C:\\m2\\settings.nexus.xml (mirror external:* -> http://nexus.local/repository/public/)
    preflight: repository ok   http://nexus.local/repository/public/ HTTP 200
    preflight: llm        ok   gpt-6-luna HTTP 200 1.3s

Each failure names the infrastructure kind (``disk | maven | jdk | repository | llm``, the ``infra:<kind>`` flags
of the runs) and whether it can go away by itself (``transient``: an outage, a full disk) or is a configuration
error the fleet should not wait for (no Maven, Java 17, settings file missing, LLM key missing / rejected).

The repository check is one GET to the settings.xml mirror URL the way Maven would go: no environment proxies (Maven
ignores HTTP_PROXY; a ``<proxy>`` of the settings is used), any HTTP answer below 500 counts as reachable (401 / 404
included), and a TLS handshake Python cannot verify still means the server is up (Maven trusts the JDK truststore,
Python does not know the company CA). The LLM check is ``sbmig.fix.company.probe_llm`` — the call `sbmig llm-probe`
makes — skipped for the fake provider and for ``loop.max_llm_calls: -1`` (v2.1: 0 means no limit, -1 no LLM call).


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
    """점검 하나의 결과: 이름, 통과 여부, 설명, 실패 시 인프라 종류(``kind``)와 저절로 풀릴 수
    있는지(``transient``). ``skipped`` 는 점검하지 않음(설정으로 꺼짐 등) — 실패로 치지 않는다."""
    name: str                              # disk | maven | java | settings | repository | llm
    ok: bool
    detail: str = ""
    kind: str = ""                         # infra kind when not ok: disk | maven | jdk | repository | llm
    transient: bool = False                # may recover by itself (outage) — else a configuration error
    skipped: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """실패인가(건너뛴 점검은 실패가 아니다)."""
        return not self.ok and not self.skipped

    def line(self) -> str:
        """``preflight: <이름> ok|FAIL|skip <설명>`` 한 줄(ASCII 만, 설명은 160 자까지: cp949 콘솔용)."""
        state = "skip" if self.skipped else ("ok" if self.ok else "FAIL")
        return f"preflight: {self.name:<10} {state:<4} {ascii_safe(self.detail, 160)}"


def _existing(path: Path) -> Path:
    """아직 없는 ``--out`` 이면 존재하는 가장 가까운 상위 폴더(같은 드라이브의 여유 공간을 재기 위해)."""
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def check_disk(path: str | Path, min_free_gb: float) -> Check:
    """``path`` (일괄 실행의 ``--out``) 드라이브의 여유 공간을 ``fleet.min_free_gb`` 와 비교(0 = 점검 안 함).
    부족은 ``transient`` (지우면 풀린다), 잴 수 없으면 건너뜀.

    Free space on the drive of `path` (the fleet's --out) against `fleet.min_free_gb` (0 = no check)."""
    if not min_free_gb or min_free_gb <= 0:
        return Check("disk", True, "no check (fleet.min_free_gb: 0)", skipped=True)
    try:
        free = shutil.disk_usage(_existing(Path(path))).free / 1024 ** 3
    except OSError as e:
        return Check("disk", True, f"free space unknown: {e}", skipped=True)
    detail = f"{free:.1f} GB free on {Path(path)} (min {min_free_gb:g})"
    return Check("disk", free >= min_free_gb, detail, kind="disk", transient=True, data={"free_gb": free})


def java_major(text: str) -> Optional[int]:
    """``mvn -v`` 의 ``Java version:`` 에서 메이저 번호(``1.8`` 같은 옛 표기는 8).

    21 from `Java version: 21.0.4`, 8 from `Java version: 1.8.0_402`."""
    m = _JAVA_RE.search(text or "")
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def check_maven_java(cfg: Config, maven: Optional[Callable[[Config], Any]] = None) -> list[Check]:
    """``mvn -v`` 한 번으로 두 점검: Maven 이 실행되는가(``build.mvn``, ``build.java_home``), Maven 이 쓰는
    Java 메이저가 ``build.require_java_major`` 와 같은가. 실패는 둘 다 설정 오류다(``transient`` 아님).

    `mvn -v` once: Maven runnable (`build.mvn`, `build.java_home`), Java major = `build.require_java_major`."""
    if maven is None:
        from sbmig.build.mvn import maven_available as maven
    try:
        res = maven(cfg)
    except Exception as e:  # noqa: BLE001 - the check reports, never raises
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
    """settings.xml 의 미러(``id``, ``mirrorOf``, ``url``)와 켜진 프록시(``active`` 가 false 아닌 것).

    Mirrors (`id`, `mirrorOf`, `url`) and active proxies (`protocol`, `host`, `port`, `nonProxyHosts`)."""
    root = ET.parse(path).getroot()
    mirrors = [{"id": _child(m, "id"), "mirrorOf": _child(m, "mirrorOf"), "url": _child(m, "url")}
               for m in _find_all(root, "mirror")]
    proxies = [{"protocol": _child(p, "protocol") or "http", "host": _child(p, "host"), "port": _child(p, "port"),
                "nonProxyHosts": _child(p, "nonProxyHosts")}
               for p in _find_all(root, "proxy") if (_child(p, "active") or "true").lower() != "false"]
    return {"mirrors": [m for m in mirrors if m["url"]], "proxies": [p for p in proxies if p["host"]]}


def _catch_all(mirrors: list[dict]) -> Optional[dict]:
    """빌드가 실제로 거치는 미러: ``*`` / ``external:*`` 를 받는 것, 없으면 첫 미러."""
    for m in mirrors:
        parts = {x.strip() for x in m["mirrorOf"].split(",")}
        if parts & {"*", "external:*", "external:http:*"}:
            return m
    return mirrors[0] if mirrors else None


def check_settings(cfg: Config) -> Check:
    """``build.settings_file`` 이 있고 읽히는지, 빌드가 거치는 미러가 무엇인지(RUNBOOK §0.1).
    설정 안 됨(또는 치환 안 된 ``${…}``)은 건너뜀, 파일 없음·깨짐은 설정 오류(kind ``repository``).

    `build.settings_file` exists and parses; names the catch-all mirror the builds go through (RUNBOOK §0.1)."""
    sf = (cfg.build.settings_file or "").strip()
    if not sf or (sf.startswith("${") and sf.endswith("}")):
        return Check("settings", True, "build.settings_file not set (Maven's own settings; the poms' repositories "
                                       "are contacted directly)", skipped=True)
    path = Path(sf).expanduser().absolute()            # the path Maven gets (`-s`, sbmig.build.mvn.base_command)
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
    """settings.xml 의 프록시 중 이 URL 에 쓸 것: ``nonProxyHosts`` 에 걸리면 건너뛴다
    (Maven 이 프록시를 고르는 규칙 그대로)."""
    host = urlparse(url).hostname or ""
    for p in proxies:
        if any(fnmatch.fnmatch(host, pat.strip()) for pat in (p.get("nonProxyHosts") or "").split("|") if pat.strip()):
            continue
        target = f"http://{p['host']}:{p['port'] or 80}"
        return {"http": target, "https": target}
    return {}


def _http_get(url: str, proxies: dict[str, str], timeout: float) -> int:
    """GET 한 번의 HTTP 상태 코드. 셸의 HTTP_PROXY / NO_PROXY 는 쓰지 않는다(Maven 도 안 쓴다 — 쓰면 Maven 과
    다른 길로 가서 점검이 거짓으로 통과하거나 실패한다). 본문은 받지 않는다(``stream=True`` 후 바로 닫음)."""
    import requests

    with requests.Session() as s:
        s.trust_env = False                       # like Maven: HTTP_PROXY / NO_PROXY of the shell are not used
        resp = s.get(url, proxies=proxies or None, timeout=timeout, allow_redirects=False, stream=True)
        try:
            return resp.status_code
        finally:
            resp.close()


def check_repository(settings: Check, *, http_get: Optional[Callable[[str, dict, float], int]] = None,
                     timeout: float = REPOSITORY_TIMEOUT_S) -> Check:
    """settings 미러 URL 에 GET 한 번: Nexus 장애면 빌드 3 개를 실패시키는 대신 일괄 실행을 기다리게 한다.
    500 미만 응답은 모두 살아 있음(401·404 포함), TLS 인증서를 파이썬이 검증 못 해도 서버가 답했으니 살아 있음
    (Maven 은 JDK truststore 의 사내 CA 를 믿지만 파이썬은 모른다). 연결 실패·5xx 는 ``transient``.

    One GET to the mirror URL of the settings (a Nexus outage stops the fleet instead of failing 3 builds)."""
    mirror = settings.data.get("mirror") if settings.ok else None
    url = (mirror or {}).get("url", "")
    if not url or not url.lower().startswith(("http://", "https://")):
        return Check("repository", True, "no http(s) mirror URL in build.settings_file to check", skipped=True)
    get = http_get or _http_get
    try:
        status = get(url, _proxies_for(url, settings.data.get("proxies") or []), timeout)
    except Exception as e:  # noqa: BLE001 - classify below
        from sbmig.fix.company import transport_cause
        name = type(e).__name__
        if "SSL" in name or "Certificate" in name:     # the server answered the TLS handshake: it is up
            return Check("repository", True, f"{url} answered (TLS certificate not verifiable by Python: "
                                             f"{ascii_safe(transport_cause(e), 80)})")
        return Check("repository", False, f"{url} unreachable: cause={transport_cause(e)}", kind="repository",
                     transient=True)
    if status >= 500:
        return Check("repository", False, f"{url} HTTP {status}", kind="repository", transient=True)
    return Check("repository", True, f"{url} HTTP {status}")


def check_llm(cfg: Config, probe: Optional[Callable[[Config], Any]] = None) -> Check:
    """작은 LLM 호출 한 번(``probe_llm`` = ``sbmig llm-probe`` 와 같은 요청). 가짜 LLM(``fake``)이거나
    ``loop.max_llm_calls: -1`` (LLM 호출 없음)이면 건너뛴다.
    키·모델 거부는 설정 오류, 게이트웨이 장애는 ``transient``.

    One tiny LLM call (`probe_llm`, the `sbmig llm-probe` request) unless no LLM is used."""
    provider = str(cfg.llm.provider)
    if provider in ("fake",):
        return Check("llm", True, f"not checked (llm.provider {provider})", skipped=True)
    if int(getattr(cfg.loop, "max_llm_calls", 0) or 0) < 0:          # -1 = no LLM call at all
        return Check("llm", True, "not checked (loop.max_llm_calls -1: no LLM calls)", skipped=True)
    if probe is None:
        from sbmig.fix.company import probe_llm as probe
    try:
        res = probe(cfg)
    except Exception as e:  # noqa: BLE001 - a probe must never stop the fleet by raising
        return Check("llm", False, f"probe failed: {type(e).__name__}: {e}", kind="llm", transient=True)
    if res.ok:
        return Check("llm", True, f"{res.model} HTTP {res.status if res.status is not None else '-'} "
                                  f"{res.latency_s:.1f}s")
    return Check("llm", False, f"{res.kind} {res.problem}".strip(), kind="llm", transient=bool(res.transient),
                 data={"hints": list(res.hints)})


def run_preflight(cfg: Config, out_dir: str | Path, *, echo: Optional[Echo] = None,
                  maven: Optional[Callable[[Config], Any]] = None, probe: Optional[Callable[[Config], Any]] = None,
                  http_get: Optional[Callable[[str, dict, float], int]] = None) -> list[Check]:
    """모든 점검을 순서대로(disk, maven, java, settings, repository, llm), 하나가 실패해도 끝까지 돈다.
    ``echo`` 가 있으면 줄이 나오는 대로 넘기고(러너는 화면 + ``fleet/sbmig.log``), 없으면 로그에만 쓴다.

    Every check in order (disk, maven, java, settings, repository, llm); `echo` gets each line as it is known."""
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
    """실패한 점검만(건너뛴 것 제외)."""
    return [c for c in checks if c.failed]
