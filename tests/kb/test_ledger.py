from __future__ import annotations

import json

from sbmig.core.models import (
    GAV,
    BlockedItem,
    BlockReason,
    BuildError,
    DecisionKind,
    ErrorKind,
    FileChange,
    KBRecipe,
    Phase,
    PhaseResult,
    ResolutionRecord,
    RewriteResult,
)
from sbmig.kb import FileKnowledgeBase
from sbmig.kb.ledger import Ledger, SymbolIndex, render_learning_md

from .helpers import FakeLLM, make_attempt, make_case, recipe_yaml, with_kb

JSON_L1 = "compile|cannot_find_symbol|class JsonComponent|package org.springframework.boot.jackson"
JSON_L2 = "compile|cannot_find_symbol|JsonComponent"
MONEY = "src/main/java/com/example/json/Money.java"


def json_error() -> BuildError:
    return BuildError(
        kind=ErrorKind.COMPILE,
        message="cannot find symbol",
        file=MONEY,
        line=3,
        symbol="class JsonComponent",
        location="package org.springframework.boot.jackson",
        signature=JSON_L1,
        signature_generic=JSON_L2,
    )


def json_changes() -> list[FileChange]:
    return [
        FileChange(
            path=MONEY,
            op="modify",
            search="import org.springframework.boot.jackson.JsonComponent;",
            replace="import org.springframework.boot.jackson.JacksonComponent;",
        ),
        FileChange(
            path=MONEY,
            op="modify",
            search="@JsonComponent\npublic class Money {",
            replace="@JacksonComponent\npublic class Money {",
        ),
    ]


def json_attempt(aid: str = "j1"):
    return make_attempt(aid, changes=json_changes(), errors=[json_error()], analysis="JsonComponent renamed")


def rec(**kw) -> ResolutionRecord:
    data = dict(
        id=kw.pop("id", "r"),
        project="p1",
        run_id="run-1",
        symbol="Foo",
        resolution="llm_patch",
        outcome="improved",
        fix_labels=["type-rename"],
        replacement="a.Foo → b.Foo",
    )
    data.update(kw)
    return ResolutionRecord(**data)


# --------------------------------------------------------------------------- Ledger
def test_ledger_append_iter_filter(tmp_path):
    led = Ledger(tmp_path / "kb" / "ledger.jsonl")
    assert list(led.iter()) == [] and led.count() == 0
    n = led.append(
        [
            rec(id="1"),
            rec(
                id="2",
                project="p2",
                symbol="Bar",
                resolution="kb_recipe",
                fix_labels=["dependency-add"],
                signature="compile|x|Bar",
            ),
            rec(id="3", resolution="blocked", outcome="blocked", symbol="com.co:legacy"),
        ]
    )
    assert n == 3 and led.count() == 3 and led.append([]) == 0
    (tmp_path / "kb" / "ledger.jsonl").open("a").write("not json\n\n")
    assert [r.id for r in led.iter()] == ["1", "2", "3"]
    assert [r.id for r in led.iter(project="p2")] == ["2"]
    assert [r.id for r in led.iter(symbol="^Foo$")] == ["1"]
    assert [r.id for r in led.iter(label="dependency")] == ["2"]
    assert [r.id for r in led.iter(resolution="blocked")] == ["3"]
    assert [r.id for r in led.iter(signature=r"\|Bar")] == ["2"]
    assert [r.id for r in led.iter(run_id="other")] == []


def test_ledger_summary(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    led.append(
        [
            rec(id="a", project="p1"),
            rec(id="b", project="p2"),
            rec(id="c", project="p3", symbol="Baz", replacement=""),
            rec(id="d", project="p3", symbol="Baz", outcome="regressed"),  # failed → not counted as fixed
            rec(id="e", project="p4", symbol="Qux"),
            rec(id="f", project="p5", symbol="Qux"),
            rec(
                id="g",
                project="p6",
                symbol="Qux",
                resolution="plan_recipe",
                recipe_id="com.company.kb.Qux",
                outcome="build_ok",
            ),
            rec(
                id="h",
                project="p1",
                resolution="blocked",
                resolver_id="external_javax_api",
                symbol="com.co:legacy",
                outcome="blocked",
                notes=["ask the library owner"],
            ),
            rec(
                id="i",
                project="p2",
                resolution="blocked",
                resolver_id="external_javax_api",
                symbol="com.co:legacy",
                outcome="blocked",
            ),
            rec(id="j", project="p2", resolution="unresolved", outcome="unresolved", symbol="Zed"),
        ]
    )
    s = led.summary()
    assert s["records"] == 10 and s["projects"] == 6
    assert s["by_resolution"]["llm_patch"] == 6
    top = s["llm_symbols"][0]
    assert top["symbol"] in ("Foo", "Qux") and top["projects"] == 2
    without = [r["symbol"] for r in s["llm_symbols_without_recipe"]]
    assert without == ["Foo"]  # Qux has a KB recipe now, Baz was fixed once
    assert s["blocked"][0] == {
        "symbol": "com.co:legacy",
        "reason": "external_javax_api",
        "count": 2,
        "suggestion": "ask the library owner",
        "projects": 2,
        "project_names": ["p1", "p2"],
    }
    assert s["kb_fired"] == [
        {"id": "com.company.kb.Qux", "kind": "plan_recipe", "count": 1, "projects": 1, "symbol": "Qux"}
    ]
    assert s["unresolved"] == [{"symbol": "Zed", "count": 1}]
    assert led.summary(run_ids=["nope"])["records"] == 0
    md = render_learning_md(s, {"cases": 3, "symbols": 2})
    assert "## Write a recipe / handler for these" in md and "| Foo |" in md and "com.co:legacy" in md
    assert "cases: 3" in md


# --------------------------------------------------------------------------- SymbolIndex
def test_symbol_index_add_and_rebuild(tmp_path):
    idx = SymbolIndex(tmp_path / "symbols.json")
    c1 = make_case(
        id="c1",
        symbol="Foo",
        fix_labels=["type-rename"],
        replacement="a.Foo → b.Foo",
        project_fingerprint="f1",
    )
    assert idx.add(c1) and not idx.add(make_case(id="c0", symbol=""))
    idx.add(c1, project_key="f2")
    idx.add(make_case(id="c2", symbol="Foo", fix_labels=["generic-rewrite"], project_fingerprint="f3"))
    idx.save()
    data = json.loads((tmp_path / "symbols.json").read_text())
    fixes = data["Foo"]["fixes"]
    assert fixes[0]["labels"] == ["type-rename"] and fixes[0]["projects"] == 2 and fixes[0]["cases"] == ["c1"]
    assert len(fixes) == 2
    assert (
        SymbolIndex(tmp_path / "symbols.json").hints("Foo")[0]
        == "[symbol Foo] fixed in 2 project(s) by type-rename: a.Foo → b.Foo [cases c1]"
    )
    assert idx.rebuild([c1]) == 1
    assert json.loads((tmp_path / "symbols.json").read_text())["Foo"]["fixes"][0]["projects"] == 1


# --------------------------------------------------------------------------- harvest integration
def _phase(*attempts, **kw) -> PhaseResult:
    return PhaseResult(phase=Phase.SPRING, attempts=list(attempts), **kw)


def test_harvest_labels_case_and_compiles_candidate_without_llm(kb, ws, profile):
    llm = FakeLLM("NONE")
    res = kb.harvest(_phase(json_attempt()), profile, ws, llm=llm)
    assert llm.calls == []  # deterministic candidate: no LLM call
    assert res.candidates_created == ["com.company.kb.JsonComponentToJacksonComponent"]
    assert any("compiled from labels, no LLM" in n for n in res.notes)
    case = kb.get_case(res.cases_created[0])
    assert case.project == "demo" and case.symbol == "JsonComponent"
    assert case.fix_labels == ["annotation-replace", "type-rename"]
    assert case.replacement == (
        "org.springframework.boot.jackson.JsonComponent → org.springframework.boot.jackson.JacksonComponent"
    )
    assert case.recipe_candidate_id == "com.company.kb.JsonComponentToJacksonComponent"
    r = kb.get_recipe(case.recipe_candidate_id)
    assert r.status == "candidate" and JSON_L2 in r.signatures and r.source_case_ids == [case.id]
    # symbols.json
    sym = json.loads(kb.symbols_path.read_text())["JsonComponent"]
    assert sym["generic_signatures"] == [JSON_L2]
    assert sym["fixes"][0]["recipe_id"] == r.id and sym["fixes"][0]["projects"] == 1
    # ledger
    lines = list(kb.ledger().iter())
    assert res.ledger_entries == 1 and len(lines) == 1
    line = lines[0]
    assert line.resolution == "llm_patch" and line.symbol == "JsonComponent" and line.project == "demo"
    assert line.case_id == case.id and line.recipe_id == r.id and line.files == ["Money.java"]
    assert line.run_id == "t-run" and line.signature_generic == JSON_L2 and line.model == "fake-model"
    assert "/" not in "".join(line.files)


def test_harvest_llm_candidate_is_tagged(kb, ws, profile):
    llm = FakeLLM(recipe_yaml())
    res = kb.harvest(_phase(make_attempt()), profile, ws, llm=llm)  # security_change: not compilable
    assert res.candidates_created == ["com.company.kb.RenameFooToBar"] and len(llm.calls) == 1
    assert llm.kwargs[0]["role"] == "recipe_candidate"
    assert llm.kwargs[0]["metadata"] == {"case_id": res.cases_created[0]}
    assert "## Deterministic labels of the fix" in llm.calls[0][1].content


def test_deterministic_candidates_can_be_disabled(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, deterministic_candidates=False))
    llm = FakeLLM("NONE")
    res = kb.harvest(_phase(json_attempt()), profile, ws, llm=llm)
    assert len(llm.calls) == 1 and res.candidates_created == []


def test_harvest_ledger_lines_for_all_resolutions(kb, ws, profile):
    r = kb.add_recipe(
        recipe_yaml(), KBRecipe(id="com.company.kb.RenameFooToBar", path="", signatures=[JSON_L2])
    )
    cid = kb.record_case(make_case(symbol="Thing"))
    blocked = BlockedItem(
        id="blk1",
        reason=BlockReason.EXTERNAL_JAVAX_API,
        phase=Phase.SPRING,
        cluster_id="c9",
        signatures=["compile|incompatible_types|x"],
        files=[f"{ws.root}/src/A.java"],
        artifact=GAV(group_id="com.company", artifact_id="legacy-web", version="1.0"),
        suggestion=f"ask owners; see {ws.root}/pom.xml",
    )
    attempts = [
        json_attempt("llm"),
        make_attempt("h", kind=DecisionKind.HANDLER, changes=[]).model_copy(
            update={
                "decision": make_attempt().decision.model_copy(
                    update={"kind": DecisionKind.HANDLER, "handler_id": "lombok-floor"}
                )
            }
        ),
        make_attempt("kr", kind=DecisionKind.KB_RECIPE, recipe_ids=[r.id], changes=[]),
        make_attempt("kp", kind=DecisionKind.KB_PATCH, kb_case_ids=[cid]),
        make_attempt("skip", kind=DecisionKind.SKIP),
    ]
    rewrite = RewriteResult(ok=True, recipes=[r.id], recipes_applied={r.id: [f"{ws.root}/src/B.java"]})
    res = kb.harvest(
        _phase(*attempts, blocked=[blocked], rewrite_results=[rewrite], build_ok=True), profile, ws
    )
    lines = list(kb.ledger().iter())
    by = {ln.resolution: ln for ln in lines}
    assert res.ledger_entries == len(lines) == 6
    assert set(by) == {"llm_patch", "handler", "kb_recipe", "kb_patch", "plan_recipe", "blocked"}
    assert by["handler"].resolver_id == "lombok-floor"
    assert by["kb_recipe"].recipe_id == r.id and by["kb_patch"].case_id == cid
    assert by["plan_recipe"].recipe_id == r.id and by["plan_recipe"].outcome == "build_ok"
    assert by["plan_recipe"].files == ["B.java"]
    b = by["blocked"]
    assert b.symbol == "com.company:legacy-web" and b.resolver_id == "external_javax_api"
    assert b.files == ["A.java"] and str(ws.root) not in b.model_dump_json()
    # the same blocked item is not written again by the run-level hook
    from sbmig.core.models import CaseType, RunReport, utcnow

    report = RunReport(
        run_id="t-run",
        project_name="demo",
        case_type=CaseType.SPRING_BOOT,
        started_at=utcnow(),
        blocked=[blocked],
        unresolved=[json_error()],
    )
    run_res = kb.record_run(report, profile, ws)
    assert run_res.ledger_entries == 1
    assert [ln.resolution for ln in kb.ledger().iter(resolution="unresolved")] == ["unresolved"]


def test_recipes_promoted_reported(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, auto_promote_after_projects=1))
    r = kb.add_recipe(
        recipe_yaml(), KBRecipe(id="com.company.kb.RenameFooToBar", path="", signatures=[JSON_L2])
    )
    res = kb.harvest(
        _phase(make_attempt("kr", kind=DecisionKind.KB_RECIPE, recipe_ids=[r.id], changes=[])), profile, ws
    )
    assert res.recipes_credited == [r.id] and res.recipes_promoted == [r.id]
    assert kb.get_recipe(r.id).status == "active"
    assert kb.drain_promoted() == []
    # promotions done outside the harvest (loop / plan credit) are reported by the next harvest
    r2 = kb.add_recipe(recipe_yaml("com.company.kb.Other"), KBRecipe(id="com.company.kb.Other", path=""))
    assert kb.credit_recipe(r2.id, True, "fp-x").status == "active"
    assert kb.credit_recipe("com.company.kb.Missing", True, "fp") is None
    assert kb.harvest(_phase(), profile, ws).recipes_promoted == [r2.id]


def test_dedupe_case_counts_new_project_in_symbol_index(kb, ws, profile):
    kb.harvest(_phase(json_attempt("a")), profile, ws)
    other = profile.model_copy(update={"fingerprint": "fp-other", "name": "other"})
    kb.harvest(_phase(json_attempt("b")), other, ws)
    fix = json.loads(kb.symbols_path.read_text())["JsonComponent"]["fixes"][0]
    assert fix["projects"] == 2
    # reindex rebuilds symbols.json from the cases (reuse_projects included)
    kb.symbols_path.unlink()
    kb.reindex()
    assert json.loads(kb.symbols_path.read_text())["JsonComponent"]["fixes"][0]["projects"] == 2
    st = kb.stats()
    assert st["symbols"] == 1 and st["ledger_entries"] == 2


def test_ledger_disabled(kb_cfg, ws, profile):
    kb = FileKnowledgeBase(with_kb(kb_cfg, ledger=False, symbol_index=False))
    res = kb.harvest(_phase(json_attempt()), profile, ws)
    assert res.ledger_entries == 0 and not kb.ledger_path.exists() and not kb.symbols_path.exists()


def test_export_import_carries_ledger(kb, kb_cfg, ws, profile, tmp_path):
    kb.harvest(_phase(json_attempt()), profile, ws)
    z = kb.export_zip(tmp_path / "kb.zip")
    other = FileKnowledgeBase(with_kb(kb_cfg, dir=str(tmp_path / "kb2")))
    counters = other.import_zip(z)
    assert counters["ledger_added"] == 1 and other.ledger().count() == 1
    assert other.import_zip(z)["ledger_added"] == 0
    assert other.symbols_path.exists()


# --------------------------------------------------------------------------- kb CLI: ledger / symbols / learning
def test_kb_cli_ledger_symbols_learning(kb, ws, profile):
    from typer.testing import CliRunner

    from sbmig.kb import kb_cli

    kb.harvest(_phase(json_attempt()), profile, ws)
    kb.ledger().append(
        [
            rec(
                id="x1",
                project="p9",
                resolution="blocked",
                resolver_id="artifact_not_found",
                symbol="com.co:gone",
                outcome="blocked",
            )
        ]
    )
    run = CliRunner()

    def cli(*args):
        r = run.invoke(kb_cli, ["--kb-dir", str(kb.root), *args], catch_exceptions=False)
        assert r.exit_code == 0, r.output
        return r.output

    out = cli("ledger")
    assert "JsonComponent" in out and "llm_patch" in out and "com.co:gone" in out
    out = cli("ledger", "--resolution", "blocked", "--json")
    assert [json.loads(ln)["id"] for ln in out.splitlines()] == ["x1"]
    assert cli("ledger", "--symbol", "^nothing$") == "no ledger records\n"
    assert len(cli("ledger", "--limit", "1").splitlines()) == 1
    assert "annotation-replace,type-rename" in cli("ledger", "--label", "type-rename")
    out = cli("symbols")
    assert "JsonComponent" in out and "recipe=com.company.kb.JsonComponentToJacksonComponent" in out
    assert "JsonComponent" in json.loads(cli("symbols", "--json"))
    out = cli("learning")
    assert "blocking most projects" in out and "com.co:gone" in out
    md = cli("learning", "--md")
    assert md.startswith("# KB learning report") and "## Knowledge base" in md
    assert json.loads(cli("learning", "--json"))["records"] == 2
    assert run.invoke(kb_cli, ["--kb-dir", str(kb.root), "ledger", "--symbol", "("]).exit_code != 0
