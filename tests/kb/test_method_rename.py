"""Round 5: `method-rename` fix label → ChangeMethodName recipe candidate, owner resolved via `symbol_owner`."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sbmig.build import signatures
from sbmig.core.models import BuildError, ErrorKind, FileChange, KBCase, Phase, PhaseResult
from sbmig.core.workspace import Workspace
from sbmig.kb import sanitize_case
from sbmig.kb.harvest import resolve_symbol_owner
from sbmig.kb.labels import compile_recipe, label_changes
from sbmig.kb.recipes import validate_recipe_yaml

from .helpers import FakeLLM, make_attempt

OWNER = "com.company.legacy.web.LegacyRequestHelper"
RECIPE_ID = "com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp"
CONTRACT_RECIPE_LIST = [
    {
        "org.openrewrite.java.ChangeMethodName": {
            "methodPattern": "com.company.legacy.web.LegacyRequestHelper clientIp(..)",
            "newMethodName": "remoteClientIp",
            "matchOverrides": True,
        }
    }
]
CTRL = "src/main/java/com/example/web/IpController.java"
FILTER = "src/main/java/com/example/web/AuditFilter.java"
L1 = "compile|cannot find symbol|method clientIp(HttpServletRequest)|class LegacyRequestHelper"
L2 = "compile|cannot_find_symbol|clientIp"

CTRL_SRC = """package com.example.web;

import com.company.legacy.web.LegacyRequestHelper;
import javax.servlet.http.HttpServletRequest;

public class IpController {
    public String ip(HttpServletRequest request) {
        return LegacyRequestHelper.remoteClientIp(request);
    }
}
"""
FILTER_SRC = """package com.example.web;

import com.company.legacy.web.LegacyRequestHelper;
import javax.servlet.http.HttpServletRequest;

public class AuditFilter {
    void audit(HttpServletRequest req) {
        String ip = LegacyRequestHelper.remoteClientIp(req);
    }
}
"""


@pytest.fixture()
def legacy_external(monkeypatch):
    """`com.company.legacy` is an in-house library group (MavenRunner registers these at runtime)."""
    monkeypatch.setattr(signatures, "_EXTRA_EXTERNAL_PREFIXES", {"com.company.legacy."})


def mod(search: str, replace: str, path: str = CTRL) -> FileChange:
    return FileChange(path=path, op="modify", search=search, replace=replace)


def ip_err(file: str = CTRL, location: str = "class LegacyRequestHelper") -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", file=file, line=8,
                      symbol="method clientIp(HttpServletRequest)", location=location,
                      signature=L1, signature_generic=L2)


def scenario_changes() -> list[FileChange]:
    return [
        mod("        return LegacyRequestHelper.clientIp(request);",
            "        return LegacyRequestHelper.remoteClientIp(request);", CTRL),
        mod("        String ip = LegacyRequestHelper.clientIp(req);",
            "        String ip = LegacyRequestHelper.remoteClientIp(req);", FILTER),
    ]


def case_of(changes, errors=None, symbol_owner: str = "") -> KBCase:
    res = label_changes(changes, errors or [])
    return KBCase(id="20261018-abc", changes=changes, errors=errors or [], fix_labels=res.labels,
                  replacement=res.replacement, symbol=res.symbol, symbol_owner=symbol_owner,
                  signatures_generic=[L2])


def _doc(text) -> dict:
    assert text is not None
    ok, errors, doc = validate_recipe_yaml(text)
    assert ok, errors
    return doc


# --------------------------------------------------------------------------- labels
def test_method_reference_rename_is_method_rename():
    ch = mod("    list.forEach(helper::oldName);", "    list.forEach(helper::newName);")
    e = BuildError(kind=ErrorKind.COMPILE, message="invalid method reference", symbol="method oldName(String)",
                   location="class Helper")
    r = label_changes([ch], [e])
    assert r.labels == ["method-rename"]
    assert r.details["method_renames"] == [{"owner": "Helper", "old": "oldName", "new": "newName"}]
    assert r.replacement == "Helper.oldName → newName"
    # a single `:` (ternary) before the token is not a method reference
    assert label_changes([mod("x = c ? a : old;", "x = c ? a : fresh;")]).labels != ["method-rename"]


# --------------------------------------------------------------------------- compile_recipe
def test_compile_method_rename_contract_recipe(legacy_external):
    changes = scenario_changes()
    errors = [ip_err(CTRL), ip_err(FILTER)]
    case = case_of(changes, errors, symbol_owner=OWNER)
    assert case.fix_labels == ["method-rename"]
    doc = _doc(compile_recipe(case))
    assert doc["name"] == RECIPE_ID
    assert doc["recipeList"] == CONTRACT_RECIPE_LIST
    assert doc["displayName"] == f"{OWNER}.clientIp(..) -> remoteClientIp"
    assert "no LLM" in doc["description"]
    # without symbol_owner the simple javac owner cannot be resolved
    assert compile_recipe(case_of(changes, errors)) is None


def test_compile_method_rename_owner_fqcn_from_error_location(legacy_external):
    errors = [ip_err(location=f"class {OWNER}")]
    doc = _doc(compile_recipe(case_of(scenario_changes()[:1], errors)))
    assert doc["recipeList"] == CONTRACT_RECIPE_LIST
    # instance call: `variable v of type X`
    ch = mod("    helper.clientIp(request);", "    helper.remoteClientIp(request);")
    e = ip_err(location=f"variable helper of type {OWNER}")
    assert _doc(compile_recipe(case_of([ch], [e])))["recipeList"] == CONTRACT_RECIPE_LIST


def test_compile_method_rename_symbol_owner_must_match_simple_name(legacy_external):
    case = case_of(scenario_changes(), [ip_err()], symbol_owner="com.company.legacy.web.OtherHelper")
    assert compile_recipe(case) is None
    # no owner at all in the errors: symbol_owner alone is enough
    assert _doc(compile_recipe(case_of(scenario_changes(), [], symbol_owner=OWNER)))["name"] == RECIPE_ID


def test_compile_method_rename_disagreeing_renames_none(legacy_external):
    changes = [
        scenario_changes()[0],
        mod("        String ip = LegacyRequestHelper.clientIp(req);",
            "        String ip = LegacyRequestHelper.clientAddress(req);", FILTER),
    ]
    case = case_of(changes, [ip_err()], symbol_owner=OWNER)
    assert case.fix_labels == ["method-rename"]
    assert compile_recipe(case) is None


def test_compile_method_rename_project_owned_or_unknown_owner_none(legacy_external):
    own = "com.example.web.RequestUtil"
    case = case_of(scenario_changes(), [ip_err(location=f"class {own}")])
    assert compile_recipe(case) is None                             # project-owned FQCN in the location
    assert compile_recipe(case_of(scenario_changes(), [ip_err(location="class RequestUtil")],
                                  symbol_owner=own)) is None        # project-owned symbol_owner
    assert compile_recipe(case_of(scenario_changes())) is None      # no owner anywhere


def test_compile_method_rename_mixed_labels_none(legacy_external):
    changes = [*scenario_changes(), mod("a();\nb();", "a();", "src/main/java/com/example/web/Other.java")]
    case = case_of(changes, [ip_err()], symbol_owner=OWNER)
    assert case.fix_labels == ["method-rename", "remove-code"]
    assert compile_recipe(case) is None


# --------------------------------------------------------------------------- harvest
@pytest.fixture()
def legacy_ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "legacy"
    (root / "src/main/java/com/example/web").mkdir(parents=True)
    (root / CTRL).write_text(CTRL_SRC)
    (root / FILTER).write_text(FILTER_SRC)
    (root / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n")
    return Workspace(root, tmp_path / "legacy-run", "t-run")


def test_resolve_symbol_owner_explicit_import(legacy_ws):
    assert resolve_symbol_owner(scenario_changes(), [ip_err()], legacy_ws) == OWNER
    # an owner already qualified in the javac location is kept as is
    assert resolve_symbol_owner(scenario_changes(), [ip_err(location=f"class {OWNER}")], None) == OWNER
    # nothing to resolve
    assert resolve_symbol_owner([], [], legacy_ws) == ""


def test_resolve_symbol_owner_static_import_and_same_package(legacy_ws):
    ch = FileChange(path="src/main/java/com/example/web/X.java", op="modify",
                    search="import static com.company.legacy.web.LegacyRequestHelper.*;\n"
                           "  LegacyRequestHelper.clientIp(r);",
                    replace="import static com.company.legacy.web.LegacyRequestHelper.*;\n"
                            "  LegacyRequestHelper.remoteClientIp(r);")
    assert resolve_symbol_owner([ch], [ip_err()], None) == OWNER
    same_pkg = FileChange(path="src/main/java/com/example/web/Y.java", op="modify",
                          search="package com.example.web;\n  Helper.clientIp(r);",
                          replace="package com.example.web;\n  Helper.remoteClientIp(r);")
    assert resolve_symbol_owner([same_pkg], [ip_err(location="class Helper")], None) == "com.example.web.Helper"


def test_sanitize_case_fills_symbol_owner(legacy_ws, profile):
    attempt = make_attempt(changes=scenario_changes(), errors=[ip_err(CTRL), ip_err(FILTER)])
    case = sanitize_case(attempt, profile, legacy_ws, Phase.SPRING)
    assert case.symbol == "clientIp" and case.fix_labels == ["method-rename"]
    assert case.symbol_owner == OWNER


def test_harvest_method_rename_compiles_change_method_name_without_llm(kb, legacy_ws, profile,
                                                                       legacy_external):
    llm = FakeLLM("NONE")
    attempt = make_attempt(changes=scenario_changes(), errors=[ip_err(CTRL), ip_err(FILTER)],
                           analysis="clientIp was renamed to remoteClientIp in legacy-web-lib 2.0")
    res = kb.harvest(PhaseResult(phase=Phase.SPRING, attempts=[attempt]), profile, legacy_ws, llm=llm)
    assert llm.calls == []  # deterministic candidate: no LLM call
    assert res.candidates_created == [RECIPE_ID]
    case = kb.get_case(res.cases_created[0])
    assert case.symbol_owner == OWNER and case.recipe_candidate_id == RECIPE_ID
    text = (kb.root / "recipes" / "candidates" / f"{RECIPE_ID}.yml").read_text()
    assert yaml.safe_load(text)["recipeList"] == CONTRACT_RECIPE_LIST
