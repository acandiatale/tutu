"""사내 프레임워크 아티팩트를 쓰는 프로젝트를 찾는 모듈. 분석 단계에서 호출한다.
찾으면 이관 범위를 JDK 21과 jakarta로 줄인다(``jdk-jakarta``). 재구성과 구조 검토는 하지 않는다.
- ``detect_framework``: 프로젝트 pom 선언, 부모 pom에서 상속한 의존성(as-is effective pom), 트리의 jar를 검사한다
- ``transitive_evidence``: 다른 라이브러리가 가져오는지 ``mvn dependency:tree``로 한 번 확인한다
- ``decide_scope``: 찾은 근거와 ``framework.*`` 설정으로 실행 범위를 정한다
- 예외를 던지지 않는다. 확인하지 못한 곳은 ``unchecked``에 남긴다

Reduced scope `jdk-jakarta`: Java 21 + jakarta recipes, the verify build (the fix loop only with
`framework.fix_loop: true`), the chained remediation; parents and the framework version kept.
Contract: docs/INTERFACES.md "v2.1z-ak".

Evidence sources, in this order: `direct` (a project pom's `<dependencies>`; a `<dependencyManagement>` entry alone is
no evidence), `parent` (a module's effective `<dependencies>` in the as-is capture `logs/effective-depmgmt.json` lists
it and no project pom declares it: inherited from a parent outside the project), `bundled` (a jar in the tree no pom
declares, by coordinates or by file name) and `transitive` (`mvn dependency:tree -Dincludes=…`, one Maven call the
orchestrator makes only when nothing else was found). The run has ONE scope, also for an MSA.
"""
from __future__ import annotations

import logging
import posixpath
from typing import Any, Optional

from sbmig.core.models import FrameworkEvidence, FrameworkUse

log = logging.getLogger("sbmig.analyze.framework")

FLAG = "framework-project"
_ORDER = {"direct": 0, "parent": 1, "bundled": 2, "transitive": 3}
_NO_CAPTURE = "parent: no as-is effective pom"


def _fw(cfg: Any) -> Any:
    return getattr(cfg, "framework", None)


def _add(out: list[FrameworkEvidence], seen: set[tuple[str, str, str]], ev: FrameworkEvidence) -> None:
    """근거를 목록에 넣는다. 같은 (출처, 아티팩트, 위치)가 이미 있으면 넣지 않는다."""
    k = (ev.source, ev.artifact, ev.where)
    if k not in seen:
        seen.add(k)
        out.append(ev)


def _clean_version(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v or None


def _capture_missing(effective: Any) -> bool:
    if not isinstance(effective, dict):
        return True
    return not (effective.get("modules") or effective.get("direct") or effective.get("managed")
                or effective.get("by_module"))


def _load_capture(ws: Any) -> Optional[dict]:
    """접수 때 저장한 as-is effective pom을 읽는다. 파일이 없으면 None을 반환한다."""
    from sbmig.build.effective import DEPMGMT_FILE, _effective_path, load_effective
    if getattr(ws, "logs_dir", None) is None and getattr(ws, "run_dir", None) is None:
        return None                       # 폴더를 그대로 분석할 때(PathView)는 실행 디렉토리가 없다
    if not _effective_path(ws, DEPMGMT_FILE).is_file():
        return None
    return load_effective(ws)


def detect_framework(ws: Any, cfg: Any, poms: list, bundled_jars: list,
                     effective: Optional[dict] = None) -> Optional[FrameworkUse]:
    """프로젝트 pom, 부모 pom 상속, 트리의 jar에서 사내 프레임워크 아티팩트를 찾는다.
    ``framework.artifacts``가 비어 있으면 None을 반환한다. Maven은 실행하지 않고 예외를 던지지 않는다.
    읽지 못한 출처는 ``unchecked``에 ``"<출처>: <이유>"``로 남긴다.

    `effective` = the as-is depmgmt dict (tests); None reads the run's capture."""
    fw = _fw(cfg)
    if fw is None or not fw.enabled:
        return None
    use = FrameworkUse(patterns=fw.patterns())
    evidence: list[FrameworkEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    declared: set[str] = set()
    pom_of: dict[str, str] = {}

    # direct: 프로젝트 pom의 <dependencies> 선언만 근거로 본다
    try:
        for p in poms or []:
            if p.group_id and p.artifact_id:
                pom_of.setdefault(f"{p.group_id}:{p.artifact_id}", p.path)
            for d in p.dependencies:
                declared.add(d.key)
                if fw.matches(d.group_id, d.artifact_id):
                    _add(evidence, seen, FrameworkEvidence(source="direct", artifact=d.key,
                                                           version=_clean_version(d.version), where=p.path))
    except Exception as e:  # noqa: BLE001 - 이 검사 때문에 분석이 실패하면 안 된다
        use.unchecked.append(f"direct: {type(e).__name__}: {e}")

    # parent: effective <dependencies>에 있는데 어느 프로젝트 pom도 선언하지 않았으면 프로젝트 밖 부모에서 상속한 것이다
    try:
        data = effective if effective is not None else _load_capture(ws)
        if _capture_missing(data):
            use.unchecked.append(_NO_CAPTURE)
        else:
            by_module = data.get("by_module") or {}
            units: list[tuple[str, dict, Optional[str]]] = []
            if by_module:
                for mod in (data.get("modules") or list(by_module)):
                    own = by_module.get(mod)
                    if isinstance(own, dict):
                        units.append((pom_of.get(mod, mod), own.get("direct") or {}, own.get("parent")))
            else:
                mods = data.get("modules") or []
                where = pom_of.get(mods[0], mods[0]) if mods else "pom.xml"
                units.append((where, data.get("direct") or {}, data.get("parent")))
            for where, direct, parent in units:
                for key, ver in direct.items():
                    if fw.matches_key(key) and key not in declared:
                        _add(evidence, seen, FrameworkEvidence(source="parent", artifact=key,
                                                               version=_clean_version(ver), where=where,
                                                               parent=parent or None))
    except Exception as e:  # noqa: BLE001
        use.unchecked.append(f"parent: {type(e).__name__}: {e}")

    # bundled: 어떤 pom도 선언하지 않은 트리의 jar
    try:
        if not getattr(getattr(cfg, "intake", None), "bundled_jars", True):
            use.unchecked.append("bundled: intake.bundled_jars is off")
        for j in bundled_jars or []:
            if j.key:
                if fw.matches(j.group_id, j.artifact_id):
                    _add(evidence, seen, FrameworkEvidence(source="bundled", artifact=j.key,
                                                           version=_clean_version(j.version), where=j.path))
            elif fw.matches_jar(j.path):
                name = j.path.replace("\\", "/").rsplit("/", 1)[-1]
                _add(evidence, seen, FrameworkEvidence(source="bundled", artifact=name,
                                                       version=_clean_version(j.version), where=j.path))
    except Exception as e:  # noqa: BLE001
        use.unchecked.append(f"bundled: {type(e).__name__}: {e}")

    use.evidence = sorted(evidence, key=lambda e: _ORDER[e.source])
    return use


def _reach(profile: Any, start: str) -> set[str]:
    by_path = {p.path: p for p in profile.poms}
    seen: set[str] = set()
    todo = [start]
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in by_path:
            continue
        seen.add(cur)
        for m in by_path[cur].modules:
            t = posixpath.normpath(posixpath.join(posixpath.dirname(cur) or ".", m))
            if not t.endswith(".xml"):
                t = posixpath.join(t, "pom.xml")
            todo.append(posixpath.normpath(t))
    return seen


def _tree_targets(ws: Any, profile: Any) -> Optional[list[tuple[Optional[str], bool]]]:
    """``dependency:tree``를 실행할 대상(모듈 디렉토리, 단독 실행 여부) 목록을 정한다. 루트 pom이 없으면 None을 반환한다.
    MSA에서 루트 집계 pom이 모든 서비스를 묶지 않으면 서비스마다 따로 실행한다."""
    services = list(getattr(profile, "services", None) or [])
    rp = getattr(profile, "root_pom", None)
    case = getattr(getattr(profile, "case_type", None), "value", "")
    if case == "msa" and services:
        covered = rp is not None and rp.is_aggregator and all(s.root_pom in _reach(profile, rp.path)
                                                               for s in services)
        if not covered:
            return [(posixpath.dirname(s.root_pom) or None, bool(posixpath.dirname(s.root_pom)))
                    for s in services]
    if ws.exists("pom.xml"):
        return [(None, False)]
    if rp is not None:
        d = posixpath.dirname(rp.path)
        return [(d or None, bool(d))]
    return None


def transitive_evidence(ws: Any, cfg: Any, profile: Any) -> tuple[list[FrameworkEvidence], Optional[str]]:
    """``mvn dependency:tree``로 다른 라이브러리가 프레임워크 아티팩트를 가져오는지 확인한다.
    트리를 읽었으면 ``(근거, None)``을, 읽지 못했으면 ``([], 이유)``를 반환한다. 예외를 던지지 않는다.

    One `dependency:tree -Dincludes=<framework patterns>` per target. A matching node at depth 1 is
    reported as `direct` (a profile or property the static parse missed), deeper nodes as `transitive` with
    `via` = the g:a:v path from the module's own dependency down to the framework artifact. Some targets
    failing still returns the evidence found, with the joined reasons."""
    try:
        fw = _fw(cfg)
        if fw is None or not fw.enabled:
            return [], "framework.artifacts is empty"
        targets = _tree_targets(ws, profile)
        if not targets:
            return [], "no root pom"
        from sbmig.build import deptree as _deptree

        pom_of = {f"{p.group_id}:{p.artifact_id}": p.path for p in (getattr(profile, "poms", None) or [])
                  if p.group_id and p.artifact_id}
        evidence: list[FrameworkEvidence] = []
        seen: set[tuple[str, str, str]] = set()
        whys: list[str] = []
        read = 0
        for module, standalone in targets:
            roots, why = _deptree.dependency_tree_status(ws, cfg, module, fw.maven_includes(), standalone=standalone)
            if why:
                whys.append(why)
                continue
            read += 1
            paths = _deptree.paths_to(roots, lambda n: fw.matches(n.gav.group_id, n.gav.artifact_id)
                                      and not n.omitted)
            for path in sorted(paths, key=len):
                root, leaf = path[0], path[-1]
                where = pom_of.get(root.gav.key, root.gav.key)
                if len(path) == 2:
                    ev = FrameworkEvidence(source="direct", artifact=leaf.gav.key, version=leaf.gav.version,
                                           where=where)
                else:
                    ev = FrameworkEvidence(source="transitive", artifact=leaf.gav.key, version=leaf.gav.version,
                                           where=where, via=[n.gav.coordinates for n in path[1:]])
                _add(evidence, seen, ev)
        evidence.sort(key=lambda e: _ORDER[e.source])
        if not read:
            return [], "; ".join(whys) or "dependency:tree failed"
        return evidence, ("; ".join(whys) if whys else None)
    except Exception as e:  # noqa: BLE001 - 이 확인 때문에 실행이 실패하면 안 된다
        log.warning("framework transitive check: %s: %s", type(e).__name__, e)
        return [], f"{type(e).__name__}: {e}"


def _applies(e: FrameworkEvidence, fw: Any) -> bool:
    return e.source != "transitive" or getattr(fw, "transitive", "apply") == "apply"


def _one(e: FrameworkEvidence) -> str:
    head = e.artifact + (f" {e.version}" if e.version else "")
    if e.source == "parent":
        tail = f"parent: {e.where}" + (f", from {e.parent}" if e.parent else "")
    elif e.source == "transitive":
        tail = f"transitive: {e.where}" + (f" via {e.via[0]}" if e.via else "")
    else:
        tail = f"{e.source}: {e.where}"
    return f"{head} ({tail})"


def _summary(evidence: list[FrameworkEvidence]) -> str:
    if not evidence:
        return "no framework artifact found"
    more = len(evidence) - 1
    return _one(evidence[0]) + (f" and {more} more" if more else "")


def decide_scope(use: Optional[FrameworkUse], cfg: Any) -> Optional[FrameworkUse]:
    """찾은 근거와 ``framework.scope``, ``framework.transitive`` 설정으로 실행 범위를 정한다.
    ``use``가 None이면 None을 반환한다. 여러 번 호출해도 결과가 같다.
    - ``framework.scope``가 ``jdk-jakarta``이고 적용되는 근거가 있으면 ``jdk-jakarta``다
    - transitive 근거는 ``framework.transitive=apply``일 때만 적용된다
    - 그 밖에는 ``full``이다

    `reason` = one English sentence; `framework_followup` reads `framework.scope=full` /
    `framework.transitive=` back out of it."""
    if use is None:
        return None
    fw = _fw(cfg)
    scope_cfg = getattr(fw, "scope", "jdk-jakarta")
    applying = [e for e in use.evidence if _applies(e, fw)]
    if not use.evidence:
        scope, reason = "full", "no framework artifact found → full migration"
    elif scope_cfg == "full":
        scope, reason = "full", f"framework artifact {_summary(use.evidence)} found; framework.scope=full → full migration"
    elif applying:
        scope, reason = "jdk-jakarta", f"framework artifact {_summary(applying)} found → scope jdk-jakarta"
    else:
        scope = "full"
        reason = (f"framework artifact {_summary(use.evidence)} found only as a transitive dependency and "
                  f"framework.transitive={getattr(fw, 'transitive', 'note')} → full migration")
    # 화면과 보고서가 읽는 이름과 수정 루프 여부를 여기서 함께 정한다
    name = fw.display_name(use.keys()) if use.evidence and hasattr(fw, "display_name") else ""
    return use.model_copy(update={"scope": scope, "reason": reason, "name": name,
                                  "fix_loop": bool(getattr(fw, "fix_loop", False))})


def summary_text(use: FrameworkUse) -> str:
    """찾은 프레임워크 아티팩트를 영어 한 줄로 요약한다. 계획 노트와 로그에 쓴다.

    `com.example.fw:example-fw-core 1.2.0 (direct: pom.xml)`, `… and N more` for several entries."""
    return _summary(list(use.evidence))


def _dir_of(path: str) -> str:
    d = posixpath.dirname((path or "").replace("\\", "/"))
    return d or "프로젝트 루트"


def _ko_one(e: FrameworkEvidence) -> str:
    """근거 하나를 보고서용 한국어 문구로 만든다. 값 뒤에는 받침과 관계없는 조사만 붙인다."""
    if e.source == "bundled" and e.artifact.count(":") != 1:
        return f"{e.artifact}, {_dir_of(e.where)}에 jar로 포함"
    head = e.artifact + (f" {e.version}" if e.version else "")
    if e.source == "direct":
        return f"{head}, {e.where}에 직접 선언"
    if e.source == "parent":
        return f"{head}, 부모 pom {e.parent}에서 상속" if e.parent else f"{head}, 부모 pom에서 상속"
    if e.source == "bundled":
        return f"{head}, {_dir_of(e.where)}에 jar로 포함"
    return f"{head}, {e.via[0]}의 의존성" if e.via else f"{head}, 다른 라이브러리의 의존성"


def _ko_desc(evidence: list[FrameworkEvidence]) -> str:
    more = len(evidence) - 1
    return _ko_one(evidence[0]) + (f" 외 {more}곳" if more > 0 else "")


NO_FIX_LOOP_PREFIX = "framework project: build errors are not fixed automatically (framework.fix_loop: false)"
NO_FIX_LOOP_NOTE = "not tried: framework project — build errors are not fixed automatically (framework.fix_loop: false)"


def no_fix_loop_followup(errors: int) -> str:
    """수정 루프 없이 빌드가 실패한 프레임워크 프로젝트의 인수인계 첫 줄을 영어로 만든다.
    ``errors``는 빌드가 남긴 오류 수다. 0이면 오류를 파싱하지 못한 실패로 보고 빌드 로그를 보라고 쓴다."""
    if errors > 0:
        return f"{NO_FIX_LOOP_PREFIX} — {errors} error(s) remain after the JDK 21 + jakarta conversion; see the error list"
    return f"{NO_FIX_LOOP_PREFIX} — the build failed after the JDK 21 + jakarta conversion; see the build log"


def framework_followup(use: FrameworkUse) -> str:
    """보고서 인수인계 섹션에 넣을 한국어 한 줄을 만든다. 찾은 근거가 없으면 빈 문자열을 반환한다.
    근거가 여럿이면 첫 근거만 쓰고 ``외 N곳``을 붙인다. 값 뒤에는 받침과 관계없는 조사만 붙인다."""
    if not use.evidence:
        return ""
    if use.reduced:
        who = f"{use.name} 프레임워크 프로젝트" if use.name else "사내 프레임워크 프로젝트"
        return (f"{who}입니다({_ko_desc(list(use.evidence))}). JDK 21과 Jakarta만 적용했고 구조 정리와 구조 검토는 "
                "하지 않았습니다. 부모 pom과 프레임워크 버전은 그대로입니다.")
    if "framework.scope=full" in (use.reason or ""):
        return (f"사내 프레임워크 아티팩트를 사용합니다({_ko_desc(list(use.evidence))}). "
                "설정(framework.scope: full)에 따라 전체 이관을 진행했습니다.")
    setting = "note"
    if "framework.transitive=" in (use.reason or ""):
        setting = use.reason.split("framework.transitive=", 1)[1].split()[0]
    return (f"다른 라이브러리가 사내 프레임워크 아티팩트를 가져옵니다({_ko_desc(list(use.evidence))}). "
            f"설정(framework.transitive: {setting})에 따라 전체 이관을 진행했습니다.")
