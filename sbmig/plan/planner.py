"""Rule-based migration planner (no LLM): ProjectProfile + Config (+ KB) → MigrationPlan.

Step ids (non-MSA):  jdk21.rewrite, jdk21.loop, spring.rewrite, spring.optional_bump, spring.loop,
restructure.apply, restructure.loop, verify.build.

* `spring.optional_bump` (StepKind.RESTRUCTURE, phase SPRING, params {"mode": "optional_bump"}, optional=True) is
  emitted when `cfg.plan.optional_bump_phase == "spring"` and optional / remove_when_present artifacts are configured;
  with `"restructure"` the bumps happen inside the restructure step only (previous behaviour).
* MSA: per target `jdk21.rewrite@<x>`, `jdk21.loop@<x>`, `spring.rewrite@<x>`, `spring.optional_bump@<x>`
  (params also carry `service` / `target`), `spring.loop@<x>` (target = root dir) — shared **library roots first**
  (dependency / reactor order), then the services; then `restructure.msa_pom` (params `services`, `parent_policy`,
  `library_roots` = library root pom paths), `restructure.loop` and `verify.build` (or `verify.build@<x>` per target
  when there is no root aggregator). With `layout.msa_parent_policy == "top-aggregator"` and a root aggregator whose
  reactor covers every target, a single un-suffixed rewrite/loop chain runs at the repo root instead.
* No java sources (pom-only, S3): no jdk21/spring steps; `restructure.pom_only` (StepKind.MSA_POM, mode msa_pom) or
  `restructure.msa_pom`, the restructure loop and verify.
* `profile.build_tool != "maven"` (S4): zero steps and a note (the orchestrator aborts).
"""
from __future__ import annotations

import logging
import posixpath
import re
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import (CaseType, MigrationPlan, Phase, PlanStep, ProjectProfile, RecipeRef, ServiceInfo,
                               StepKind)

from .pre_handlers import pre_handler_reasons

log = logging.getLogger("sbmig.plan")

ART_MIGRATE = "org.openrewrite.recipe:rewrite-migrate-java"
ART_SPRING = "org.openrewrite.recipe:rewrite-spring"
ART_TESTING = "org.openrewrite.recipe:rewrite-testing-frameworks"
ART_STATIC = "org.openrewrite.recipe:rewrite-static-analysis"
SERVLET_RECIPE = "com.company.migrate.ServletToJakartaAndBoot"

# javax packages that are Java EE (→ jakarta); JDK packages like javax.swing/sql/crypto/naming/net are excluded
EE_JAVAX = ("javax.servlet", "javax.persistence", "javax.xml.bind", "javax.validation", "javax.inject",
            "javax.ws", "javax.jms", "javax.mail", "javax.ejb", "javax.faces", "javax.websocket", "javax.el",
            "javax.json", "javax.batch", "javax.enterprise", "javax.activation", "javax.resource",
            "javax.transaction", "javax.interceptor", "javax.decorator", "javax.jws", "javax.xml.ws",
            "javax.security.enterprise")
# javax.annotation is shared with JSR-305 (@Nullable) and the JDK (javax.annotation.processing): count it as EE
# only when the analyzer confirmed EE usage (library "javax-annotation").


def artifact_for(recipe_id: str) -> Optional[str]:
    if recipe_id.startswith("org.openrewrite.java.spring"):
        return ART_SPRING
    if recipe_id.startswith("org.openrewrite.java.migrate"):
        return ART_MIGRATE
    if recipe_id.startswith("org.openrewrite.java.testing"):
        return ART_TESTING
    if recipe_id.startswith("org.openrewrite.staticanalysis"):
        return ART_STATIC
    return None


def _project_recipe_file(recipe_id: str, cfg: Config) -> Optional[str]:
    d = cfg.base_recipe_dir
    try:
        for f in sorted([*d.glob("*.yml"), *d.glob("*.yaml")]):
            if re.search(rf"^name:\s*{re.escape(recipe_id)}\s*$", f.read_text(encoding="utf-8"), re.M):
                return str(f.resolve())
    except OSError:  # pragma: no cover - unreadable dir
        pass
    return None


def recipe_ref(recipe_id: str, cfg: Config) -> RecipeRef:
    if recipe_id.startswith("com.company."):
        return RecipeRef(id=recipe_id, source="project", file=_project_recipe_file(recipe_id, cfg))
    return RecipeRef(id=recipe_id, source="builtin", artifact=artifact_for(recipe_id))


def _refs(ids: list[str], cfg: Config, into: list[RecipeRef]) -> None:
    for i in ids:
        if not any(r.id == i for r in into):
            into.append(recipe_ref(i, cfg))


def ee_javax_packages(profile_source_javax: dict[str, int], has_javax_annotation: bool) -> list[str]:
    hits = [k for k in profile_source_javax if any(k == p or k.startswith(p + ".") for p in EE_JAVAX)]
    if has_javax_annotation and "javax.annotation" in profile_source_javax:
        hits.append("javax.annotation")
    return sorted(set(hits))


# ---------------------------------------------------------------------------
# per-case spring recipes
# ---------------------------------------------------------------------------
class _Facts:
    """The few profile facts the recipe rules need; for MSA computed per service."""

    def __init__(self, case: CaseType, libs: set[str], javax: dict[str, int]):
        self.case = case
        self.libs = libs
        self.javax = javax


def spring_recipes(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> list[RecipeRef]:
    rs = cfg.openrewrite.recipes
    out: list[RecipeRef] = []
    where = f" [{label}]" if label else ""
    case = facts.case
    if case == CaseType.PURE_JAVA:
        ee = ee_javax_packages(facts.javax, "javax-annotation" in facts.libs)
        if ee:
            _refs(rs.jakarta, cfg, out)
            notes.append(f"spring{where}: pure_java with Java EE javax imports ({', '.join(ee)}) → jakarta recipes")
        else:
            notes.append(f"spring{where}: pure_java without Java EE javax imports → no jakarta migration")
    elif case == CaseType.SERVLET_TOMCAT:
        out.append(RecipeRef(id=SERVLET_RECIPE, source="project", file=_project_recipe_file(SERVLET_RECIPE, cfg)))
        notes.append(f"spring{where}: servlet_tomcat → {SERVLET_RECIPE} (javax→jakarta + servlet-api swap)")
    elif case == CaseType.SPRING_LEGACY:
        _refs(rs.jakarta, cfg, out)
        _refs(rs.spring_framework_7, cfg, out)
        msg = f"spring{where}: spring_legacy → jakarta + Spring Framework 7"
        if "spring-security" in facts.libs:
            _refs(rs.spring_security_7, cfg, out)
            msg += " + Spring Security 7 (spring-security detected)"
        notes.append(msg)
    elif case == CaseType.SPRING_BOOT:
        _refs(rs.spring_boot_4, cfg, out)
        notes.append(f"spring{where}: spring_boot → Boot 4.0 umbrella + 4.1 properties + 4.1 version pin")
    if "junit4" in facts.libs and case != CaseType.UNKNOWN:
        _refs(rs.junit5, cfg, out)
        notes.append(f"spring{where}: junit4 detected → JUnit 4→5 migration")
    extra = rs.extra.get(case.value, []) + rs.extra.get("all", [])
    if extra:
        _refs(extra, cfg, out)
        notes.append(f"spring{where}: extra recipes from config ({case.value}/all): {', '.join(extra)}")
    return out


def kb_recipes(kb: Any, cfg: Config, notes: list[str]) -> list[RecipeRef]:
    if kb is None or not cfg.kb.use_active_recipes:
        return []
    try:
        ids = list(kb.active_recipe_ids())
        files = list(kb.active_recipe_files())
    except Exception as e:  # a missing/broken KB must never break planning
        log.warning("knowledge base unavailable for planning: %s", e)
        notes.append(f"kb: active recipes unavailable ({type(e).__name__}); planning without them")
        return []
    out = [RecipeRef(id=i, source="kb", file=str(f) if f is not None else None)
           for i, f in zip(ids, files + [None] * (len(ids) - len(files)))]
    if out:
        notes.append(f"kb: {len(out)} active KB recipe(s) appended to the spring phase: "
                     + ", ".join(r.id for r in out))
    if getattr(cfg.kb, "try_candidates_in_plan", False):
        # Candidates are whitelisted declarative recipes (ChangeType, dependency/property changes …): no-ops when the
        # old symbol is absent, and only reliable while the as-is classpath is intact (type attribution). Running
        # them here, next to the plan's recipes, is what lets a candidate earn its promotion on the next project.
        try:
            cands = [r for r in kb.list_recipes("candidate") if r.id not in ids]
        except Exception as e:  # pragma: no cover - optional capability
            log.warning("knowledge base candidates unavailable for planning: %s", e)
            cands = []
        if cands:
            out += [RecipeRef(id=r.id, source="kb", file=str(r.path) if r.path else None) for r in cands]
            notes.append(f"kb: {len(cands)} candidate KB recipe(s) tried in the spring phase (credited by the build): "
                         + ", ".join(r.id for r in cands))
    return out


# ---------------------------------------------------------------------------
# step factories
# ---------------------------------------------------------------------------
def _sfx(svc: Optional[str]) -> str:
    return f"@{svc}" if svc else ""


def _loop_step(phase_key: str, phase: Phase, cfg: Config, reason: str, *, step_id: str,
               target: Optional[str] = None, params: Optional[dict[str, Any]] = None) -> Optional[PlanStep]:
    pc = cfg.loop.phases.get(phase_key)
    if pc is None or not pc.enabled:
        return None
    return PlanStep(id=step_id, kind=StepKind.BUILD_FIX_LOOP, phase=phase,
                    title=f"{phase_key} build-fix loop (mvn {pc.goal})", build_goal=pc.goal,
                    max_iterations=pc.max_iterations, target=target, reason=reason, params=params or {})


def _optional_bump_wanted(cfg: Config) -> bool:
    return cfg.plan.optional_bump_phase == "spring" and bool(cfg.artifacts.optional or cfg.artifacts.remove_when_present)


def _optional_bump_step(svc: Optional[str], target: Optional[str]) -> PlanStep:
    params: dict[str, Any] = {"mode": "optional_bump"}
    if svc:
        params["service"] = svc
        params["target"] = target
    return PlanStep(
        id="spring.optional_bump" + _sfx(svc), kind=StepKind.RESTRUCTURE, phase=Phase.SPRING,
        title="bump in-house optional artifacts (+ remove_when_present)" + (f" ({svc})" if svc else ""),
        params=params, target=target, optional=True,
        reason="jakarta builds of in-house libraries must be on the classpath before the first spring build "
               "(plan.optional_bump_phase=spring); no moves, no parent change")


def _recipe_phase_steps(facts: _Facts, cfg: Config, kb_refs: list[RecipeRef], notes: list[str], *,
                        svc: Optional[str] = None, target: Optional[str] = None,
                        recipes: Optional[list[RecipeRef]] = None, title_case: Optional[str] = None) -> list[PlanStep]:
    """jdk21.rewrite, jdk21.loop, spring.rewrite, spring.optional_bump, spring.loop (ids suffixed `@svc`).

    `recipes` overrides the per-case spring recipes (MSA single root rewrite: union of the services' recipes)."""
    steps: list[PlanStep] = []
    params = {"service": svc} if svc else {}
    jdk = [recipe_ref(i, cfg) for i in cfg.openrewrite.recipes.jdk21]
    if jdk:
        steps.append(PlanStep(
            id="jdk21.rewrite" + _sfx(svc), kind=StepKind.OPENREWRITE, phase=Phase.JDK21,
            title="OpenRewrite: upgrade to Java 21" + (f" ({svc})" if svc else ""), recipes=jdk, target=target,
            params=dict(params), reason="every case runs UpgradeToJava21 first (type attribution on as-is classpath)"))
    if cfg.plan.intermediate_builds:
        s = _loop_step("jdk21", Phase.JDK21, cfg, "gate: the JDK 21 rewrite must compile before Spring recipes",
                       step_id="jdk21.loop" + _sfx(svc), target=target, params=dict(params))
        if s:
            steps.append(s)
    label = svc or ""
    if recipes is None:
        recipes = spring_recipes(facts, cfg, notes, label)
    recipes = list(recipes) + [r for r in kb_refs if not any(x.id == r.id for x in recipes)]
    if recipes:
        steps.append(PlanStep(
            id="spring.rewrite" + _sfx(svc), kind=StepKind.OPENREWRITE, phase=Phase.SPRING,
            title=f"OpenRewrite: {title_case or facts.case.value} → jakarta / Spring Boot 4.1"
                  + (f" ({svc})" if svc else ""),
            recipes=recipes, target=target, params=dict(params),
            reason=f"case {title_case or facts.case.value}: recipes per ARCHITECTURE §2 case matrix"))
    else:
        notes.append(f"spring{f' [{label}]' if label else ''}: no spring-phase recipes apply → spring.rewrite omitted")
    if _optional_bump_wanted(cfg):
        steps.append(_optional_bump_step(svc, target))
    s = _loop_step("spring", Phase.SPRING, cfg, "gate: sources and tests must compile after the Spring phase",
                   step_id="spring.loop" + _sfx(svc), target=target, params=dict(params))
    if s:
        steps.append(s)
    return steps


def _verify(target: Optional[str] = None, svc: Optional[str] = None) -> PlanStep:
    return PlanStep(id="verify.build" + _sfx(svc), kind=StepKind.BUILD, phase=Phase.VERIFY,
                    title="final mvn clean package" + (f" ({svc})" if svc else ""), build_goal="clean package",
                    target=target, params={"service": svc} if svc else {},
                    reason="a run succeeds only on BUILD SUCCESS of mvn clean package")


def _service_libs(profile: ProjectProfile, svc: ServiceInfo) -> set[str]:
    """Libraries of one MSA service: pom evidence from the service's own poms, plus repo-wide libraries known
    only from imports (imports are not attributed per service in the profile)."""
    from sbmig.analyze.libraries import detect_libraries

    sub = [p for p in profile.poms if p.path in set(svc.poms)]
    own = {lib.name for lib in detect_libraries(sub)}
    import_only = {lib.name for lib in profile.libraries if not any(e.startswith("pom:") for e in lib.evidence)}
    return own | import_only


def _service_dir(s: ServiceInfo) -> str:
    return posixpath.dirname(s.root_pom) or "."


def _module_path(pom_path: str, m: str) -> str:
    t = posixpath.normpath(posixpath.join(posixpath.dirname(pom_path) or ".", m))
    if not t.endswith(".xml"):
        t = posixpath.join(t, "pom.xml")
    return posixpath.normpath(t)


def _reactor_reach(profile: ProjectProfile, start: str) -> set[str]:
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
    rp = profile.root_pom
    if rp is None or not rp.is_aggregator:
        return False
    reach = _reactor_reach(profile, rp.path)
    return all(t.root_pom in reach for t in targets)


def _order_libraries(profile: ProjectProfile, libs: list[ServiceInfo]) -> list[ServiceInfo]:
    """Libraries in dependency order (a library another library depends on comes first), ties by reactor order."""
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


def _msa_steps(profile: ProjectProfile, cfg: Config, kb_refs: list[RecipeRef], notes: list[str]) -> list[PlanStep]:
    from sbmig.analyze.classifier import is_library_root

    steps: list[PlanStep] = []
    libs = _order_libraries(profile, [s for s in profile.services if is_library_root(s, profile)])
    svcs = [s for s in profile.services if not is_library_root(s, profile)]
    targets = libs + svcs
    for lib in libs:
        notes.append(f"msa library root {lib.name} ({_service_dir(lib)}): shared module, migrated before the services "
                     f"as sub-case {lib.case_type.value} (jakarta / Framework recipes, never the Boot umbrella)")
    facts_of: dict[str, _Facts] = {}
    for t in targets:
        case = t.case_type
        if t in libs and case in (CaseType.SPRING_BOOT, CaseType.MSA):
            case = CaseType.SPRING_LEGACY
        facts_of[t.name] = _Facts(case, _service_libs(profile, t), profile.source.javax_imports)
        if t in svcs:
            notes.append(f"msa service {t.name} ({_service_dir(t)}): sub-case {t.case_type.value}, "
                         f"boot {t.spring_boot_version or '-'}, java {t.java_version or '-'}")
    no_sources = profile.source.java_files == 0
    single = cfg.layout.msa_parent_policy == "top-aggregator" and _aggregator_covers(profile, targets)
    if no_sources:
        notes.append("msa: no java sources → no OpenRewrite / build-fix steps for jdk21 and spring (pom edits + verify)")
    elif single:
        union: list[RecipeRef] = []
        for t in targets:
            for r in spring_recipes(facts_of[t.name], cfg, notes, t.name):
                if not any(x.id == r.id for x in union):
                    union.append(r)
        notes.append(f"msa: top-aggregator policy and {profile.root_pom.path if profile.root_pom else 'pom.xml'} "
                     "aggregates every service/library → one rewrite + loop at the repo root (union of the "
                     "sub-case recipes) instead of per service")
        steps += _recipe_phase_steps(_Facts(CaseType.MSA, set(), profile.source.javax_imports), cfg, kb_refs, notes,
                                     recipes=union, title_case="msa (all services)")
    else:
        for t in targets:
            steps += _recipe_phase_steps(facts_of[t.name], cfg, kb_refs, notes, svc=t.name, target=_service_dir(t))
    params: dict[str, Any] = {"mode": "msa_pom", "services": [s.name for s in svcs],
                              "parent_policy": cfg.layout.msa_parent_policy}
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
    s = _loop_step("restructure", Phase.RESTRUCTURE, cfg, "gate: every service must package after pom edits",
                   step_id="restructure.loop")
    if s:
        steps.append(s)
    rp = profile.root_pom
    if rp is not None and rp.is_aggregator:
        steps.append(_verify())
        notes.append(f"verify: root aggregator {rp.path} → one mvn clean package for the whole repo")
    else:
        for t in targets:
            steps.append(_verify(target=_service_dir(t), svc=t.name))
        notes.append("verify: no root aggregator pom → one mvn clean package per service"
                     + (" and library root" if libs else ""))
    return steps


def _single_steps(profile: ProjectProfile, cfg: Config, kb_refs: list[RecipeRef], notes: list[str]) -> list[PlanStep]:
    steps: list[PlanStep] = []
    case = profile.case_type
    if case == CaseType.MSA:
        notes.append("msa without discovered services → planned as a single project")
    libs = {lib.name for lib in profile.libraries}
    libs_facts = _Facts(case if case != CaseType.MSA else CaseType.SPRING_BOOT, libs, profile.source.javax_imports)
    if profile.source.java_files == 0 and profile.poms:
        # S3: pom-only aggregator / BOM / parent → pom edits only (parent, mandatory, optional, cleanup) + verify
        notes.append("no java sources → no OpenRewrite / build-fix steps for jdk21 and spring; restructure is "
                     "limited to pom edits (no module split, no Application)")
        steps.append(PlanStep(
            id="restructure.pom_only", kind=StepKind.MSA_POM, phase=Phase.RESTRUCTURE,
            title="pom-only parent / mandatory / optional edits (no sources)",
            params={"mode": "msa_pom", "services": [], "parent_policy": cfg.layout.msa_parent_policy,
                    "pom_only": True},
            reason="pom-only project: nothing to split; align the root pom(s) to the in-house parent"))
        s = _loop_step("restructure", Phase.RESTRUCTURE, cfg, "gate: the poms must package after the edits",
                       step_id="restructure.loop")
        if s:
            steps.append(s)
        steps.append(_verify())
        return steps
    steps += _recipe_phase_steps(libs_facts, cfg, kb_refs, notes)
    starter = cfg.layout.starter_by_case.get(libs_facts.case.value, "auto")
    steps.append(PlanStep(
        id="restructure.apply", kind=StepKind.RESTRUCTURE, phase=Phase.RESTRUCTURE,
        title="restructure into module/{comp-jar,boot-jar} with the in-house parent",
        params={"mode": "layout", "starter": starter},
        reason=f"non-msa: company layout; boot-jar starter '{starter}' for {libs_facts.case.value}"))
    notes.append(f"restructure: layout split, starter '{starter}'"
                 + (f", multi-module policy '{cfg.layout.multi_module_policy}'" if profile.is_multi_module else ""))
    if libs_facts.case == CaseType.SERVLET_TOMCAT and profile.source.has_web_xml:
        notes.append("followup: web.xml wiring is not migrated automatically (runtime owners)")
    if libs_facts.case == CaseType.SPRING_LEGACY and profile.source.xml_bean_configs:
        notes.append(f"restructure: {len(profile.source.xml_bean_configs)} XML bean config(s) → @ImportResource")
    s = _loop_step("restructure", Phase.RESTRUCTURE, cfg, "gate: the restructured project must package",
                   step_id="restructure.loop")
    if s:
        steps.append(s)
    steps.append(_verify())
    return steps


def build_plan(profile: ProjectProfile, cfg: Config, kb: Any = None) -> MigrationPlan:
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
    if not cfg.plan.intermediate_builds:
        notes.append("plan.intermediate_builds=false → no jdk21 build-fix loop (JDK 21 errors are fixed in the spring loop)")
    for key, pc in cfg.loop.phases.items():
        if not pc.enabled:
            notes.append(f"loop.phases.{key}.enabled=false → {key} build-fix loop omitted")
    if cfg.plan.optional_bump_phase == "spring":
        if _optional_bump_wanted(cfg):
            notes.append("optional artifacts: bumped right after spring.rewrite (spring.optional_bump, "
                         "plan.optional_bump_phase=spring) so jakarta builds of in-house libraries are on the "
                         "classpath before the first spring build; restructure finds nothing left to bump")
        else:
            notes.append("optional artifacts: none configured → no spring.optional_bump step")
    else:
        notes.append("optional artifacts: bumped during restructure (plan.optional_bump_phase=restructure)")
    kb_refs = kb_recipes(kb, cfg, notes)

    if case == CaseType.MSA and profile.services:
        steps += _msa_steps(profile, cfg, kb_refs, notes)
    else:
        steps += _single_steps(profile, cfg, kb_refs, notes)

    pre = pre_handler_reasons(profile, cfg)
    if pre:
        notes.append("pre-emptive handlers: " + "; ".join(f"{h} ({why})" for h, why in pre))
    for w in profile.warnings:
        notes.append(f"warning: {w}")
    return MigrationPlan(profile_fingerprint=profile.fingerprint, case_type=case, steps=steps, notes=notes)


def advise_plan(plan: MigrationPlan, profile: ProjectProfile, llm: Any = None,
                cfg: Optional[Config] = None) -> MigrationPlan:
    """Optional LLM review of the rule-based plan.

    Returns the plan unchanged when no LLM is given or `cfg.plan.advisor == "none"`.
    TODO(plan-advisor): send plan + profile summary to the LLM, accept only additive recipe suggestions from a
    whitelist, record them in plan.notes.
    """
    if llm is None or (cfg is not None and cfg.plan.advisor == "none"):
        return plan
    raise NotImplementedError("LLM plan advisor is not implemented yet (set plan.advisor: none)")
