"""javax.*를 쓰는 라이브러리의 jakarta 빌드가 저장소(회사 Nexus, 미러, 로컬 저장소)에 있는지 직접 찾는 모듈.

docs/INTERFACES.md "v2.1z-e".
Why: the LLM declines when the jakarta build sits under a higher major (``2.0.5-jakarta``) or another
artifactId (``grid8-jakarta``). The fix loop calls ``discover_jakarta_builds`` for the
``external_javax_api`` pre-try ``sbmig-jakarta`` and for the prompt's "Dependency versions" lines. Results
are cached per (g, a) for the run.

`discover_jakarta_builds(lookup, group, artifact, current, table_entry=…, cfg=…)` returns `JakartaDiscovery`:
the verified jakarta builds of a javax-era library in the configured repository, in this order and all
bounded: (a) `table`: the library table's `jakarta_key` (a different coordinate) / `classifier`; (b) `same`:
the same g:a's newer releases (`RepositoryLookup.candidates`: same major first, then the higher majors, e.g. a
vendor line `1.0.6 … 1.1.15` (javax) + `2.0.5-jakarta`); (c) `nexus-search`: Nexus 3 `service/rest/v1/search`
with the name / group STEM (trailing digits stripped) between wildcards, ≤ 3 pages per query; (d)
`name-probe`: `maven-metadata.xml` of sibling names (`{a}-jakarta`, `{a}_jakarta`, `jakarta-{a}`,
`{a}-ee9|ee10`, and the trailing digits of the artifactId / the group's last segment raised by one and two:
`grid8` to `grid9`, `com.example.grid8:grid8` to `com.example.grid9:grid9`), ≤ `libraries.max_jakarta_probes`.
Verification per candidate: a `jakarta` version qualifier or classifier → verified ("version qualifier"); else
its pom (`pom_is_jakarta`); else a bounded **jar scan** (`libraries.max_jar_scan_mb`, ≤
`libraries.max_jar_scans` downloads): the class files' constant pools referencing `jakarta/<ee>/` vs
`javax/<ee>/`. Unverified candidates are reported, never applied. The repository's reachability rules of
`RepositoryLookup` apply (unreachable twice → not asked again this run). Never raises. """
from __future__ import annotations

import io
import json
import logging
import re
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

from .depchanges import major_of
from .repo_versions import _vkey, is_prerelease, pom_is_jakarta

log = logging.getLogger("sbmig.build.jakarta_discovery")

SOURCE_ORDER = ("table", "same", "nexus-search", "name-probe")
DEFAULT_MAX_PROBES = 12
DEFAULT_MAX_JAR_SCANS = 2
DEFAULT_MAX_JAR_SCAN_MB = 20
NEXUS_PAGES = 3                           # Nexus 검색 한 번에 넘겨 보는 continuationToken 페이지 수
NEXUS_MAX_PAIRS = 10                      # 검색 결과에서 가져올 (group, name) 쌍 수
MAX_CLASS_ENTRIES = 4000                  # jar 검사 한 번에 보는 class 파일 수
STEM_MIN_CHARS = 3                        # 이보다 짧은 이름(`a`, `ui`)은 검색하지 않는다. Nexus의 거의 모든 것에 맞는다
# jar 검사가 세는 EE 루트 패키지. jakarta로 옮겨진 패키지들이다. javax.annotation.processing은 JDK 패키지라 뺀다
_EE_ROOTS = ("servlet", "persistence", "validation", "annotation", "inject", "transaction", "ws/rs", "jms", "mail",
             "websocket", "el", "faces", "xml/bind", "enterprise", "ejb", "activation", "json", "batch", "resource",
             "interceptor", "decorator", "security/enterprise", "xml/ws", "xml/soap", "jws")
_JAKARTA_REFS = tuple(f"jakarta/{r}/".encode() for r in _EE_ROOTS)
_JAVAX_REFS = tuple(f"javax/{r}/".encode() for r in _EE_ROOTS)
_JAVAX_NOT_EE = (b"javax/annotation/processing/",)
_DIGITS_RE = re.compile(r"^(?P<stem>.*?)(?P<sep>[-_.]?)(?P<n>\d+)$")
_NEXUS_RE = re.compile(r"^(?P<base>https?://.+?)/repository/(?P<repo>[^/]+)/?$")
_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+")


@dataclass
class JakartaBuild:
    """찾은 jakarta 빌드 후보 하나를 담는 클래스.
    jakarta로 확인됐는지(``verified``)와 그 근거(``how``), 상위 메이저인지(``major_change``)를 함께 담는다.
    상위 메이저 후보는 운영자가 확인해야 한다.

    `source`: table / same / nexus-search / name-probe."""
    group: str
    artifact: str
    version: str
    classifier: Optional[str] = None
    source: str = "same"
    verified: bool = False
    how: str = ""
    major_change: bool = False

    @property
    def key(self) -> str:
        """``groupId:artifactId``를 반환한다."""
        return f"{self.group}:{self.artifact}"

    @property
    def javax(self) -> bool:
        """확인해 보니 javax 빌드였던 후보인지 반환한다. 이런 후보는 jakarta 후보로 보여 주지 않는다."""
        return self.how in ("pom declares javax.*", "jar scan: javax")

    @property
    def coordinates(self) -> str:
        """``g:a:v``를 반환한다. classifier가 있으면 ``:classifier``를 붙인다."""
        return f"{self.key}:{self.version}" + (f":{self.classifier}" if self.classifier else "")

    def text(self) -> str:
        """프롬프트와 인수인계에 넣을 짧은 설명을 만든다. 좌표, 버전, jakarta 확인 근거, 찾은 경로를 담는다."""
        mark = "jakarta ✓" if self.verified else "jakarta unverified"
        where = {"same": "same coordinates", "table": "the library table", "nexus-search": "Nexus search",
                 "name-probe": "name probe"}.get(self.source, self.source)
        tail = f"; found by {where}" + (", a higher major" if self.major_change else "")
        cls = f" classifier {self.classifier}" if self.classifier else ""
        return f"{self.key} {self.version}{cls} ({mark} — {self.how or 'no evidence'}{tail})"


@dataclass
class JakartaDiscovery:
    """라이브러리 하나의 탐색 결과를 담는 클래스.
    찾은 후보(``builds``), 조회 기록(``searched``), 찾아본 이름(``probed``)을 담는다.
    저장소를 전혀 쓸 수 없었으면 ``error``에 이유가 들어간다."""
    key: str
    current: Optional[str] = None
    builds: list[JakartaBuild] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    probed: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def verified(self) -> list[JakartaBuild]:
        """jakarta 빌드로 확인된 후보 목록을 반환한다. 적용할 수 있는 후보들이다."""
        return [b for b in self.builds if b.verified]

    def best(self) -> Optional[JakartaBuild]:
        """확인된 후보 중에서 사전 시도가 적용할 빌드 하나를 고른다. 확인된 후보가 없으면 None을 반환한다.
        우선순위는 라이브러리 표, 같은 메이저의 최신, 가장 낮은 상위 메이저의 최신, Nexus 검색, 이름 탐색 순이다."""
        ok = self.verified
        if not ok:
            return None
        table = [b for b in ok if b.source == "table"]
        if table:
            return table[0]
        same = [b for b in ok if b.source == "same" and not b.major_change]
        if same:
            return max(same, key=lambda b: _vkey(b.version))
        higher = [b for b in ok if b.source == "same" and b.major_change]
        if higher:
            lowest = min((major_of(b.version) or 0) for b in higher)
            return max((b for b in higher if (major_of(b.version) or 0) == lowest), key=lambda b: _vkey(b.version))
        for src in ("nexus-search", "name-probe"):
            for b in ok:
                if b.source == src:
                    return b
        return ok[0]

    def searched_text(self) -> str:
        """인수인계와 힌트에 쓸 조회 기록 한 문장을 만든다. 무엇을 어디서 찾았고 결과가 어땠는지 담는다."""
        parts = list(self.searched)
        if self.probed:
            shown = ", ".join(self.probed[:8]) + (f", … (+{len(self.probed) - 8})" if len(self.probed) > 8 else "")
            found = [b for b in self.builds if b.source == "name-probe"]
            parts.append(f"probed {len(self.probed)} name(s) ({shown}): "
                         + (", ".join(b.coordinates for b in found) if found else "none found"))
        if self.error and not parts:
            parts.append(self.error)
        return "; ".join(parts) or "nothing searched"

    def hint_lines(self) -> list[str]:
        """프롬프트 "Dependency versions" 절에 넣을 줄을 만든다.
        후보마다 한 줄이다. 확인되지 않은 후보에는 자동으로 적용하지 않는다는 표시를 붙인다.
        javax 빌드로 확인된 후보는 넣지 않는다. 후보가 없으면 조회 내용을 한 줄로 남긴다."""
        out: list[str] = []
        for b in self.builds:
            if b.javax:
                continue                                   # javax 빌드는 후보가 아니다
            if b.verified:
                out.append(f"repository also has {b.text()} — a jakarta build of {self.key}: switch the declaration "
                           f"to it instead of working around the javax API")
            else:
                out.append(f"repository also lists {b.text()} — not verified as a jakarta build, not applied "
                           f"automatically")
        if not out and not self.off:
            out.append(f"no jakarta build of {self.key} was found in the repository ({self.searched_text()})")
        return out

    @property
    def off(self) -> bool:
        """설정(``libraries.discover_jakarta_builds: false``)으로 탐색을 껐으면 True를 반환한다.
        이때는 jakarta 빌드가 없다고 말하지 않는다."""
        return any(x.startswith("discovery off (") for x in self.searched)


# ---------------------------------------------------------------------------------------------------------------------
# 이름 규칙
# ---------------------------------------------------------------------------------------------------------------------
def stem(name: str) -> str:
    """이름 끝의 숫자와 그 앞 구분자를 떼어 반환한다. 예를 들어 ``grid8``은 ``grid``가 된다."""
    m = _DIGITS_RE.match(name or "")
    return (m.group("stem") or name) if m else (name or "")


def digit_variants(name: str, steps: int = 2) -> list[str]:
    """이름 끝의 숫자를 1부터 ``steps``까지 올린 이름 목록을 만든다. 숫자로 끝나지 않으면 빈 목록을 반환한다."""
    m = _DIGITS_RE.match(name or "")
    if not m:
        return []
    n = int(m.group("n"))
    return [f"{m.group('stem')}{m.group('sep')}{n + k}" for k in range(1, steps + 1)]


def sibling_names(group: str, artifact: str, limit: int = DEFAULT_MAX_PROBES) -> list[tuple[str, str]]:
    """탐색할 형제 좌표를 가능성이 높은 순으로 ``limit``개까지 만든다.
    ``-jakarta``를 붙인 이름, 숫자를 올린 이름, ``ee9``와 ``ee10`` 이름 순이다.
    groupId만 올린 좌표(``com.vendor9:vendor8``)는 만들지 않는다.

    Digits are raised on the artifactId AND the group's last segment together first, then on the artifactId
    alone."""
    out: list[tuple[str, str]] = []

    def add(g: str, a: str) -> None:
        if (g, a) != (group, artifact) and (g, a) not in out and g and a:
            out.append((g, a))

    add(group, f"{artifact}-jakarta")
    a_up = digit_variants(artifact)
    head, _, last = group.rpartition(".")
    g_up = [f"{head}.{x}" if head else x for x in digit_variants(last)]
    for a2, g2 in zip(a_up, g_up):                    # 둘 다 올린다. com.example.grid9:grid9, 다음은 grid10
        add(g2, a2)
    for a2 in a_up:                                   # artifactId만 올린다. com.example.grid8:grid9
        add(group, a2)
    add(group, f"{artifact}_jakarta")
    add(group, f"{artifact}.jakarta")
    add(group, f"jakarta-{artifact}")
    add(group, f"{artifact}-jakarta-ee")
    add(group, f"{artifact}-ee10")
    add(group, f"{artifact}-ee9")
    for a2, g2 in zip(a_up, g_up):                    # 올린 좌표에 -jakarta를 붙인 이름은 마지막에 본다
        add(g2, f"{a2}-jakarta")
    return out[: max(0, int(limit))]


def artifact_names_jakarta(artifact_id: str) -> bool:
    """artifactId의 토큰에 ``jakarta``가 있는지 반환한다.
    이름에 jakarta가 있으면(``dext5-jakarta``) 공급사가 표시한 빌드로 보고, 그것만으로 확인된 것으로 친다."""
    return any(t.lower() == "jakarta" for t in re.split(r"[-_.]", artifact_id or "") if t)


def qualifier_is_jakarta(version: str) -> bool:
    """버전에 ``jakarta`` 토큰이 있는지(``2.0.5-jakarta``) 반환한다. 그것만으로 jakarta 빌드로 확인한다."""
    return any(t.lower() == "jakarta" for t in _TOKEN_RE.findall(version or ""))


# ---------------------------------------------------------------------------------------------------------------------
# jar 검사
# ---------------------------------------------------------------------------------------------------------------------
def scan_jar(data: bytes, max_classes: int = MAX_CLASS_ENTRIES) -> Optional[bool]:
    """jar가 jakarta 빌드인지 class 파일을 보고 판단한다.
    ``jakarta/<ee>/``를 참조하면 True, ``javax/<ee>/``만 참조하면 False다. 둘 다 없거나 zip이 깨졌으면 None이다.
    상수 풀을 파싱하지 않고 바이트를 검색한다. JDK의 ``javax.annotation.processing``은 세지 않는다."""
    jakarta = javax = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            n = 0
            for info in zf.infolist():
                if not info.filename.endswith(".class") or info.is_dir():
                    continue
                n += 1
                if n > max_classes:
                    break
                try:
                    blob = zf.read(info)
                except Exception:  # noqa: BLE001 - 읽지 못한 항목은 건너뛴다
                    continue
                if any(r in blob for r in _JAKARTA_REFS):
                    jakarta += 1
                cleaned = blob
                for skip in _JAVAX_NOT_EE:
                    cleaned = cleaned.replace(skip, b"")
                if any(r in cleaned for r in _JAVAX_REFS):
                    javax += 1
    except Exception as e:  # noqa: BLE001 - zip이 아니거나 덜 받은 파일
        log.debug("jar scan: %s: %s", type(e).__name__, e)
        return None
    if jakarta and not javax:
        return True
    if javax and not jakarta:
        return False
    return True if jakarta else None


# ---------------------------------------------------------------------------------------------------------------------
# 탐색
# ---------------------------------------------------------------------------------------------------------------------
def _cfg_int(cfg: Any, name: str, default: int) -> int:
    try:
        return max(0, int(getattr(getattr(cfg, "libraries", None), name, default)))
    except (TypeError, ValueError):
        return default


def _cfg_bool(cfg: Any, name: str, default: bool) -> bool:
    return bool(getattr(getattr(cfg, "libraries", None), name, default))


def _newest_release(versions: list[str], include_prerelease: bool = False) -> Optional[str]:
    vs = [v for v in versions or [] if v and (include_prerelease or not is_prerelease(v))]
    return max(vs, key=_vkey) if vs else None


class _Verifier:
    """후보 하나가 jakarta 빌드인지 확인하는 클래스.
    조회 횟수 상한 안에서 버전 한정어, pom, jar 검사 순으로 확인한다."""

    def __init__(self, lookup: Any, cfg: Any) -> None:
        self.lookup = lookup
        self.scans_left = _cfg_int(cfg, "max_jar_scans", DEFAULT_MAX_JAR_SCANS)
        self.max_bytes = _cfg_int(cfg, "max_jar_scan_mb", DEFAULT_MAX_JAR_SCAN_MB) * 1024 * 1024
        self.scanned: dict[tuple[str, str, str, Optional[str]], Optional[bool]] = {}

    def verify(self, g: str, a: str, v: str, classifier: Optional[str], pom_hint: Optional[bool] = None
               ) -> tuple[bool, str]:
        if (classifier or "").lower() == "jakarta":
            return True, "jakarta classifier"
        if qualifier_is_jakarta(v):
            return True, "version qualifier"
        if artifact_names_jakarta(a):
            # artifactId에 jakarta가 붙은 빌드(`dext5-jakarta:21.1`)는 공급사가 표시한 것이다. pom, jar는 더 보지 않는다
            return True, "artifactId names jakarta"
        pj = pom_hint
        if pj is None:
            try:
                text = self.lookup.pom_text(g, a, v)
            except Exception:  # noqa: BLE001
                text = None
            pj = pom_is_jakarta(text) if text else None
        if pj is True:
            return True, "pom declares jakarta.*"
        if pj is False:
            return False, "pom declares javax.*"
        key = (g, a, v, classifier)
        if key not in self.scanned:
            verdict: Optional[bool] = None
            if self.scans_left > 0 and hasattr(self.lookup, "jar_bytes"):
                self.scans_left -= 1
                try:
                    data = self.lookup.jar_bytes(g, a, v, classifier, max_bytes=self.max_bytes)
                except Exception as e:  # noqa: BLE001
                    log.debug("jar_bytes(%s:%s:%s): %s", g, a, v, e)
                    data = None
                verdict = scan_jar(data) if data else None
                self.scanned[key] = verdict
            else:
                self.scanned[key] = None
                return False, "pom silent, jar not scanned"
        verdict = self.scanned[key]
        if verdict is True:
            return True, "jar scan ✓"
        if verdict is False:
            return False, "jar scan: javax"
        return False, "pom silent, jar inconclusive"


def _nexus_search(lookup: Any, group: str, artifact: str, d: JakartaDiscovery) -> list[tuple[str, str]]:
    """Nexus 3 검색 API로 이름이 비슷한 라이브러리를 찾는다. 끝 숫자를 뗀 이름이나 groupId로 검색한다.
    현재 좌표는 빼고 최대 NEXUS_MAX_PAIRS개를 반환한다. 조회한 내용은 ``d.searched``에 남긴다."""
    urls: list[str] = []
    try:
        if hasattr(lookup, "repository_urls"):
            urls = [u for u, _c, _o in lookup.repository_urls()]
        else:
            u = lookup.repository_url()
            urls = [u] if u else []
    except Exception:  # noqa: BLE001
        urls = []
    bases: list[tuple[str, str]] = []
    hosts: set[str] = set()
    for u in urls:
        m = _NEXUS_RE.match(str(u).rstrip("/") + "/")
        if m is None:
            continue
        host = urllib.parse.urlsplit(m.group("base")).netloc
        if host in hosts:
            continue
        hosts.add(host)
        bases.append((m.group("base"), m.group("repo")))
    if not bases:
        d.searched.append("Nexus search: no Nexus 3 repository URL (skipped)")
        return []
    st, gst = stem(artifact), stem(group.rpartition(".")[2] or group)
    queries: list[tuple[str, str]] = []
    if len(st) >= STEM_MIN_CHARS:
        queries.append(("name", f"*{st}*"))
    if len(gst) >= STEM_MIN_CHARS and gst != st:
        queries.append(("group", f"*{gst}*"))
    if not queries:
        d.searched.append(f"Nexus search: stem of {artifact} too short (skipped)")
        return []
    pairs: list[tuple[str, str]] = []
    answered: list[str] = []
    for base, repo in bases:
        base_url = next((u for u in urls if u.startswith(base)), base)
        for field_name, pattern in queries:
            token: Optional[str] = None
            for _page in range(NEXUS_PAGES):
                q = {"repository": repo, field_name: pattern}
                if token:
                    q["continuationToken"] = token
                search = f"{base}/service/rest/v1/search?{urllib.parse.urlencode(q)}"
                try:
                    text = lookup._fetch(search, None, base_url) if hasattr(lookup, "repository_urls") \
                        else lookup._fetch(search)
                except Exception as e:  # noqa: BLE001
                    log.debug("nexus search %s: %s", search, e)
                    text = None
                if not text:
                    break
                try:
                    data = json.loads(text)
                except ValueError:
                    break
                if base not in answered:
                    answered.append(base)
                for item in data.get("items") or []:
                    g2, a2 = str(item.get("group") or ""), str(item.get("name") or "")
                    if not g2 or not a2 or (g2, a2) == (group, artifact) or (g2, a2) in pairs:
                        continue
                    if st.lower() not in a2.lower() and stem(a2).lower() != st.lower():
                        continue                  # 검색 결과가 넓다. 앞부분이 같은 이름만 남긴다
                    pairs.append((g2, a2))
                    if len(pairs) >= NEXUS_MAX_PAIRS:
                        break
                token = data.get("continuationToken") or None
                if not token or len(pairs) >= NEXUS_MAX_PAIRS:
                    break
    what = " / ".join(f"{f}={p}" for f, p in queries)
    if not answered:
        d.searched.append(f"Nexus search {what}: no answer from " + ", ".join(b + "/service/rest/v1/search" for b, _r in bases))
    else:
        d.searched.append(f"Nexus search {what} at {', '.join(answered)}: "
                          + (", ".join(f"{g}:{a}" for g, a in pairs) if pairs else "no other artifact with that stem"))
    return pairs


def discover_jakarta_builds(lookup: Any, group: str, artifact: str, current: Optional[str], *,
                            table_entry: Any = None, cfg: Any = None, rv: Any = None) -> JakartaDiscovery:
    """``group:artifact``의 jakarta 빌드를 찾는다.
    결과는 ``lookup``에 (g, a, current)별로 캐시한다. 예외를 던지지 않고 실패는 ``error``에 남긴다.
    ``rv``를 넘기면 저장소 후보를 다시 조회하지 않는다.

    Source order and verification: module docstring."""
    key = f"{group}:{artifact}"
    cur = current if current and "${" not in current else None
    cache = getattr(lookup, "_jakarta_cache", None)
    if cache is None:
        try:
            cache = lookup._jakarta_cache = {}
        except Exception:  # noqa: BLE001 - 속성을 받지 않는 lookup이면 캐시 없이 간다
            cache = {}
    ck = (group, artifact, cur)
    if ck in cache:
        return cache[ck]
    d = JakartaDiscovery(key=key, current=cur)
    try:
        _discover(lookup, group, artifact, cur, d, table_entry, cfg, rv)
    except Exception as e:  # noqa: BLE001 - 탐색은 보조 기능이라 케이스를 실패시키지 않는다
        log.debug("jakarta discovery %s: %s: %s", key, type(e).__name__, e)
        d.error = d.error or f"{type(e).__name__}: {e}"[:120]
    cache[ck] = d
    if d.verified:
        log.info("jakarta 빌드 탐색 %s: %s", key, "; ".join(b.text() for b in d.verified[:3]))
    else:
        log.info("jakarta 빌드 탐색 %s: 없음 (%s)", key, d.searched_text()[:300])
    return d


def _discover(lookup: Any, group: str, artifact: str, cur: Optional[str], d: JakartaDiscovery,
              entry: Any, cfg: Any, rv: Any = None) -> None:
    if lookup is None:
        d.error = "no repository lookup"
        return
    if not _cfg_bool(cfg, "discover_jakarta_builds", True):
        # 문제가 생길 때 끄는 스위치. 꺼지면 확인, 형제 탐색, 사전 시도, 거절 반박을 하지 않는다
        d.searched.append("discovery off (libraries.discover_jakarta_builds: false)")
        return
    ver = _Verifier(lookup, cfg)
    include_pre = bool(getattr(lookup, "include_prerelease", False))
    cur_major = major_of(cur) if cur else None
    seen: set[tuple[str, str, str, Optional[str]]] = set()

    def add(g: str, a: str, v: str, classifier: Optional[str], source: str, pom_hint: Optional[bool] = None) -> None:
        k = (g, a, v, classifier)
        if k in seen:
            return
        seen.add(k)
        if source == "table":
            ok, how = True, "library table (a person's entry)"     # 사람이 만든 표라서 따로 확인하지 않는다
        else:
            ok, how = ver.verify(g, a, v, classifier, pom_hint)
        major_change = (g, a) == (group, artifact) and cur_major is not None and (major_of(v) or 0) > cur_major
        d.builds.append(JakartaBuild(group=g, artifact=a, version=v, classifier=classifier, source=source,
                                     verified=ok, how=how, major_change=major_change))

    # (a) 라이브러리 표: 다른 좌표(`jakarta_key`)나 같은 좌표의 classifier jar
    jk = str(getattr(entry, "jakarta_key", "") or "") if entry is not None else ""
    if jk and jk.count(":") == 1 and jk != f"{group}:{artifact}":
        tg, ta = jk.split(":")
        try:
            vs = list(lookup.versions(tg, ta) or [])
        except Exception:  # noqa: BLE001
            vs = []
        v = _newest_release(vs, include_pre) or str(getattr(entry, "version", "") or "")
        if v:
            add(tg, ta, v, getattr(entry, "classifier", None), "table",
                pom_hint=True if not vs else None)      # 저장소를 읽지 못하면 표의 정보를 믿는다
            d.searched.append(f"library table: {jk} {v}" + ("" if vs else " (repository unreadable; the table's version)"))
        else:
            d.searched.append(f"library table: {jk} lists no release")
    elif entry is not None and getattr(entry, "classifier", None) and getattr(entry, "version", None):
        add(group, artifact, str(entry.version), str(entry.classifier), "table", pom_hint=True)
        d.searched.append(f"library table: classifier {entry.classifier} at {entry.version}")

    # (b) 같은 좌표: 같은 메이저의 새 릴리스, 다음은 상위 메이저. 호출자가 rv를 주면 다시 조회하지 않는다
    if rv is None:
        try:
            rv = lookup.candidates(f"{group}:{artifact}", cur)
        except Exception as e:  # noqa: BLE001
            rv = None
            d.error = f"{type(e).__name__}: {e}"[:120]
    try:
        remote = lookup.remote_failures(group, artifact) if hasattr(lookup, "remote_failures") else ""
    except Exception:  # noqa: BLE001
        remote = ""
    if remote:
        d.searched.append(remote + "; local repository only")
    if rv is not None and (rv.same_major or rv.higher):
        checked = 0
        for c in reversed(list(rv.same_major)):
            add(group, artifact, c.version, None, "same", pom_hint=c.jakarta)
            checked += 1
        for c in reversed(list(rv.higher)):
            add(group, artifact, c.version, None, "same", pom_hint=c.jakarta)
            checked += 1
        same_ok = [b for b in d.builds if b.source == "same" and b.verified]
        d.searched.append(f"same coordinates: {checked} release(s) newer than {cur or '?'} checked, "
                          + (", ".join(b.version for b in same_ok) + " jakarta" if same_ok else "none jakarta"))
    elif rv is not None:
        d.searched.append(f"same coordinates: no newer release" + (f" ({rv.error})" if rv.error else ""))

    if not hasattr(lookup, "versions") or (rv is not None and rv.error and not rv.same_major and not rv.higher
                                           and "not found" not in rv.error):
        # 저장소를 아예 읽지 못했으면 형제 이름도 찾을 수 없다. 조회 기록만 남기고 끝낸다
        d.searched.append("sibling search skipped: the repository could not be read")
        return
    # (c) 이름 앞부분으로 Nexus 검색
    pairs: list[tuple[str, str]] = []
    if _cfg_bool(cfg, "nexus_search", True):
        pairs = _nexus_search(lookup, group, artifact, d)
        for g2, a2 in pairs:
            try:
                vs = list(lookup.versions(g2, a2) or [])
            except Exception:  # noqa: BLE001
                vs = []
            v = _newest_release(vs, include_pre)
            if v:
                add(g2, a2, v, None, "nexus-search")
    # (d) 이름 규칙으로 형제 좌표 탐색
    limit = _cfg_int(cfg, "max_jakarta_probes", DEFAULT_MAX_PROBES)
    for g2, a2 in sibling_names(group, artifact, limit):
        if (g2, a2) in pairs:
            continue
        d.probed.append(a2 if g2 == group else f"{g2}:{a2}")
        try:
            vs = list(lookup.versions(g2, a2) or [])
        except Exception:  # noqa: BLE001
            vs = []
        v = _newest_release(vs, include_pre)
        if v:
            add(g2, a2, v, None, "name-probe")


# ---------------------------------------------------------------------------------------------------------------------
# 진단 명령 `sbmig repo-probe`
# ---------------------------------------------------------------------------------------------------------------------
def probe_report(lookup: Any, group: str, artifact: str, current: Optional[str], *, table_entry: Any = None,
                 cfg: Any = None) -> list[str]:
    """``sbmig repo-probe g:a[:v]``가 출력할 줄을 만든다.
    폐쇄망 PC에서는 settings.xml을 보여 줄 수 없다. 대신 저장소 조회 결과를 한 화면에 보여 준다.
    조회한 URL과 응답, 찾은 버전, 후보가 jakarta인지, 탐색 결과가 들어간다.
    마지막 줄은 ``REPO-PROBE ok|none …`` 요약이다. 예외를 던지지 않는다."""
    out: list[str] = []
    key = f"{group}:{artifact}"
    try:
        out.append(f"REPO-PROBE {key}" + (f" (current {current})" if current else ""))
        srcs = list(lookup.repository_urls()) if hasattr(lookup, "repository_urls") else []
        files = list(getattr(lookup, "settings_files", []) or [])
        out.append("settings read: " + (", ".join(files) if files else "none (no ~/.m2/settings.xml, no build.settings_file, "
                                                                  "no global settings)"))
        for m in list(getattr(lookup, "settings_missing", []) or []):
            out.append(f"settings looked for but not read: {m}")
        # build.repository_url 값을 그대로 출력한다. not set이면 yml을 -c로 주지 않았거나 키가 build: 아래에 없다
        c = cfg if cfg is not None else getattr(lookup, "cfg", None)
        explicit = getattr(getattr(c, "build", None), "repository_url", None)
        if explicit and str(explicit).strip():
            out.append(f"build.repository_url: {str(explicit).strip()} (asked first, never mirrored, not subject to the HTTP blocker)")
        else:
            out.append("build.repository_url: not set — the set below is the settings mirrors / repositories, the poms' "
                       "<repositories> and Central only (set it under build: in the company yml and run with -c <yml>)")
        if getattr(lookup, "project_root", None) is None:
            out.append("pom <repositories>: not included (no --project given; a run reads the project's poms)")
        out.append("repositories (in order — settings mirrors by their mirrorOf rules, then unmirrored repositories):")
        for i, (u, c, o) in enumerate(srcs, 1):
            out.append(f"  {i}. {u}  [{o}; credentials: {'yes' if c else 'no'}]")
        if not srcs:
            out.append("  (none — offline and no mirror / repository URL)")
        for n in getattr(lookup, "url_notes", []) or []:
            out.append(f"  note: {n}")
        versions = list(lookup.versions(group, artifact) or [])
        out.append(f"maven-metadata.xml of {key}:")
        trace = lookup.lookup_trace(group, artifact) if hasattr(lookup, "lookup_trace") else []
        for u, o in trace:
            out.append(f"  {u} → {o}")
        if not trace:
            out.append("  (no repository asked)")
        pre = [v for v in versions if is_prerelease(v)]
        out.append("versions: " + (", ".join(versions) if versions else "none")
                   + (f" (prerelease, filtered unless libraries.include_prerelease: {', '.join(pre)})" if pre else ""))
        rv = lookup.candidates(key, current)
        if rv.error:
            out.append(f"lookup note: {rv.error}")
        if rv.same_major or rv.higher:
            ver = _Verifier(lookup, cfg)
            def line(c: Any) -> str:
                ok, how = ver.verify(group, artifact, c.version, None, c.jakarta)
                return f"{c.version} ({'jakarta ✓' if ok else 'not verified'} — {how})"
            out.append("same major: " + (", ".join(line(c) for c in rv.same_major) or "no newer release"))
            out.append("higher majors: " + (", ".join(line(c) for c in rv.higher) or "none"))
        d = discover_jakarta_builds(lookup, group, artifact, current, table_entry=table_entry, cfg=cfg)
        out.append("discovery:")
        for b in d.builds:
            out.append(f"  {b.text()}")
        if not d.builds:
            out.append("  no candidate")
        out.append(f"searched: {d.searched_text()}")
        best = d.best()
        out.append(f"REPO-PROBE {'ok' if best else 'none'} {key} versions={len(versions)} "
                   f"jakarta={best.coordinates if best else '-'} repositories={len(srcs)}")
    except Exception as e:  # noqa: BLE001
        out.append(f"REPO-PROBE FAIL {key}: {type(e).__name__}: {e}")
    return out
