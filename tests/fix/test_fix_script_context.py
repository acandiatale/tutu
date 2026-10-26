"""v2 fix scripts: ContextPacker.pack_script — one case (L2) spread over many files within llm.max_context_chars."""
import json
import re

from sbmig.core.interfaces import FixContext
from sbmig.core.models import (
    GAV,
    BuildError,
    DecisionKind,
    ErrorCluster,
    ErrorKind,
    FixAttempt,
    Phase,
    ScriptRunResult,
    TriageDecision,
)
from sbmig.fix import ContextPacker, FakeLLMClient, LLMFixer, make_llm_client
from sbmig.fix.client import NO_FIX_RESPONSE, ReplayResponder, load_replay_entries
from sbmig.fix.context import SCRIPT_MAX_ERROR_LINES, dedupe_errors, summarize_script_attempt

SIG = "compile|cannot_find_symbol|method|clientIp"


def big_java(name: str, n: int = 300, imports: int = 6) -> str:
    imps = "".join(f"import com.example.lib{i}.Thing{i};\n" for i in range(imports))
    body = "".join(f"    int f{i} = LegacyRequestHelper.clientIp(req{i});\n" for i in range(n))
    return f"package com.example.web;\n\n{imps}\npublic class {name} {{\n{body}}}\n"


def errs_for(path: str, lines, sig: str = SIG) -> list[BuildError]:
    return [BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=path, line=ln, column=40,
                       symbol="method clientIp(HttpServletRequest)", location="class com.example.web.X",
                       signature=f"{path}:{ln}", signature_generic=sig) for ln in lines]


def case(errors: list[BuildError], kind=ErrorKind.COMPILE, case_id="k1") -> ErrorCluster:
    return ErrorCluster(id=f"case:{case_id}", kind=kind, errors=errors, case_id=case_id)


def sctx(**kw) -> FixContext:
    base = dict(phase=Phase.VERIFY, iteration=2, script_mode="new", case_id="k1", signature_generic=SIG)
    base.update(kw)
    return FixContext(**base)


def text_of(packed) -> str:
    return packed.render()


def test_many_files_small_budget_spread_and_listed(cfg, ws, put):
    files = [f"app/src/main/java/com/example/web/C{i:02d}.java" for i in range(30)]
    errors: list[BuildError] = []
    for i, f in enumerate(files):
        put(f, big_java(f"C{i:02d}"))
        errors += errs_for(f, [20 + i, 22 + i, 200])                    # two close lines → one window, one far away
    packer = ContextPacker(cfg)
    packed = packer.pack_script(ws, case(errors), sctx(), "case=spring_boot java=17", budget=20000)
    body = text_of(packed)
    assert packed.chars <= 20000 + 200
    shown, listed = packed.files_included, packed.files_listed
    assert shown and listed and not set(shown) & set(listed)
    assert set(shown) | set(listed) == set(files)
    index = dict(packed.sections)["Files with errors (30)"]
    for f in files:                                                    # every file is named
        assert f"- {f} — 3 error(s), line(s) " in index
    for f in listed:
        assert f"- {f} — 3 error(s)" in index and f"{f} — 3 error(s), line(s) {20 + files.index(f)}, " in index
        assert f"// {f} lines" not in body                              # listed by name only
    assert "the script must reach them through `p.errors` / `p.files(...)`" in index
    src = dict(packed.sections)["Source (exact text around the error lines; line numbers as in p.errors)"]
    for f in shown:
        assert f"// {f} lines 1-" in src                                # the import block (package line first) …
        assert "import com.example.lib5.Thing5;" in src
    first = shown[0]
    i = files.index(first)
    assert f"line(s) {20 + i}, {22 + i}, 200" in index
    assert src.count(f"// {first} lines") >= 2                          # … and windows around the error lines
    errs = dict(packed.sections)["Errors of this case (90)"]
    assert len(errs) <= 20000 * 0.3 + 200 and errs.count("\n- ") >= 20
    assert re.search(r"… \d+ more error\(s\) — the script gets all of them in p.errors", errs)
    tiny = packer.pack_script(ws, case(errors), sctx(), budget=6000)          # index capped, never dropped
    assert "Files with errors (30)" in dict(tiny.sections)
    assert re.search(r"… \d+ more file\(s\), not shown \(all of them in p.errors\)", tiny.render())


def test_small_files_whole_and_surplus_goes_to_big_ones(cfg, ws, put):
    small = "app/src/main/java/com/example/web/Small.java"
    big = "app/src/main/java/com/example/web/Big.java"
    put(small, "package com.example.web;\n\npublic class Small {\n    int x = LegacyRequestHelper.clientIp(r);\n}\n")
    put(big, big_java("Big", n=900))
    errors = errs_for(small, [4]) + errs_for(big, [100, 700])
    packed = ContextPacker(cfg).pack_script(ws, case(errors), sctx(), budget=30000)
    src = text_of(packed)
    assert f"// {small} lines 1-5 (whole file)" in src
    assert f"// {big} lines 1-8 (of 911)" in src                         # import block
    assert f"// {big} lines 40-160 (of 911)" in src and f"// {big} lines 640-760 (of 911)" in src   # widest windows
    assert packed.files_listed == []


def test_large_budget_shows_every_file(cfg, ws, put):
    files = [f"m/src/main/java/a/F{i}.java" for i in range(5)]
    errors = []
    for i, f in enumerate(files):
        put(f, f"package a;\n\nimport javax.annotation.Resource;\n\npublic class F{i} {{\n}}\n")
        errors += errs_for(f, [3])
    packed = ContextPacker(cfg).pack_script(ws, case(errors), sctx(), budget=60000)
    assert sorted(packed.files_included) == sorted(files) and packed.files_listed == []


def test_errors_deduped_capped_and_missing_files(cfg, ws, put):
    f = "app/src/main/java/a/A.java"
    put(f, big_java("A", n=200))
    errors = errs_for(f, range(10, 10 + SCRIPT_MAX_ERROR_LINES + 5))
    errors += errs_for(f, [10, 11])                                      # reactor duplicates
    errors += errs_for("app/src/main/java/a/Gone.java", [7])
    packed = ContextPacker(cfg).pack_script(ws, case(errors), sctx(), budget=40000)
    secs = dict(packed.sections)
    title = f"Errors of this case ({SCRIPT_MAX_ERROR_LINES + 6})"
    assert title in secs and secs[title].count("\n- ") == SCRIPT_MAX_ERROR_LINES - 1
    assert "… 6 more error(s)" in secs[title]
    index = secs["Files with errors (2)"]
    assert "- app/src/main/java/a/Gone.java — 1 error(s), line(s) 7 (file not found in the project)" in index
    assert len(dedupe_errors(errors)) == SCRIPT_MAX_ERROR_LINES + 6


def test_pom_case_gets_the_module_poms(cfg, ws, put):
    put("pom.xml", "<project>\n  <artifactId>root</artifactId>\n  <modules><module>demo-back</module></modules>\n</project>\n")
    put("demo-back/pom.xml", "<project>\n  <artifactId>demo-back</artifactId>\n</project>\n")
    put("demo-boot/pom.xml", "<project>\n  <artifactId>demo-boot</artifactId>\n</project>\n")
    err = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not resolve dependencies for project",
                     module="demo-back", artifact=GAV(group_id="javax.servlet", artifact_id="jstl", version="1.2"),
                     signature="dep", signature_generic="dependency_resolution|javax.servlet:jstl")
    packed = ContextPacker(cfg).pack_script(ws, case([err], kind=ErrorKind.DEPENDENCY_RESOLUTION), sctx(), budget=30000)
    titles = [t for t, _ in packed.sections]
    assert "POM (demo-back/pom.xml)" in titles and "POM (pom.xml)" in titles and "POM (demo-boot/pom.xml)" not in titles
    errs = dict(packed.sections)["Errors of this case (1)"]
    assert "- (no file): Could not resolve dependencies for project | artifact: javax.servlet:jstl:1.2 | module: demo-back" \
        in errs
    # a plain compile case without a pom-ish message gets no pom
    put("demo-back/src/main/java/a/A.java", "package a;\n\npublic class A {\n    int x = y();\n}\n")
    plain = errs_for("demo-back/src/main/java/a/A.java", [4])
    packed = ContextPacker(cfg).pack_script(ws, case(plain), sctx(), budget=30000)
    assert not any(t.startswith("POM") for t, _ in packed.sections)


def test_fileless_module_error_gets_its_pom_by_artifact_id(cfg, ws, put):
    """A file-less error names the module by its artifactId (`ledger-utils-comp`), the folder is `module/comp-jar`
    (pre-9c layout) or `{name}-back` (company layout): its pom is found by the pom's own artifactId (not the parent's).
    A compile failure without a source file (javac options: `--boot-class-path` with `--release`) is a compiler
    configuration problem, so it gets the module pom too (2026-10-26 e2e: java6-legacy, enforcer-java8-werror)."""
    put("pom.xml", "<project>\n  <artifactId>ledger-utils</artifactId>\n  <modules><module>module/comp-jar</module>"
                   "</modules>\n</project>\n")
    put("module/comp-jar/pom.xml", "<project>\n  <parent>\n    <artifactId>ledger-utils</artifactId>\n  </parent>\n"
                                   "  <artifactId>ledger-utils-comp</artifactId>\n  <build><plugins><plugin>"
                                   "<artifactId>maven-compiler-plugin</artifactId></plugin></plugins></build>\n"
                                   "</project>\n")
    put("module/boot-jar/pom.xml", "<project>\n  <parent>\n    <artifactId>ledger-utils</artifactId>\n  </parent>\n"
                                   "  <artifactId>ledger-utils-boot</artifactId>\n</project>\n")
    err = BuildError(kind=ErrorKind.COMPILE, message="Compilation failure: Compilation failure:",
                     module="ledger-utils-comp", raw="[ERROR] error: option --boot-class-path cannot be used together "
                     "with --release", signature="compile|build|x", signature_generic="compile|build|x")
    packed = ContextPacker(cfg).pack_script(ws, case([err]), sctx(), budget=30000)
    titles = [t for t, _ in packed.sections]
    assert "POM (module/comp-jar/pom.xml)" in titles and "POM (module/boot-jar/pom.xml)" not in titles
    assert "maven-compiler-plugin" in dict(packed.sections)["POM (module/comp-jar/pom.xml)"]
    # an enforcer failure (a pom kind) of the same module: the module pom, then the root
    enf = err.model_copy(update={"kind": ErrorKind.ENFORCER, "message": "RequireJavaVersion: must be built with JDK 8"})
    titles = [t for t, _ in ContextPacker(cfg).pack_script(ws, case([enf], kind=ErrorKind.ENFORCER), sctx(),
                                                           budget=30000).sections]
    assert titles.index("POM (module/comp-jar/pom.xml)") < titles.index("POM (pom.xml)")


def test_deterministic_and_hints(cfg, ws, put, tmp_path):
    files = [f"app/src/main/java/a/D{i}.java" for i in range(8)]
    errors = []
    for i, f in enumerate(files):
        put(f, big_java(f"D{i}", n=150))
        errors += errs_for(f, [12, 90])
    attempt = FixAttempt(id="r:verify-1-1", phase=Phase.VERIFY, iteration=1, cluster_id="case:k1",
                         decision=TriageDecision(cluster_id="case:k1", kind=DecisionKind.SCRIPT), outcome="no_change",
                         script_mode="new", script_version=1, errors_before=16, errors_after=16,
                         script_result=ScriptRunResult(status="error", error="KeyError: 'file'"))
    ctx = sctx(advisory_hints=["legacy-web-lib 3: clientIp → remoteClientIp"], kb_hints=["seen in 2 projects"],
               plan_notes=["boot module: app-boot"], other_errors_summary="case:k2 compile 3 error(s)",
               reference_files={"asis-merge/A.java": "class A {}"}, previous_attempts=[attempt])
    packer = ContextPacker(cfg)
    a = packer.pack_script(ws, case(errors), ctx, budget=20000)
    b = packer.pack_script(ws, case(list(reversed(errors))), ctx, budget=20000)
    assert a.sections == b.sections and a.files_included == b.files_included
    body = text_of(a)
    assert "Library migration guidance (what to migrate TO)" in body and "remoteClientIp" in body
    assert "Reference files (read-only, NOT in the build — the script must never write these paths)" in body
    assert "script new v1 → outcome: no_change, errors 16→16" in body and "run: error — KeyError: 'file'" in body
    assert "seen in 2 projects" in body and "boot module: app-boot" in body and "case:k2 compile" in body
    assert summarize_script_attempt(attempt).startswith("- attempt r:verify-1-1 (iteration 1): script new v1")


def test_fixer_uses_pack_script_budget(cfg, ws, put):
    files = [f"app/src/main/java/a/E{i}.java" for i in range(20)]
    errors = []
    for i, f in enumerate(files):
        put(f, big_java(f"E{i}", n=400))
        errors += errs_for(f, [50, 300])
    cfg.llm.max_context_chars = 12000
    client = FakeLLMClient(["<analysis>a</analysis><script>\ndef fix(p):\n    pass\n</script>"])
    LLMFixer(cfg, client).propose_script(ws, case(errors), sctx())
    user = client.calls[0]["messages"][1].content
    assert len(user) < 12000 + 1500 and "(not shown)" in user


# ---------------------------------------------------------------------------
# replay of recorded answers (FakeLLMClient)
# ---------------------------------------------------------------------------
SCRIPT_ANSWER = "<analysis>rename</analysis>\n<script>\ndef fix(p):\n    p.log('x')\n</script>\n"


def write_replay(d, items):
    d.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(items, 1):
        (d / f"llm-{i:03d}.json").write_text(json.dumps(item), encoding="utf-8")


def test_positional_replay_unchanged(cfg, tmp_path):
    d = tmp_path / "replay"
    write_replay(d, [{"note": "1", "response": "A"}, {"response": "B"}, {"error": "x", "response": None}])
    cfg.llm.provider, cfg.llm.base_url, cfg.llm.record_dir = "fake", f"replay:{d}", None
    client = make_llm_client(cfg)
    assert isinstance(client, FakeLLMClient) and client.responses == ["A", "B"]


def test_replay_by_match_and_role(cfg, tmp_path):
    d = tmp_path / "replay"
    write_replay(d, [{"replay_role": "structure_review", "response": '{"verdict": "ok", "issues": []}'},
                     {"match": "case: k2", "response": "K2 v1"},
                     {"match": "case: k2", "response": "K2 v2"},
                     {"match": "case: k1", "replay_role": "fix", "response": SCRIPT_ANSWER}])
    cfg.llm.provider, cfg.llm.base_url, cfg.llm.record_dir = "fake", f"replay:{d}", None
    client = make_llm_client(cfg)
    assert isinstance(client.responses, ReplayResponder)

    def ask(text, role="fix"):
        from sbmig.core.interfaces import ChatMessage
        return client.complete([ChatMessage(role="system", content="case: k1 case: k2"),
                                ChatMessage(role="user", content=text)], role=role).content

    assert ask("review the poms", role="structure_review").startswith('{"verdict"')
    assert ask("case: k2 …") == "K2 v1" and ask("case: k2 …") == "K2 v2" and ask("case: k2 …") == "K2 v2"
    assert ask("case: k1 …", role="fix_escalation") == SCRIPT_ANSWER        # "fix" also answers escalation calls
    assert ask("case: k9 …") == NO_FIX_RESPONSE                            # the system prompt is never matched
    assert client.responses.served[:3] == ["llm-001.json", "llm-002.json", "llm-003.json"]
    assert [e.name for e in load_replay_entries(d)] == ["llm-001.json", "llm-002.json", "llm-003.json", "llm-004.json"]


def test_script_answer_replays_through_propose_script(cfg, ws, put, tmp_path):
    d = tmp_path / "replay"
    write_replay(d, [{"note": "case k1, new script", "match": "case: k1", "response": SCRIPT_ANSWER}])
    cfg.llm.provider, cfg.llm.base_url, cfg.llm.record_dir = "fake", f"replay:{d}", None
    f = "app/src/main/java/a/A.java"
    put(f, "package a;\n\npublic class A {\n    int x = y();\n}\n")
    p = LLMFixer(cfg, make_llm_client(cfg)).propose_script(ws, case(errs_for(f, [4])), sctx())
    assert p.code == "def fix(p):\n    p.log('x')\n" and p.analysis == "rename" and p.error == ""
