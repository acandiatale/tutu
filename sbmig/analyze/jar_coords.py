"""Ant 프로젝트 lib 의 jar 하나 → Maven 좌표(v2.1j, 접수의 pom 합성이 쓴다). 흐름상 위치: 접수의
``_check_build_tool`` → :func:`sbmig.analyze.ant.synthesize_pom` 이 lib 의 jar 마다 :func:`resolve_jar` 를
부르고, 소스가 그 jar 의 패키지를 쓰는지 :func:`mark_used` 로 본다. 좌표 사다리: ① jar 안
``META-INF/maven/<g>/<a>/pom.properties`` ② 이름 표(``knowledge/libraries/jar_names.yml`` + 회사 표
``<kb>/jar_names.yml``, 버전은 파일 이름에서) ③ ``intake.jar_lookup`` 이면 sha1 저장소 조회(:class:`Sha1Lookup`:
Nexus 3 search API 또는 Central search) ④ MANIFEST 는 메모로만. 지킬 것: 예외 없음(못 찾으면 source=unknown +
메모), 조회 실패 2번 연속이면 그 실행에서 더 묻지 않음, 테스트는 네트워크에 나가지 않음(``tests/conftest.py``
가 ``RepositoryLookup`` 을 오프라인 대역으로 바꿈 — 여기는 그 클래스를 호출 때마다 모듈에서 찾는다).

Jar → Maven coordinates for the Ant pom synthesis (v2.1j, docs/INTERFACES.md "v2.1j").

`resolve_jar(path, data_or_file, cfg, lookup=None)` walks the ladder: ① the jar's own
`META-INF/maven/<g>/<a>/pom.properties`, ② the stem table (shipped `knowledge/libraries/jar_names.yml` + the
company rows `<kb>/jar_names.yml`, version from the file name), ③ when `intake.jar_lookup`, a sha1 lookup in the
repository `sbmig.build.repo_versions.RepositoryLookup` picks (`build.repository_url` → settings mirror → Central),
④ the MANIFEST (`Implementation-Title` / `Bundle-SymbolicName`) only as a note. `jar_packages` lists the top-level
packages of the jar's classes; `mark_used` sets `used` from the package references of the sources (Java imports
and qualified names, JSP page imports, class names in XML / properties). Servlet / JSP / EL APIs get scope
`provided`. Never raises.

Sha1 lookup assumptions: a `build.repository_url` / mirror of the form `<base>/repository/<name>/` is a Nexus 3 —
`GET <base>/service/rest/v1/search/assets?sha1=<hex>` (any other URL is used as the Nexus base as it is; Nexus 2 /
Artifactory answer 404 → a failure note); Maven Central → `https://search.maven.org/solrsearch/select?q=1:"<sha1>"`.
One base URL per run, like `RepositoryLookup`; two failures in a row → no more lookups in this run.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import posixpath
import re
import urllib.parse
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from sbmig.core.models import JarCoordinate
from sbmig.core.workspace import detect_encoding

from .webapp_inventory import JAR_NAMES_FILE, SKIP_DIRS, jar_names

log = logging.getLogger("sbmig.analyze.jar_coords")

DEFAULT_MAX_JAR_BYTES = 64 * 1024 * 1024
MAX_PACKAGES = 40                       # JarCoordinate.packages cap (display; the used check keeps them all)
MAX_REF_FILE_BYTES = 1_000_000           # a text file bigger than this is not scanned for references
LOOKUP_FAILURE_LIMIT = 2                 # sha1 lookups failing in a row → no more lookups in this run
CENTRAL_HOSTS = ("repo1.maven.org", "repo.maven.apache.org", "central.maven.org")
CENTRAL_SEARCH = "https://search.maven.org/solrsearch/select?q=1:%22{sha1}%22&rows=1&wt=json"
NEXUS_SEARCH = "{base}service/rest/v1/search/assets?sha1={sha1}"

# 컨테이너(Tomcat)가 주는 API — WAR 에 넣으면 안 되므로 provided. JSTL(javax.servlet:jstl, taglibs:standard)은 Tomcat
# 이 주지 않아 WAR 에 들어가야 하므로 여기 없다.
# container APIs Tomcat supplies → `provided` (JSTL is NOT one: Tomcat does not ship it, the WAR must carry it)
PROVIDED_KEYS = frozenset({
    "javax.servlet:servlet-api", "javax.servlet:javax.servlet-api", "jakarta.servlet:jakarta.servlet-api",
    "javax.servlet.jsp:jsp-api", "javax.servlet.jsp:javax.servlet.jsp-api", "jakarta.servlet.jsp:jakarta.servlet.jsp-api",
    "javax.el:el-api", "javax.el:javax.el-api", "jakarta.el:jakarta.el-api",
    "javax.websocket:javax.websocket-api", "jakarta.websocket:jakarta.websocket-api",
})
PROVIDED_GROUP_PREFIXES = ("org.apache.tomcat",)          # tomcat-servlet-api, tomcat-jsp-api, catalina, jasper, …
# packages of the container APIs (an unknown jar holding only these is a servlet / JSP / EL API copy)
CONTAINER_PACKAGES = ("javax.servlet", "javax.el", "javax.websocket", "jakarta.servlet", "jakarta.el",
                      "jakarta.websocket")
JSTL_PACKAGES = ("javax.servlet.jsp.jstl", "jakarta.servlet.jsp.jstl")
# 파일 이름의 버전 뒤에 붙어도 버전의 일부로 보는 단어(이외의 단어가 있으면 그 경계는 버전 시작이 아니다:
# log4j-1.2-api-2.17.1 → stem log4j-1.2-api)
# words that may follow a version in a file name (anything else means the version starts at a later boundary)
VERSION_WORDS = frozenset({
    "snapshot", "release", "final", "ga", "sp", "rc", "cr", "alpha", "beta", "milestone", "bin", "jre", "jdk",
    "java", "android", "incubating", "dev", "nightly", "ea", "pre", "preview", "redhat", "jboss", "osgi", "bundle",
    "patch", "hotfix", "fix", "sec", "jakarta", "javax", "all", "full", "nodeps", "shaded", "lite",
})
# 플러그인 표식: import 없이 쓰이는 jar(태그 라이브러리, ServiceLoader, Spring XML 네임스페이스, web-fragment)
# plug-in markers: jars used without any import (taglibs, ServiceLoader, Spring XML namespaces, web fragments)
PLUGIN_MARKERS = (("META-INF/services/", "META-INF/services"), ("META-INF/spring.handlers", "spring.handlers"),
                  ("META-INF/web-fragment.xml", "web-fragment.xml"))
REF_TEXT_EXT = frozenset({".jsp", ".jspf", ".jspx", ".tag", ".tagx", ".xml", ".properties", ".tld"})

_BOUNDARY_RE = re.compile(r"[-_](?=\d)")
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_DATE_SUFFIX_RE = re.compile(r"^(\d[\w.]*?)-\d{8}$")          # poi-3.9-20121203 → 3.9
# package part of a qualified name: `org.apache.commons.lang` in `import org.apache.commons.lang.StringUtils;`,
# `com.acme.util` in `class="com.acme.util.X"`, `java.util` in `import java.util.*` (lowercase segments, then a
# class name or `*`)
_REF_RE = re.compile(r"(?<![\w$.])([a-z_][\w$]*(?:\.[a-z_][\w$]*)+)\.(?:[A-Z][\w$]*|\*)")

JarData = bytes | bytearray | str | Path


# ------------------------------------------------------------------------------------------------ file names
def _bare(filename: str) -> str:
    """경로·``.jar`` 를 뗀 파일 이름."""
    name = posixpath.basename(str(filename).replace("\\", "/"))
    return name[:-4] if name.lower().endswith(".jar") else name


def _clean_version(ver: str) -> str:
    """파일 이름 버전의 군더더기를 뗀다: MySQL 의 ``-bin``, POI 의 ``-20121203`` 같은 날짜 꼬리."""
    v = ver[:-4] if ver.lower().endswith("-bin") else ver
    m = _DATE_SUFFIX_RE.match(v)
    return m.group(1) if m else v


def _version_ok(between: str) -> bool:
    """두 경계 사이 글자에 버전 단어가 아닌 낱말이 없으면 True (``1.2-api`` 는 False)."""
    return all(w.lower() in VERSION_WORDS for w in _WORD_RE.findall(between))


def stem_and_version(filename: str) -> tuple[str, str]:
    """jar 파일 이름 → (stem, 버전). ``commons-lang-2.6.jar`` → (commons-lang, 2.6), ``foo_1.2.3.jar`` →
    (foo, 1.2.3), ``foo-1.0-SNAPSHOT.jar`` → (foo, 1.0-SNAPSHOT), ``foo.jar`` → (foo, ""). 버전은 ``-`` / ``_``
    뒤 숫자로 시작하고, 그 뒤에 버전 단어가 아닌 낱말이 있으면 더 뒤의 경계를 쓴다(log4j-1.2-api-2.17.1 →
    log4j-1.2-api / 2.17.1). MySQL 의 ``-bin`` 은 뗀다.

    (stem, version) of a jar file name; version "" when the name carries none."""
    bare = _bare(filename)
    bounds = [m.start() for m in _BOUNDARY_RE.finditer(bare)]
    for i, b in enumerate(bounds):
        if b == 0:
            continue
        nxt = bounds[i + 1] if i + 1 < len(bounds) else None
        if nxt is not None and not _version_ok(bare[b + 1:nxt]):
            continue
        return bare[:b], _clean_version(bare[b + 1:])
    return bare, ""


def _table_hit(filename: str, table: dict[str, tuple[str, str]]) -> tuple[str, str, str] | None:
    """이름 표의 (groupId:artifactId, 버전, stem): 파일 이름 전체(``ojdbc6``) → 표가 아는 가장 긴 stem. 남는
    부분이 버전처럼 보이지 않는 stem 은 맞지 않은 것으로 본다(log4j-1.2-api-2.17.1 은 log4j 가 아니다).

    Table hit for a file name: the whole bare name first, then the longest known stem whose remainder is a
    version (`log4j-1.2-api-2.17.1` is not `log4j`)."""
    bare = _bare(filename)
    hit = table.get(bare.lower())
    if hit:
        return hit[0], "", bare
    for m in reversed(list(_BOUNDARY_RE.finditer(bare))):
        stem, ver = bare[:m.start()], bare[m.end():]
        hit = table.get(stem.lower()) if stem else None
        if hit and _version_ok(ver):
            return hit[0], _clean_version(ver), stem
    return None


# ------------------------------------------------------------------------------------------------ name tables
@lru_cache(maxsize=8)
def _company_rows(path: str, mtime_ns: int, size: int) -> dict[str, tuple[str, str]]:
    """회사 이름 표(배포 표와 같은 형식). (mtime_ns, size) 는 캐시 키일 뿐 — 고친 파일은 다시 읽힌다(500개 플릿이
    한 프로세스)."""
    return jar_names.__wrapped__(path)


def company_jar_names_file(cfg: Any) -> Path | None:
    """회사 jar 이름 표 경로: ``<kb dir>/<intake.company_jar_names_file>`` (절대 경로면 그것). 설정이 없으면 None.

    `<kb dir>/<intake.company_jar_names_file>` (an absolute path wins); None without a config."""
    intake = getattr(cfg, "intake", None)
    name = getattr(intake, "company_jar_names_file", None) if intake is not None else None
    if not name:
        return None
    p = Path(str(name)).expanduser()
    if p.is_absolute():
        return p
    kb = getattr(cfg, "kb_dir", None)
    try:
        kb = kb() if callable(kb) else kb
    except Exception:  # noqa: BLE001 - a config without kb: no company table
        kb = None
    return Path(kb) / p if kb else None


def jar_name_table(cfg: Any = None) -> dict[str, tuple[str, str]]:
    """배포 이름 표(``intake.jar_names_file``)에 회사 표(``<kb>/jar_names.yml``)를 덮어쓴 표 ``{소문자 stem:
    (groupId:artifactId, 메모)}``. 회사 표는 재배포 없이 고치고, 같은 stem 이면 회사 행이 이긴다.

    The shipped stem table merged with the company rows (`<kb>/jar_names.yml`, same format; a company row wins)."""
    intake = getattr(cfg, "intake", None) if cfg is not None else None
    shipped_name = getattr(intake, "jar_names_file", None) if intake is not None else None
    shipped_path = JAR_NAMES_FILE
    if shipped_name:
        resolve = getattr(cfg, "resolve_path", None)
        shipped_path = Path(resolve(shipped_name)) if callable(resolve) else Path(shipped_name)
    table = dict(jar_names(str(shipped_path)))
    company = company_jar_names_file(cfg) if cfg is not None else None
    if company is not None:
        try:
            st = company.stat()
        except OSError:
            st = None
        if st is not None and company.is_file():
            table.update(_company_rows(str(company), st.st_mtime_ns, st.st_size))
    return table


# ------------------------------------------------------------------------------------------------ jar contents
@dataclass
class JarScan:
    """jar 안을 훑은 결과: pom.properties 좌표들, 클래스의 전체 패키지, MANIFEST 항목, 플러그인 표식, 오류."""
    pom_properties: list[tuple[str, str, str]] = field(default_factory=list)
    packages: set[str] = field(default_factory=set)        # every package holding a class
    manifest: dict[str, str] = field(default_factory=dict)
    markers: list[str] = field(default_factory=list)       # tld / META-INF/services / spring.handlers / web-fragment
    scanned: bool = False
    error: str = ""


def _parse_properties(text: str) -> dict[str, str]:
    """pom.properties 정도의 단순한 .properties (``key=value`` / ``key: value``, ``#`` 주석)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#!":
            continue
        m = re.match(r"([^=:\s]+)\s*[=:\s]\s*(.*)$", s)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _parse_manifest(text: str) -> dict[str, str]:
    """MANIFEST.MF 의 주 섹션(빈 줄 전까지, 공백 하나로 시작하는 줄은 앞 줄의 이어짐)."""
    out: dict[str, str] = {}
    last = None
    for line in text.splitlines():
        if not line.strip():
            break
        if line.startswith(" ") and last:
            out[last] += line[1:]
            continue
        k, sep, v = line.partition(":")
        if sep:
            last = k.strip()
            out[last] = v.strip()
    return out


def scan_jar(data: bytes) -> JarScan:
    """jar 바이트를 훑는다(zipfile): ``META-INF/maven/*/*/pom.properties``, class 파일의 패키지(멀티 릴리스
    ``META-INF/versions/N/`` 와 Boot 의 ``BOOT-INF/classes/`` 접두어는 뗌), MANIFEST, 플러그인 표식. 깨진 zip 은
    ``error`` 만 채운다.

    Scan the jar bytes: pom.properties coordinates, class packages, manifest, plug-in markers; never raises."""
    js = JarScan()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        js.error = f"not a readable jar ({e})"
        return js
    with zf:
        js.scanned = True
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            low = name.lower()
            if name.startswith("META-INF/maven/") and name.endswith("/pom.properties"):
                try:
                    props = _parse_properties(zf.read(info).decode("utf-8", errors="replace"))
                except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
                    continue
                g, a, v = props.get("groupId", ""), props.get("artifactId", ""), props.get("version", "")
                if g and a and v and "${" not in g + a + v:
                    js.pom_properties.append((g, a, v))
                continue
            if low == "meta-inf/manifest.mf":
                try:
                    js.manifest = _parse_manifest(zf.read(info).decode("utf-8", errors="replace"))
                except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
                    pass
                continue
            if low.startswith("meta-inf/") and low.endswith(".tld"):
                if "taglib (META-INF/*.tld)" not in js.markers:
                    js.markers.append("taglib (META-INF/*.tld)")
                continue
            hit = next((label for prefix, label in PLUGIN_MARKERS if name.startswith(prefix)), None)
            if hit:
                if hit not in js.markers:
                    js.markers.append(hit)
                continue
            if not name.endswith(".class"):
                continue
            rel = name
            if rel.startswith("META-INF/versions/"):
                rel = rel.split("/", 3)[3] if rel.count("/") >= 3 else ""
            elif rel.startswith("BOOT-INF/classes/"):
                rel = rel[len("BOOT-INF/classes/"):]
            elif rel.startswith("META-INF/"):
                continue
            d = posixpath.dirname(rel)
            if d:
                js.packages.add(d.replace("/", "."))
    return js


def top_packages(packages: Iterable[str]) -> list[str]:
    """패키지 집합에서 조상이 집합에 없는 것만(정렬). ``org.apache.commons.lang`` + ``…lang.builder`` →
    ``org.apache.commons.lang``.

    The packages whose ancestors are not in the set (sorted)."""
    out: list[str] = []
    for p in sorted(set(packages)):
        if not any(p == q or p.startswith(q + ".") for q in out):
            out.append(p)
    return out


def jar_packages(data: bytes) -> list[str]:
    """jar 의 최상위 패키지들(예: ``org.apache.commons.lang``) — 인수인계 표와 ``used`` 판정에 쓴다.

    Top-level packages of the jar's classes (e.g. `org.apache.commons.lang`)."""
    return top_packages(scan_jar(bytes(data)).packages)


def is_container_api(packages: Iterable[str]) -> bool:
    """패키지가 전부 서블릿 / JSP / EL / WebSocket API 이면 True(JSTL 패키지가 있으면 False — JSTL 은 WAR 에 들어간다).

    True when every package is a container API (servlet / JSP / EL / WebSocket); JSTL is not one."""
    pk = [p for p in packages if p]
    if not pk:
        return False
    if any(p == j or p.startswith(j + ".") for p in pk for j in JSTL_PACKAGES):
        return False
    return all(any(p == c or p.startswith(c + ".") for c in CONTAINER_PACKAGES) for p in pk)


def provided_scope(group_id: str, artifact_id: str) -> bool:
    """컨테이너가 주는 API 좌표면 True(→ scope provided). JSTL 은 아니다."""
    key = f"{group_id}:{artifact_id}"
    return key in PROVIDED_KEYS or any(group_id == g or group_id.startswith(g + ".") for g in PROVIDED_GROUP_PREFIXES)


# ------------------------------------------------------------------------------------------------ sha1 lookup
def nexus_base(url: str) -> str:
    """저장소 URL 에서 Nexus 기본 URL: ``https://nexus/repository/maven-public/`` → ``https://nexus/`` (컨텍스트
    경로는 남김: ``https://h/nexus/repository/x/`` → ``https://h/nexus/``). ``/repository/<이름>`` 이 없으면 그대로.

    Nexus base of a repository URL (`<base>/repository/<name>/` → `<base>/`); any other URL unchanged."""
    u = (url or "").strip()
    u = u if u.endswith("/") else u + "/"
    m = re.match(r"^(.*?/)repository/[^/]+/(?:.*)?$", u)
    return m.group(1) if m else u


def is_central(url: str) -> bool:
    """Maven Central 주소인지(repo1.maven.org / repo.maven.apache.org)."""
    try:
        host = urllib.parse.urlparse(url or "").hostname or ""
    except ValueError:
        return False
    return host in CENTRAL_HOSTS


def sha1_search_url(repo_url: str, sha1: str) -> str:
    """sha1 조회 URL: Central 이면 search.maven.org solrsearch, 아니면 Nexus 3 search assets API.

    The sha1 search URL for a repository base: Central → search.maven.org, else Nexus 3 search assets."""
    if is_central(repo_url):
        return CENTRAL_SEARCH.format(sha1=sha1)
    return NEXUS_SEARCH.format(base=nexus_base(repo_url), sha1=sha1)


def _coords_from_path(path: str) -> tuple[str, str, str] | None:
    """저장소 경로 ``g/r/o/u/p/artifact/version/artifact-version.jar`` → (g, a, v)."""
    parts = [p for p in (path or "").strip("/").split("/") if p]
    if len(parts) < 4:
        return None
    v, a, fname = parts[-2], parts[-3], parts[-1]
    if not fname.startswith(f"{a}-"):
        return None
    return ".".join(parts[:-3]), a, v


def parse_nexus_assets(text: str) -> tuple[str, str, str] | None:
    """Nexus 3 ``/service/rest/v1/search/assets`` JSON 의 첫 jar 자산 → (g, a, v). 분류자(classifier)가 붙은
    자산은 건너뛴다. 없으면 None, JSON 이 아니면 ValueError.

    First classifier-less jar asset of a Nexus 3 search answer → (g, a, v); None when none; ValueError when the
    text is not JSON."""
    doc = json.loads(text)
    for it in (doc.get("items") or []) if isinstance(doc, dict) else []:
        if not isinstance(it, dict):
            continue
        m = it.get("maven2") if isinstance(it.get("maven2"), dict) else {}
        if m.get("classifier"):
            continue
        ext = m.get("extension") or posixpath.splitext(str(it.get("path") or ""))[1].lstrip(".")
        if ext and ext != "jar":
            continue
        g, a = m.get("groupId"), m.get("artifactId")
        v = m.get("baseVersion") or m.get("version")
        if g and a and v:
            return str(g), str(a), str(v)
        c = _coords_from_path(str(it.get("path") or ""))
        if c:
            return c
    return None


def parse_central_search(text: str) -> tuple[str, str, str] | None:
    """search.maven.org solrsearch JSON 의 첫 문서 → (g, a, v). 없으면 None, JSON 이 아니면 ValueError."""
    doc = json.loads(text)
    docs = ((doc.get("response") or {}).get("docs") or []) if isinstance(doc, dict) else []
    for d in docs:
        if isinstance(d, dict) and d.get("g") and d.get("a") and d.get("v"):
            return str(d["g"]), str(d["a"]), str(d["v"])
    return None


class Sha1Lookup:
    """sha1 → Maven 좌표 조회기(한 실행에 하나). 기본 URL·자격증명·HTTP GET 은 ``RepositoryLookup`` 의 것을 쓴다
    (``build.repository_url`` → settings 미러 → offline 이 아니면 Central). 실패(시간 초과·연결 실패·404 외의
    HTTP 오류·JSON 아님)가 ``LOOKUP_FAILURE_LIMIT`` 번 연속이면 더 묻지 않는다. 예외 없음: 못 찾으면 None 과
    ``note`` (이유 한 줄).

    sha1 → coordinates through the repository `RepositoryLookup` picks (its base URL, credentials and urllib GET).
    `find(sha1)` → (g, a, v) or None, `note` says why nothing was found. Two failures in a row → no more lookups.
    `fetch(url, credentials, timeout) -> text | None` and `repo` can be injected (tests)."""

    def __init__(self, cfg: Any, *, fetch: Any = None, repo: Any = None) -> None:
        self.cfg = cfg
        self._fetch_fn = fetch
        self._repo = repo
        self._repo_done = repo is not None
        self.failures = 0
        self.dead = ""
        self.note = ""
        self.asked = 0
        self._cache: dict[str, tuple[str, str, str] | None] = {}
        build = getattr(cfg, "build", None)
        self.timeout = float(getattr(build, "repository_timeout_seconds", 10) or 10)

    def _repository(self) -> Any:
        """``RepositoryLookup`` 인스턴스(호출 때 모듈에서 찾는다 — 테스트의 오프라인 대역이 먹도록)."""
        if not self._repo_done:
            self._repo_done = True
            try:
                from sbmig.build import repo_versions
                self._repo = repo_versions.RepositoryLookup(self.cfg)
            except Exception as e:  # noqa: BLE001 - no lookup, never an error
                log.debug("sha1 lookup: RepositoryLookup unavailable: %s", e)
                self._repo = None
        return self._repo

    def base_url(self) -> str | None:
        """조회할 저장소 URL(없으면 None: offline + 미러 없음, 또는 테스트의 오프라인 대역)."""
        repo = self._repository()
        try:
            return repo.repository_url() if repo is not None else None
        except Exception as e:  # noqa: BLE001
            log.debug("sha1 lookup: repository_url: %s", e)
            return None

    def _get(self, url: str) -> tuple[str | None, str]:
        """GET 한 번 → (텍스트, 오류). 주입한 fetch 가 없으면 ``RepositoryLookup._urllib_fetch`` (자격증명 포함)."""
        repo = self._repository()
        fetch = self._fetch_fn or getattr(repo, "_urllib_fetch", None)
        if fetch is None:
            return None, "no HTTP client (offline)"
        creds = getattr(repo, "_creds", None)
        try:
            text = fetch(url, creds, self.timeout)
        except Exception as e:  # noqa: BLE001 - an injected fetch never breaks the synthesis
            return None, (f"timeout after {self.timeout:.0f}s" if isinstance(e, TimeoutError)
                          else f"{type(e).__name__}: {e}")[:160]
        if text:
            return text, ""
        return None, str(getattr(repo, "_last_error", "") or "no answer")

    def find(self, sha1: str) -> tuple[str, str, str] | None:
        """sha1 의 (groupId, artifactId, version). 못 찾으면 None 과 ``note``. 같은 sha1 은 한 번만 묻는다.

        (g, a, v) of the jar with this sha1, or None (`note` says why)."""
        self.note = ""
        if not sha1:
            self.note = "no sha1"
            return None
        if sha1 in self._cache:
            hit = self._cache[sha1]
            self.note = "" if hit else "not found in the repository (cached answer)"
            return hit
        if self.dead:
            self.note = self.dead
            return None
        base = self.base_url()
        if not base:
            self.note = "sha1 lookup: no repository URL (offline)"
            return None
        url = sha1_search_url(base, sha1)
        self.asked += 1
        text, err = self._get(url)
        hit: tuple[str, str, str] | None = None
        failed = ""
        if text is None:
            # 404 는 "그 API 가 없다"(Nexus 2 / Artifactory) — 실패로 센다; Central / Nexus 는 '없음' 을 빈 목록으로 답한다
            failed = f"sha1 lookup {url} failed ({err})"
        else:
            try:
                hit = parse_central_search(text) if is_central(base) else parse_nexus_assets(text)
            except (ValueError, TypeError, AttributeError) as e:
                failed = f"sha1 lookup {url}: answer is not the expected JSON ({type(e).__name__})"
        if failed:
            self.failures += 1
            self.note = failed
            if self.failures >= LOOKUP_FAILURE_LIMIT:
                self.dead = (f"sha1 lookup skipped: {self.failures} failures in a row at {nexus_base(base)} "
                             f"(last: {err or 'bad answer'})")
                log.info("jar sha1 lookup: %d failures in a row (%s) — not asked again in this run",
                         self.failures, err or "bad answer")
            return None
        self.failures = 0
        self._cache[sha1] = hit
        if hit is None:
            self.note = f"sha1 not found in the repository ({nexus_base(base) if not is_central(base) else 'Central'})"
        return hit


# ------------------------------------------------------------------------------------------------ resolve
def _read(data: JarData, limit: int) -> tuple[bytes | None, str, int, str]:
    """jar 내용 → (바이트 또는 None(너무 큼), sha1, 크기, 오류). 파일 경로면 sha1 은 조각으로 읽어 셈."""
    if isinstance(data, (bytes, bytearray)):
        b = bytes(data)
        return (b if len(b) <= limit else None), hashlib.sha1(b).hexdigest(), len(b), ""
    p = Path(data)
    try:
        size = p.stat().st_size
        h = hashlib.sha1()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        body = p.read_bytes() if size <= limit else None
    except OSError as e:
        return None, "", 0, f"unreadable ({e})"
    return body, h.hexdigest(), size, ""


def _pick_pom_properties(found: list[tuple[str, str, str]], stem: str) -> tuple[str, str, str] | None:
    """pom.properties 가 여럿(셰이드 jar)이면 artifactId 가 파일 stem 과 같은 것, 아니면 파일 이름이 그
    artifactId 로 시작하는 것 중 가장 긴 것, 그것도 없으면 하나뿐일 때만 그것."""
    if not found:
        return None
    s = stem.lower()
    same = [c for c in found if c[1].lower() == s]
    if same:
        return same[0]
    prefix = sorted((c for c in found if s.startswith(c[1].lower())), key=lambda c: -len(c[1]))
    if prefix:
        return prefix[0]
    return found[0] if len(found) == 1 else None


def resolve_jar_scan(path: str, data: JarData, cfg: Any = None, lookup: Any = None, *,
                     table: dict[str, tuple[str, str]] | None = None) -> tuple[JarCoordinate, JarScan]:
    """:func:`resolve_jar` 과 같고 jar 를 훑은 :class:`JarScan` 도 돌려준다(pom 합성이 전체 패키지·플러그인
    표식을 쓴다). ``table`` 은 미리 합친 이름 표(jar 마다 다시 읽지 않게).

    `resolve_jar` plus the `JarScan` (full package set, plug-in markers) the synthesis needs."""
    intake = getattr(cfg, "intake", None) if cfg is not None else None
    limit = int(getattr(intake, "max_jar_bytes_scan", DEFAULT_MAX_JAR_BYTES) or DEFAULT_MAX_JAR_BYTES)
    body, sha1, size, err = _read(data, limit)
    jc = JarCoordinate(path=path, sha1=sha1)
    notes: list[str] = []
    js = JarScan()
    if err:
        notes.append(err)
    elif body is None:
        notes.append(f"not scanned ({size // (1024 * 1024)} MB > intake.max_jar_bytes_scan)")
    else:
        js = scan_jar(body)
        if js.error:
            notes.append(js.error)
    jc.packages = top_packages(js.packages)[:MAX_PACKAGES]
    stem, _file_version = stem_and_version(path)
    # ① jar 안의 pom.properties — jar 를 만든 Maven 빌드가 남긴 좌표라 가장 믿을 만하다
    picked = _pick_pom_properties(js.pom_properties, stem)
    if picked:
        jc.group_id, jc.artifact_id, jc.version = picked
        jc.source = "pom_properties"
        if len(js.pom_properties) > 1:
            notes.append(f"{len(js.pom_properties)} pom.properties inside (shaded jar): "
                         f"{picked[0]}:{picked[1]} taken")
    else:
        if len(js.pom_properties) > 1:
            notes.append(f"{len(js.pom_properties)} pom.properties inside (shaded jar), none matches the file name")
        # ② 이름 표(배포 + 회사) — 버전은 파일 이름에서만
        hit = _table_hit(path, table if table is not None else jar_name_table(cfg))
        if hit:
            g, _, a = hit[0].partition(":")
            jc.group_id, jc.artifact_id, jc.version, jc.source = g, a, hit[1], "name_table"
        # ③ sha1 조회 — 표에 없거나 파일 이름에 버전이 없을 때만
        if (jc.source == "unknown" or not jc.version) and getattr(intake, "jar_lookup", True) and sha1 \
                and lookup is not False:
            lk = lookup if lookup is not None else Sha1Lookup(cfg)
            try:
                found = lk.find(sha1)
            except Exception as e:  # noqa: BLE001 - an injected lookup never breaks the synthesis
                found = None
                notes.append(f"sha1 lookup failed ({type(e).__name__}: {e})"[:200])
            if found:
                jc.group_id, jc.artifact_id, jc.version = found
                jc.source = "sha1_lookup"
            else:
                why = str(getattr(lk, "note", "") or "")
                if why:
                    notes.append(why)
        if jc.source == "name_table" and not jc.version:
            notes.append("no version in the file name")
    # ④ MANIFEST — 좌표가 아니라 사람이 찾을 실마리(메모)
    if jc.source == "unknown" or not jc.version:
        mf = js.manifest
        bits = [f"{k}={mf[k]}" for k in ("Implementation-Title", "Implementation-Version", "Implementation-Vendor",
                                          "Bundle-SymbolicName", "Bundle-Version") if mf.get(k)]
        if bits:
            notes.append("manifest: " + ", ".join(bits))
    if jc.group_id and jc.artifact_id and provided_scope(jc.group_id, jc.artifact_id):
        jc.scope = "provided"
    if js.markers:
        notes.append("plug-in jar (" + ", ".join(js.markers) + ")")
    jc.note = "; ".join(n for n in notes if n)
    return jc, js


def resolve_jar(path: str, data: JarData, cfg: Any = None, lookup: Any = None) -> JarCoordinate:
    """jar 하나의 Maven 좌표(사다리: pom.properties → 이름 표 → sha1 조회 → MANIFEST 메모). ``data`` 는 jar 바이트
    또는 파일 경로, ``path`` 는 보고서에 쓸 저장소 상대 경로. ``lookup`` 은 ``find(sha1) -> (g, a, v) | None``
    (+ ``note``)을 가진 객체(None 이면 ``intake.jar_lookup`` 일 때 :class:`Sha1Lookup` 을 새로 만듦, False 면 조회
    안 함 — jar 가 많으면 하나를 만들어 넘겨야 실패 2번 규칙이 실행 전체에 걸린다). 예외 없음.

    Maven coordinates of one jar (ladder of docs/INTERFACES.md "v2.1j"). Never raises."""
    try:
        return resolve_jar_scan(path, data, cfg, lookup)[0]
    except Exception as e:  # one bad jar must not stop the synthesis (logged with the traceback)
        log.debug("resolve_jar(%s): %s", path, e, exc_info=True)
        return JarCoordinate(path=path, note=f"could not be resolved ({type(e).__name__}: {e})"[:200])


# ------------------------------------------------------------------------------------------------ used check
def _iter_files(root: Path, d: str, exts: frozenset[str]) -> Iterable[Path]:
    """``root/d`` 아래 확장자가 ``exts`` 인 파일(건너뛸 디렉토리 제외)."""
    base = root / d if d else root
    if not base.is_dir():
        return
    for p in sorted(base.rglob("*")):
        if p.suffix.lower() not in exts or not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(x in SKIP_DIRS for x in rel_parts):
            continue
        yield p


def references(ws: Any, dirs: Iterable[str], exts: Iterable[str] = (".java",)) -> set[str]:
    """``dirs`` 아래 파일들이 이름으로 가리키는 패키지 집합(import 줄, 코드 안 정규화된 이름, JSP page import,
    XML / properties 의 클래스 이름). 패키지 = 소문자 마디들 다음에 대문자 클래스 이름이나 ``*``.

    Packages the files below `dirs` name: Java imports and qualified names, JSP page imports, class names in XML
    / properties (lowercase segments followed by a class name or `*`)."""
    root = Path(ws.root)
    cands = tuple(getattr(ws, "encoding_candidates", None) or ())
    out: set[str] = set()
    seen: set[Path] = set()
    ext_set = frozenset(e.lower() for e in exts)
    for d in dict.fromkeys(dirs):
        for p in _iter_files(root, d, ext_set):
            if p in seen:
                continue
            seen.add(p)
            try:
                if p.stat().st_size > MAX_REF_FILE_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            enc = detect_encoding(p, cands or None) if not data.isascii() else "ascii"
            text = data.decode(enc if enc != "ascii" else "utf-8", errors="replace")
            out.update(m.group(1) for m in _REF_RE.finditer(text))
    return out


def _closure(refs: Iterable[str]) -> set[str]:
    """각 패키지와 그 조상 전부(``a.b.c`` → a, a.b, a.b.c)."""
    out: set[str] = set()
    for r in refs:
        parts = r.split(".")
        for i in range(1, len(parts) + 1):
            out.add(".".join(parts[:i]))
    return out


def apply_used(jars: Iterable[JarCoordinate], refs: Iterable[str]) -> None:
    """``refs`` (가리킨 패키지)로 jar 마다 ``used`` 를 정한다: 최상위 패키지 하나가 참조(또는 그 조상)와 같으면
    True, 패키지를 모르면(훑지 못한 jar) None 그대로.

    `used` = one of the jar's top-level packages is referenced (directly or as the parent of a reference); jars
    without known packages stay None."""
    closure = _closure(refs)
    for j in jars:
        if j.packages:
            j.used = any(p in closure for p in j.packages)


def mark_used(jars: list[JarCoordinate], source_roots: Iterable[str], ws: Any, *,
              extra_dirs: Iterable[str] = ()) -> list[JarCoordinate]:
    """소스 루트의 Java 파일(import·정규화된 이름)과 ``extra_dirs`` (웹 루트·리소스: JSP page import, XML /
    properties 의 클래스 이름)로 jar 마다 ``used`` 를 채운다. 같은 목록을 돌려준다.

    Set `used` on every jar from the Java sources of `source_roots` and the JSP / XML / properties files of
    `extra_dirs` (web root, resource dirs). Returns the same list."""
    refs = references(ws, list(source_roots), (".java",))
    extra = [d for d in extra_dirs if d is not None]
    if extra:
        refs |= references(ws, extra, REF_TEXT_EXT)
    apply_used(jars, refs)
    return jars
