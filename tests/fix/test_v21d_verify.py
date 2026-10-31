"""v2.1d dependency version policy on the fixer side (docs/INTERFACES.md "v2.1d"): rule 14 of the script prompt, the
`<verify>` part (운영자 확인 사항) parsed into `ScriptProposal.verify`, the "Dependency versions" prompt section from
`FixContext.library_hints`, and `LLMFixer.verify_guide` (the `VerifyGuideLike` seam)."""
from __future__ import annotations

import json

import pytest

from sbmig.core.interfaces import FixContext, FixerLike, VerifyGuideLike
from sbmig.core.models import (
    GAV,
    BuildError,
    DependencyChange,
    ErrorCluster,
    ErrorKind,
    LLMUsage,
    Phase,
    ScriptBase,
)
from sbmig.fix import ContextPacker, FakeLLMClient, LLMFixer
from sbmig.fix.client import NO_FIX_RESPONSE, LLMError
from sbmig.fix.context import SCRIPT_TRUNCATE_ORDER, target_text
from sbmig.fix.fixer import (
    VERIFY_MAX_CHARS,
    VERIFY_MAX_DIFF_CHARS,
    VERIFY_ROLE,
    load_script_prompts,
    load_verify_prompt,
    parse_verify_answer,
)
from sbmig.fix.script_protocol import PROTOCOL_WARNING, parse_script_response

CODE = ('def fix(p):\n    for e in p.errors:\n        path = e.get("file")\n        if path and p.exists(path):\n'
        '            p.replace(path, "import javax.annotation.PostConstruct;", '
        '"import jakarta.annotation.PostConstruct;")\n')
EDITS = [{"op": "pom", "path": "demo-back/pom.xml", "desc": "demo-back/pom.xml 의 sitemesh 버전을 올립니다",
          "set_dependency_version": {"group_id": "org.sitemesh", "artifact_id": "sitemesh", "version": "3.2.1"}}]
GUIDE = ("운영자 확인: demo-back/src/main/resources/sitemesh3.xml 의 매핑이 decorators.xml 의 패턴과 같은 화면을 "
         "장식하는지 확인\n운영자 확인: web.xml 의 SiteMeshFilter 등록이 ConfigurableSiteMeshFilter 로 바뀌었는지 기동 로그 확인")
SIG = "compile|package_missing|com.opensymphony.module.sitemesh"
FILES = [f"demo-back/src/main/java/a/S{i}.java" for i in range(2)]


def answer(verify: str | None = GUIDE, edits=EDITS, script: str = CODE) -> str:
    out = "<analysis>원인: sitemesh 2 is a javax build</analysis>\n<solution>해결 방법: sitemesh 3.2.1</solution>\n"
    if verify is not None:
        out += f"<verify>{verify}</verify>\n"
    if edits is not None:
        out += f"<edits>{edits if isinstance(edits, str) else json.dumps(edits, ensure_ascii=False)}</edits>\n"
    if script:
        out += f"<script>\n{script}</script>\n"
    return out


def cluster(n: int = 2) -> ErrorCluster:
    errs = [BuildError(kind=ErrorKind.COMPILE, message="package com.opensymphony.module.sitemesh does not exist",
                       file=FILES[i % 2], line=3 + i, column=24, module="demo-back", signature=f"s{i}",
                       signature_generic=SIG) for i in range(n)]
    return ErrorCluster(id="case:abc123", kind=ErrorKind.COMPILE, errors=errs, case_id="abc123",
                        signatures=[e.signature for e in errs])


def ctx(**kw) -> FixContext:
    base = {"phase": Phase.VERIFY, "iteration": 1, "profile_summary": "case=spring_legacy java=1.8 jsp=4",
            "script_mode": "new", "case_id": "abc123", "signature_generic": SIG}
    base.update(kw)
    return FixContext(**base)


def major(**kw) -> DependencyChange:
    base = {"path": "demo-back/pom.xml", "key": "org.sitemesh:sitemesh", "before": "2.4.2", "after": "3.2.1",
            "kind": "major"}
    base.update(kw)
    return DependencyChange(**base)


@pytest.fixture()
def proj(ws, put):
    for i, f in enumerate(FILES):
        put(f, f"package a;\n\nimport com.opensymphony.module.sitemesh.Page;\n\nclass S{i} {{}}\n")
    put("demo-back/pom.xml", "<project><artifactId>demo-back</artifactId></project>\n")
    return ws


# ---------------------------------------------------------------------------
# parsing <verify>
# ---------------------------------------------------------------------------
def test_verify_is_parsed_and_trimmed():
    r = parse_script_response(answer())
    assert r.verify == GUIDE and r.solution == "해결 방법: sitemesh 3.2.1" and r.code == CODE and len(r.edits) == 1
    assert r.warnings == []


def test_verify_with_fence_and_crlf():
    fenced = answer(verify=f"\n```text\n{GUIDE}\n```\n").replace("\n", "\r\n")
    r = parse_script_response(fenced)
    assert r.verify == GUIDE and r.code == CODE and len(r.edits) == 1


def test_missing_verify_is_empty_and_unclosed_verify_ends_at_the_next_part():
    r = parse_script_response(answer(verify=None))
    assert r.verify == "" and r.code == CODE
    r = parse_script_response(answer().replace("</verify>", ""))
    assert r.verify == GUIDE and len(r.edits) == 1 and r.code == CODE


def test_a_script_tag_inside_verify_never_opens_the_script():
    text = answer(verify="운영자 확인: decorator.jsp 의 <script> 태그가 그대로 출력되는지 화면에서 확인", script="")
    r = parse_script_response(text + "<no_script>this project's decorators</no_script>")
    assert r.code == "" and r.candidate == "" and len(r.edits) == 1
    assert r.verify.startswith("운영자 확인: decorator.jsp 의 <script> 태그")
    r = parse_script_response(answer(verify="the <script> of decorator.jsp: check it"))
    assert r.code == CODE and r.verify == "the <script> of decorator.jsp: check it"


def test_verify_alone_is_not_an_answer():
    r = parse_script_response(f"<verify>{GUIDE}</verify>")
    assert r.verify == GUIDE and PROTOCOL_WARNING in r.warnings and not r.usable


# ---------------------------------------------------------------------------
# propose_script carries <verify>
# ---------------------------------------------------------------------------
def test_proposal_carries_verify(cfg, proj):
    client = FakeLLMClient([answer()])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 1 and p.verify == GUIDE and p.code == CODE and len(p.edits) == 1
    ev = [json.loads(ln) for ln in proj.journal_path.read_text().splitlines() if '"llm_script"' in ln][-1]
    assert ev["verify_chars"] == len(GUIDE)
    assert LLMFixer(cfg, FakeLLMClient([answer(verify=None)])).propose_script(proj, cluster(), ctx()).verify == ""


def test_verify_survives_the_feedback_round(cfg, proj):
    bad = answer(edits='[{"op": "pom" "path": 1}]')                        # unreadable edits → one feedback round
    client = FakeLLMClient([bad, answer(verify=None)])                     # the re-sent answer drops <verify>
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 2 and len(p.edits) == 1 and p.verify == GUIDE
    assert "<verify> after a major version upgrade" in client.calls[1]["messages"][-1].content
    client = FakeLLMClient([bad, answer(verify="운영자 확인: 새 가이드")])     # the latest one wins
    assert LLMFixer(cfg, client).propose_script(proj, cluster(), ctx()).verify == "운영자 확인: 새 가이드"


def test_verify_after_a_context_round(cfg, proj):
    ask = "<analysis>need the pom</analysis><need_files>demo-back/pom.xml</need_files>"
    client = FakeLLMClient([ask, answer()])
    p = LLMFixer(cfg, client).propose_script(proj, cluster(), ctx())
    assert len(client.calls) == 2 and p.verify == GUIDE and p.code == CODE


def test_no_fix_answer_has_no_verify(cfg, proj):
    p = LLMFixer(cfg, FakeLLMClient([f"<analysis>cannot</analysis><verify>{GUIDE}</verify>"])).propose_script(
        proj, cluster(), ctx())
    assert p.code == "" and p.edits == [] and p.verify == ""


# ---------------------------------------------------------------------------
# the "Dependency versions" section
# ---------------------------------------------------------------------------
HINTS = ["opensymphony:sitemesh 2.4.2: 같은 메이저 — 2.4.4 (javax), 2.5.0 (javax); 상위 — 3.2.1 (jakarta ✓)",
         "org.apache.tiles:tiles-core 3.0.8 → approved 3.0.8 (people's table: keep, jakarta build of tiles 3)"]


def pom_case(put) -> ErrorCluster:
    put("pom.xml", "<project>\n  <artifactId>root</artifactId>\n  <modules><module>demo-back</module></modules>\n"
                   "</project>\n")
    put("demo-back/pom.xml", "<project>\n  <artifactId>demo-back</artifactId>\n</project>\n")
    err = BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not resolve dependencies for project",
                     module="demo-back", artifact=GAV(group_id="opensymphony", artifact_id="sitemesh", version="2.4.2"),
                     signature="dep", signature_generic="dependency_resolution|opensymphony:sitemesh")
    return ErrorCluster(id="case:k1", kind=ErrorKind.DEPENDENCY_RESOLUTION, errors=[err], case_id="k1")


def test_dependency_versions_section_after_the_poms_before_the_advisories(cfg, ws, put):
    c = pom_case(put)
    packed = ContextPacker(cfg).pack_script(ws, c, ctx(case_id="k1", library_hints=HINTS,
                                                       advisory_hints=["sitemesh 2 → sitemesh 3"]), budget=30000)
    titles = [t for t, _ in packed.sections]
    lib = ContextPacker.LIBRARY_HINTS_TITLE
    assert lib == "Dependency versions (what the repository has; policy: same major first, see rule 14)"
    assert lib in titles
    assert max(titles.index("POM (pom.xml)"), titles.index("POM (demo-back/pom.xml)")) < titles.index(lib) \
        < titles.index(ContextPacker.ADVISORY_TITLE)
    assert dict(packed.sections)[lib] == "\n".join(f"- {h}" for h in HINTS)
    # no hints → no section
    assert lib not in [t for t, _ in ContextPacker(cfg).pack_script(ws, c, ctx(case_id="k1"), budget=30000).sections]


def test_dependency_versions_are_dropped_late(cfg, ws, put):
    order = list(SCRIPT_TRUNCATE_ORDER)
    assert order.index("library_hints") == order.index("reference_notes") + 1
    assert order.index("library_hints") < order.index("files")
    f = "demo-back/src/main/java/a/A.java"
    put(f, "package a;\n\nimport com.opensymphony.module.sitemesh.Page;\n\npublic class A {\n}\n")
    err = BuildError(kind=ErrorKind.COMPILE, message="package com.opensymphony.module.sitemesh does not exist",
                     file=f, line=3, column=40, signature="s", signature_generic=SIG)
    c = ErrorCluster(id="case:k1", kind=ErrorKind.COMPILE, errors=[err], case_id="k1")
    big = ctx(case_id="k1", library_hints=HINTS, kb_hints=["x" * 3000], other_errors_summary="y" * 3000,
              plan_notes=["z" * 2000], advisory_hints=["w" * 2500])
    packed = ContextPacker(cfg).pack_script(ws, c, big, budget=6000)
    secs = dict(packed.sections)
    assert packed.truncated and any(t.startswith(("kb_hints", "other_errors", "plan_notes", "advisory_hints"))
                                    for t in packed.truncated)
    assert not any(t.startswith("library_hints") for t in packed.truncated)
    assert secs[ContextPacker.LIBRARY_HINTS_TITLE] == "\n".join(f"- {h}" for h in HINTS)


def test_dependency_versions_reach_the_prompt(cfg, proj):
    client = FakeLLMClient([answer()])
    LLMFixer(cfg, client).propose_script(proj, cluster(), ctx(library_hints=HINTS))
    user = client.calls[0]["messages"][1].content
    assert "## Dependency versions (what the repository has; policy: same major first, see rule 14)" in user
    assert f"- {HINTS[0]}" in user


# ---------------------------------------------------------------------------
# prompt texts
# ---------------------------------------------------------------------------
def test_system_prompt_states_the_policy_and_the_verify_part():
    system, _ = load_script_prompts("v1")
    flat = " ".join(system.split())
    for part in ("14. Dependency versions", "MAJOR version", "never swap a library for another on your own",
                 "the newest jakarta release of the SAME major", 'The "Dependency versions" facts are authoritative',
                 "`verdict:` / `approved` lines decide a javax library's version", "only when that major has none",
                 "the newest release of a higher major that has one", "JSP tags", "MIGRATION-TODO",
                 "`<verify>` is REQUIRED whenever a major changed or a library was replaced",
                 "replace a library on your own (rule 14)", "Rules 3, 4, 8, 9, 10 and 14 apply to the edits",
                 "운영자 확인"):
        assert part in flat, part
    out = system[system.index("## Output format (mandatory)"):]
    assert out.index("<solution>해결 방법") < out.index("<verify>운영자 확인: xxx-back/src/main/resources/sitemesh3.xml "
                                                       "의 매핑이 decorators.xml 의 패턴과 같은 화면을 장식하는지 확인") \
        < out.index("<edits>\n[")
    # v2.1u (B-L2): the repeated <verify> paragraph is gone — rule 14 and the example line carry it
    assert "`<verify>` (운영자 확인 사항) is left out when no dependency's major version changed" not in out
    assert "{{" not in system and "{%" not in system


def test_user_prompt_closing_lists_verify(cfg, proj):
    client = FakeLLMClient([answer(), answer()])
    fixer = LLMFixer(cfg, client)
    fixer.propose_script(proj, cluster(), ctx())
    assert client.calls[0]["messages"][1].content.rstrip().endswith(
        '<analysis>, <solution>, <verify> (only after a major upgrade), <edits> (each with its "desc"), <changes>, '
        "then <script> defining `def fix(p)` (or <no_script> with the reason) — or <need_files> / <need_grep>.")
    base = ScriptBase(case_id="abc123", version=1, code="def fix(p):\n    p.log('x')\n", outcome="no_change",
                      status="ok")
    fixer.propose_script(proj, cluster(), ctx(script_mode="augment", script_base=base))
    assert "Answer now using only the output format: <analysis>, <solution>, <verify> (only after a major upgrade), " \
           "<changes>, then the COMPLETE new <script>" in client.calls[1]["messages"][1].content


# ---------------------------------------------------------------------------
# verify_guide (VerifyGuideLike)
# ---------------------------------------------------------------------------
def test_llm_fixer_is_a_verify_guide(cfg):
    fixer = LLMFixer(cfg, FakeLLMClient())
    assert isinstance(fixer, VerifyGuideLike) and isinstance(fixer, FixerLike)


def test_verify_guide_returns_the_answer_and_charges_usage(cfg, proj):
    client = FakeLLMClient([f"<verify>\n{GUIDE}\n</verify>"])
    fixer = LLMFixer(cfg, client)
    diff = "diff --git a/demo-back/pom.xml b/demo-back/pom.xml\n-<version>2.4.2</version>\n+<version>3.2.1</version>\n"
    out = fixer.verify_guide(proj, cluster(8), ctx(), [major(), major(key="opensymphony:sitemesh", kind="replaced",
                                                                  replaced_by="org.sitemesh:sitemesh", approved=True,
                                                                  note="tiles 3 stays")],
                             diff, solution="sitemesh 3 로 올리고 필터를 바꿉니다")
    assert out == GUIDE                                                   # the echoed <verify> tag is stripped
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["role"] == VERIFY_ROLE == "verify_guide"
    assert call["metadata"]["purpose"] == "verify_guide" and call["metadata"]["case_id"] == "abc123"
    system, user = call["messages"][0].content, call["messages"][1].content
    # v2.1u (B-L3): the guide is written right after the apply, before the rebuild
    assert "The build has\nnot been re-run yet; the runtime is unverified" in system and "{{" not in system
    assert "build passes" not in system and "build passes" not in user
    assert "Korean" in system or "한국어" in system
    assert "## Project\nas-is (before sbmig): case=spring_legacy java=1.8 jsp=4" in user
    assert "## Case abc123 (compile, 8 error(s))" in user and user.count("does not exist") == 5
    assert "… 3 more error(s)" in user
    assert "- [major] org.sitemesh:sitemesh 2.4.2 → 3.2.1 — dependencies in demo-back/pom.xml" in user
    assert ("- [replaced] opensymphony:sitemesh 2.4.2 → org.sitemesh:sitemesh 3.2.1 — dependencies in "
            "demo-back/pom.xml — approved by a person (libraries.yml): tiles 3 stays") in user
    assert "## What the fix says it does (해결 방법)\nsitemesh 3 로 올리고 필터를 바꿉니다" in user
    assert "+<version>3.2.1</version>" in user and "case: abc123" not in user      # no fix-replay match by accident
    u = fixer.last_verify_usage
    assert isinstance(u, LLMUsage) and u.calls == 1 and u.input_tokens > 0 and u.output_tokens > 0
    fixer.verify_guide(proj, cluster(), ctx(), [major()], diff)
    assert fixer.verify_usage.calls == 2 and fixer.last_verify_usage.calls == 1
    ev = [json.loads(ln) for ln in proj.journal_path.read_text().splitlines() if '"llm_verify_guide"' in ln]
    assert len(ev) == 2 and ev[0]["calls"] == 1 and ev[0]["changes"][0] == "org.sitemesh:sitemesh 2.4.2 → 3.2.1"
    assert ev[0]["error"] is None


def test_verify_guide_diff_is_capped(cfg, proj):
    client = FakeLLMClient([GUIDE])
    diff = "+" + "x" * (VERIFY_MAX_DIFF_CHARS * 2)
    assert LLMFixer(cfg, client).verify_guide(proj, cluster(), ctx(), [major()], diff) == GUIDE   # no tag: as is
    user = client.calls[0]["messages"][1].content
    assert "… diff cut (" in user and len(user) < VERIFY_MAX_DIFF_CHARS + 3000


def test_verify_guide_empty_changes_failures_and_off(cfg, proj):
    client = FakeLLMClient([GUIDE])
    fixer = LLMFixer(cfg, client)
    assert fixer.verify_guide(proj, cluster(), ctx(), [], "diff") == "" and client.calls == []
    for exc in (RuntimeError("boom"), LLMError(503, "gateway busy")):
        f = LLMFixer(cfg, FakeLLMClient([exc]))
        assert f.verify_guide(proj, cluster(), ctx(), [major()], "diff") == ""
    assert LLMFixer(cfg, FakeLLMClient([NO_FIX_RESPONSE])).verify_guide(proj, cluster(), ctx(), [major()], "") == ""
    assert LLMFixer(cfg, FakeLLMClient([answer()])).verify_guide(proj, cluster(), ctx(), [major()], "") == GUIDE
    assert LLMFixer(cfg, FakeLLMClient(["   "])).verify_guide(proj, cluster(), ctx(), [major()], "") == ""
    cfg.libraries.verify_guide_llm = False
    off = FakeLLMClient([GUIDE])
    assert LLMFixer(cfg, off).verify_guide(proj, cluster(), ctx(), [major()], "diff") == "" and off.calls == []


def test_verify_guide_without_a_case_or_context(cfg, proj):
    """The structure review's commit has no case: cluster / ctx may be None."""
    client = FakeLLMClient([GUIDE])
    assert LLMFixer(cfg, client).verify_guide(proj, None, None, [major()], "") == GUIDE
    user = client.calls[0]["messages"][1].content
    assert "## Case" not in user and "## Dependency changes" in user


def test_parse_verify_answer_cleans_and_caps():
    assert parse_verify_answer(f"```\n## 운영자 확인 사항\n{GUIDE}\n```").text == f"운영자 확인 사항\n{GUIDE}"
    assert parse_verify_answer(f"<verify>{GUIDE}").text == GUIDE                     # closing tag optional
    assert parse_verify_answer(f"some words\n<VERIFY>\n{GUIDE}\n</VERIFY>\nthanks").text == GUIDE
    r = parse_verify_answer("<analysis>no fix</analysis>")
    assert r.text == "" and r.warnings
    long = parse_verify_answer("\n".join(f"운영자 확인: item {i} " + "x" * 80 for i in range(200))).text
    assert len(long) <= VERIFY_MAX_CHARS and long.endswith("\n…")


def test_verify_prompt_file():
    text = load_verify_prompt("v1")
    assert "{{ language }}" in text and "운영자 확인 사항" in text and "No code, no apologies." in text
    assert load_verify_prompt("v9") == text                                        # unknown version → v1
    # v2.1u: the target is rendered from the caller's target_text(cfg), never written in the file
    assert "{{ target }}" not in text and target_text(None) in text
    assert "JDK 25, Spring Boot 4.2.0" in load_verify_prompt("v1", "JDK 25, Spring Boot 4.2.0")
