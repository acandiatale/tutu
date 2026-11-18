"""Korean advisory texts: every shipped entry carries title_ko / summary_ko / followup_ko (and replacement_ko where the
replacement is free text), a finding copies them from its entry (extra files too), an entry without them still loads,
and the Korean keeps the identifiers the English text names."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sbmig.advisory import AdvisoryCatalog, evaluate_advisories, load_advisories
from sbmig.core.models import LibraryUsage
from tests.advisory.helpers import profile, write_advisories

REPO = Path(__file__).resolve().parents[2]
SHIPPED = REPO / "knowledge" / "libraries" / "advisories.yml"
HANGUL = re.compile(r"[가-힣]")
KO_FIELDS = ("title_ko", "summary_ko", "followup_ko")
# words the guide must not use (docs/INTERFACES.md, "Wording")
BANNED = ("사유", "관리됨", "as-is 버전으로 고정", "할 것.", "할 것 ")


def _raw_entries() -> list[dict]:
    return yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))["advisories"]


def _shipped():
    return AdvisoryCatalog.from_files([SHIPPED])


def test_every_shipped_entry_has_its_korean_texts():
    cat = _shipped()
    assert not cat.load_problems and len(cat) == len(_raw_entries())
    for e in cat:
        for ko, en in (("title_ko", e.title), ("summary_ko", e.summary), ("followup_ko", e.followup)):
            if en.strip():
                assert getattr(e, ko).strip(), f"{e.id}: {ko} missing"


def test_free_text_replacements_have_a_korean_text():
    for e in _shipped():
        free = bool(e.replacement) and " " in e.replacement.strip()      # a plain coordinate has no space
        assert bool(e.replacement_ko.strip()) is free, e.id


def test_every_korean_text_contains_hangul_and_no_banned_wording():
    for e in _shipped():
        for field in KO_FIELDS + ("replacement_ko",):
            text = getattr(e, field)
            if not text:
                continue
            assert HANGUL.search(text), f"{e.id}.{field} has no Hangul"
            for word in BANNED:
                assert word not in text, f"{e.id}.{field}: {word!r}"
            assert "\n" not in text and "  " not in text, f"{e.id}.{field}"


def test_the_english_fields_are_untouched_by_the_korean_ones():
    for raw in _raw_entries():
        for field in ("title", "summary", "followup", "replacement"):
            if raw.get(field):
                assert not HANGUL.search(str(raw[field])), f"{raw['id']}.{field}"


@pytest.mark.parametrize("aid, needles", [
    ("hystrix", ["org.springframework.cloud:spring-cloud-starter-circuitbreaker-resilience4j", "@HystrixCommand",
                 "@CircuitBreaker", "@EnableHystrix", "@EnableCircuitBreaker", "commandKey", "fallbackMethod",
                 "resilience4j.circuitbreaker.instances.<name>", "configs.default", "hystrix.command.*",
                 "resilience4j.timelimiter.*", "Throwable", "groupKey", "threadPoolKey", "commandProperties",
                 "ignoreExceptions", "spring-boot-starter-aspectj", "Spring Cloud 2020.0", "@EnableHystrixDashboard",
                 "hystrix-javanica", "hystrix-core", "MIGRATION-TODO", "Turbine"]),
    ("sleuth", ["Spring Cloud 2021.0.x", "Micrometer Tracing", "Brave", "OpenTelemetry", "micrometer-tracing",
                "1.0.x", "management.tracing.sampling.probability", "zipkin", "otlp", "traceId", "spanId"]),
    ("springfox", ["3.0.0", "springdoc-openapi", "GroupedOpenApi", "Docket", "/swagger-ui/index.html",
                   "/v3/api-docs", "springdoc.*", "springfox.*"]),
    ("jdk-removed-apis", ["sun.misc.BASE64Encoder", "BASE64Decoder", "java.util.Base64.getEncoder()",
                          "Base64.getMimeEncoder()", "Base64.getMimeDecoder()", "sun.misc.Unsafe", "jdk.unsupported",
                          "jakarta.xml.bind:jakarta.xml.bind-api", "org.glassfish.jaxb:jaxb-runtime",
                          "jakarta.annotation:jakarta.annotation-api", "jakarta.activation-api",
                          "UnsupportedOperationException", "com.sun.image.codec.jpeg", "javax.imageio.ImageIO",
                          "JEP 320", "JEP 403", "UpgradeToJava21", "MIGRATION-TODO", "--release 21"]),
])
def test_the_korean_keeps_the_identifiers_of_the_english(aid, needles):
    e = _shipped().get(aid)
    korean = f"{e.title_ko} {e.summary_ko} {e.followup_ko} {e.replacement_ko}"
    for n in needles:
        assert n in korean, f"{aid}: {n!r} missing from the Korean text"


def test_a_finding_carries_the_korean_texts(cfg):
    p = profile(libs=(LibraryUsage(name="hystrix", version="2.2.10.RELEASE"),))
    f = next(x for x in evaluate_advisories(p, cfg) if x.advisory_id == "hystrix")
    e = load_advisories(cfg).get("hystrix")
    assert (f.title_ko, f.summary_ko, f.followup_ko) == (e.title_ko, e.summary_ko, e.followup_ko)
    assert f.title_ko == "Netflix Hystrix는 지원이 종료되었습니다" and f.replacement_ko == ""
    assert f.title == e.title and f.followup == e.followup.strip()                # English kept beside it


def test_a_finding_carries_replacement_ko(cfg):
    p = profile(libs=(LibraryUsage(name="undertow", version="2.3.0"),))
    [f] = [x for x in evaluate_advisories(p, cfg) if x.advisory_id == "undertow"]
    assert f.replacement.startswith("org.springframework.boot:spring-boot-starter-tomcat")
    assert f.replacement_ko.startswith("`org.springframework.boot:spring-boot-starter-tomcat`")


def test_extra_file_entries_carry_their_korean_texts_and_entries_without_them_load(cfg, tmp_path):
    extra = write_advisories(tmp_path / "company-advisories.yml", [
        {"id": "acme-legacy", "artifacts": ["com.acme:legacy-*"], "status": "unmanaged", "title": "Acme legacy",
         "summary": "Old.", "followup": "Replace it.", "replacement": "the in-house successor (ask the team)",
         "title_ko": "Acme legacy는 더 이상 관리하지 않습니다", "summary_ko": "오래된 사내 라이브러리입니다.",
         "followup_ko": "후속 라이브러리로 바꿔 주세요.", "replacement_ko": "사내 후속 라이브러리(담당 팀 문의)"},
        {"id": "acme-plain", "artifacts": ["com.acme:plain-*"], "status": "eol", "title": "Acme plain",
         "followup": "Drop it."},
    ])
    cfg.advisories.extra_files = [str(extra)]
    cat = load_advisories(cfg)
    assert not cat.load_problems and "hystrix" in cat
    plain = cat.get("acme-plain")
    assert (plain.title_ko, plain.summary_ko, plain.followup_ko, plain.replacement_ko) == ("", "", "", "")
    found = {f.advisory_id: f for f in evaluate_advisories(
        profile(deps=("com.acme:legacy-core:1.0", "com.acme:plain-util:2.0")), cfg)}
    leg, pl = found["acme-legacy"], found["acme-plain"]
    assert (leg.title_ko, leg.summary_ko, leg.followup_ko, leg.replacement_ko) == (
        "Acme legacy는 더 이상 관리하지 않습니다", "오래된 사내 라이브러리입니다.", "후속 라이브러리로 바꿔 주세요.",
        "사내 후속 라이브러리(담당 팀 문의)")
    assert (pl.title_ko, pl.summary_ko, pl.followup_ko, pl.replacement_ko) == ("", "", "", "")
    assert pl.title == "Acme plain" and pl.followup == "Drop it."


def test_an_extra_file_override_replaces_the_whole_entry_korean_included(cfg, tmp_path):
    extra = write_advisories(tmp_path / "override.yml", [
        {"id": "hystrix", "library": "hystrix", "status": "eol", "title": "Hystrix (company text)",
         "followup": "Ask the platform team."}])
    cfg.advisories.extra_files = [str(extra)]
    [f] = [x for x in evaluate_advisories(profile(libs=(LibraryUsage(name="hystrix"),)), cfg)
           if x.advisory_id == "hystrix"]
    # the shipped Korean of another text must not survive under the company's English
    assert f.title == "Hystrix (company text)" and f.title_ko == "" and f.followup_ko == ""
