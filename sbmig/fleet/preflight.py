"""Fleet preflight (round 10): is the infrastructure there before the next ~500 projects start?

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
    name: str                              # disk | maven | java | settings | repository | llm
    ok: bool
    detail: str = ""
    kind: str = ""                         # infra kind when not ok: disk | maven | jdk | repository | llm
    transient: bool = False                # may recover by itself (outage) — else a configuration error
    skipped: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return not self.ok and not self.skipped

    def line(self) -> str:
        state = "skip" if self.skipped else ("ok" if self.ok else "FAIL")
        return f"preflight: {self.name:<10} {state:<4} {ascii_safe(self.detail, 160)}"


def _existing(path: Path) -> Path:
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def check_disk(path: str | Path, min_free_gb: float) -> Check:
    """Free space on the drive of `path` (the fleet's --out) against `fleet.min_free_gb` (0 = no check)."""
    if not min_free_gb or min_free_gb <= 0:
        return Check("disk", True, "no check (fleet.min_free_gb: 0)", skipped=True)
    try:
        free = shutil.disk_usage(_existing(Path(path))).free / 1024 ** 3
    except OSError as e:
        return Check("disk", True, f"free space unknown: {e}", skipped=True)
    detail = f"{free:.1f} GB free on {Path(path)} (min {min_free_gb:g})"
    return Check("disk", free >= min_free_gb, detail, kind="disk", transient=True, data={"free_gb": free})


def java_major(text: str) -> Optional[int]:
    """21 from `Java version: 21.0.4`, 8 from `Java version: 1.8.0_402`."""
    m = _JAVA_RE.search(text or "")
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def check_maven_java(cfg: Config, maven: Optional[Callable[[Config], Any]] = None) -> list[Check]:
    """`mvn -v` once: Maven runnable (`build.mvn`, `build.java_home`), Java major = `build.require_java_major`."""
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
    """Mirrors (`id`, `mirrorOf`, `url`) and active proxies (`protocol`, `host`, `port`, `nonProxyHosts`)."""
    root = ET.parse(path).getroot()
    mirrors = [{"id": _child(m, "id"), "mirrorOf": _child(m, "mirrorOf"), "url": _child(m, "url")}
               for m in _find_all(root, "mirror")]
    proxies = [{"protocol": _child(p, "protocol") or "http", "host": _child(p, "host"), "port": _child(p, "port"),
                "nonProxyHosts": _child(p, "nonProxyHosts")}
               for p in _find_all(root, "proxy") if (_child(p, "active") or "true").lower() != "false"]
    return {"mirrors": [m for m in mirrors if m["url"]], "proxies": [p for p in proxies if p["host"]]}


def _catch_all(mirrors: list[dict]) -> Optional[dict]:
    for m in mirrors:
        parts = {x.strip() for x in m["mirrorOf"].split(",")}
        if parts & {"*", "external:*", "external:http:*"}:
            return m
    return mirrors[0] if mirrors else None


def check_settings(cfg: Config) -> Check:
    """`build.settings_file` exists and parses; names the catch-all mirror the builds go through (RUNBOOK §0.1)."""
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
    host = urlparse(url).hostname or ""
    for p in proxies:
        if any(fnmatch.fnmatch(host, pat.strip()) for pat in (p.get("nonProxyHosts") or "").split("|") if pat.strip()):
            continue
        target = f"http://{p['host']}:{p['port'] or 80}"
        return {"http": target, "https": target}
    return {}


def _http_get(url: str, proxies: dict[str, str], timeout: float) -> int:
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
    """One GET to the mirror URL of the settings (a Nexus outage stops the fleet instead of failing 3 builds)."""
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
    """One tiny LLM call (`probe_llm`, the `sbmig llm-probe` request) unless no LLM is used."""
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
    """Every check in order (disk, maven, java, settings, repository, llm); `echo` gets each line as it is known."""
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
    return [c for c in checks if c.failed]
