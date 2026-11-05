"""v2.1z-e jakarta 빌드 탐색 — javax.* 를 아직 쓰는 라이브러리의 jakarta 빌드를 저장소(회사 Nexus / 미러 / 로컬
저장소)에서 sbmig 가 직접 찾는다. 왜: 회사 실행에서 상용 UI 라이브러리의 javax 빌드가 걸렸을 때 LLM 은 "jakarta 빌드가
없다" 고 거절했고 보고서는 사람에게 찾으라고 했지만, 같은 좌표의 상위 메이저 ``2.0.5-jakarta`` 가 Nexus 에 있었다 — 그리고
그런 빌드는 다른 artifactId(``grid8-jakarta``) 나 다른 버전 줄 좌표(``com.vendor9:vendor9``)로도 올라온다. 흐름상 위치:
수정 루프가 ``external_javax_api`` 케이스를 LLM 에 묻기 전(사전 시도 ``sbmig-jakarta``)과 프롬프트의 "Dependency versions"
줄을 만들 때 ``discover_jakarta_builds`` 를 부른다. 찾는 순서: (a) 표(``jakarta_key`` / classifier) → (b) 같은 좌표의
상위 릴리스(``RepositoryLookup.candidates``) → (c) Nexus 3 검색 API(이름·그룹의 줄기 ``*grid*``) → (d) 이름 규칙
탐침(``-jakarta`` 류, 뒷자리 숫자 올리기). 후보마다 "정말 jakarta 빌드인가" 를 확인한다: 버전 한정어 / classifier 가
``jakarta`` 면 확인(벤더 pom 은 보통 의존성을 선언하지 않아 pom 만으로는 모른다) → pom 이 jakarta.* 를 선언 → 그래도
모르면 jar 를 내려받아(상한 MB) class 파일 바이트에서 ``jakarta/servlet/`` 대 ``javax/servlet/`` 참조를 센다. 확인되지
않은 후보는 보고만 하고 절대 적용하지 않는다. 모든 조회는 상한이 있고 실행당 (g, a) 별로 캐시하며 절대 예외를 올리지
않는다(실패 = 빈 결과 + ``searched`` 기록). 사람이 무엇을 찾아봤는지 볼 수 있게 조회 내용을 문장으로 남긴다.

v2.1z-e jakarta build discovery (docs/INTERFACES.md "v2.1z-e").

`discover_jakarta_builds(lookup, group, artifact, current, table_entry=…, cfg=…)` → `JakartaDiscovery`: the verified
jakarta builds of a javax-era library in the configured repository, in this order and all bounded —
(a) `table`: the library table's `jakarta_key` (a different coordinate) / `classifier`; (b) `same`: the same g:a's
newer releases (`RepositoryLookup.candidates`: same major first, then the higher majors — a vendor line such as
`1.0.6 … 1.1.15` (javax) + `2.0.5-jakarta`); (c) `nexus-search`: Nexus 3 `service/rest/v1/search` with the name /
group STEM (trailing digits stripped) between wildcards, ≤ 3 pages per query; (d) `name-probe`: `maven-metadata.xml`
of sibling names (`{a}-jakarta`, `{a}_jakarta`, `jakarta-{a}`, `{a}-ee9|ee10`, and the trailing digits of the
artifactId / the group's last segment raised by one and two — `grid8` → `grid9`, `com.example.grid8:grid8` →
`com.example.grid9:grid9`), ≤ `libraries.max_jakarta_probes`. Verification per candidate: a `jakarta` version
qualifier or classifier → verified ("version qualifier"); else its pom (`pom_is_jakarta`); else a bounded **jar
scan** (`libraries.max_jar_scan_mb`, ≤ `libraries.max_jar_scans` downloads): the class files' constant pools
referencing `jakarta/<ee>/` vs `javax/<ee>/`. Unverified candidates are reported, never applied. The repository's
reachability rules of `RepositoryLookup` apply (unreachable twice → not asked again this run). Never raises.
"""
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
NEXUS_PAGES = 3                           # continuationToken pages per Nexus query
NEXUS_MAX_PAIRS = 10                      # (group, name) pairs taken from the search results
MAX_CLASS_ENTRIES = 4000                  # class files looked at per jar scan
STEM_MIN_CHARS = 3                        # a shorter stem (`a`, `ui`) would match everything in Nexus
# javax → jakarta 로 옮겨진 EE 루트 패키지(jar 스캔이 세는 것). javax.annotation.processing 은 JDK 라 뺀다.
# the EE roots that moved to jakarta.* (what the jar scan counts); `javax/annotation/processing/` is the JDK's
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
    """찾은 jakarta 빌드 후보 하나: 좌표·버전·classifier, 어디서 찾았나(``source``), 확인됐나(``verified``)와 그 근거
    (``how``), 같은 좌표의 상위 메이저인가(``major_change`` — 운영자 확인 대상).

    One candidate jakarta build: coordinates, `source` (table / same / nexus-search / name-probe), whether it is
    verified and how (`how`), and whether it is a higher major of the same coordinates."""
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
        """`groupId:artifactId`."""
        return f"{self.group}:{self.artifact}"

    @property
    def javax(self) -> bool:
        """확인 결과가 "javax 빌드" 로 판명된 후보인가(pom 이 javax.* 를 선언 / jar 스캔이 javax) — jakarta 후보로 보여 주지 않는다."""
        return self.how in ("pom declares javax.*", "jar scan: javax")

    @property
    def coordinates(self) -> str:
        """`g:a:v` (+ `:classifier`)."""
        return f"{self.key}:{self.version}" + (f":{self.classifier}" if self.classifier else "")

    def text(self) -> str:
        """프롬프트 / 인수인계 한 토막: ``com.example.grid8:grid8 2.0.5-jakarta (jakarta ✓ — version qualifier; found by same coordinates, a higher major)``."""
        mark = "jakarta ✓" if self.verified else "jakarta unverified"
        where = {"same": "same coordinates", "table": "the library table", "nexus-search": "Nexus search",
                 "name-probe": "name probe"}.get(self.source, self.source)
        tail = f"; found by {where}" + (", a higher major" if self.major_change else "")
        cls = f" classifier {self.classifier}" if self.classifier else ""
        return f"{self.key} {self.version}{cls} ({mark} — {self.how or 'no evidence'}{tail})"


@dataclass
class JakartaDiscovery:
    """한 라이브러리의 탐색 결과: 후보들(``builds``), 사람이 읽는 조회 기록(``searched``), 탐침한 이름들(``probed``).

    The discovery result of one library: `builds`, the human-readable record of every query (`searched`), the
    probed sibling names (`probed`), and `error` when the repository could not be used at all."""
    key: str
    current: Optional[str] = None
    builds: list[JakartaBuild] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    probed: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def verified(self) -> list[JakartaBuild]:
        """확인된 빌드들(적용 후보)."""
        return [b for b in self.builds if b.verified]

    def best(self) -> Optional[JakartaBuild]:
        """적용할 빌드 하나: 표 → 같은 좌표의 같은 메이저(최신) → 같은 좌표의 상위 메이저(가장 낮은 메이저의 최신) →
        Nexus 검색 → 이름 탐침. 확인된 것만.

        The build the pre-try applies: the table's, else the same coordinates on the same major (newest), else the
        same coordinates on the lowest higher major (newest of it), else a Nexus-search / name-probe sibling."""
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
        """조회 기록 한 문장(인수인계·힌트): 무엇을 어디서 찾아봤고 결과가 어땠는지."""
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
        """프롬프트 "Dependency versions" 절의 줄들: 확인된 빌드마다 한 줄(적용 가능), 미확인 후보는 '자동 적용 안 함'
        꼬리, 아무것도 없으면 무엇을 찾아봤는지 한 줄.

        Prompt lines: one per build (`repository also has …`), or one line saying what was searched when nothing
        was found."""
        out: list[str] = []
        for b in self.builds:
            if b.javax:
                continue                                   # a javax build is not a candidate at all
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
        """스위치가 꺼져 아무것도 찾아보지 않았다(``libraries.discover_jakarta_builds: false``) — "없음" 이라 말하지 않는다."""
        return any(x.startswith("discovery off (") for x in self.searched)


# ---------------------------------------------------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------------------------------------------------
def stem(name: str) -> str:
    """이름의 줄기: 끝의 숫자(와 그 앞의 ``-`` / ``_`` / ``.``)를 뗀 것 — ``grid8`` → ``grid``, ``vendor-ui9`` → ``vendor-ui``.

    The name without its trailing digits (and the separator before them)."""
    m = _DIGITS_RE.match(name or "")
    return (m.group("stem") or name) if m else (name or "")


def digit_variants(name: str, steps: int = 2) -> list[str]:
    """끝자리 숫자를 1, 2 올린 이름들(``grid8`` → ``grid9``, ``grid10``); 숫자로 끝나지 않으면 [].

    The name with its trailing number raised by 1 … `steps` ([] when it does not end in digits)."""
    m = _DIGITS_RE.match(name or "")
    if not m:
        return []
    n = int(m.group("n"))
    return [f"{m.group('stem')}{m.group('sep')}{n + k}" for k in range(1, steps + 1)]


def sibling_names(group: str, artifact: str, limit: int = DEFAULT_MAX_PROBES) -> list[tuple[str, str]]:
    """탐침할 형제 좌표(가능성 높은 순, 중복 없이, ``limit`` 개까지): 원래 그룹의 ``{a}-jakarta`` 류 → artifactId 와
    그룹 마지막 세그먼트의 뒷자리 숫자를 같이 올린 것(``com.example.grid9:grid9``) → artifactId 만 올린 것
    (``com.example.grid8:grid9``) → ``ee9`` / ``ee10`` 류. ``com.vendor9:vendor8`` 처럼 그룹만 올린 것은 뜻이 없어 뺀다.

    The sibling coordinates to probe, most likely first, deduplicated, at most `limit`: `-jakarta` variants on the
    original group, the trailing digits of the artifactId AND the group's last segment raised together, the
    artifactId alone, then the `ee9` / `ee10` variants. The group alone (`com.vendor9:vendor8`) is skipped."""
    out: list[tuple[str, str]] = []

    def add(g: str, a: str) -> None:
        if (g, a) != (group, artifact) and (g, a) not in out and g and a:
            out.append((g, a))

    add(group, f"{artifact}-jakarta")
    a_up = digit_variants(artifact)
    head, _, last = group.rpartition(".")
    g_up = [f"{head}.{x}" if head else x for x in digit_variants(last)]
    for a2, g2 in zip(a_up, g_up):                    # together: com.example.grid9:grid9, then grid10
        add(g2, a2)
    for a2 in a_up:                                   # the artifactId alone: com.example.grid8:grid9
        add(group, a2)
    add(group, f"{artifact}_jakarta")
    add(group, f"{artifact}.jakarta")
    add(group, f"jakarta-{artifact}")
    add(group, f"{artifact}-jakarta-ee")
    add(group, f"{artifact}-ee10")
    add(group, f"{artifact}-ee9")
    for a2, g2 in zip(a_up, g_up):                    # the -jakarta name on the raised group line, last
        add(g2, f"{a2}-jakarta")
    return out[: max(0, int(limit))]


def artifact_names_jakarta(artifact_id: str) -> bool:
    """artifactId 의 토큰에 ``jakarta`` 가 있는가(``dext5-jakarta``, ``grid_jakarta``, ``x.jakarta``) — 공급사가 이름으로 표시한
    jakarta 빌드라 그 자체로 확인으로 본다(groupId 는 보지 않는다).

    True when a token of the artifactId is `jakarta` (`dext5-jakarta`): the vendor's own label verifies the build."""
    return any(t.lower() == "jakarta" for t in re.split(r"[-_.]", artifact_id or "") if t)


def qualifier_is_jakarta(version: str) -> bool:
    """버전 한정어에 ``jakarta`` 토큰이 있는가(``2.0.5-jakarta``, ``3.1.jakarta``) — 그 자체로 jakarta 빌드의 확인.

    True when a version token is `jakarta` (`2.0.5-jakarta`): that alone verifies the build."""
    return any(t.lower() == "jakarta" for t in _TOKEN_RE.findall(version or ""))


# ---------------------------------------------------------------------------------------------------------------------
# jar scan
# ---------------------------------------------------------------------------------------------------------------------
def scan_jar(data: bytes, max_classes: int = MAX_CLASS_ENTRIES) -> Optional[bool]:
    """jar 바이트의 class 파일들이 ``jakarta/<ee>/`` 를 참조하면 True, ``javax/<ee>/`` 만 참조하면 False, 둘 다
    없으면(EE API 를 안 쓰는 jar) None. 깨진 zip 도 None. 상수 풀을 파싱하지 않고 바이트 검색으로 센다(충분히 정확하고
    빠르다).

    The jar's class files: True when they reference `jakarta/<ee root>/`, False when only `javax/<ee root>/`
    (JDK `javax.annotation.processing` not counted), None when neither or the zip is unreadable."""
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
                except Exception:  # noqa: BLE001 - one unreadable entry: skip it
                    continue
                if any(r in blob for r in _JAKARTA_REFS):
                    jakarta += 1
                cleaned = blob
                for skip in _JAVAX_NOT_EE:
                    cleaned = cleaned.replace(skip, b"")
                if any(r in cleaned for r in _JAVAX_REFS):
                    javax += 1
    except Exception as e:  # noqa: BLE001 - not a zip / truncated download
        log.debug("jar scan: %s: %s", type(e).__name__, e)
        return None
    if jakarta and not javax:
        return True
    if javax and not jakarta:
        return False
    return True if jakarta else None


# ---------------------------------------------------------------------------------------------------------------------
# discovery
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
    """후보 하나의 확인 절차(한정어 → pom → jar 스캔)와 그 예산."""

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
            # v2.1z-g (회사 Nexus: `dext5-jakarta:21.1` 처럼 artifactId 에 붙은 jakarta — 공급사가 이름으로 표시한 빌드; pom 은
            # 침묵하고 jar 는 클 수 있다) / the vendor labelled the artifact itself: that alone verifies it
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
    """Nexus 3 검색 API 로 이름·그룹 줄기가 같은 (group, name) 쌍들(현재 좌표 제외, 최대 NEXUS_MAX_PAIRS)."""
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
                        continue                  # the search was wide: keep the names that share the stem
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
    """라이브러리 ``group:artifact``(현재 ``current``)의 jakarta 빌드를 찾는다(모듈 docstring 의 순서와 확인 규칙).
    결과는 ``lookup`` 에 (g, a, current) 별로 캐시된다. 절대 예외 없음.

    The verified jakarta builds of `group:artifact` (module docstring). Cached on `lookup` per (g, a, current).
    Never raises."""
    key = f"{group}:{artifact}"
    cur = current if current and "${" not in current else None
    cache = getattr(lookup, "_jakarta_cache", None)
    if cache is None:
        try:
            cache = lookup._jakarta_cache = {}
        except Exception:  # noqa: BLE001 - a lookup that refuses attributes: no cache
            cache = {}
    ck = (group, artifact, cur)
    if ck in cache:
        return cache[ck]
    d = JakartaDiscovery(key=key, current=cur)
    try:
        _discover(lookup, group, artifact, cur, d, table_entry, cfg, rv)
    except Exception as e:  # noqa: BLE001 - discovery is an aid: never a reason to fail a case
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
        # 회사 첫 실행의 비상 스위치: 꺼지면 v2.1d 그대로(같은 좌표 릴리스 나열만) — 확인·형제 탐색·사전 시도·거절 반박 모두 없음
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
            ok, how = True, "library table (a person's entry)"     # the people's table is the confirmation
        else:
            ok, how = ver.verify(g, a, v, classifier, pom_hint)
        major_change = (g, a) == (group, artifact) and cur_major is not None and (major_of(v) or 0) > cur_major
        d.builds.append(JakartaBuild(group=g, artifact=a, version=v, classifier=classifier, source=source,
                                     verified=ok, how=how, major_change=major_change))

    # (a) the table: a different coordinate (`jakarta_key`) or a classifier jar of the same coordinates
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
                pom_hint=True if not vs else None)      # table knowledge counts when the repository is unreadable
            d.searched.append(f"library table: {jk} {v}" + ("" if vs else " (repository unreadable; the table's version)"))
        else:
            d.searched.append(f"library table: {jk} lists no release")
    elif entry is not None and getattr(entry, "classifier", None) and getattr(entry, "version", None):
        add(group, artifact, str(entry.version), str(entry.classifier), "table", pom_hint=True)
        d.searched.append(f"library table: classifier {entry.classifier} at {entry.version}")

    # (b) the same coordinates: newer releases of the same major, then the higher majors (the caller's rv when given —
    #     `candidates` is asked once per library and run)
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
        # 저장소 자체를 못 읽었으면(offline·timeout·401) 형제를 찔러도 같은 답이다 — 조회 기록만 남기고 끝
        d.searched.append("sibling search skipped: the repository could not be read")
        return
    # (c) Nexus search by stem
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
    # (d) name probes
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
# diagnostics: `sbmig repo-probe`
# ---------------------------------------------------------------------------------------------------------------------
def probe_report(lookup: Any, group: str, artifact: str, current: Optional[str], *, table_entry: Any = None,
                 cfg: Any = None) -> list[str]:
    """``sbmig repo-probe g:a[:v]`` 가 찍는 줄들 — 폐쇄망 PC 에서 settings.xml 을 보여 줄 수 없을 때 "저장소 조회가 왜 그렇게
    답했나" 를 한 화면에: 물어보는 URL 들(출처·자격증명 유무), g:a 의 maven-metadata.xml 을 URL 마다 무엇이라 답했는지, 찾은
    버전들(프리릴리스로 걸러진 것 포함), 후보의 jakarta 판정(한정어 / pom / jar 스캔), 탐색이 찾은 형제 빌드와 조회 기록.
    마지막 줄은 ``REPO-PROBE ok|none …`` 요약. 절대 예외 없음.

    The lines of `sbmig repo-probe`: the repository set, the per-URL outcome of the metadata lookup, the versions
    (prereleases marked), the candidates' jakarta verdicts, the discovery's builds / searched record; last line
    `REPO-PROBE ok|none …`. Never raises."""
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
        # 2026-11-05 (회사 PC: "repository_url 을 넣었는데 1 번이 Central"): 조회가 본 build.repository_url 값을 그대로 찍는다 —
        # not set 이면 yml 이 -c 로 안 들어왔거나 키가 build: 아래에 없는 것(설정 로더가 WARNING 으로 알린다)
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
