"""v2.1d 의존성 버전 정책 — 저장소(회사 Nexus / Central)에 그 라이브러리의 어떤 릴리스가 있는지
조회해 프롬프트에 '사실' 로 넣는다. 왜: 수정 프롬프트의 규칙 14 는 "javax 시절 라이브러리는 같은 메이저 중
jakarta 빌드인 최신 릴리스로" 다. LLM 은 회사 Nexus 에 무엇이 있는지 모르고, 없는 버전을 지어내면 빌드 한
라운드를 버린다 → sbmig 가 찾아서 "Dependency versions" 절로 넘긴다. 흐름상 위치: 수정 루프가 새 수정 / 보강
LLM 호출 전에 케이스 뒤의 라이브러리마다 ``candidates`` → ``hint_lines`` (로그 ``의존성 버전 조회: <key>
<현재> → 같은 메이저 …, 상위 …``). 마지막 줄은 판정(verdict):
같은 메이저 jakarta 로 올려라 / 그 메이저엔 jakarta 가 없으니 상위 메이저 허용(``<verify>`` 필수) / 사람이
allow_major 로 승인 / 어디에도 없으니 머물러라. 조회 순서: ``build.repository_url`` → settings 의 catch-all
미러(자격증명 포함) → Central(offline 이 아니면) → 로컬 저장소. 지킬 것: 같은 메이저 우선; 시간 초과 / 연결
실패가 2번 연속이면 그 실행에서 더는 묻지 않는다(폐쇄망에서 케이스마다 기다리지 않게); 절대 예외 없음(실패는
``RepositoryVersions.error``).

v2.1d dependency version policy — what the repository has for a library (docs/INTERFACES.md "v2.1d").

Why: the policy in the fix prompt says "raise a javax-era library to the newest release of the SAME major that is a
jakarta build". The LLM cannot know which releases the company's Nexus has, and guessing a version that does not
exist costs a build round; so sbmig looks it up and hands the facts over ("Dependency versions" section).

`RepositoryLookup(cfg)` reads `maven-metadata.xml` (and candidate poms) from ONE base URL: `build.repository_url`,
else the catch-all `<mirror>` of `build.settings_file` (or of the user's `~/.m2/settings.xml` when no settings file
is configured — the one Maven itself reads; with the matching `<server>` credentials, `${env.X}` resolved; an
encrypted password → no auth, logged once), else Maven Central unless `build.offline`. Every HTTP call has
`build.repository_timeout_seconds`; the local repository (`<local repo>/<g>/<a>/`) is the fallback when the URL gives
nothing. Results are cached per instance (one run). Never raises: a failed lookup is a `RepositoryVersions` with
`error` set.

`candidates(key, current)` → `RepositoryVersions`: the same-major releases newer than `current` (oldest → newest,
prereleases only with `libraries.include_prerelease`), the newest release of every higher major, and for up to
`libraries.max_pom_checks` candidate poms (newest same-major first, walking down while they are jakarta builds; then
one per higher major, newest first; budget left → down the jakarta higher majors to find the first jakarta release)
whether the release is a jakarta build (its pom declares a `jakarta.*` dependency or a `jakarta` classifier /
artifact) and the minimum Java it states. `hint_lines(rv, entry)` renders the prompt lines.


"""
from __future__ import annotations

import base64
import logging
import os
import re
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import RepositoryVersions, VersionCandidate

from .depchanges import major_of

log = logging.getLogger("sbmig.build.repo_versions")

Fetch = Callable[[str, Optional[tuple[str, str]], float], Optional[str]]   # (url, (user, password) | None, timeout) → text

CENTRAL = "https://repo1.maven.org/maven2/"
MAX_FETCH_BYTES = 8 * 1024 * 1024
# 연속 2번 시간 초과 / 연결 실패면 이 실행 동안 저장소를 건너뛴다(미러
# 없는 폐쇄망에서 라이브러리마다 timeout 을 기다리지 않게).
UNREACHABLE_LIMIT = 2                 # timeouts / connection failures in a row → the repository is skipped (one run)
_TOKEN_RE = re.compile(r"\d+|[A-Za-z]+")
_ENV_RE = re.compile(r"\$\{env\.([A-Za-z0-9_.-]+)\}")
# '아직 릴리스가 아님' 한정어 — 기본은 후보에서 뺀다(``libraries.include_prerelease``). 아래 _RELEASE_* 는
# 붙어 있어도 릴리스인 한정어.
# qualifiers that mean "not a release yet" (Maven's order: alpha < beta < milestone < rc < snapshot < release)
_PRERELEASE_ORDER = {"alpha": 2, "a": 2, "ea": 1, "beta": 3, "b": 3, "milestone": 4, "m": 4, "preview": 4, "pre": 4,
                     "rc": 5, "cr": 5, "snapshot": 6, "dev": 1, "nightly": 1, "incubating": 1}
# qualifiers that still name a release (2.7.18.RELEASE, 5.6.15.Final, 33.0.0-jre, 9.4.53.v20231009, 1.0-sp1, …)
_RELEASE_EQUAL = {"release", "final", "ga"}
_RELEASE_OTHER = {"sp", "jre", "jdk", "java", "android", "v", "r", "jakarta", "javax", "ee", "redhat", "osgi",
                  "bundle", "jdk8", "jdk11", "jdk17"}
_END = (1, 0, "")
# javax 로 시작하지만 Java EE 가 아니라 jakarta 로 옮겨지지
# 않는 API — 이것만 쓰는 pom 을 'javax 빌드' 로 보지 않는다.
_NON_EE_JAVAX = ("javax.cache", "javax.money", "javax.measure", "javax.jdo", "javax.media", "javax.vecmath",
                 "javax.help", "javax.usb", "javax.time")
_logged_encrypted: set[str] = set()


# ---------------------------------------------------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------------------------------------------------
def _tokens(v: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(v or "")]


def parse_version(v: str) -> tuple:
    """Maven 버전 정렬 키: 숫자는 정수, 끝의 0 은 무시(1.0 == 1.0.0), 릴리스가 그 프리릴리스보다 위(3.3.0-RC1
    < 3.3.0 < 3.3.1), ``sp`` / ``jre`` 등은 같은 릴리스보다 위, 모르는 단어는 가장 아래. 절대 예외 없음.

    A sortable key for Maven versions: numeric parts as ints, trailing zeros ignored (1.0 == 1.0.0), a release
    above its prereleases (3.3.0-RC1 < 3.3.0 < 3.3.1, 2.6.0-M1 < 2.6.0), `sp` / `jre` / … above the plain release,
    unknown words lowest. Never raises; junk sorts low."""
    try:
        items: list[tuple[int, int, str]] = []
        run: list[tuple[int, int, str]] = []

        def flush() -> None:
            while run and run[-1] == (2, 0, ""):
                run.pop()
            items.extend(run)
            run.clear()

        for t in _tokens(str(v or "").strip()):
            if t.isdigit():
                run.append((2, int(t), ""))
                continue
            flush()
            if t in _RELEASE_EQUAL:
                continue
            if t in _PRERELEASE_ORDER:
                items.append((0, _PRERELEASE_ORDER[t], ""))
            elif _release_word(t):
                items.append((1, 1 if t.startswith("sp") else 2, t))
            else:
                items.append((0, 0, t))
        flush()
        if not items:
            return ((0, -1, ""),)
        return tuple(items) + (_END,)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return ((0, -1, ""),)


def _release_word(t: str) -> bool:
    return t in _RELEASE_EQUAL or t in _RELEASE_OTHER or re.fullmatch(r"(sp|jre|jdk)\d*", t) is not None


def is_prerelease(v: str) -> bool:
    """프리릴리스인가: milestone / RC / alpha / beta / SNAPSHOT / preview / ea / cr, 그리고 숫자 뒤의 '릴리스
    한정어가 아닌' 모든 단어. 숫자로 시작하지 않는 버전(RELEASE, LATEST, 쓰레기 값)도 릴리스가 아니다.

    milestone / RC / alpha / beta / SNAPSHOT / preview / ea / cr spellings (`2.6.0-M1`, `3.3.0.RC2`,
    `1.0-SNAPSHOT`), and any other word after the numbers that is not a release qualifier (`3.0-alpa-1`);
    `2.6.0`, `2.7.18.RELEASE`, `5.6.15.Final`, `33.0.0-jre`, `1.0-sp1`, `9.4.53.v20231009` are releases. A version
    that does not start with a number (`RELEASE`, `LATEST`, junk) is not a release either."""
    try:
        toks = _tokens(str(v or ""))
        if toks and toks[0] == "v" and len(toks) > 1:
            toks = toks[1:]
        if not toks or not toks[0].isdigit():
            return True
        return any(not t.isdigit() and (t in _PRERELEASE_ORDER or not _release_word(t)) for t in toks)
    except Exception:  # noqa: BLE001  # pragma: no cover
        return False


def _vkey(v: Optional[str]) -> tuple:
    return parse_version(v or "")


# ---------------------------------------------------------------------------------------------------------------------
# settings.xml
# ---------------------------------------------------------------------------------------------------------------------
def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kids(el: Any, name: str) -> list[Any]:
    return [c for c in list(el) if _local(c.tag) == name] if el is not None else []


def _kid(el: Any, name: str) -> Any:
    k = _kids(el, name)
    return k[0] if k else None


def _txt(el: Any, name: str) -> str:
    c = _kid(el, name)
    return (c.text or "").strip() if c is not None else ""


def _resolve_env(value: str) -> Optional[str]:
    """``${env.NAME}`` 을 환경 변수로 푼다. 변수가 없거나 다른 ``${…}`` 가 남으면 None(그 값은 쓰지 않는다).

    `${env.NAME}` from the environment; None when a variable is unset or another `${…}` stays."""
    missing = False

    def repl(m: re.Match[str]) -> str:
        nonlocal missing
        val = os.environ.get(m.group(1))
        if val is None:
            missing = True
            return m.group(0)
        return val

    out = _ENV_RE.sub(repl, value or "")
    if missing or "${" in out:
        return None
    return out


def _normalize_url(url: str) -> str:
    u = (url or "").strip()
    return u if u.endswith("/") else u + "/"


def _parse_xml(text: str) -> Any:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return ET.fromstring(data)


def mirror_from_settings(path: str) -> Optional[tuple[str, Optional[tuple[str, str]]]]:
    """settings.xml 의 catch-all 미러(``*`` / ``external:*``, 없으면 첫 미러)의 (url, 자격증명).
    자격증명은 미러 id 와 같은 ``<server>`` 에서(``${env.X}`` 해석; 암호화된 비밀번호면 인증 없이 읽고 로그 한
    번). 미러가 없거나 못 읽으면 None.

    (url, credentials) of the catch-all mirror (`<mirrorOf>*</mirrorOf>` / `external:*`, else the first mirror) of
    a settings.xml; credentials from the `<server>` with the mirror's id (`${env.X}` resolved; encrypted → None).
    None when the file has no mirror / cannot be read."""
    try:
        if not path:
            return None
        p = Path(str(path)).expanduser()
        if not p.is_file():
            return None
        root = _parse_xml(p.read_text(encoding="utf-8", errors="replace"))
        mirrors = [m for m in _kids(_kid(root, "mirrors"), "mirror") if _txt(m, "url")]
        if not mirrors:
            return None

        def catch_all(m: Any) -> bool:
            vals = [x.strip() for x in _txt(m, "mirrorOf").split(",")]
            return "*" in vals or "external:*" in vals

        mirror = next((m for m in mirrors if catch_all(m)), mirrors[0])
        url = _resolve_env(_txt(mirror, "url"))
        if not url:
            log.info("settings %s: mirror %s url %r has an unresolved variable — not used", p, _txt(mirror, "id"),
                     _txt(mirror, "url"))
            return None
        creds: Optional[tuple[str, str]] = None
        mid = _txt(mirror, "id")
        server = next((s for s in _kids(_kid(root, "servers"), "server") if mid and _txt(s, "id") == mid), None)
        if server is not None:
            user, pw = _txt(server, "username"), _txt(server, "password")
            if pw.startswith("{") and pw.endswith("}") and len(pw) > 2:
                if str(p) not in _logged_encrypted:
                    _logged_encrypted.add(str(p))
                    log.info("settings %s: the password of server %s is encrypted (settings-security.xml) — the "
                             "release lookup reads the mirror without credentials", p, mid)
            elif user:
                ru, rp = _resolve_env(user), _resolve_env(pw)
                if ru is not None and rp is not None:
                    creds = (ru, rp)
        return _normalize_url(url), creds
    except Exception as e:  # noqa: BLE001 - a broken settings file: no mirror
        log.debug("mirror_from_settings(%s): %s", path, e)
        return None


# ---------------------------------------------------------------------------------------------------------------------
# poms: jakarta build? minimum Java?
# ---------------------------------------------------------------------------------------------------------------------
def _pom_root(text: str) -> Any:
    try:
        return _parse_xml(text)
    except Exception:  # noqa: BLE001 - not XML: the caller decides
        return None


def pom_is_jakarta(text: str) -> Optional[bool]:
    """pom 이 jakarta 빌드인가: ``jakarta.*`` 의존성(또는 artifactId / classifier 에 jakarta, 자기 artifactId
    포함)이 있으면 True, javax EE 의존성만 있으면 False, 둘 다 아니면 None(모름).

    True when the pom declares a `jakarta.*` dependency (or an artifactId / classifier with `jakarta`, its own
    artifactId included), False when it declares `javax.*` EE dependencies only, None when neither."""
    root = _pom_root(text)
    if root is None:
        low = text or ""
        if re.search(r"<groupId>\s*jakarta\.", low):
            return True
        if re.search(r"<groupId>\s*javax\.", low):
            return False
        return None
    if "jakarta" in _txt(root, "artifactId").lower():
        return True
    jakarta = javax = False
    for dep in root.iter():
        if _local(dep.tag) != "dependency":
            continue
        g, a, c = _txt(dep, "groupId").lower(), _txt(dep, "artifactId").lower(), _txt(dep, "classifier").lower()
        if g.startswith("jakarta.") or "jakarta" in a or "jakarta" in c:
            jakarta = True
        elif (g.startswith("javax.") or g == "javax" or a.startswith("javax.")) and not g.startswith(_NON_EE_JAVAX):
            javax = True
    if jakarta:
        return True
    return False if javax else None


def _java_number(v: str) -> Optional[int]:
    v = (v or "").strip()
    m = re.fullmatch(r"1\.(\d+)(?:\.\d+)?", v)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d+)(?:\.\d+)*", v)
    return int(m.group(1)) if m else None


def pom_java(text: str) -> Optional[int]:
    """pom 이 밝힌 최소 Java: ``maven.compiler.release`` / ``.target`` / ``.source`` / ``java.version``
    프로퍼티, 없으면 maven-compiler-plugin 의 ``<release>`` / ``<target>``(``1.8`` → 8).

    The minimum Java a pom states: `maven.compiler.release` / `.target` / `.source` / `java.version` properties,
    else `<release>` / `<target>` of maven-compiler-plugin (`1.8` → 8)."""
    root = _pom_root(text)
    if root is None:
        return None
    pel = _kid(root, "properties")
    props = {_local(c.tag): (c.text or "").strip() for c in (list(pel) if pel is not None else [])
             if isinstance(c.tag, str)}

    def resolve(v: str) -> str:
        for _ in range(3):
            m = re.fullmatch(r"\$\{([^}]+)\}", v.strip())
            if not m:
                break
            v = props.get(m.group(1), "")
        return v

    for k in ("maven.compiler.release", "maven.compiler.target", "maven.compiler.source", "java.version"):
        n = _java_number(resolve(props.get(k, "")))
        if n:
            return n
    for plugin in root.iter():
        if _local(plugin.tag) != "plugin" or _txt(plugin, "artifactId") != "maven-compiler-plugin":
            continue
        conf = _kid(plugin, "configuration")
        for k in ("release", "target", "source"):
            n = _java_number(resolve(_txt(conf, k)))
            if n:
                return n
    return None


def _metadata_versions(text: str) -> list[str]:
    """maven-metadata.xml 의 ``<versions>`` 목록, 없으면 ``<release>`` / ``<latest>``."""
    root = _pom_root(text or "")
    if root is None:
        return []
    out: list[str] = []
    versioning = _kid(root, "versioning")
    for v in _kids(_kid(versioning, "versions"), "version"):
        t = (v.text or "").strip()
        if t:
            out.append(t)
    if not out:
        for k in ("release", "latest"):
            t = _txt(versioning, k)
            if t:
                out.append(t)
    return out


def _sorted_unique(vs: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for v in vs:
        if v and v not in seen:
            seen[v] = None
    return sorted(seen, key=_vkey)


# ---------------------------------------------------------------------------------------------------------------------
# the lookup
# ---------------------------------------------------------------------------------------------------------------------
class RepositoryLookup:
    """저장소 한 곳(기본 URL 하나)에서 릴리스 목록(maven-metadata.xml)과 후보 pom 을 읽는 조회기. 한 실행에
    하나, 결과는 인스턴스 안에 캐시한다. 테스트는 진짜 저장소에 나가지 않는다: ``tests/conftest.py`` 가 이
    클래스를 오프라인 대역으로 바꾸고, ``tests/build/test_repo_versions.py`` 는 가짜 ``fetch`` 를 주입한다."""
    def __init__(self, cfg: Config, *, fetch: Optional[Fetch] = None) -> None:
        self.cfg = cfg
        build = getattr(cfg, "build", None)
        libs = getattr(cfg, "libraries", None)
        self.timeout = float(getattr(build, "repository_timeout_seconds", 10) or 10)
        self.include_prerelease = bool(getattr(libs, "include_prerelease", False))
        self.max_pom_checks = max(0, int(getattr(libs, "max_pom_checks", 6) or 0))
        self._fetch_fn: Fetch = fetch or self._urllib_fetch
        self._url_done = False
        self._url: Optional[str] = None
        self._creds: Optional[tuple[str, str]] = None
        self._last_error = ""
        self._versions: dict[tuple[str, str], tuple[list[str], str, str]] = {}   # → (versions, source, error)
        self._poms: dict[tuple[str, str, str], Optional[str]] = {}
        self._failures = 0                                        # timeouts / connection failures in a row
        self._dead = ""                                           # set → the repository is not asked again

    # ------------------------------------------------------------------ http
    def _urllib_fetch(self, url: str, credentials: Optional[tuple[str, str]], timeout: float) -> Optional[str]:
        """GET 한 번(User-Agent sbmig, 자격증명이 있으면 basic auth, 프록시는 환경 변수대로). 오류면 None —
        404 는 경고가 아니다(후보 대부분은 원래 없다).

        GET with `User-Agent: sbmig`, basic auth when credentials, urllib's proxy handling (environment). The
        text, or None on any error (404 is not a warning: most candidates are simply absent)."""
        self._last_error = ""
        headers = {"User-Agent": "sbmig"}
        if credentials:
            token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_FETCH_BYTES)
                charset = r.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            self._last_error = f"HTTP {e.code}"
            log.debug("release lookup %s → HTTP %s", url, e.code)
        except TimeoutError as e:
            self._last_error = f"timeout after {timeout:.0f}s"
            log.debug("release lookup %s → timeout (%s)", url, e)
        except urllib.error.URLError as e:
            reason = e.reason
            self._last_error = (f"timeout after {timeout:.0f}s" if isinstance(reason, (socket.timeout, TimeoutError))
                                else f"unreachable ({reason})")
            log.debug("release lookup %s → %s", url, reason)
        except Exception as e:  # noqa: BLE001 - ValueError (bad URL), OSError, http.client trouble
            self._last_error = f"{type(e).__name__}: {e}"[:120]
            log.debug("release lookup %s → %s", url, e)
        return None

    def _fetch(self, url: str) -> Optional[str]:
        """GET 한 번. 시간 초과 / 연결 실패가 ``UNREACHABLE_LIMIT`` 번 연속이면 이 인스턴스(한 실행)는 다시
        묻지 않는다 — 미러 없는 폐쇄망이면 모든 케이스의 모든 라이브러리마다 ``repository_timeout_seconds`` 를
        기다리게 된다. 404 / 401 같은 응답은 '닿는다' 는 뜻이라 연속 횟수를 0 으로 돌린다.

        One GET through the fetch function. After `UNREACHABLE_LIMIT` timeouts / connection failures in a row the
        repository is not contacted again by this instance (one run): an air-gapped host without a mirror would
        otherwise wait `repository_timeout_seconds` for every library of every case."""
        if self._dead:
            self._last_error = self._dead
            return None
        self._last_error = ""
        try:
            text = self._fetch_fn(url, self._creds, self.timeout)
        except Exception as e:  # noqa: BLE001 - an injected fetch never breaks the lookup
            self._last_error = ("timeout" if isinstance(e, TimeoutError) else f"{type(e).__name__}: {e}")[:120]
            log.debug("release lookup %s → %s", url, e)
            text = None
        if text:
            self._failures = 0
            return text
        if self._last_error.startswith(("timeout", "unreachable")):
            self._failures += 1
            if self._failures >= UNREACHABLE_LIMIT:
                self._dead = f"{self._url} did not answer ({self._last_error}); skipped for the rest of the run"
                log.info("release lookup: %s did not answer %d times in a row (%s) — not asked again in this run",
                         self._url, self._failures, self._last_error)
        else:
            self._failures = 0                                    # an answer (404 / 401) means it is reachable
        return None

    # ------------------------------------------------------------------ where
    def repository_url(self) -> Optional[str]:
        """쓸 기본 URL(끝 ``/``), 없으면 None(offline + 미러 없음). 순서: ``build.repository_url``(미러와 같은
        URL 이면 그 자격증명을 빌림) → ``build.settings_file``(없으면 Maven 이 실제로 읽는
        ``~/.m2/settings.xml``)의 미러 → offline 이 아니면 Maven Central.

        The base URL used (trailing `/`), None when none (offline without a mirror)."""
        if self._url_done:
            return self._url
        self._url_done = True
        try:
            build = getattr(self.cfg, "build", None)
            settings = getattr(build, "settings_file", None)
            if not settings or not str(settings).strip():
                user = Path.home() / ".m2" / "settings.xml"
                settings = str(user) if user.is_file() else None
            mirror = mirror_from_settings(settings) if settings else None
            configured = getattr(build, "repository_url", None)
            if configured and str(configured).strip():
                self._url = _normalize_url(str(configured))
                if mirror and mirror[0] == self._url:
                    self._creds = mirror[1]
            elif mirror:
                self._url, self._creds = mirror
            elif not getattr(build, "offline", False):
                self._url = CENTRAL
        except Exception as e:  # noqa: BLE001
            log.debug("repository_url: %s", e)
            self._url = None
        return self._url

    def _local_dir(self, group_id: str, artifact_id: str) -> Optional[Path]:
        try:
            from .mvn import local_repo_path
            return local_repo_path(self.cfg).joinpath(*group_id.split("."), artifact_id)
        except Exception as e:  # noqa: BLE001
            log.debug("local repository path: %s", e)
            return None

    def _local_versions(self, d: Optional[Path]) -> list[str]:
        """로컬 저장소 디렉토리가 아는 버전들: maven-metadata*.xml 의 목록 + pom 이 든 하위 디렉토리 이름."""
        if d is None or not d.is_dir():
            return []
        out: list[str] = []
        try:
            for f in sorted(d.glob("maven-metadata*.xml")):
                try:
                    out += _metadata_versions(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and any(sub.glob("*.pom")):
                    out.append(sub.name)
        except OSError as e:
            log.debug("local repository %s: %s", d, e)
        return out

    # ------------------------------------------------------------------ what
    def _lookup(self, group_id: str, artifact_id: str) -> tuple[list[str], str, str]:
        """(버전 목록, 출처, 오류)를 (g, a) 별로 캐시. URL 의 maven-metadata.xml → 없으면 로컬 저장소. 오류
        문구는 프롬프트 줄에 그대로 나가므로 경우를 나눈다(건너뜀 / 시간 초과 / 못 읽음 / 없음 / offline)."""
        key = (group_id, artifact_id)
        if key in self._versions:
            return self._versions[key]
        versions: list[str] = []
        source = error = ""
        try:
            url = self.repository_url()
            if url:
                meta = f"{url}{group_id.replace('.', '/')}/{artifact_id}/maven-metadata.xml"
                text = self._fetch(meta)
                versions = _metadata_versions(text) if text else []
                if versions:
                    source = meta
                elif self._dead and self._last_error == self._dead:
                    error = self._dead
                elif self._last_error.startswith("timeout"):
                    error = f"{self._last_error} reading {meta}"
                elif self._last_error and not self._last_error.startswith("HTTP 404"):
                    error = f"maven-metadata.xml not readable at {meta} ({self._last_error})"
                else:
                    error = f"maven-metadata.xml not found at {meta}"
            else:
                error = "no repository URL (offline)"
            if not versions:
                d = self._local_dir(group_id, artifact_id)
                local = self._local_versions(d)
                if local:
                    versions, source, error = local, str(d), ""
        except Exception as e:  # noqa: BLE001
            log.debug("versions(%s:%s): %s", group_id, artifact_id, e)
            error = error or f"{type(e).__name__}: {e}"[:120]
        result = (_sorted_unique(versions), source, error)
        self._versions[key] = result
        return result

    def versions(self, group_id: str, artifact_id: str) -> list[str]:
        """저장소 / 로컬 저장소가 아는 그 아티팩트의 모든 릴리스(오래된 것 → 최신).

        Every release of the artifact the repository / local repository knows, sorted (oldest → newest)."""
        return list(self._lookup(group_id, artifact_id)[0])

    def pom_text(self, group_id: str, artifact_id: str, version: str) -> Optional[str]:
        """후보 버전의 pom 텍스트(URL, 없으면 로컬 저장소), 없으면 None. (g, a, v) 별 캐시."""
        key = (group_id, artifact_id, version)
        if key in self._poms:
            return self._poms[key]
        text: Optional[str] = None
        try:
            url = self.repository_url()
            if url:
                text = self._fetch(f"{url}{group_id.replace('.', '/')}/{artifact_id}/{version}/"
                                   f"{artifact_id}-{version}.pom")
            if not text:
                d = self._local_dir(group_id, artifact_id)
                f = d / version / f"{artifact_id}-{version}.pom" if d is not None else None
                if f is not None and f.is_file():
                    text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            log.debug("pom_text(%s:%s:%s): %s", group_id, artifact_id, version, e)
            text = None
        self._poms[key] = text or None
        return self._poms[key]

    def candidates(self, key: str, current: Optional[str]) -> RepositoryVersions:
        """``key``(groupId:artifactId)와 현재 버전으로 RepositoryVersions 를 만든다: 현재보다 새로운 같은
        메이저 릴리스(오래된 → 최신), 상위 메이저마다 최신 하나, 그리고 ``_check_poms`` 로 jakarta 여부·최소
        Java. 프리릴리스는 ``libraries.include_prerelease`` 일 때만. 절대 예외 없음.

        See the module docstring. `key` = groupId:artifactId."""
        cur = current if current and "${" not in current else None
        rv = RepositoryVersions(key=key, current=current, major=major_of(cur))
        try:
            parts = (key or "").split(":")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                rv.error = f"not a groupId:artifactId: {key!r}"
                return rv
            g, a = parts[0], parts[1]
            versions, rv.source, rv.error = self._lookup(g, a)
            if not versions:
                rv.error = rv.error or "no release found"
                return rv
            usable = [v for v in versions if self.include_prerelease or not is_prerelease(v)]
            by_major: dict[int, list[str]] = {}
            for v in usable:
                m = major_of(v)
                if m is not None:
                    by_major.setdefault(m, []).append(v)
            cur_major = rv.major
            if cur_major is not None:
                ck = _vkey(cur)
                same = [v for v in by_major.get(cur_major, []) if _vkey(v) > ck]
                higher_majors = sorted(m for m in by_major if m > cur_major)
            else:
                same = []
                higher_majors = sorted(by_major)
            rv.same_major = [VersionCandidate(version=v, prerelease=is_prerelease(v)) for v in same]
            rv.higher = [VersionCandidate(version=by_major[m][-1], prerelease=is_prerelease(by_major[m][-1]))
                         for m in higher_majors]
            self._check_poms(g, a, rv, by_major)
        except Exception as e:
            log.debug("candidates(%s, %s): %s", key, current, e, exc_info=True)
            rv.error = rv.error or f"{type(e).__name__}: {e}"[:120]
        return rv

    def _check_poms(self, g: str, a: str, rv: RepositoryVersions, by_major: dict[int, list[str]]) -> None:
        """후보 pom 검사(모두 합쳐 최대 ``libraries.max_pom_checks``개): 같은 메이저는 최신부터 내려가다 javax
        빌드를 만나면 멈춘다(그보다 오래된 것도 javax 이므로). 다음 상위 메이저마다 최신 하나. 예산이 남으면
        jakarta 인 상위 메이저를 첫 jakarta 릴리스까지 내려간다(``first_jakarta``)."""
        budget = self.max_pom_checks
        jakarta_seen: dict[str, bool] = {}

        def check(c: VersionCandidate) -> Optional[bool]:
            nonlocal budget
            if budget <= 0:
                return None
            budget -= 1
            text = self.pom_text(g, a, c.version)
            if text:
                c.jakarta = pom_is_jakarta(text)
                c.java = pom_java(text)
            if c.jakarta is not None:
                jakarta_seen[c.version] = c.jakarta
            return c.jakarta

        # the same major, newest first: once a release is a javax build, the older ones are too
        for c in reversed(rv.same_major):
            if budget <= 0:
                break
            if check(c) is False:
                break
        # one per higher major, newest major first
        for c in reversed(rv.higher):
            if budget <= 0:
                break
            check(c)
        # budget left: walk down the jakarta higher majors to their first jakarta release
        for c in reversed(rv.higher):
            if c.jakarta is not True:
                continue
            cm = major_of(c.version)
            older = [v for v in by_major.get(cm, []) if _vkey(v) < _vkey(c.version)] if cm is not None else []
            for v in reversed(older):
                if budget <= 0:
                    break
                probe = VersionCandidate(version=v, prerelease=is_prerelease(v))
                if check(probe) is False:
                    break
        jak = [v for v, ok in jakarta_seen.items() if ok]
        rv.first_jakarta = min(jak, key=_vkey) if jak else None


# ---------------------------------------------------------------------------------------------------------------------
# prompt lines
# ---------------------------------------------------------------------------------------------------------------------
def _tag(c: VersionCandidate, first_jakarta: Optional[str] = None) -> str:
    """후보 하나의 표시 '2.6.0 (jakarta ✓, Java 17)'. 같은 메이저의 첫 jakarta
    릴리스가 따로 있으면 덧붙인다."""
    bits: list[str] = []
    if c.jakarta is True:
        bits.append("jakarta ✓")
    elif c.jakarta is False:
        bits.append("javax")
    if c.java:
        bits.append(f"Java {c.java}")
    if c.prerelease:
        bits.append("prerelease")
    tag = ", ".join(bits)
    if (first_jakarta and c.jakarta is True and first_jakarta != c.version
            and major_of(first_jakarta) == major_of(c.version)):
        tag = f"{tag}; first jakarta release {first_jakarta}" if tag else f"first jakarta release {first_jakarta}"
    return f"{c.version} ({tag})" if tag else c.version


def _listing(cands: list[VersionCandidate], first_jakarta: Optional[str], cap: int = 6) -> str:
    """후보 목록 표시: 최신 ``cap`` 개만, 앞의 나머지는 '… (+n)'."""
    shown = cands[-cap:]
    rest = len(cands) - len(shown)
    if first_jakarta in {c.version for c in shown}:
        first_jakarta = None                                  # already visible as a jakarta ✓ release
    body = ", ".join(_tag(c, first_jakarta) for c in shown)
    return f"… (+{rest}), {body}" if rest > 0 else body


def hint_lines(rv: RepositoryVersions, entry: Any = None) -> list[str]:
    """라이브러리 하나의 프롬프트 줄들: 표 항목 줄(``library_hint_line``) 먼저, 다음 저장소 사실(같은 메이저의
    새 릴리스와 jakarta ✓ / javax, 상위 메이저들), 마지막에 판정 한 줄(``_verdict_line``). 조회 실패면 '같은
    메이저에 머물라' 한 줄. 절대 예외 없음(프롬프트 줄 때문에 수정이 깨지면 안 된다).

    Prompt lines for one library: the table entry first (`library_hint_line`), then what the repository has
    (`<key> — current 2.4.2; same major (2.x) in the repository, newer than current: 2.5.0 (javax), 2.6.0 (jakarta
    ✓, Java 17)`; `<key> — higher majors: 3.x: 3.3.0 (jakarta ✓; first jakarta release 3.2.0)`), or the lookup
    error in one line. Never raises."""
    lines: list[str] = []
    try:
        if entry is not None:
            from sbmig.core.jakarta_libraries import library_hint_line
            lines.append(library_hint_line(entry, rv.current))
        key = rv.key
        if rv.error and not rv.same_major and not rv.higher:
            lines.append(f"{key} — the repository could not be read ({rv.error}); stay on the same major and ask "
                         "if unsure")
            return lines
        where = "" if not rv.source or rv.source.startswith(("http://", "https://")) else " (local repository only)"
        fj = rv.first_jakarta
        if rv.current and rv.major is not None:
            if rv.same_major:
                lines.append(f"{key} — current {rv.current}; same major ({rv.major}.x) in the repository{where}, "
                             f"newer than current: {_listing(rv.same_major, fj)}")
            else:
                lines.append(f"{key} — current {rv.current}; no newer release on the {rv.major}.x line in the "
                             f"repository{where}")
            if rv.higher:
                majors = "; ".join(f"{major_of(c.version)}.x: {_tag(c, fj)}" for c in rv.higher)
                lines.append(f"{key} — higher majors: {majors}")
        else:
            cur = f"current {rv.current} (unresolved)" if rv.current else "current version unknown"
            if rv.higher:
                majors = "; ".join(f"{major_of(c.version)}.x: {_tag(c, fj)}" for c in rv.higher)
                lines.append(f"{key} — {cur}; newest release per major in the repository{where}: {majors}")
            else:
                lines.append(f"{key} — {cur}; the repository lists no release{where}")
        verdict = _verdict_line(rv, entry)
        if verdict:
            lines.append(verdict)
    except Exception as e:  # noqa: BLE001 - a prompt line never breaks a fix
        log.debug("hint_lines(%s): %s", getattr(rv, "key", "?"), e)
    return lines


def _verdict_line(rv: RepositoryVersions, entry: Any = None) -> str:
    """규칙 14 의 판정을 한 줄로 명시한다 — 모델이 표 줄과 저장소 줄을 스스로 조합하지 않게(2026-10-27,
    "qlrm 같은 경우는 메이저로 올라가야 하지 않아?"). 코드의 검사 순서: 사람의 ``allow_major`` → 같은 메이저
    jakarta(저장소 ✓, 또는 표가 현재 메이저의 더 새 버전을 앎) → 그 메이저엔 없고 표 / 저장소가 상위 메이저
    jakarta 를 앎(표 버전, 없으면 jakarta 인 가장 높은 메이저의 최신; 메이저 업그레이드 허용, ``<verify>``
    필수) → 릴리스는 있는데 jakarta 가 어디에도 없음(머물고
    ``<analysis>`` 에 적기). 사실이 없으면(조회 오류, 현재 버전 모름) "".

    ONE explicit rule-14 verdict so the model does not have to combine the table line and the repository line
    itself (2026-10-27, "qlrm 같은 경우는 메이저로 올라가야 하지 않아?"):
    * a same-major jakarta build exists (repository ✓, or the table pins one on the current line) → raise to it, no
      `<verify>`;
    * none on the current line but the table / the repository knows a jakarta release of a higher major → the major
      upgrade is allowed here: newest release of that major, port what changes, `<verify>` required;
    * a person's `allow_major` → approved as given;
    * nothing known → stay on the major and say so in `<analysis>` (BLOCKED / Handover is better than a guess).
    "" when the facts are not there (lookup error, unknown current version)."""
    try:
        current = rv.current
        cm = rv.major if rv.major is not None else major_of(current)
        if not current or cm is None:
            return ""
        allow = bool(getattr(entry, "allow_major", False)) if entry is not None else False
        ev = str(getattr(entry, "version", "") or "") if entry is not None else ""
        em = major_of(ev) if ev else None
        if allow and ev:
            return (f"{rv.key} → verdict: a person approved the move to {ev} (allow_major) — use it as given and "
                    f"write <verify>")
        same_jakarta = [c.version for c in rv.same_major if c.jakarta]
        pinned_same = ev if ev and em == cm and (not current or parse_version(ev) > parse_version(current)) else ""
        if same_jakarta or pinned_same:
            target = same_jakarta[-1] if same_jakarta else pinned_same
            return (f"{rv.key} → verdict: raise to {target} (same major {cm}.x, jakarta build) — no major change, "
                    f"no <verify> needed")
        higher_jakarta = [c for c in rv.higher if c.jakarta]
        table_higher = ev if ev and em is not None and em > cm else ""
        if table_higher or higher_jakarta:
            best = table_higher or higher_jakarta[-1].version
            since = f"; first jakarta release {rv.first_jakarta}" if rv.first_jakarta and rv.first_jakarta != best \
                else (f"; jakarta since {getattr(entry, 'jakarta_since', '')}"
                      if entry is not None and getattr(entry, "jakarta_since", "") else "")
            return (f"{rv.key} → verdict: the {cm}.x line has NO jakarta build, so a major upgrade to {best} is "
                    f"allowed here (rule 14: the newest release of that major{since}) — port what that major "
                    f"changes (coordinates, removed packages, API) and write <verify>; the change will be listed "
                    f"for the operator")
        if rv.same_major or rv.higher:
            return (f"{rv.key} → verdict: no jakarta build is known on any line — stay on {cm}.x, say so in "
                    f"<analysis> and answer <no_script> if the case cannot be fixed without it")
        return ""
    except Exception as e:  # noqa: BLE001
        log.debug("_verdict_line(%s): %s", getattr(rv, "key", "?"), e)
        return ""
