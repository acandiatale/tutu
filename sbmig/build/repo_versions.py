"""저장소(회사 Nexus, Central)에 라이브러리의 어떤 릴리스가 있는지 조회해 수정 프롬프트에 넣는 모듈.
LLM은 Nexus에 무엇이 있는지 모르고, 없는 버전을 지어내면 빌드 한 라운드를 낭비하므로 sbmig가 직접 찾아 넘긴다.
수정 루프는 신규, 보강 LLM 호출 전에 케이스의 라이브러리마다 ``candidates`` -> ``hint_lines``를 호출한다.
프롬프트의 "Dependency versions" 절이 되고, 마지막 줄은 어떤 버전을 쓸지에 대한 결론이다.
- 조회 순서는 ``build.repository_url`` -> settings의 저장소 -> 프로젝트 pom의 저장소 -> Central -> 로컬 저장소다. 미러가 있으면 미러로 조회한다
- 같은 메이저 버전을 우선한다
- 시간 초과나 연결 실패가 두 번 연속 나면 그 실행에서는 그 저장소를 다시 조회하지 않는다
- 예외를 던지지 않고, 실패는 ``RepositoryVersions.error``에 남긴다

docs/INTERFACES.md "v2.1d". The fix prompt's policy: "raise a javax-era library to the newest release of the
SAME major that is a jakarta build"; this module supplies the releases that policy picks from.

`RepositoryLookup(cfg)` reads `maven-metadata.xml` (and candidate poms / jars) from the repository set of
`repository_urls()`, asked in order, with the matching `<server>` credentials (`${env.X}` resolved; an
encrypted password: no auth, logged once). Every HTTP call has `build.repository_timeout_seconds`; the local
repository (`<local repo>/<g>/<a>/`) is the fallback when no URL gives anything. Results are cached per
instance (one run). Never raises: a failed lookup is a `RepositoryVersions` with `error` set.

`candidates(key, current)` returns `RepositoryVersions`: the same-major releases newer than `current` (oldest
first, prereleases only with `libraries.include_prerelease`), the newest release of every higher major, and
for up to `libraries.max_pom_checks` candidate poms (newest same-major first, walking down while they are
jakarta builds; then one per higher major, newest first; budget left: down the jakarta higher majors to find
the first jakarta release) whether the release is a jakarta build (its pom declares a `jakarta.*` dependency
or a `jakarta` classifier / artifact) and the minimum Java it states. `hint_lines(rv, entry)` renders the
prompt lines.
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

# (url, (user, password) 또는 None, timeout)을 받아 본문 텍스트를 반환한다
Fetch = Callable[[str, Optional[tuple[str, str]], float], Optional[str]]
# (url, 자격증명, timeout, max_bytes)를 받아 원본 바이트를 반환한다. jakarta jar 검사에 쓰고, 오류면 None
FetchBytes = Callable[[str, Optional[tuple[str, str]], float, int], Optional[bytes]]
# (url, 자격증명, timeout, "HEAD" | "GET" | "RANGE")를 받아 (HTTP 상태 또는 None, 실패 이유)를 반환한다
Status = Callable[[str, Optional[tuple[str, str]], float, str], "tuple[Optional[int], str]"]

CENTRAL = "https://repo1.maven.org/maven2/"
MAX_FETCH_BYTES = 8 * 1024 * 1024
# 미러가 없는 폐쇄망에서 라이브러리마다 타임아웃을 기다리지 않도록 연속 실패 한도를 둔다
UNREACHABLE_LIMIT = 2
_TOKEN_RE = re.compile(r"\d+|[A-Za-z]+")
_ENV_RE = re.compile(r"\$\{env\.([A-Za-z0-9_.-]+)\}")
# 아직 정식 릴리스가 아니라는 뜻의 한정어. 기본적으로 후보에서 뺀다(``libraries.include_prerelease``)
# Maven's order: alpha < beta < milestone < rc < snapshot < release
_PRERELEASE_ORDER = {"alpha": 2, "a": 2, "ea": 1, "beta": 3, "b": 3, "milestone": 4, "m": 4, "preview": 4, "pre": 4,
                     "rc": 5, "cr": 5, "snapshot": 6, "dev": 1, "nightly": 1, "incubating": 1}
# 붙어 있어도 릴리스로 보는 한정어. 2.7.18.RELEASE, 5.6.15.Final, 33.0.0-jre, 9.4.53.v20231009, 1.0-sp1 등
_RELEASE_EQUAL = {"release", "final", "ga"}
_RELEASE_OTHER = {"sp", "jre", "jdk", "java", "android", "v", "r", "jakarta", "javax", "ee", "redhat", "osgi",
                  "bundle", "jdk8", "jdk11", "jdk17"}
_END = (1, 0, "")
# javax로 시작하지만 Java EE가 아니어서 jakarta로 옮겨지지 않은 API. 이것만 쓰는 pom은 javax 빌드로 보지 않는다
_NON_EE_JAVAX = ("javax.cache", "javax.money", "javax.measure", "javax.jdo", "javax.media", "javax.vecmath",
                 "javax.help", "javax.usb", "javax.time")
_logged_encrypted: set[str] = set()


# ---------------------------------------------------------------------------------------------------------------------
# 버전 비교
# ---------------------------------------------------------------------------------------------------------------------
def _tokens(v: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(v or "")]


def parse_version(v: str) -> tuple:
    """Maven 버전 정렬에 쓰는 키를 만든다.
    끝의 0은 무시하고, 릴리스를 프리릴리스보다 높게, 모르는 단어는 가장 낮게 둔다. 예외를 던지지 않는다.

    1.0 == 1.0.0; 3.3.0-RC1 < 3.3.0 < 3.3.1; 2.6.0-M1 < 2.6.0; `sp` / `jre` / … above the plain release; junk
    sorts low."""
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
    """프리릴리스 버전인지 확인한다.
    숫자 뒤에 릴리스 한정어가 아닌 단어가 있거나 숫자로 시작하지 않으면 프리릴리스로 본다.

    Prereleases: `2.6.0-M1`, `3.3.0.RC2`, `1.0-SNAPSHOT`, `3.0-alpa-1`, `RELEASE`, `LATEST`. Releases:
    `2.6.0`, `2.7.18.RELEASE`, `5.6.15.Final`, `33.0.0-jre`, `1.0-sp1`, `9.4.53.v20231009`."""
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
# settings.xml 읽기
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
    """``${env.NAME}``을 환경 변수 값으로 치환한다. 변수가 없거나 다른 ``${…}``가 남으면 None을 반환한다."""
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
    """``<server id=…>``의 (user, password)를 반환한다.
    비밀번호가 암호화되어 있으면 None을 반환하고 로그를 한 번 남긴다."""
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
    """settings.xml 하나에서 읽은 미러, 활성 프로필의 저장소, 서버 자격증명을 담는 클래스."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.mirrors: list[tuple[str, str, str, Optional[tuple[str, str]]]] = []   # (id, url, mirrorOf, creds)
        self.repos: list[tuple[str, str, Optional[tuple[str, str]]]] = []           # 활성 프로필의 (id, url, creds)
        # 활성 프로필의 <pluginRepositories>. Maven은 플러그인과 build extension을 여기서 받는다
        self.plugin_repos: list[tuple[str, str, Optional[tuple[str, str]]]] = []    # (id, url, creds)
        self.servers: dict[str, Optional[tuple[str, str]]] = {}
        self.blocked: set[str] = set()                # <blocked>true</blocked>인 미러 id. Maven 3.8.1 이상


def read_settings(path: Optional[str]) -> Optional[SettingsData]:
    """settings.xml에서 미러, ``<servers>`` 자격증명, 활성 프로필의 저장소를 Maven 규칙대로 읽는다.
    미러는 파일 순서를 지킨다. 파일이 없거나 읽지 못하면 None을 반환한다.
    파싱에 실패하면 WARNING을 남긴다. 그러면 sbmig의 저장소 조회는 이 파일의 미러를 쓰지 않는다."""
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
    except Exception as e:  # noqa: BLE001
        # 파싱에 실패하면 Nexus 미러가 조회에서 조용히 빠지므로 반드시 경고를 남긴다
        log.warning("settings %s could not be read (%s: %s) — its mirrors / repositories / servers are NOT used by sbmig's "
                    "repository lookups (Maven may read it differently); fix the file or set build.repository_url", path,
                    type(e).__name__, str(e)[:160])
        return None


def global_settings_file(cfg: Any = None) -> Optional[Path]:
    """Maven 전역 settings 파일(``conf/settings.xml``)을 찾는다. 없으면 None을 반환한다.
    ``MAVEN_HOME``, ``M2_HOME``, ``build.mvn``, PATH의 ``mvn`` 순서로 설치 폴더를 찾는다.
    PATH에 Maven이 없고 ``build.mvn``에 전체 경로만 있는 PC도 있으므로 ``build.mvn``도 본다.

    `build.mvn` is resolved like the build runner does (`mvn.mvn_executable`)."""
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
                exe = mvn_executable(cfg)
            except Exception as e:  # noqa: BLE001 - PATH로 넘어간다
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
    """Maven 3.8.1 이상의 차단 미러인지 확인한다.
    ``<blocked>true</blocked>``이거나 호스트가 ``0.0.0.0``이면 차단 미러다.

    `0.0.0.0` is what the default `maven-default-http-blocker` of the global settings uses. A repository it
    covers is unusable for Maven too."""
    if blocked:
        return True
    try:
        from urllib.parse import urlsplit
        return (urlsplit(mirror_url or "").hostname or "") == "0.0.0.0"
    except Exception:  # noqa: BLE001
        return False


def mirror_covers(mirror_of: str, repo_id: str, repo_url: str) -> bool:
    """Maven의 ``mirrorOf`` 규칙에 따라 저장소가 이 미러에 포함되는지 확인한다.
    ``!id`` 제외 규칙을 먼저 본다.

    Supported: `*`, `external:*` (not localhost / file), `external:http:*`, explicit ids, `!id`,
    comma-separated."""
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
    """프로젝트 pom이 선언한 저장소를 중복 없이 (id, url, 출처) 목록으로 반환한다.
    ``${…}``는 pom과 프로젝트 안 부모 pom의 속성으로 치환하고, 치환하지 못한 URL은 뺀다."""
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
    """settings.xml에서 모든 저장소를 받는 미러의 (url, 자격증명)을 반환한다. 그런 미러가 없으면 첫 미러를 쓴다.
    미러가 없거나 파일을 읽지 못하면 None을 반환한다. 비밀번호가 암호화되어 있으면 자격증명은 None이다.

    Catch-all = `<mirrorOf>` contains `*` or `external:*`. Credentials come from the `<server>` with the
    mirror's id (`${env.X}` resolved)."""
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
    except Exception as e:  # noqa: BLE001
        log.debug("mirror_from_settings(%s): %s", path, e)
        return None


# ---------------------------------------------------------------------------------------------------------------------
# pom 확인: jakarta 빌드인지, 최소 Java 버전
# ---------------------------------------------------------------------------------------------------------------------
def _pom_root(text: str) -> Any:
    try:
        return _parse_xml(text)
    except Exception:  # noqa: BLE001 - XML이 아니면 호출하는 쪽이 판단한다
        return None


def pom_is_jakarta(text: str) -> Optional[bool]:
    """pom이 jakarta 빌드인지 확인한다.
    jakarta 의존성이 있으면 True, javax EE 의존성만 있으면 False, 둘 다 없으면 None을 반환한다.

    jakarta = a `jakarta.*` groupId, or `jakarta` in an artifactId / classifier (the pom's own artifactId
    included). `_NON_EE_JAVAX` groups do not count as javax. Not XML: a regex on the raw text."""
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
    """pom에 명시된 최소 Java 버전을 반환한다.
    컴파일러 속성을 먼저 보고, 없으면 maven-compiler-plugin 설정을 본다. ``1.8``은 8로 읽는다. 없으면 None이다.

    Properties in order: `maven.compiler.release`, `.target`, `.source`, `java.version`; then the plugin's
    `<release>` / `<target>` / `<source>`."""
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
    """maven-metadata.xml에서 ``<versions>`` 목록을 읽는다.
    목록이 없으면 ``<release>`` / ``<latest>``를 쓴다."""
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
# 저장소 조회
# ---------------------------------------------------------------------------------------------------------------------
class RepositoryLookup:
    """저장소에서 릴리스 목록과 후보 pom을 조회하는 클래스.
    실행마다 하나를 만들고 조회 결과는 인스턴스에 캐시한다.
    테스트에서는 ``tests/conftest.py``가 오프라인 가짜 구현으로 바꾸므로 실제 저장소에 접속하지 않는다."""
    def __init__(self, cfg: Config, *, fetch: Optional[Fetch] = None, fetch_bytes: Optional[FetchBytes] = None,
                 project_root: Optional[Path] = None, status: Optional[Status] = None) -> None:
        self.cfg = cfg
        # 프로젝트 pom의 <repositories>도 조회 대상에 넣기 위해 루트를 받는다
        self.project_root = Path(project_root) if project_root else None
        build = getattr(cfg, "build", None)
        libs = getattr(cfg, "libraries", None)
        self.timeout = float(getattr(build, "repository_timeout_seconds", 10) or 10)
        self.include_prerelease = bool(getattr(libs, "include_prerelease", False))
        self.max_pom_checks = max(0, int(getattr(libs, "max_pom_checks", 6) or 0))
        self._fetch_fn: Fetch = fetch or self._urllib_fetch
        self._fetch_bytes_fn: FetchBytes = fetch_bytes or self._urllib_fetch_bytes   # jar 내려받기
        self._status_fn: Status = status or self._urllib_status                    # 특정 파일이 있는지만 확인
        self._jars: dict[tuple[str, str, str, Optional[str]], Optional[bytes]] = {}
        self._url_done = False
        self._url: Optional[str] = None
        self._creds: Optional[tuple[str, str]] = None
        self._last_error = ""
        self._versions: dict[tuple[str, str], tuple[list[str], str, str]] = {}   # (버전 목록, 출처, 오류)
        self._poms: dict[tuple[str, str, str], Optional[str]] = {}
        # 운영자가 Maven의 settings 미러와 비교할 수 있도록 pom을 받은 저장소 URL을 기록한다
        self._pom_sources: dict[tuple[str, str, str], str] = {}
        self._failures = 0                                        # 주 URL의 연속 타임아웃, 연결 실패 횟수
        self._dead = ""                                           # 값이 있으면 주 저장소를 다시 조회하지 않는다
        # 조회할 저장소 전체. 조회 순서대로 (url, 자격증명, 출처)
        self._sources: Optional[list[tuple[str, Optional[tuple[str, str]], str]]] = None
        self._url_failures: dict[str, int] = {}                   # URL별 연속 타임아웃, 연결 실패 횟수
        self._url_dead: dict[str, str] = {}                       # URL별로 이번 실행에서 다시 조회하지 않는 이유
        self._trace: dict[tuple[str, str], list[tuple[str, str]]] = {}   # (g, a)별 [(url, 결과), …]
        self._answered: dict[tuple[str, str], str] = {}           # (g, a)의 버전 목록을 준 URL
        self.settings_files: list[str] = []                       # 읽은 settings.xml 파일
        self.settings_missing: list[str] = []                     # 찾았지만 없거나 읽지 못한 settings 파일
        self.url_notes: list[str] = []                            # URL을 넣거나 뺀 이유

    # ------------------------------------------------------------------ HTTP 요청
    def _urllib_fetch(self, url: str, credentials: Optional[tuple[str, str]], timeout: float) -> Optional[str]:
        """URL에 GET 요청을 한 번 보낸다. 자격증명이 있으면 basic auth를 쓴다.
        오류가 나면 None을 반환하고, 후보 대부분은 원래 없으므로 404는 경고하지 않는다.

        `User-Agent: sbmig`; proxies from the environment (urllib). The failure text goes to `_last_error`."""
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
        except Exception as e:  # noqa: BLE001 - 잘못된 URL, OSError, http.client 오류
            self._last_error = f"{type(e).__name__}: {e}"[:120]
            log.debug("release lookup %s → %s", url, e)
        return None

    def _urllib_fetch_bytes(self, url: str, credentials: Optional[tuple[str, str]], timeout: float,
                            max_bytes: int) -> Optional[bytes]:
        """jar를 ``max_bytes``까지만 내려받는다. 더 크거나 오류가 나면 None을 반환한다."""
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
        except Exception as e:  # noqa: BLE001
            self._last_error = f"{type(e).__name__}: {e}"[:120]
        log.debug("jar download %s → %s", url, self._last_error)
        return None

    def jar_bytes(self, group_id: str, artifact_id: str, version: str, classifier: Optional[str] = None, *,
                  max_bytes: int = 20 * 1024 * 1024) -> Optional[bytes]:
        """후보 jar의 바이트를 저장소에서, 없으면 로컬 저장소에서 읽어 반환한다.
        크기를 넘거나 오류가 나면 None을 반환한다. 응답하지 않는 저장소는 건너뛴다.
        결과는 (g, a, v, classifier)마다 캐시한다."""
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
        """(g, a)의 버전 목록을 준 URL을 맨 앞에 둔 저장소 순서를 반환한다.
        pom과 jar도 그 저장소에 있을 가능성이 가장 높기 때문이다."""
        srcs = list(self.repository_urls())
        hit = self._answered.get((group_id, artifact_id))
        if hit:
            srcs.sort(key=lambda t: 0 if t[0] == hit else 1)
        return srcs

    def _fetch(self, url: str, credentials: Optional[tuple[str, str]] = None, base: Optional[str] = None
               ) -> Optional[str]:
        """URL에 GET 요청을 한 번 보낸다.
        시간 초과나 연결 실패가 ``UNREACHABLE_LIMIT``번 연속이면 이 실행에서 그 URL을 다시 조회하지 않는다.
        404, 401 같은 응답은 접속은 된다는 뜻이므로 연속 횟수를 0으로 되돌린다. 횟수는 URL마다 센다.

        Without the limit an air-gapped host without a mirror would wait `repository_timeout_seconds` for
        every library of every case."""
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
        except Exception as e:  # noqa: BLE001
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
            self._url_failures[base] = 0                          # 404, 401도 응답이므로 접속은 된다
            if base == self._url:
                self._failures = 0
        return None

    # ------------------------------------------------------------------ 조회할 저장소
    def repository_url(self) -> Optional[str]:
        """주 저장소 URL(끝에 ``/``)을 반환한다. ``repository_urls``의 첫 항목이고, 없으면 None을 반환한다."""
        if self._url_done:
            return self._url
        self._url_done = True
        srcs = self.repository_urls()
        self._url, self._creds = (srcs[0][0], srcs[0][1]) if srcs else (None, None)
        return self._url

    def repository_urls(self) -> list[tuple[str, Optional[tuple[str, str]], str]]:
        """조회할 저장소 전체를 Maven 규칙에 따라 (url, 자격증명, 이유) 목록으로 반환한다.
        - ``build.repository_url``을 지정했으면 맨 앞에 둔다. settings로 정한 주 URL과 다르면 WARNING을 한 번 남긴다
        - 그다음은 settings 활성 프로필 저장소, 프로젝트 pom 저장소, Central 순이다
        - 각 저장소는 그 저장소를 포함하는 첫 settings 미러의 URL로 바꾼다. 차단 미러에 걸린 저장소는 뺀다
        - offline이면 미러가 없는 Central은 뺀다
        실행마다 한 번 계산하고, 사용하는 URL과 이유를 INFO로 남긴다.

        Settings files: `build.settings_file`, else `~/.m2/settings.xml`, plus the global
        `$MAVEN_HOME/conf/settings.xml`. Active-profile repositories include pluginRepositories. Credentials
        of `build.repository_url`: `build.repository_server_id` first, else the `<server>` of a mirror /
        repository with the same URL."""
        if self._sources is not None:
            return self._sources
        out: list[tuple[str, Optional[tuple[str, str]], str]] = []
        notes: list[str] = []
        try:
            build = getattr(self.cfg, "build", None)
            offline = bool(getattr(build, "offline", False))
            # Maven이 사용자 설정을 먼저 두고 합치므로 사용자 settings를 전역 settings보다 앞에 둔다
            files: list[tuple[Path, str]] = []
            configured_settings = getattr(build, "settings_file", None)
            if configured_settings and str(configured_settings).strip():
                files.append((Path(str(configured_settings)).expanduser(), "build.settings_file"))
            else:
                # Maven은 `user.home`/.m2/settings.xml을 읽는다. Python의 홈은 %USERPROFILE%이라 PC에 따라 다를 수 있다
                # repo-probe가 찾은 경로를 출력하므로 둘이 다른 PC도 확인할 수 있다
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
            # (id, url, mirrorOf, creds, origin, blocked). Maven처럼 사용자 미러를 먼저 두고 같은 id의 전역 미러는 버린다
            # 회사 settings는 `maven-default-http-blocker` id를 Nexus 미러로 다시 쓰므로 전역 차단 미러가 다시 잡으면 안 된다
            # `blocked`는 id가 아니라 그 파일의 값이다. `!id`로 뺀 저장소는 Maven처럼 원래 URL을 쓴다
            mirrors = []
            mirror_ids: set[str] = set()
            for d, origin in datas:
                for mid, url, mo, creds in d.mirrors:
                    if mid and mid in mirror_ids:
                        continue
                    mirror_ids.add(mid)
                    mirrors.append((mid, url, mo, creds, origin, mid in d.blocked))
            servers: dict[str, Optional[tuple[str, str]]] = {}
            for d, _o in reversed(datas):             # 사용자 settings가 전역 settings를 덮어쓴다
                servers.update(d.servers)
            # 조회 순서대로 미러를 적용할 저장소 목록
            repos: list[tuple[str, str, Optional[tuple[str, str]], str]] = []
            for d, origin in datas:
                for rid, url, creds in d.repos:
                    repos.append((rid, url, creds, f"{origin} active-profile repository '{rid}'"))
                # Maven은 플러그인과 build extension을 pluginRepository에서 받는다. <repository>와 같은 URL은 한 번만 넣는다
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
                        # Maven 3.8.1 이상의 기본 http 차단 미러에 걸린 저장소는 Maven도 쓸 수 없으므로
                        # 0.0.0.0에 접속하는 대신 빠진 이유를 남긴다
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
                    if rid == "central" and any(not is_blocking_mirror(murl, mblocked) for _i, murl, _m, _c, _o, mblocked in mirrors):
                        # Central을 포함하는 미러가 없으면 sbmig는 Central을 직접 읽는다.
                        # Maven은 Central에서 실패하는 PC도 있으므로 이 점을 남긴다
                        notes.append("Maven Central is not covered by any settings mirror: sbmig reads it directly, which is fine ONLY if "
                                     "Maven can download from Central too — check with one build or `mvn dependency:get`; if Maven fails "
                                     "there (PKIX / proxy) either let Maven reach a host it can (mirrorOf central → that URL) or cover "
                                     "central with the Nexus mirror (mirrorOf *) so sbmig and Maven read one place")
                    out.append((url, creds, origin))
            configured = getattr(build, "repository_url", None)
            if configured and str(configured).strip():
                u = _normalize_url(str(configured))
                # build.repository_url로 차단된 Nexus를 다시 가리킬 때도 로그인이 필요하므로, 목록에서 빠진 저장소라도
                # URL이 같으면 그 <server> 자격증명을 쓴다
                sid = getattr(build, "repository_server_id", None)
                creds, creds_from = None, ""
                if sid:                                   # 운영자가 지정한 server id가 URL로 찾는 것보다 우선한다
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
            # 조용히 넘어가면 build.repository_url까지 빠지고 Central만 남으므로 메모와 로그에 남긴다
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
        """(g, a)의 maven-metadata.xml을 어느 URL에 조회해 무엇을 받았는지 목록으로 반환한다.
        조회한 뒤에 채워진다."""
        return list(self._trace.get((group_id, artifact_id), []))

    def remote_failures(self, group_id: str, artifact_id: str) -> str:
        """원격 저장소를 하나도 읽지 못했으면 ``remote repositories unreadable: …`` 문장을 반환한다.
        한 곳이라도 응답했거나 모두 "없음"이면 빈 문자열을 반환한다."""
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
        """로컬 저장소 디렉토리에 있는 버전 목록을 반환한다.
        maven-metadata*.xml과 pom이 들어 있는 하위 디렉토리 이름을 읽는다."""
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

    # ------------------------------------------------------------------ 버전과 pom 조회
    def _lookup(self, group_id: str, artifact_id: str) -> tuple[list[str], str, str]:
        """(g, a)의 (버전 목록, 출처, 오류)를 조회하고 캐시한다.
        각 URL의 maven-metadata.xml을 먼저 보고, 없으면 로컬 저장소를 본다.
        오류 문구는 프롬프트에 그대로 들어가므로 경우마다 다르게 쓴다."""
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
                # 404나 빈 응답은 저장소에 없다는 뜻이고 그 밖의 실패는 읽지 못한 것이다
                # 읽지 못한 저장소가 있으면 Nexus에 없다고 단정하지 않는다
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
        """저장소나 로컬 저장소에 있는 그 아티팩트의 모든 버전을 오래된 것부터 반환한다."""
        return list(self._lookup(group_id, artifact_id)[0])

    def pom_text(self, group_id: str, artifact_id: str, version: str) -> Optional[str]:
        """후보 버전의 pom 텍스트를 저장소 URL에서, 없으면 로컬 저장소에서 읽어 반환한다.
        어디에도 없으면 None을 반환한다. (g, a, v)별로 캐시한다."""
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

    def _urllib_status(self, url: str, credentials: Optional[tuple[str, str]], timeout: float,
                       method: str) -> tuple[Optional[int], str]:
        """URL에 요청을 한 번 보내고 (HTTP 상태 코드, 이유)를 반환한다. 응답이 없으면 상태 코드는 None이다.
        예외를 던지지 않는다.

        `GET` reads the body up to `MAX_FETCH_BYTES` and drops it; `RANGE` is a GET of the first byte only. An
        error status comes back as (code, "HTTP <code>")."""
        headers = {"User-Agent": "sbmig"}
        if credentials:
            token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        if method == "RANGE":
            headers["Range"] = "bytes=0-0"
        try:
            req = urllib.request.Request(url, headers=headers, method="HEAD" if method == "HEAD" else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if method != "HEAD":
                    r.read(1 if method == "RANGE" else MAX_FETCH_BYTES)
                return int(r.status), ""
        except urllib.error.HTTPError as e:
            return int(e.code), f"HTTP {e.code}"
        except TimeoutError:
            return None, f"timeout after {timeout:.0f}s"
        except urllib.error.URLError as e:
            reason = e.reason
            return None, (f"timeout after {timeout:.0f}s" if isinstance(reason, (socket.timeout, TimeoutError))
                          else f"unreachable ({reason})")
        except Exception as e:  # noqa: BLE001 - 잘못된 URL, OSError, http.client 오류
            return None, f"{type(e).__name__}: {e}"[:120]

    def file_answers(self, group_id: str, artifact_id: str, version: str, ext: str) -> list[tuple[str, str, str]]:
        """저장소마다 ``<a>-<v>.<ext>`` 파일을 받을 수 있는지 묻고 (url, 결과, 설명) 목록을 반환한다.
        결과는 ok, missing, denied, failed 중 하나이고, 받을 수 있는 저장소를 찾으면 거기서 멈춘다.
        jar는 내려받지 않고 HEAD로 확인한다.

        `ok` 200 / 206, `missing` 404 / 410, `denied` 401 / 403, `failed` any other status, timeout or refused
        connection. A pom is asked with GET (what Maven does; a pom is small); a jar with HEAD, and when a
        server refuses HEAD (405 / 501) with a GET of its first byte. Credentials come from the settings'
        `<server>` entries,
        never from the URL. Contract: v2.1z-ax."""
        out: list[tuple[str, str, str]] = []
        rel = f"{group_id.replace('.', '/')}/{artifact_id}/{version}/{artifact_id}-{version}.{ext}"
        try:
            for url, creds, _o in self.repository_urls():
                method = "GET" if ext == "pom" else "HEAD"
                code, why = self._status_fn(f"{url}{rel}", creds, self.timeout, method)
                if method == "HEAD" and code in (405, 501):
                    code, why = self._status_fn(f"{url}{rel}", creds, self.timeout, "RANGE")
                if code in (200, 206):
                    out.append((url, "ok", ""))
                    break
                if code in (404, 410):
                    out.append((url, "missing", why or f"HTTP {code}"))
                elif code in (401, 403):
                    out.append((url, "denied", why or f"HTTP {code}"))
                else:
                    out.append((url, "failed", why or (f"HTTP {code}" if code is not None else "no answer")))
        except Exception as e:  # noqa: BLE001
            log.debug("file_answers(%s:%s:%s %s): %s", group_id, artifact_id, version, ext, e)
        return out

    def pom_source(self, group_id: str, artifact_id: str, version: str) -> Optional[str]:
        """``pom_text``가 그 pom을 읽은 저장소 URL을 반환한다. 로컬이면 ``local repository``를 반환한다.
        읽지 않았거나 읽지 못했으면 None을 반환한다.
        sbmig와 Maven이 다른 URL을 조회했는지 로그에서 확인할 때 쓴다."""
        return self._pom_sources.get((group_id, artifact_id, version))

    def candidates(self, key: str, current: Optional[str]) -> RepositoryVersions:
        """``key``(groupId:artifactId)와 현재 버전으로 저장소의 후보 버전 정보를 만든다.
        같은 메이저의 새 릴리스, 상위 메이저별 최신 버전, jakarta 여부, 최소 Java 버전을 담는다.
        프리릴리스는 ``libraries.include_prerelease``일 때만 넣는다. 예외를 던지지 않고 실패는 ``error``에 남긴다.

        The pom-check order is in the module docstring."""
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
        """후보 버전의 pom을 읽어 jakarta 여부와 Java 버전을 확인한다.
        - 모두 합쳐 ``libraries.max_pom_checks``개까지만 본다
        - 같은 메이저는 최신부터 내려가다 javax 빌드를 만나면 멈춘다
        - 상위 메이저는 메이저마다 최신 하나를 본다
        - 남은 횟수가 있으면 jakarta인 상위 메이저에서 첫 jakarta 릴리스(``first_jakarta``)까지 내려간다"""
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

        # 같은 메이저는 최신부터 본다. javax 빌드가 나오면 그보다 오래된 것도 javax다
        for c in reversed(rv.same_major):
            if budget <= 0:
                break
            if check(c) is False:
                break
        # 상위 메이저는 높은 메이저부터 하나씩 본다
        for c in reversed(rv.higher):
            if budget <= 0:
                break
            check(c)
        # 남은 횟수로 jakarta인 상위 메이저의 첫 jakarta 릴리스를 찾는다
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
# 프롬프트 줄 만들기
# ---------------------------------------------------------------------------------------------------------------------
def _tag(c: VersionCandidate, first_jakarta: Optional[str] = None) -> str:
    """후보 하나를 '2.6.0 (jakarta ✓, Java 17)' 형태의 문구로 만든다.
    같은 메이저의 첫 jakarta 릴리스가 따로 있으면 덧붙인다."""
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
    """후보 목록을 표시 문구로 만든다. 최신 ``cap``개만 보이고 나머지는 '… (+n)'으로 줄인다."""
    shown = cands[-cap:]
    rest = len(cands) - len(shown)
    if first_jakarta in {c.version for c in shown}:
        first_jakarta = None                                  # 목록에 이미 jakarta ✓로 보이므로 따로 쓰지 않는다
    body = ", ".join(_tag(c, first_jakarta) for c in shown)
    return f"… (+{rest}), {body}" if rest > 0 else body


def hint_lines(rv: RepositoryVersions, entry: Any = None) -> list[str]:
    """라이브러리 하나에 대한 프롬프트 줄을 만든다. 표 항목 줄, 저장소 정보, 결론 한 줄 순서다.
    조회에 실패하면 같은 메이저에 머물라는 한 줄만 덧붙인다.
    프롬프트 줄 때문에 수정이 깨지면 안 되므로 예외를 던지지 않는다.

    Examples: `<key> — current 2.4.2; same major (2.x) in the repository, newer than current: 2.5.0 (javax),
    2.6.0 (jakarta ✓, Java 17)`; `<key> — higher majors: 3.x: 3.3.0 (jakarta ✓; first jakarta release
    3.2.0)`."""
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
            # 새 릴리스가 없다는 말이 Nexus에 없다는 뜻으로 읽히면 안 되므로 원격을 읽지 못한 사실을 남긴다
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
    except Exception as e:  # noqa: BLE001
        log.debug("hint_lines(%s): %s", getattr(rv, "key", "?"), e)
    return lines


def _verdict_line(rv: RepositoryVersions, entry: Any = None) -> str:
    """프롬프트 규칙 14에 따라 어떤 버전을 쓸지 결론을 한 줄로 만든다.
    모델이 표 줄과 저장소 줄을 직접 조합하지 않게 하기 위해서다.
    검사 순서는 ``allow_major`` -> 같은 메이저의 jakarta -> 상위 메이저의 jakarta -> jakarta 없음이다.
    현재 버전을 모르거나 저장소에 릴리스가 없으면 빈 문자열을 반환한다.

    * a person's `allow_major`: approved as given, `<verify>`;
    * a same-major jakarta build exists (repository ✓, or the table pins one on the current line): raise to
      it, no `<verify>`;
    * none on the current line but the table / the repository knows a jakarta release of a higher major: the
      major upgrade is allowed here, newest release of that major, port what changes, `<verify>` required;
    * releases but no jakarta build known on any line (usually a jakarta-native library whose pom declares no
      `jakarta.*` dependency, e.g. Spring itself): the API changed inside the same major, fix the code against
      the managed version and leave the version (never a `<no_script>` refusal)."""
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
            # pom에 jakarta를 선언하지 않는 jakarta 전용 라이브러리도 있으므로 거절하지 않고 API 변경으로 본다
            return (f"{rv.key} → verdict: no jakarta build is known on any line — the API changed inside the same "
                    f"major: fix the code against the version the parent chain / Boot BOM manages and leave the "
                    f"version")
        return ""
    except Exception as e:  # noqa: BLE001
        log.debug("_verdict_line(%s): %s", getattr(rv, "key", "?"), e)
        return ""
