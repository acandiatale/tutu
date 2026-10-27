"""report.md: javax API jars still on the classpath (X1b) and the Blocked cell with `via` (2026-10-22)."""
from __future__ import annotations

import json

from sbmig.core.config import ArtifactSpec
from sbmig.core.models import GAV, BlockedItem, BlockReason, JavaxApiJar, Phase
from sbmig.core.workspace import Workspace
from sbmig.report import render_markdown, write_report
from sbmig.report.markdown import FLAG_HELP, JAVAX_JARS_TITLE, blocked_rows, javax_api_lines

from .test_report import sample_report

API = "javax.persistence:javax.persistence-api:2.2"


def jars() -> list[JavaxApiJar]:
    return [
        JavaxApiJar(artifact=GAV.parse(API), module="shop-back", scope="compile", via=GAV.parse("org.qlrm:qlrm:2.1.1"),
                    path=["com.acme:shop-back:jar:1.0", "org.qlrm:qlrm:jar:2.1.1:compile", API]),
        JavaxApiJar(artifact=GAV.parse("javax.inject:javax.inject:1"), module=None, scope="provided",
                    via=GAV.parse("com.company.web:legacy-dao:1.4.0")),
        JavaxApiJar(artifact=GAV.parse("javax.servlet:javax.servlet-api:4.0.1"), module="shop-boot",
                    scope="provided", declared=True),
        JavaxApiJar(artifact=GAV.parse("javax.mail:javax.mail-api:1.6.2"), via=GAV.parse("org.vendor:mailer:2.0")),
        JavaxApiJar(artifact=GAV.parse(API), via=GAV.parse("ch.simas.qlrm:qlrm:1.7.1")),
    ]


def report_with_jars():
    r = sample_report()
    r.javax_api_jars = jars()
    r.flags = [*r.flags, "javax-api-on-classpath"]
    return r


def test_section_rendered_after_blocked(cfg):
    c = cfg.model_copy(deep=True)
    c.artifacts.parent = ArtifactSpec(group_id="com.company", artifact_id="base-parent", version="4.0.0")
    md = render_markdown(report_with_jars(), cfg=c)
    assert f"## {JAVAX_JARS_TITLE}" in md and "| javax API jars | 5 |" in md
    assert md.index(f"## {JAVAX_JARS_TITLE}") < md.index("## Unresolved errors")
    assert "| artifact | module | scope | via (direct dependency) | declared in pom | what to do |" in md
    assert (f"| `{API}` | shop-back | compile | `org.qlrm:qlrm:2.1.1` | no | raise to 4.2.1 (jakarta.* since "
            "4.0.1), requires Java 17 — QLRM") in md
    assert "| `javax.inject:javax.inject:1` | (root) | provided | `com.company.web:legacy-dao:1.4.0` | no | " \
           "in-house → Jakarta build |" in md
    assert "| shop-boot | provided | - | yes | remove the declaration from the pom |" in md
    assert "`org.vendor:mailer:2.0` | no | upgrade / replace |" in md
    assert "`ch.simas.qlrm:qlrm:1.7.1` | no | coordinates changed: relocate to `org.qlrm:qlrm:4.2.1`" in md
    assert f"- `javax-api-on-classpath`: {FLAG_HELP['javax-api-on-classpath']}" in md


def test_in_house_from_handover_parent_without_cfg():
    r = report_with_jars()
    from sbmig.core.models import HandoverSummary
    r.handover = HandoverSummary(parent_after="com.company:base-parent:4.0.0")
    assert "in-house → Jakarta build" in "\n".join(javax_api_lines(r))
    r.handover = None
    assert "in-house → Jakarta build" not in "\n".join(javax_api_lines(r))


def test_section_absent_when_empty():
    md = render_markdown(sample_report())
    assert JAVAX_JARS_TITLE not in md and "| javax API jars |" not in md


def test_korean_section():
    ko = render_markdown(report_with_jars(), language="ko")
    assert "## 클래스패스에 남은 javax API jar" in ko and "| javax API jar | 5개 |" in ko
    assert "| 아티팩트 | 모듈 | 스코프 | via (직접 의존성) | pom에 선언 | 할 일 |" in ko
    assert "4.2.1(으)로 올림 (4.0.1부터 jakarta.*), Java 17 이상 필요" in ko
    assert "| (루트) |" in ko and "pom에서 선언 제거" in ko and "업그레이드 / 교체" in ko
    assert "좌표가 바뀜: `org.qlrm:qlrm:4.2.1`(으)로 이전" in ko
    assert JAVAX_JARS_TITLE not in ko


def test_blocked_cell_shows_via():
    b = BlockedItem(id="blk-q", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.SPRING, cluster_id="c",
                    artifact=GAV.parse(API), owner_class="javax.persistence.Query",
                    via=["com.acme:shop-back:jar:1.0", "org.qlrm:qlrm:jar:2.1.1:compile", API],
                    via_direct=GAV.parse("org.qlrm:qlrm:2.1.1"), suggestion="raise it")
    row = blocked_rows([b])[2]
    assert f"| `{API}` ← `org.qlrm:qlrm:2.1.1` (class `javax.persistence.Query`) |" in row
    d = b.model_copy(update={"via": ["com.acme:shop-back:jar:1.0", API], "via_direct": None})
    assert f"`{API}` ← declared in the pom" in blocked_rows([d])[2]
    assert f"`{API}` ← pom에 직접 선언" in blocked_rows([d], "ko")[2]
    long = b.model_copy(update={"owner_class": "x" * 300})
    cell = blocked_rows([long])[2].split(" | ")[1]
    assert len(cell) <= 160


def test_report_json_carries_the_inventory(tmp_path):
    ws = Workspace(tmp_path / "p", tmp_path / "run", "t")
    (tmp_path / "p").mkdir()
    out = write_report(report_with_jars(), ws)
    data = json.loads(open(out["report_json"], encoding="utf-8").read())
    assert data["javax_api_jars"][0]["via"]["artifact_id"] == "qlrm"
    assert data["javax_api_jars"][2]["declared"] is True
