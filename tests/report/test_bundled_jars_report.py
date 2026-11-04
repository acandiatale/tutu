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
    assert "| `src/main/webapp/WEB-INF/lib/acme-seed-1.0.jar` | unknown | 2 | yes | system | coordinates unknown |" in lines
    # v2.1z-j: a jar no source references is `unused`
    assert "| `src/main/webapp/WEB-INF/lib/vendor.jar` | `com.vendor:vendor:3.1` | 1 | no | — | unused |" in lines
    assert "| `lib/odd.jar` | unknown | 0 | — | — | coordinates unknown |" in lines


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


# ---------------------------------------------------------------------------------------------- v2.1z-j stages
def _staged():
    prof = as_is_profile()
    prof.bundled_jars = [
        JarCoordinate(path="w/WEB-INF/lib/acme-seed-1.0.jar", provides=["com.acme.seed"], used=True,
                      note="restructure: wired (system)"),
        JarCoordinate(path="w/WEB-INF/lib/late.jar", provides=["com.late"], used=True,
                      note="restructure: unused: referenced by no source / JSP / XML / properties file"),
        JarCoordinate(path="w/WEB-INF/lib/servlet-api-2.5.jar", provides=["javax.servlet"], used=False,
                      note="restructure: container: a servlet / JSP API copy the container supplies"),
        JarCoordinate(path="w/WEB-INF/lib/jsp-api.jar", provides=["javax.servlet.jsp"], scope="provided"),
        JarCoordinate(path="w/WEB-INF/lib/loopy.jar", provides=["com.loopy"], used=True,
                      note="restructure: system: coordinates unknown"),
    ]
    loop_line = WIRED.replace("acme-seed-1.0.jar", "loopy.jar")
    return make_report(profile=prof, flags=["bundled-jars"], manual_followups=[WIRED, loop_line])


def test_section_stage_and_status_v21zj():
    lines = bundled_jars_lines(_staged())
    assert any(x.startswith("The restructure wired every referenced jar before the first build") for x in lines)
    assert "| `w/WEB-INF/lib/acme-seed-1.0.jar` | unknown | 1 | yes | system (restructure) | coordinates unknown |" in lines
    assert "| `w/WEB-INF/lib/late.jar` | unknown | 1 | no | — | unused |" in lines
    assert "| `w/WEB-INF/lib/servlet-api-2.5.jar` | unknown | 1 | no | — | container API |" in lines
    assert "| `w/WEB-INF/lib/jsp-api.jar` | unknown | 1 | — | — | container API |" in lines
    assert "| `w/WEB-INF/lib/loopy.jar` | unknown | 1 | yes | system (loop) | coordinates unknown |" in lines


def test_section_stage_korean_v21zj():
    md = render_markdown(_staged(), "ko")
    assert "| system (재구성) |" in md and "| system (수정 루프) |" in md
    assert "| 참조 없음 |" in md and "| 컨테이너 API |" in md
    assert "재구성이 첫 빌드 전에 참조되는 jar 를 모두 연결했다" in md
