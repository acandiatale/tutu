"""A run that converted a framework project into a generated Spring Boot project (`RunReport.conversion`) says so in
both reports: the Korean guide's block under the head table (with the grouped hand-over items) and the English
"Framework conversion" section. A run without a conversion renders as before (the goldens pin the English)."""
from __future__ import annotations

import re

import pytest

from sbmig.core.models import ConversionInfo
from sbmig.loop import convert as cv
from sbmig.report import guide_ko, render_markdown
from sbmig.report.guide_ko import CONVERT_GROUPS, DB_MISSING_KO, conversion_group, framework_conversion_lines
from sbmig.report.markdown import framework_conversion_lines as en_lines

from .test_framework_scope import DIRECT, _report, _use
from .test_handover import make_report
from .test_report_i18n import _wording_problems

TITLE = "## Example 프레임워크 프로젝트를 사내 규정으로 전환했습니다"
# one reason of each kind the pipeline writes (docs/FW-CONVERT.md section 4 + sbmig/fwconvert/pipeline.py)
REASONS = {
    "DATASOURCE": "[DATASOURCE] jndi: the datasource is a JNDI lookup",
    "DATASOURCE_PASSWORD": "[DATASOURCE_PASSWORD] a database password is written in db.properties",
    "DATASOURCE_DRIVER": "[DATASOURCE_DRIVER] vendor tibero has no JDBC driver coordinate",
    "STUB_SPRING_LOGIC": "[STUB_SPRING_LOGIC] order has a router but no method reaches a SQL statement",
    "java_only": "[java_only] no trace of a database",
    "2b": "[2b] SqlSession",
    "3": "[3] source not found",
    "AMBIGUOUS_SQL_KEY": "[AMBIGUOUS_SQL_KEY] order.select is defined by a.example_sql and b.example_sql",
    "UNKNOWN_SQL_KEY": "[UNKNOWN_SQL_KEY] order.missing names no statement",
    "NO_SQL_KEY": "[NO_SQL_KEY] order::getList is a database activity without a sql-key",
    "external": "[extcall] remotecall",
    "CUSTOM_ACTIVITY": "[CUSTOM_ACTIVITY] the project's own activity class ShopUpsert",
    "BEAN_DIALECT": "[BEAN_DIALECT] the service 'legacy-service' is written in the bean dialect",
    "ENDPOINT_DUPLICATE": "[ENDPOINT_DUPLICATE] GET /api/item is already mapped; no handler was generated",
    "ENDPOINT_VERB": "[ENDPOINT_VERB] the endpoint verb 'HEAD' is not supported",
    "AJAX_NO_CONTROLLER": "[AJAX_NO_CONTROLLER] service 'Order': no router",
    "AJAX_UNMATCHED": "[AJAX_UNMATCHED] the endpoint name is still in the file",
    "JSP_PATH": "[JSP_PATH] /WEB-INF/inc/top.jspf",
    "FRONTEND_MISSING": "FRONTEND_MISSING src/main/webapp/css/a.css: not copied",
    "risk": "risk_score>=0.7 Class.forName",
    "MAVEN_BUILD": "MAVEN_BUILD compile failed",
    "other": "something nobody wrote yet",
}
DUP_ERROR = "GET /api/order: claimed by OrderController.list and Order2Controller.list"


def info(**kw) -> ConversionInfo:
    queue = [{"component": f"c-{k}", "reason": r, "source_path": f"src/{k}.x"} for k, r in REASONS.items()]
    base = dict(converted=True, framework="Example", project="myShop", package="com.example.myShop",
                modules=["myShop-boot", "myShop-domain", "myShop-service", "myShop-store"], commit="0123456789abcdef",
                counts={"services": 4, "mappers": 3, "statements": 27, "resources": 5, "java_files": 31, "jsp": 12,
                        "static": 40, "js_rewritten": 6, "pages": 3, "queue": len(queue)},
                datasource="configured:oracle", queue=queue, queue_total=len(queue),
                unexposed=["GET /api/order/summary  (OrderService.getSummary)"],
                skipped=["page: dashboard: no JSP"], errors=[DUP_ERROR, "x.example_sql: not a queryMap"])
    base.update(kw)
    return ConversionInfo(**base)


def report(conv: ConversionInfo | None = None, **kw):
    conv = conv if conv is not None else info()
    follow = [cv.duplicate_followup(conv.errors)] if cv.duplicate_followup(conv.errors) else []
    kw.setdefault("flags", ["tests-not-compiled", "framework-converted"])
    kw["manual_followups"] = follow + kw.get("manual_followups", ["web.xml not migrated"])
    return make_report(conversion=conv, framework_mode="convert", **kw)


def section(md: str, head: str) -> str:
    start = md.index(head)
    nxt = md.find("\n## ", start + len(head))
    return md[start:nxt if nxt >= 0 else len(md)]


# ------------------------------------------------------------------------------------------- grouping
@pytest.mark.parametrize("key", sorted(REASONS))
def test_every_reason_falls_into_its_group(key):
    got, tag = conversion_group(REASONS[key])
    assert got == key
    assert tag == ("extcall" if key == "external" else "")


def test_every_group_has_a_title_and_a_polite_sentence():
    keys = [k for k, _, _ in CONVERT_GROUPS]
    assert sorted(keys) == sorted(REASONS) and keys[-1] == "other"
    for _key, title, sentence in CONVERT_GROUPS:
        assert title and sentence.endswith("주세요.") and sentence.count(". ") == 0, sentence
        assert not _wording_problems(sentence), (sentence, _wording_problems(sentence))
    for text in DB_MISSING_KO.values():
        assert not _wording_problems(text), text


def test_unknown_bracket_tags_are_external_calls_each_in_its_own_group():
    md = render_markdown(report(info(queue=[{"component": "a::b", "reason": "[mailer] smtp", "source_path": ""},
                                            {"component": "c::d", "reason": "[extcall] x", "source_path": ""}],
                                     queue_total=2)), "ko")
    assert "**외부 시스템 호출: `extcall` (1건)**" in md and "**외부 시스템 호출: `mailer` (1건)**" in md


# ------------------------------------------------------------------------------------------- Korean guide
def test_guide_block_sits_right_under_the_head_table():
    md = render_markdown(report(), "ko")
    head_end = md.index("| 플래그 |")
    assert md.index(TITLE) > head_end
    between = md[md.index("\n", head_end) + 1:md.index(TITLE)]
    assert between.strip() == ""
    assert md.index(TITLE) < md.index("이 문서는 마이그레이션을 요청하신 담당자께") < md.index("## 확인해 주세요")


def test_guide_block_holds_the_facts_counts_steps_and_database():
    s = section(render_markdown(report(), "ko"), TITLE)
    for x in ("`myShop`", "`com.example.myShop`", "`myShop-boot`, `myShop-domain`, `myShop-service`, `myShop-store`",
              "| 서비스 | 4 |", "| SQL 묶음(mapper) | 3 |", "| SQL 문 | 27 |", "| 화면(JSP) | 12 |", "| 정적 파일 | 40 |",
              "| 주소를 바꾼 스크립트 | 6 |", "| 화면 컨트롤러 | 3 |",
              "구조 정리(사내 부모 pom과 필수 아티팩트 적용), 구조 검토, 빌드와 빌드 오류 수정",
              "Oracle 연결 정보를 찾아", "`SPRING_DATASOURCE_PASSWORD`", "`convert/queue.json`"):
        assert x in s, x
    assert "resources" not in s and "| queue" not in s
    for word in ("사유", "관리됨", "stub", "endpoint", "hand-over", "인수인계"):
        assert word not in s, word


def test_guide_groups_every_reason_once_in_the_fixed_order():
    s = section(render_markdown(report(), "ko"), TITLE)
    check = s[s.index("### 직접 확인해 주세요"):]
    pos = []
    for key, title, sentence in CONVERT_GROUPS:
        head = f"**{title}: `extcall` (1건)**" if key == "external" else f"**{title} (1건)**"
        assert check.count(head) == 1, head
        assert sentence in check
        pos.append(check.index(head))
    assert pos == sorted(pos)
    assert "- `c-DATASOURCE` — `src/DATASOURCE.x`" in check
    assert "- `c-other` — `src/other.x`: something nobody wrote yet" in check


def test_guide_caps_a_group_at_twenty_items():
    queue = [{"component": f"js_ajax::p{i}.js", "reason": "[AJAX_UNMATCHED] x", "source_path": ""} for i in range(23)]
    md = render_markdown(report(info(queue=queue, queue_total=230)), "ko")
    assert "**주소를 바꾸지 못한 스크립트 (23건)**" in md
    assert "`js_ajax::p19.js`" in md and "`js_ajax::p20.js`" not in md and "- 외 3건" in md
    assert "곳이 230건 있습니다" in md and "보고서에는 앞의 23건만 담았습니다." in md


def test_guide_lists_unexposed_skipped_and_errors_without_the_duplicates():
    s = section(render_markdown(report(), "ko"), TITLE)
    assert "### 노출하지 않은 주소" in s and "404" in s and "- `GET /api/order/summary` (`OrderService.getSummary`)" in s
    assert "### 읽지 않은 파일" in s and "- page: dashboard: no JSP" in s
    errs = s[s.index("### 전환 중 남은 오류"):]
    assert "not a queryMap" in errs and "claimed by" not in errs


def test_guide_lists_are_left_out_when_empty():
    s = section(render_markdown(report(info(queue=[], queue_total=0, unexposed=[], skipped=[], errors=[])), "ko"), TITLE)
    for x in ("### 직접 확인해 주세요", "### 노출하지 않은 주소", "### 읽지 않은 파일", "### 전환 중 남은 오류"):
        assert x not in s


@pytest.mark.parametrize("reason", sorted(DB_MISSING_KO))
def test_guide_database_not_found(reason):
    s = section(render_markdown(report(info(datasource=reason)), "ko"), TITLE)
    assert DB_MISSING_KO[reason] in s
    assert "애플리케이션은 DB 연결 설정이 있어야 시작하므로 `spring.datasource` 값을 직접 넣어 주세요." in s


def test_check_section_points_at_the_block_first_and_keeps_the_duplicates_next():
    md = render_markdown(report(), "ko")
    check = section(md, "## 확인해 주세요")
    items = re.findall(r"^\d+\. (.*)$", check, flags=re.MULTILINE)
    assert items[0] == (f"**사내 규정 전환에서 확인할 곳 {len(REASONS)}건** — 위 「{TITLE[3:]}」 절의 「직접 확인해 주세요」를 "
                        "먼저 봐 주세요.")
    assert items[1].startswith("**겹치는 주소** — 사내 규정 전환으로 만든 컨트롤러에")
    assert "claimed by" not in check


def test_no_pointer_without_items():
    md = render_markdown(report(info(queue=[], queue_total=0, errors=[])), "ko")
    assert "사내 규정 전환에서 확인할 곳" not in md


def test_title_without_a_framework_name():
    md = render_markdown(report(info(framework="")), "ko")
    assert "## 사내 프레임워크 프로젝트를 사내 규정으로 전환했습니다" in md
    assert "사내 프레임워크로 만든 프로젝트를" in md


def test_nothing_converted_gives_no_section_and_the_line_in_korean():
    conv = info(converted=False, project="", package="", modules=[], counts={}, datasource="", queue=[],
                queue_total=0, unexposed=[], skipped=[], errors=[])
    r = make_report(conversion=conv, framework_mode="convert", manual_followups=[cv.SKIPPED_LINE])
    ko, en = render_markdown(r, "ko"), render_markdown(r, "en")
    assert "사내 규정으로 전환했습니다" not in ko and "## Framework conversion" not in en
    assert framework_conversion_lines(r) == [] and en_lines(r) == []
    assert ("**사내 규정 전환** — 사내 규정 전환은 하지 않았습니다. 이 프로젝트에는 사내 프레임워크의 서비스 XML도 SQL "
            "파일도 없어서") in ko


def test_a_run_without_conversion_renders_as_before():
    r = make_report()
    assert framework_conversion_lines(r) == [] and en_lines(r) == []
    assert guide_ko.conversion_pointer(r) is None


def test_reduced_framework_block_is_not_shown_for_a_converted_run():
    r = _report(_use(DIRECT, scope="full", reason="framework.scope=full → full migration"), scope="full")
    assert "framework.scope: full" in render_markdown(r, "en")           # the block exists without a conversion
    r.conversion = info()
    assert "framework.scope: full" not in render_markdown(r, "en")
    assert "framework.scope: full" not in render_markdown(r, "ko")


# ------------------------------------------------------------------------------------------- English report
def test_english_section_right_under_the_head_table():
    md = render_markdown(report(), "en")
    assert "## Framework conversion" in md
    assert md.index("| Flags |") < md.index("## Framework conversion") < md.index("## Manual follow-ups")
    s = section(md, "## Framework conversion")
    for x in ("`myShop`, package `com.example.myShop`", "`myShop-boot`, `myShop-domain`", "`0123456789ab`",
              "services 4", "Datasource: oracle", "`${SPRING_DATASOURCE_PASSWORD:}`", "`convert/queue.json`",
              f"### Conversion hand-over items ({len(REASONS)})", "- `[extcall]` (1)", "- `risk_score>=0.7` (1)",
              "- `FRONTEND_MISSING` (1)", "- `[AMBIGUOUS_SQL_KEY]` (1)", "- `[UNKNOWN_SQL_KEY]` (1)", "- `[NO_SQL_KEY]` (1)", "### Endpoints not exposed (no screen calls them) (1)",
              "### Files not read (1)", "### Conversion errors (2)", DUP_ERROR):
        assert x in s, x
    assert s.rstrip().splitlines()[-1] != ""
    assert s.index("- `[extcall]`") < s.index("- `(other)` (1)")


def test_english_database_not_found_and_caps():
    queue = [{"component": f"c{i}", "reason": "[3] x", "source_path": ""} for i in range(25)]
    s = section(render_markdown(report(info(datasource="not_found", queue=queue, queue_total=300)), "en"),
                "## Framework conversion")
    assert "Datasource: not configured (not\\_found)" in s and "does not start without them" in s
    assert "### Conversion hand-over items (300, 25 in this report)" in s and "(+5 more)" in s


def test_flag_help_for_the_converted_flag():
    en = render_markdown(report(), "en")
    assert "- `framework-converted`: the in-house framework project was converted at intake" in en
    ko = render_markdown(report(), "ko")
    assert "플래그 `framework-converted`: 사내 프레임워크 프로젝트를 접수 단계에서" in ko


# ------------------------------------------------------------------------------------------- methods without an address
NO_ADDRESS = [
    "no address: OrderService.findOther  (statement in another mapper: com.example.myShop.store.ItemStore.find)",
    "no address: OrderService.ghost  (no statement)",
    "no address: CodeService.lovStatus  (list-of-values statement)",
    "no address: OrderService.calc  (not database-backed: java_only)",
    "no address: OrderService.orphan  (no transition)",
    "no address: OrderService.raw  (no endpoint)",
    "no address: OrderService.odd  (something new)",
]
KO_WHY = ["다른 SQL 묶음의 SQL 문을 씁니다(`com.example.myShop.store.ItemStore.find`)", "연결된 SQL 문이 없습니다",
          "코드 목록(LOV)용 SQL 문입니다", "DB를 쓰지 않는 기능입니다", "router의 어떤 주소도 이 기능을 가리키지 않습니다",
          "no endpoint", "something new"]
EXPOSED = "GET /api/order/summary  (OrderService.getSummary)"


def test_guide_splits_removed_endpoints_from_methods_without_an_address():
    s = section(render_markdown(report(info(unexposed=[EXPOSED, *NO_ADDRESS])), "ko"), TITLE)
    removed = s[s.index("### 노출하지 않은 주소"):s.index("### 주소가 없는 서비스 기능")]
    assert "`GET /api/order/summary`" in removed and "no address" not in removed
    block = s[s.index("### 주소가 없는 서비스 기능"):s.index("### 읽지 않은 파일")]
    assert ("생성한 서비스 클래스에는 있지만 화면에서 부를 주소가 없는 기능입니다. 화면에서 이 기능이 필요하면 그 서비스의 "
            "컨트롤러에 주소를 직접 추가해 주세요.") in block
    rows = [ln for ln in block.splitlines() if ln.startswith("- ")]
    names = ["OrderService.findOther", "OrderService.ghost", "CodeService.lovStatus", "OrderService.calc",
             "OrderService.orphan", "OrderService.raw", "OrderService.odd"]
    assert rows == [f"- `{n}`: {w}" for n, w in zip(names, KO_WHY)]
    assert not _wording_problems(block.replace("no endpoint", "").replace("something new", ""))


def test_guide_methods_without_an_address_cap_at_thirty():
    rows = [f"no address: S.m{i}  (no statement)" for i in range(33)]
    s = section(render_markdown(report(info(unexposed=rows)), "ko"), TITLE)
    assert "### 노출하지 않은 주소" not in s
    assert "`S.m29`" in s and "`S.m30`" not in s and "- 외 3건" in s


def test_only_removed_endpoints_render_as_before():
    s = section(render_markdown(report(info(unexposed=[EXPOSED])), "ko"), TITLE)
    assert "### 주소가 없는 서비스 기능" not in s and "- `GET /api/order/summary` (`OrderService.getSummary`)" in s
    en = section(render_markdown(report(info(unexposed=[EXPOSED])), "en"), "## Framework conversion")
    assert "Service methods without an address" not in en
    assert "### Endpoints not exposed (no screen calls them) (1)\n\n- `" + " ".join(EXPOSED.split()) + "`" in en


def test_english_splits_the_two_kinds():
    en = section(render_markdown(report(info(unexposed=[EXPOSED, *NO_ADDRESS])), "en"), "## Framework conversion")
    assert "### Endpoints not exposed (no screen calls them) (1)" in en
    block = en[en.index("### Service methods without an address (7)"):]
    assert all(f"- `{' '.join(x.split())}`" in block for x in NO_ADDRESS)
    assert " ".join(EXPOSED.split()) not in block


# ------------------------------------------------------------------------------------------- what ran after the conversion
from sbmig.core.models import BuildResult, Phase, PhaseResult  # noqa: E402

COMPLETE_KO = ("만든 프로젝트는 이어서 다른 Spring Boot 프로젝트와 같은 순서로 처리했습니다. 순서는 자동 변환(OpenRewrite), "
               "구조 정리(사내 부모 pom과 필수 아티팩트 적용), 구조 검토, 빌드와 빌드 오류 수정입니다.")


def _build(ok: bool = True) -> BuildResult:
    return BuildResult(ok=ok, goal="clean package", command=["mvn"], exit_code=0 if ok else 1, duration_s=1.0,
                       log_path="")


def _phases(*steps: str) -> list[PhaseResult]:
    return [PhaseResult(phase=Phase.TRANSFORM, step_ids=list(steps))]


def test_complete_run_keeps_the_full_sentence():
    r = report(final_build=_build(), phases=_phases("transform.rewrite", "restructure.boot_pom", "restructure.review",
                                                    "verify.build"))
    assert COMPLETE_KO in render_markdown(r, "ko")
    assert "- Then: OpenRewrite, company restructure (parent pom, mandatory artifacts), structure review, build and " \
           "fix loop, as for any multi-module Spring Boot project" in render_markdown(r, "en")


def test_stopped_run_names_what_ran_and_what_did_not():
    r = report(success=False, final_build=None, phases=_phases("transform.rewrite"),
               flags=["framework-converted", "aborted"], manual_followups=["run aborted: stop requested"])
    ko, en = render_markdown(r, "ko"), render_markdown(r, "en")
    assert COMPLETE_KO not in ko
    assert ("만든 프로젝트는 이어서 자동 변환(OpenRewrite)까지 진행했고, 구조 정리(사내 부모 pom과 필수 아티팩트 적용), 구조 "
            "검토, 빌드와 빌드 오류 수정은 하지 않은 채 실행이 끝났습니다. 끝난 이유는 맨 위 표의 결과 칸을 봐 주세요.") in ko
    assert ("- Then: OpenRewrite ran; the run ended before company restructure (parent pom, mandatory artifacts), "
            "structure review, build and fix loop (see Result at the top)") in en


def test_run_that_ended_right_after_the_conversion():
    r = report(success=False, final_build=None, phases=[], flags=["framework-converted", "stopped-after-intake"])
    ko, en = render_markdown(r, "ko"), render_markdown(r, "en")
    assert ("만든 프로젝트는 그 뒤 단계(자동 변환(OpenRewrite), 구조 정리(사내 부모 pom과 필수 아티팩트 적용), 구조 검토, "
            "빌드와 빌드 오류 수정)를 하나도 진행하지 못하고 실행이 끝났습니다.") in ko
    assert "- Then: the run ended before any later stage ran (see Result at the top)" in en


def test_aborted_after_a_build_says_it_ended_early():
    r = report(success=False, final_build=_build(False), flags=["framework-converted", "aborted"],
               phases=_phases("transform.rewrite", "restructure.boot_pom", "restructure.review@svc", "verify.build"),
               manual_followups=["run aborted: internal error: x"])
    assert ("만든 프로젝트는 이어서 자동 변환(OpenRewrite), 구조 정리(사내 부모 pom과 필수 아티팩트 적용), 구조 검토, 빌드와 "
            "빌드 오류 수정까지 진행했지만 실행이 중간에 끝났습니다.") in render_markdown(r, "ko")
    assert "the run ended early (see Result at the top)" in render_markdown(r, "en")


# ------------------------------------------------------------------------------------------- project text is inert
EVIL = "a`<b>x</b>`b\n# head\n- item **x** [l](http://e)"


def _evil_info() -> ConversionInfo:
    return info(framework="Ex`am\n# ple", project="p`r\noj", package="com.`x", modules=["m`1\n- x"],
                queue=[{"component": EVIL, "reason": "[**x**] " + EVIL, "source_path": "p`q\n## h"},
                       {"component": "z", "reason": "something " + EVIL, "source_path": ""}], queue_total=2,
                unexposed=["GET /api/`x  (" + EVIL + ")", "no address: S.`m  (" + EVIL + ")"],
                skipped=["- " + EVIL], errors=["1. " + EVIL], datasource="configured:ora`cle<b>")


def _code_spans_balanced(md: str) -> None:
    for line in md.splitlines():
        assert len(re.findall(r"(?<!\\)`", line)) % 2 == 0, line
        assert not re.search(r"(?<![\\`])<b>", re.sub(r"`[^`]*`", "", line)), line


def test_english_section_keeps_project_text_inert():
    r = report(_evil_info())
    lines = en_lines(r)
    md = "\n".join(lines)
    _code_spans_balanced(md)
    assert all("\n" not in x for x in lines)
    assert not any(x.startswith(("# ", "## h", "- item", "### h")) for x in md.splitlines()[1:]
                   if not x.startswith("### ") or "(" in x)
    assert "- **[**x**]** (1)" not in md and "- `[**x**]` (1)" in md
    assert "`a<b>x</b>b # head - item **x** [l](http://e)`" in md
    assert "- 1\\. a" in md and "- \\- a" in md


def test_korean_section_keeps_project_text_inert():
    r = report(_evil_info())
    lines = framework_conversion_lines(r)
    md = "\n".join(lines)
    _code_spans_balanced(md)
    assert all("\n" not in x for x in lines)
    heads = [x for x in md.splitlines() if x.startswith("#")]
    assert heads[0].startswith("## Ex\\`am \\# ple 프레임워크 프로젝트를")
    assert all(h.startswith(("## ", "### ")) for h in heads)
    assert not any(x.startswith(("- item", "- - ", "1. ")) for x in md.splitlines())


def test_generated_project_row_replaces_as_is_in_a_converted_run():
    from .test_handover import as_is_profile
    r = report(profile=as_is_profile())
    en = render_markdown(r, "en")
    assert "| Generated project | java " in en and "| As-is |" not in en and "(analysed after the conversion)" in en
    assert "- 생성한 프로젝트(전환 뒤 분석): " in render_markdown(r, "ko")
    plain = make_report(profile=as_is_profile())
    assert "| As-is | java " in render_markdown(plain, "en") and "| Generated project" not in render_markdown(plain, "en")
    assert "- 전환 전: " in render_markdown(plain, "ko")


def test_partial_sentence_particle_follows_the_last_stage():
    r = report(success=False, final_build=None, phases=_phases("transform.rewrite", "restructure.boot_pom", "verify.build"),
               flags=["framework-converted", "aborted"], manual_followups=["run aborted: stop requested"])
    assert "빌드와 빌드 오류 수정까지 진행했고, 구조 검토는 하지 않은 채 실행이 끝났습니다." in render_markdown(r, "ko")


def test_complete_run_without_a_review_step_lists_only_what_ran():
    r = report(final_build=_build(), phases=_phases("transform.rewrite", "restructure.boot_pom", "verify.build"))
    ko, en = render_markdown(r, "ko"), render_markdown(r, "en")
    assert ("만든 프로젝트는 이어서 다른 Spring Boot 프로젝트와 같은 순서로 처리했습니다. 순서는 자동 변환(OpenRewrite), 구조 "
            "정리(사내 부모 pom과 필수 아티팩트 적용), 빌드와 빌드 오류 수정입니다.") in ko
    then = next(x for x in en.splitlines() if x.startswith("- Then:"))
    assert then == ("- Then: OpenRewrite, company restructure (parent pom, mandatory artifacts), build and fix loop, as "
                    "for any multi-module Spring Boot project")


def test_complete_run_with_a_failed_final_build_does_not_read_as_a_success():
    r = report(success=False, final_build=_build(False),
               phases=_phases("transform.rewrite", "restructure.boot_pom", "restructure.review", "verify.build"))
    ko, en = render_markdown(r, "ko"), render_markdown(r, "en")
    sentence = next(x for x in ko.splitlines() if x.startswith("만든 프로젝트는"))
    assert sentence.startswith("만든 프로젝트는 이어서 자동 변환(OpenRewrite), 구조 정리(사내 부모 pom과 필수 아티팩트 적용), "
                               "구조 검토, 빌드와 빌드 오류 수정을 진행했습니다. 그 결과는 맨 위 표의 결과 칸을 봐 주세요.")
    for word in ("처리했습니다", "같은 순서", "통과", "성공"):
        assert word not in sentence, word
    then = next(x for x in en.splitlines() if x.startswith("- Then:"))
    assert then == ("- Then: OpenRewrite, company restructure (parent pom, mandatory artifacts), structure review, build "
                    "and fix loop ran (the outcome is in Result at the top)")
    assert "as for any" not in then
