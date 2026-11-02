"""Oracle ADF(JDeveloper) 프로젝트 판정과 규모 산정(v2.1z-d, 2026-11-02). 흐름상 위치: 분석(analyze)이 프로필을 만들 때
``detect_adf`` 를 부르고, ADF 근거가 있으면 ``ProjectProfile.adf`` 에 요약(``AdfSummary``)을 남긴다; 오케스트레이터는 **두 계열
이상**의 근거가 있을 때만(``is_adf``) 실행을 멈추고 "ADF 프로젝트라 변환이 불가능합니다" 로 끝낸다(``intake.adf_policy: stop``,
exit 3, 플래그 ``adf-project``; ``continue`` = 보고만 하고 계속, 권장하지 않음). 한 계열뿐이면 플래그 ``adf-traces`` 와 인수인계 메모
("ADF 흔적: … — 변환은 계속하되 확인 필요")만 남기고 변환을 계속한다. 현업 결정(2026-11-02): ADF 는 변환 대상이 아니고 재작성
계획도 없다 — 도구가 할 일은 확실한 ADF 만 가려내고(오탐 금지) 센서스용 규모를 적는 것.

판정 근거 네 계열(폴더 이름은 보지 않는다 — Oracle 의 파일 이름 · XML 네임스페이스 · import 만):
A ``bc4j``  — ``http://xmlns.oracle.com/bc4j`` 네임스페이스의 ``Entity`` / ``ViewObject`` / ``AppModule`` / ``Association`` / ``ViewLink`` XML;
B ``java``  — ``oracle.jbo.`` / ``oracle.adf.`` / ``oracle.binding.`` 을 import 하는 java;
C ``model`` — ADF Model 바인딩 파일 ``adfm.xml`` / ``DataBindings.cpx`` / ``*PageDef.xml``(루트 ``pageDefinition``);
D ``faces`` — ``http://xmlns.oracle.com/adf/faces`` 네임스페이스를 쓰는 ``jspx`` / ``jsff``, ``adfc-config.xml``,
``http://xmlns.oracle.com/adf/controller`` 네임스페이스의 bounded ``*-flow.xml``.
``.jpr`` 의 기술 이름과 ``adf-config.xml`` 은 근거가 아니라 참고(``hints``)다. ``target/`` · 형상관리 · IDE 메타데이터 폴더는 건너뛰고
jar 는 열지 않는다. 근거 경로(상한)는 저널과 보고서의 "판정 근거 / evidence" 에 남아 사람이 판정을 검토할 수 있다. 파일 읽기만
하고 절대 예외를 올리지 않는다.

Oracle ADF (JDeveloper) detection and sizing (v2.1z-d, 2026-11-02; business decision: ADF projects are NOT converted — the
run ends with "ADF 프로젝트라 변환이 불가능합니다", nothing else is planned). `detect_adf(ws)` walks the tree once and collects an
`AdfInventory` over four independent evidence families — A `bc4j` (BC4J XML in the `http://xmlns.oracle.com/bc4j` namespace),
B `java` (imports of `oracle.jbo.` / `oracle.adf.` / `oracle.binding.`), C `model` (`adfm.xml`, `DataBindings.cpx`,
`*PageDef.xml`), D `faces` (`jspx` / `jsff` carrying the `http://xmlns.oracle.com/adf/faces` namespace, `adfc-config.xml`,
bounded `*-flow.xml` in the `http://xmlns.oracle.com/adf/controller` namespace) — plus counts (entities + distinct
`DBObjectName` tables, view objects, application modules, page definitions, task flows + views, ADF pages, java total / on ADF)
and the same per top-level directory. **`is_adf` = at least two families present** (no false positives: a `.jpr` technology name,
`adf-config.xml` or a single family alone never rejects); exactly one family = `traces` (soft flag `adf-traces`, the run goes
on). `target/`, VCS / IDE metadata directories are ignored; archives are never opened. Evidence paths (capped) go into the
journal event and the report so a person can audit the decision. Pure reads; never raises.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from sbmig.core.models import AdfSummary

log = logging.getLogger("sbmig.analyze")

ADF_FLAG = "adf-project"
TRACES_FLAG = "adf-traces"
ADF_MESSAGE_KO = "ADF 프로젝트라 변환이 불가능합니다"
TRACES_MESSAGE_KO = "ADF 흔적: 변환은 계속하되 확인 필요"
ADF_PREFIX = "ADF project (Oracle JDeveloper / ADF): conversion is not possible —"
TRACES_PREFIX = "ADF traces"
FAMILIES = ("bc4j", "java", "model", "faces")        # A, B, C, D — the order of every listing
BC4J_NS = "http://xmlns.oracle.com/bc4j"
FACES_NS = "http://xmlns.oracle.com/adf/faces"       # also matches …/adf/faces/rich
CONTROLLER_NS = "http://xmlns.oracle.com/adf/controller"
BC4J_ROOTS = frozenset({"Entity", "ViewObject", "AppModule", "Association", "ViewLink"})
ADF_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?oracle\.(?:adf|jbo|binding)\.", re.MULTILINE)
# .jpr(JDeveloper 프로젝트 파일)의 ADF 기술 키 — 판정 근거가 아니라 참고 / named in a project file: a hint, never evidence
JPR_ADF_RE = re.compile(r"ADF_FACES|ADFbc|ADF_PAGE_FLOW|ADFm\b|ADF_BINDING|oracle\.adf\.|TECHNOLOGY_SCOPE_ADF", re.IGNORECASE)
PAGE_EXT = (".jspx", ".jsff")
_PRUNE = frozenset({".git", ".svn", ".hg", ".idea", ".settings", "node_modules", "__pycache__", "target"})
_MAX_FILES = 200_000
_HEAD_BYTES = 8192              # the root element / namespaces sit at the top of an XML or page file
_JAVA_HEAD_LINES = 120          # imports sit at the top of a java file
_MAX_XML_BYTES = 2 * 1024 * 1024
_CAP_TABLES = 50
_CAP_PER_FAMILY = 8
_CAP_HINTS = 10
_ROOT_TAG_RE = re.compile(r"<\s*(?:[\w.-]+:)?([A-Za-z_][\w.-]*)\b([^>]*)>")


def _empty_counts() -> dict[str, int]:
    return {"java": 0, "java_adf": 0, "bc4j": 0, "pages": 0, "page_defs": 0}


@dataclass
class AdfInventory:
    """판정 결과: 계열별 근거 경로, 참고 경로(``.jpr`` · ``adf-config.xml``), 종류별 개수, 테이블 목록, 최상위 폴더별 개수.

    evidence paths per family, hint paths, counts per kind, the distinct tables, per-top-level-directory counts."""
    families: dict[str, list[str]] = field(default_factory=lambda: {f: [] for f in FAMILIES})
    hints: list[str] = field(default_factory=list)
    entities: int = 0
    view_objects: int = 0
    app_modules: int = 0
    associations: int = 0
    view_links: int = 0
    page_defs: int = 0
    task_flows: int = 0
    flow_views: int = 0
    pages: int = 0
    java_total: int = 0
    java_adf: int = 0
    tables: list[str] = field(default_factory=list)
    by_dir: dict[str, dict[str, int]] = field(default_factory=dict)
    error: str = ""

    @property
    def present(self) -> list[str]:
        """근거가 있는 계열들(A→D 순)."""
        return [f for f in FAMILIES if self.families.get(f)]

    @property
    def is_adf(self) -> bool:
        """확실한 ADF 프로젝트인가 = 서로 다른 계열이 **둘 이상** — 그래야 "변환 불가" 로 멈춘다(오탐 금지).

        At least two different evidence families — only then the run is rejected."""
        return len(self.present) >= 2

    @property
    def traces(self) -> bool:
        """한 계열뿐인가 = 흔적만(플래그 ``adf-traces``, 변환 계속)."""
        return len(self.present) == 1

    @property
    def evidence_total(self) -> int:
        """근거 파일 수(계열 합)."""
        return sum(len(v) for v in self.families.values())

    def summary(self) -> AdfSummary:
        """프로필 · inventory 에 저장하는 요약(``AdfSummary``; 목록은 상한까지만).

        The capped pydantic summary stored on the profile / inventory."""
        return AdfSummary(
            is_adf=self.is_adf, traces=self.traces, families=self.present,
            evidence={f: list(v[:_CAP_PER_FAMILY]) for f, v in self.families.items() if v},
            evidence_counts={f: len(v) for f, v in self.families.items() if v}, evidence_total=self.evidence_total,
            hints=self.hints[:_CAP_HINTS], entities=self.entities, view_objects=self.view_objects,
            app_modules=self.app_modules, associations=self.associations, view_links=self.view_links,
            page_defs=self.page_defs, task_flows=self.task_flows, flow_views=self.flow_views, pages=self.pages,
            java_total=self.java_total, java_adf=self.java_adf, tables=sorted(self.tables)[:_CAP_TABLES],
            tables_total=len(self.tables), by_dir={d: dict(c) for d, c in self.by_dir.items()})


# ---------------------------------------------------------------------------------------------------- text
def counts_text(s: AdfSummary) -> str:
    """간결한 규모 문구(영어 원문): ``1 entities / 1 tables, 1 view objects, 1 application modules, 1 page definitions, 1 task
    flows, 2 pages, 2 of 3 java files on ADF``."""
    return (f"{s.entities} entities / {s.tables_total} tables, {s.view_objects} view objects, {s.app_modules} application "
            f"modules, {s.page_defs} page definitions, {s.task_flows} task flows, {s.pages} pages, {s.java_adf} of "
            f"{s.java_total} java files on ADF")


def _evidence_text(s: AdfSummary, per_family: int = 2) -> str:
    """``bc4j: a.xml, b.xml; java: C.java`` — 계열별 근거 경로 몇 개."""
    parts = []
    for f in FAMILIES:
        paths = (s.evidence or {}).get(f) or []
        if paths:
            more = f" (+{s.evidence_counts.get(f, len(paths)) - per_family})" if s.evidence_counts.get(f, 0) > per_family else ""
            parts.append(f"{f}: " + ", ".join(paths[:per_family]) + more)
    return "; ".join(parts)


def adf_followup(s: AdfSummary, policy: str = "stop") -> str:
    """"변환 불가" 첫 줄(영어 원문; ``sbmig.report.i18n.PATTERNS`` 가 한국어로 — 한국어는 ``ADF 프로젝트라 변환이 불가능합니다`` 로
    시작하고 괄호에 간결한 규모가 붙는다).

    The Handover's first line for a rejected ADF project (English; rendered in Korean by `i18n.PATTERNS`)."""
    tail = "" if policy == "stop" else " — intake.adf_policy: continue (report only, the run goes on; not recommended)"
    return (f"{ADF_PREFIX} {ADF_MESSAGE_KO} (evidence: {', '.join(s.families)}; {counts_text(s)}){tail}")


def traces_followup(s: AdfSummary) -> str:
    """흔적만 있을 때의 인수인계 메모(영어 원문): 계열 하나와 그 근거 경로, 규모 — 변환은 계속한다.

    The Handover note for ADF traces (one family only): the family and its evidence, the compact counts."""
    fam = s.families[0] if s.families else "?"
    return (f"{TRACES_PREFIX} (one evidence family only — {fam}: {', '.join((s.evidence or {}).get(fam, [])[:3])}) — "
            f"the conversion goes on but needs a check: {TRACES_MESSAGE_KO} ({counts_text(s)})")


def adf_abort_reason(policy: str = "stop") -> str:
    """``_abort`` 에 주는 짧은 사유(``run aborted: …`` 줄)."""
    return f"ADF project — conversion is not possible ({ADF_MESSAGE_KO}; intake.adf_policy: {policy})"


def adf_sizing_lines(s: AdfSummary) -> list[str]:
    """콘솔 · inventory 용 영어 요약 줄들: 판정, 규모, 계열별 근거.

    Plain English sizing lines for the console / inventory.md: verdict, counts, evidence per family."""
    verdict = ("ADF project — conversion is not possible" if s.is_adf else
               "ADF traces (one evidence family) — not rejected, check needed" if s.traces else "no ADF evidence")
    out = [f"ADF: {verdict}; families: {', '.join(s.families) or '-'}; {counts_text(s)}"]
    ev = _evidence_text(s, 3)
    if ev:
        out.append("  evidence: " + ev)
    if s.hints:
        out.append("  hints (not evidence): " + ", ".join(s.hints[:4]) + (" …" if len(s.hints) > 4 else ""))
    if s.tables:
        out.append("  tables: " + ", ".join(s.tables[:12]) + (f" … (+{s.tables_total - 12})" if s.tables_total > 12 else ""))
    return out


# ---------------------------------------------------------------------------------------------------- file probes
def _read_head(path: Path, nbytes: int) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(nbytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _xml_root_tag(head: str) -> tuple[str, str]:
    """XML 텍스트 머리의 루트 요소 이름(접두어 제외)과 시작 태그 속성 문자열 — 선언 · 주석 · DOCTYPE 을 건너뛴다.

    (root element local name, its attribute text) from the file head; ("", "") when there is none."""
    if not head:
        return "", ""
    text = re.sub(r"<\?.*?\?>", "", head, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    m = _ROOT_TAG_RE.search(text)
    return (m.group(1), m.group(2)) if m else ("", "")


def _attr(attrs: str, name: str) -> str:
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*"([^"]*)"', attrs) or \
        re.search(r"\b" + re.escape(name) + r"\s*=\s*'([^']*)'", attrs)
    return m.group(1).strip() if m else ""


def _count_tags(path: Path, tag: str, limit: int = _MAX_XML_BYTES) -> int:
    try:
        if path.stat().st_size > limit:
            return 0
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(re.findall(r"<\s*(?:[\w.-]+:)?" + re.escape(tag) + r"\b", text))


def _java_uses_adf(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = []
            for i, line in enumerate(fh):
                if i >= _JAVA_HEAD_LINES:
                    break
                lines.append(line)
    except OSError:
        return False
    return bool(ADF_IMPORT_RE.search("".join(lines)))


def _root_dir(ws: Any) -> Path:
    """``ws`` 가 경로면 그대로, Workspace / PathView 면 ``.root`` — ``PurePath.root``('/') 를 루트로 잘못 잡지 않게."""
    if isinstance(ws, (str, PurePath)):
        return Path(ws)
    return Path(getattr(ws, "root", ws))


# ---------------------------------------------------------------------------------------------------- the walk
def detect_adf(ws: Any) -> AdfInventory:
    """``ws.root`` (또는 경로)를 한 번 걸으며 네 계열의 근거와 개수를 모은다(``target/`` · 형상관리 · IDE 폴더 제외, jar 는 열지 않음).
    절대 예외를 올리지 않는다(``error`` 에 메모).

    Walk the tree once and collect the evidence families and counts. Never raises (`error` notes a failure)."""
    inv = AdfInventory()
    try:
        root = _root_dir(ws)
        if not root.is_dir():
            return inv
        seen = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE)
            rel_dir = posixpath.normpath(os.path.relpath(dirpath, root).replace("\\", "/"))
            rel_dir = "" if rel_dir == "." else rel_dir
            top = rel_dir.split("/", 1)[0] if rel_dir else "."
            for name in sorted(filenames):
                seen += 1
                if seen > _MAX_FILES:
                    inv.error = f"stopped scanning after {_MAX_FILES} files"
                    return inv
                rel = f"{rel_dir}/{name}" if rel_dir else name
                path = Path(dirpath) / name
                low = name.lower()
                counts = inv.by_dir.setdefault(top, _empty_counts())
                if low.endswith(".java"):
                    inv.java_total += 1
                    counts["java"] += 1
                    if _java_uses_adf(path):
                        inv.java_adf += 1
                        counts["java_adf"] += 1
                        inv.families["java"].append(rel)
                elif low.endswith(PAGE_EXT):
                    if FACES_NS in _read_head(path, _HEAD_BYTES):      # a plain JSPX is not ADF Faces
                        inv.pages += 1
                        counts["pages"] += 1
                        inv.families["faces"].append(rel)
                elif low == "adfm.xml" or low == "databindings.cpx":
                    inv.families["model"].append(rel)
                elif low == "adfc-config.xml":
                    inv.families["faces"].append(rel)
                    views = _count_tags(path, "view")
                    inv.flow_views += views
                    inv.task_flows += 1 if views or _count_tags(path, "control-flow-rule") else 0
                elif low == "adf-config.xml":
                    inv.hints.append(rel)
                elif low.endswith(".jpr"):
                    if JPR_ADF_RE.search(_read_head(path, 256 * 1024)):
                        inv.hints.append(rel)
                elif low.endswith(".xml"):
                    head = _read_head(path, _HEAD_BYTES)
                    tag, attrs = _xml_root_tag(head)
                    if low.endswith("pagedef.xml") and tag == "pageDefinition":
                        inv.page_defs += 1
                        counts["page_defs"] += 1
                        inv.families["model"].append(rel)
                    elif low.endswith("-flow.xml") and CONTROLLER_NS in attrs and _count_tags(path, "task-flow-definition"):
                        inv.task_flows += 1
                        inv.flow_views += _count_tags(path, "view")
                        inv.families["faces"].append(rel)
                    elif tag in BC4J_ROOTS and BC4J_NS in attrs:
                        inv.families["bc4j"].append(rel)
                        counts["bc4j"] += 1
                        if tag == "Entity":
                            inv.entities += 1
                            table = _attr(attrs, "DBObjectName")
                            if table and table not in inv.tables:
                                inv.tables.append(table)
                        elif tag == "ViewObject":
                            inv.view_objects += 1
                        elif tag == "AppModule":
                            inv.app_modules += 1
                        elif tag == "Association":
                            inv.associations += 1
                        else:
                            inv.view_links += 1
        for d in list(inv.by_dir):
            if not any(inv.by_dir[d].values()):
                del inv.by_dir[d]
    except Exception as e:  # noqa: BLE001 - the detector must never stop the run
        log.warning("ADF detection failed: %s: %s", type(e).__name__, e)
        inv.error = f"{type(e).__name__}: {e}"
    return inv
