"""규칙 기반 계획기(LLM 없음, v2) — ProjectProfile + Config → MigrationPlan(실행할 단계 목록).
흐름상 위치: 분석 다음 계획 단계. 오케스트레이터가 ``build_plan`` 을 부르고 단계들을 차례로 실행한다. 단계(MSA
아님): ``transform.rewrite``(변환, OpenRewrite 1회) → ``transform.compile``(변환 후 컴파일 — 오류를 목록에
적기만, 고치지 않음) → ``restructure.apply`` | ``restructure.boot_pom`` | ``restructure.pom_only`` (재구성) →
``restructure.review``(구조 검토) → ``verify.build``(검증 빌드, 실패하면 수정 루프). 레시피는 Java 21 /
jakarta / Boot 4 세트만 계획한다 — 현업 결정(2026-10-26) "레시피가 무엇을 바꿀지 모르니 넣지 않는다". 그 밖의
문제는 빌드 오류로 두고 수정 루프의 스크립트가 고쳐 KB 에 남긴다. 중간 빌드도 없다. MSA 는 서비스·공유
라이브러리 루트별 단계(라이브러리 먼저). 권고는 계획 노트 한 줄씩이다(안내만, 레시피가 되지 않는다).

Rule-based migration planner (no LLM), v2 (flow-v2): ProjectProfile + Config → MigrationPlan.

Step ids (non-MSA): transform.rewrite, transform.compile, restructure.apply | restructure.boot_pom |
restructure.pom_only, restructure.review, verify.build. The only build before verify.build is v2.1's one compile
(recorded, never fixed); verify.build's fix loop (`loop.phases.verify`) fixes the errors.

* `transform.rewrite` (StepKind.OPENREWRITE, Phase.TRANSFORM): ONE OpenRewrite run on the as-is classpath. Recipes,
  in this order: `openrewrite.recipes.jdk21`; `jakarta` (spring_legacy and servlet_tomcat always, pure_java only with
  Java EE javax imports, never spring_boot — the Boot umbrella migrates jakarta itself); `spring_framework_7` /
  `spring_security_7` (spring_legacy; security only with spring-security) — the framework set is empty by default, the
  security set is the 6_0 … 6_5 → 7_0 chain since v2.1l (2026-10-30, planned only when the analyzer found
  `spring-security`); `spring_boot_4`
  (spring_boot); `junit5` (junit4 detected) — empty by default; `extra.<case>` + `extra.all`. OpenRewrite recipes
  only: no project, library or KB recipes (business decision, PLAN-v2 §2).
* `transform.compile` (StepKind.BUILD, Phase.TRANSFORM, `mvn clean compile`, v2.1 — "OpenRewrite 를 다 돌린 뒤
  compile 을 한 번"): every error goes on the run's error list (seen `compile`), nothing is fixed. Planned whenever the
  project has java sources (also when no recipe applied). MSA: one at the root aggregator, else
  `transform.compile@<x>` per target (like verify).
* Restructure (unchanged from v1, round 9c routing): `case_type == spring_boot` with `layout.boot_as_is ==
  "in-place"` (default) plans `restructure.boot_pom` (StepKind.MSA_POM, params {"mode": "boot_pom", "boot_modules"})
  — directories kept, poms only; `boot_as_is == "layout"` and every non-Boot case plan `restructure.apply`
  (StepKind.RESTRUCTURE, params {"mode": "layout", "starter"}: `{name}-boot` / `{name}-back` / `{name}-front`).
  The in-house optional artifacts are bumped inside the restructure step; v2 never plans `spring.optional_bump`.
* `restructure.review` (StepKind.REVIEW, Phase.RESTRUCTURE, params {"restructure_step": <id>}): the LLM structure
  review of poms / dependencies / modules right after the restructure (business rule, PLAN-v2 §1.4); omitted with a
  note when `review.enabled` is false.
* `verify.build` (StepKind.BUILD, Phase.VERIFY, `mvn clean package`).
* MSA: `transform.rewrite@<x>` per target (params `service`, target = its directory) — shared **library roots
  first** (dependency / reactor order; never the Boot set), then the services with their own sub-case; then
  `restructure.msa_pom` (params `services`, `parent_policy`, `boot_modules`, `library_roots`), `restructure.review`
  and `verify.build` (or `verify.build@<x>` per target when there is no root aggregator). With
  `layout.msa_parent_policy == "top-aggregator"` and a root aggregator whose reactor covers every target, one
  un-suffixed `transform.rewrite` at the repo root runs the union of the sub-case recipes instead.
* No java sources (pom-only, S3): no transform step; `restructure.pom_only` (StepKind.MSA_POM, mode msa_pom) or
  `restructure.msa_pom`, the review and verify.
* `profile.build_tool != "maven"` (S4): zero steps and a note (the orchestrator aborts).
* `loop.phases.{jdk21,spring,restructure}.enabled` (v1 intermediate loops) are ignored and noted when set.
* Library advisories (`build_plan(..., advisories=findings)`, sbmig.advisory) are guide-only: one plan note
  `advisory: <id> → guide (LLM hints + Handover)` each; the fix loop gets their hints, the handover their follow-ups.


"""
from __future__ import annotations

import logging
import posixpath
from typing import Any, Optional

from sbmig.core.config import Config, RecipeSetConfig
from sbmig.core.models import (CaseType, MigrationPlan, Phase, PlanStep, ProjectProfile, RecipeRef, ServiceInfo,
                               StepKind)

from .catalog import GATES, default_catalog, prefix_artifact

log = logging.getLogger("sbmig.plan")

# javax packages that are Java EE (→ jakarta); JDK packages like javax.swing/sql/crypto/naming/net are excluded
EE_JAVAX = ("javax.servlet", "javax.persistence", "javax.xml.bind", "javax.validation", "javax.inject",
            "javax.ws", "javax.jms", "javax.mail", "javax.ejb", "javax.faces", "javax.websocket", "javax.el",
            "javax.json", "javax.batch", "javax.enterprise", "javax.activation", "javax.resource",
            "javax.transaction", "javax.interceptor", "javax.decorator", "javax.jws", "javax.xml.ws",
            "javax.security.enterprise")
# javax.annotation is shared with JSR-305 (@Nullable) and the JDK (javax.annotation.processing): count it as EE
# only when the analyzer confirmed EE usage (library "javax-annotation").

TRANSFORM = "transform.rewrite"
REVIEW = "restructure.review"
V1_LOOP_PHASES = ("jdk21", "spring", "restructure")

# recipe buckets of one transform run, in run order (labels for the step title)
_BUCKETS: tuple[tuple[str, str], ...] = (
    ("jdk21", "Java 21"), ("jakarta", "jakarta"), ("framework", "Spring Framework 7"),
    ("security", "Spring Security 7"), ("boot", "Spring Boot 4.1"), ("junit5", "JUnit 5"), ("extra", "extra recipes"),
)


def artifact_for(recipe_id: str, cfg: Optional[Config] = None) -> Optional[str]:
    """레시피 id 의 jar: 카탈로그 먼저, 없으면 id 접두어로 추정.

    Recipe jar for a builtin id: the recipe catalog first, then the id-prefix fallback."""
    return default_catalog(cfg).artifact_for(recipe_id)


def recipe_ref(recipe_id: str, cfg: Config) -> RecipeRef:
    """레시피 id → OpenRewrite 레시피 참조(RecipeRef, jar 는 카탈로그 / 접두어에서).

    A recipe id → RecipeRef of an OpenRewrite recipe (artifact from the catalog / id prefix)."""
    return RecipeRef(id=recipe_id, source="builtin", artifact=artifact_for(recipe_id, cfg))


def ee_javax_packages(profile_source_javax: dict[str, int], has_javax_annotation: bool) -> list[str]:
    """소스가 import 하는 Java EE javax 패키지들(jakarta 로 옮겨 갈 것). javax.swing / sql 같은 JDK 패키지는
    빼고, ``javax.annotation`` 은 분석기가 EE 사용을 확인했을
    때만 센다(JSR-305 ``@Nullable`` 과 이름이 겹친다)."""
    hits = [k for k in profile_source_javax if any(k == p or k.startswith(p + ".") for p in EE_JAVAX)]
    if has_javax_annotation and "javax.annotation" in profile_source_javax:
        hits.append("javax.annotation")
    return sorted(set(hits))


def _short(recipe_id: str) -> str:
    return recipe_id.rsplit(".", 1)[-1]


def _shorts(ids: list[str]) -> str:
    return ", ".join(_short(i) for i in ids)


# ---------------------------------------------------------------------------
# per-case transform recipes
# ---------------------------------------------------------------------------
class _Facts:
    """레시피 규칙에 필요한 프로필 사실 몇 가지(케이스 타입,
    라이브러리, javax import). MSA 는 서비스마다 따로 만든다.

    The few profile facts the recipe rules need; for MSA computed per service."""

    def __init__(self, case: CaseType, libs: set[str], javax: dict[str, int]):
        self.case = case
        self.libs = libs
        self.javax = javax


def _case_buckets(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> dict[str, list[str]]:
    """변환 1회의 묶음별 레시피 id. 케이스별 규칙:
    - jdk21 세트는 항상.
    - jakarta 세트는 spring_legacy·servlet_tomcat 항상, pure_java 는 EE javax import 가 있을 때만,
      spring_boot 는 넣지 않는다(Boot 4 umbrella 가 javax → jakarta 를 직접 한다).
    - Framework 7 / Security 7 / JUnit 5 세트는 v2 기본값이 비어 있다. ``extra.<case>`` + ``extra.all`` 은
    그대로. 판단 근거는 ``notes`` 에 남긴다(보고서 Notes, plan.json).

    Recipe ids per bucket for one transform run."""
    rs = cfg.openrewrite.recipes
    out: dict[str, list[str]] = {k: [] for k, _ in _BUCKETS}
    where = f" [{label}]" if label else ""
    case = facts.case
    out["jdk21"] = list(rs.jdk21)
    if case == CaseType.PURE_JAVA:
        ee = ee_javax_packages(facts.javax, "javax-annotation" in facts.libs)
        if ee:
            out["jakarta"] = list(rs.jakarta)
            notes.append(f"transform{where}: pure_java with Java EE javax imports ({', '.join(ee)}) → jakarta set")
        else:
            notes.append(f"transform{where}: pure_java without Java EE javax imports → no jakarta migration")
    elif case == CaseType.SERVLET_TOMCAT:
        out["jakarta"] = list(rs.jakarta)
        notes.append(f"transform{where}: servlet_tomcat → jakarta set (javax → jakarta)")
    elif case == CaseType.SPRING_LEGACY:
        out["jakarta"] = list(rs.jakarta)
        msg = f"transform{where}: spring_legacy → jakarta set"
        if rs.spring_framework_7:
            out["framework"] = list(rs.spring_framework_7)
            msg += " + Spring Framework 7 set"
        if "spring-security" in facts.libs:
            if rs.spring_security_7:
                out["security"] = list(rs.spring_security_7)
                first, last = _short(rs.spring_security_7[0]), _short(rs.spring_security_7[-1])
                chain = f"{first} … {last}" if len(rs.spring_security_7) > 1 else first
                msg += f" + Spring Security set {chain} (spring-security detected)"
            else:
                msg += "; spring-security detected, the Spring Security set is empty (v2)"
        notes.append(msg)
    elif case == CaseType.SPRING_BOOT:
        out["boot"] = list(rs.spring_boot_4)
        notes.append(f"transform{where}: spring_boot → Spring Boot 4 set (the umbrella migrates javax → jakarta "
                     "itself)")
    if "junit4" in facts.libs and case != CaseType.UNKNOWN:
        if rs.junit5:
            out["junit5"] = list(rs.junit5)
            notes.append(f"transform{where}: junit4 detected → JUnit 5 set")
        else:
            notes.append(f"transform{where}: junit4 detected, the JUnit 5 set is empty (v2) → tests not migrated")
    extra = rs.extra.get(case.value, []) + rs.extra.get("all", [])
    if extra:
        out["extra"] = list(extra)
        notes.append(f"transform{where}: extra recipes from config ({case.value}/all): {', '.join(extra)}")
    return out


def _flatten(buckets: dict[str, list[str]]) -> list[str]:
    """묶음들을 실행 순서(``_BUCKETS``)대로 한 목록으로(중복 없이) — jdk21 세트가 맨 앞."""
    seen: list[str] = []
    for key, _ in _BUCKETS:
        for i in buckets.get(key, []):
            if i not in seen:
                seen.append(i)
    return seen


def _union(parts: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    """여러 대상의 묶음을 합친다(MSA top-aggregator 의 루트 1회 실행용)."""
    out: dict[str, list[str]] = {k: [] for k, _ in _BUCKETS}
    for b in parts:
        for k, ids in b.items():
            out[k] += [i for i in ids if i not in out[k]]
    return out


def transform_recipes(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> list[RecipeRef]:
    """케이스의 변환 1회 레시피(jdk21 세트 먼저)를 RecipeRef 목록으로.

    The case's recipes of one transform run (jdk21 set first), as RecipeRefs."""
    return [recipe_ref(i, cfg) for i in _flatten(_case_buckets(facts, cfg, notes, label))]


spring_recipes = transform_recipes          # v1 name (sbmig.plan exports both)


# ---------------------------------------------------------------------------
# step factories
# ---------------------------------------------------------------------------
def _sfx(svc: Optional[str]) -> str:
    """MSA 서비스별 단계 id 접미어 ``@<서비스>``."""
    return f"@{svc}" if svc else ""


def _transform_step(facts: _Facts, cfg: Config, notes: list[str], *, svc: Optional[str] = None,
                    target: Optional[str] = None, buckets: Optional[dict[str, list[str]]] = None,
                    title_case: Optional[str] = None) -> Optional[PlanStep]:
    """``transform.rewrite[@svc]`` 단계 — 케이스의 모든 레시피를 as-is classpath 위에서 한 번에(타입 정보가
    살아 있을 때). 재구성 전에는 빌드하지 않는다. 적용할 레시피가 없으면 None(노트만).

    `transform.rewrite[@svc]` — every recipe of the case in one run, or None when no recipe applies.

    `buckets` overrides the per-case recipes (MSA single root run: union of the targets' buckets)."""
    label = svc or ""
    where = f" [{label}]" if label else ""
    if buckets is None:
        buckets = _case_buckets(facts, cfg, notes, label)
    ids = _flatten(buckets)
    if not ids:
        notes.append(f"transform{where}: no recipes apply → {TRANSFORM}{_sfx(svc)} omitted")
        return None
    recipes = [recipe_ref(i, cfg) for i in ids]
    parts = [lbl for key, lbl in _BUCKETS if buckets.get(key)]
    notes.append(f"transform{where}: one OpenRewrite run ({_shorts(ids)}) — then one compile, no fixing until the "
                 "restructure is done (v2.1)")
    return PlanStep(
        id=TRANSFORM + _sfx(svc), kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM,
        title="OpenRewrite in one run: " + " + ".join(parts) + (f" ({svc})" if svc else ""),
        recipes=recipes, target=target, params={"service": svc} if svc else {},
        reason=f"v2 case {title_case or facts.case.value}: Java 21 / jakarta / Spring Boot recipes in one run on the "
               "as-is classpath (type attribution), no build before the restructure (business decision)")


def _review_step(restructure_step: str, cfg: Config, notes: list[str]) -> Optional[PlanStep]:
    """``restructure.review`` 단계(현업 규칙: 첫 빌드 전에 LLM 이 pom·의존성·모듈을 회사 규칙에 비추어 검토).
    ``review.enabled`` 가 false 면 None(노트만). critical 수정 여부는 ``review.fix_critical``."""
    if not cfg.review.enabled:
        notes.append(f"review: review.enabled=false → {REVIEW} omitted (the build follows the restructure)")
        return None
    notes.append("review: the LLM checks poms / dependencies / modules after the restructure"
                 + ("; critical issues → one fix script" if cfg.review.fix_critical else "; findings recorded only")
                 + " (restructure.review)")
    return PlanStep(
        id=REVIEW, kind=StepKind.REVIEW, phase=Phase.RESTRUCTURE,
        title="LLM structure review (poms / dependencies / modules)",
        params={"restructure_step": restructure_step},
        reason="business rule: the LLM confirms poms, dependencies and the module split against the company rules "
               "before the first build; critical issues are fixed by a script, warnings are recorded")


COMPILE = "transform.compile"


def _compile(notes: list[str], target: Optional[str] = None, svc: Optional[str] = None,
             note: bool = True) -> PlanStep:
    """``transform.compile`` 단계(v2.1): OpenRewrite 뒤 ``mvn clean compile`` 한 번 — 모든 오류를 오류 목록에
    ``seen=compile`` 로 적고 고치지 않는다(빌드 게이트 아님). 재구성 뒤 빌드는 새 오류만 더한다."""
    if note:
        notes.append("compile: one mvn clean compile after OpenRewrite → every error on the error list (seen "
                     "compile), no fixing (v2.1)")
    return PlanStep(id=COMPILE + _sfx(svc), kind=StepKind.BUILD, phase=Phase.TRANSFORM,
                    title="mvn clean compile once — errors to the error list, no fixing" + (f" ({svc})" if svc else ""),
                    build_goal="clean compile", target=target, params={"service": svc} if svc else {},
                    reason="v2.1: OpenRewrite's result compiled once; the error list keeps every error (seen compile) "
                           "and the build after the restructure adds only new ones")


def _verify(target: Optional[str] = None, svc: Optional[str] = None) -> PlanStep:
    """``verify.build`` 단계: ``mvn clean package`` — BUILD
    SUCCESS 여야 실행 성공(빌드 게이트, 불변조건 1)."""
    return PlanStep(id="verify.build" + _sfx(svc), kind=StepKind.BUILD, phase=Phase.VERIFY,
                    title="final mvn clean package" + (f" ({svc})" if svc else ""), build_goal="clean package",
                    target=target, params={"service": svc} if svc else {},
                    reason="a run succeeds only on BUILD SUCCESS of mvn clean package")


def _service_libs(profile: ProjectProfile, svc: ServiceInfo) -> set[str]:
    """MSA 서비스 하나의 라이브러리: 그 서비스 pom 의 근거 + import 로만 아는 레포 전체 라이브러리
    (프로필은 import 를 서비스별로 나누지 않는다).

    Libraries of one MSA service: pom evidence from the service's own poms, plus repo-wide libraries known
    only from imports (imports are not attributed per service in the profile)."""
    from sbmig.analyze.libraries import detect_libraries

    sub = [p for p in profile.poms if p.path in set(svc.poms)]
    own = {lib.name for lib in detect_libraries(sub)}
    import_only = {lib.name for lib in profile.libraries if not any(e.startswith("pom:") for e in lib.evidence)}
    return own | import_only


def _service_dir(s: ServiceInfo) -> str:
    """서비스 루트 pom 의 디렉토리(빌드 대상)."""
    return posixpath.dirname(s.root_pom) or "."


def _module_path(pom_path: str, m: str) -> str:
    """``<module>`` 값 → 그 모듈 pom 경로(디렉토리면 ``/pom.xml`` 을 붙인다)."""
    t = posixpath.normpath(posixpath.join(posixpath.dirname(pom_path) or ".", m))
    if not t.endswith(".xml"):
        t = posixpath.join(t, "pom.xml")
    return posixpath.normpath(t)


def _reactor_reach(profile: ProjectProfile, start: str) -> set[str]:
    """``start`` pom 에서 ``<modules>`` 를 따라 닿는 pom 경로 전부(reactor 범위)."""
    by_path = {p.path: p for p in profile.poms}
    seen: set[str] = set()
    todo = [start]
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in by_path:
            continue
        seen.add(cur)
        todo += [_module_path(cur, m) for m in by_path[cur].modules]
    return seen


def _aggregator_covers(profile: ProjectProfile, targets: list[ServiceInfo]) -> bool:
    """루트 pom 이 집합(aggregator)이고 그 reactor 가 모든 서비스·라이브러리를 품는가."""
    rp = profile.root_pom
    if rp is None or not rp.is_aggregator:
        return False
    reach = _reactor_reach(profile, rp.path)
    return all(t.root_pom in reach for t in targets)


def _order_libraries(profile: ProjectProfile, libs: list[ServiceInfo]) -> list[ServiceInfo]:
    """공유 라이브러리 루트를 의존 순서로(다른 라이브러리가 쓰는 것이 먼저, 같으면 reactor 순서, 순환이면
    reactor 순서). 서비스보다 먼저 변환해야 서비스가 마이그레이션된 라이브러리를 본다.

    Libraries in dependency order (a library another library depends on comes first), ties by reactor order."""
    from sbmig.analyze.pom_parser import module_order

    order = {p: i for i, p in enumerate(module_order(profile.poms))}
    by_path = {p.path: p for p in profile.poms}
    produced = {lib.name: {f"{by_path[x].group_id}:{by_path[x].artifact_id}" for x in lib.poms if x in by_path}
                for lib in libs}
    needs = {lib.name: {d.key for x in lib.poms if x in by_path for d in by_path[x].dependencies} for lib in libs}
    remaining = sorted(libs, key=lambda s: order.get(s.root_pom, 10 ** 6))
    out: list[ServiceInfo] = []
    while remaining:
        ready = [s for s in remaining
                 if not any(o.name != s.name and needs[s.name] & produced[o.name] for o in remaining)]
        nxt = ready[0] if ready else remaining[0]          # cycle → reactor order
        out.append(nxt)
        remaining.remove(nxt)
    return out


def _msa_steps(profile: ProjectProfile, cfg: Config, notes: list[str]) -> list[PlanStep]:
    """MSA 계획: 공유 라이브러리 루트 먼저(Boot 세트는 절대 안 줌 — 라이브러리는 spring_legacy 로 본다),
    그다음 서비스별 ``transform.rewrite@<x>`` (top-aggregator 정책 + 루트가 전부를 품으면 루트에서 합집합 1회)
    → 컴파일 → ``restructure.msa_pom``(디렉토리 유지, pom 만) →
    구조 검토 → 검증 빌드(루트 집합이 없으면 서비스별)."""
    from sbmig.analyze.classifier import is_library_root

    steps: list[PlanStep] = []
    libs = _order_libraries(profile, [s for s in profile.services if is_library_root(s, profile)])
    svcs = [s for s in profile.services if not is_library_root(s, profile)]
    targets = libs + svcs
    for lib in libs:
        notes.append(f"msa library root {lib.name} ({_service_dir(lib)}): shared module, migrated before the services "
                     f"as sub-case {lib.case_type.value} (jakarta recipes, never the Boot set)")
    facts_of: dict[str, _Facts] = {}
    for t in targets:
        case = t.case_type
        # 공유 라이브러리는 Boot 앱이 아니다 — Boot 4 세트 대신 jakarta 계열만 받도록 spring_legacy 로 본다
        if t in libs and case in (CaseType.SPRING_BOOT, CaseType.MSA):
            case = CaseType.SPRING_LEGACY
        facts_of[t.name] = _Facts(case, _service_libs(profile, t), profile.source.javax_imports)
        if t in svcs:
            notes.append(f"msa service {t.name} ({_service_dir(t)}): sub-case {t.case_type.value}, "
                         f"boot {t.spring_boot_version or '-'}, java {t.java_version or '-'}")
    no_sources = profile.source.java_files == 0
    single = cfg.layout.msa_parent_policy == "top-aggregator" and _aggregator_covers(profile, targets)
    if no_sources:
        notes.append("msa: no java sources → no transform step (pom edits, structure review, verify)")
    elif single:
        union = _union([_case_buckets(facts_of[t.name], cfg, notes, t.name) for t in targets])
        notes.append(f"msa: top-aggregator policy and {profile.root_pom.path if profile.root_pom else 'pom.xml'} "
                     f"aggregates every service/library → one {TRANSFORM} at the repo root (union of the sub-case "
                     "recipes) instead of per service")
        st = _transform_step(_Facts(CaseType.MSA, set(), profile.source.javax_imports), cfg, notes, buckets=union,
                             title_case="msa (all services)")
        steps += [st] if st else []
    else:
        for t in targets:
            st = _transform_step(facts_of[t.name], cfg, notes, svc=t.name, target=_service_dir(t))
            steps += [st] if st else []
    rp = profile.root_pom
    aggregated = rp is not None and rp.is_aggregator
    if not no_sources:
        if aggregated:
            steps.append(_compile(notes))
        else:
            for i, t in enumerate(targets):
                steps.append(_compile(notes, target=_service_dir(t), svc=t.name, note=i == 0))
    params: dict[str, Any] = {"mode": "msa_pom", "services": [s.name for s in svcs],
                              "parent_policy": cfg.layout.msa_parent_policy,
                              "boot_modules": list(profile.boot_modules)}
    if libs:
        params["library_roots"] = [s.root_pom for s in libs]
    steps.append(PlanStep(
        id="restructure.msa_pom", kind=StepKind.MSA_POM, phase=Phase.RESTRUCTURE,
        title="MSA: pom-only parent / mandatory / optional edits on each service root",
        params=params,
        reason="msa: directory structure is kept; only service poms are aligned to the in-house parent"))
    notes.append(f"restructure: msa → pom-only edits ({cfg.layout.msa_parent_policy}), no module split")
    if libs:
        notes.append("restructure: library roots " + ", ".join(s.root_pom for s in libs) + " get the parent swap + "
                     "optional bumps + remove_when_present, but no mandatory dependencies (those go to deployables)")
    rv = _review_step("restructure.msa_pom", cfg, notes)
    steps += [rv] if rv else []
    if aggregated:
        steps.append(_verify())
        notes.append(f"verify: root aggregator {rp.path} → one mvn clean package for the whole repo")
    else:
        for t in targets:
            steps.append(_verify(target=_service_dir(t), svc=t.name))
        notes.append("verify: no root aggregator pom → one mvn clean package per service"
                     + (" and library root" if libs else ""))
    return steps


def _single_steps(profile: ProjectProfile, cfg: Config, notes: list[str]) -> list[PlanStep]:
    """단일 프로젝트 계획. 소스가 없으면 pom 만 고친다(``restructure.pom_only``). 아니면 변환 → 컴파일 →
    재구성: Boot as-is(``layout.boot_as_is == "in-place"``)는 디렉토리를 그대로 두고 pom
    만(``restructure.boot_pom``), 그 밖은 회사 레이아웃으로 나눈다(``restructure.apply``: ``{name}-boot`` /
    ``-back`` / 웹 자원이 있으면 ``-front``) → 구조 검토 → 검증 빌드."""
    steps: list[PlanStep] = []
    case = profile.case_type
    if case == CaseType.MSA:
        notes.append("msa without discovered services → planned as a single project")
    libs = {lib.name for lib in profile.libraries}
    facts = _Facts(case if case != CaseType.MSA else CaseType.SPRING_BOOT, libs, profile.source.javax_imports)
    if profile.source.java_files == 0 and profile.poms:
        # S3: pom-only aggregator / BOM / parent → pom edits only (parent, mandatory, optional, cleanup) + verify
        notes.append("no java sources → no transform step; restructure is limited to pom edits (no module split, "
                     "no Application)")
        steps.append(PlanStep(
            id="restructure.pom_only", kind=StepKind.MSA_POM, phase=Phase.RESTRUCTURE,
            title="pom-only parent / mandatory / optional edits (no sources)",
            params={"mode": "msa_pom", "services": [], "parent_policy": cfg.layout.msa_parent_policy,
                    "pom_only": True},
            reason="pom-only project: nothing to split; align the root pom(s) to the in-house parent"))
        rv = _review_step("restructure.pom_only", cfg, notes)
        steps += [rv] if rv else []
        steps.append(_verify())
        return steps
    st = _transform_step(facts, cfg, notes)
    steps += [st] if st else []
    steps.append(_compile(notes))
    if case == CaseType.SPRING_BOOT and cfg.layout.boot_as_is == "in-place":
        # round 9c company rule: a Boot as-is project keeps its directories; only the poms change
        steps.append(PlanStep(
            id="restructure.boot_pom", kind=StepKind.MSA_POM, phase=Phase.RESTRUCTURE,
            title="Boot as-is: parents / mandatory / optional in place (directories kept)",
            params={"mode": "boot_pom", "boot_modules": list(profile.boot_modules)},
            reason="spring_boot as-is: company rule — directories kept, root → in-house parent, boot modules → "
                   "boot parent, other modules → comp parent"))
        pat = cfg.layout.boot_module_pattern
        if profile.boot_modules:
            where = f"boot module(s) {', '.join(profile.boot_modules)} (named {pat}); every other module → comp parent"
        elif profile.is_multi_module:
            where = f"no module named {pat} with a boot pom → no boot parent assigned by name (see warnings)"
        else:
            name = cfg.layout.project_name or profile.name
            where = f"single-module Boot app → wrapped into {cfg.layout.module_path('boot', name)}"
        notes.append("restructure: rule boot-as-is (layout.boot_as_is=in-place) → pom-only edits in place "
                     f"(boot_pom), directories kept; {where}")
    else:
        starter = cfg.layout.starter_by_case.get(facts.case.value, "auto")
        name = cfg.layout.project_name or profile.name
        boot_m = cfg.layout.module_path("boot", name)
        comp_m = cfg.layout.module_path("comp", name)
        front_m = cfg.layout.module_path("front", name)
        src = profile.source
        has_web = bool(src.jsp_paths or src.web_static_count or src.jsp_count)
        mods = [comp_m, boot_m] + ([front_m] if front_m and has_web else [])
        steps.append(PlanStep(
            id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE,
            title=f"restructure into {', '.join(m for m in mods if m)} with the in-house parent",
            params={"mode": "layout", "starter": starter},
            reason=f"non-msa: company layout; boot module starter '{starter}' for {facts.case.value}"))
        rule = ("rule boot-as-is=layout (Boot split like the other cases)" if case == CaseType.SPRING_BOOT
                else f"rule non-boot ({facts.case.value})")
        notes.append(f"restructure: {rule} → layout split into {', '.join(m for m in mods if m)}, starter '{starter}'"
                     + (f", multi-module policy '{cfg.layout.multi_module_policy}'" if profile.is_multi_module else ""))
        if front_m and has_web:
            notes.append(f"restructure: web resources ({len(src.jsp_paths) or src.jsp_count} JSP(s), "
                         f"{src.web_static_count} static file(s) in {', '.join(src.web_static_dirs) or '-'}) → "
                         f"{front_m}"
                         + (f"; JSPs → {boot_m} packaged as {cfg.layout.jsp_boot_packaging}"
                            if (src.jsp_paths or src.jsp_count) else ""))
    if facts.case == CaseType.SERVLET_TOMCAT and profile.source.has_web_xml:
        src_ = profile.source
        if cfg.layout.web_xml_registrations == "generate" and \
                (src_.web_xml_servlets or src_.web_xml_filters or src_.web_xml_listeners):
            # 2026-10-23: the restructure generates WebXmlRegistrations (sbmig.restructure.webxml_registrations)
            notes.append(f"restructure: web.xml ({src_.web_xml_spec or 'spec unknown'}: {src_.web_xml_servlets} "
                         f"servlet(s), {src_.web_xml_filters} filter(s), {src_.web_xml_listeners} listener(s)) → "
                         "generated WebXmlRegistrations in the boot module")
        else:
            notes.append("followup: web.xml wiring is not migrated automatically (runtime owners)")
    if facts.case == CaseType.SPRING_LEGACY and profile.source.xml_bean_configs:
        notes.append(f"restructure: {len(profile.source.xml_bean_configs)} XML bean config(s) → @ImportResource")
    rv = _review_step(steps[-1].id, cfg, notes)
    steps += [rv] if rv else []
    steps.append(_verify())
    return steps


def _settings_notes(cfg: Config, notes: list[str]) -> None:
    """v2 가 무시하는 루프 설정(v1 의 중간 루프 jdk21 / spring / restructure)과 선택 아티팩트에 대한 노트.
    설정이 무엇이든 v2 는 같은 흐름을 계획한다(선택 아티팩트는 재구성 단계 안에서 올린다).

    Notes for loop settings v2 ignores and for the optional artifacts; v2 plans the same flow whatever they say."""
    on = [k for k in V1_LOOP_PHASES if (pc := cfg.loop.phases.get(k)) is not None and pc.enabled]
    if on:
        notes.append(f"v2: no intermediate build-fix loops (loop.phases.{{{','.join(on)}}}.enabled=true ignored)")
    vc = cfg.loop.phases.get("verify")
    if vc is not None and not vc.enabled:
        notes.append("loop.phases.verify.enabled=false → the final build is not followed by the script fix loop")
    if cfg.artifacts.optional or cfg.artifacts.remove_when_present:
        notes.append("optional artifacts: bumped inside the restructure step (no separate bump step in v2)")
    else:
        notes.append("optional artifacts: none configured")


def build_plan(profile: ProjectProfile, cfg: Config, kb: Any = None,
               advisories: Optional[list[Any]] = None) -> MigrationPlan:
    """규칙 기반 v2 계획을 만든다. ``kb`` 는 인터페이스 호환용으로 받기만 한다(v2 에 KB 레시피 없음).
    ``advisories`` 는 권고 결과 — 각각 계획 노트 한 줄이 될 뿐 레시피가 되지 않는다. Maven 이 아니면 단계 0개
    + 노트(오케스트레이터가 실행을 중단한다).

    Rule-based v2 plan. `kb` is accepted for the seam but not used (no KB recipes in v2). `advisories` =
    `sbmig.advisory.evaluate_advisories` findings (guide-only: one plan note each, never a recipe)."""
    notes: list[str] = []
    steps: list[PlanStep] = []
    case = profile.case_type
    notes.append(f"case {case.value} (confidence {profile.confidence:.2f}): "
                 + ("; ".join(profile.reasons[:2]) if profile.reasons else "no reasons recorded"))
    if profile.build_tool != "maven":
        why = profile.warnings[0] if profile.warnings else "no pom.xml"
        notes.append(f"build tool '{profile.build_tool}' is not supported (only Maven): {why} → empty plan, the run "
                     "is aborted")
        for w in profile.warnings[1:]:
            notes.append(f"warning: {w}")
        return MigrationPlan(profile_fingerprint=profile.fingerprint, case_type=case, steps=[], notes=notes)
    _settings_notes(cfg, notes)

    if case == CaseType.MSA and profile.services:
        steps += _msa_steps(profile, cfg, notes)
    else:
        steps += _single_steps(profile, cfg, notes)
    for f in advisories or []:
        notes.append(f"advisory: {getattr(f, 'advisory_id', f)} → guide (LLM hints + Handover)")
    for w in profile.warnings:
        notes.append(f"warning: {w}")
    return MigrationPlan(profile_fingerprint=profile.fingerprint, case_type=case, steps=steps, notes=notes)


# ---------------------------------------------------------------------------
# 선택 기능: LLM 계획 조언자(``plan.advisor: llm``, 기본은 none = 꺼짐). 더하기만 하고 빼지 않는다
# optional LLM plan advisor (plan.advisor: llm)
# ---------------------------------------------------------------------------
# The advisor whitelist / coverage / gates are derived from the recipe catalog (knowledge/recipes/catalog.yml,
# see sbmig/plan/catalog.py and docs/RECIPES.md). Module-level snapshots of the repo catalog, taken at import time;
# `advise_plan(cfg=...)` re-reads `cfg.recipe_catalog_file`. v2: `advise_plan` narrows the whitelist to the recipes
# of the configured sets + extras (`v2_recipe_scope`); the constants stay the catalog's full advisor view.
_DEFAULT_CATALOG = default_catalog()

# Recipes the advisor may ADD (ids verified against docs.openrewrite.org / our recipe sets; never removes anything).
ADVISOR_CATALOG: dict[str, str] = _DEFAULT_CATALOG.advisor_catalog()

# What the planned composites already include (so the advisor is told, and duplicates are filtered deterministically).
ADVISOR_COVERAGE: dict[str, list[str]] = _DEFAULT_CATALOG.coverage()

# Profile facts a suggestion must be backed by — a small model sometimes suggests recipes "just in case".
ADVISOR_GATES: dict[str, Any] = _DEFAULT_CATALOG.gates()

_has_javax_ee = GATES["has_javax_ee"]          # backwards-compatible alias


def v2_recipe_scope(cfg: Optional[Config] = None) -> set[str]:
    """v2 가 돌릴 수 있는 레시피 id 전부: 설정 세트(jdk21, jakarta, spring_boot_4 와 비어 있는 선택 세트들) +
    extra. 조언자의 화이트리스트를 이 범위로 좁힌다.

    Every recipe id v2 may run: the configured sets (jdk21, jakarta, spring_boot_4 and the opt-in framework /
    security / junit5 sets) plus every `extra` list."""
    rs = cfg.openrewrite.recipes if cfg is not None else RecipeSetConfig()
    ids = {*rs.jdk21, *rs.jakarta, *rs.spring_boot_4, *rs.spring_framework_7, *rs.spring_security_7, *rs.junit5}
    for extra in (rs.extra or {}).values():
        ids.update(extra)
    return ids


def advisor_whitelist(cfg: Optional[Config] = None) -> dict[str, str]:
    """v2 범위 안의, 조언자가 더할 수 있는 카탈로그 레시피(카탈로그 순서).

    The advisor-eligible catalog recipes inside the v2 scope (catalog order)."""
    cat = default_catalog(cfg) if cfg is not None else _DEFAULT_CATALOG
    scope = v2_recipe_scope(cfg)
    return {rid: desc for rid, desc in (cat.advisor_catalog() or ADVISOR_CATALOG).items() if rid in scope}


def _covered_by(planned: set[str], coverage: Optional[dict[str, list[str]]] = None) -> dict[str, str]:
    """레시피 id → 그것을 이미 포함하는 계획된 묶음 레시피.

    recipe id → the planned composite that already includes it."""
    out: dict[str, str] = {}
    for comp, inner in (ADVISOR_COVERAGE if coverage is None else coverage).items():
        if comp in planned:
            for rid in inner:
                out.setdefault(rid, comp)
    return out


def _artifact_for(recipe_id: str, catalog_obj: Any = None) -> Optional[str]:
    """조언자가 더한 레시피의 jar(주어진 카탈로그, 없으면 import 시점의 기본 카탈로그)."""
    cat = catalog_obj if catalog_obj is not None else _DEFAULT_CATALOG
    return cat.artifact_for(recipe_id) if recipe_id in cat else prefix_artifact(recipe_id)


def _advisor_prompt(plan: MigrationPlan, profile: ProjectProfile, catalog: dict[str, str],
                    coverage: Optional[dict[str, list[str]]] = None) -> str:
    """조언자 프롬프트: 프로필 요약, 규칙 기반 계획, 더할 수
    있는 레시피 목록, 이미 포함된 것, 답 형식(JSON)."""
    libs = ", ".join(f"{l.name}{'@' + l.version if l.version else ''}" for l in profile.libraries) or "-"
    javax = ", ".join(f"{k}:{v}" for k, v in sorted(profile.source.javax_imports.items())) or "-"
    steps = "\n".join(f"- {st.id} [{st.phase.value}]: " + (", ".join(r.id for r in st.recipes) or "(no recipes)")
                      for st in plan.steps if st.recipes or st.kind.value == "openrewrite")
    cat = "\n".join(f"- {rid}: {desc}" for rid, desc in catalog.items()) or "- (none)"
    planned_ids = {r.id for st in plan.steps for r in st.recipes}
    covered = _covered_by(planned_ids, coverage)
    cov_text = "\n".join(f"- {rid} is already included by {comp}" for rid, comp in sorted(covered.items())) or "- (none)"
    return (
        "PROJECT PROFILE\n"
        f"- case: {profile.case_type.value} (confidence {profile.confidence:.2f}); reasons: {'; '.join(profile.reasons[:6])}\n"
        f"- java: {profile.java_version}, spring-boot: {profile.spring_boot_version}, spring: {profile.spring_version}, packaging: {profile.packaging}\n"
        f"- libraries: {libs}\n- javax imports: {javax}\n"
        f"- jakarta imports: {', '.join(sorted(profile.source.jakarta_imports)) or '-'}\n"
        f"- main classes: {len(profile.source.main_classes)}, servlets: {len(profile.source.servlet_classes)}, "
        f"web.xml: {profile.source.has_web_xml}, xml bean configs: {len(profile.source.xml_bean_configs)}, "
        f"java files: {profile.source.java_files}, test files: {profile.source.test_files}\n"
        f"- warnings: {'; '.join(profile.warnings[:6]) or '-'}\n\n"
        "RULE-BASED PLAN (one OpenRewrite run per transform step, before the restructure)\n" + steps + "\n\n"
        "CATALOG OF RECIPES YOU MAY ADD\n" + cat + "\n\n"
        "ALREADY COVERED BY PLANNED COMPOSITE RECIPES (do not suggest these)\n" + cov_text + "\n\n"
        "Task: suggest ONLY additional recipes from the catalog that the profile clearly warrants and the plan lacks "
        "(e.g. Java EE javax imports but no JavaxMigrationToJakarta). Do not suggest recipes already planned, do not "
        "remove anything, do not invent ids. If nothing is missing, return an empty list. Also list short concerns "
        "the migration team should know (max 5).\n"
        'Answer with JSON only: {"add": [{"recipe": "<catalog id>", "reason": "<why>"}], "concerns": ["<text>"]}'
    )


def _parse_advice(text: str) -> tuple[list[dict], list[str], Optional[str]]:
    """조언자 답(코드 펜스가 있어도)에서 (추가 제안, 우려 최대 5개, 오류 문장)을 꺼낸다."""
    import json as _json
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return [], [], "advisor answer is not JSON"
    try:
        data = _json.loads(t[i:j + 1])
    except ValueError as e:
        return [], [], f"advisor JSON invalid: {e}"
    adds = [a for a in (data.get("add") or []) if isinstance(a, dict) and a.get("recipe")]
    concerns = [str(c) for c in (data.get("concerns") or []) if c][:5]
    return adds, concerns, None


def advise_plan(plan: MigrationPlan, profile: ProjectProfile, llm: Any = None,
                cfg: Optional[Config] = None, *, catalog: Optional[dict[str, str]] = None) -> MigrationPlan:
    """선택 기능: 규칙 기반 계획의 LLM 검토(``plan.advisor == "llm"``, 모델 ``llm.recipe_model``; 기본은
    꺼짐). 더하기만 한다 — 화이트리스트 레시피를 변환 단계에 더하고 우려를 노트에 적는다. 모르는 id, 이미
    계획된 / 포함된 id, 게이트가 뒷받침하지 않는 제안, 깨진 답은
    무시하고 노트만. 예외를 내지 않는다(계획 단계를 멈추지 않는다).

    Optional LLM review of the rule-based plan (`cfg.plan.advisor == "llm"`, model `cfg.llm.recipe_model`).

    The advisor is additive only: it may add whitelisted recipes to the transform step(s) and record concerns in
    `plan.notes`; unknown ids, already-planned ids and malformed answers are ignored (noted). The whitelist is
    `catalog` when given, else the advisor-eligible catalog recipes inside the v2 scope (`advisor_whitelist`).
    Never raises."""
    if llm is None or (cfg is not None and cfg.plan.advisor == "none"):
        return plan
    cat_obj = default_catalog(cfg) if cfg is not None else _DEFAULT_CATALOG
    if cfg is not None:
        coverage, gates = cat_obj.coverage(), cat_obj.gates()
    else:
        coverage, gates = ADVISOR_COVERAGE, ADVISOR_GATES
    catalog = catalog if catalog is not None else advisor_whitelist(cfg)
    model = (cfg.llm.recipe_model or None) if cfg is not None else None
    try:
        from sbmig.core.interfaces import ChatMessage
        resp = llm.complete(
            [ChatMessage(role="system", content="You are a senior Java/Spring migration planner. You review a "
                                                 "rule-based OpenRewrite plan and only add clearly warranted recipes "
                                                 "from a fixed catalog. Answer with JSON only."),
             ChatMessage(role="user", content=_advisor_prompt(plan, profile, catalog, coverage))],
            model=model, max_tokens=1200, temperature=0.0, role="plan_advisor", metadata={"profile": profile.name})
    except Exception as e:  # advisor must never break planning
        plan.notes.append(f"advisor: LLM call failed ({type(e).__name__}: {e}); plan unchanged")
        return plan
    used = getattr(resp, "model", model) or model or "?"
    try:
        from sbmig.core.models import LLMUsage
        plan.llm_usage = plan.llm_usage.add(LLMUsage(model=used, calls=1, input_tokens=int(resp.input_tokens or 0),
                                                     output_tokens=int(resp.output_tokens or 0)))
    except Exception:  # pragma: no cover - bookkeeping only
        pass
    adds, concerns, err = _parse_advice(getattr(resp, "content", "") or "")
    plan.notes.append(f"advisor[{used}]: {resp.input_tokens}+{resp.output_tokens} tokens, "
                      f"{len(adds)} suggestion(s), {len(concerns)} concern(s)")
    if err:
        plan.notes.append(f"advisor: {err}; plan unchanged")
        return plan
    planned = {r.id for st in plan.steps for r in st.recipes}
    targets = [st for st in plan.steps if st.id == TRANSFORM or st.id.startswith(TRANSFORM + "@")]
    for a in adds:
        rid = str(a.get("recipe", "")).strip()
        reason = str(a.get("reason", "")).strip()[:200]
        if rid not in catalog:
            plan.notes.append(f"advisor: ignored unknown recipe {rid}")
            continue
        if rid in planned:
            plan.notes.append(f"advisor: {rid} already planned")
            continue
        covered = _covered_by(planned, coverage)
        if rid in covered:
            plan.notes.append(f"advisor: {rid} already covered by {covered[rid]}; not added")
            continue
        gate = gates.get(rid)
        try:
            warranted = True if gate is None else bool(gate(profile))
        except Exception:  # pragma: no cover - defensive
            warranted = True
        if not warranted:
            plan.notes.append(f"advisor: {rid} not warranted by the profile; ignored ({reason or 'no reason'})")
            continue
        if not targets:
            plan.notes.append(f"advisor: no {TRANSFORM} step to attach {rid}; ignored")
            continue
        for st in targets:
            st.recipes.append(RecipeRef(id=rid, source="builtin", artifact=_artifact_for(rid, cat_obj)))
        planned.add(rid)
        plan.notes.append(f"advisor: +{rid} → {TRANSFORM} ({reason or 'no reason given'})")
    for c in concerns:
        plan.notes.append(f"advisor concern: {c}")
    return plan
