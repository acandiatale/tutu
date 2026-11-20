"""v2.1z-ak / v2.1z-aw: the page shows a framework project's reduced run (scope jdk-jakarta) — a highlighted notice
naming the framework (headline + what is not done) while running (after analyze) and in the result card, a standing
notice on the upload card when the server's configuration names a framework, and the stages the plan does not have (재구성, 구조 검토) as skipped, never pending.
The page learns the scope from the journal (`framework_project` at analyze, `plan` with `scope`), which reaches the page
in every web mode (in-process and Job modes tail the run directory's journal.jsonl). Static checks on the page source;
the behaviour was checked in a headless Chromium against a scripted SSE server when the page changed."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sbmig.core.config as config_mod
import sbmig.web
from sbmig.core.config import PACKAGE_ROOT
from sbmig.web import RunManager, create_app

PAGE = (Path(sbmig.web.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
def _notice(card: str, el_id: str) -> str:
    return card.split(f'id="{el_id}"', 1)[1].split("</div>\n    </div>", 1)[0]


def test_notice_boxes_in_upload_run_and_result_cards() -> None:
    """v2.1z-aw: a highlighted box (headline + one sentence) in the three places a person meets the reduced scope."""
    intake = PAGE.split('class="card intake"', 1)[1].split("</section>", 1)[0]
    run_card = PAGE.split('id="runcard"', 1)[1].split("</section>", 1)[0]
    result_card = PAGE.split('id="resultcard"', 1)[1].split("</section>", 1)[0]
    for card, el in ((intake, "fwupload"), (run_card, "fwnote"), (result_card, "fwresult")):
        assert f'<div class="fwnote" id="{el}" role="note" hidden>' in card, el
        body = _notice(card, el)
        assert f'id="{el}-head"' in body and f'id="{el}-body"' in body
        # the texts come from the script (the name is server data): nothing hard-coded in the markup
        assert "JDK" not in body
    # the old small chip line is gone
    assert '<span class="chip">프레임워크 프로젝트</span>' not in PAGE
    # the upload notice stands above the flow choice, visible without opening anything
    assert intake.index('id="fwupload"') < intake.index('id="flowpick"') and "<details" not in intake.split('id="fwupload"')[0]
    # a bordered, highlighted box in the page's own tokens; information, not an error
    css = PAGE.split("<style>", 1)[1].split("</style>", 1)[0]
    rule = css.split(".fwnote {", 1)[1].split("}", 1)[0]
    assert "border: 1px solid var(--blue-mid)" in rule and "border-left: 4px solid var(--blue)" in rule
    assert "background: var(--blue-soft)" in rule and "--bad" not in rule and "--warn" not in rule
    assert ".fwnote .fwhead {" in css and "font-weight: 700" in css.split(".fwnote .fwhead {", 1)[1].split("}", 1)[0]


def test_notice_sentences() -> None:
    # the headline, with and without a name (contract v2.1z-aw "The sentence")
    assert "`${fw.name} 프레임워크는 JDK 21, Jakarta만 적용합니다.`" in SCRIPT
    assert "'사내 프레임워크 프로젝트는 JDK 21, Jakarta만 적용합니다.'" in SCRIPT
    # what is not done and what the person gets, by fix_loop
    assert ("'구조 정리, 구조 검토, 빌드 오류 자동 수정은 하지 않습니다. 변환한 뒤 빌드를 한 번 하고, 남은 오류는 보고서로 "
            "알려 드립니다.'") in SCRIPT
    assert "'구조 정리와 구조 검토는 하지 않습니다. 변환한 뒤 빌드하고, 빌드 오류는 자동으로 수정합니다.'" in SCRIPT
    # before the upload
    assert "`${name} 프레임워크를 쓰는 프로젝트는 JDK 21, Jakarta만 적용합니다.`" in SCRIPT
    assert "'사내 프레임워크를 쓰는 프로젝트는 JDK 21, Jakarta만 적용합니다.'" in SCRIPT
    assert "'구조 정리와 빌드 오류 자동 수정은 하지 않고, 변환한 프로젝트와 빌드 결과를 보고서와 함께 드립니다.'" in SCRIPT
    assert "'구조 정리와 구조 검토는 하지 않고, 변환한 뒤 빌드 오류까지 자동으로 수정합니다.'" in SCRIPT
    # the hover title of the skipped stages names the same framework
    assert "n.title = `${fwWho(fwInfo)}라 이 단계는 하지 않습니다`" in SCRIPT
    assert "`${fw.name} 프레임워크 프로젝트` : '사내 프레임워크 프로젝트'" in SCRIPT
    # every older wording is gone
    assert "JDK 21과 jakarta만" not in PAGE and 'title="사내 프레임워크 프로젝트라' not in PAGE


def test_server_text_goes_in_as_text() -> None:
    set_fw = SCRIPT.split("function renderFramework() {", 1)[1].split("\n  }\n", 1)[0]
    upload = SCRIPT.split("function setUploadFramework(f) {", 1)[1].split("\n  }\n", 1)[0]
    for body in (set_fw, upload):
        assert "setText(" in body and "innerHTML" not in body and "insertAdjacentHTML" not in body
    # the skipped stages' markup carries no server text; the title is set as a property afterwards
    skipped = SCRIPT.split("function placeSkipped() {", 1)[1].split("\n  }\n", 1)[0]
    assert "fwInfo" not in skipped.split("insertAdjacentHTML", 1)[0] and "title=" not in skipped.split("n.title", 1)[0]


def test_scope_comes_from_the_journal_and_the_status() -> None:
    assert ("(name === 'framework_project' || name === 'plan') && ev.scope === 'jdk-jakarta') "
            "setFramework(fwFrom(ev, fwInfo));") in SCRIPT
    # the status object (report.json or journal on the server) sets it too: a run opened from 지난 실행
    assert ("if (st.framework && typeof st.framework === 'object' && st.framework.scope === 'jdk-jakarta') "
            "setFramework(fwFrom(st.framework, fwInfo));") in SCRIPT
    # an event of an older run (no name, no fix_loop) reads as: no name, the fix loop ran (it always did before)
    fw_from = SCRIPT.split("function fwFrom(src, prev) {", 1)[1].split("\n  }\n", 1)[0]
    assert "typeof src.fix_loop === 'boolean' ? src.fix_loop : (typeof p.fixLoop === 'boolean' ? p.fixLoop : true)" in fw_from
    assert "typeof src.name === 'string' ? src.name.trim() : (p.name || '')" in fw_from
    # a new run starts without the notice (a full run shows nothing special)
    assert "resetRail(runFlow); setFramework(null);" in SCRIPT
    # the upload notice follows /api/configs
    assert "setUploadFramework(d.framework);" in SCRIPT and "f.configured === true" in SCRIPT


def test_skipped_stages_are_drawn_and_never_current() -> None:
    assert "const SKIP_STEPS = ['restructure', 'review'];" in SCRIPT
    assert "review: '구조 검토'" in SCRIPT and "restructure: '재구성'" in SCRIPT
    assert 'class="step skipped"' in SCRIPT and "하지 않음" in SCRIPT
    assert ".rail .step.skipped .dot" in PAGE
    # re-placed after the plan draws its phases, and the next current step skips them
    assert "for (const ph of u.phases) phaseStep(ph, true);\n      placeSkipped();" in SCRIPT
    assert "steps.slice(last + 1).find((n) => !n.classList.contains('skipped'))" in SCRIPT
    # the remediation-only flow has no migration stages to skip
    assert "if (!fwReduced || railFlow === FLOW_REM) return;" in SCRIPT


def test_page_comments_are_plain() -> None:
    added = [ln for ln in PAGE.splitlines() if "프레임워크" in ln and ("//" in ln or "/*" in ln or "<!--" in ln)]
    assert added
    for ln in added:
        assert "—" not in ln and "→" not in ln and "v2." not in ln, ln


# ---- HTTP (v2.1z-aw): GET /api/configs "framework", GET /api/runs/{id} "framework"

FW_YML = 'framework:\n  artifacts: ["example-fw-core"]\n'
FW_YML_NO_LOOP = FW_YML + "  fix_loop: false\n"


def _repo(tmp_path, extra: str | None) -> Path:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    shutil.copy(PACKAGE_ROOT / "config" / "artifacts.local-e2e.yml", repo / "config" / "artifacts.local-e2e.yml")
    if extra is not None:
        (repo / "config" / "site.yml").write_text(extra, encoding="utf-8")
    return repo


def _client(tmp_path, extra: str | None, configs: list[str] | None = None) -> TestClient:
    repo = _repo(tmp_path, extra)
    names = configs if configs is not None else (["artifacts.local-e2e.yml", "site.yml"] if extra is not None else None)
    return TestClient(create_app(tmp_path / "out", repo, default_configs=names))


@pytest.fixture(autouse=True)
def _no_framework_env(monkeypatch):
    for key in ("SBMIG_FRAMEWORK__ARTIFACTS", "SBMIG_FRAMEWORK__NAME", "SBMIG_FRAMEWORK__FIX_LOOP",
                "SBMIG_FRAMEWORK__SCOPE", "SBMIG_FRAMEWORK"):
        monkeypatch.delenv(key, raising=False)


def test_configs_names_the_configured_framework(tmp_path) -> None:
    d = _client(tmp_path, FW_YML_NO_LOOP).get("/api/configs").json()
    assert d["framework"] == {"configured": True, "name": "Example", "fix_loop": False, "remediation_always": False}


def test_configs_framework_fix_loop_defaults_to_true(tmp_path) -> None:
    """v2.1z-bi: without `framework.fix_loop` in the yml the framework run keeps the fix loop."""
    d = _client(tmp_path, FW_YML).get("/api/configs").json()
    assert d["framework"] == {"configured": True, "name": "Example", "fix_loop": True, "remediation_always": False}


def test_configs_framework_name_and_fix_loop_from_the_yml(tmp_path) -> None:
    yml = 'framework:\n  artifacts: ["com.example:example-fw-core"]\n  name: "Example Platform"\n  fix_loop: true\n'
    d = _client(tmp_path, yml).get("/api/configs").json()
    assert d["framework"] == {"configured": True, "name": "Example Platform", "fix_loop": True, "remediation_always": False}


def test_configs_without_framework_or_with_full_scope(tmp_path) -> None:
    assert _client(tmp_path / "a", None).get("/api/configs").json()["framework"] == \
        {"configured": False, "name": "", "fix_loop": False, "remediation_always": False}
    # scope full: such a project gets the whole migration, nothing to announce before the upload
    full = FW_YML + "  scope: full\n"
    assert _client(tmp_path / "b", full).get("/api/configs").json()["framework"]["configured"] is False
    # a glob names no single artifact: configured, without a name (the page says 사내 프레임워크)
    glob = 'framework:\n  artifacts: ["example-fw-*"]\n  fix_loop: false\n'
    assert _client(tmp_path / "c", glob).get("/api/configs").json()["framework"] == \
        {"configured": True, "name": "", "fix_loop": False, "remediation_always": False}


def test_configs_unloadable_config_is_not_a_500(tmp_path) -> None:
    r = _client(tmp_path, "framework:\n  artifacts: [\n").get("/api/configs")          # broken yaml
    assert r.status_code == 200 and r.json()["framework"] == {"configured": False, "name": "", "fix_loop": False, "remediation_always": False}
    r = _client(tmp_path / "x", "framework:\n  scope: nonsense\n").get("/api/configs")   # validation error
    assert r.status_code == 200 and r.json()["framework"]["configured"] is False


def test_configs_framework_is_cached_until_a_file_changes(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, FW_YML)
    calls = []
    real = config_mod.load_config
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    for _ in range(3):
        assert client.get("/api/configs").json()["framework"]["name"] == "Example"
    assert len(calls) <= 1                                   # loaded at most once (the app may have loaded it already)
    site = tmp_path / "repo" / "config" / "site.yml"
    site.write_text('framework:\n  artifacts: ["example-fw-core"]\n  name: "Example Two"\n', encoding="utf-8")
    st = site.stat()
    os.utime(site, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert client.get("/api/configs").json()["framework"]["name"] == "Example Two"


def _run_dir(tmp_path, rid: str) -> Path:
    rd = tmp_path / "out" / rid
    rd.mkdir(parents=True)
    return rd


def _journal(rd, *events: dict) -> None:
    with open(rd / "journal.jsonl", "a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps({"ts": "2026-11-19T00:00:00+00:00", **ev}) + "\n")


def _report(rd, framework: dict | None) -> None:
    profile = {"framework": framework} if framework is not None else {}
    (rd / "report.json").write_text(json.dumps({"success": False, "blocked": [], "unresolved": [], "profile": profile}),
                                    encoding="utf-8")


def test_status_framework_from_report(tmp_path) -> None:
    client = _client(tmp_path, None)
    rd = _run_dir(tmp_path, "fw-run")
    _report(rd, {"scope": "jdk-jakarta", "name": "Example", "fix_loop": False, "patterns": ["example-fw-core"]})
    # the report wins over a journal that says something else
    _journal(rd, {"event": "framework_project", "scope": "jdk-jakarta", "name": "Other", "fix_loop": True})
    st = client.get("/api/runs/fw-run").json()
    assert st["framework"] == {"name": "Example", "scope": "jdk-jakarta", "fix_loop": False}
    # the list carries it too (same status dict)
    row = next(r for r in client.get("/api/runs").json()["runs"] if r["run_id"] == "fw-run")
    assert row["framework"]["name"] == "Example"


def test_status_no_framework_for_other_runs(tmp_path) -> None:
    client = _client(tmp_path, None)
    _report(_run_dir(tmp_path, "plain"), None)
    _report(_run_dir(tmp_path, "full-scope"), {"scope": "full", "name": "Example", "fix_loop": False})
    rd = _run_dir(tmp_path, "running-plain")
    _journal(rd, {"event": "plan", "steps": ["transform.rewrite"], "scope": "full"})
    _run_dir(tmp_path, "empty")
    for rid in ("plain", "full-scope", "running-plain", "empty"):
        assert "framework" not in client.get(f"/api/runs/{rid}").json(), rid


def test_status_framework_from_the_journal_while_running(tmp_path) -> None:
    client = _client(tmp_path, None)
    rd = _run_dir(tmp_path, "fw-live")
    _journal(rd, {"event": "intake"})
    assert "framework" not in client.get("/api/runs/fw-live").json()
    _journal(rd, {"event": "framework_project", "scope": "jdk-jakarta", "artifacts": ["example-fw-core"],
                  "name": "Example", "fix_loop": False})
    assert client.get("/api/runs/fw-live").json()["framework"] == \
        {"name": "Example", "scope": "jdk-jakarta", "fix_loop": False}
    _journal(rd, {"event": "plan", "steps": ["transform.rewrite", "verify.build"], "scope": "jdk-jakarta",
                  "name": "Example", "fix_loop": False})
    assert client.get("/api/runs/fw-live").json()["framework"]["name"] == "Example"
    # after the plan event the journal is not read any further
    scan = client.app.state.manager._fw_scans["fw-live"]
    assert scan.done
    offset = scan.offset
    _journal(rd, {"event": "build", "ok": False})
    client.get("/api/runs/fw-live")
    assert scan.offset == offset


def test_status_framework_of_an_older_run(tmp_path) -> None:
    """A run written before v2.1z-aw: no name, no fix_loop. Those runs ran the fix loop, so fix_loop reads true."""
    client = _client(tmp_path, None)
    rd = _run_dir(tmp_path, "old-journal")
    _journal(rd, {"event": "framework_project", "scope": "jdk-jakarta", "artifacts": ["example-fw-core"]},
             {"event": "plan", "steps": ["transform.rewrite"], "scope": "jdk-jakarta"})
    assert client.get("/api/runs/old-journal").json()["framework"] == \
        {"name": "", "scope": "jdk-jakarta", "fix_loop": True}
    _report(_run_dir(tmp_path, "old-report"), {"scope": "jdk-jakarta", "patterns": ["example-fw-core"]})
    assert client.get("/api/runs/old-report").json()["framework"] == \
        {"name": "", "scope": "jdk-jakarta", "fix_loop": True}


def test_status_framework_never_raises(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, None)
    rd = _run_dir(tmp_path, "odd")
    (rd / "journal.jsonl").write_bytes(b'{"event": "framework_project", "scope": "jdk-jak\n\xff\xfe not json "plan"\n'
                                       b'{"event": "framework_project", "scope": "jdk-jakarta", "name": 7}\n'
                                       b'{"event": "framework_project", "scope": "jdk-jakarta"')   # last line unfinished
    st = client.get("/api/runs/odd").json()
    assert st["framework"] == {"name": "", "scope": "jdk-jakarta", "fix_loop": True}
    (rd / "report.json").write_text("{broken", encoding="utf-8")       # unreadable report: the journal answers
    assert client.get("/api/runs/odd").json()["framework"]["scope"] == "jdk-jakarta"

    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(RunManager, "_journal_framework", boom)
    (rd / "report.json").unlink()
    r = client.get("/api/runs/odd")
    assert r.status_code == 200 and "framework" not in r.json()


def test_sse_status_carries_framework(tmp_path) -> None:
    client = _client(tmp_path, None)
    rd = _run_dir(tmp_path, "fw-sse")
    _report(rd, {"scope": "jdk-jakarta", "name": "Example", "fix_loop": False})
    (rd / "web-status.json").write_text(json.dumps({"run_id": "fw-sse", "exit_code": 2}), encoding="utf-8")
    with client.stream("GET", "/api/runs/fw-sse/events") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                first = json.loads(line[6:])
                break
    assert first["framework"] == {"name": "Example", "scope": "jdk-jakarta", "fix_loop": False}


# ---- v2.1z-aw "The remediation runs after a failed build too": the page side

REM_FW_YML = FW_YML_NO_LOOP + "remediation:\n  after_migration: true\n"


def test_configs_says_whether_the_remediation_follows_a_failed_build(tmp_path) -> None:
    on = _client(tmp_path / "a", REM_FW_YML).get("/api/configs").json()["framework"]
    assert on == {"configured": True, "name": "Example", "fix_loop": False, "remediation_always": True}
    # the chained remediation off, or switched to `never` after a failed build: nothing to promise
    off = _client(tmp_path / "b", FW_YML + "remediation:\n  after_migration: false\n").get("/api/configs").json()
    assert off["framework"]["remediation_always"] is False
    never = REM_FW_YML + "  after_failed_build: never\n"
    assert _client(tmp_path / "c", never).get("/api/configs").json()["framework"]["remediation_always"] is False
    # v2.1z-bi: with the fix loop on (the default) the remediation follows a failed build too
    loop = REM_FW_YML.replace("  fix_loop: false\n", "  fix_loop: true\n", 1)
    assert _client(tmp_path / "d", loop).get("/api/configs").json()["framework"] == \
        {"configured": True, "name": "Example", "fix_loop": True, "remediation_always": True}
    default = FW_YML + "remediation:\n  after_migration: true\n"
    assert _client(tmp_path / "g", default).get("/api/configs").json()["framework"]["remediation_always"] is True
    # nothing configured, or a config that cannot be loaded
    assert _client(tmp_path / "e", None).get("/api/configs").json()["framework"]["remediation_always"] is False
    assert _client(tmp_path / "f", "framework: [\n").get("/api/configs").json()["framework"]["remediation_always"] is False


def _report_with_remediation(rd, rem: dict, *, success: bool = False) -> None:
    (rd / "report.json").write_text(json.dumps({
        "success": success, "blocked": [], "unresolved": [{"x": 1}], "error_entries": [],
        "profile": {"framework": {"scope": "jdk-jakarta", "name": "Example", "fix_loop": False}},
        "remediation": rem}), encoding="utf-8")
    (rd / "web-status.json").write_text(json.dumps({"run_id": rd.name, "exit_code": 2 if not success else 0,
                                                    "flow": "full", "success": success}), encoding="utf-8")


def test_status_of_a_failed_framework_run_carries_the_remediation(tmp_path) -> None:
    client = _client(tmp_path, None)
    rd = _run_dir(tmp_path, "fw-failed")
    _report_with_remediation(rd, {"status": "INCOMPLETE", "reason": "baseline_build_failed", "baseline_tolerated": True,
                                  "missing": None})
    st = client.get("/api/runs/fw-failed").json()
    # the migration stays failed; the remediation summary is there beside it, flag included
    assert st["success"] is False and st["exit_code"] == 2 and st["state"] == "finished"
    assert st["remediation"] == {"status": "INCOMPLETE", "reason": "baseline_build_failed", "baseline_tolerated": True}
    # with a reasons list in the summary it is passed on, so the list can tell "fixed, not built" from "partly"
    rd4 = _run_dir(tmp_path, "fw-reasons")
    _report_with_remediation(rd4, {"status": "INCOMPLETE", "reason": "baseline_build_failed", "baseline_tolerated": True,
                                   "reasons": ["baseline_build_failed"]})
    assert client.get("/api/runs/fw-reasons").json()["remediation"]["reasons"] == ["baseline_build_failed"]
    rd5 = _run_dir(tmp_path, "odd-reasons")
    _report_with_remediation(rd5, {"status": "INCOMPLETE", "reasons": "baseline_build_failed"})
    assert "reasons" not in client.get("/api/runs/odd-reasons").json()["remediation"]
    assert st["framework"]["fix_loop"] is False
    # an older summary without the flag: unchanged shape
    rd2 = _run_dir(tmp_path, "older")
    _report_with_remediation(rd2, {"status": "FAILED", "reason": "baseline_build_failed"}, success=True)
    assert client.get("/api/runs/older").json()["remediation"] == {"status": "FAILED", "reason": "baseline_build_failed"}
    # a non-boolean flag is not passed on
    rd3 = _run_dir(tmp_path, "odd-flag")
    _report_with_remediation(rd3, {"status": "INCOMPLETE", "baseline_tolerated": "yes"})
    assert "baseline_tolerated" not in client.get("/api/runs/odd-flag").json()["remediation"]


def test_page_tolerated_remediation_reads_plainly() -> None:
    render = SCRIPT.split("function renderRemediation(runId, st, force) {", 1)[1].split("\n  }\n", 1)[0]
    # the flag from the remediation status, else from the run's status summary (report.remediation)
    assert "const tolerated = remTolerated(st);" in render
    fn = SCRIPT.split("function remTolerated(st) {", 1)[1].split("\n  }\n", 1)[0]
    assert "st.baseline_tolerated === true" in fn and "runRem.baseline_tolerated === true" in fn
    # one plain sentence instead of reading like an ordinary failure
    assert ("'빌드가 실패한 프로젝트라 바꾼 라이브러리 버전을 빌드로 확인하지 못했습니다. "
            "빌드 오류를 고친 뒤 한 번 빌드해서 확인해 주세요.'") in SCRIPT
    assert "setText('remed-note', unconfirmed ?" in render and 'id="remed-note"' in PAGE
    # the reason chip and the ZIP badge say what happened, not "the remediation broke the build"
    assert ("tolerated && code === 'baseline_build_failed' ? (unconfirmed ? '빌드로 확인하지 못함' : '조치 전에도 빌드 실패')"
            in render)
    assert "sameFailure ? '빌드 미확인' : '빌드 실패본'" in render
    # the note and the badge follow the delivery's own flag: a change set that added a build error is a failed build
    unconfirmed = SCRIPT.split("function remUnconfirmed(st) {", 1)[1].split("\n  }\n", 1)[0]
    assert "remTolerated(st)" in unconfirmed and "st.delivery.build_tolerated === true" in unconfirmed
    assert "const unconfirmed = remUnconfirmed(st) && Number(c.proposals_applied || 0) > 0;" in render


def test_page_failed_build_and_remediation_both_readable() -> None:
    # the run card's sub line and the 지난 실행 list add the remediation's outcome AFTER the migration's
    fn = SCRIPT.split("function remShort(st) {", 1)[1].split("\n  }\n", 1)[0]
    assert "st.flow === FLOW_REM" in fn and "st.state !== 'finished'" in fn   # skipped: not in REM_SHORT, so nothing
    assert "`취약점 ${REM_SHORT[r.status]}`" in fn
    # the owner (2026-11-19): a finished remediation adds nothing to the list (see test_page_list_word_*)
    assert "INCOMPLETE:" not in SCRIPT.split("const REM_SHORT =", 1)[1].split("};", 1)[0]
    # the list: the migration's word first, then the remediation's
    assert "const sfx = remShort(run);" in SCRIPT
    assert ('<span class="state ${cls}">${esc(s.short)}${sfx ? `<span class="sfx ${remShortCls(run)}"> · ${esc(sfx)}</span>`'
            in SCRIPT)
    # the run card: the failure stays the title (describe() unchanged), the remediation goes into the sub line after it
    set_status = SCRIPT.split("function setStatus(st) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const remS = remShort(st); if (remS) bits.push(remS);" in set_status
    assert set_status.index("setTitle(d.cls, d.text, live)") < set_status.index("remShort(st)")
    # the result card's remediation block is loaded for every finished full run, whatever the migration's verdict
    show = SCRIPT.split("function showResult(st, d) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (current) loadRemediation(current);" in show and "success" not in show


def test_page_notice_says_the_remediation_follows_any_build() -> None:
    assert "'취약점 조치는 빌드 결과와 관계없이 이어서 합니다.'" in SCRIPT
    body = SCRIPT.split("function fwBody(fw) {", 1)[1].split("\n  }\n", 1)[0]
    # v2.1z-bi: the sentence is added whether or not the loop runs (both upload and run notices)
    assert "return fwRemAlways ? `${base} ${FW_REM_ALWAYS}` : base;" in body and "!fw.fixLoop && fwRemAlways" not in body
    upload = SCRIPT.split("function setUploadFramework(f) {", 1)[1].split("\n  }\n", 1)[0]
    assert "+ (fwRemAlways ? ` ${FW_REM_ALWAYS}` : ''));" in upload
    # the "not fixed" wording stays bound to the loop being off
    assert upload.index("f.fix_loop === true ?") < upload.index("빌드 오류 자동 수정은 하지 않고")
    assert "fwRemAlways = !!(d.framework && d.framework.remediation_always === true);" in SCRIPT
    # configs may arrive after the run's notice: the notice is written again
    assert "renderFramework();" in SCRIPT.split("async function loadConfigs() {", 1)[1].split("\n  }\n", 1)[0]


def test_page_finished_remediation_leaves_no_running_note() -> None:
    # a finished remediation (after a passing or a failing build) must not keep saying 취약점 조치 중 under the rail
    load = SCRIPT.split("async function loadRemediation(runId, force) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if ($('railnote').textContent === REM_RUNNING_NOTE) $('railnote').textContent = '';" in load
    # the tolerated ZIP badge is a caution, not an error
    assert "sameFailure ? 'warn' : 'bad');" in SCRIPT and ".badge.warn {" in PAGE


def test_page_fixed_but_not_built_is_not_called_partly() -> None:
    """Case B: INCOMPLETE with the one reason baseline_build_failed after a failed build reads as remediated, unbuilt."""
    fn = SCRIPT.split("function remBuildOnly(r, tolerated) {", 1)[1].split("\n  }\n", 1)[0]
    assert "r.status === 'INCOMPLETE'" in fn and "tolerated" in fn
    assert "r.reasons.length === 1 && r.reasons[0] === 'baseline_build_failed'" in fn
    assert "const REM_UNCONFIRMED_KO = '조치함 · 빌드 미확인';" in SCRIPT
    render = SCRIPT.split("function renderRemediation(runId, st, force) {", 1)[1].split("\n  }\n", 1)[0]
    assert "remBuildOnly(st, remTolerated(st)) ? REM_UNCONFIRMED_KO : (REM_KO[st.status] || st.status)" in render
    # the list and the sub line say nothing for an INCOMPLETE remediation any more (the block's chip still does)
    short = SCRIPT.split("function remShort(st) {", 1)[1].split("\n  }\n", 1)[0]
    assert "remBuildOnly" not in short and "조치함(빌드 미확인)" not in SCRIPT
    assert "INCOMPLETE: '일부만 조치됨'" in SCRIPT


def test_page_unchanged_original_after_a_failed_build_has_no_badge() -> None:
    """Case A: nothing was applied, so the original ZIP gets no 빌드 미확인 / 빌드 실패본 badge."""
    render = SCRIPT.split("function renderRemediation(runId, st, force) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const applied = Number(c.proposals_applied || 0) > 0;" in render
    assert "const badBuild = failed && !(sameFailure && !applied);" in render
    assert "(badBuild ? 'ghost' : 'quiet')" in render
    assert "badBuild ? (sameFailure ? '빌드 미확인' : '빌드 실패본') : ''" in render


# ---- the owner (2026-11-19): "성공, 취약점 일부만 조치 이부분 그냥 완료 로만 나오게 해줘!"

def test_page_list_word_of_a_successful_run_is_done() -> None:
    describe = SCRIPT.split("function describe(st) {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (st.success === true) return { cls: 'ok', text: '빌드에 성공했습니다', short: '완료' };" in describe
    assert "short: '성공'" not in SCRIPT
    # a remediation-only run keeps its own words
    assert "if (st.success === true) return { cls: 'ok', text: '조치를 마쳤습니다', short: '조치 완료' };" in describe


def test_page_list_suffix_only_when_a_person_must_know() -> None:
    table = SCRIPT.split("const REM_SHORT =", 1)[1].split("};", 1)[0]
    # rule 2: nothing for a remediation that ended without anything to do from the list
    for quiet in ("SUCCEEDED", "INCOMPLETE", "skipped", "LEGACY_WAITING"):
        assert quiet not in table, quiet
    # rule 3: the states a person must act on or know about
    for status, word in (("FAILED", "조치 실패"), ("AWAITING_CONFIRMATION", "확인 필요"), ("STOPPED", "조치 멈춤"),
                         ("INTERRUPTED", "조치 중단"), ("QUEUED", "조치 대기"), ("RUNNING", "조치 중")):
        assert f"{status}: '{word}'" in table, status
    fn = SCRIPT.split("function remShort(st) {", 1)[1].split("\n  }\n", 1)[0]
    # a remediation running again in a new pod (the report still shows the state it stopped in)
    assert "rj.active === true" in fn and "return '취약점 조치 중';" in fn
    # rule 4: the same suffix after every migration outcome; the list and the sub line share remShort
    assert "const sfx = remShort(run);" in SCRIPT
    assert "const remS = remShort(st); if (remS) bits.push(remS);" in SCRIPT
    # the suffix keeps its own colour: 조치 실패 after 완료 is not green
    cls = SCRIPT.split("function remShortCls(st) {", 1)[1].split("\n  }\n", 1)[0]
    assert "status === 'FAILED'" in cls and "return 'bad';" in cls and "status === 'AWAITING_CONFIRMATION'" in cls
    assert ".runs .state .sfx.bad { color: var(--bad); }" in PAGE
    # a remediation-only run: its outcome is the remediation, no suffix
    assert "st.flow === FLOW_REM" in fn
