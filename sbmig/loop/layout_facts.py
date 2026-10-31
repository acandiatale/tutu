"""재구성 뒤 프로젝트 사실(v2.1v, 2026-10-31 프롬프트 검토 B-H2) — 수정 프롬프트의 "Project after the restructure" 절.
왜: 수정 프롬프트의 머리말은 분석 시점(as-is) 프로필만 보여 줘서(Java 1.8, Spring 5.3, 모듈 1개) LLM 이 sbmig 가 이미
Boot 애플리케이션으로 바꿨다는 것, boot / back / front 모듈과 그 부모·패키징, 생성한 파일(Application,
``legacy-config/`` 사본, WebXmlRegistrations), 사내 아티팩트 규칙을 몰랐다. 흐름상 위치: 오케스트레이터가 재구성 결과
(``RestructureResult``)를 루프에 넘기고(``BuildFixLoop.restructure_results``), 루프가 LLM 요청마다 이 함수로 줄을 만들어
``FixContext.layout_facts`` 에 넣는다(패커가 순서 0.5 로, 자르지 않고 렌더). 지킬 것: 값은 전부 재구성 결과와
``cfg.artifacts`` / ``cfg.layout`` 에서 — 코드에 좌표·버전 리터럴 없음; 전체 ~1,500 자(목록마다 개수 상한, 넘치면 우선순위가
낮은 줄부터 뺌); Boot as-is / MSA 의 pom 전용 모드는 부모·필수·BOM 줄만; 예외 없음(사실 하나 때문에 수정이 깨지면 안 된다).

v2.1v (docs/INTERFACES.md "v2.1v — the facts reach the LLM"): the lines of `FixContext.layout_facts` built from the
run's `RestructureResult`s and the configuration. Each line appears only when its value exists:

* `converted by sbmig: <case> → Spring Boot application (company layout)` (a Boot as-is project split by
  `layout.boot_as_is: layout`: `restructured by sbmig: …`);
* `boot module <dir>: artifactId <a>, packaging <jar | war (executable, java -jar)>, parent <g:a:v>, starter <s>`;
* `back module <dir>: every source incl. controllers, parent <g:a:v>`; `front module <dir>: static under <dir>, JSPs
  under <dir>`;
* `generated: <Application FQCN>, <dir>/legacy-config/ (N XML imported via @ImportResource: …), <other FQCNs>`;
* `XML not imported: …`, `legacy properties written: …`, `scopes restored: …`, `source encodings written: …`;
* `company artifacts: root parent <g:a:v>; …; mandatory <keys> declared in <root | modules>; optional in-house artifacts
  only where already used; versions managed by the parent chain — never write a literal for them`;
* `BOM imported: …`, `parents set: …` (≤ 6), `- <key>: <value>` per `conversion_facts` entry.

Boot as-is / MSA pom-only results (`boot_pom` / `msa_pom`) give the company-artifact, BOM and parents lines only.
"""
from __future__ import annotations

import logging
import posixpath
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger("sbmig.loop")

LAYOUT_FACTS_CHARS = 1500            # the whole section (the packer never truncates it): ~1,200 asked; a real
                                     # company layout (front paths, two mandatory g:a) needs ~1,300 for the lists
LIST_ITEMS = 4                       # items per list line (`XML not imported: …`, `scopes restored: …`)
ITEM_CHARS = 140                     # one item of such a list
PARENTS_SET_MAX = 6                  # `parents set: …` entries
BOM_IMPORTS_MAX = 3
CONVERSION_FACTS_MAX = 6
POM_ONLY_MODES = frozenset({"boot_pom", "msa_pom"})   # Boot as-is / MSA: directories kept, poms only
BOOT_CASES = frozenset({"spring_boot", "msa"})
JAVA_ROOT = "/src/main/java/"
LEGACY_DIR = "legacy-config/"


def _short(text: Any, limit: int = ITEM_CHARS) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _items(values: Iterable[Any], cap: int = LIST_ITEMS) -> str:
    """목록 줄의 값: 앞의 ``cap`` 개(각 ``ITEM_CHARS`` 자) + 남은 개수.

    The first `cap` items (each capped) joined with "; ", plus "(+N more)"."""
    vals = [_short(v) for v in values if str(v or "").strip()]
    text = "; ".join(vals[:cap])
    return text + (f" (+{len(vals) - cap} more)" if len(vals) > cap else "")


def spec_gav(spec: Any) -> str:
    """설정 스펙(ArtifactSpec)의 ``g:a:v`` (버전이 없으면 ``g:a``) — 프롬프트에 쓰는 좌표는 언제나 설정에서.

    `g:a:v` of a configured ArtifactSpec (`g:a` without a version); "" for None."""
    if spec is None:
        return ""
    g, a, v = (str(getattr(spec, k, "") or "") for k in ("group_id", "artifact_id", "version"))
    if not g or not a:
        return ""
    return f"{g}:{a}:{v}" if v else f"{g}:{a}"


def _new_parents(res: Any) -> dict[str, str]:
    """``parents_set`` 의 ``<pom>: <옛> → <새>`` 에서 pom → 새 부모(g:a:v)."""
    out: dict[str, str] = {}
    for entry in list(getattr(res, "parents_set", None) or []):
        pom, sep, rest = str(entry).partition(": ")
        if not sep or "→" not in rest:
            continue
        out[pom.strip()] = rest.rsplit("→", 1)[-1].strip()
    return out


def _mode_of(res: Any, mode: str | None) -> str:
    """결과의 재구성 모드(layout / boot_pom / msa_pom). 모르면 결과로 짐작: boot 모듈에 생성된 ``.java`` 가 있으면
    layout(Application 을 만든 것은 레이아웃 분할뿐), 아니면 pom 전용."""
    if mode:
        return str(mode)
    gen = [str(f) for f in (getattr(res, "generated_files", None) or [])]
    return "layout" if any(f.endswith(".java") and JAVA_ROOT in "/" + f for f in gen) else "pom_only"


def _fqcn(rel: str) -> str:
    """``<module>/src/main/java/com/x/App.java`` → ``com.x.App``."""
    tail = ("/" + rel).split(JAVA_ROOT, 1)[1]
    return tail[: -len(".java")].replace("/", ".")


def _generated_line(res: Any, cfg: Any) -> str:
    """생성 파일 줄: Application FQCN 먼저, ``legacy-config/`` 사본(@ImportResource 목록), 그 밖의 생성 클래스."""
    gen = [str(f) for f in (getattr(res, "generated_files", None) or [])]
    java = [_fqcn(f) for f in gen if f.endswith(".java") and JAVA_ROOT in "/" + f]
    app_name = str(getattr(getattr(cfg, "layout", None), "application_class_name", "") or "")
    app = [x for x in java if app_name and x.rsplit(".", 1)[-1].endswith(app_name)][:1]
    others = [x for x in java if x not in app]
    imports = [str(x) for x in (getattr(res, "xml_imports", None) or [])]
    copies = [f for f in gen if LEGACY_DIR in f and f.lower().endswith(".xml")]
    names = [x.split(LEGACY_DIR, 1)[1] if LEGACY_DIR in x else posixpath.basename(x) for x in (imports or copies)]
    parts = list(app)
    if names:
        where = (copies[0].split(LEGACY_DIR, 1)[0] + LEGACY_DIR) if copies else LEGACY_DIR
        parts.append(f"{where} ({len(names)} XML imported via @ImportResource: {_items(names, 6)})")
    parts += others[:LIST_ITEMS]
    return ("generated: " + ", ".join(parts)) if parts else ""


def _company_line(cfg: Any) -> str:
    """사내 아티팩트 규칙 한 줄(값은 ``cfg.artifacts`` 에서만)."""
    A = getattr(cfg, "artifacts", None)
    if A is None:
        return ""
    parts: list[str] = []
    root = spec_gav(getattr(A, "parent", None))
    if root:
        parts.append(f"root parent {root}")
    boot, comp = spec_gav(getattr(A, "boot_parent", None)), spec_gav(getattr(A, "comp_parent", None))
    if boot and comp:
        parts.append(f"role parents {boot} (boot modules) / {comp} (every other module)")
    mand = [str(getattr(s, "key", "") or "") for s in (getattr(A, "mandatory", None) or [])]
    mand = [k for k in mand if k]
    if mand:
        target = str(getattr(A, "mandatory_target", "root") or "root")
        parts.append(f"mandatory {', '.join(mand)} declared in {'root' if target == 'root' else 'modules'}")
    parts.append("optional in-house artifacts only where already used")
    parts.append("versions managed by the parent chain — never write a literal for them")
    return "company artifacts: " + "; ".join(parts)


def _layout_lines(res: Any, cfg: Any, case: str, read_artifact_id: Callable[[str], str] | None
                  ) -> list[tuple[int, str]]:
    """회사 레이아웃(분할) 결과의 줄들 — (우선순위, 줄). 낮은 수가 먼저 남는다."""
    L = getattr(cfg, "layout", None)
    parents = _new_parents(res)
    A = getattr(cfg, "artifacts", None)
    out: list[tuple[int, str]] = []
    boot = getattr(res, "boot_module", None)
    comp = getattr(res, "comp_module", None)
    front = getattr(res, "front_module", None)
    who = case or "the project"
    if case in BOOT_CASES:
        roles = " / ".join(x for x, d in (("boot", boot), ("back", comp), ("front", front)) if d)
        out.append((0, f"restructured by sbmig: {who} → company layout ({roles or 'modules'})"))
    else:
        out.append((0, f"converted by sbmig: {who} → Spring Boot application (company layout)"))

    def parent_of(module: str, spec: Any) -> str:
        return parents.get(f"{module}/pom.xml") or spec_gav(spec)

    if boot:
        bits: list[str] = []
        aid = ""
        if read_artifact_id is not None:
            try:
                aid = str(read_artifact_id(f"{boot}/pom.xml") or "")
            except Exception as e:  # noqa: BLE001 - a fact line never breaks a fix
                log.debug("artifactId of %s unavailable: %s", boot, e)
        if aid:
            bits.append(f"artifactId {aid}")
        pk = str(getattr(res, "boot_packaging", "") or "")
        if pk:
            bits.append("packaging " + ("war (executable, java -jar)" if pk == "war" else pk))
        par = parent_of(boot, getattr(A, "boot_parent", None) if A is not None else None)
        if par:
            bits.append(f"parent {par}")
        starter = str(getattr(res, "starter", "") or "")
        if starter:
            bits.append(f"starter {starter}")
        out.append((0, f"boot module {boot}: " + ", ".join(bits) if bits else f"boot module {boot}"))
    if comp:
        par = parent_of(comp, getattr(A, "comp_parent", None) if A is not None else None)
        out.append((1, f"back module {comp}: every source incl. controllers" + (f", parent {par}" if par else "")))
    if front:
        static = str(getattr(L, "front_static_dir", "") or "")
        jsp = str(getattr(L, "front_jsp_dir", "") or "")
        bits = [x for x in ((f"static under {front}/{static}" if static else ""),
                            (f"JSPs under {front}/{jsp}" if jsp else "")) if x]
        out.append((1, f"front module {front}: " + ", ".join(bits) if bits else f"front module {front}"))
    gen = _generated_line(res, cfg)
    if gen:
        out.append((1, gen))
    for prio, label, attr in ((3, "XML not imported", "xml_skipped"),
                              (3, "legacy properties written", "legacy_properties_written"),
                              (3, "scopes restored", "scope_restored"),
                              (3, "source encodings written", "source_encodings_written")):
        vals = list(getattr(res, attr, None) or [])
        if vals:
            out.append((prio, f"{label}: {_items(vals)}"))
    return out


def _pom_lines(res: Any, layout: bool = False) -> list[tuple[int, str]]:
    """모든 모드에 공통인 pom 줄: BOM import, 부모 교체(레이아웃 모드면 boot / back / front 줄이 이미 말한 모듈 pom 은 뺀다)."""
    out: list[tuple[int, str]] = []
    boms = list(getattr(res, "bom_imports", None) or [])
    if boms:
        out.append((2, f"BOM imported: {_items(boms, BOM_IMPORTS_MAX)}"))
    sets = list(getattr(res, "parents_set", None) or [])
    if layout:
        shown = {f"{m}/pom.xml" for m in (getattr(res, "boot_module", None), getattr(res, "comp_module", None),
                                          getattr(res, "front_module", None)) if m}
        sets = [x for x in sets if str(x).partition(": ")[0].strip() not in shown]
    if sets:
        out.append((3, f"parents set: {_items(sets, PARENTS_SET_MAX)}"))
    return out


def _conversion_lines(res: Any) -> list[tuple[int, str]]:
    facts = getattr(res, "conversion_facts", None) or {}
    out: list[tuple[int, str]] = []
    if not isinstance(facts, dict):
        return out
    for key, value in list(facts.items())[:CONVERSION_FACTS_MAX]:
        if value in (None, "", [], {}):
            continue
        text = _items(value, LIST_ITEMS) if isinstance(value, (list, tuple, set)) else _short(value)
        out.append((4, f"- {key}: {text}"))
    return out


def _fit(lines: list[tuple[int, str]], budget: int) -> list[str]:
    """우선순위가 높은 줄(작은 수)부터 예산 안에 담고, 원래 순서로 돌려준다. 넘치는 줄은 남은 자리가 넉넉하면 잘라
    넣고(… 표시), 아니면 뺀다.

    Keep the most important lines (lowest priority number first) within `budget` characters, in display order."""
    keep: dict[int, str] = {}
    used = 0
    for i in sorted(range(len(lines)), key=lambda k: (lines[k][0], k)):
        text = lines[i][1]
        cost = len(text) + 1
        if used + cost <= budget:
            keep[i], used = text, used + cost
        elif budget - used > 80:
            keep[i] = text[: budget - used - 2] + "…"
            used = budget
    return [keep[i] for i in sorted(keep)]


def layout_fact_lines(results: Iterable[Any], cfg: Any, *, modes: list[str | None] | None = None,
                      case: str = "", read_artifact_id: Callable[[str], str] | None = None,
                      budget: int = LAYOUT_FACTS_CHARS) -> list[str]:
    """재구성 결과들 → "Project after the restructure" 절의 줄들(모듈 docstring 의 목록, 예산 ``budget`` 자). 결과가
    없거나 모두 실패했으면 []. ``modes`` = 결과마다 재구성 모드(layout / boot_pom / msa_pom; 모르면 결과로 짐작),
    ``case`` = as-is 프로젝트 종류(``spring_legacy`` …), ``read_artifact_id(pom 경로)`` = 모듈 pom 의 artifactId(없으면
    그 칸 생략). 예외 없음.

    The `FixContext.layout_facts` lines for the run's restructure results (docs/INTERFACES.md v2.1v). `modes` gives
    each result's restructure mode (`layout` / `boot_pom` / `msa_pom`; inferred when missing); pom-only results give
    the company-artifact / BOM / parents lines only. [] without a usable result. Never raises."""
    try:
        res_list = [r for r in (results or []) if r is not None and getattr(r, "ok", True)]
        if not res_list:
            return []
        mode_list = list(modes or [])
        lines: list[tuple[int, str]] = []
        company_done = False
        for i, res in enumerate(res_list):
            mode = _mode_of(res, mode_list[i] if i < len(mode_list) else None)
            if mode not in POM_ONLY_MODES and mode != "pom_only":
                lines += _layout_lines(res, cfg, str(case or ""), read_artifact_id)
            if not company_done:
                company = _company_line(cfg)
                if company:
                    lines.append((0, company))
                company_done = True
            lines += _pom_lines(res, layout=mode not in POM_ONLY_MODES and mode != "pom_only")
            if mode not in POM_ONLY_MODES and mode != "pom_only":
                lines += _conversion_lines(res)
        seen: set[str] = set()
        uniq = []
        for prio, text in lines:
            if text and text not in seen:
                seen.add(text)
                uniq.append((prio, text))
        return _fit(uniq, budget)
    except Exception as e:  # noqa: BLE001 - a fact section never breaks a fix attempt
        log.debug("layout facts unavailable: %s: %s", type(e).__name__, e)
        return []
