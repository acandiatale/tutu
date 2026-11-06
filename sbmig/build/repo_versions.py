"""v2.1d 의존성 버전 정책 — 저장소(회사 Nexus / Central)에 그 라이브러리의 어떤 릴리스가 있는지
조회해 프롬프트에 '사실' 로 넣는다. 왜: 수정 프롬프트의 규칙 14 는 "javax 시절 라이브러리는 같은 메이저 중
jakarta 빌드인 최신 릴리스로" 다. LLM 은 회사 Nexus 에 무엇이 있는지 모르고, 없는 버전을 지어내면 빌드 한
라운드를 버린다 → sbmig 가 찾아서 "Dependency versions" 절로 넘긴다. 흐름상 위치: 수정 루프가 새 수정 / 보강
LLM 호출 전에 케이스 뒤의 라이브러리마다 ``candidates`` → ``hint_lines`` (로그 ``의존성 버전 조회: <key>
<현재> → 같은 메이저 …, 상위 …``). 마지막 줄은 판정(verdict):
같은 메이저 jakarta 로 올려라 / 그 메이저엔 jakarta 가 없으니 상위 메이저 허용(``<verify>`` 필수) / 사람이
allow_major 로 승인 / 어디에도 jakarta 가 안 보이면 같은 메이저 안의 API 변경으로 보고 코드를 고쳐라(v2.1v). 조회 순서: ``build.repository_url`` → settings 의 catch-all
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
# v2.1z-e: (url, credentials, timeout, max_bytes) → raw bytes (a candidate jar for the jakarta jar scan), None on any error
FetchBytes = Callable[[str, Optional[tuple[str, str]], float, int], Optional[bytes]]

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


def _server_credentials(root: Any, server_id: str, path: Any) -> Optional[tuple[str, str]]:
    """``<server id=…>`` 의 (user, password) — ``${env.X}`` 를 풀고, 암호화된 비밀번호면 None(로그 한 번)."""
    if not server_id:
        return None
    server = next((x for x in _kids(_kid(root, "servers"), "server") if _txt(x, "id") == server_id), None)
    if server is None:
        return None
    user, pw = _txt(server, "username"), _txt(server, "password")
    if pw.startswith("{") and pw.endswith("}") and len(pw) > 2:
        if str(path) not in _logged_encrypted:
            _logged_encrypted.add(str(path))
            log.info("settings %s: the password of server %s is encrypted (settings-security.xml) — the "
                     "release lookup reads the repository without credentials", path, server_id)
        return None
    if not user:
        return None
    ru, rp = _resolve_env(user), _resolve_env(pw)
    return (ru, rp) if ru is not None and rp is not None else None


class SettingsData:
    """settings.xml 하나에서 읽은 것: 미러들(id, url, mirrorOf, 자격증명), 활성 프로필의 저장소들(id, url, 자격증명), 파일 경로.

    What one settings.xml says: its mirrors (id, url, mirrorOf, credentials), the active profiles' repositories
    (id, url, credentials) and the path."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.mirrors: list[tuple[str, str, str, Optional[tuple[str, str]]]] = []   # (id, url, mirrorOf, creds)
        self.repos: list[tuple[str, str, Optional[tuple[str, str]]]] = []           # (id, url, creds) of active profiles
        # 2026-11-06: 활성 프로필의 <pluginRepositories> — Maven 은 플러그인과 **빌드 확장**(<build><extensions>)을 여기서 받는다.
        # 회사 PC 의 wagon-webdav 는 settings 의 pluginRepository(dmz 프록시)에만 있었고 sbmig 의 집합에는 빠져 있었다
        self.plugin_repos: list[tuple[str, str, Optional[tuple[str, str]]]] = []    # (id, url, creds) of active profiles
        self.servers: dict[str, Optional[tuple[str, str]]] = {}
        self.blocked: set[str] = set()                # 2026-11-05: mirror ids with <blocked>true</blocked> (Maven 3.8.1+)


def read_settings(path: Optional[str]) -> Optional[SettingsData]:
    """v2.1z-e: settings.xml 을 Maven 의미대로 읽는다 — 모든 ``<mirror>`` (파일 순서, mirrorOf 패턴 그대로), ``<servers>`` 의
    자격증명(``${env.X}`` 해석, 암호화면 None), 활성 프로필(``<activeProfiles>`` + ``activeByDefault``)의 ``<repositories>`` 와
    ``<pluginRepositories>``(플러그인·빌드 확장은 Maven 이 여기서 받는다, 2026-11-06).
    왜: 회사 PC 의 조회가 settings 의 미러가 아닌 다른 URL 을 쓰고 있었다 — 사용자: "무조건 여기서 받고 안 받아지면 사고".
    없거나 못 읽는 파일 → None.

    A settings.xml by Maven's rules: every `<mirror>` in file order with its `mirrorOf` pattern, the `<servers>`
    credentials, the active profiles' `<repositories>` and `<pluginRepositories>`. None when the file is missing / unreadable."""
    try:
        if not path:
            return None
        p = Path(str(path)).expanduser()
        if not p.is_file():
            return None
        root = _parse_xml(p.read_text(encoding="utf-8", errors="replace"))
        data = SettingsData(str(p))
        for sv in _kids(_kid(root, "servers"), "server"):
            sid = _txt(sv, "id")
            if sid:
                data.servers[sid] = _server_credentials(root, sid, p)
        for m in _kids(_kid(root, "mirrors"), "mirror"):
            raw = _txt(m, "url")
            if not raw:
                continue
            url = _resolve_env(raw)
            mid = _txt(m, "id")
            if not url:
                log.info("settings %s: mirror %s url %r has an unresolved variable — not used", p, mid, raw)
                continue
            data.mirrors.append((mid, _normalize_url(url), _txt(m, "mirrorOf") or "*", data.servers.get(mid)))
            if _txt(m, "blocked").strip().lower() == "true":
                data.blocked.add(mid)
        active = {(x.text or "").strip() for x in _kids(_kid(root, "activeProfiles"), "activeProfile")}
        for prof in _kids(_kid(root, "profiles"), "profile"):
            pid = _txt(prof, "id")
            act = _kid(prof, "activation")
            by_default = act is not None and _txt(act, "activeByDefault").lower() == "true"
            if pid not in active and not by_default:
                continue
            for r in _kids(_kid(prof, "repositories"), "repository"):
                url = _resolve_env(_txt(r, "url"))
                rid = _txt(r, "id")
                if url:
                    data.repos.append((rid or "?", _normalize_url(url), data.servers.get(rid)))
            for r in _kids(_kid(prof, "pluginRepositories"), "pluginRepository"):
                url = _resolve_env(_txt(r, "url"))
                rid = _txt(r, "id")
                if url:
                    data.plugin_repos.append((rid or "?", _normalize_url(url), data.servers.get(rid)))
        return data
    except Exception as e:  # noqa: BLE001 - a broken settings file: nothing from it
        # 2026-11-05: a settings.xml that does not parse silently dropped the Nexus mirror from every lookup — say so
        log.warning("settings %s could not be read (%s: %s) — its mirrors / repositories / servers are NOT used by sbmig's "
                    "repository lookups (Maven may read it differently); fix the file or set build.repository_url", path,
                    type(e).__name__, str(e)[:160])
        return None


def global_settings_file(cfg: Any = None) -> Optional[Path]:
    """Maven 의 전역 settings(``$MAVEN_HOME/conf/settings.xml`` / ``M2_HOME`` / **설정한 ``build.mvn``** 또는 PATH 의 ``mvn`` 에서
    되짚은 설치 폴더), 없으면 None. 2026-11-05: 회사 PC 는 Maven 을 PATH 에 두지 않고 ``build.mvn`` 에 전체 경로로 적어 둔다 —
    그 설치의 ``conf/settings.xml`` 이 Nexus 미러를 들고 있는데 PATH 만 보면 못 찾아 조회 저장소에 Nexus 가 빠졌다.

    Maven's global settings file: `MAVEN_HOME` / `M2_HOME`, the installation behind the configured `build.mvn` (a path,
    or resolved like the build runner does) or `mvn` on PATH; None when none has `conf/settings.xml`."""
    try:
        import shutil
        homes: list[Path] = []
        for var in ("MAVEN_HOME", "M2_HOME"):
            v = os.environ.get(var)
            if v:
                homes.append(Path(v))
        exe: Optional[str] = None
        if cfg is not None and getattr(getattr(cfg, "build", None), "mvn", None):
            try:
                from .mvn import mvn_executable
                exe = mvn_executable(cfg)                      # the same resolution the build runner uses
            except Exception as e:  # noqa: BLE001 - fall back to PATH
                log.debug("global_settings_file: build.mvn not resolved: %s", e)
                exe = None
            if exe and not Path(exe).expanduser().exists():
                exe = None
        exe = exe or shutil.which("mvn") or shutil.which("mvn.cmd")
        if exe:
            real = Path(exe).expanduser().resolve()
            homes += [real.parent.parent, real.parent.parent / "libexec"]
        for h in homes:
            f = h / "conf" / "settings.xml"
            if f.is_file():
                return f
    except Exception as e:  # noqa: BLE001
        log.debug("global_settings_file: %s", e)
    return None


def is_blocking_mirror(mirror_url: str, blocked: bool = False) -> bool:
    """Maven 3.8.1+ 의 차단 미러인가: 그 미러에 ``<blocked>true</blocked>`` 가 붙었거나 URL 의 호스트가 ``0.0.0.0`` (전역 settings 의
    ``maven-default-http-blocker`` 가 쓰는 주소). 차단 미러가 덮은 저장소는 Maven 도 쓰지 않는다.

    A Maven 3.8.1+ blocking mirror: `<blocked>true</blocked>` on it, or a URL whose host is `0.0.0.0` (what the default
    `maven-default-http-blocker` of the global settings uses). A repository it covers is unusable for Maven too."""
    if blocked:
        return True
    try:
        from urllib.parse import urlsplit
        return (urlsplit(mirror_url or "").hostname or "") == "0.0.0.0"
    except Exception:  # noqa: BLE001 - a URL that does not parse blocks nothing
        return False


def mirror_covers(mirror_of: str, repo_id: str, repo_url: str) -> bool:
    """Maven 의 ``mirrorOf`` 규칙: ``*`` 전부, ``external:*`` 로컬호스트·file 제외 전부, ``external:http:*`` 비-https 외부,
    ``central`` / 명시 id, ``!id`` 제외(먼저 본다). 쉼표로 여럿.

    Maven's `mirrorOf` matching: `*`, `external:*` (not localhost / file), `external:http:*`, explicit ids, `!id`
    exclusions (checked first), comma-separated."""
    pats = [x.strip() for x in (mirror_of or "").split(",") if x.strip()]
    if any(x == f"!{repo_id}" for x in pats):
        return False
    low = (repo_url or "").lower()
    external = not (low.startswith("file:") or "://localhost" in low or "://127.0.0.1" in low)
    for x in pats:
        if x == "*" or x == repo_id:
            return True
        if x == "external:*" and external:
            return True
        if x == "external:http:*" and external and low.startswith("http://"):
            return True
    return False


_POM_SKIP = frozenset({"target", ".git", ".svn", ".hg", ".idea", ".settings", "node_modules", "out", "build"})


def pom_repositories(root: Optional[Path], limit: int = 400) -> list[tuple[str, str, str]]:
    """v2.1z-e: 프로젝트 pom 들(과 저장소 안 부모 체인)이 선언한 ``<repositories><repository><url>`` — (url, 출처). ``${…}`` 는
    그 pom 과 부모 pom 들의 ``<properties>`` 로 풀고 못 풀면 뺀다. 중복 없이, 트리 순서.

    The `<repository>` URLs the project's poms declare (`${…}` interpolated from the pom's and its in-repo parents'
    properties; unresolved ones skipped), deduplicated, as (id, url, origin)."""
    out: list[tuple[str, str, str]] = []
    if root is None:
        return out
    base = Path(root)
    if not base.is_dir():
        return out
    poms: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _POM_SKIP and not d.startswith(".")]
            if "pom.xml" in filenames:
                poms.append(Path(dirpath) / "pom.xml")
                if len(poms) >= limit:
                    break
    except OSError:
        return out
    cache: dict[Path, Any] = {}

    def parsed(path: Path) -> Any:
        if path not in cache:
            try:
                cache[path] = _parse_xml(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                cache[path] = None
        return cache[path]

    def props_of(path: Path) -> dict[str, str]:
        props: dict[str, str] = {}
        cur: Optional[Path] = path
        for _ in range(10):
            r = parsed(cur) if cur is not None else None
            if r is None:
                break
            pel = _kid(r, "properties")
            for el in (list(pel) if pel is not None else []):
                name = _local(el.tag)
                if name and name not in props:
                    props[name] = (el.text or "").strip()
            parent = _kid(r, "parent")
            if parent is None:
                break
            rel = _txt(parent, "relativePath") or "../pom.xml"
            nxt = (cur.parent / rel).resolve()
            if nxt.is_dir():
                nxt = nxt / "pom.xml"
            if not nxt.is_file() or nxt == cur:
                break
            try:
                nxt.relative_to(base.resolve())
            except ValueError:
                break
            cur = nxt
        return props

    seen: set[str] = set()
    for pom in poms:
        r = parsed(pom)
        if r is None:
            continue
        repos = _kids(_kid(r, "repositories"), "repository")
        if not repos:
            continue
        props = props_of(pom)
        for rep_el in repos:
            url = _txt(rep_el, "url")
            for _ in range(5):
                if "${" not in url:
                    break
                url = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), url)
            if not url or "${" in url:
                continue
            u = _normalize_url(url)
            if u not in seen:
                seen.add(u)
                try:
                    where = str(pom.relative_to(base))
                except ValueError:
                    where = str(pom)
                out.append((_txt(rep_el, "id") or "?", u, f"pom {where} repository {_txt(rep_el, 'id') or '?'}"))
    return out


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
    def __init__(self, cfg: Config, *, fetch: Optional[Fetch] = None, fetch_bytes: Optional[FetchBytes] = None,
                 project_root: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.project_root = Path(project_root) if project_root else None   # v2.1z-e: the poms' <repositories> join the set
        build = getattr(cfg, "build", None)
        libs = getattr(cfg, "libraries", None)
        self.timeout = float(getattr(build, "repository_timeout_seconds", 10) or 10)
        self.include_prerelease = bool(getattr(libs, "include_prerelease", False))
        self.max_pom_checks = max(0, int(getattr(libs, "max_pom_checks", 6) or 0))
        self._fetch_fn: Fetch = fetch or self._urllib_fetch
        self._fetch_bytes_fn: FetchBytes = fetch_bytes or self._urllib_fetch_bytes   # v2.1z-e: jar downloads
        self._jars: dict[tuple[str, str, str, Optional[str]], Optional[bytes]] = {}
        self._url_done = False
        self._url: Optional[str] = None
        self._creds: Optional[tuple[str, str]] = None
        self._last_error = ""
        self._versions: dict[tuple[str, str], tuple[list[str], str, str]] = {}   # → (versions, source, error)
        self._poms: dict[tuple[str, str, str], Optional[str]] = {}
        # 어느 저장소 URL 이 그 pom 을 줬는지(로컬 저장소면 'local repository'): 운영자가 Maven 의 settings 미러와 비교할 수 있게
        self._pom_sources: dict[tuple[str, str, str], str] = {}
        self._failures = 0                                        # timeouts / connection failures in a row (primary URL)
        self._dead = ""                                           # set → the primary repository is not asked again
        # v2.1z-e: the repository SET — (url, credentials, origin) in query order — and what each URL answered per g:a
        self._sources: Optional[list[tuple[str, Optional[tuple[str, str]], str]]] = None
        self._url_failures: dict[str, int] = {}                   # per URL: timeouts / connection failures in a row
        self._url_dead: dict[str, str] = {}                       # per URL: why it is not asked again this run
        self._trace: dict[tuple[str, str], list[tuple[str, str]]] = {}   # (g, a) → [(url, outcome), …]
        self._answered: dict[tuple[str, str], str] = {}           # (g, a) → the URL that listed its versions
        self.settings_files: list[str] = []                       # the settings.xml files read (user / global / configured)
        self.settings_missing: list[str] = []                     # 2026-11-05: the candidates looked for but missing / unreadable
        self.url_notes: list[str] = []                            # why each URL is in the set / why one was not used

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

    def _urllib_fetch_bytes(self, url: str, credentials: Optional[tuple[str, str]], timeout: float,
                            max_bytes: int) -> Optional[bytes]:
        """v2.1z-e: jar 한 번 내려받기(``max_bytes`` 까지, 더 크면 None — 스캔하지 않는다). 그 밖은 ``_urllib_fetch`` 와 같다.

        One GET for a jar, at most `max_bytes` (a bigger file → None: not scanned); otherwise like `_urllib_fetch`."""
        self._last_error = ""
        headers = {"User-Agent": "sbmig"}
        if credentials:
            token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                length = r.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    self._last_error = f"jar larger than {max_bytes // (1024 * 1024)} MB"
                    return None
                data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                self._last_error = f"jar larger than {max_bytes // (1024 * 1024)} MB"
                return None
            return data
        except urllib.error.HTTPError as e:
            self._last_error = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001 - timeout / unreachable / bad URL: no jar
            self._last_error = f"{type(e).__name__}: {e}"[:120]
        log.debug("jar download %s → %s", url, self._last_error)
        return None

    def jar_bytes(self, group_id: str, artifact_id: str, version: str, classifier: Optional[str] = None, *,
                  max_bytes: int = 20 * 1024 * 1024) -> Optional[bytes]:
        """v2.1z-e: 후보 jar 의 바이트(저장소 URL, 없으면 로컬 저장소; ``max_bytes`` 초과·오류면 None). (g, a, v, classifier)
        별 캐시. 저장소가 ``_dead`` 면 로컬만 본다.

        The candidate jar's bytes (the repository URL, else the local repository; None when bigger than `max_bytes` or
        on any error). Cached per (g, a, v, classifier)."""
        key = (group_id, artifact_id, version, classifier or None)
        if key in self._jars:
            return self._jars[key]
        name = f"{artifact_id}-{version}" + (f"-{classifier}" if classifier else "") + ".jar"
        data: Optional[bytes] = None
        try:
            rel = f"{group_id.replace('.', '/')}/{artifact_id}/{version}/{name}"
            for url, creds, _o in self._ordered_sources(group_id, artifact_id):
                if self._url_dead.get(url):
                    continue
                data = self._fetch_bytes_fn(f"{url}{rel}", creds, self.timeout, max_bytes)
                if data:
                    break
            if not data:
                d = self._local_dir(group_id, artifact_id)
                f = d / version / name if d is not None else None
                if f is not None and f.is_file() and f.stat().st_size <= max_bytes:
                    data = f.read_bytes()
        except Exception as e:  # noqa: BLE001
            log.debug("jar_bytes(%s:%s:%s): %s", group_id, artifact_id, version, e)
            data = None
        self._jars[key] = data or None
        return self._jars[key]

    def _ordered_sources(self, group_id: str, artifact_id: str) -> list[tuple[str, Optional[tuple[str, str]], str]]:
        """(g, a) 의 버전 목록을 준 URL 을 앞에 둔 저장소 순서(그 저장소에 pom / jar 도 있을 가능성이 가장 크다)."""
        srcs = list(self.repository_urls())
        hit = self._answered.get((group_id, artifact_id))
        if hit:
            srcs.sort(key=lambda t: 0 if t[0] == hit else 1)
        return srcs

    def _fetch(self, url: str, credentials: Optional[tuple[str, str]] = None, base: Optional[str] = None
               ) -> Optional[str]:
        """GET 한 번. 시간 초과 / 연결 실패가 ``UNREACHABLE_LIMIT`` 번 연속이면 그 저장소 URL 은 이 인스턴스(한 실행)에서 다시
        묻지 않는다 — 미러 없는 폐쇄망이면 모든 케이스의 모든 라이브러리마다 ``repository_timeout_seconds`` 를 기다리게
        된다. 404 / 401 같은 응답은 '닿는다' 는 뜻이라 연속 횟수를 0 으로 돌린다. v2.1z-e: URL 마다 따로 센다(``base`` = 그
        저장소 URL; 없으면 ``url`` 의 저장소를 ``_sources`` 에서 찾는다).

        One GET through the fetch function. After `UNREACHABLE_LIMIT` timeouts / connection failures in a row THAT
        repository URL is not contacted again by this instance (one run): an air-gapped host without a mirror would
        otherwise wait `repository_timeout_seconds` for every library of every case."""
        base = base or next((u for u, _c, _o in self.repository_urls() if url.startswith(u)), None) or self._url or ""
        if credentials is None and base:
            credentials = next((c for u, c, _o in self.repository_urls() if u == base), None)
        dead = self._url_dead.get(base) or (self._dead if base == self._url else "")
        if dead:
            self._last_error = dead
            return None
        self._last_error = ""
        try:
            text = self._fetch_fn(url, credentials, self.timeout)
        except Exception as e:  # noqa: BLE001 - an injected fetch never breaks the lookup
            self._last_error = ("timeout" if isinstance(e, TimeoutError) else f"{type(e).__name__}: {e}")[:120]
            log.debug("release lookup %s → %s", url, e)
            text = None
        if text:
            self._url_failures[base] = 0
            if base == self._url:
                self._failures = 0
            return text
        if self._last_error.startswith(("timeout", "unreachable")):
            n = self._url_failures.get(base, 0) + 1
            self._url_failures[base] = n
            if base == self._url:
                self._failures = n
            if n >= UNREACHABLE_LIMIT:
                why = f"{base} did not answer ({self._last_error}); skipped for the rest of the run"
                self._url_dead[base] = why
                if base == self._url:
                    self._dead = why
                log.info("release lookup: %s did not answer %d times in a row (%s) — not asked again in this run",
                         base, n, self._last_error)
        else:
            self._url_failures[base] = 0                          # an answer (404 / 401) means it is reachable
            if base == self._url:
                self._failures = 0
        return None

    # ------------------------------------------------------------------ where
    def repository_url(self) -> Optional[str]:
        """첫 번째(주) 저장소 URL(끝 ``/``), 없으면 None — v2.1z-e 부터 ``repository_urls`` 의 첫 항목(settings 의 미러가 먼저).

        The primary base URL (trailing `/`), None when there is none: the first entry of `repository_urls()`."""
        if self._url_done:
            return self._url
        self._url_done = True
        srcs = self.repository_urls()
        self._url, self._creds = (srcs[0][0], srcs[0][1]) if srcs else (None, None)
        return self._url

    def repository_urls(self) -> list[tuple[str, Optional[tuple[str, str]], str]]:
        """v2.1z-e: 묻는 저장소 전부, Maven 의 의미대로 (url, 자격증명, 이유) 순서로 — settings 가 기준이다(사용자: "무조건 여기서 받고
        안 받아지면 사고"). 저장소 목록 = settings 활성 프로필의 repositories(와 pluginRepositories) → 프로젝트 pom(과 부모 체인)의 repositories →
        Maven Central(``central``); 각각 그 id/URL 을 덮는 **첫 미러**(``mirrorOf`` 규칙)가 있으면 미러 URL 로 바뀐다(``mirrorOf *``
        면 pom 의 저장소는 절대 직접 묻지 않는다). offline 이면 미러 없는 Central 은 뺀다. 명시적 ``build.repository_url`` 은 운영자의
        결정이라 **맨 앞**(회사 PC 가 그렇게 설정돼 있다) — 자격증명은 같은 URL 의 미러 / 저장소의 ``<server>``, 아니면
        ``build.repository_server_id`` 로; settings 가 가리키는 주 URL 과 다르면 WARNING 한 번("Maven 은 <settings url> 에서 받고
        sbmig 의 조회는 build.repository_url 을 쓴다")으로 묵은 yml 이 보이게 한다. settings 파일: ``build.settings_file``, 없으면
        ``~/.m2/settings.xml``, 그리고 전역 ``$MAVEN_HOME/conf/settings.xml``(사용자 파일 항목이 먼저). 한 실행에 한 번 계산,
        INFO 로 어떤 URL 을 왜 쓰는지 남긴다.

        Every repository the lookup asks, by Maven's rules, as (url, credentials, reason): an explicit
        `build.repository_url` FIRST (the operator's decision; credentials from the `<server>` of the mirror / repository
        with the same URL, else `build.repository_server_id`; one WARNING naming both when it differs from the
        settings-derived primary), then the repositories (settings active-profile repositories and pluginRepositories, the project poms'
        repositories, Maven Central) each replaced by the FIRST settings mirror whose `mirrorOf` covers it; unmirrored
        Central dropped when offline. Settings files: `build.settings_file`, else `~/.m2/settings.xml`, plus the global
        `$MAVEN_HOME/conf/settings.xml`. Computed once; INFO lines say which URL is used and why."""
        if self._sources is not None:
            return self._sources
        out: list[tuple[str, Optional[tuple[str, str]], str]] = []
        notes: list[str] = []
        try:
            build = getattr(self.cfg, "build", None)
            offline = bool(getattr(build, "offline", False))
            # the settings files, user before global (Maven merges them with the user's entries first)
            files: list[tuple[Path, str]] = []
            configured_settings = getattr(build, "settings_file", None)
            if configured_settings and str(configured_settings).strip():
                files.append((Path(str(configured_settings)).expanduser(), "build.settings_file"))
            else:
                # Maven's user settings: `user.home`/.m2/settings.xml — Python's home is %USERPROFILE% (HOMEDRIVE+HOMEPATH
                # without it); the probe prints the path looked for so a PC where the two differ shows it
                files.append((Path.home() / ".m2" / "settings.xml", "~/.m2/settings.xml"))
            g = global_settings_file(self.cfg)
            if g is not None and all(g.resolve() != f.resolve() for f, _o in files if f.exists()):
                files.append((g, "global settings"))
            datas: list[tuple[SettingsData, str]] = []
            for f, origin in files:
                d = read_settings(str(f))
                if d is not None:
                    datas.append((d, origin))
                    self.settings_files.append(f"{d.path} ({origin})")
                else:
                    state = "unreadable (see the WARNING above)" if f.is_file() else "missing"
                    self.settings_missing.append(f"{f} ({origin}): {state}")
            # (id, url, mirrorOf, creds, origin, blocked) — user entries first: a user mirror that redefines the global
            # blocker's id (the documented override) wins by order, and `blocked` is that file's flag, not the id's
            mirrors = [(mid, url, mo, creds, origin, mid in d.blocked) for d, origin in datas for mid, url, mo, creds in d.mirrors]
            servers: dict[str, Optional[tuple[str, str]]] = {}
            for d, _o in reversed(datas):             # user entries win over global ones
                servers.update(d.servers)
            # the repositories to resolve, in lookup order
            repos: list[tuple[str, str, Optional[tuple[str, str]], str]] = []
            for d, origin in datas:
                for rid, url, creds in d.repos:
                    repos.append((rid, url, creds, f"{origin} active-profile repository '{rid}'"))
                # Maven asks the plugin repositories for plugins AND build extensions (2026-11-06: the company's wagon-webdav lived
                # only in a settings pluginRepository); a URL already listed as a <repository> is asked once
                for rid, url, creds in getattr(d, "plugin_repos", []):
                    if not any(u == url for _r, u, _c, _o in repos):
                        repos.append((rid, url, creds, f"{origin} active-profile pluginRepository '{rid}'"))
            for rid, url, origin in pom_repositories(self.project_root):
                repos.append((rid, url, servers.get(rid), origin))
            repos.append(("central", CENTRAL, None, "Maven Central"))
            settings_gave_url = False
            for rid, url, creds, origin in repos:
                hit = next(((mid, murl, mo, mcreds, morigin, mblocked) for mid, murl, mo, mcreds, morigin, mblocked in mirrors
                            if mirror_covers(mo, rid, url)), None)
                if hit is not None:
                    mid, murl, mo, mcreds, morigin, mblocked = hit
                    if is_blocking_mirror(murl, mblocked):
                        # 2026-11-05 (회사 PC): Maven 3.8.1+ 의 전역 settings 가 기본으로 넣는 `maven-default-http-blocker`
                        # (mirrorOf external:http:*, url http://0.0.0.0/) 가 사내 Nexus 의 http 주소를 덮었다 — Maven 과 똑같이 그
                        # 저장소는 쓸 수 없다. 0.0.0.0 을 묻는 대신(WinError 10049) 왜 빠졌는지 적는다
                        notes.append(f"{rid} ({url}) is blocked by Maven's HTTP blocker (mirror '{mid}', mirrorOf {mo}, {morigin}): "
                                     "Maven 3.8.1+ refuses plain-http external repositories — use an https URL, override the "
                                     "mirror in the user settings.xml, or set build.repository_url (asked first, never mirrored)")
                        continue
                    settings_gave_url = True
                    out.append((murl, mcreds, f"settings mirror '{mid or '?'}' (mirrorOf {mo}) covering {rid} — {morigin}"))
                elif rid == "central" and offline:
                    notes.append("Maven Central skipped (build.offline)")
                else:
                    if origin.startswith("build.settings_file") or origin.startswith("~/.m2") or origin.startswith("global"):
                        settings_gave_url = True
                    out.append((url, creds, origin))
            configured = getattr(build, "repository_url", None)
            if configured and str(configured).strip():
                u = _normalize_url(str(configured))
                # credentials: the <server> of the mirror / repository with the same URL, else build.repository_server_id
                # 2026-11-05: 차단 미러가 덮어 set 에서 빠진 저장소(out 에 없음)의 <server> 도 같은 URL 이면 쓴다 — 운영자가
                # build.repository_url 로 그 http Nexus 를 다시 가리킬 때 로그인이 사라지면 안 된다
                sid = getattr(build, "repository_server_id", None)
                creds, creds_from = None, ""
                if sid:                                   # the operator's explicit server id beats every URL heuristic
                    creds = servers.get(str(sid))
                    if creds is None:
                        notes.append(f"build.repository_server_id {sid}: no usable <server> credentials in the settings")
                    else:
                        creds_from = f"; credentials from server {sid}"
                if creds is None:
                    creds = next((c for cu, c, _o in out if cu == u and c), None) or next(
                        (mc for _i, mu, _mo, mc, _mo2, _b in mirrors if mu == u and mc), None) or next(
                        (rc for _rid, ru, rc, _ro in repos if ru == u and rc), None)
                    if creds is not None:
                        creds_from = "; credentials from the settings entry with the same URL"
                derived = next((x[0] for x in out if x[0] != CENTRAL), None)
                if derived and derived != u:
                    notes.append(f"Maven itself resolves from {derived} (settings); sbmig's lookups use build.repository_url {u} "
                                 "— a stale yml value would show up here")
                out.insert(0, (u, creds, "build.repository_url (explicit: first)" + creds_from))
        except Exception as e:  # noqa: BLE001
            # 2026-11-05: 여기서 조용히 넘어가면 build.repository_url 까지 사라진 채 Central 만 남는다 — 화면(note:)과 로그에 남긴다
            notes.append(f"repository set incomplete: building it failed midway ({type(e).__name__}: {e}) — an sbmig error, "
                         "report this line; the URLs above are what was collected before it")
        seen: set[str] = set()
        uniq: list[tuple[str, Optional[tuple[str, str]], str]] = []
        for u, c, o in out:
            if u and u not in seen:
                seen.add(u)
                uniq.append((u, c, o))
        self._sources = uniq
        self.url_notes = notes
        for u, _c, o in uniq:
            log.info("repository lookup: %s (%s)", u, o)
        for m in self.settings_missing:
            log.info("repository lookup: settings %s", m)
        if self.project_root is None:
            log.info("repository lookup: no project root — the poms' <repositories> are not part of this set")
        for n in notes:
            (log.warning if n.startswith(("Maven itself resolves", "build.repository_server_id", "repository set incomplete"))
             else log.info)(
                "repository lookup: %s", n)
        if not uniq:
            log.info("repository lookup: no repository URL (offline, no mirror / repository / build.repository_url)")
        return uniq

    def lookup_trace(self, group_id: str, artifact_id: str) -> list[tuple[str, str]]:
        """v2.1z-e: (g, a) 의 maven-metadata.xml 을 어느 URL 에 물어 무엇이 돌아왔는지 — [(url, 결과)] (조회 뒤에 있다)."""
        return list(self._trace.get((group_id, artifact_id), []))

    def remote_failures(self, group_id: str, artifact_id: str) -> str:
        """v2.1z-e: 원격 저장소가 하나도 답하지 못했으면 "remote repositories unreadable: <url> HTTP 401, …" 한 문장, 아니면 ""."""
        trace = self.lookup_trace(group_id, artifact_id)
        remote = [(u, o) for u, o in trace if u.startswith(("http://", "https://"))]
        if not remote or any(o.startswith("ok") for _u, o in remote):
            return ""
        bad = [(u, o) for u, o in remote if not o.startswith(("not found", "empty"))]
        return ("remote repositories unreadable: " + ", ".join(f"{u} {o}" for u, o in bad)) if bad else ""

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
        trace: list[tuple[str, str]] = []
        try:
            sources = self.repository_urls()
            rel = f"{group_id.replace('.', '/')}/{artifact_id}/maven-metadata.xml"
            for url, creds, _origin in sources:
                if self._url_dead.get(url):
                    trace.append((url, "skipped: did not answer earlier in this run"))
                    continue
                meta = f"{url}{rel}"
                text = self._fetch(meta, creds, url)
                got = _metadata_versions(text) if text else []
                if got:
                    versions, source = got, meta
                    trace.append((url, f"ok ({len(got)} release(s))"))
                    self._answered[key] = url
                    break
                # 결과 분류: 'not found'(404, 또는 오류 없이 빈 답) 는 "그 저장소에 없다" 는 사실, 나머지(timeout·연결 실패·401/403·
                # 5xx) 는 "못 읽었다" — 둘을 가른다. 못 읽은 저장소가 있으면 "없음" 이 Nexus 에 대한 사실로 읽히지 않게 말한다.
                if text:
                    outcome = "empty maven-metadata.xml"
                elif not self._last_error or self._last_error.startswith("HTTP 404"):
                    outcome = "not found (HTTP 404)" if self._last_error else "not found"
                elif self._last_error.startswith(("HTTP 401", "HTTP 403")):
                    outcome = f"{self._last_error.split()[0]} {self._last_error.split()[1]} (credentials?)"
                else:
                    outcome = self._last_error
                trace.append((url, outcome))
            if not versions:
                remote = [(u, o) for u, o in trace if u.startswith(("http://", "https://"))]
                unreadable = [(u, o) for u, o in remote if not o.startswith(("ok", "not found", "empty"))]
                if not sources:
                    error = "no repository URL (offline)"
                elif unreadable:
                    error = "remote repositories unreadable: " + ", ".join(f"{u} {o}" for u, o in unreadable)
                else:
                    error = "maven-metadata.xml not found at " + ", ".join(u + rel for u, _o in trace[:3]) \
                        + (f" (+{len(trace) - 3} more)" if len(trace) > 3 else "")
                d = self._local_dir(group_id, artifact_id)
                local = self._local_versions(d)
                if local:
                    versions, source = local, str(d)
                    trace.append((str(d), f"ok ({len(local)} release(s), local repository)"))
                    error = ("remote repositories unreadable: " + ", ".join(f"{u} {o}" for u, o in unreadable)
                             + "; local repository only") if unreadable else ""
                else:
                    trace.append((str(d) if d is not None else "local repository", "nothing"))
        except Exception as e:  # noqa: BLE001
            log.debug("versions(%s:%s): %s", group_id, artifact_id, e)
            error = error or f"{type(e).__name__}: {e}"[:120]
        self._trace[key] = trace
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
            rel = f"{group_id.replace('.', '/')}/{artifact_id}/{version}/{artifact_id}-{version}.pom"
            for url, creds, _o in self._ordered_sources(group_id, artifact_id):
                text = self._fetch(f"{url}{rel}", creds, url)
                if text:
                    self._pom_sources[key] = url
                    break
            if not text:
                d = self._local_dir(group_id, artifact_id)
                f = d / version / f"{artifact_id}-{version}.pom" if d is not None else None
                if f is not None and f.is_file():
                    text = f.read_text(encoding="utf-8", errors="replace")
                    self._pom_sources[key] = "local repository"
        except Exception as e:  # noqa: BLE001
            log.debug("pom_text(%s:%s:%s): %s", group_id, artifact_id, version, e)
            text = None
        self._poms[key] = text or None
        return self._poms[key]

    def pom_source(self, group_id: str, artifact_id: str, version: str) -> Optional[str]:
        """``pom_text`` 가 그 pom 을 읽어 온 저장소 URL(로컬 저장소면 'local repository'), 아직 안 읽었거나 못 읽었으면 None.
        2026-11-06 회사 실행: sbmig 는 읽었는데 Maven 은 404 — 둘이 다른 URL 을 물었는지 로그에서 바로 보이게.

        The repository URL `pom_text` read that pom from (or 'local repository'); None when not read."""
        return self._pom_sources.get((group_id, artifact_id, version))

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
            rv.urls_tried = [f"{u} → {o}" for u, o in self.lookup_trace(g, a)]
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
        if where:
            # v2.1z-e: 원격이 못 읽혔으면 그렇다고 말한다 — "newer release 없음" 이 Nexus 에 대한 사실로 읽히지 않게
            failed = [t.split(" → ", 1) for t in (rv.urls_tried or []) if t.startswith(("http://", "https://"))]
            bad = [f"{u} {o}" for u, o in failed if not o.startswith(("ok", "not found", "empty"))]
            if bad:
                where = f" (remote repositories unreadable: {', '.join(bad)}; local repository only)"
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
    필수) → 릴리스는 있는데 jakarta 가 어디에도 없음(v2.1v: 같은 메이저 안의 API 변경 — 관리 버전에 맞춰 코드를
    고치고 버전은 그대로; ``<no_script>`` 거절을 시키지 않는다). 사실이 없으면(조회 오류, 현재 버전 모름) "".

    ONE explicit rule-14 verdict so the model does not have to combine the table line and the repository line
    itself (2026-10-27, "qlrm 같은 경우는 메이저로 올라가야 하지 않아?"):
    * a same-major jakarta build exists (repository ✓, or the table pins one on the current line) → raise to it, no
      `<verify>`;
    * none on the current line but the table / the repository knows a jakarta release of a higher major → the major
      upgrade is allowed here: newest release of that major, port what changes, `<verify>` required;
    * a person's `allow_major` → approved as given;
    * releases but no jakarta build known on any line (v2.1v: usually a jakarta-native library whose pom declares no
      `jakarta.*` dependency, e.g. Spring itself) → the API changed inside the same major: fix the code against the
      managed version and leave the version (never a `<no_script>` refusal).
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
            # v2.1v (B-H1 / E-H1): "jakarta 여부 미상"(Spring 처럼 pom 이 jakarta.* 를 선언하지 않는 jakarta 네이티브
            # 라이브러리)을 "jakarta 빌드 없음 → <no_script>" 로 읽어 거짓 거절을 시켰다 — 같은 메이저 안의 API 변경으로 본다
            return (f"{rv.key} → verdict: no jakarta build is known on any line — the API changed inside the same "
                    f"major: fix the code against the version the parent chain / Boot BOM manages and leave the "
                    f"version")
        return ""
    except Exception as e:  # noqa: BLE001
        log.debug("_verdict_line(%s): %s", getattr(rv, "key", "?"), e)
        return ""
