"""Source-line callee resolution for javac errors that never name the callee (incompatible types / cannot access)."""
from pathlib import Path

from sbmig.build import classpath as cp
from sbmig.core.models import BuildError, ErrorKind

SRC = """package com.example.members.web;

import com.company.legacy.web.LegacyRequestHelper;
import com.company.legacy.util.*;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.GetMapping;

public class MemberController {
    private final LegacyRequestHelper helper = null;

    public String ip(HttpServletRequest request) {
        LegacyRequestHelper h = new LegacyRequestHelper(request);
        String a = LegacyRequestHelper.clientIp(request);
        String b = Other.thing(request);
        return helper.describe(request) + a + b + h;
    }
}
"""


def _ws(tmp_path, make_ws):
    src = tmp_path / "p"; (src / "src/main/java/com/example/members/web").mkdir(parents=True)
    (src / "pom.xml").write_text("<project/>")
    (src / "src/main/java/com/example/members/web/MemberController.java").write_text(SRC)
    return make_ws(src)


def test_callee_candidates_constructor_static_wildcard_and_instance(tmp_path, make_ws):
    ws = _ws(tmp_path, make_ws)
    f = "src/main/java/com/example/members/web/MemberController.java"
    assert cp.callee_candidates(ws, f, 12) == ["com.company.legacy.web.LegacyRequestHelper"]
    assert cp.callee_candidates(ws, f, 13) == ["com.company.legacy.web.LegacyRequestHelper"]
    # wildcard import first, then the same-package candidate (locate_type_owner skips in-project hits)
    assert cp.callee_candidates(ws, f, 14) == ["com.company.legacy.util.Other", "com.example.members.web.Other"]
    assert cp.callee_candidates(ws, f, 15) == ["com.company.legacy.web.LegacyRequestHelper"]  # field type
    assert cp.callee_candidates(ws, f, 1) == [] and cp.callee_candidates(ws, f, 999) == []
    assert cp.callee_candidates(ws, "nope.java", 3) == []


def test_locate_type_owner_prefers_callee_over_api_type(tmp_path, make_ws, cfg, monkeypatch):
    ws = _ws(tmp_path, make_ws)
    f = "src/main/java/com/example/members/web/MemberController.java"
    seen = []
    def fake_locate(ws_, cfg_, fqcn, module=None):
        seen.append(fqcn)
        if fqcn == "com.company.legacy.web.LegacyRequestHelper":
            from sbmig.core.models import GAV
            return cp.ClassLocation(fqcn=fqcn, artifact=GAV(group_id="com.company.legacy", artifact_id="legacy-web-lib",
                                                            version="1.0.0"), jar="/x/legacy-web-lib-1.0.0.jar",
                                    in_project=False)
        return None
    monkeypatch.setattr(cp, "locate_class", fake_locate)
    err = BuildError(kind=ErrorKind.COMPILE, file=f, line=12, module="member-api",
                     message="incompatible types: jakarta.servlet.http.HttpServletRequest cannot be converted to "
                             "javax.servlet.http.HttpServletRequest",
                     raw="[ERROR] /x/MemberController.java:[12,45] incompatible types: jakarta.servlet.http.HttpServletRequest "
                         "cannot be converted to javax.servlet.http.HttpServletRequest")
    loc = cp.locate_type_owner(ws, cfg, err)
    assert loc is not None and loc.artifact.artifact_id == "legacy-web-lib"
    assert seen[0] == "com.company.legacy.web.LegacyRequestHelper"       # callee tried before the javax API type
    err2 = BuildError(kind=ErrorKind.COMPILE, file=f, line=13, module="member-api",
                      message="cannot access javax.servlet.http.HttpServletRequest",
                      raw="[ERROR] /x/MemberController.java:[13,38] cannot access javax.servlet.http.HttpServletRequest\n"
                          "[ERROR]   class file for javax.servlet.http.HttpServletRequest not found")
    loc2 = cp.locate_type_owner(ws, cfg, err2)
    assert loc2 is not None and loc2.artifact.artifact_id == "legacy-web-lib"
