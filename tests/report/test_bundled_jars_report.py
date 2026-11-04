"""v2.1z-i: report section "Bundled jars (declared by no pom)" and the `bundled-jars` flag text (en / ko)."""
from __future__ import annotations

from sbmig.core.models import JarCoordinate
from sbmig.report import render_markdown
from sbmig.report.markdown import BUNDLED_TITLE, bundled_jars_lines, flag_help

from .test_handover import as_is_profile, make_report

WIRED = ("⚠ bundled jar `acme-seed-1.0.jar` (Maven coordinates unknown — no pom.properties, not in the jar name table) "
         "wired into `shop-back/pom.xml` as a system dependency `com.example:acme-seed:1.0` — wired by sbmig …")


def _report(**kw):
    prof = as_is_profile()
    prof.bundled_jars = [
        JarCoordinate(path="src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar", provides=["com.acme.seed", "com.acme.seed.x"],
                      used=True),
        JarCoordinate(path="src/main/webapp/WEB-INF/lib/vendor.jar", group_id="com.vendor", artifact_id="vendor",
                      version="3.1", source="pom_properties", provides=["com.vendor"], used=False),
        JarCoordinate(path="lib/odd.jar"),
    ]
    return make_report(profile=prof, flags=["bundled-jars"], **kw)


def test_section_rows():
    r = _report(manual_followups=[WIRED])
    lines = bundled_jars_lines(r)
    assert lines[0] == f"## {BUNDLED_TITLE}"
    assert "| `src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar` | unknown | 2 | yes | system |" in lines
    assert "| `src/main/webapp/WEB-INF/lib/vendor.jar` | `com.vendor:vendor:3.1` | 1 | no | — |" in lines
    assert "| `lib/odd.jar` | unknown | 0 | — | — |" in lines


def test_section_placement_and_flag():
    md = render_markdown(_report(manual_followups=[WIRED]), "en")
    assert f"## {BUNDLED_TITLE}" in md
    assert md.index("## Error list") < md.index(f"## {BUNDLED_TITLE}")
    assert "- `bundled-jars`: jars sit in the tree" in md
    assert flag_help("bundled-jars")


def test_no_section_without_bundled_jars():
    assert bundled_jars_lines(make_report()) == []
    assert BUNDLED_TITLE not in render_markdown(make_report(), "en")


def test_korean():
    md = render_markdown(_report(manual_followups=[WIRED]), "ko")
    assert "## 동봉 jar (pom 미선언)" in md and "| jar | 좌표 | 패키지 | 사용 | 연결 |" in md
    assert "| `src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar` | 모름 | 2 | 예 | system |" in md
    assert flag_help("bundled-jars", "ko").startswith("어느 pom 도 선언하지 않은 jar")
