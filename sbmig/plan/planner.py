"""프로젝트 분석 결과와 설정을 보고 이관 단계 목록(MigrationPlan)을 규칙만으로 정하는 모듈.
설정으로 켠 경우에만 LLM 조언자가 레시피를 더 제안한다.

The orchestrator calls ``build_plan`` after analyze and runs the steps in order. Only the Java 21 / jakarta
/ Boot 4 sets (plus the exceptions below) are planned: the business decided not to add a recipe whose
changes are unknown. Everything else is left as a build error for the fix loop's scripts.

Step ids (non-MSA): transform.rewrite, transform.compile, restructure.apply | restructure.boot_pom |
restructure.pom_only, restructure.review, verify.build. The only build before verify.build is the optional
compile (recorded, never fixed); verify.build's fix loop (`loop.phases.verify`) fixes the errors.

* `transform.rewrite` (StepKind.OPENREWRITE, Phase.TRANSFORM): ONE OpenRewrite run on the as-is classpath.
  Recipes, in this order: `openrewrite.recipes.jdk21`; `jakarta` (spring_legacy and servlet_tomcat always,
  pure_java only with Java EE javax imports, never spring_boot: the Boot umbrella migrates jakarta itself)
  followed in the same bucket by `servlet_jakarta_10` (every servlet_tomcat project, pure_java with
  `javax.servlet` imports, spring_legacy only when the framework set is empty: UpgradeSpringFramework_7_0
  already includes it); `spring_framework_7` (every spring_legacy project) / `spring_security_7`
  (spring_legacy, the 6_0 … 6_5, 7_0 chain, only when the analyzer found `spring-security`) / `spring_batch_6`
  and `hibernate_7` (spring_legacy with the analyzer's `spring-batch` / `hibernate` library; `hibernate` is
  the native ORM rule, a JPA-only project has `jpa` only); `spring_boot_4` (spring_boot; the umbrella covers
  Batch, Hibernate and the Servlet removals); `junit5` (junit4 detected, empty by default); `extra.<case>` +
  `extra.all`. OpenRewrite recipes only: no project, library or KB recipes (PLAN-v2 §2).
  `hystrix_resilience4j`: every case whose plan has the transform step, when the analyzer found `hystrix`,
  right after the jakarta bucket and before the Framework / Security / … / Boot sets (the starter swap sees
  the as-is Spring Cloud BOM); one plan note. The composite is a declarative recipe of this repository
  (`knowledge/recipes/base/hystrix-to-resilience4j.yml`, catalog `file`).
* `transform.compile` (StepKind.BUILD, Phase.TRANSFORM, `mvn clean compile`, only with
  `plan.compile_after_rewrite`): every error goes on the run's error list (seen `compile`), nothing is fixed.
  Planned whenever the project has java sources (also when no recipe applied). MSA: one at the root
  aggregator, else `transform.compile@<x>` per target (like verify).
* Restructure: `case_type == spring_boot` with `layout.boot_as_is == "in-place"` (default) plans
  `restructure.boot_pom` (StepKind.MSA_POM, params {"mode": "boot_pom", "boot_modules"}): directories kept,
  poms only; `boot_as_is == "layout"` and every non-Boot case plan `restructure.apply` (StepKind.RESTRUCTURE,
  params {"mode": "layout", "starter"}: `{name}-boot` / `{name}-back` / `{name}-front`). A single-module Boot
  app (the root pom lists no modules) under `in-place` is split the same way when `layout.boot_single_module`
  is `split` (the default): `restructure.apply` with `"boot_single": True`; `wrap` keeps
  `restructure.boot_pom` (wrapped into `{name}-boot`). The in-house optional artifacts are bumped inside the
  restructure step; `spring.optional_bump` is never planned.
* `restructure.review` (StepKind.REVIEW, Phase.RESTRUCTURE, params {"restructure_step": <id>}): the LLM
  structure review right after the restructure; omitted with a note when `review.enabled` is false.
* `verify.build` (StepKind.BUILD, Phase.VERIFY, `mvn clean package`).
* MSA: `transform.rewrite@<x>` per target (params `service`, target = its directory): shared library roots
  first (dependency / reactor order; never the Boot set), then the services with their own sub-case; then
  `restructure.msa_pom` (params `services`, `parent_policy`, `boot_modules`, `library_roots`),
  `restructure.review` and `verify.build` (or `verify.build@<x>` per target when there is no root aggregator).
  With `layout.msa_parent_policy == "top-aggregator"` and a root aggregator whose reactor covers every target,
  one un-suffixed `transform.rewrite` at the repo root runs the union of the sub-case recipes instead.
* No java sources (pom-only): no transform step; `restructure.pom_only` (StepKind.MSA_POM, mode msa_pom) or
  `restructure.msa_pom`, the review and verify.
* `profile.build_tool != "maven"`: zero steps and a note (the orchestrator aborts).
* `loop.phases.{jdk21,spring,restructure}.enabled` (the old intermediate loops) are ignored and noted when
  set.
* Library advisories (`build_plan(..., advisories=findings)`, sbmig.advisory) are guide-only: one plan note
  `advisory: <id> → guide (LLM hints + Handover)` each; the fix loop gets their hints, the handover their
  follow-ups.
* Framework projects (`profile.reduced_scope`): `MigrationPlan.scope = "jdk-jakarta"` and only
  `transform.rewrite[@svc]` (jdk21 + jakarta sets), `transform.jsp_jakarta` (StepKind.RESTRUCTURE,
  Phase.TRANSFORM, params {"mode": "jsp_jakarta"}) and `verify.build[@svc]`; never a restructure, the review
  or `transform.compile`. The first note starts `scope: jdk-jakarta — framework project (…)`. A full-scope
  plan is unchanged. Contract: `docs/INTERFACES.md "v2.1z-ak"`.
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

# jakarta로 옮길 Java EE javax 패키지. javax.swing/sql/crypto/naming/net 같은 JDK 패키지는 넣지 않는다
EE_JAVAX = ("javax.servlet", "javax.persistence", "javax.xml.bind", "javax.validation", "javax.inject",
            "javax.ws", "javax.jms", "javax.mail", "javax.ejb", "javax.faces", "javax.websocket", "javax.el",
            "javax.json", "javax.batch", "javax.enterprise", "javax.activation", "javax.resource",
            "javax.transaction", "javax.interceptor", "javax.decorator", "javax.jws", "javax.xml.ws",
            "javax.security.enterprise")
# javax.annotation은 JSR-305(@Nullable)와 JDK(javax.annotation.processing)에도 있는 이름이다.
# 분석기가 EE 사용(라이브러리 "javax-annotation")을 확인했을 때만 EE로 센다

TRANSFORM = "transform.rewrite"
REVIEW = "restructure.review"
V1_LOOP_PHASES = ("jdk21", "spring", "restructure")

# 변환 1회의 레시피 묶음과 실행 순서.
# Servlet 6 제거 레시피는 jakarta.servlet 타입에 적용된다. 그래서 javax를 jakarta로 바꾼 다음에 실행한다
# Hystrix 전환은 Boot / Framework 세트보다 먼저 실행한다. as-is Spring Cloud BOM이 pom에 남아 있어야 starter를 바꾼다
# Boot 세트는 Boot 프로젝트에만 들어간다. 레거시 세트와 함께 들어가지 않는다. 두 번째 값은 단계 제목에 쓴다
_BUCKETS: tuple[tuple[str, str], ...] = (
    ("jdk21", "Java 21"), ("jakarta", "jakarta"), ("hystrix", "Hystrix → Resilience4j"),
    ("framework", "Spring Framework 7"),
    ("security", "Spring Security 7"), ("batch", "Spring Batch 6"), ("hibernate", "Hibernate 7.1"),
    ("boot", "Spring Boot 4.1"), ("junit5", "JUnit 5"), ("extra", "extra recipes"),
)


def artifact_for(recipe_id: str, cfg: Optional[Config] = None) -> Optional[str]:
    """레시피 id의 jar를 반환한다. 카탈로그를 먼저 보고 없으면 id 접두어로 추정한다."""
    return default_catalog(cfg).artifact_for(recipe_id)


def recipe_ref(recipe_id: str, cfg: Config) -> RecipeRef:
    """레시피 id를 OpenRewrite 레시피 참조(RecipeRef)로 바꾼다. jar는 :func:`artifact_for`로 정한다."""
    return RecipeRef(id=recipe_id, source="builtin", artifact=artifact_for(recipe_id, cfg))


def ee_javax_packages(profile_source_javax: dict[str, int], has_javax_annotation: bool) -> list[str]:
    """소스가 import하는 Java EE javax 패키지 중 jakarta로 옮길 것을 반환한다.
    javax.swing / sql 같은 JDK 패키지는 뺀다.
    ``javax.annotation``은 JSR-305 ``@Nullable``과 이름이 겹친다. 분석기가 EE 사용을 확인했을 때만 센다."""
    hits = [k for k in profile_source_javax if any(k == p or k.startswith(p + ".") for p in EE_JAVAX)]
    if has_javax_annotation and "javax.annotation" in profile_source_javax:
        hits.append("javax.annotation")
    return sorted(set(hits))


def _short(recipe_id: str) -> str:
    return recipe_id.rsplit(".", 1)[-1]


def _shorts(ids: list[str]) -> str:
    return ", ".join(_short(i) for i in ids)


# ---------------------------------------------------------------------------
# 케이스별 변환 레시피
# ---------------------------------------------------------------------------
class _Facts:
    """레시피 규칙에 필요한 프로필 정보(케이스 타입, 라이브러리, javax import)를 담는 클래스.
    MSA는 서비스마다 따로 만든다."""

    def __init__(self, case: CaseType, libs: set[str], javax: dict[str, int]):
        self.case = case
        self.libs = libs
        self.javax = javax


def _chain(ids: list[str]) -> str:
    """계획 노트에 쓸 세트 id의 짧은 이름을 만든다. 하나면 그 이름을, 여럿이면 ``첫 … 끝``을 반환한다."""
    first, last = _short(ids[0]), _short(ids[-1])
    return f"{first} … {last}" if len(ids) > 1 else first


# 분석기가 찾은 라이브러리별로 spring_legacy에 더할 조건부 세트.
# ``hibernate``는 네이티브 Hibernate ORM을 쓸 때만 붙는다. JPA API만 쓰는 프로젝트는 받지 않는다
_LEGACY_LIBRARY_SETS: tuple[tuple[str, str, str, str], ...] = (
    ("spring-security", "security", "spring_security_7", "Spring Security"),
    ("spring-batch", "batch", "spring_batch_6", "Spring Batch 6"),
    ("hibernate", "hibernate", "hibernate_7", "Hibernate 7.1"),
)


def _case_buckets(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> dict[str, list[str]]:
    """변환 1회에 실행할 레시피 id를 묶음별로 정한다.
    - jdk21 세트는 항상 넣는다. jakarta 세트는 spring_legacy와 servlet_tomcat에 항상, pure_java는 EE javax
      import가 있을 때만 넣고 spring_boot에는 넣지 않는다
    - Servlet 6 제거 세트(``servlet_jakarta_10``)는 servlet_tomcat에 항상, pure_java는 ``javax.servlet``
      import가 있을 때, spring_legacy는 Framework 7 세트가 비었을 때만 넣는다
    - spring_legacy는 Framework 7 세트를 항상, 분석기가 라이브러리를 찾았을 때만 Security / Batch 6 /
      Hibernate 7.1 세트를 넣는다
    - 판단 근거는 ``notes``에 남긴다. 실행 순서는 ``_BUCKETS``를 따른다

    The Framework 7 set already includes the Servlet 6 removals. Boot projects get none of the legacy sets:
    the Boot 4 umbrella runs all three."""
    rs = cfg.openrewrite.recipes
    out: dict[str, list[str]] = {k: [] for k, _ in _BUCKETS}
    where = f" [{label}]" if label else ""
    case = facts.case
    out["jdk21"] = list(rs.jdk21)
    servlet10 = list(rs.servlet_jakarta_10)
    servlet10_note = f" + Servlet 6 removals {_chain(servlet10)}" if servlet10 else ""
    if case == CaseType.PURE_JAVA:
        ee = ee_javax_packages(facts.javax, "javax-annotation" in facts.libs)
        if ee:
            out["jakarta"] = list(rs.jakarta)
            msg = f"transform{where}: pure_java with Java EE javax imports ({', '.join(ee)}) → jakarta set"
            # 분류기는 javax.servlet import가 있으면 보통 servlet_tomcat으로 본다. 그래서 여기는 드문 경우다
            if servlet10 and any(k == "javax.servlet" or k.startswith("javax.servlet.") for k in ee):
                out["jakarta"] += [i for i in servlet10 if i not in out["jakarta"]]
                msg += servlet10_note + " (javax.servlet imports)"
            notes.append(msg)
        else:
            notes.append(f"transform{where}: pure_java without Java EE javax imports → no jakarta migration")
    elif case == CaseType.SERVLET_TOMCAT:
        out["jakarta"] = list(rs.jakarta) + [i for i in servlet10 if i not in rs.jakarta]
        notes.append(f"transform{where}: servlet_tomcat → jakarta set (javax → jakarta){servlet10_note}")
    elif case == CaseType.SPRING_LEGACY:
        out["jakarta"] = list(rs.jakarta)
        msg = f"transform{where}: spring_legacy → jakarta set"
        if rs.spring_framework_7:
            out["framework"] = list(rs.spring_framework_7)
            msg += " + Spring Framework 7 set"
        elif servlet10:
            # Framework 7 세트가 꺼진 회사 설정이면 그 안에 있던 Servlet 6 제거 레시피를 따로 넣는다
            out["jakarta"] += [i for i in servlet10 if i not in out["jakarta"]]
            msg += servlet10_note + " (the Spring Framework 7 set is empty)"
        for lib, bucket, set_name, title in _LEGACY_LIBRARY_SETS:
            if lib not in facts.libs:
                continue
            ids = list(getattr(rs, set_name))
            if ids:
                out[bucket] = ids
                msg += f" + {title} set {_chain(ids)} ({lib} detected)"
            else:
                msg += f"; {lib} detected, the {title} set is empty"
        notes.append(msg)
    elif case == CaseType.SPRING_BOOT:
        out["boot"] = list(rs.spring_boot_4)
        notes.append(f"transform{where}: spring_boot → Spring Boot 4 set (the umbrella migrates javax → jakarta "
                     "itself)")
    if "hystrix" in facts.libs:
        _hystrix_bucket(rs, out, notes, where)
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


def _hystrix_bucket(rs: RecipeSetConfig, out: dict[str, list[str]], notes: list[str], where: str) -> None:
    """분석기가 Hystrix를 찾았을 때 ``hystrix_resilience4j`` 세트를 넣고 노트 한 줄을 남긴다.
    세트가 비어 있으면 넣지 않고 Hystrix가 그대로 남는다는 노트만 남긴다."""
    ids = list(rs.hystrix_resilience4j)
    if ids:
        out["hystrix"] = ids
        notes.append(f"transform{where}: hystrix detected → Hystrix → Resilience4j set {_chain(ids)} before the Boot / "
                     "Framework sets (no version written: the restructure's version rules decide the new starter)")
    else:
        notes.append(f"transform{where}: hystrix detected, the hystrix_resilience4j set is empty → Hystrix stays at "
                     "its as-is version")


def _flatten(buckets: dict[str, list[str]]) -> list[str]:
    """묶음들을 실행 순서(``_BUCKETS``)대로 중복 없이 한 목록으로 합친다. jdk21 세트가 맨 앞에 온다."""
    seen: list[str] = []
    for key, _ in _BUCKETS:
        for i in buckets.get(key, []):
            if i not in seen:
                seen.append(i)
    return seen


def _union(parts: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    """여러 대상의 레시피 묶음을 하나로 합친다. MSA 루트에서 한 번에 실행할 때 쓴다."""
    out: dict[str, list[str]] = {k: [] for k, _ in _BUCKETS}
    for b in parts:
        for k, ids in b.items():
            out[k] += [i for i in ids if i not in out[k]]
    return out


def transform_recipes(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> list[RecipeRef]:
    """케이스의 변환 1회 레시피를 jdk21 세트부터 RecipeRef 목록으로 반환한다."""
    return [recipe_ref(i, cfg) for i in _flatten(_case_buckets(facts, cfg, notes, label))]


spring_recipes = transform_recipes          # 이전 이름. sbmig.plan이 둘 다 내보낸다


# ---------------------------------------------------------------------------
# 단계 생성
# ---------------------------------------------------------------------------
def _sfx(svc: Optional[str]) -> str:
    """MSA 서비스별 단계 id 접미어 ``@<서비스>``를 만든다."""
    return f"@{svc}" if svc else ""


def _transform_step(facts: _Facts, cfg: Config, notes: list[str], *, svc: Optional[str] = None,
                    target: Optional[str] = None, buckets: Optional[dict[str, list[str]]] = None,
                    title_case: Optional[str] = None) -> Optional[PlanStep]:
    """케이스의 레시피를 모두 한 번에 실행하는 ``transform.rewrite[@svc]`` 단계를 만든다.
    레시피는 as-is classpath에서 실행해야 타입 정보를 쓸 수 있다. 그래서 재구성 전에는 빌드하지 않는다.
    적용할 레시피가 없으면 노트만 남기고 None을 반환한다.

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
    notes.append(f"transform{where}: one OpenRewrite run ({_shorts(ids)}) — "
                 + ("then one compile, no fixing until the restructure is done (v2.1)" if cfg.plan.compile_after_rewrite
                    else "no build before the restructure (plan.compile_after_rewrite=false, v2.1o)"))
    return PlanStep(
        id=TRANSFORM + _sfx(svc), kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM,
        title="OpenRewrite in one run: " + " + ".join(parts) + (f" ({svc})" if svc else ""),
        recipes=recipes, target=target, params={"service": svc} if svc else {},
        reason=f"v2 case {title_case or facts.case.value}: Java 21 / jakarta / Spring Boot recipes in one run on the "
               "as-is classpath (type attribution), no build before the restructure (business decision)")


def _review_step(restructure_step: str, cfg: Config, notes: list[str]) -> Optional[PlanStep]:
    """첫 빌드 전에 LLM이 pom, 의존성, 모듈을 회사 규칙에 맞춰 검토하는 ``restructure.review`` 단계를 만든다.
    ``review.enabled``가 false면 노트만 남기고 None을 반환한다."""
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


def _no_compile_note(notes: list[str]) -> None:
    """재구성 전에는 빌드하지 않는다는 계획 노트를 남긴다. 같은 노트는 한 번만 넣는다.
    ``plan.compile_after_rewrite: true``면 이 노트 대신 ``transform.compile`` 단계가 들어간다."""
    note = ("compile: no build before the restructure (plan.compile_after_rewrite=false, v2.1o) — the error list starts "
            "with the restructure's pom corrections and the first verify build")
    if note not in notes:
        notes.append(note)


def _compile(notes: list[str], target: Optional[str] = None, svc: Optional[str] = None,
             note: bool = True) -> PlanStep:
    """OpenRewrite 뒤에 ``mvn clean compile``을 한 번 실행하는 ``transform.compile`` 단계를 만든다.
    오류는 오류 목록에 ``seen=compile``로 기록만 하고 고치지 않는다. 실행의 성공은 이 빌드가 아니라 검증 빌드로 정한다."""
    if note:
        notes.append("compile: one mvn clean compile after OpenRewrite → every error on the error list (seen "
                     "compile), no fixing (v2.1)")
    return PlanStep(id=COMPILE + _sfx(svc), kind=StepKind.BUILD, phase=Phase.TRANSFORM,
                    title="mvn clean compile once — errors to the error list, no fixing" + (f" ({svc})" if svc else ""),
                    build_goal="clean compile", target=target, params={"service": svc} if svc else {},
                    reason="v2.1: OpenRewrite's result compiled once; the error list keeps every error (seen compile) "
                           "and the build after the restructure adds only new ones")


def _verify(target: Optional[str] = None, svc: Optional[str] = None) -> PlanStep:
    """``mvn clean package``를 실행하는 ``verify.build`` 단계를 만든다. BUILD SUCCESS여야 실행이 성공한다."""
    return PlanStep(id="verify.build" + _sfx(svc), kind=StepKind.BUILD, phase=Phase.VERIFY,
                    title="final mvn clean package" + (f" ({svc})" if svc else ""), build_goal="clean package",
                    target=target, params={"service": svc} if svc else {},
                    reason="a run succeeds only on BUILD SUCCESS of mvn clean package")


def _service_libs(profile: ProjectProfile, svc: ServiceInfo) -> set[str]:
    """MSA 서비스 하나가 쓰는 라이브러리를 구한다.
    서비스 pom에서 찾은 라이브러리에 import로만 찾은 라이브러리를 더한다.
    프로필은 import를 서비스별로 나누지 않는다. 그래서 import 쪽은 저장소 전체 것을 더한다."""
    from sbmig.analyze.libraries import detect_libraries

    sub = [p for p in profile.poms if p.path in set(svc.poms)]
    own = {lib.name for lib in detect_libraries(sub)}
    import_only = {lib.name for lib in profile.libraries if not any(e.startswith("pom:") for e in lib.evidence)}
    return own | import_only


def _service_dir(s: ServiceInfo) -> str:
    """빌드 대상인 서비스 루트 pom의 디렉토리를 반환한다."""
    return posixpath.dirname(s.root_pom) or "."


def _module_path(pom_path: str, m: str) -> str:
    """``<module>`` 값을 그 모듈의 pom 경로로 바꾼다. 디렉토리면 ``/pom.xml``을 붙인다."""
    t = posixpath.normpath(posixpath.join(posixpath.dirname(pom_path) or ".", m))
    if not t.endswith(".xml"):
        t = posixpath.join(t, "pom.xml")
    return posixpath.normpath(t)


def _reactor_reach(profile: ProjectProfile, start: str) -> set[str]:
    """``start`` pom에서 ``<modules>``를 따라 도달하는 pom 경로를 모두 반환한다."""
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
    """루트 pom이 aggregator이고 그 reactor가 모든 서비스와 라이브러리를 포함하는지 확인한다."""
    rp = profile.root_pom
    if rp is None or not rp.is_aggregator:
        return False
    reach = _reactor_reach(profile, rp.path)
    return all(t.root_pom in reach for t in targets)


def _order_libraries(profile: ProjectProfile, libs: list[ServiceInfo]) -> list[ServiceInfo]:
    """공유 라이브러리 루트를 의존 순서로 정렬한다.
    다른 라이브러리가 쓰는 라이브러리가 먼저 온다. 순서가 같거나 순환하면 reactor 순서를 따른다.
    라이브러리는 서비스보다 먼저 변환한다. 서비스가 이관된 라이브러리를 봐야 한다."""
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
        nxt = ready[0] if ready else remaining[0]          # 순환이면 reactor 순서를 따른다
        out.append(nxt)
        remaining.remove(nxt)
    return out


def _msa_steps(profile: ProjectProfile, cfg: Config, notes: list[str]) -> list[PlanStep]:
    """MSA 계획을 만든다.
    공유 라이브러리 루트 -> 서비스별 ``transform.rewrite@<x>`` -> ``restructure.msa_pom``(디렉토리는 그대로
    두고 pom만) -> 구조 검토 -> 검증 빌드 순서다.
    라이브러리에는 Boot 세트를 주지 않는다.
    루트 aggregator가 모두를 포함하면 변환과 검증 빌드를 루트에서 한 번에 실행한다. 아니면 서비스별로 실행한다."""
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
        # 공유 라이브러리는 Boot 앱이 아니다. Boot 4 세트 대신 jakarta 계열만 받도록 spring_legacy로 본다
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
    if not no_sources and cfg.plan.compile_after_rewrite:
        if aggregated:
            steps.append(_compile(notes))
        else:
            for i, t in enumerate(targets):
                steps.append(_compile(notes, target=_service_dir(t), svc=t.name, note=i == 0))
    elif not no_sources:
        _no_compile_note(notes)
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


def boot_single_split(profile: Optional[ProjectProfile], cfg: Config) -> bool:
    """모듈이 하나인 Spring Boot 앱을 ``{name}-boot``로 감싸지 않고 회사 모듈로 나눌지 확인한다.
    ``layout.boot_as_is``가 ``in-place``, ``layout.boot_single_module``이 ``split``이고
    루트 pom에 모듈이 없을 때 True를 반환한다."""
    if profile is None or profile.case_type != CaseType.SPRING_BOOT:
        return False
    lay = cfg.layout
    if lay.boot_as_is != "in-place" or getattr(lay, "boot_single_module", "split") != "split":
        return False
    return profile.root_pom is not None and not profile.root_pom.modules


def _single_steps(profile: ProjectProfile, cfg: Config, notes: list[str]) -> list[PlanStep]:
    """단일 프로젝트 계획을 만든다. 소스가 없으면 pom만 고친다(``restructure.pom_only``).
    소스가 있으면 변환 -> 재구성 -> 구조 검토 -> 검증 빌드 순서다.
    Boot as-is(``layout.boot_as_is == "in-place"``)면 디렉토리를 그대로 두고 pom만 고친다(``restructure.boot_pom``).
    그 밖은 회사 레이아웃(``{name}-boot`` / ``-back`` / 웹 자원이 있으면 ``-front``)으로 나눈다(``restructure.apply``)."""
    steps: list[PlanStep] = []
    case = profile.case_type
    if case == CaseType.MSA:
        notes.append("msa without discovered services → planned as a single project")
    libs = {lib.name for lib in profile.libraries}
    facts = _Facts(case if case != CaseType.MSA else CaseType.SPRING_BOOT, libs, profile.source.javax_imports)
    if profile.source.java_files == 0 and profile.poms:
        # aggregator, BOM, 부모 pom만 있는 프로젝트다. pom만 고치고(부모, 필수, 선택, 정리) 검증 빌드를 한다
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
    if cfg.plan.compile_after_rewrite:
        steps.append(_compile(notes))
    else:
        _no_compile_note(notes)
    boot_split = boot_single_split(profile, cfg)
    if case == CaseType.SPRING_BOOT and cfg.layout.boot_as_is == "in-place" and not boot_split:
        # 회사 규칙: Boot as-is 프로젝트는 디렉토리를 그대로 두고 pom만 고친다
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
            where = (f"single-module Boot app → wrapped into {cfg.layout.module_path('boot', name)} "
                     "(layout.boot_single_module=wrap)")
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
        # 모듈이 하나인 Boot 앱을 나눌 때는 재구성이 Boot 전용 배치(Application 사용 클래스, Boot 정적 경로)를 더한다
        params: dict[str, Any] = {"mode": "layout", "starter": starter, **({"boot_single": True} if boot_split else {})}
        steps.append(PlanStep(
            id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE,
            title=f"restructure into {', '.join(m for m in mods if m)} with the in-house parent",
            params=params,
            reason=f"non-msa: company layout; boot module starter '{starter}' for {facts.case.value}"))
        if boot_split:
            rule = ("rule boot-single-module=split (layout.boot_as_is=in-place, single-module Boot app split like the "
                    "other cases)")
        elif case == CaseType.SPRING_BOOT:
            rule = "rule boot-as-is=layout (Boot split like the other cases)"
        else:
            rule = f"rule non-boot ({facts.case.value})"
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
            # web.xml 등록은 재구성이 WebXmlRegistrations로 옮긴다(sbmig.restructure.webxml_registrations)
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


def _settings_notes(cfg: Config, notes: list[str], optional: bool = True) -> None:
    """무시하는 루프 설정과 선택 아티팩트에 대한 노트를 남긴다.
    이 설정은 계획 흐름을 바꾸지 않는다. 선택 아티팩트는 재구성 단계에서 버전을 올린다.
    ``optional``이 False면 선택 아티팩트 노트는 넣지 않는다. jdk-jakarta 범위에는 재구성이 없다."""
    on = [k for k in V1_LOOP_PHASES if (pc := cfg.loop.phases.get(k)) is not None and pc.enabled]
    if on:
        notes.append(f"v2: no intermediate build-fix loops (loop.phases.{{{','.join(on)}}}.enabled=true ignored)")
    vc = cfg.loop.phases.get("verify")
    if vc is not None and not vc.enabled:
        notes.append("loop.phases.verify.enabled=false → the final build is not followed by the script fix loop")
    if not optional:
        return
    if cfg.artifacts.optional or cfg.artifacts.remove_when_present:
        notes.append("optional artifacts: bumped inside the restructure step (no separate bump step in v2)")
    else:
        notes.append("optional artifacts: none configured")


def build_plan(profile: ProjectProfile, cfg: Config, kb: Any = None,
               advisories: Optional[list[Any]] = None) -> MigrationPlan:
    """규칙 기반 이관 계획을 만든다.
    ``kb``는 쓰지 않는다. 인터페이스를 맞추려고 받기만 한다.
    권고(``advisories``)는 하나당 계획 노트 한 줄이 된다. 레시피는 더하지 않는다.
    Maven 프로젝트가 아니면 단계 없이 노트만 남긴다. 그러면 오케스트레이터가 실행을 중단한다.

    `advisories` = `sbmig.advisory.evaluate_advisories` findings."""
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
    reduced = profile.reduced_scope
    if reduced:
        # 사내 프레임워크 프로젝트는 JDK 21과 jakarta만 올린다. 범위 노트를 맨 앞에 둔다
        notes.insert(0, _scope_note(profile))
        _settings_notes(cfg, notes, optional=False)
        steps += _reduced_steps(profile, cfg, notes)
    else:
        _settings_notes(cfg, notes)
        if case == CaseType.MSA and profile.services:
            steps += _msa_steps(profile, cfg, notes)
        else:
            steps += _single_steps(profile, cfg, notes)
    for f in advisories or []:
        notes.append(f"advisory: {getattr(f, 'advisory_id', f)} → guide (LLM hints + Handover)")
    for w in profile.warnings:
        notes.append(f"warning: {w}")
    plan = MigrationPlan(profile_fingerprint=profile.fingerprint, case_type=case, steps=steps, notes=notes)
    if reduced:
        plan.scope = "jdk-jakarta"
    return plan


# ---------------------------------------------------------------------------
# 사내 프레임워크 프로젝트의 축소 계획(jdk-jakarta). 변환과 검증 빌드만 있고 재구성과 구조 검토는 없다
# ---------------------------------------------------------------------------
JSP_JAKARTA = "transform.jsp_jakarta"


def _scope_note(profile: ProjectProfile) -> str:
    """축소 계획의 첫 노트를 만든다. 무엇을 실행하고 무엇을 하지 않는지 담는다."""
    from sbmig.analyze.framework import summary_text
    fw = summary_text(profile.framework) if profile.framework is not None else "-"
    return (f"scope: jdk-jakarta — framework project ({fw}): Java 21 + jakarta recipes, then the verify build with "
            "the fix loop; no restructure, no structure review, the parent poms and the framework version are kept")


# 축소 계획에서 `framework.recipe_sets`의 세트 이름이 들어갈 묶음. 전체 계획과 같은 묶음을 쓴다
_REDUCED_SET_BUCKET: dict[str, str] = {
    "jdk21": "jdk21", "jakarta": "jakarta", "servlet_jakarta_10": "jakarta", "spring_framework_7": "framework",
    "spring_security_7": "security", "spring_batch_6": "batch", "hibernate_7": "hibernate", "spring_boot_4": "boot",
    "hystrix_resilience4j": "hystrix", "junit5": "junit5",
}


def _reduced_buckets(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> dict[str, list[str]]:
    """축소 계획의 레시피 묶음을 ``framework.recipe_sets``에 있는 세트로 정한다. 기본은 jdk21과 jakarta다.
    - jakarta 세트는 EE javax import가 없는 pure_java / unknown에는 넣지 않는다
    - servlet_jakarta_10 세트는 jakarta 세트가 들어갈 때만 jakarta 뒤에 넣는다
    - 나머지 세트는 목록에 있으면 조건 없이 넣는다. extra는 쓰지 않는다
    - 노트에는 넣은 세트와 목록에 있지만 뺀 세트를 남긴다"""
    rs = cfg.openrewrite.recipes
    out: dict[str, list[str]] = {k: [] for k, _ in _BUCKETS}
    where = f" [{label}]" if label else ""
    fw = getattr(cfg, "framework", None)
    sets = list(getattr(fw, "recipe_sets", None) or []) if fw is not None else ["jdk21", "jakarta"]
    case = facts.case
    planned: list[str] = []
    left: list[str] = []
    detail = ""
    jakarta_ok = False
    if "jakarta" in sets:
        if case in (CaseType.PURE_JAVA, CaseType.UNKNOWN):
            ee = ee_javax_packages(facts.javax, "javax-annotation" in facts.libs)
            jakarta_ok = bool(ee)
            if ee:
                detail = f" with Java EE javax imports ({', '.join(ee)})"
            else:
                left.append("jakarta (no Java EE javax imports)")
        else:
            jakarta_ok = True
    # servlet_jakarta_10은 jakarta 세트가 들어가는지 알아야 정할 수 있다. 그래서 jakarta를 맨 앞에 둔다
    ordered = [n for n in sets if n == "jakarta"] + [n for n in sets if n != "jakarta"]
    for name in ordered:
        if name not in _REDUCED_SET_BUCKET:
            continue
        if name == "jakarta" and not jakarta_ok:
            continue
        if name == "servlet_jakarta_10" and not jakarta_ok:
            left.append("servlet_jakarta_10 (no jakarta set planned)")
            continue
        ids = list(getattr(rs, name, []) or [])
        if not ids:
            left.append(f"{name} (the set is empty)")
            continue
        bucket = out[_REDUCED_SET_BUCKET[name]]
        bucket += [i for i in ids if i not in bucket]
        planned.append(name)
    shown = [n for n in sets if n in planned]
    msg = (f"transform{where}: jdk-jakarta scope, {case.value}{detail} → recipe sets: "
           + (", ".join(shown) or "none"))
    if left:
        msg += "; left out: " + ", ".join(left)
    notes.append(msg)
    return out


def _reduced_transform_step(buckets: dict[str, list[str]], cfg: Config, notes: list[str], *,
                            svc: Optional[str] = None, target: Optional[str] = None) -> Optional[PlanStep]:
    """축소 계획의 ``transform.rewrite[@svc]`` 단계를 만든다. 적용할 레시피가 없으면 None을 반환한다."""
    where = f" [{svc}]" if svc else ""
    ids = _flatten(buckets)
    if not ids:
        notes.append(f"transform{where}: no recipes apply → {TRANSFORM}{_sfx(svc)} omitted")
        return None
    parts = [lbl for key, lbl in _BUCKETS if buckets.get(key)]
    notes.append(f"transform{where}: one OpenRewrite run ({_shorts(ids)}) on the as-is classpath, no restructure "
                 "(jdk-jakarta scope)")
    return PlanStep(
        id=TRANSFORM + _sfx(svc), kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM,
        title="OpenRewrite in one run: " + " + ".join(parts) + (f" ({svc})" if svc else ""),
        recipes=[recipe_ref(i, cfg) for i in ids], target=target, params={"service": svc} if svc else {},
        reason="framework project (scope jdk-jakarta): Java 21 / jakarta recipes only, on the as-is classpath; the "
               "parents, the module layout and the framework version stay as they are")


def _reduced_steps(profile: ProjectProfile, cfg: Config, notes: list[str]) -> list[PlanStep]:
    """사내 프레임워크 프로젝트의 단계를 만든다.
    순서는 ``transform.rewrite`` -> ``transform.jsp_jakarta`` -> ``verify.build``다.
    재구성, 구조 검토, ``transform.compile``은 넣지 않는다.

    `transform.jsp_jakarta` is one step for the whole tree. MSA: per target (library roots first) or one root
    run when the root aggregator covers every target; verify at the root when the root pom aggregates, else
    per target."""
    from sbmig.analyze.classifier import is_library_root

    steps: list[PlanStep] = []
    javax = profile.source.javax_imports
    no_sources = profile.source.java_files == 0
    if cfg.plan.compile_after_rewrite:
        notes.append("compile: plan.compile_after_rewrite=true does not apply in the jdk-jakarta scope (no "
                     f"{COMPILE}); the verify build is the first build")
    case = profile.case_type
    msa = case == CaseType.MSA and bool(profile.services)
    if msa:
        libs = _order_libraries(profile, [s for s in profile.services if is_library_root(s, profile)])
        svcs = [s for s in profile.services if not is_library_root(s, profile)]
        targets = libs + svcs
        for t in targets:
            notes.append(f"msa {'library root' if t in libs else 'service'} {t.name} ({_service_dir(t)}): sub-case "
                         f"{t.case_type.value}")
        if no_sources:
            notes.append("msa: no java sources → no transform step (verify only)")
        elif _aggregator_covers(profile, targets):
            union = _union([_reduced_buckets(_Facts(t.case_type, _service_libs(profile, t), javax), cfg, notes, t.name)
                            for t in targets])
            notes.append(f"msa: {profile.root_pom.path if profile.root_pom else 'pom.xml'} aggregates every "
                         f"service/library → one {TRANSFORM} at the repo root")
            st = _reduced_transform_step(union, cfg, notes)
            steps += [st] if st else []
        else:
            for t in targets:
                b = _reduced_buckets(_Facts(t.case_type, _service_libs(profile, t), javax), cfg, notes, t.name)
                st = _reduced_transform_step(b, cfg, notes, svc=t.name, target=_service_dir(t))
                steps += [st] if st else []
    else:
        if case == CaseType.MSA:
            notes.append("msa without discovered services → planned as a single project")
        if no_sources:
            notes.append("no java sources → no transform step (verify only)")
        else:
            b = _reduced_buckets(_Facts(case, {lib.name for lib in profile.libraries}, javax), cfg, notes)
            st = _reduced_transform_step(b, cfg, notes)
            steps += [st] if st else []
    jakarta_ids = set(cfg.openrewrite.recipes.jakarta)
    jakarta_planned = bool(jakarta_ids) and any(r.id in jakarta_ids for st in steps for r in st.recipes)
    src = profile.source
    if jakarta_planned and (src.jsp_paths or src.jsp_count):
        if cfg.layout.jsp_jakarta_rename:
            steps.append(PlanStep(
                id=JSP_JAKARTA, kind=StepKind.RESTRUCTURE, phase=Phase.TRANSFORM,
                title="JSP javax → jakarta rename (whole tree)", params={"mode": "jsp_jakarta"},
                reason="OpenRewrite does not parse JSPs; the javax EE names in JSP / tag files are renamed in place"))
            notes.append(f"transform: {len(src.jsp_paths) or src.jsp_count} JSP(s) → {JSP_JAKARTA} renames the javax "
                         "EE names in place (layout.jsp_jakarta_rename=true)")
        else:
            notes.append("transform: JSPs keep their javax names (layout.jsp_jakarta_rename=false) → follow-up for "
                         "the owners")
    rp = profile.root_pom
    if msa and not (rp is not None and rp.is_aggregator):
        for t in targets:
            steps.append(_verify(target=_service_dir(t), svc=t.name))
        notes.append("verify: no root aggregator pom → one mvn clean package per service"
                     + (" and library root" if libs else ""))
    else:
        steps.append(_verify())
        if msa:
            notes.append(f"verify: root aggregator {rp.path} → one mvn clean package for the whole repo")
    return steps


# ---------------------------------------------------------------------------
# 선택 기능인 LLM 계획 조언자(``plan.advisor: llm``, 기본은 꺼짐). 레시피를 더하기만 하고 빼지 않는다
# ---------------------------------------------------------------------------
# 아래 상수는 import할 때 저장소 카탈로그(knowledge/recipes/catalog.yml)로 만든 기본값이다.
# `advise_plan(cfg=...)`은 `cfg.recipe_catalog_file`을 다시 읽는다. 허용 목록은 `v2_recipe_scope` 범위로 좁힌다
_DEFAULT_CATALOG = default_catalog()

# 조언자가 더할 수 있는 레시피
ADVISOR_CATALOG: dict[str, str] = _DEFAULT_CATALOG.advisor_catalog()

# 계획된 묶음 레시피가 이미 포함하는 레시피. 조언자에게 알려 주고 중복 제안을 걸러 낸다
ADVISOR_COVERAGE: dict[str, list[str]] = _DEFAULT_CATALOG.coverage()

# 작은 모델은 "혹시 몰라" 레시피를 제안하곤 한다. 그래서 제안마다 프로필 근거를 요구한다
ADVISOR_GATES: dict[str, Any] = _DEFAULT_CATALOG.gates()

_has_javax_ee = GATES["has_javax_ee"]          # 이전 이름


def v2_recipe_scope(cfg: Optional[Config] = None) -> set[str]:
    """실행할 수 있는 레시피 id를 모두 반환한다(설정 세트와 extra).
    조언자의 허용 목록을 이 범위로 좁힐 때 쓴다."""
    rs = cfg.openrewrite.recipes if cfg is not None else RecipeSetConfig()
    ids = {*rs.jdk21, *rs.jakarta, *rs.servlet_jakarta_10, *rs.spring_boot_4, *rs.spring_framework_7,
           *rs.spring_security_7, *rs.spring_batch_6, *rs.hibernate_7, *rs.hystrix_resilience4j, *rs.junit5}
    for extra in (rs.extra or {}).values():
        ids.update(extra)
    return ids


def advisor_whitelist(cfg: Optional[Config] = None) -> dict[str, str]:
    """실행 범위 안에서 조언자가 더할 수 있는 카탈로그 레시피를 카탈로그 순서로 반환한다."""
    cat = default_catalog(cfg) if cfg is not None else _DEFAULT_CATALOG
    scope = v2_recipe_scope(cfg)
    return {rid: desc for rid, desc in (cat.advisor_catalog() or ADVISOR_CATALOG).items() if rid in scope}


def _covered_by(planned: set[str], coverage: Optional[dict[str, list[str]]] = None) -> dict[str, str]:
    """레시피 id마다 그것을 이미 포함하는 계획된 묶음 레시피를 반환한다."""
    out: dict[str, str] = {}
    for comp, inner in (ADVISOR_COVERAGE if coverage is None else coverage).items():
        if comp in planned:
            for rid in inner:
                out.setdefault(rid, comp)
    return out


def _artifact_for(recipe_id: str, catalog_obj: Any = None) -> Optional[str]:
    """조언자가 더한 레시피의 jar를 반환한다. 카탈로그를 주지 않으면 import 시점의 기본 카탈로그를 쓴다."""
    cat = catalog_obj if catalog_obj is not None else _DEFAULT_CATALOG
    return cat.artifact_for(recipe_id) if recipe_id in cat else prefix_artifact(recipe_id)


def _advisor_prompt(plan: MigrationPlan, profile: ProjectProfile, catalog: dict[str, str],
                    coverage: Optional[dict[str, list[str]]] = None) -> str:
    """조언자 프롬프트를 만든다.
    프로필 요약, 규칙 기반 계획, 더할 수 있는 레시피 목록, 이미 포함된 레시피, 답 형식(JSON)을 담는다."""
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
    """조언자 답에서 (추가 제안, 최대 5개의 우려, 오류 문장)을 꺼낸다. 코드 펜스가 있어도 읽는다."""
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
    """규칙 기반 계획을 LLM이 검토하게 한다. ``plan.advisor: llm``일 때 쓰고 모델은 ``llm.recipe_model``이다.
    허용 목록에 있는 레시피만 변환 단계에 더하고 우려는 노트에 남긴다. 레시피를 빼지는 않는다.
    모르는 id, 이미 계획된 id, 프로필 근거가 없는 제안, 깨진 답은 노트만 남기고 무시한다.
    예외를 던지지 않는다. 계획 단계가 멈추면 안 된다.

    The whitelist is `catalog` when given, else `advisor_whitelist(cfg)`.
    A jdk-jakarta plan is never changed."""
    if llm is None or (cfg is not None and cfg.plan.advisor == "none"):
        return plan
    if plan.scope == "jdk-jakarta":
        # 사내 프레임워크 프로젝트는 JDK와 jakarta 세트만 실행한다. 레시피를 더하지 않는다
        plan.notes.append("advisor: jdk-jakarta scope (framework project) → plan unchanged, no recipe added")
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
    except Exception as e:  # 조언자 실패로 계획을 멈추지 않는다
        plan.notes.append(f"advisor: LLM call failed ({type(e).__name__}: {e}); plan unchanged")
        return plan
    from sbmig.core.models import LLMUsage, asked_model_name, served_beside
    used = asked_model_name(getattr(resp, "model", ""), model, getattr(llm, "model", ""),
                            cfg.llm.model if cfg is not None else None)
    try:
        plan.llm_usage = plan.llm_usage.add(LLMUsage(model=used, calls=1, input_tokens=int(resp.input_tokens or 0),
                                                     output_tokens=int(resp.output_tokens or 0),
                                                     served=served_beside(used, list(
                                                         getattr(resp, "served_models", None) or []))))
    except Exception:  # pragma: no cover - 사용량 기록일 뿐이다
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
