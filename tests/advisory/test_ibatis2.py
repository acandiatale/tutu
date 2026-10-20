"""Advisory `ibatis2` (round 9): iBATIS 2 / SqlMapClient code — Spring 4.0 removed org.springframework.orm.ibatis."""
from __future__ import annotations

from pathlib import Path

from sbmig.advisory import evaluate_advisories, followups_for, hints_for, load_advisories, verify_advisories
from sbmig.analyze import PathView, detect_libraries, parse_poms, scan_sources
from sbmig.core.models import BuildError, CaseType, ErrorKind, LibraryUsage
from tests.advisory.helpers import profile
from tests.analyze.helpers import dep, pom, write, write_java

DAO_IMPORTS = ("com.ibatis.sqlmap.client.SqlMapClient",
               "org.springframework.orm.ibatis.support.SqlMapClientDaoSupport")


def _finding(cfg, evidence=("import:org.springframework.orm.ibatis",)):
    p = profile(libs=(LibraryUsage(name="ibatis2", evidence=list(evidence)),), case=CaseType.SPRING_LEGACY)
    [f] = [f for f in evaluate_advisories(p, cfg) if f.advisory_id == "ibatis2"]
    return f


def test_catalog_entry_shape(cfg):
    e = load_advisories(cfg).get("ibatis2")
    assert e is not None and e.library == "ibatis2" and e.status == "incompatible" and e.policy == "guide"
    assert e.recipes == [] and e.notes == "knowledge/notes/ibatis2.md"
    assert e.docs == ["https://mybatis.org/mybatis-3/", "https://mybatis.org/spring/"]
    assert "org.mybatis:mybatis-spring" in e.replacement
    for s in ("SqlSessionTemplate", "#{name}", "${name}", "selectList", "selectOne", "<configuration>"):
        assert s in e.followup, s


def test_detected_end_to_end_from_pom_and_imports(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("legacy", deps=(dep("org.apache.ibatis", "ibatis-sqlmap", "2.3.4.726"),)))
    write_java(tmp_path, "", "com.acme.dao", "OrderDao", imports=DAO_IMPORTS)
    ws = PathView(tmp_path)
    poms, _ = parse_poms(ws)
    libs = detect_libraries(poms, scan_sources(ws))
    assert [lib.name for lib in libs if "batis" in lib.name] == ["ibatis2"]
    p = profile(libs=tuple(libs), case=CaseType.SPRING_LEGACY)
    [f] = [f for f in evaluate_advisories(p, cfg) if f.advisory_id == "ibatis2"]
    assert f.policy == "guide" and f.detected_version == "2.3.4.726"
    assert f.notes == ["see knowledge/notes/ibatis2.md"]


def test_mybatis3_project_has_no_ibatis2_advisory(cfg):
    p = profile(libs=(LibraryUsage(name="mybatis", evidence=["import:org.apache.ibatis"]),),
                case=CaseType.SPRING_BOOT)
    assert "ibatis2" not in [f.advisory_id for f in evaluate_advisories(p, cfg)]


def test_hint_for_orm_ibatis_compile_error_carries_the_fix_loop_note(cfg):
    f = _finding(cfg)
    e = BuildError(kind=ErrorKind.COMPILE, message="package org.springframework.orm.ibatis.support does not exist",
                   file="src/main/java/com/acme/dao/OrderDao.java")
    hints, ids = hints_for([e], [f])
    assert ids == ["ibatis2"] and len(hints) == 1
    assert hints[0].startswith("ibatis2 (incompatible, policy guide): replace with org.mybatis:mybatis")
    assert "Note (ibatis2.md § fix-loop: ibatis2)" in hints[0]
    assert "SqlSessionTemplate" in hints[0]
    # a symbol-only error matches through the raw javac block (import line)
    sym = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", symbol="class SqlMapClientDaoSupport",
                     file="src/main/java/com/acme/dao/OrderDao.java",
                     raw="import org.springframework.orm.ibatis.support.SqlMapClientDaoSupport;")
    assert hints_for([sym], [f])[1] == ["ibatis2"]
    other = BuildError(kind=ErrorKind.COMPILE, message="package org.apache.ibatis.session does not exist",
                       file="src/main/java/com/acme/dao/X.java")
    assert hints_for([other], [f]) == ([], [])


def test_verify_replaced_once_ported_to_mybatis(tmp_path: Path, cfg):
    write(tmp_path, "pom.xml", pom("x"))
    write_java(tmp_path, "", "com.acme.dao", "OrderDao", imports=DAO_IMPORTS)
    f = _finding(cfg)
    verify_advisories([f], PathView(tmp_path), cfg)
    assert f.outcome == "still-present"
    write_java(tmp_path, "", "com.acme.dao", "OrderDao", imports=("org.mybatis.spring.SqlSessionTemplate",))
    f2 = _finding(cfg)
    verify_advisories([f2], PathView(tmp_path), cfg)
    assert f2.outcome == "replaced"
    assert followups_for([f2])[0].startswith("ibatis2 → org.mybatis:mybatis")
