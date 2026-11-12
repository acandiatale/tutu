"""Maven 모듈에는 소스가 없고 실제 소스는 트리의 다른 곳에 있는 프로젝트(hollow reactor)를 찾는 모듈.
분석 단계가 프로필을 만든 직후 ``detect_hollow``를 호출하고, 오케스트레이터가 결과에 따라 플래그를 단다.
- ``hollow-reactor``: 성공으로 보지 않는다(exit 2, 인수인계 첫 줄).
  빌드할 소스가 없어 성공한 실행이 이관 완료로 집계되면 안 되기 때문이다
- ``sources-outside-reactor``: 알림만 남긴다
- 폴더 이름은 보지 않는다. pom이 선언한 소스, 리소스, 웹 루트의 안과 밖에서 java / jsp / 웹 파일 수를 센다.
  치환하지 못한 ``${…}``는 unresolved로 남긴다
- 파일을 읽기만 하고 예외를 던지지 않는다

Hollow-reactor detection (2026-11-02). A Maven reactor whose modules' source / resource / web roots hold no sources
while the tree holds them elsewhere used to end as `success` with nothing migrated. `detect_hollow(ws, poms)` counts
`*.java`, JSP / tag files and html / css / js inside the roots the poms declare (defaults plus `<sourceDirectory>`,
`<resources>`, `warSourceDirectory`, `<webResources>`; `${…}` interpolated from the pom's and its in-repo parents'
properties, unresolvable expressions recorded) versus outside them (test roots and build output ignored). Verdicts:
`hollow` = no java inside and java outside (or, for a WAR, no JSP / web file inside and JSPs outside);
`sources_outside` = more java outside than inside (soft). No directory NAME is ever interpreted. Never raises.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sbmig.core.models import PomInfo

from .pom_parser import read_pom_text
from .source_scanner import _kid, _kids, _ktext, _xml_root

log = logging.getLogger("sbmig.analyze")

HOLLOW_FLAG = "hollow-reactor"
OUTSIDE_FLAG = "sources-outside-reactor"
HOLLOW_PREFIX = "the Maven reactor compiles no sources:"
OUTSIDE_PREFIX = "sources outside the Maven reactor:"

JAVA_EXT = (".java",)
JSP_EXT = (".jsp", ".jspx", ".jspf", ".tag", ".tagx")
WEB_EXT = (".html", ".htm", ".css", ".js")
# 소스가 있을 수 없는 폴더만 빼고 센다.
# 폴더 이름으로 역할을 정하지 않는다 / pruned while walking: never sources
_PRUNE = frozenset({".git", ".svn", ".hg", ".idea", ".settings", "node_modules", "__pycache__"})
_PROP_RE = re.compile(r"\$\{([^}]+)\}")
_BASEDIR_KEYS = ("basedir", "project.basedir", "pom.basedir", "project.build.directory")
_MAX_FILES = 200_000


@dataclass
class HollowResult:
    """hollow reactor 판단 결과를 담는 데이터 클래스. 모듈 안 / 밖의 종류별 파일 수, 밖에 있는 파일의 최상위
    폴더별 개수, 검사한 루트, 치환하지 못한 ``${…}``, WAR 여부를 담는다.

    inside / outside counts per kind (`java`, `jsp`, `web`), the outside files per top-level directory, the roots
    considered (root-relative), the unresolved `${…}` directory expressions, whether a WAR module exists."""
    inside: dict[str, int] = field(default_factory=lambda: {"java": 0, "jsp": 0, "web": 0})
    outside: dict[str, int] = field(default_factory=lambda: {"java": 0, "jsp": 0, "web": 0})
    outside_by_dir: dict[str, dict[str, int]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    war: bool = False
    error: str = ""

    @property
    def hollow(self) -> bool:
        """hollow reactor인지 확인한다. 모듈 안에 java가 없고 밖에 java가 있거나, WAR인데 안에 JSP / 웹
        파일이 없고 밖에 JSP가 있으면 참이다.

        No java inside and java outside — or a WAR with no JSP / web file inside and JSPs outside."""
        if self.inside["java"] == 0 and self.outside["java"] > 0:
            return True
        return self.war and self.inside["jsp"] + self.inside["web"] == 0 and self.outside["jsp"] > 0

    @property
    def sources_outside(self) -> bool:
        """hollow reactor는 아니지만 모듈 밖의 java가 안보다 많은지 확인한다.

        Not hollow, but more java files lie outside the roots than inside them."""
        return not self.hollow and self.outside["java"] > self.inside["java"] > 0

    @property
    def outside_total(self) -> int:
        """모듈 밖에 있는 파일의 전체 수."""
        return sum(self.outside.values())

    def counts_text(self) -> str:
        """모듈 밖 파일 수를 요약한 문구를 만든다. 0인 종류는 생략한다.
        예: ``120 java, 35 jsp, 25 web files``"""
        parts = [f"{n} {k}" for k, n in self.outside.items() if n]
        return (", ".join(parts) + (" files" if parts else "")) or "0 files"

    def by_dir_text(self, limit: int = 6) -> str:
        """모듈 밖 파일의 최상위 폴더별 개수를 많은 순으로 ``limit``개까지 보여 주는 문구를 만든다.
        예: ``DEV: java 120, jsp 35; STATIC: web 25``"""
        rows = sorted(self.outside_by_dir.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
        out = [f"{d}: " + ", ".join(f"{k} {n}" for k, n in c.items() if n) for d, c in rows[:limit]]
        if len(rows) > limit:
            out.append(f"+{len(rows) - limit} more")
        return "; ".join(out)

    def unresolved_text(self) -> str:
        """치환하지 못한 디렉터리 식을 `` (unresolved directories: ${DIR_X}, …)`` 형식으로 반환한다. 없으면 빈 문자열을 반환한다."""
        return f" (unresolved directories: {', '.join(self.unresolved[:6])})" if self.unresolved else ""

    def as_journal(self) -> dict[str, Any]:
        """저널 이벤트에 넣을 dict(개수, 루트, 치환하지 못한 식)를 만든다."""
        return {"inside": dict(self.inside), "outside": dict(self.outside),
                "outside_by_dir": {d: dict(c) for d, c in self.outside_by_dir.items()},
                "roots": list(self.roots), "unresolved": list(self.unresolved), "war": self.war}


def hollow_followup(res: HollowResult) -> str:
    """소스가 reactor 밖에 있어 이관되지 않았다는 인수인계 첫 줄(영어)을 만든다.
    i18n PATTERNS가 한국어로 바꾼다.

    The Handover's first line for a hollow reactor (English; `sbmig.report.i18n.PATTERNS` renders it in Korean)."""
    return (f"{HOLLOW_PREFIX} {res.counts_text()} lie outside its modules ({res.by_dir_text()}) and were NOT migrated — "
            f"the as-is build only packaged files produced elsewhere{res.unresolved_text()}; the sources must be mapped "
            "into the modules first (sbmig: tree profile, next increment)")


def outside_followup(res: HollowResult) -> str:
    """reactor 밖의 java가 애플리케이션의 일부인지 확인하라는 후속 조치 줄(영어)을 만든다.

    The soft follow-up: more java outside the roots than inside — check whether it belongs to the application."""
    return (f"{OUTSIDE_PREFIX} {res.counts_text()} lie outside the modules' source roots ({res.by_dir_text()}) and are "
            "not built or migrated — check whether they belong to the application (a stale copy, a tool, or sources the "
            "as-is build reached in another way)")


# ---------------------------------------------------------------------------------------------------- roots
def _norm(rel: str) -> str:
    rel = posixpath.normpath(rel.replace("\\", "/"))
    return "" if rel in (".", "") else rel.strip("/")


def _chain_props(pom: PomInfo, by_key: dict[tuple[str, str], PomInfo]) -> dict[str, str]:
    """pom의 ``<properties>``에 저장소 안 부모 pom의 속성을 합쳐 실제로 적용되는 속성을 만든다(자식 우선).
    ``basedir`` 계열은 ``.``으로 둔다."""
    chain: list[PomInfo] = []
    cur: PomInfo | None = pom
    seen: set[str] = set()
    while cur is not None and cur.path not in seen:
        seen.add(cur.path)
        chain.append(cur)
        par = cur.parent
        cur = by_key.get(((par.group_id or ""), par.artifact_id)) if par is not None and par.artifact_id else None
    props: dict[str, str] = {}
    for p in reversed(chain):
        props.update(p.properties or {})
    for k in _BASEDIR_KEYS:
        props.setdefault(k, ".")
    return props


def _interpolate(value: str, props: dict[str, str], depth: int = 6) -> str:
    out = value
    for _ in range(depth):
        if "${" not in out:
            break
        out = _PROP_RE.sub(lambda m: props.get(m.group(1), m.group(0)), out)
    return out


def _resolve_dir(raw: str | None, base: str, props: dict[str, str], unresolved: list[str]) -> str | None:
    """pom의 디렉토리 값을 루트 기준 posix 경로로 바꾼다.
    ``${…}``가 남거나 프로젝트 밖이면 None을 반환하고 ``unresolved``에 기록한다."""
    if not raw or not raw.strip():
        return None
    text = _interpolate(raw.strip().replace("\\", "/"), props)
    if "${" in text:
        if text not in unresolved:
            unresolved.append(text)
        return None
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    joined = posixpath.normpath(posixpath.join(base or ".", text))
    if joined.startswith("../") or joined == "..":
        return None
    return _norm(joined)


def module_roots(ws: Any, poms: Iterable[PomInfo]) -> tuple[list[str], list[str], list[str], bool]:
    """각 pom이 컴파일하고 패키징하는 루트, 무시할 테스트 루트, 치환하지 못한 ``${…}``, WAR 모듈 여부를 반환한다.
    기본 ``src/main/*`` 루트에 pom이 선언한 소스, 리소스, 웹 루트를 더한다.

    (roots, ignored roots, unresolved expressions, has_war) — root-relative; the defaults are listed whether or not
    they exist (a missing default simply holds nothing)."""
    pom_list = list(poms or [])
    by_key = {((p.group_id or ""), p.artifact_id): p for p in pom_list}
    roots: list[str] = []
    ignored: list[str] = []
    unresolved: list[str] = []
    war = False

    def add(lst: list[str], d: str | None) -> None:
        if d is not None and d not in lst:
            lst.append(d)

    for p in pom_list:
        base = posixpath.dirname(p.path.replace("\\", "/"))
        war = war or (p.packaging or "").lower() == "war"
        props = _chain_props(p, by_key)
        try:
            root_el = _xml_root(read_pom_text(ws, p.path))
        except Exception:  # noqa: BLE001 - unreadable pom: defaults only
            root_el = None
        build = _kid(root_el, "build") if root_el is not None else None
        src = _resolve_dir(_ktext(build, "sourceDirectory"), base, props, unresolved) if build is not None else None
        add(roots, src if src is not None else _norm(posixpath.join(base, "src/main/java")))
        if build is not None:
            add(roots, _resolve_dir(_ktext(build, "scriptSourceDirectory"), base, props, unresolved))
            tsrc = _resolve_dir(_ktext(build, "testSourceDirectory"), base, props, unresolved)
            add(ignored, tsrc if tsrc is not None else _norm(posixpath.join(base, "src/test")))
            res = _kid(build, "resources")
            if res is None:
                add(roots, _norm(posixpath.join(base, "src/main/resources")))
            for r in _kids(res, "resource"):
                d = _resolve_dir(_ktext(r, "directory"), base, props, unresolved) if _kid(r, "directory") is not None \
                    else _norm(posixpath.join(base, "src/main/resources"))
                add(roots, d)
            for plugins in (_kid(build, "plugins"), _kid(_kid(build, "pluginManagement"), "plugins")):
                for pl in _kids(plugins, "plugin"):
                    if _ktext(pl, "artifactId") != "maven-war-plugin":
                        continue
                    conf = _kid(pl, "configuration")
                    add(roots, _resolve_dir(_ktext(conf, "warSourceDirectory"), base, props, unresolved))
                    for r in _kids(_kid(conf, "webResources"), "resource"):
                        add(roots, _resolve_dir(_ktext(r, "directory"), base, props, unresolved))
        else:
            add(roots, _norm(posixpath.join(base, "src/main/resources")))
            add(ignored, _norm(posixpath.join(base, "src/test")))
        add(roots, _norm(posixpath.join(base, "src/main/webapp")))
    return roots, ignored, unresolved, war


def _kind(name: str) -> str | None:
    low = name.lower()
    if low.endswith(JAVA_EXT):
        return "java"
    if low.endswith(JSP_EXT):
        return "jsp"
    if low.endswith(WEB_EXT):
        return "web"
    return None


def _under(rel: str, d: str) -> bool:
    return d == "" or rel == d or rel.startswith(d + "/")


def detect_hollow(ws: Any, poms: Iterable[PomInfo]) -> HollowResult:
    """작업공간을 순회하며 java / JSP / 웹 파일을 pom 루트 안과 밖으로 나눠 센다.
    테스트 루트와 빌드 산출물은 제외한다.
    pom이 없으면 빈 결과를 반환한다. 예외를 던지지 않고, 실패는 ``error``에 남긴다.

    Walk `ws.root`, classify every java / JSP / web file as inside or outside the poms' roots (test roots and Maven
    build output ignored). No poms → an empty result. Never raises."""
    res = HollowResult()
    try:
        # 디스크에 실제로 있는 pom만 본다. 파일 없이 프로필에만 있는 항목은 근거가 되지 않는다
        # only poms that exist in the tree count — a profile's stand-in PomInfo without a file is no evidence
        exists = getattr(ws, "exists", None)
        pom_list = [p for p in (poms or []) if p.path and (exists is None or exists(p.path))]
        if not pom_list:
            return res
        roots, ignored, unresolved, war = module_roots(ws, pom_list)
        res.roots, res.ignored, res.unresolved, res.war = roots, ignored, unresolved, war
        root = Path(ws.root)
        pom_dirs = {posixpath.dirname(p.path.replace("\\", "/")) for p in pom_list}
        # 접수가 무시한 폴더의 java가 reactor 밖 소스로 잡히지 않도록 세지 않는다
        # v2.1z-h: the intake's ignored dirs (stray pom copies) are not sources (their java would count as "outside")
        is_ignored = getattr(ws, "is_ignored", None)
        seen = 0
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = _norm(os.path.relpath(dirpath, root))
            # pom 옆의 target만 뺀다. pom이 없는 곳의 target은 소스 폴더일 수 있기 때문이다
            dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE and not (
                d == "target" and (rel_dir in pom_dirs or "pom.xml" in filenames))
                and not (callable(is_ignored) and is_ignored(f"{rel_dir}/{d}" if rel_dir else d)))
            if any(_under(rel_dir, ig) for ig in ignored if ig):
                dirnames[:] = []
                continue
            for name in filenames:
                kind = _kind(name)
                if kind is None:
                    continue
                seen += 1
                if seen > _MAX_FILES:
                    res.error = f"stopped counting after {_MAX_FILES} files"
                    return res
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if any(_under(rel, r) for r in roots):
                    res.inside[kind] += 1
                else:
                    res.outside[kind] += 1
                    top = rel.split("/", 1)[0] if "/" in rel else "."
                    res.outside_by_dir.setdefault(top, {"java": 0, "jsp": 0, "web": 0})[kind] += 1
        for d in list(res.outside_by_dir):
            res.outside_by_dir[d] = {k: n for k, n in res.outside_by_dir[d].items() if n}
    except Exception as e:  # noqa: BLE001 - the detector must never stop the run
        log.warning("hollow-reactor detection failed: %s: %s", type(e).__name__, e)
        res.error = f"{type(e).__name__}: {e}"
    return res
