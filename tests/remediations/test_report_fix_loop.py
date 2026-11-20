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
    assert "`rounds/1/fix.patch`" in body and "fix.patch" in body
    assert DETACHED.search(body) is None, body


def test_fixed_round_leads_the_action_list(tmp_path):
    state, paths = _fixed(tmp_path)
    md = report.build_report_md(state, paths)
    steps = _section(md, "해야 할 일").strip().splitlines()
    assert steps[0].startswith("1. **자동으로 고친 소스 확인하기**")
    assert "자동으로 고친 소스 2개를 확인해 주세요." in steps[0]
    assert "`rounds/1/fix.patch`" in steps[0]


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


def test_record_shows_the_first_build_and_the_fix(tmp_path):
    state, paths = _fixed(tmp_path)
    record = _section(report.build_report_md(state, paths), "처리 기록")
    assert "- 처음 수정본 빌드: 실패 (종료 코드 1)" in record
    assert "- 빌드 오류 자동 수정: 고침 · 파일 2개 · 2회 반복 · LLM 호출 4회" in record


def test_report_json_carries_rounds_fix(tmp_path):
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
    state, paths = _unfixed(tmp_path, "time budget used up", attempted=False)
    md = report.build_report_md(state, paths)
    assert "남은 시간이 없어 빌드 오류 자동 수정은 시작하지 않았습니다." in md
    assert "시도했지만" not in md.split("## 처리 기록", 1)[0]
    assert "- 빌드 오류 자동 수정: 남은 시간이 없어 시작하지 않음" in md


def test_every_reason_reads_in_korean_in_the_report(tmp_path):
    reasons = ["not fixed: 1 error(s) left", "llm unavailable: gateway 503", "stop requested", "time budget used up",
               "rejected: pom.xml must not be edited: app/pom.xml", "rejected: something else",
               "error: confirming build failed",
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
    """The three sentences `fixer.check_paths` writes keep only the path; nothing English is left."""
    from sbmig.remediations.fixer import check_paths
    root = tmp_path / "tree"
    (root / "app/src").mkdir(parents=True)
    (root / "app/pom.xml").write_text("<project/>", encoding="utf-8")
    (root / "app/src/New.java").write_text("class New {}", encoding="utf-8")
    cases = {"app/pom.xml": "고칠 수 없는 파일: app/pom.xml", "app/src/Gone.java": "지울 수 없는 파일: app/src/Gone.java",
             "app/src/New.java": "올린 프로젝트에 없는 파일: app/src/New.java"}
    for path, want in cases.items():
        why = check_paths(root, [path], {"app/pom.xml"})
        assert why, path
        ko, raw = i18n.fix_reason_ko(f"rejected: {why}")
        assert (ko, raw) == (want, ""), (path, why, ko, raw)
        assert i18n.ko_display(f"automatic build fix: rejected: {why}") == (
            f"빌드 오류 자동 수정을 시도했지만 고치지 못했습니다({want}). 소스 수정은 되돌렸습니다.")
