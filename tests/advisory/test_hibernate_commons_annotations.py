"""Advisory `hibernate-commons-annotations` (2026-10-22, company run): Hibernate ORM 7 (Boot 4) dropped HCANN, so
`org.hibernate.annotations.common.*` imports stop compiling (`package … does not exist`)."""
from __future__ import annotations

from sbmig.advisory import evaluate_advisories, hints_for
from sbmig.core.models import BuildError, CaseType, ErrorKind, LibraryUsage
from tests.advisory.helpers import profile


def _finding(cfg):
    p = profile(libs=(LibraryUsage(name="hibernate-commons-annotations",
                                   evidence=["import:org.hibernate.annotations.common"]),),
                case=CaseType.SPRING_LEGACY)
    [f] = [f for f in evaluate_advisories(p, cfg) if f.advisory_id == "hibernate-commons-annotations"]
    return f


def test_detected_from_imports_with_guide_policy(cfg):
    f = _finding(cfg)
    assert f.status == "incompatible" and f.policy == "guide" and f.recipes == []
    assert "org.jboss.logging.Logger" in (f.replacement or "") and f.notes == ["see knowledge/notes/hibernate-7.md"]
    assert "7.0.3.Final" in f.followup


def test_hint_for_the_missing_package_error(cfg):
    f = _finding(cfg)
    e = BuildError(kind=ErrorKind.COMPILE, file="src/main/java/com/acme/dao/BaseDao.java", line=3,
                   message="package org.hibernate.annotations.common.util.impl does not exist",
                   raw="import org.hibernate.annotations.common.util.impl.LoggerFactory;")
    hints, ids = hints_for([e], [f])
    assert ids == ["hibernate-commons-annotations"] and len(hints) == 1
    assert "fix-loop: hibernate-commons-annotations" in hints[0] and "org.jboss.logging.Logger" in hints[0]
    other = BuildError(kind=ErrorKind.COMPILE, message="cannot find symbol", symbol="class Codec",
                       file="src/main/java/com/acme/App.java")
    assert hints_for([other], [f]) == ([], [])
