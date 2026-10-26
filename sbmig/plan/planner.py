"""Rule-based migration planner (no LLM), v2 (flow-v2): ProjectProfile + Config → MigrationPlan.

Step ids (non-MSA): transform.rewrite, restructure.apply | restructure.boot_pom | restructure.pom_only,
restructure.review, verify.build. No build runs before verify.build; its fix loop (`loop.phases.verify`) fixes every
error with LLM scripts.

* `transform.rewrite` (StepKind.OPENREWRITE, Phase.TRANSFORM): ONE OpenRewrite run on the as-is classpath. Recipes,
  in this order: `openrewrite.recipes.jdk21`; advisory recipes (only with `advisories.allow_recipes`); `jakarta`
  (spring_legacy and servlet_tomcat always, pure_java only with Java EE javax imports, never spring_boot — the Boot
  umbrella migrates jakarta itself); `spring_framework_7` / `spring_security_7` (spring_legacy; security only with
  spring-security) — both empty by default; `spring_boot_4` (spring_boot); `junit5` (junit4 detected) — empty by
  default; `extra.<case>` + `extra.all`. Never: project recipes (`com.company.migrate.*`), library recipes
  (`knowledge/recipes/library_recipes.yml`), KB recipes / candidates (business decision, PLAN-v2 §2).
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
* v1 settings that v2 ignores (each noted when set): `plan.intermediate_builds`, `loop.phases.{jdk21,spring,
  restructure}.enabled`, `plan.parent_phase: spring`, `plan.optional_bump_phase: spring`. Pre-emptive handlers are
  off (sbmig/plan/pre_handlers.py stays until PLAN-v2 step 4).
* Library advisories (`build_plan(..., advisories=findings)`, sbmig.advisory): findings are guide-only unless
  `advisories.allow_recipes` is set; then the advisory recipes that are neither planned nor covered by a planned
  composite run in the transform step right after the jdk21 set (v1 order: before the Boot umbrella). Plan notes
  `advisory: <id> → <recipes> (added)` / `(covered by the plan)` / `advisory: <id> → guide (<reason>)`. MSA
  per-service steps only get the advisories whose library the service uses. Follow-ups are written by the handover.
"""
from __future__ import annotations

import logging
import posixpath
import re
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
    ("jdk21", "Java 21"), ("advisory", "advisory recipes"), ("jakarta", "jakarta"),
    ("framework", "Spring Framework 7"), ("security", "Spring Security 7"), ("boot", "Spring Boot 4.1"),
    ("junit5", "JUnit 5"), ("extra", "extra recipes"),
)


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
    """A recipe id → RecipeRef (`com.company.*` ids = project recipes of `openrewrite.base_recipe_dir`; v2 plans
    them only when a company config lists them in `openrewrite.recipes.extra`)."""
    if recipe_id.startswith("com.company."):
        return RecipeRef(id=recipe_id, source="project", file=_project_recipe_file(recipe_id, cfg))
    return RecipeRef(id=recipe_id, source="builtin", artifact=artifact_for(recipe_id, cfg))


def ee_javax_packages(profile_source_javax: dict[str, int], has_javax_annotation: bool) -> list[str]:
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
    """The few profile facts the recipe rules need; for MSA computed per service."""

    def __init__(self, case: CaseType, libs: set[str], javax: dict[str, int]):
        self.case = case
        self.libs = libs
        self.javax = javax


def _case_buckets(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> dict[str, list[str]]:
    """Recipe ids per bucket for one transform run (the advisory bucket is filled by the caller)."""
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
    seen: list[str] = []
    for key, _ in _BUCKETS:
        for i in buckets.get(key, []):
            if i not in seen:
                seen.append(i)
    return seen


def _union(parts: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k, _ in _BUCKETS}
    for b in parts:
        for k, ids in b.items():
            out[k] += [i for i in ids if i not in out[k]]
    return out


def transform_recipes(facts: _Facts, cfg: Config, notes: list[str], label: str = "") -> list[RecipeRef]:
    """The case's recipes of one transform run (jdk21 set first, no advisory recipes), as RecipeRefs."""
    return [recipe_ref(i, cfg) for i in _flatten(_case_buckets(facts, cfg, notes, label))]


spring_recipes = transform_recipes          # v1 name (sbmig.plan exports both)


# ---------------------------------------------------------------------------
# step factories
# ---------------------------------------------------------------------------
def _sfx(svc: Optional[str]) -> str:
    return f"@{svc}" if svc else ""


def _advisory_refs(advisories: Optional[list[Any]], facts: _Facts, cfg: Config, planned: list[str],
                   notes: list[str], label: str, per_service: bool) -> list[RecipeRef]:
    """Advisory recipe refs for one transform step + plan notes. Never raises."""
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

        refs = list(advisory_recipe_refs(findings, cfg, planned) or [])
    except Exception as e:  # advisories must never break planning
        log.warning("advisory recipes unavailable for planning: %s", e)
        notes.append(f"advisory: recipe planning failed ({type(e).__name__}: {e}); advisories stay guide-only")
        return []
    added = {r.id for r in refs}
    where = f" [{label}]" if label else ""
    for f in findings:
        mine = [r for r in f.recipes if r in added]
        if mine:
            notes.append(f"advisory: {f.advisory_id} → {_shorts(mine)} (added){where}")
        else:
            notes.append(f"advisory: {f.advisory_id} → {_shorts(f.recipes)} (covered by the plan){where}")
    return refs


def _transform_step(facts: _Facts, cfg: Config, notes: list[str], *, svc: Optional[str] = None,
                    target: Optional[str] = None, buckets: Optional[dict[str, list[str]]] = None,
                    title_case: Optional[str] = None, advisories: Optional[list[Any]] = None) -> Optional[PlanStep]:
    """`transform.rewrite[@svc]` — every recipe of the case in one run, or None when no recipe applies.

    `buckets` overrides the per-case recipes (MSA single root run: union of the targets' buckets)."""
    label = svc or ""
    where = f" [{label}]" if label else ""
    if buckets is None:
        buckets = _case_buckets(facts, cfg, notes, label)
    adv = _advisory_refs(advisories, facts, cfg, _flatten(buckets), notes, label, per_service=svc is not None)
    adv_by_id = {r.id: r for r in adv}
    buckets = dict(buckets, advisory=[r.id for r in adv])
    ids = _flatten(buckets)
    if not ids:
        notes.append(f"transform{where}: no recipes apply → {TRANSFORM}{_sfx(svc)} omitted")
        return None
    recipes = [adv_by_id.get(i) or recipe_ref(i, cfg) for i in ids]
    parts = [lbl for key, lbl in _BUCKETS if buckets.get(key)]
    notes.append(f"transform{where}: one OpenRewrite run ({_shorts(ids)}) — no build until the restructure is "
                 "done (v2)")
    return PlanStep(
        id=TRANSFORM + _sfx(svc), kind=StepKind.OPENREWRITE, phase=Phase.TRANSFORM,
        title="OpenRewrite in one run: " + " + ".join(parts) + (f" ({svc})" if svc else ""),
        recipes=recipes, target=target, params={"service": svc} if svc else {},
        reason=f"v2 case {title_case or facts.case.value}: Java 21 / jakarta / Spring Boot recipes in one run on the "
               "as-is classpath (type attribution), no build before the restructure (business decision)")


def _review_step(restructure_step: str, cfg: Config, notes: list[str]) -> Optional[PlanStep]:
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


def _msa_steps(profile: ProjectProfile, cfg: Config, notes: list[str],
               advisories: Optional[list[Any]] = None) -> list[PlanStep]:
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
                             title_case="msa (all services)", advisories=advisories)
        steps += [st] if st else []
    else:
        for t in targets:
            st = _transform_step(facts_of[t.name], cfg, notes, svc=t.name, target=_service_dir(t),
                                 advisories=advisories)
            steps += [st] if st else []
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


def _single_steps(profile: ProjectProfile, cfg: Config, notes: list[str],
                  advisories: Optional[list[Any]] = None) -> list[PlanStep]:
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
    st = _transform_step(facts, cfg, notes, advisories=advisories)
    steps += [st] if st else []
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


def _ignored_v1_settings(cfg: Config, kb: Any, notes: list[str]) -> None:
    """Notes for the v1 knobs a config may still set; v2 plans the same flow whatever they say."""
    if cfg.plan.intermediate_builds:
        notes.append("plan.intermediate_builds=true ignored: v2 has no build before the restructure")
    on = [k for k in V1_LOOP_PHASES if (pc := cfg.loop.phases.get(k)) is not None and pc.enabled]
    if on:
        notes.append(f"v2: no intermediate build-fix loops (loop.phases.{{{','.join(on)}}}.enabled=true ignored)")
    vc = cfg.loop.phases.get("verify")
    if vc is not None and not vc.enabled:
        notes.append("loop.phases.verify.enabled=false → the final build is not followed by the script fix loop")
    if cfg.plan.parent_phase == "spring":
        notes.append("plan.parent_phase=spring ignored: v2 restructures right after the transform")
    if cfg.plan.optional_bump_phase == "spring":
        notes.append("plan.optional_bump_phase=spring ignored: v2 bumps the optional artifacts inside the restructure")
    if cfg.artifacts.optional or cfg.artifacts.remove_when_present:
        notes.append("optional artifacts: bumped inside the restructure step (no separate bump step in v2)")
    else:
        notes.append("optional artifacts: none configured")
    if kb is not None or cfg.kb.use_active_recipes or cfg.kb.try_candidates_in_plan:
        flags = [k for k in ("use_active_recipes", "try_candidates_in_plan") if getattr(cfg.kb, k, False)]
        notes.append("kb: KB recipes are not used in v2"
                     + (f" (kb.{', kb.'.join(flags)}=true ignored)" if flags else ""))


def build_plan(profile: ProjectProfile, cfg: Config, kb: Any = None,
               advisories: Optional[list[Any]] = None) -> MigrationPlan:
    """Rule-based v2 plan. `kb` is accepted for the seam but not used (no KB recipes in v2). `advisories` =
    `sbmig.advisory.evaluate_advisories` findings (mutated: recipe_source, and policy → guide when no transform step
    took a recipe finding)."""
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
    _ignored_v1_settings(cfg, kb, notes)
    advisories = list(advisories or [])

    if case == CaseType.MSA and profile.services:
        steps += _msa_steps(profile, cfg, notes, advisories)
    else:
        steps += _single_steps(profile, cfg, notes, advisories)
    transform = any(st.kind == StepKind.OPENREWRITE and st.phase == Phase.TRANSFORM for st in steps)
    for f in advisories:
        if f.policy != "recipe":
            notes.append(f"advisory: {f.advisory_id} → guide ({f.policy_reason or 'catalog'})")
        elif f.recipe_source == "none":       # no transform step took it (pom-only, module outside services)
            f.policy, f.recipes = "guide", []
            f.policy_reason = (f"no {TRANSFORM} step for {f.library} → guide" if transform
                               else f"no {TRANSFORM} step → guide")
            notes.append(f"advisory: {f.advisory_id} → guide ({f.policy_reason})")
    notes.append("pre-emptive handlers: off in v2 (errors are fixed by LLM scripts)")
    for w in profile.warnings:
        notes.append(f"warning: {w}")
    return MigrationPlan(profile_fingerprint=profile.fingerprint, case_type=case, steps=steps, notes=notes)


# ---------------------------------------------------------------------------
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
    """Every recipe id v2 may run: the configured sets (jdk21, jakarta, spring_boot_4 and the opt-in framework /
    security / junit5 sets) plus every `extra` list."""
    rs = cfg.openrewrite.recipes if cfg is not None else RecipeSetConfig()
    ids = {*rs.jdk21, *rs.jakarta, *rs.spring_boot_4, *rs.spring_framework_7, *rs.spring_security_7, *rs.junit5}
    for extra in (rs.extra or {}).values():
        ids.update(extra)
    return ids


def advisor_whitelist(cfg: Optional[Config] = None) -> dict[str, str]:
    """The advisor-eligible catalog recipes inside the v2 scope (catalog order)."""
    cat = default_catalog(cfg) if cfg is not None else _DEFAULT_CATALOG
    scope = v2_recipe_scope(cfg)
    return {rid: desc for rid, desc in (cat.advisor_catalog() or ADVISOR_CATALOG).items() if rid in scope}


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
