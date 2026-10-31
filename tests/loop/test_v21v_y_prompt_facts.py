"""v2.1v / v2.1y, fix-loop side (docs/INTERFACES.md "v2.1u … v2.1y — follow-through of the prompt review"):

* B-H1 / E-H1 — a library the new parent chain / the target Boot BOM manages gets ONE fact line ("the API changed, fix
  the code and leave the version"), never the repository listing nor a `<no_script>` verdict; the listing stays for
  javax cases (an error names `javax.`, or the library is in the javax table);
* B-M3 / E-H2 — the as-is / new-chain facts also cover the libraries located on the classpath, with `(major X → Y)`;
* B-M4 — `package <p> does not exist` → the managed artifactIds of the longest groupId prefixing `<p>`;
* B-H2 — `FixContext.layout_facts` from the orchestrator's RestructureResults;
* E-L9 — `other_errors_summary` cut at a line boundary, `introduced by script:<case>:v<N>` kb hints;
* E-M1 — declined answers → BLOCKED needs_owner after `script.max_declines_per_case`, same-cause siblings blocked
  without an LLM call;
* E-H3 — the idempotency check (`script.idempotency_check`): a second change marks the stored version
  `non_idempotent`, only the second run's changes are undone."""
from __future__ import annotations

from sbmig.build.classpath import ClassLocation
from sbmig.core.interfaces import RestructureResult
from sbmig.core.models import (
    GAV,
    BuildError,
    ErrorCluster,
    ErrorKind,
    Phase,
    RepositoryVersions,
    ScriptProposal,
    VersionCandidate,
)
from sbmig.loop import BuildFixLoop, LoopBudget
from sbmig.loop.fixloop import API_CHANGED_TAIL, VersionResolver, cut_lines
from sbmig.loop.layout_facts import LAYOUT_FACTS_CHARS, layout_fact_lines, spec_gav
from sbmig.triage import Triager

from .fakes import (
    FakeBuildRunner,
    FakeEditApplier,
    FakeFixer,
    FakeKB,
    FakeRestructurer,
    FakeScriptRunner,
    answer,
    bad_build,
    case_of,
    compile_error,
    journal_events,
    make_profile,
    ok_build,
    script,
    v2_plan,
    write_script,
)

FOO = "src/main/java/com/x/Foo.java"
BAR = "src/main/java/com/x/Bar.java"
WEBMVC = "org.springframework:spring-webmvc"
BATCH = "org.springframework.batch:spring-batch-core"
PMC = "org.springframework.web.servlet.config.annotation.PathMatchConfigurer"


def make_loop(cfg, ws, fixer, *, build=None, kb=None, runner=None, locator=None, **kw) -> BuildFixLoop:
    return BuildFixLoop(cfg, ws, FakeBuildRunner(ws, fn=build or (lambda w, call: ok_build())), Triager(cfg), fixer,
                        kb if kb is not None else FakeKB(), make_profile(), budget=LoopBudget.from_config(cfg),
                        script_runner=runner or FakeScriptRunner(), edit_applier=FakeEditApplier(), locator=locator,
                        **kw)


class Locator:
    """sbmig.build.classpath stand-in: every error's owner class is `fq`, located at `where`."""

    def __init__(self, fq: str, where: ClassLocation | None):
        self.fq, self.where, self.asked = fq, where, []

    def owner_fqcn(self, e):
        return self.fq

    def type_mismatch(self, e):
        return None, None

    def locate_class(self, ws, cfg, fqcn, module=None):
        self.asked.append(fqcn)
        return self.where if fqcn == self.fq else None


class Lookup:
    """RepositoryLookup stand-in: `rv` for every key; records the keys asked."""

    def __init__(self, rv: RepositoryVersions | None = None):
        self.rv, self.asked = rv, []

    def candidates(self, key, current):
        self.asked.append(key)
        return (self.rv or RepositoryVersions(key=key, current=current)).model_copy(update={"key": key,
                                                                                           "current": current})

    def versions(self, g, a):
        return []


def removal_error(msg: str = "cannot find symbol", raw: str = "") -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message=msg, symbol="method setUseSuffixPatternMatch(boolean)",
                      location=f"variable configurer of type {PMC}", file="src/main/java/com/x/MvcConfig.java",
                      module="demo", raw=raw, signature="compile|setUseSuffixPatternMatch",
                      signature_generic="compile|cannot_find_symbol|setUseSuffixPatternMatch")


def resolver(asis: dict | None = None, after: dict | None = None, boot: dict | None = None) -> VersionResolver:
    return VersionResolver(asis or {}, after or {}, boot=boot or {})


# ------------------------------------------------------------------------------- B-H1 / E-H1: no false verdicts
def test_a_managed_library_gets_one_fact_line_and_no_repository_listing(cfg, ws, monkeypatch):
    """e2e 2026-10-30: `spring-webmvc → verdict: no jakarta build … answer <no_script>` 22 times, with 7.0.9 / 7.0.10
    listed next to the managed 7.0.8 — now the managed-version line only (and the as-is fact)."""
    lookup = Lookup(RepositoryVersions(key=WEBMVC, current="7.0.8", major=7,
                                       same_major=[VersionCandidate(version="7.0.9"),
                                                   VersionCandidate(version="7.0.10")]))
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", lambda cfg, fetch=None: lookup)
    webmvc = GAV(group_id="org.springframework", artifact_id="spring-webmvc", version="7.0.8")
    loop = make_loop(cfg, ws, FakeFixer(), locator=Locator(PMC, ClassLocation(artifact=webmvc, jar="/r/w.jar")))
    loop._version_resolver = resolver({"managed": {WEBMVC: "5.3.31"}, "parent": "com.legacy:parent:1"},
                                      {"managed": {WEBMVC: "7.0.8"}})
    c = ErrorCluster(id="case:x", kind=ErrorKind.COMPILE, module="demo", errors=[removal_error()])
    hints = loop._library_hints(c)
    assert hints == [f"as-is build: {WEBMVC} 5.3.31 (managed by com.legacy:parent:1)",
                     f"new parent chain: manages {WEBMVC} at 7.0.8 (major 5 → 7){API_CHANGED_TAIL}"]
    assert lookup.asked == [] and not any("verdict" in h or "7.0.10" in h for h in hints)
    # the target Boot BOM manages it (the chain does not): the same single line, BOM wording
    loop2 = make_loop(cfg, ws, FakeFixer(), locator=Locator(PMC, ClassLocation(artifact=webmvc, jar="/r/w.jar")))
    loop2._version_resolver = resolver(boot={WEBMVC: "7.0.8"})
    assert loop2._library_hints(c) == [f"target Boot BOM: manages {WEBMVC} at 7.0.8{API_CHANGED_TAIL}"]
    ev = [e for e in journal_events(ws) if e["event"] == "library_hints"]
    assert ev and ev[0]["keys"] == [WEBMVC] and ev[0]["listed"] == []


def test_the_repository_listing_stays_for_javax_cases_only(cfg, ws, monkeypatch):
    lookup = Lookup(RepositoryVersions(key="x", current="2.4.2", major=2,
                                       same_major=[VersionCandidate(version="2.6.0", jakarta=True)]))
    monkeypatch.setattr("sbmig.build.repo_versions.RepositoryLookup", lambda cfg, fetch=None: lookup)
    lib = GAV(group_id="com.acme", artifact_id="legacy-web", version="2.4.2")
    loc = Locator(PMC, ClassLocation(artifact=lib, jar="/r/l.jar"))
    loop = make_loop(cfg, ws, FakeFixer(), locator=loc)
    loop._version_resolver = resolver()
    plain = ErrorCluster(id="case:p", kind=ErrorKind.COMPILE, module="demo", errors=[removal_error()])
    assert loop._library_hints(plain) == [] and lookup.asked == []             # not javax, not in the table
    javax = ErrorCluster(id="case:j", kind=ErrorKind.COMPILE, module="demo", errors=[removal_error(
        raw="cannot access javax.servlet.Filter\n  class file for javax.servlet.Filter not found")])
    hints = loop._library_hints(javax)
    assert lookup.asked == ["com.acme:legacy-web"]
    assert hints[-1] == ("com.acme:legacy-web → verdict: raise to 2.6.0 (same major 2.x, jakarta build) — no major "
                         "change, no <verify> needed")


# --------------------------------------------------------------------------- B-M3 / E-H2: facts for located libs
def test_version_facts_cover_the_located_libraries_with_the_major_change(cfg, ws):
    loop = make_loop(cfg, ws, FakeFixer())
    loop._version_resolver = resolver({"by_module": {"com.x:demo": {"managed": {BATCH: "4.3.10"},
                                                                    "parent": "com.legacy:parent:1"}}},
                                      {"managed": {BATCH: "6.0.4"}})
    c = ErrorCluster(id="case:b", kind=ErrorKind.COMPILE, module="demo",
                     errors=[compile_error(msg="cannot find symbol", l2="compile|cannot_find_symbol|JobBuilderFactory")])
    libs = [GAV(group_id="org.springframework.batch", artifact_id="spring-batch-core", version="6.0.4")]
    assert loop._version_facts(c, libs) == [f"as-is build: {BATCH} 4.3.10 (managed by com.legacy:parent:1)",
                                            f"new parent chain: manages {BATCH} at 6.0.4 (major 4 → 6)"
                                            f"{API_CHANGED_TAIL}"]
    assert loop._version_facts(c) == []                                       # no artifact, nothing located
    # duplicates (an e.artifact and the same located key) are one entry; the e.artifact one carries no tail
    dep = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact", module="demo",
                     artifact=GAV(group_id="org.springframework.batch", artifact_id="spring-batch-core"),
                     signature_generic=f"dependency_resolution|{BATCH}")
    c2 = ErrorCluster(id="case:d", kind=ErrorKind.DEPENDENCY_RESOLUTION, errors=[dep])
    assert loop._version_facts(c2, libs)[1] == f"new parent chain: manages {BATCH} at 6.0.4 (major 4 → 6)"


# ---------------------------------------------------------------------------------------- B-M4: package facts
def test_package_facts_name_the_managed_artifacts_of_the_longest_group(cfg, ws):
    loop = make_loop(cfg, ws, FakeFixer())
    loop._version_resolver = resolver(after={"managed": {BATCH: "6.0.4",
                                                         "org.springframework.batch:spring-batch-infrastructure": "6.0.4",
                                                         "org.springframework:spring-core": "7.0.8"}},
                                      boot={"org.springframework.security:spring-security-access": "7.0.0"})

    def missing(pkg: str) -> ErrorCluster:
        return ErrorCluster(id=f"case:{pkg}", kind=ErrorKind.COMPILE, errors=[compile_error(
            msg=f"package {pkg} does not exist", l2=f"compile|package_does_not_exist|{pkg}")])

    assert loop._package_facts(missing("org.springframework.batch.item.support")) == [
        "managed artifacts under org.springframework.batch (add the one that holds "
        "org.springframework.batch.item.support, version-less): spring-batch-core, spring-batch-infrastructure"]
    assert loop._package_facts(missing("org.springframework.security.access.vote")) == [
        "managed artifacts under org.springframework.security (add the one that holds "
        "org.springframework.security.access.vote, version-less): spring-security-access"]
    assert loop._package_facts(missing("javax.servlet.http")) == []            # the javax facts own those
    assert loop._package_facts(missing("com.acme.util")) == []                 # no managed group matches
    # a long group is capped at 25 artifactIds, sorted
    loop._version_resolver = resolver(boot={f"org.big:a{i:02d}": "1" for i in range(30)})
    [line] = loop._package_facts(missing("org.big.pkg"))
    assert line.endswith("a23, a24, … (+5)") and line.split("): ", 1)[1].count(", ") == 25
    # the facts reach the prompt section, after the version facts
    c = missing("org.big.pkg")
    assert loop._library_hints(c) == [line]


# ------------------------------------------------------------------------------------ B-H2: the layout facts
def layout_result(**kw) -> RestructureResult:
    base = dict(ok=True, boot_module="ops-boot", comp_module="ops-back", front_module="ops-front",
                boot_packaging="war", starter="spring-boot-starter-webmvc",
                generated_files=["ops-boot/pom.xml", "ops-boot/src/main/java/com/example/ops/Application.java",
                                 "ops-boot/src/main/resources/legacy-config/applicationContext.xml",
                                 "ops-boot/src/main/java/com/example/ops/WebXmlRegistrations.java"],
                xml_imports=["classpath:legacy-config/applicationContext.xml"],
                xml_skipped=["src/test/resources/test-context.xml (test)"],
                legacy_properties_written=["spring.main.allow-bean-definition-overriding=true (as-is Framework)"],
                scope_restored=["jakarta.servlet:jakarta.servlet-api: compile → provided (as-is scope, ops-back/pom.xml)"],
                parents_set=["pom.xml: - → co:base-parent:4.0.0", "ops-boot/pom.xml: - → co:boot-jar:4.0.0",
                             "ops-back/pom.xml: - → co:comp-jar:4.0.0"],
                bom_imports=["ops-boot/pom.xml: org.springframework.boot:spring-boot-dependencies ${spring-boot.version} "
                             "(property of co:base-parent:4.0.0)"],
                conversion_facts={"enable_web_mvc": "com.example.ops.MvcConfig"})
    base.update(kw)
    return RestructureResult(**base)


def test_layout_facts_of_a_converted_project(cfg):
    lines = layout_fact_lines([layout_result()], cfg, modes=["layout"], case="spring_legacy",
                              read_artifact_id=lambda rel: rel.split("/")[0])
    L = cfg.layout
    assert lines[0] == "converted by sbmig: spring_legacy → Spring Boot application (company layout)"
    assert lines[1] == ("boot module ops-boot: artifactId ops-boot, packaging war (executable, java -jar), parent "
                        "co:boot-jar:4.0.0, starter spring-boot-starter-webmvc")
    assert lines[2] == "back module ops-back: every source incl. controllers, parent co:comp-jar:4.0.0"
    assert lines[3] == (f"front module ops-front: static under ops-front/{L.front_static_dir}, JSPs under "
                        f"ops-front/{L.front_jsp_dir}")
    assert lines[4] == ("generated: com.example.ops.Application, ops-boot/src/main/resources/legacy-config/ (1 XML "
                        "imported via @ImportResource: applicationContext.xml), com.example.ops.WebXmlRegistrations")
    assert "XML not imported: src/test/resources/test-context.xml (test)" in lines
    assert "legacy properties written: spring.main.allow-bean-definition-overriding=true (as-is Framework)" in lines
    assert any(x.startswith("scopes restored: jakarta.servlet:jakarta.servlet-api") for x in lines)
    company = next(x for x in lines if x.startswith("company artifacts: "))
    assert company.startswith(f"company artifacts: root parent {spec_gav(cfg.artifacts.parent)}")
    assert company.endswith("optional in-house artifacts only where already used; versions managed by the parent "
                            "chain — never write a literal for them")
    for spec in cfg.artifacts.mandatory:
        assert spec.key in company
    assert any(x.startswith("BOM imported: ops-boot/pom.xml: org.springframework.boot:spring-boot-dependencies")
               for x in lines)
    assert "parents set: pom.xml: - → co:base-parent:4.0.0" in lines          # the module poms: in their own lines
    assert lines[-1] == "- enable_web_mvc: com.example.ops.MvcConfig"
    assert sum(len(x) + 1 for x in lines) <= LAYOUT_FACTS_CHARS


def test_layout_facts_of_pom_only_modes_and_no_restructure(cfg):
    lines = layout_fact_lines([layout_result()], cfg, modes=["boot_pom"], case="spring_boot")
    assert [x.split(":")[0] for x in lines] == ["company artifacts", "BOM imported", "parents set"]
    assert layout_fact_lines([], cfg) == [] and layout_fact_lines([RestructureResult(ok=False)], cfg) == []
    # no mode given: a result without a generated Application class counts as pom-only
    assert [x.split(":")[0] for x in layout_fact_lines([RestructureResult(ok=True, parents_set=["pom.xml: - → a:b:1"])],
                                                       cfg)] == ["company artifacts", "parents set"]
    # a Boot project split by layout.boot_as_is: layout is "restructured", not "converted"
    assert layout_fact_lines([layout_result()], cfg, modes=["layout"], case="spring_boot")[0] == \
        "restructured by sbmig: spring_boot → company layout (boot / back / front)"
    # long lists stay within the section's budget
    big = layout_result(xml_skipped=[f"src/main/resources/x{i}.xml (not in contextConfigLocation)" for i in range(50)],
                        parents_set=[f"m{i}/pom.xml: - → co:comp-jar:4.0.0" for i in range(40)],
                        conversion_facts={f"k{i}": "v" * 300 for i in range(10)})
    out = layout_fact_lines([big], cfg, modes=["layout"], case="spring_legacy")
    assert sum(len(x) + 1 for x in out) <= LAYOUT_FACTS_CHARS and out[0].startswith("converted by sbmig")
    assert any(x.startswith("XML not imported: ") and "(+46 more)" in x for x in out)


def test_the_loop_hands_the_layout_facts_to_the_fixer(cfg, ws):
    fixer = FakeFixer(scripts=[write_script({FOO: "class Foo { ok }\n", BAR: "class Bar { ok }\n"})])

    def build(w, call):
        errs = [compile_error(f) for f in (FOO, BAR) if "broken" in w.read(f)]
        return bad_build(*errs) if errs else ok_build()
    loop = make_loop(cfg, ws, fixer, build=build)
    assert loop._layout_facts() == []                                          # no restructure: nothing
    loop.restructure_results, loop.restructure_modes = [layout_result()], ["layout"]
    assert loop.run(Phase.VERIFY, "package", 3).ok
    facts = fixer.contexts[0].layout_facts
    assert facts[0] == "restructured by sbmig: spring_boot → company layout (boot / back / front)"   # profile: Boot
    assert [e for e in journal_events(ws) if e["event"] == "layout_facts"][0]["lines"] == facts


def test_the_orchestrator_hands_its_restructure_results_to_the_loop(cfg, ws):
    from sbmig.loop import Orchestrator

    from .fakes import FakeReviewer, FakeRewriteRunner
    orch = Orchestrator(cfg, ws, analyzer=lambda w, c: make_profile(), planner=lambda p, c, k: v2_plan(),
                        restructurer=FakeRestructurer(), rewrite_runner=FakeRewriteRunner(ws),
                        build_runner=FakeBuildRunner(ws, fn=lambda w, call: ok_build()), triager=Triager(cfg),
                        fixer=FakeFixer(), kb=FakeKB(), llm=object(), script_runner=FakeScriptRunner(),
                        reviewer=FakeReviewer())
    report = orch.run()
    assert report.success
    assert [r.generated_files for r in orch.loop.restructure_results] == [["module/boot-jar/pom.xml"]]
    assert orch.loop.restructure_modes == ["layout"]


# ------------------------------------------------------------------------- E-L9: other errors, introduced by
def test_other_errors_are_cut_at_a_line_boundary():
    text = "\n".join(f"case {i}: " + "x" * 40 for i in range(200))
    out = cut_lines(text, 4000)
    assert len(out) <= 4000 + 40 and out.endswith("more lines)") and "\ncase 200" not in out
    kept = out.split("\n")[:-1]
    assert all(line.startswith("case ") and line.endswith("x") for line in kept)          # no line cut mid-way
    assert out.split("\n")[-1] == f"(… {200 - len(kept)} more lines)"
    assert cut_lines("short", 4000) == "short" and cut_lines("y" * 50, 10) == "y" * 9 + "…"


def test_a_case_introduced_by_a_kept_script_names_it(cfg, ws):
    """TokenCodec (e2e 2026-10-30): script 26271fddc9 fixed its case but left `catch (IOException e)` → the follow-on
    case's prompt now says which script introduced it."""
    l2a, l2b = "compile|cannot_find_symbol|BASE64Decoder", "compile|exception_never_thrown|IOException"

    def build(w, call):
        text = w.read(FOO)
        if "broken" in text:
            return bad_build(compile_error(FOO, "cannot find symbol", l2=l2a))
        if "half" in text:
            return bad_build(compile_error(FOO, "exception java.io.IOException is never thrown", l2=l2b))
        return ok_build()
    fixer = FakeFixer(scripts=[write_script({FOO: "class Foo { half }\n"}),
                               write_script({FOO: "class Foo { ok }\n"})])
    kb = FakeKB()
    pr = make_loop(cfg, ws, fixer, build=build, kb=kb).run(Phase.VERIFY, "package", 4)
    assert pr.ok and len(fixer.contexts) == 2
    first = pr.attempts[0]
    assert f"introduced by script:{case_of(l2a)}:v{first.script_version} (attempt {first.id}) — prefer augmenting " \
           "that script" in fixer.contexts[1].kb_hints
    assert not any(h.startswith("introduced by") for h in fixer.contexts[0].kb_hints)


# ---------------------------------------------------------------------------------- E-M1: declined answers
DAO = "src/main/java/com/x/CatalogDao.java"
IBATIS = ("compile|package_does_not_exist|org.springframework.orm.ibatis.support",
          "compile|cannot_find_symbol|SqlMapClientDaoSupport", "compile|cannot_find_symbol|getSqlMapClientTemplate")


def ibatis_build(w, call):
    errs = [compile_error(DAO, f"error {i}", sig=f"compile|ibatis{i}", l2=l2, line=i + 1) for i, l2 in enumerate(IBATIS)]
    if "broken" in w.read(BAR):
        errs.append(compile_error(BAR, "cannot find symbol", l2="compile|cannot_find_symbol|other"))
    return bad_build(*errs)


def test_legacy_ibatis_one_decline_blocks_the_case_and_its_same_file_siblings(cfg, ws):
    """legacy-ibatis (e2e): 3 cases of CatalogDao.java × 3 analysis-only answers = 9 LLM calls → 1; the other file's
    case is still asked."""
    ws.write(DAO, "class CatalogDao extends SqlMapClientDaoSupport {}\n")
    ws.commit("dao")
    decline = ScriptProposal(code="", analysis="CatalogDao extends SqlMapClientDaoSupport; Spring 4 removed the iBATIS "
                                                "support. A MyBatis port needs the owning team's decisions.",
                             error="no <script> block", model="m")
    calls: list[str] = []

    def fx(w, c, ctx):
        calls.append(c.errors[0].file)
        if c.errors[0].file == DAO:
            return decline
        return write_script({BAR: "class Bar { ok }\n"})
    loop = make_loop(cfg, ws, FakeFixer(script_fn=fx), build=ibatis_build)
    pr = loop.run(Phase.VERIFY, "package", 5)
    assert calls.count(DAO) == 1 and calls.count(BAR) == 1
    owner = [b for b in pr.blocked if b.reason.value == "needs_owner"]
    assert len(owner) == 3
    root = case_of(IBATIS[0])
    sentence = "CatalogDao extends SqlMapClientDaoSupport; Spring 4 removed the iBATIS support."
    assert any(b.suggestion == f"{sentence} — 소유팀 결정 필요" for b in owner)
    assert sum(1 for b in owner if b.suggestion == f"same cause as {root}: {sentence} — 소유팀 결정 필요") == 2
    events = journal_events(ws)
    assert [e["case"] for e in events if e["event"] == "declined"] == [root]
    assert sorted(e["case"] for e in events if e["event"] == "same_cause_blocked") == sorted(
        case_of(x) for x in IBATIS[1:])
    [dao_try] = [a for a in pr.attempts if a.cluster_errors[0].file == DAO]
    assert dao_try.outcome == "blocked" and dao_try.notes[0].startswith("declined: CatalogDao extends")


def test_an_explicit_blocked_answer_and_more_declines_allowed(cfg, ws):
    """`<blocked>` (ScriptProposal.blocked_reason, error "blocked: …") is a decline too; with
    script.max_declines_per_case 2 the case is asked twice — the second time as a new fix, never an augment."""
    cfg.script.max_declines_per_case = 2
    ans = ScriptProposal(code="", analysis="no jakarta build.", blocked_reason="no jakarta build of com.acme:legacy "
                         "exists; the owners must replace it", error="blocked: no jakarta build", model="m")
    fixer = FakeFixer(script_fn=lambda w, c, ctx: ans)

    def build(w, call):
        return bad_build(compile_error(FOO, "cannot access javax.servlet.Filter"))
    pr = make_loop(cfg, ws, fixer, build=build).run(Phase.VERIFY, "package", 5)
    assert len(fixer.contexts) == 2 and [ctx.script_mode for ctx in fixer.contexts] == ["new", "new"]
    [b] = pr.blocked
    assert b.reason.value == "needs_owner"
    assert b.suggestion == "no jakarta build of com.acme:legacy exists; the owners must replace it — 소유팀 결정 필요"
    assert [e["explicit"] for e in journal_events(ws) if e["event"] == "declined"] == [True, True]


def test_a_sibling_naming_the_same_artifact_is_blocked_without_a_call(cfg, ws):
    loop = make_loop(cfg, ws, FakeFixer())
    art = GAV(group_id="com.acme", artifact_id="legacy-api", version="1.0")
    a = ErrorCluster(id="case:a", case_id="a", kind=ErrorKind.DEPENDENCY_RESOLUTION,
                     errors=[BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="x", artifact=art)])
    b = ErrorCluster(id="case:b", case_id="b", kind=ErrorKind.COMPILE,
                     errors=[BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="y", artifact=art)])
    other = ErrorCluster(id="case:c", case_id="c", kind=ErrorKind.COMPILE, errors=[compile_error(BAR)])
    loop._owner_blocked.append(("a", frozenset(), frozenset({art.key}), "the owners decide"))
    assert loop._owner_cause(b) == ("a", "the owners decide")
    assert loop._owner_cause(a) is None and loop._owner_cause(other) is None
    # files: only when every error file of the sibling lies inside the declined case's files
    loop._owner_blocked.append(("d", frozenset({FOO}), frozenset(), "s"))
    assert loop._owner_cause(ErrorCluster(id="case:e", kind=ErrorKind.COMPILE, errors=[compile_error(FOO)])) == \
        ("d", "s")
    assert loop._owner_cause(ErrorCluster(id="case:f", kind=ErrorKind.COMPILE,
                                          errors=[compile_error(FOO), compile_error(BAR)])) is None


def test_declines_off_restores_the_old_failed_try(cfg, ws):
    cfg.script.max_declines_per_case = 0
    cfg.script.max_augments_per_case = 2
    fixer = FakeFixer(script_fn=lambda w, c, ctx: ScriptProposal(code="", analysis="I cannot.",
                                                                 error="no <script> block", model="m"))
    pr = make_loop(cfg, ws, fixer, build=lambda w, call: bad_build(compile_error(FOO))).run(Phase.VERIFY, "package", 3)
    assert {a.outcome for a in pr.attempts} == {"apply_failed"} and len(fixer.contexts) == 2
    assert [b.reason.value for b in pr.blocked] == ["repeated_failure"]


# ---------------------------------------------------------------------------------------- E-H3: idempotency
def foo_build(w, call):
    return bad_build(compile_error(FOO)) if "broken" in w.read(FOO) else ok_build()


def test_a_non_idempotent_script_is_marked_and_only_its_second_change_is_undone(cfg, ws):
    kb = FakeKB()
    # the 1b37194675 shape: the rewrite runs again on its own output (`new JobBuilder(.., jobRepository, jobRepository)`)
    code = script("t = p.read('src/main/java/com/x/Foo.java')\n"
                  "p.write('src/main/java/com/x/Foo.java', t.replace('broken', 'ok') + '// repo\\n')\n"
                  "if p.exists('a.txt'):\n"
                  "    p.write('b.txt', 'second run\\n')\n"
                  "p.write('a.txt', 'first run\\n')")
    runner = FakeScriptRunner()
    pr = make_loop(cfg, ws, FakeFixer(scripts=[code]), build=foo_build, kb=kb, runner=runner).run(
        Phase.VERIFY, "package", 3)
    assert pr.ok and len(runner.runs) == 2
    [a] = pr.attempts
    assert a.outcome == "build_ok"                                 # scored by the rebuild as usual
    assert ws.read(FOO).count("// repo") == 1 and not ws.exists("b.txt") and ws.read("a.txt") == "first run\n"
    assert not ws.is_dirty()
    case = a.script_case_id
    assert [(c, v) for c, v, _n in kb.cases.marked] == [(case, 1)]
    assert kb.cases.find(case).scripts[0].non_idempotent and kb.cases.best(case) is None
    assert any(n.startswith("non-idempotent: a second run of script v1 changed") for n in a.notes)
    [ev] = [e for e in journal_events(ws) if e["event"] == "script_non_idempotent"]
    assert ev["case"] == case and ev["version"] == 1 and sorted(ev["files"]) == ["b.txt", FOO]   # a.txt: same bytes
    assert ev["marked"] is True


def test_an_idempotent_script_runs_twice_and_is_not_marked_and_the_check_can_be_off(cfg, ws, tmp_path):
    kb = FakeKB()
    runner = FakeScriptRunner()
    pr = make_loop(cfg, ws, FakeFixer(scripts=[write_script({FOO: "class Foo { ok }\n"})]), build=foo_build, kb=kb,
                   runner=runner).run(Phase.VERIFY, "package", 3)
    assert pr.ok and len(runner.runs) == 2 and kb.cases.marked == []
    assert not [e for e in journal_events(ws) if e["event"] == "script_non_idempotent"]
    # off: one run
    from sbmig.core.workspace import Workspace

    from .fakes import make_project
    ws2 = Workspace.from_input(make_project(tmp_path / "two"), tmp_path / "out2", run_id="t-run")
    ws2.git_init()
    cfg.script.idempotency_check = False
    runner2 = FakeScriptRunner()
    assert make_loop(cfg, ws2, FakeFixer(scripts=[write_script({FOO: "class Foo { ok }\n"})]), build=foo_build,
                     runner=runner2).run(Phase.VERIFY, "package", 3).ok
    assert len(runner2.runs) == 1


def test_a_stored_non_idempotent_version_is_not_reused(cfg, ws):
    kb = FakeKB()
    l2 = "compile|x"
    kb.cases.seed(case_of(l2), write_script({FOO: "class Foo { ok }\n"}).code)
    kb.cases.mark_non_idempotent(case_of(l2), 1)
    fixer = FakeFixer(scripts=[answer(code=write_script({FOO: "class Foo { fine }\n"}).code)])
    pr = make_loop(cfg, ws, fixer, build=foo_build, kb=kb).run(Phase.VERIFY, "package", 3)
    assert pr.ok and [a.source for a in pr.attempts] == ["llm-new"] and len(fixer.contexts) == 1


def test_a_declined_missing_artifact_keeps_the_classifiers_reason_and_artifact(cfg, ws):
    """missing-artifact (e2e): the analysis-only answer is a decline → BLOCKED after one call (was two), and the
    block keeps the triage classifier's reason `artifact_not_found` with its composed suggestion and the artifact
    (PM 2026-10-31: operators / the handbook / attn codes key on it); the LLM's sentence is in the rationale."""
    ghost = GAV(group_id="com.company.nonexistent", artifact_id="ghost-lib", version="1.2.3")
    err = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact "
                     "com.company.nonexistent:ghost-lib:jar:1.2.3", module="demo", artifact=ghost,
                     signature="dependency_resolution|ghost", signature_generic=f"dependency_resolution|{ghost.key}")
    fixer = FakeFixer(script_fn=lambda w, c, ctx: ScriptProposal(
        code="", analysis="Maven cannot resolve com.company.nonexistent:ghost-lib:1.2.3 from any repository.",
        error="no <script> block", model="m"))
    pr = make_loop(cfg, ws, fixer, build=lambda w, call: bad_build(err)).run(Phase.VERIFY, "package", 3)
    [b] = pr.blocked
    assert len(fixer.contexts) == 1 and b.reason.value == "artifact_not_found" and b.artifact.key == ghost.key
    assert "소유팀" not in b.suggestion and "is not in any repository" in b.suggestion
    assert any("the LLM declined: Maven cannot resolve" in n for n in b.notes)
    assert [e["reason"] for e in journal_events(ws) if e["event"] == "blocked"] == ["artifact_not_found"]


def test_a_partly_classified_declined_case_blocks_the_rest_as_needs_owner(cfg, ws, monkeypatch):
    """The classifier covers only some errors of the declined case: those keep its reason, the rest → needs_owner."""
    from sbmig.core.models import BlockReason
    from sbmig.triage import blocked as tb
    errs = [compile_error(FOO, "cannot access javax.servlet.Filter", sig="compile|a"),
            compile_error(FOO, "cannot find symbol", sig="compile|b")]
    monkeypatch.setattr(tb, "classify_blocked", lambda c, p, cfg=None, ws=None: tb.BlockedCandidate(
        reason=BlockReason.EXTERNAL_JAVAX_API, errors=[e for e in c.errors if "javax" in e.message],
        required_type="javax.servlet.Filter") if any("javax" in e.message for e in c.errors) else None)
    fixer = FakeFixer(script_fn=lambda w, c, ctx: ScriptProposal(code="", analysis="The owners must decide.",
                                                                 error="no <script> block", model="m"))
    pr = make_loop(cfg, ws, fixer, build=lambda w, call: bad_build(*errs)).run(Phase.VERIFY, "package", 3)
    assert len(fixer.contexts) == 1
    assert sorted(b.reason.value for b in pr.blocked) == ["external_javax_api", "needs_owner"]
    owner = next(b for b in pr.blocked if b.reason.value == "needs_owner")
    assert owner.suggestion == "The owners must decide. — 소유팀 결정 필요"
