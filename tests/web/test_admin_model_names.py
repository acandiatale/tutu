"""v2.1z-bg point 3 on the admin page: rows are named by the asked model with the served model(s) beside it; runs
written before the change whose rows say `unknown` / `?` / nothing are put under ONE unnamed row (`model: ""`,
`unnamed: true`), unless the run's own sbmig.log names the role of every answered call."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.web.admin import collect_usage, run_usage, usage_from_log

REQ = {"accepted_at": "2026-11-19T10:00:00+00:00", "upload_name": "shop.zip", "flow": "full"}

# what the company client wrote before the change: the attempt line names params.role, the answer line the gateway's
# placeholder model
OLD_LOG = (
    "t INFO sbmig.fix.company: llm call provider=company role=fix model=asset-42 params.role=build_error_fixer "
    "attempt 1/3 → POST http://gw/chat (prompt 10 chars, timeout 60s)\n"
    "t INFO sbmig.fix.company: llm call provider=company model=unknown in=100 out=10 latency=1.0s finish=stop\n"
    "t INFO sbmig.fix.company: llm call provider=company role=fix_escalation model=asset-42 "
    "params.role=escalation_fixer attempt 1/3 → POST http://gw/chat (prompt 10 chars, timeout 60s)\n"
    "t WARNING sbmig.fix.company: llm company HTTP 503 (attempt 1/3)\n"
    "t INFO sbmig.fix.company: llm call provider=company role=fix_escalation model=asset-42 "
    "params.role=escalation_fixer attempt 2/3 → POST http://gw/chat (prompt 10 chars, timeout 60s)\n"
    "t INFO sbmig.fix.company: llm call provider=company model=unknown in=200 out=20 latency=1.0s finish=stop\n")


def _w(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _run(out: Path, rid: str, *, by_model=None, total=None, log: str | None = None, cost=None) -> Path:
    rd = out / rid
    _w(rd / "control" / "request.json", {**REQ, "run_id": rid})
    if by_model is not None or total is not None:
        _w(rd / "report.json", {"run_id": rid, "started_at": REQ["accepted_at"],
                                "llm_usage": total or {"calls": 0, "input_tokens": 0, "output_tokens": 0},
                                "llm_usage_by_model": by_model or [], "llm_cost": cost})
    if log is not None:
        _w(rd / "sbmig.log", log)
    return rd


def _models(row):
    return [(m["model"], m.get("served", []), m.get("unnamed", False), m["calls"]) for m in row["by_model"]]


def test_old_unknown_and_question_mark_rows_become_one_unnamed_row(tmp_path):
    out = tmp_path / "out"
    _run(out, "a-20261119-100000-aaaa", by_model=[{"model": "unknown", "calls": 2, "input_tokens": 100,
                                                    "output_tokens": 10}])
    _run(out, "b-20261119-100001-bbbb", total={"calls": 3, "input_tokens": 0, "output_tokens": 0, "model": ""})
    _run(out, "c-20261119-100002-cccc", by_model=[{"model": "?", "calls": 1, "input_tokens": 5, "output_tokens": 1},
                                                  {"model": "", "calls": 1, "input_tokens": 5, "output_tokens": 1}])
    data = collect_usage(out)
    assert [(m["model"], m.get("unnamed")) for m in data["by_model"]] == [("", True)]
    assert data["by_model"][0]["calls"] == 7
    c = next(r for p in data["projects"] for r in p["run_list"] if r["run_id"].startswith("c-"))
    assert _models(c) == [("", [], True, 2)]


def test_new_rows_carry_the_served_models(tmp_path):
    out = tmp_path / "out"
    _run(out, "n-20261120-100000-aaaa", by_model=[
        {"model": "build_error_fixer", "calls": 2, "input_tokens": 100, "output_tokens": 10, "served": ["gpt-5.5"]},
        {"model": "escalation_fixer", "calls": 1, "input_tokens": 50, "output_tokens": 5,
         "served": ["gpt-5.6-sol"]}])
    _run(out, "m-20261120-100001-bbbb", by_model=[
        {"model": "build_error_fixer", "calls": 1, "input_tokens": 1, "output_tokens": 1, "served": ["gpt-5.5b"]}])
    data = collect_usage(out)
    agg = {m["model"]: m for m in data["by_model"]}
    assert agg["build_error_fixer"]["served"] == ["gpt-5.5", "gpt-5.5b"] and agg["build_error_fixer"]["calls"] == 3
    assert agg["escalation_fixer"]["served"] == ["gpt-5.6-sol"] and "unnamed" not in agg["escalation_fixer"]


def test_new_log_lines_with_served_are_read(tmp_path):
    rd = _run(tmp_path / "out", "r-20261120-100000-aaaa", log=(
        "t INFO sbmig.fix.company: llm call provider=company model=build_error_fixer in=100 out=10 latency=1.0s "
        "finish=stop served=gpt-5.5\n"
        "t INFO sbmig.fix.company: llm call provider=company model=build_error_fixer in=1 out=1 latency=1.0s "
        "finish=stop served=gpt-5.5,gpt-5.6-sol\n"
        "t INFO sbmig.fix.client: llm call provider=openai model=m in=1 out=1 latency=1.0s finish=stop\n"))
    found = usage_from_log(rd)
    assert found["build_error_fixer"]["calls"] == 2 and found["build_error_fixer"]["served"] == ["gpt-5.5",
                                                                                                 "gpt-5.6-sol"]
    assert found["m"]["calls"] == 1 and not found["m"].get("served")


def test_an_old_log_names_the_role_of_each_answered_call(tmp_path):
    rd = _run(tmp_path / "out", "o-20261119-100000-aaaa", log=OLD_LOG)
    row = run_usage(rd)
    assert _models(row) == [("escalation_fixer", [], False, 1), ("build_error_fixer", [], False, 1)]


def test_an_old_report_is_renamed_from_its_log_only_when_the_counts_agree(tmp_path):
    out = tmp_path / "out"
    unknown = [{"model": "unknown", "calls": 2, "input_tokens": 300, "output_tokens": 30}]
    row = run_usage(_run(out, "o-20261119-100000-aaaa", by_model=unknown, log=OLD_LOG))
    assert _models(row) == [("escalation_fixer", [], False, 1), ("build_error_fixer", [], False, 1)]
    # the log misses a call the report counted: nothing is guessed
    row = run_usage(_run(out, "p-20261119-100000-bbbb", by_model=[{**unknown[0], "calls": 3}], log=OLD_LOG))
    assert _models(row) == [("", [], True, 3)]
    # an answer line without a role before it (legacy gateway, or a line lost): not renamed either
    row = run_usage(_run(out, "q-20261119-100000-cccc", by_model=unknown,
                         log=OLD_LOG.replace("params.role=build_error_fixer ", "")))
    assert _models(row) == [("", [], True, 2)]


def test_an_old_log_without_roles_stays_unnamed(tmp_path):
    rd = _run(tmp_path / "out", "o-20261119-100000-aaaa", log=(
        "t INFO sbmig.fix.company: llm call provider=company model=unknown in=1 out=1 latency=1.0s finish=stop\n"))
    assert _models(run_usage(rd)) == [("", [], True, 1)]


def test_the_page_shows_the_asked_name_the_served_models_and_one_neutral_unnamed_label():
    page = (Path(__file__).resolve().parents[2] / "sbmig" / "web" / "static" / "admin.html").read_text(encoding="utf-8")
    assert "function modelName(m)" in page and "m.unnamed" in page
    assert "이름 없음" in page and "모델 이름을 기록하기 전에 실행한 기록입니다" in page
    assert "(이름 없음)" not in page                         # the old fallback for an empty name only
    assert ".mnone { color: var(--muted);" in page and ".mserved {" in page and "var(--faint)" in page
    assert '<th scope="col">모델</th>' in page and "<td class=\"stick\">${modelName(m)}</td>" in page
    for word in ("오류", "실패", "알 수 없음", "unknown"):
        assert word not in page.split("const UNNAMED_HINT")[1].split("function modelName")[0]
