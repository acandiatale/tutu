"""The remediation report and the page digest of the build fix loop on a failed candidate (INTERFACES "v2.1z-bi" B,
"Report and page"): a fixed round gets its own section, leads the action list and changes the "nothing changed"
sentences; a round the loop could not fix gets one line under the failed-build warning; `report.json` carries
`rounds[].fix`; `report.fix_summary` feeds the page."""
from __future__ import annotations

import json
import re

from sbmig.remediations import graph, i18n, report
from sbmig.remediations.models import FAILED, SUCCEEDED, BuildOutcome, FixResult, RoundPlan

from .fakes import (
    FakeMaven,
    FakeScanner,
    Recorder,
    ScriptedPlanner,
    finding,
    make_cfg,
    make_components,
    make_run,
    proposal,
    scan_result,
)

DETACHED = re.compile(r"[A-Za-z0-9`)\]] (은|는|이|가|을|를|과|와|로|으로|의|에|에서|도|만|이라|라)(?=[\s,.)]|$)")
FILES = ["app-back/src/main/java/com/example/Mapper.java", "app-back/src/main/java/com/example/Service.java"]


def _run(tmp_path, **kw):
    state, paths = make_run(tmp_path)
    cfg = kw.pop("cfg", None) or make_cfg()
    comps = make_components(**kw)
    rec = Recorder()
    out = graph.run_remediation(state, paths, cfg, comps, save=rec.save, journal=rec.journal, stop=rec.stop)
    return out, paths


def _fixing(**kw):
    return dict(scan=FakeScanner({"baseline": [scan_result(finding())], "final-r1": [scan_result()]}),
                plan=ScriptedPlanner([RoundPlan(round=1, source_tree_hash="", policy_hash="", proposals=[proposal()])]),
                **kw)


def _section(md: str, title: str) -> str:
    head = next(ln for ln in md.splitlines() if ln.startswith(f"## {title}"))
    return md.split(f"\n{head}\n", 1)[1].split("\n## ", 1)[0]


def _fixed(tmp_path):
    """A succeeded run whose round 1 candidate failed first and was fixed by the loop."""
    out, paths = _run(tmp_path, **_fixing())
    assert out.status == SUCCEEDED and out.rounds[0].committed
    rs = out.rounds[0]
    rs.fix = FixResult(attempted=True, fixed=True, iterations=2, attempts=3, llm_calls=4, input_tokens=100,
                       output_tokens=50, models=["build_error_fixer"], changed=list(FILES), patch="rounds/1/fix.patch")
    rs.fix_first_build = BuildOutcome(ok=False, exit_code=1, log="rounds/1/build.log", summary="[ERROR] cannot find symbol")
    return out, paths


def _unfixed(tmp_path, reason="not fixed: 2 error(s) left", *, attempted=True, **cfg):
    out, paths = _run(tmp_path, cfg=make_cfg(**cfg), **_fixing(maven=FakeMaven(outcomes=[True, False])))
    assert out.status == FAILED
    out.rounds[-1].fix = FixResult(attempted=attempted, fixed=False, iterations=3 if attempted else 0,
                                   llm_calls=5 if attempted else 0, reason=reason, remaining=2)
    out.message = (out.message or "the candidate build failed") + f" | automatic build fix: {reason}"
    return out, paths


def test_fixed_round_has_its_own_section_after_the_applied_changes(tmp_path):
    state, paths = _fixed(tmp_path)
    md = report.build_report_md(state, paths)
    heads = [ln for ln in md.splitlines() if ln.startswith("## ")]
    names = [h[3:].split(" — ")[0] for h in heads]
    assert names.index("빌드 오류 수정") == names.index("조치 완료") + 1
    body = _section(md, "빌드 오류 수정")
    assert "버전을 올린 뒤 빌드가 실패해 소스를 고쳤습니다." in body
    assert "| 파일 |" in body
    for f in FILES:
        assert f"| `{f}` |" in body
    assert "2회 반복" in body and "LLM을 4회 호출했습니다" in body
    # one download for the reader; the per-round path stays in the record
    assert "바꾼 내용 전체는 `fix.patch` 파일에 모아 두었습니다." in body
    assert "rounds/1/fix.patch" not in body
    assert DETACHED.search(body) is None, body


def test_fixed_round_leads_the_action_list(tmp_path):
    state, paths = _fixed(tmp_path)
    md = report.build_report_md(state, paths)
    steps = _section(md, "해야 할 일").strip().splitlines()
    assert steps[0].startswith("1. **자동으로 고친 소스 확인하기**")
    assert "자동으로 고친 소스 2개를 확인해 주세요." in steps[0]
    assert "바꾼 내용은 「빌드 오류 수정」과 `fix.patch` 파일에 있습니다." in steps[0]
    assert "rounds/" not in steps[0]


def test_fixed_round_changes_the_nothing_changed_sentences(tmp_path):
    state, paths = _fixed(tmp_path)
    body = _section(report.build_report_md(state, paths), "변경하지 않은 것")
    assert "애플리케이션 소스와 프로젝트 구조(파일·폴더)는 변경하지 않았습니다." not in body
    assert "프로젝트 구조(파일·폴더)는 변경하지 않았습니다. 소스는 빌드 오류를 고치느라 2개 파일을 바꿨습니다." in body
    assert "달라진 것은 적용된 pom.xml의 버전 표기와 고친 소스 2개뿐입니다." in body


def test_an_override_entry_and_a_fix_join_with_the_right_particle(tmp_path):
    state, paths = _fixed(tmp_path)
    plan = RoundPlan.from_dict(json.loads(paths.plan(1).read_text(encoding="utf-8")))
    for p in plan.proposals:
        p.kind = "managed_override"
    paths.plan(1).write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    body = _section(report.build_report_md(state, paths), "변경하지 않은 것")
    assert "버전 표기, dependencyManagement에 추가한 버전 지정과 고친 소스 2개뿐입니다." in body


def test_record_shows_the_first_build_and_theFixResult(tmp_path):
    state, paths = _fixed(tmp_path)
    record = _section(report.build_report_md(state, paths), "처리 기록")
    assert "- 처음 수정본 빌드: 실패 (종료 코드 1)" in record
    assert "- 빌드 오류 자동 수정: 고침 · 파일 2개 · 2회 반복 · LLM 호출 4회 · 바꾼 내용 `rounds/1/fix.patch`" in record


def test_report_json_carries_roundsFixResult(tmp_path):
    state, paths = _fixed(tmp_path)
    data = report.build_report_json(state, paths)
    fix = data["rounds"][0]["fix"]
    assert fix["fixed"] is True and fix["changed"] == FILES and fix["patch"] == "rounds/1/fix.patch"
    assert fix["llm_calls"] == 4 and fix["models"] == ["build_error_fixer"]
    assert data["state"]["rounds"][0]["fix"]["iterations"] == 2
    json.dumps(data)                                 # JSON-able


def test_a_round_without_the_loop_is_unchanged(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    data = report.build_report_json(out, paths)
    assert "fix" not in data["rounds"][0]
    md = report.build_report_md(out, paths)
    assert "빌드 오류 수정" not in md and "자동으로 고친" not in md
    assert "애플리케이션 소스와 프로젝트 구조(파일·폴더)는 변경하지 않았습니다." in md
    assert report.fix_summary(out) is None


def test_unfixed_round_adds_one_line_under_the_failed_build_warning(tmp_path):
    state, paths = _unfixed(tmp_path)
    md = report.build_report_md(state, paths)
    lines = md.splitlines()
    warn = next(i for i, ln in enumerate(lines) if ln.startswith("> **주의:** 결과 ZIP"))
    block = lines[warn:lines.index("", warn)]
    want = "> 빌드 오류 자동 수정을 시도했지만 고치지 못했습니다(남은 오류 2건). 소스 수정은 되돌렸습니다."
    assert block[-1] == want
    assert md.count("빌드 오류 자동 수정을 시도했지만") == 1     # the message suffix is not repeated elsewhere
    assert "빌드 오류 수정\n" not in md and "자동으로 고친 소스" not in md
    assert "애플리케이션 소스와 프로젝트 구조(파일·폴더)는 변경하지 않았습니다." in md
    record = _section(md, "처리 기록")
    assert "- 빌드 오류 자동 수정: 고치지 못함(남은 오류 2건) · 3회 반복 · LLM 호출 5회 · 소스 수정은 되돌림" in record


def test_unfixed_round_without_a_delivery_still_says_it(tmp_path):
    state, paths = _unfixed(tmp_path, "llm unavailable: LLM_API_KEY is not set", failed_candidate="discard")
    assert state.delivery is None
    head = report.build_report_md(state, paths).split("\n## ", 1)[0]
    assert ("> 빌드 오류 자동 수정을 시도했지만 고치지 못했습니다(LLM을 사용할 수 없음). 소스 수정은 되돌렸습니다. "
            "원문: `LLM_API_KEY is not set`") in head


def test_a_loop_that_never_started_does_not_claim_a_try(tmp_path):
    for i, (reason, why) in enumerate((("time budget used up", "남은 시간 부족"), ("stop requested", "중지 요청"))):
        state, paths = _unfixed(tmp_path / str(i), reason, attempted=False)
        md = report.build_report_md(state, paths)
        head = md.split("\n## ", 1)[0]
        assert f"> 빌드 오류 자동 수정은 시작하지 않았습니다({why})." in head.splitlines()
        assert "시도했지만" not in md.split("## 처리 기록", 1)[0]
        assert "되돌렸" not in head and "되돌림" not in md
        assert f"- 빌드 오류 자동 수정: 시작하지 않음({why})" in md
        assert DETACHED.search(head) is None, head


def test_every_reason_reads_in_korean_in_the_report(tmp_path):
    reasons = ["not fixed: 1 error(s) left", "llm unavailable: gateway 503", "llm unavailable", "stop requested",
               "time budget used up", "rejected: pom.xml must not be edited: app/pom.xml",
               "rejected: app/mvnw must not be edited", "rejected: something else",
               "error: confirming build failed", "discarded: the candidate was remade",
               "error: RuntimeError: boom"]
    for i, reason in enumerate(reasons):
        state, paths = _unfixed(tmp_path / str(i), reason)
        head = report.build_report_md(state, paths).split("\n## ", 1)[0]
        line = next(ln for ln in head.splitlines() if "빌드 오류 자동 수정" in ln)
        assert re.search(r"고치지 못했습니다\([^()]+\)\. 소스 수정은 되돌렸습니다\.", line), line
        assert DETACHED.search(line) is None, line
        assert line[2:] == i18n.fix_failed_ko(reason)


def test_state_message_suffix_translates_for_the_page(tmp_path):
    state, _paths = _unfixed(tmp_path, "stop requested")
    ko, raw = i18n.split_known(state.message)
    assert "빌드 오류 자동 수정을 시도했지만 고치지 못했습니다(중지 요청). 소스 수정은 되돌렸습니다." in ko
    assert not any("automatic build fix" in r for r in raw)


def test_fix_summary_for_the_page(tmp_path):
    state, _paths = _fixed(tmp_path)
    assert report.fix_summary(state) == {"fixed": True, "changed": 2, "llm_calls": 4, "round": 1}
    state, _paths = _unfixed(tmp_path / "u")
    assert report.fix_summary(state) == {"fixed": False, "changed": 0, "llm_calls": 5, "round": None}


def test_a_fixed_round_that_was_not_committed_counts_no_source_change(tmp_path):
    """The loop fixed the build but the round was discarded later (e.g. the resolution check): nothing of it is in the
    result, so the report keeps saying the source was not changed."""
    state, paths = _fixed(tmp_path)
    state.rounds[0].committed = False
    md = report.build_report_md(state, paths)
    assert "## 빌드 오류 수정" not in md and "자동으로 고친 소스" not in md
    assert report.fix_summary(state)["fixed"] is False


def test_old_state_without_fix_field_renders(tmp_path):
    """A RoundState object from before the field existed (no `fix` attribute at all) still renders."""
    out, paths = _run(tmp_path, **_fixing())
    rs = out.rounds[0]
    object.__setattr__(rs, "__dict__", {k: v for k, v in rs.__dict__.items() if k not in ("fix", "fix_first_build")})
    assert not hasattr(rs, "fix") or getattr(rs, "fix", None) is None
    report.build_report_md(out, paths)
    report.build_report_json(out, paths)
    assert report.fix_summary(out) is None


def test_status_api_carries_the_fix_digest(tmp_path, monkeypatch):
    """GET /api/remediations/{id} carries `fix` (None without a loop) — what the page's chip and LLM count read."""
    from sbmig.core.config import PACKAGE_ROOT, load_config
    from sbmig.remediations.models import (
        INCOMPLETE,
        RemediationState,
        RoundState,
        RunPaths,
        read_json,
        write_json_atomic,
    )
    from sbmig.remediations.service import REMEDIATIONS_DIR

    from .test_service import _foreign_run, manager
    for k, v in (("IVAS_BASE_URL", "https://ivas.invalid"), ("IVAS_USER_ID", "tester"), ("IVAS_PASSWORD", "pw")):
        monkeypatch.setenv(k, v)
    cfg = load_config(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml")
    cfg = cfg.model_copy(update={"remediation": cfg.remediation.model_copy(update={"trusted_projects": True})})
    rid = _foreign_run(tmp_path, cfg, "r20261120-100000-aaaaaa", INCOMPLETE)
    path = RunPaths(tmp_path / "out" / REMEDIATIONS_DIR / rid).state
    st = RemediationState.from_dict(read_json(path))
    st.rounds = [RoundState(round=1, applied=["p-1-1"], committed=True,
                            fix=FixResult(attempted=True, fixed=True, iterations=1, llm_calls=2, changed=FILES[:1],
                                          patch="rounds/1/fix.patch"))]
    write_json_atomic(path, st.to_dict())
    plain = _foreign_run(tmp_path, cfg, "r20261120-100001-bbbbbb", INCOMPLETE)
    mgr = manager(tmp_path, cfg)
    assert mgr.status(rid)["fix"] == {"fixed": True, "changed": 1, "llm_calls": 2, "round": 1}
    assert mgr.status(plain)["fix"] is None


def test_guard_sentences_of_the_fixer_read_as_words(tmp_path):
    """The sentences `fixer.check_paths` writes keep only the path; nothing English is left."""
    import inspect

    from sbmig.remediations.fixer import check_paths
    root = tmp_path / "tree"
    (root / "app/src").mkdir(parents=True)
    (root / "app/pom.xml").write_text("<project/>", encoding="utf-8")
    (root / "app/mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "app/src/New.java").write_text("class New {}", encoding="utf-8")
    loops = {"app/pom.xml", "app/mvnw", "app/src/Gone.java"}
    cases = {"app/pom.xml": "고칠 수 없는 파일: app/pom.xml", "app/src/Gone.java": "지울 수 없는 파일: app/src/Gone.java",
             "app/src/New.java": "올린 프로젝트에 없는 파일: app/src/New.java"}
    four = len(inspect.signature(check_paths).parameters) >= 4
    if four:                                          # the work-copy fixer's guard: a loop path that is not editable
        cases["app/mvnw"] = "고칠 수 없는 파일: app/mvnw"
    for path, want in cases.items():
        why = check_paths(root, [path], loops, {"app/src/Gone.java"}) if four else check_paths(root, [path], loops)
        assert why, path
        ko, raw = i18n.fix_reason_ko(f"rejected: {why}")
        assert (ko, raw) == (want, ""), (path, why, ko, raw)
        assert i18n.ko_display(f"automatic build fix: rejected: {why}") == (
            f"빌드 오류 자동 수정을 시도했지만 고치지 못했습니다({want}). 소스 수정은 되돌렸습니다.")


# ---------------------------------------------------------------------------- v2.1z-bj: a fix is shown only when it
# is in the result (committed, fixed, files changed); retries, discarded fixes, not-started loops


def _retry(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    rs = out.rounds[0]
    rs.fix = FixResult(attempted=True, fixed=True, iterations=1, llm_calls=0, changed=[], retry_passed=True)
    rs.fix_first_build = BuildOutcome(ok=False, exit_code=1, log="rounds/1/build.log", summary="[ERROR] flaky")
    return out, paths


def test_a_retry_that_passed_gets_one_record_line_and_no_fix_section(tmp_path):
    state, paths = _retry(tmp_path)
    md = report.build_report_md(state, paths)
    assert "## 빌드 오류 수정" not in md and "자동으로 고친 소스" not in md
    assert "애플리케이션 소스와 프로젝트 구조(파일·폴더)는 변경하지 않았습니다." in md
    assert "고친 소스" not in _section(md, "변경하지 않은 것")
    record = _section(md, "처리 기록")
    assert record.count("- 빌드 오류 자동 수정:") == 1
    assert "- 빌드 오류 자동 수정: 처음 빌드는 실패했지만 다시 빌드하니 통과해 소스는 고치지 않았습니다." in record
    assert report.fix_summary(state) == {"fixed": False, "changed": 0, "llm_calls": 0, "round": None}
    assert report.served_fix_round(state) is None


def test_a_fixed_result_without_changed_files_is_read_as_a_retry(tmp_path):
    """A FixResult written without `retry_passed` (fixed, no files) is not shown as a fix either."""
    state, paths = _retry(tmp_path)
    state.rounds[0].fix.retry_passed = False
    md = report.build_report_md(state, paths)
    assert "## 빌드 오류 수정" not in md
    assert "다시 빌드하니 통과해 소스는 고치지 않았습니다." in md


def test_a_discarded_fix_gets_a_record_line_and_its_calls_count(tmp_path):
    """Round 1: a fix discarded when the candidate was remade, then a second loop fixed the remade candidate."""
    state, paths = _fixed(tmp_path)
    rs = state.rounds[0]
    rs.fix_discarded = [FixResult(attempted=True, fixed=False, iterations=2, llm_calls=3, changed=[],
                                  discarded_changed=FILES[:1], reason="discarded: the candidate was remade")]
    md = report.build_report_md(state, paths)
    record = _section(md, "처리 기록")
    assert ("- 빌드 오류 자동 수정: 버린 수정(수정본을 다시 만들어 이전 수정은 버림) · 파일 1개 · 2회 반복 · "
            "LLM 호출 3회") in record
    assert "- 빌드 오류 자동 수정: 고침 · 파일 2개 · 2회 반복 · LLM 호출 4회" in record
    assert DETACHED.search(record.split("버린 수정", 1)[1].splitlines()[0]) is None
    assert "## 빌드 오류 수정" in md                    # the second fix is in the result
    assert report.fix_summary(state) == {"fixed": True, "changed": 2, "llm_calls": 7, "round": 1}
    data = report.build_report_json(state, paths)
    assert data["rounds"][0]["fix_discarded"][0]["llm_calls"] == 3


def test_a_fix_whose_confirming_build_failed_is_a_discardedFixResult(tmp_path):
    state, paths = _unfixed(tmp_path, "error: confirming build failed")
    rs = state.rounds[-1]
    rs.fix = FixResult(attempted=True, fixed=False, iterations=3, llm_calls=5, changed=[], discarded_changed=FILES,
                  reason="error: confirming build failed")
    md = report.build_report_md(state, paths)
    record = _section(md, "처리 기록")
    assert ("- 빌드 오류 자동 수정: 버린 수정(고친 뒤 다시 한 빌드가 실패함) · 파일 2개 · 3회 반복 · LLM 호출 5회"
            in record)
    assert record.count("- 빌드 오류 자동 수정:") == 1
    head = md.split("\n## ", 1)[0]
    assert "> " + i18n.fix_failed_ko("error: confirming build failed") in head.splitlines()
    assert "## 빌드 오류 수정" not in md
    assert report.fix_summary(state) == {"fixed": False, "changed": 0, "llm_calls": 5, "round": None}


def test_only_discarded_fixes_still_give_a_digest(tmp_path):
    """The fix was discarded with the candidate, the remade candidate built without the loop."""
    out, paths = _run(tmp_path, **_fixing())
    out.rounds[0].fix_discarded = [FixResult(attempted=True, fixed=False, llm_calls=2, discarded_changed=FILES,
                                             reason="discarded: the candidate was remade")]
    assert report.fix_summary(out) == {"fixed": False, "changed": 0, "llm_calls": 2, "round": None}
    md = report.build_report_md(out, paths)
    assert "## 빌드 오류 수정" not in md and "버린 수정(수정본을 다시 만들어 이전 수정은 버림)" in md


def test_fix_summary_per_state(tmp_path):
    """The digest the page reads, one state each (INTERFACES "v2.1z-bj", Files route, report, page)."""
    fixed, _ = _fixed(tmp_path / "a")
    assert report.fix_summary(fixed) == {"fixed": True, "changed": 2, "llm_calls": 4, "round": 1}
    fixed.rounds[0].committed = False
    assert report.fix_summary(fixed) == {"fixed": False, "changed": 0, "llm_calls": 4, "round": None}
    retry, _ = _retry(tmp_path / "b")
    assert report.fix_summary(retry) == {"fixed": False, "changed": 0, "llm_calls": 0, "round": None}
    not_started, _ = _unfixed(tmp_path / "c", "time budget used up", attempted=False)
    assert report.fix_summary(not_started) == {"fixed": False, "changed": 0, "llm_calls": 0, "round": None}
    failed, _ = _unfixed(tmp_path / "d")
    assert report.fix_summary(failed) == {"fixed": False, "changed": 0, "llm_calls": 5, "round": None}


def test_not_started_suffix_of_the_state_message_reads_for_the_page(tmp_path):
    state, _paths = _unfixed(tmp_path, "time budget used up", attempted=False)
    state.message = state.message.replace("automatic build fix:", "automatic build fix not started:")
    ko, raw = i18n.split_known(state.message)
    assert "빌드 오류 자동 수정은 시작하지 않았습니다(남은 시간 부족)." in ko
    assert not any("automatic build fix" in r for r in raw)
    assert not any("되돌렸" in k for k in ko)


# ---------------------------------------------------------------------------- v2.1z-bk: one patch for the reader,
# build-side changes and an interrupted loop in the record


def _round(n, *, committed=True, fix=None, **kw):
    from sbmig.remediations.models import RoundState
    return RoundState(round=n, committed=committed, fix=fix, **kw)


def _fixed_fix(n, files):
    return FixResult(attempted=True, fixed=True, iterations=1, llm_calls=2, changed=list(files),
                     patch=f"rounds/{n}/fix.patch")


def _two_fixed(tmp_path):
    """Rounds 1 and 2 committed, each with a fix; `FILES[0]` is fixed in both rounds."""
    state, paths = _fixed(tmp_path)
    state.rounds.append(_round(2, fix=_fixed_fix(2, [FILES[0], "app-back/src/main/java/com/example/Third.java"])))
    for n in (1, 2):
        paths.round_dir(n).mkdir(parents=True, exist_ok=True)
        (paths.round_dir(n) / "fix.patch").write_text(f"--- a/x{n}\n+++ b/x{n}\n", encoding="utf-8")
    return state, paths


def _before_record(md):
    return md.split("## 처리 기록", 1)[0]


def test_two_fixed_rounds_give_one_combined_patch_and_one_download_name(tmp_path):
    state, paths = _two_fixed(tmp_path)
    report.write_report(state, paths)
    combined = (paths.root / "fix.patch").read_text(encoding="utf-8")
    assert combined == "# round 1\n--- a/x1\n+++ b/x1\n# round 2\n--- a/x2\n+++ b/x2\n"
    assert not list(paths.root.glob("fix.patch.tmp"))
    md = paths.report_md.read_text(encoding="utf-8")
    top = _before_record(md)
    assert "rounds/1/fix.patch" not in top and "rounds/2/fix.patch" not in top
    body = _section(md, "빌드 오류 수정")
    assert body.count("`fix.patch`") == 1 and "### 라운드 1" in body and "### 라운드 2" in body
    assert "바꾼 내용 전체는 `fix.patch` 파일에 라운드마다 나누어 모아 두었습니다." in body
    steps = _section(md, "해야 할 일")
    assert steps.count("`fix.patch`") == 1 and "자동으로 고친 소스 3개를 확인해 주세요." in steps
    record = _section(md, "처리 기록")
    assert "바꾼 내용 `rounds/1/fix.patch`" in record and "바꾼 내용 `rounds/2/fix.patch`" in record


def test_two_fixed_rounds_never_say_only_version_text(tmp_path):
    state, paths = _two_fixed(tmp_path)
    md = report.build_report_md(state, paths)
    body = _section(md, "변경하지 않은 것")
    assert "버전 표기뿐입니다" not in md
    assert "달라진 것은 적용된 pom.xml의 버전 표기와 고친 소스 3개뿐입니다." in body
    assert "소스는 빌드 오류를 고치느라 3개 파일을 바꿨습니다." in body
    assert "애플리케이션 소스" not in body


def test_a_file_fixed_in_two_rounds_counts_once(tmp_path):
    state, _paths = _two_fixed(tmp_path)
    assert report.fix_summary(state) == {"fixed": True, "changed": 3, "llm_calls": 6, "round": 2}


def test_a_fix_not_in_the_result_is_not_mentioned_outside_the_record(tmp_path):
    """Round 2 fixed but did not commit, and holds a discarded fix too: only round 1 is in the result."""
    state, paths = _fixed(tmp_path)
    other = "app-back/src/main/java/com/example/Other.java"
    gone = "app-back/src/main/java/com/example/Gone.java"
    rs2 = _round(2, committed=False, fix=_fixed_fix(2, [other]))
    rs2.fix_discarded = [FixResult(attempted=True, fixed=False, iterations=1, llm_calls=1, discarded_changed=[gone],
                                   reason="discarded: the stored fix is not usable")]
    state.rounds.append(rs2)
    for n in (1, 2):
        paths.round_dir(n).mkdir(parents=True, exist_ok=True)
        (paths.round_dir(n) / "fix.patch").write_text(f"patch {n}\n", encoding="utf-8")
    report.write_report(state, paths)
    assert (paths.root / "fix.patch").read_text(encoding="utf-8") == "# round 1\npatch 1\n"
    md = paths.report_md.read_text(encoding="utf-8")
    top = _before_record(md)
    assert other not in top and gone not in top and "라운드 2" not in _section(md, "빌드 오류 수정")
    assert "자동으로 고친 소스 2개" in top and "고친 소스 2개뿐입니다" in top
    record = _section(md, "처리 기록")
    assert "- 빌드 오류 자동 수정: 수정본을 채택하지 않아 결과에 넣지 않음 · 파일 1개" in record
    assert "rounds/2/fix.patch" not in md
    assert "버린 수정(저장해 둔 수정을 쓸 수 없어 버림) · 파일 1개" in record


def test_no_fix_in_the_result_removes_the_combined_patch(tmp_path):
    state, paths = _fixed(tmp_path)
    paths.round_dir(1).mkdir(parents=True, exist_ok=True)
    (paths.round_dir(1) / "fix.patch").write_text("patch 1\n", encoding="utf-8")
    report.write_report(state, paths)
    assert (paths.root / "fix.patch").is_file()
    state.rounds[0].committed = False
    report.write_report(state, paths)
    assert not (paths.root / "fix.patch").exists()
    assert "fix.patch" not in _before_record(paths.report_md.read_text(encoding="utf-8"))


def test_build_side_changes_get_one_record_line(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    poms = ["app/pom.xml", "app/src/main/java/com/example/A.java"]
    out.rounds[0] = _round(1, committed=True, build=out.rounds[0].build, applied=out.rounds[0].applied,
                           build_changed=poms)
    record = _section(report.build_report_md(out, paths), "처리 기록")
    assert ("- 빌드가 바꾼 파일 2개는 결과에 넣지 않고 원래대로 두었습니다. `app/pom.xml`, "
            "`app/src/main/java/com/example/A.java`") in record
    many = [f"app/src/main/java/com/example/F{i}.java" for i in range(8)]
    out.rounds[0].build_changed = many
    md = report.build_report_md(out, paths)
    line = next(ln for ln in md.splitlines() if ln.startswith("- 빌드가 바꾼 파일"))
    assert line.startswith("- 빌드가 바꾼 파일 8개는 결과에 넣지 않고 원래대로 두었습니다. ")
    assert line.count("`") == 10 and line.endswith(" 외 3개") and "F5.java" not in line
    assert DETACHED.search(line) is None, line
    out.rounds[0].build_changed = []
    assert "빌드가 바꾼 파일" not in report.build_report_md(out, paths)
    assert "build_changed" not in out.rounds[0].to_dict()          # left out of the JSON when empty


def test_an_interrupted_loop_is_a_discarded_line_with_its_calls(tmp_path):
    out, paths = _run(tmp_path, **_fixing())
    out.rounds[0].fix_discarded = [
        FixResult(attempted=True, fixed=False, llm_calls=7, reason="interrupted: the run ended during the loop"),
        FixResult(attempted=True, fixed=False, llm_calls=0,
                  reason="interrupted: the run ended during the loop (usage unknown)")]
    md = report.build_report_md(out, paths)
    record = _section(md, "처리 기록")
    assert "- 빌드 오류 자동 수정: 버린 수정(실행이 수정 도중에 끝남) · LLM 호출 7회" in record.splitlines()
    assert "- 빌드 오류 자동 수정: 버린 수정(실행이 수정 도중에 끝남) · LLM 호출 수는 알 수 없음" in record.splitlines()
    assert "interrupted" not in md and "usage unknown" not in md
    assert "## 빌드 오류 수정" not in md
    assert report.fix_summary(out) == {"fixed": False, "changed": 0, "llm_calls": 7, "round": None}
