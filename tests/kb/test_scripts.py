"""v2 fix-script KB (sbmig.kb.scripts): versions per case, dedupe by sha1, lineage, credit, the reuse order (best /
pin / rejected case), latest, quarantine of a script whose code holds a secret, corrupt metas, the KB lock and the
audit lines; FileKnowledgeBase.scripts / record_review; stats, export / import."""
from __future__ import annotations

import json
import logging
import zipfile

import pytest

from sbmig.core.interfaces import ScriptKnowledgeLike, ScriptStoreLike
from sbmig.core.models import (
    CaseAction,
    CaseType,
    ReviewIssue,
    ScriptMeta,
    ScriptRunResult,
    StructureReview,
    utcnow,
)
from sbmig.kb import FileKnowledgeBase, ScriptStore, parse_script_resolver, script_resolver
from sbmig.kb.catalog import case_id_for
from sbmig.kb.lock import holding
from sbmig.kb.scripts import code_sha1, lineage_text, rank_proven

from .helpers import EC_L2

CID = case_id_for(EC_L2)
FIX_A = "def fix(p):\n    p.replace('src/A.java', 'clientIp', 'remoteClientIp')\n"
FIX_B = "def fix(p):\n    for f in p.files('**/*.java'):\n        p.replace(f, 'clientIp(', 'remoteClientIp(')\n"
FIX_C = "def fix(p):\n    p.sub('src/A.java', r'\\bclientIp\\b', 'remoteClientIp')\n"
SECRET_FIX = ("def fix(p):\n    p.write('src/main/resources/application.properties', "
              "'spring.datasource.password=hunter2')\n")


@pytest.fixture()
def store(v2_kb_cfg) -> ScriptStore:
    return FileKnowledgeBase(v2_kb_cfg).scripts


def audit_lines(root) -> list[dict]:
    p = root / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# --------------------------------------------------------------------------- helpers / protocol
def test_protocols_and_resolver_helpers(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    assert isinstance(kb.scripts, ScriptStoreLike) and isinstance(kb, ScriptKnowledgeLike)
    assert kb.scripts is kb.scripts and kb.scripts.scripts_dir == kb.root / "scripts"
    assert script_resolver("abc1234567", 3) == "script:abc1234567:v3"
    assert parse_script_resolver("script:abc1234567:v3") == ("abc1234567", 3)
    assert parse_script_resolver("fake-model") is None and parse_script_resolver("script:../x:v1") is None
    assert parse_script_resolver(None) is None


# --------------------------------------------------------------------------- add / dedupe / lineage
def test_add_versions_dedupe_and_lineage(store):
    m1 = store.add(CID, EC_L2, FIX_A, analysis="rename the call", model="gpt", run_id="r1", project="alpha")
    assert (m1.version, m1.parent, m1.status, m1.source, m1.run_id, m1.project, m1.model) == (
        1, None, "active", "llm", "r1", "alpha", "gpt")
    assert m1.sha1 == code_sha1(FIX_A) and m1.signature_generic == EC_L2 and m1.analysis == "rename the call"
    files = sorted(p.name for p in store.case_dir(CID).iterdir())
    assert files == ["v1.meta.json", "v1.py"]
    # the identical script again: the existing meta, nothing written
    again = store.add(CID, EC_L2, FIX_A, run_id="r2", project="beta")
    assert again.version == 1 and again.run_id == "r1"
    assert sorted(p.name for p in store.case_dir(CID).iterdir()) == files
    m2 = store.add(CID, EC_L2, FIX_B, parent=1, run_id="r2")
    m3 = store.add(CID, EC_L2, FIX_C, parent=2, run_id="r2")
    m4 = store.add(CID, EC_L2, FIX_C + "\n# again\n", parent=0)        # 0 = an unstored base: no parent
    assert [m.version for m in (m2, m3, m4)] == [2, 3, 4] and (m2.parent, m3.parent, m4.parent) == (1, 2, None)
    assert [m.version for m in store.versions(CID)] == [1, 2, 3, 4]
    assert lineage_text(store.lineage(CID, 3)) == "v1 → v2 → v3" and lineage_text(store.lineage(CID, 4)) == "v4"
    meta, code = store.get(CID, 2)
    assert meta == m2 and code == FIX_B
    assert store.get(CID, 9) is None and store.get("nope", 1) is None
    assert store.case_ids() == [CID]
    # a parent that is not stored is kept and noted
    m5 = store.add(CID, EC_L2, FIX_A + "# 5\n", parent=42)
    assert m5.parent == 42 and "parent v42 is not in the store" in m5.notes


def test_code_is_stored_as_exact_bytes(store):
    crlf = "def fix(p):\r\n    p.log('é — 한글')\r\n"
    m = store.add(CID, EC_L2, crlf)
    raw = store.code_path(CID, 1).read_bytes()
    assert raw == crlf.encode("utf-8") and b"\r\n" in raw
    assert m.sha1 == code_sha1(crlf) and store.get(CID, 1)[1] == crlf


def test_add_rejects_bad_arguments(store):
    with pytest.raises(ValueError):
        store.add("../evil", EC_L2, FIX_A)
    with pytest.raises(ValueError):
        store.add("a/b", EC_L2, FIX_A)
    with pytest.raises(ValueError):
        store.add(CID, EC_L2, "   \n")
    assert not store.scripts_dir.exists()


def test_add_write_failure_gives_an_unstored_meta(store, monkeypatch, caplog):
    import sbmig.kb.scripts as mod

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_bytes", boom)
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        m = store.add(CID, EC_L2, FIX_A, run_id="r1")
    assert m.version == 0 and any("not stored" in n for n in m.notes) and "disk full" in caplog.text


# --------------------------------------------------------------------------- credit / best / latest
def test_credit_outcomes(store):
    store.add(CID, EC_L2, FIX_A)
    for outcome, project in (("improved", "alpha"), ("build_ok", "beta"), ("build_ok", "alpha"), ("partial", "x"),
                             ("no_change", "y"), ("regressed", "y"), ("reverted", "y"), ("apply_failed", "y"),
                             ("blocked", "y"), ("inconclusive", "z"), ("pending", "z")):
        store.credit(CID, 1, outcome, project=project, run_id=f"run-{outcome}")
    m = store.get_meta(CID, 1)
    assert (m.successes, m.partials, m.failures) == (3, 1, 5)
    assert m.success_projects == ["alpha", "beta"]
    assert m.last_outcome == "pending" and m.last_run_id == "run-pending" and m.last_at is not None
    # unknown versions / cases are ignored, never raised
    store.credit(CID, 7, "improved")
    store.credit("../x", 1, "improved")
    store.credit(CID, 0, "improved")
    assert store.get_meta(CID, 1).successes == 3


def test_best_order_and_latest(store):
    for code in (FIX_A, FIX_B, FIX_C, FIX_A + "# 4\n", FIX_A + "# 5\n"):
        store.add(CID, EC_L2, code)
    assert store.best(CID) == [] and store.latest(CID)[0].version == 5      # nothing proven yet
    # v1: 2 projects, 1 failure; v2: 1 project, 3 successes; v3: 2 projects, 0 failures; v4: retired with 3 projects;
    # v5: quarantined-free but only failures
    for p in ("a", "b"):
        store.credit(CID, 1, "improved", project=p)
    store.credit(CID, 1, "no_change")
    for _ in range(3):
        store.credit(CID, 2, "improved", project="a")
    for p in ("a", "b"):
        store.credit(CID, 3, "build_ok", project=p)
    for p in ("a", "b", "c"):
        store.credit(CID, 4, "improved", project=p)
    store.credit(CID, 5, "no_change")
    store.retire(CID, 4, who="tester", reason="breaks the web.xml")
    best = store.best(CID, limit=10)
    assert [m.version for m, _ in best] == [3, 1, 2]            # projects, then successes − failures, then newest
    assert best[0][1] == FIX_C and [m.version for m, _ in store.best(CID)] == [3]
    assert store.best(CID, limit=0) == []
    assert [m.version for m in rank_proven(store.versions(CID))] == [3, 1, 2]
    # equal record → the newest first
    store.credit(CID, 1, "improved", project="a")                # v1: 2 projects, 3 ok − 1 failed = 2 = v3
    assert [m.version for m, _ in store.best(CID, limit=2)] == [3, 1]
    assert store.latest(CID)[0].version == 5
    store.retire(CID, 5)
    assert store.latest(CID)[0].version == 3                      # newest ACTIVE (v4 retired, v5 retired)


def test_human_pin_comes_first_and_a_rejected_case_gives_nothing(v2_kb_cfg):
    from .test_catalog import rec

    kb = FileKnowledgeBase(v2_kb_cfg)
    st = kb.scripts
    for code in (FIX_A, FIX_B, FIX_C):
        st.add(CID, EC_L2, code)
    st.credit(CID, 1, "improved", project="alpha")
    kb.append_error_cases([rec("e1")], [])                       # the catalog case of EC_L2
    cat = kb.catalog()
    assert [m.version for m, _ in st.best(CID, limit=3)] == [1]
    cat.set_human(CID, action=CaseAction(kind="script", script_version=3), status="active")
    assert st.pinned_version(CID) == 3
    assert [m.version for m, _ in st.best(CID, limit=3)] == [3, 1]          # the pin first, without a success
    assert [m.version for m, _ in st.best(CID)] == [3]
    st.retire(CID, 3)                                             # retiring wins over the pin (the later decision)
    assert [m.version for m in st.ranked(CID)] == [1]
    # a pin may only name a stored, not quarantined version of the case
    with pytest.raises(ValueError, match="no stored fix script v9"):
        cat.set_human(CID, action=CaseAction(kind="script", script_version=9))
    with pytest.raises(ValueError, match="needs a version"):
        cat.set_human(CID, action=CaseAction(kind="script"))
    q = st.add(CID, EC_L2, SECRET_FIX)
    with pytest.raises(ValueError, match="quarantined"):
        cat.set_human(CID, action=CaseAction(kind="script", script_version=q.version))
    cat.set_human(CID, status="rejected")                         # switched off by a person: nothing to reuse
    assert st.best(CID, limit=3) == [] and st.latest(CID)[0].version == 2


def test_quarantine_when_the_code_holds_a_secret(store, caplog):
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        m = store.add(CID, EC_L2, SECRET_FIX, analysis="set the password: password=hunter2")
    assert m.status == "quarantined" and any(n.startswith("quarantined:") for n in m.notes)
    stored = store.code_path(CID, m.version).read_text(encoding="utf-8")
    assert "hunter2" not in stored and "***" in stored and m.sha1 == code_sha1(stored)
    assert "hunter2" not in m.analysis and "hunter2" not in store.meta_path(CID, 1).read_text(encoding="utf-8")
    assert "quarantined" in caplog.text
    assert store.best(CID) == [] and store.latest(CID) is None
    store.credit(CID, 1, "improved", project="alpha")              # counted, still never selected
    assert store.best(CID) == [] and store.get_meta(CID, 1).successes == 1
    assert store.add(CID, EC_L2, SECRET_FIX).version == 1           # the same secret code: the same version
    with pytest.raises(ValueError, match="quarantined"):
        store.activate(CID, 1)
    with pytest.raises(ValueError, match="quarantined"):
        store.retire(CID, 1)
    assert store.add(CID, EC_L2, FIX_A).status == "active"
    assert store.counts()["scripts_quarantined"] == 1


# --------------------------------------------------------------------------- robustness
def test_corrupt_and_missing_meta_files_are_skipped(store, caplog):
    for code in (FIX_A, FIX_B, FIX_C):
        store.add(CID, EC_L2, code)
    store.credit(CID, 1, "improved", project="a")
    store.meta_path(CID, 1).write_text("{not json", encoding="utf-8")
    store.meta_path(CID, 2).unlink()                               # an orphan v2.py (crash between the writes)
    bad = json.loads(store.meta_path(CID, 3).read_text(encoding="utf-8"))
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        assert [m.version for m in store.versions(CID)] == [3]
        assert store.best(CID) == [] and store.get(CID, 1) is None
    assert "unreadable" in caplog.text and "no meta file" in caplog.text
    # warned once per file state
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        store.versions(CID)
    assert caplog.text == ""
    # numbers are never reused: the next version is above every file on disk
    assert store.add(CID, EC_L2, FIX_A + "# new\n").version == 4
    # a meta naming another version is skipped too
    bad["version"] = 7
    store.meta_path(CID, 3).write_text(json.dumps(bad), encoding="utf-8")
    assert [m.version for m in store.versions(CID)] == [4]
    store.code_path(CID, 4).unlink()
    assert store.latest(CID) is None and store.best(CID) == []
    problems = store.check()
    assert any("v1.meta.json: does not parse" in p for p in problems)
    assert any("v2.py: no meta file" in p for p in problems)
    assert any("v3.meta.json: names case" in p for p in problems)
    assert any("v4.py: missing" in p for p in problems)
    store.credit(CID, 1, "improved")                               # never raises


def test_check_reports_hand_edited_code_and_missing_parents(store):
    store.add(CID, EC_L2, FIX_A)
    store.add(CID, EC_L2, FIX_B, parent=1)
    assert store.check() == []
    store.code_path(CID, 1).write_bytes(b"def fix(p):\n    pass\n")
    store.meta_path(CID, 1).rename(store.meta_path(CID, 1).with_name("x.json"))
    store.code_path(CID, 1).unlink()
    (store.scripts_dir / "not a case").mkdir()
    problems = store.check()
    assert any("parent v1 does not exist" in p for p in problems) and any("not a case id" in p for p in problems)
    store.code_path(CID, 2).write_bytes(b"def fix(p):\n    pass\n")
    assert any("sha1 differs" in p for p in store.check())


def test_writes_hold_the_kb_lock(store, monkeypatch):
    import sbmig.kb.scripts as mod

    seen: list[bool] = []
    real = mod._write_bytes
    monkeypatch.setattr(mod, "_write_bytes", lambda path, data: seen.append(holding(store.root)) or real(path, data))
    store.add(CID, EC_L2, FIX_A)
    store.credit(CID, 1, "improved", project="a")
    store.retire(CID, 1)
    store.activate(CID, 1)
    assert len(seen) == 5 and all(seen)                            # code + meta, then 3 meta rewrites
    assert not holding(store.root)
    assert not (store.root / ".lock").exists()


def test_audit_lines(store, monkeypatch):
    import sbmig.kb.catalog as catalog_mod

    monkeypatch.setattr(catalog_mod, "_who", lambda: "jdoe")
    store.add(CID, EC_L2, FIX_A, run_id="r1", project="alpha", model="gpt")
    store.add(CID, EC_L2, FIX_A)                                   # dedupe: no line
    store.credit(CID, 1, "improved", project="alpha", run_id="r1")
    store.retire(CID, 1, reason="wrong file", command="sbmig kb scripts retire")
    store.retire(CID, 1)                                           # already retired: no line
    store.activate(CID, 1)
    store.add(CID, EC_L2, SECRET_FIX)
    lines = [a for a in audit_lines(store.root) if a["event"] == "script"]
    assert [a["action"] for a in lines] == ["created", "credited", "retired", "activated", "quarantined"]
    created, credited, retired, activated, quarantined = lines
    assert created["who"] == "sbmig" and created["case_id"] == CID and created["version"] == 1
    assert created["signature_generic"] == EC_L2 and created["run_id"] == "r1" and created["model"] == "gpt"
    assert created["schema_version"] == 3 and created["agent_version"] and created["at"]
    assert credited["outcome"] == "improved" and credited["after"]["successes"] == 1
    assert credited["after"]["success_projects"] == 1
    assert retired["who"] == "jdoe" and retired["reason"] == "wrong file" and retired["before"] == {"status": "active"}
    assert retired["command"] == "sbmig kb scripts retire" and retired["after"]["status"] == "retired"
    assert activated["after"]["status"] == "active" and quarantined["version"] == 2
    assert "hunter2" not in (store.root / "audit.jsonl").read_text(encoding="utf-8")
    with pytest.raises(KeyError):
        store.retire(CID, 9)


def test_counts(store):
    assert store.counts() == {"script_cases": 0, "script_versions": 0, "scripts_active": 0, "scripts_retired": 0,
                              "scripts_quarantined": 0, "scripts_proven": 0}
    store.add(CID, EC_L2, FIX_A)
    store.add(CID, EC_L2, FIX_B)
    store.add("0123456789", "compile|x|y", FIX_A)
    store.credit(CID, 2, "improved", project="p")
    store.retire(CID, 1)
    assert store.counts() == {"script_cases": 2, "script_versions": 3, "scripts_active": 2, "scripts_retired": 1,
                              "scripts_quarantined": 0, "scripts_proven": 1}


# --------------------------------------------------------------------------- record_review
def _review(**kw) -> StructureReview:
    data = dict(run_id="r1", project="alpha", case_type=CaseType.SPRING_LEGACY, model="gpt", verdict="issues",
                issues=[ReviewIssue(severity="critical", file="alpha-boot/pom.xml", problem="no parent; "
                                    "db.password=hunter2 in the pom", fix_hint="set <password>s3cret</password>"),
                        ReviewIssue(severity="warning", problem="duplicate dependency")],
                analysis="the boot module lacks boot-jar; spring.datasource.password=hunter2",
                script="def fix(p):\n    password = 'hunter2'\n",
                script_result=ScriptRunResult(status="ok", changed_files=["alpha-boot/pom.xml"],
                                              log=["token=abc123def"], error=""),
                commit="abc", note="script applied")
    data.update(kw)
    return StructureReview(**data)


def test_record_review_one_redacted_line_per_run(v2_kb_cfg):
    kb = FileKnowledgeBase(v2_kb_cfg)
    kb.record_review(_review())
    kb.record_review(_review(note="again"))                     # same run: not written twice
    kb.record_review(_review(run_id="r2", verdict="ok", issues=[], script="", script_result=None))
    text = kb.reviews_path.read_text(encoding="utf-8")
    assert len(text.splitlines()) == 2
    for secret in ("hunter2", "s3cret", "abc123def"):
        assert secret not in text
    got = list(kb.iter_reviews())
    assert [r.id for r in got] == ["r1:review", "r2:review"] and got[0].schema_version == 3
    r = got[0]
    assert r.verdict == "issues" and [i.severity for i in r.issues] == ["critical", "warning"]
    assert r.issues[0].file == "alpha-boot/pom.xml" and "***" in r.issues[0].problem
    assert r.script.startswith("def fix(p):") and "***" in r.script and r.script_result.log == ["token=***"]
    assert r.commit == "abc" and r.note == "script applied" and r.case_type == CaseType.SPRING_LEGACY
    assert kb.stats()["reviews"] == 2


def test_record_review_never_raises(v2_kb_cfg, monkeypatch, caplog):
    import sbmig.kb.store as store_mod

    kb = FileKnowledgeBase(v2_kb_cfg)
    monkeypatch.setattr(store_mod, "append_lines", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with caplog.at_level(logging.WARNING, logger="sbmig.kb"):
        kb.record_review(_review(id="", run_id=""))
    assert "record_review failed" in caplog.text


# --------------------------------------------------------------------------- stats / export / import
def test_stats_export_and_import(v2_kb_cfg, tmp_path):
    kb = FileKnowledgeBase(v2_kb_cfg)
    kb.scripts.add(CID, EC_L2, FIX_A, run_id="r1", project="alpha")
    kb.scripts.add(CID, EC_L2, FIX_B, parent=1)
    kb.scripts.credit(CID, 1, "improved", project="alpha")
    kb.record_review(_review())
    st = kb.stats()
    assert (st["script_cases"], st["script_versions"], st["scripts_proven"], st["reviews"]) == (1, 2, 1, 1)
    out = kb.export_zip(tmp_path / "kb.zip")
    names = zipfile.ZipFile(out).namelist()
    assert f"scripts/{CID}/v1.py" in names and f"scripts/{CID}/v2.meta.json" in names and "reviews.jsonl" in names

    # into an empty KB: as they are
    empty = FileKnowledgeBase(v2_kb_cfg, kb_dir=tmp_path / "empty")
    c = empty.import_zip(out)
    assert c["script_versions_added"] == 2 and c["reviews_added"] == 1
    assert [m.version for m in empty.scripts.versions(CID)] == [1, 2] and empty.scripts.get(CID, 1)[1] == FIX_A
    assert empty.import_zip(out)["script_versions_added"] == 0 and empty.import_zip(out)["reviews_added"] == 0

    # into a KB with its own v1: same code merges the counts, other code becomes the next version, parents mapped
    local = FileKnowledgeBase(v2_kb_cfg, kb_dir=tmp_path / "local")
    local.scripts.add(CID, EC_L2, FIX_C, project="gamma")
    local.scripts.add(CID, EC_L2, FIX_A, project="delta")             # = their v1
    local.scripts.credit(CID, 2, "improved", project="delta")
    c = local.import_zip(out)
    assert c["script_versions_added"] == 1 and c["script_versions_merged"] == 1
    metas = {m.version: m for m in local.scripts.versions(CID)}
    assert sorted(metas) == [1, 2, 3]
    assert metas[2].sha1 == code_sha1(FIX_A) and metas[2].successes == 1
    assert sorted(metas[2].success_projects) == ["alpha", "delta"]
    assert metas[3].sha1 == code_sha1(FIX_B) and metas[3].parent == 2 and "imported (v2 there)" in metas[3].notes
    assert local.scripts.check() == []
    again = local.import_zip(out)
    assert again["script_versions_added"] == 0 and again["script_versions_merged"] == 0

    # --replace: the zip's store as it is
    local.import_zip(out, merge=False)
    assert [m.sha1 for m in local.scripts.versions(CID)] == [code_sha1(FIX_A), code_sha1(FIX_B)]
    assert len(list(local.iter_reviews())) == 1


def test_meta_round_trip_fields(store):
    m = store.add(CID, EC_L2, FIX_A, source="human")
    raw = json.loads(store.meta_path(CID, 1).read_text(encoding="utf-8"))
    assert raw["case_id"] == CID and raw["version"] == 1 and raw["source"] == "human" and raw["schema_version"] == 3
    assert ScriptMeta.model_validate(raw) == m
    assert m.created_at <= utcnow()
