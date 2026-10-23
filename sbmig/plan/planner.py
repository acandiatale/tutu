"""Rule-based migration planner (no LLM): ProjectProfile + Config (+ KB) → MigrationPlan.

Step ids (non-MSA):  jdk21.rewrite, jdk21.loop, spring.rewrite, spring.optional_bump, spring.loop,
restructure.apply, restructure.loop, verify.build.

* Round 9c routing: `case_type == spring_boot` with `layout.boot_as_is == "in-place"` (default) plans
  `restructure.boot_pom` (StepKind.MSA_POM, params {"mode": "boot_pom", "boot_modules": profile.boot_modules})
  instead of `restructure.apply` — directories kept, poms only; `boot_as_is == "layout"` and every non-Boot case
  keep `restructure.apply` (mode layout: `{name}-boot` / `{name}-back` / `{name}-front`). MSA `restructure.msa_pom`
  params also carry `boot_modules`.

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
* Library advisories (`build_plan(..., advisories=findings)`, sbmig.advisory): for policy "recipe" findings the
  advisory recipes that are neither planned nor covered by a planned composite are PREPENDED to each
  spring.rewrite step (before the Boot umbrella); plan notes `advisory: <id> → <recipes> (added)` /
  `(covered by the plan)` / `advisory: <id> → guide (<reason>)`. MSA per-service steps only get the advisories
  whose library the service uses. Follow-ups are not planner notes (the handover writes them).
"""
from __future__ import annotations

import logging
import posixpath
import re
from typing import Any, Optional

from sbmig.core.config import Config
from sbmig.core.models import (CaseType, MigrationPlan, Phase, PlanStep, ProjectProfile, RecipeRef, ServiceInfo,
                               StepKind)

from .catalog import GATES, default_catalog, prefix_artifact
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


def artifact_for(recipe_id: str, cfg: Optional[Config] = None) -> Optional[str]:
    """Recipe jar for a builtin id: the recipe catalog first, then the id-prefix fallback."""
    return default_catalog(cfg).artifact_for(recipe_id)


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
    return RecipeRef(id=recipe_id, source="builtin", artifact=artifact_for(recipe_id, cfg))


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


def _short(recipe_id: str) -> str:
    return recipe_id.rsplit(".", 1)[-1]


def _advisory_refs(advisories: Optional[list[Any]], facts: _Facts, cfg: Config, recipes: list[RecipeRef],
                   notes: list[str], label: str, per_service: bool) -> list[RecipeRef]:
    """Advisory recipe refs for one spring.rewrite step (to prepend) + plan notes. Never raises."""
    if not advisories:
        return []
    findings = [f for f in advisories if f.policy == "recipe"]
    if per_service:
        from sbmig.analyze.libraries import LIBRARY_NAMES

        findings = [f for f in findings if f.library not in LIBRARY_NAMES or f.library in facts.libs]
    if not findings:
        return []
    try:
        from sbmig.advisory import advisory_recipe_refs

        refs = list(advisory_recipe_refs(findings, cfg, [r.id for r in recipes]) or [])
    except Exception as e:  # advisories must never break planning
        log.warning("advisory recipes unavailable for planning: %s", e)
        notes.append(f"advisory: recipe planning failed ({type(e).__name__}: {e}); advisories stay guide-only")
        return []
    added = {r.id for r in refs}
    where = f" [{label}]" if label else ""
    for f in findings:
        mine = [r for r in f.recipes if r in added]
        ids = ", ".join(_short(r) for r in f.recipes)
        if mine:
            notes.append(f"advisory: {f.advisory_id} → {', '.join(_short(r) for r in mine)} (added){where}")
        else:
            notes.append(f"advisory: {f.advisory_id} → {ids} (covered by the plan){where}")
    return refs


def _recipe_phase_steps(facts: _Facts, cfg: Config, kb_refs: list[RecipeRef], notes: list[str], *,
                        svc: Optional[str] = None, target: Optional[str] = None,
                        recipes: Optional[list[RecipeRef]] = None, title_case: Optional[str] = None,
                        advisories: Optional[list[Any]] = None) -> list[PlanStep]:
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
    adv = _advisory_refs(advisories, facts, cfg, recipes, notes, label, per_service=svc is not None)
    recipes = adv + [r for r in recipes if not any(x.id == r.id for x in adv)]
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


def _msa_steps(profile: ProjectProfile, cfg: Config, kb_refs: list[RecipeRef], notes: list[str],
               advisories: Optional[list[Any]] = None) -> list[PlanStep]:
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
                                     recipes=union, title_case="msa (all services)", advisories=advisories)
    else:
        for t in targets:
            steps += _recipe_phase_steps(facts_of[t.name], cfg, kb_refs, notes, svc=t.name, target=_service_dir(t),
                                         advisories=advisories)
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


def _single_steps(profile: ProjectProfile, cfg: Config, kb_refs: list[RecipeRef], notes: list[str],
                  advisories: Optional[list[Any]] = None) -> list[PlanStep]:
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
    steps += _recipe_phase_steps(libs_facts, cfg, kb_refs, notes, advisories=advisories)
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
        starter = cfg.layout.starter_by_case.get(libs_facts.case.value, "auto")
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
            reason=f"non-msa: company layout; boot module starter '{starter}' for {libs_facts.case.value}"))
        rule = ("rule boot-as-is=layout (Boot split like the other cases)" if case == CaseType.SPRING_BOOT
                else f"rule non-boot ({libs_facts.case.value})")
        notes.append(f"restructure: {rule} → layout split into {', '.join(m for m in mods if m)}, starter '{starter}'"
                     + (f", multi-module policy '{cfg.layout.multi_module_policy}'" if profile.is_multi_module else ""))
        if front_m and has_web:
            notes.append(f"restructure: web resources ({len(src.jsp_paths) or src.jsp_count} JSP(s), "
                         f"{src.web_static_count} static file(s) in {', '.join(src.web_static_dirs) or '-'}) → "
                         f"{front_m}"
                         + (f"; JSPs → {boot_m} packaged as {cfg.layout.jsp_boot_packaging}"
                            if (src.jsp_paths or src.jsp_count) else ""))
    if libs_facts.case == CaseType.SERVLET_TOMCAT and profile.source.has_web_xml:
        src_ = profile.source
        if cfg.layout.web_xml_registrations == "generate" and \
                (src_.web_xml_servlets or src_.web_xml_filters or src_.web_xml_listeners):
            # 2026-10-23: the restructure generates WebXmlRegistrations (sbmig.restructure.webxml_registrations)
            notes.append(f"restructure: web.xml ({src_.web_xml_spec or 'spec unknown'}: {src_.web_xml_servlets} "
                         f"servlet(s), {src_.web_xml_filters} filter(s), {src_.web_xml_listeners} listener(s)) → "
                         "generated WebXmlRegistrations in the boot module")
        else:
            notes.append("followup: web.xml wiring is not migrated automatically (runtime owners)")
    if libs_facts.case == CaseType.SPRING_LEGACY and profile.source.xml_bean_configs:
        notes.append(f"restructure: {len(profile.source.xml_bean_configs)} XML bean config(s) → @ImportResource")
    s = _loop_step("restructure", Phase.RESTRUCTURE, cfg, "gate: the restructured project must package",
                   step_id="restructure.loop")
    if s:
        steps.append(s)
    steps.append(_verify())
    return steps


def _apply_parent_phase(steps: list[PlanStep], cfg: Config, notes: list[str]) -> list[PlanStep]:
    """`plan.parent_phase: spring` (round 9c): the restructure step(s) (`restructure.apply` / `boot_pom` / `msa_pom`)
    move into the spring phase right before the first spring build-fix loop, so that loop fixes against the company
    parent chain; the restructure-phase loop goes (the final verify stays). Default `restructure` = unchanged."""
    if cfg.plan.parent_phase != "spring":
        return steps
    restr = [s for s in steps if s.kind in (StepKind.RESTRUCTURE, StepKind.MSA_POM) and s.phase == Phase.RESTRUCTURE
             and str(s.params.get("mode") or "") != "optional_bump"]
    if not restr:
        return steps
    drop = set(id(s) for s in restr) | {id(s) for s in steps
                                        if s.kind == StepKind.BUILD_FIX_LOOP and s.phase == Phase.RESTRUCTURE}
    out = [s for s in steps if id(s) not in drop]
    idx = next((i for i, s in enumerate(out) if s.kind == StepKind.BUILD_FIX_LOOP and s.phase == Phase.SPRING), None)
    if idx is None:
        spring_idx = [i for i, s in enumerate(out) if s.phase == Phase.SPRING]
        if not spring_idx:
            notes.append("plan.parent_phase=spring ignored: no spring-phase step (pom-only project)")
            return steps
        idx = spring_idx[-1] + 1
    moved = [s.model_copy(update={"phase": Phase.SPRING,
                                  "reason": (s.reason + "; plan.parent_phase=spring: applied before the spring "
                                             "build-fix loop so it fixes against the company parent chain")})
             for s in restr]
    out[idx:idx] = moved
    notes.append("restructure: parents / layout applied in the spring phase right after spring.rewrite, before the "
                 "spring build-fix loop (plan.parent_phase=spring); the restructure phase keeps only the final verify")
    return out


def build_plan(profile: ProjectProfile, cfg: Config, kb: Any = None,
               advisories: Optional[list[Any]] = None) -> MigrationPlan:
    """Rule-based plan. `advisories` = `sbmig.advisory.evaluate_advisories` findings (mutated: recipe_source)."""
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
    advisories = list(advisories or [])

    if case == CaseType.MSA and profile.services:
        steps += _msa_steps(profile, cfg, kb_refs, notes, advisories)
    else:
        steps += _single_steps(profile, cfg, kb_refs, notes, advisories)
    steps = _apply_parent_phase(steps, cfg, notes)
    for f in advisories:
        if f.policy != "recipe":
            notes.append(f"advisory: {f.advisory_id} → guide ({f.policy_reason or 'catalog'})")
        elif f.recipe_source == "none":       # no spring.rewrite step took it (pom-only, module outside services)
            spring = any(st.kind == StepKind.OPENREWRITE and st.phase == Phase.SPRING for st in steps)
            f.policy, f.recipes = "guide", []
            f.policy_reason = (f"no spring.rewrite step for {f.library} → guide" if spring
                               else "no spring.rewrite step → guide")
            notes.append(f"advisory: {f.advisory_id} → guide ({f.policy_reason})")

    pre = pre_handler_reasons(profile, cfg)
    if pre:
        notes.append("pre-emptive handlers: " + "; ".join(f"{h} ({why})" for h, why in pre))
    for w in profile.warnings:
        notes.append(f"warning: {w}")
    return MigrationPlan(profile_fingerprint=profile.fingerprint, case_type=case, steps=steps, notes=notes)


# The advisor whitelist / coverage / gates are derived from the recipe catalog (knowledge/recipes/catalog.yml,
# see sbmig/plan/catalog.py and docs/RECIPES.md). Module-level snapshots of the repo catalog, taken at import time;
# `advise_plan(cfg=...)` re-reads `cfg.recipe_catalog_file`.
_DEFAULT_CATALOG = default_catalog()

# Recipes the advisor may ADD (ids verified against docs.openrewrite.org / our recipe sets; never removes anything).
ADVISOR_CATALOG: dict[str, str] = _DEFAULT_CATALOG.advisor_catalog()

# What the planned composites already include (so the advisor is told, and duplicates are filtered deterministically).
ADVISOR_COVERAGE: dict[str, list[str]] = _DEFAULT_CATALOG.coverage()

# Profile facts a suggestion must be backed by — a small model sometimes suggests recipes "just in case".
ADVISOR_GATES: dict[str, Any] = _DEFAULT_CATALOG.gates()

_has_javax_ee = GATES["has_javax_ee"]          # backwards-compatible alias


def _covered_by(planned: set[str], coverage: Optional[dict[str, list[str]]] = None) -> dict[str, str]:
    """recipe id → the planned composite that already includes it."""
    out: dict[str, str] = {}
    for comp, inner in (ADVISOR_COVERAGE if coverage is None else coverage).items():
        if comp in planned:
            for rid in inner:
                out.setdefault(rid, comp)
    return out


def _artifact_for(recipe_id: str, catalog_obj: Any = None) -> Optional[str]:
    cat = catalog_obj if catalog_obj is not None else _DEFAULT_CATALOG
    return cat.artifact_for(recipe_id) if recipe_id in cat else prefix_artifact(recipe_id)


def _advisor_prompt(plan: MigrationPlan, profile: ProjectProfile, catalog: dict[str, str],
                    coverage: Optional[dict[str, list[str]]] = None) -> str:
    libs = ", ".join(f"{l.name}{'@' + l.version if l.version else ''}" for l in profile.libraries) or "-"
    javax = ", ".join(f"{k}:{v}" for k, v in sorted(profile.source.javax_imports.items())) or "-"
    steps = "\n".join(f"- {st.id} [{st.phase.value}]: " + (", ".join(r.id for r in st.recipes) or "(no recipes)")
                      for st in plan.steps if st.recipes or st.kind.value == "openrewrite")
    cat = "\n".join(f"- {rid}: {desc}" for rid, desc in catalog.items())
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
        "RULE-BASED PLAN (OpenRewrite steps)\n" + steps + "\n\n"
        "CATALOG OF RECIPES YOU MAY ADD\n" + cat + "\n\n"
        "ALREADY COVERED BY PLANNED COMPOSITE RECIPES (do not suggest these)\n" + cov_text + "\n\n"
        "Task: suggest ONLY additional recipes from the catalog that the profile clearly warrants and the plan lacks "
        "(e.g. junit4 present but no JUnit4to5Migration). Do not suggest recipes already planned, do not remove anything, "
        "do not invent ids. If nothing is missing, return an empty list. Also list short concerns the migration team "
        "should know (max 5).\n"
        'Answer with JSON only: {"add": [{"recipe": "<catalog id>", "phase": "spring" | "jdk21", "reason": "<why>"}], '
        '"concerns": ["<text>"]}'
    )


def _parse_advice(text: str) -> tuple[list[dict], list[str], Optional[str]]:
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
    """Optional LLM review of the rule-based plan (`cfg.plan.advisor == "llm"`, model `cfg.llm.recipe_model`).

    The advisor is additive only: it may add catalog recipes to the OpenRewrite steps and record concerns in
    `plan.notes`; unknown ids, already-planned ids and malformed answers are ignored (noted). Never raises."""
    if llm is None or (cfg is not None and cfg.plan.advisor == "none"):
        return plan
    cat_obj = default_catalog(cfg) if cfg is not None else _DEFAULT_CATALOG
    if cfg is not None:
        coverage, gates = cat_obj.coverage(), cat_obj.gates()
        catalog = catalog or cat_obj.advisor_catalog() or ADVISOR_CATALOG
    else:
        coverage, gates = ADVISOR_COVERAGE, ADVISOR_GATES
        catalog = catalog or ADVISOR_CATALOG
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
    for a in adds:
        rid = str(a.get("recipe", "")).strip()
        phase = str(a.get("phase", "spring")).strip().lower()
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
        step_prefix = "jdk21.rewrite" if phase == "jdk21" else "spring.rewrite"
        targets = [st for st in plan.steps if st.id == step_prefix or st.id.startswith(step_prefix + "@")]
        if not targets:
            plan.notes.append(f"advisor: no {step_prefix} step to attach {rid}; ignored")
            continue
        for st in targets:
            st.recipes.append(RecipeRef(id=rid, source="builtin", artifact=_artifact_for(rid, cat_obj)))
        planned.add(rid)
        plan.notes.append(f"advisor: +{rid} → {step_prefix} ({reason or 'no reason given'})")
    for c in concerns:
        plan.notes.append(f"advisor concern: {c}")
    return plan
