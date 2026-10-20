"""Round 9: externalized-config scanner — config-placeholder source, per-service (MSA) inventory, edge probes."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze import analyze_path, load_rules, scan_properties
from sbmig.analyze.analyzer import PathView
from sbmig.analyze.pom_parser import parse_poms
from sbmig.analyze.properties import iter_placeholders, parse_properties
from sbmig.analyze.source_scanner import scan_sources
from sbmig.core.models import CaseType, ServiceInfo
from tests.analyze.helpers import dep, pom, write

REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "knowledge" / "config" / "externalized.yml"
APP = "src/main/resources/application.properties"


def jw(root: Path, name: str, body: str, *, module: str = "", head: str = "", test: bool = False) -> None:
    base = f"{module}/" if module else ""
    kind = "test" if test else "main"
    write(root, f"{base}src/{kind}/java/com/acme/{name}.java",
          f"package com.acme;\n\n{head}\npublic class {name} {{\n{body}\n}}\n")


def inv(root: Path, cfg, services=None):
    if not (root / "pom.xml").exists():
        write(root, "pom.xml", pom("app"))
    ws = PathView(root)
    poms, _ = parse_poms(ws)
    return scan_properties(ws, poms, scan_sources(ws), load_rules(RULES), cfg, services=services)


def by(inventory) -> dict:
    return {f.key: f for f in inventory.findings}


def svc(name: str, root_pom: str, kind: str = "service") -> ServiceInfo:
    return ServiceInfo(name=name, root_pom=root_pom, case_type=CaseType.SPRING_BOOT, kind=kind)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# C1: config-placeholder
# ---------------------------------------------------------------------------
def test_config_placeholder_source_and_classification(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "C", "", head='@PropertySource("file:${conf.dir}/x.properties")')
    write(tmp_path, APP, "spring.datasource.url=${DB_URL}\nlog.dir=${LOG_DIR:/tmp}\n")
    write(tmp_path, "src/main/resources/application.yml", "app:\n  key: ${APP_KEY}\n")
    f = by(inv(tmp_path, cfg))
    r = f["DB_URL"].refs[0]
    assert r.source == "config-placeholder" and r.file == APP and r.line == 1 and "spring.datasource.url" in r.raw
    assert f["DB_URL"].status == "external" and f["DB_URL"].boot_keys == ["DB_URL"]
    assert f["LOG_DIR"].status == "defaulted"
    assert f["APP_KEY"].refs[0].source == "config-placeholder" and f["APP_KEY"].refs[0].line == 2
    assert f["conf.dir"].refs[0].source == "property-source"            # locations keep the old source


# ---------------------------------------------------------------------------
# C2: per-service inventory
# ---------------------------------------------------------------------------
def _msa(root: Path) -> list[ServiceInfo]:
    write(root, "pom.xml", pom("parent", packaging="pom", modules=("order", "user", "common")))
    for m in ("order", "user", "common"):
        write(root, f"{m}/pom.xml", pom(m))
    jw(root, "OrderCfg", '@Value("${db.url}") String u; @Value("${order.only}") String o;', module="order")
    jw(root, "UserCfg", '@Value("${db.url}") String u; @Value("${user.only}") String o;', module="user")
    jw(root, "Shared", '@Value("${shared.key}") String s; @Value("${db.url}") String u;', module="common")
    write(root, "order/" + APP, "db.url=jdbc:x\norder.only=1\n")
    write(root, "user/" + APP, "shared.key=1\n")
    return [svc("order", "order/pom.xml"), svc("user", "user/pom.xml")]


def test_per_service_definitions_are_not_shared(tmp_path: Path, cfg) -> None:
    i = inv(tmp_path, cfg, services=_msa(tmp_path))
    assert i.services == ["order", "user"]
    got = {(f.service, f.key): f.status for f in i.findings}
    assert got == {("order", "db.url"): "defined", ("user", "db.url"): "external", (None, "db.url"): "defined",
                   ("order", "order.only"): "defined", ("user", "user.only"): "external",
                   (None, "shared.key"): "defined"}
    keys = [(f.service, f.key) for f in i.findings]
    assert len(keys) == len(set(keys))
    assert [k for k in keys if k[1] == "db.url"] == [(None, "db.url"), ("order", "db.url"), ("user", "db.url")]
    user_db = next(f for f in i.findings if f.service == "user" and f.key == "db.url")
    assert [r.file for r in user_db.refs] == ["user/src/main/java/com/acme/UserCfg.java"]
    assert user_db.defined_in == [] and user_db.boot_keys == ["db.url"]
    assert {(f.service, f.key) for f in i.external} == {("user", "db.url"), ("user", "user.only")}


def test_project_wide_without_services(tmp_path: Path, cfg) -> None:
    _msa(tmp_path)
    for services in (None, []):
        i = inv(tmp_path, cfg, services=services)
        assert i.services == [] and all(f.service is None for f in i.findings)
        assert {f.key: f.status for f in i.findings}["user.only"] == "external"
        assert {f.key: f.status for f in i.findings}["db.url"] == "defined"


def test_nested_and_root_services_deepest_wins(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("gateway"))
    write(tmp_path, "inner/pom.xml", pom("inner"))
    jw(tmp_path, "Gw", '@Value("${k}") String k; @Value("${gw.only}") String g;')
    jw(tmp_path, "In", '@Value("${k}") String k; @Value("${gw.only}") String g;', module="inner")
    write(tmp_path, APP, "gw.only=1\n")
    write(tmp_path, "inner/" + APP, "k=2\n")
    i = inv(tmp_path, cfg, services=[svc("gateway", "pom.xml"), svc("inner", "inner/pom.xml")])
    got = {(f.service, f.key): f.status for f in i.findings}
    assert got == {("gateway", "k"): "external", ("inner", "k"): "defined",
                   ("gateway", "gw.only"): "defined", ("inner", "gw.only"): "external"}


def test_system_property_and_rules_unchanged_per_service(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("p", packaging="pom", modules=("a",)))
    write(tmp_path, "a/pom.xml", pom("a"))
    jw(tmp_path, "S", 'String h = System.getProperty("app.home"); String n = "java:comp/env/jdbc/x";', module="a")
    i = inv(tmp_path, cfg, services=[svc("a", "a/pom.xml")])
    f = by(i)
    assert f["app.home"].service == "a" and f["app.home"].boot_keys == [] and f["app.home"].supplied_by == "system-property"
    assert f["java:comp/env/jdbc/x"].boot_keys[0] == "spring.datasource.url"


def test_analyzer_passes_deployables_for_msa(tmp_path: Path, cfg) -> None:
    boot = dep("org.springframework.boot", "spring-boot-starter-web")
    write(tmp_path, "pom.xml", pom("parent", packaging="pom", modules=("order-service", "user-service", "common")))
    for m in ("order-service", "user-service"):
        write(tmp_path, f"{m}/pom.xml", pom(m, deps=(boot,)))
        write(tmp_path, f"{m}/src/main/java/com/acme/{m[:4]}/App.java",
              f"package com.acme.{m[:4]};\nimport org.springframework.boot.autoconfigure.SpringBootApplication;\n"
              f"@SpringBootApplication\npublic class App {{\n  @org.springframework.beans.factory.annotation.Value"
              f'("${{{m[:4]}.key}}") String k;\n  public static void main(String[] a) {{}}\n}}\n')
    write(tmp_path, "common/pom.xml", pom("common"))
    jw(tmp_path, "Util", '@Value("${common.key}") String c;', module="common")
    p = analyze_path(tmp_path, cfg)
    assert p.case_type == CaseType.MSA
    deployables = sorted(s.name for s in p.services if s.kind == "service")
    assert p.properties.services == deployables and "common" not in p.properties.services
    got = {(f.service, f.key) for f in p.properties.findings}
    assert ("common.key" in {k for _, k in got}) and (None, "common.key") in got


# ---------------------------------------------------------------------------
# C4: edge probes
# ---------------------------------------------------------------------------
def test_nested_default_records_inner_key(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Value("${a:${b:x}}") String a; @Value("${c:${d}}") String c;')
    f = by(inv(tmp_path, cfg))
    assert f["a"].status == "defaulted" and f["b"].status == "defaulted"
    assert f["c"].status == "defaulted" and f["d"].status == "external"      # the inner one has no default


def test_spel_only_is_ignored(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", "@Value(\"#{systemProperties['x']}\") String a; @Value(\"#{environment['y'] ?: 'z'}\") String b;"
                      '@Value("#{ ${n:2} * 2 }") int n;')
    assert set(by(inv(tmp_path, cfg))) == {"n"}


def test_value_on_constructor_and_setter_parameters(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", 'public A(@Value("${ctor.key}") String a,\n    @Value("${ctor.two:1}") int b) {}\n'
                      'public void setX(@Value("${setter.key}") String x) {}')
    f = by(inv(tmp_path, cfg))
    assert f["ctor.key"].status == "external" and f["ctor.two"].status == "defaulted"
    assert f["setter.key"].refs[0].line == 7


@pytest.mark.parametrize("annot, key", [
    ('@ConditionalOnProperty(prefix = "feature", name = "mail", havingValue = "true")', "feature.mail"),
    ('@ConditionalOnProperty(prefix = "feature.", name = {"a", "b"})', "feature.a"),
    ('@ConditionalOnProperty("feature.cache.enabled")', "feature.cache.enabled"),
    ('@ConditionalOnProperty(value = "x.y", matchIfMissing = true)', "x.y"),
    ('@ConditionalOnBooleanProperty(name = "flag.z")', "flag.z"),
])
def test_conditional_on_property_is_a_defaulted_read(tmp_path: Path, cfg, annot: str, key: str) -> None:
    jw(tmp_path, "C", "", head=annot)
    f = by(inv(tmp_path, cfg))
    assert key in f
    x = f[key]
    assert x.status == "defaulted" and x.boot_keys == [] and "ConditionalOnProperty" in x.note
    assert x.refs[0].source == "value-annotation" and x.refs[0].default.startswith("(unset")
    if "matchIfMissing" in annot:
        assert "matchIfMissing" in x.refs[0].default
    if "{" in annot:
        assert "feature.b" in f


def test_conditional_key_defined_and_mixed_reads(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "C", '@Value("${feature.mail}") String m;', head='@ConditionalOnProperty("feature.mail")')
    jw(tmp_path, "D", "", head='@ConditionalOnProperty("feature.sms")')
    write(tmp_path, APP, "feature.sms=on\n")
    f = by(inv(tmp_path, cfg))
    assert f["feature.mail"].status == "external" and f["feature.mail"].boot_keys == ["feature.mail"]  # @Value needs it
    assert f["feature.sms"].status == "defined"


def test_scheduled_request_mapping_and_index_keys(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Scheduled(cron = "${job.cron:0 0 * * * *}") void run() {}\n'
                      '@RequestMapping("${api.base}/orders") void m() {}\n'
                      '@Value("${my.list[0]}") String first; @Value("${my.props[1]}") String p;')
    write(tmp_path, "src/main/resources/application.yml", "my:\n  list:\n    - a\n    - b\n")
    write(tmp_path, APP, "my.props[1]=x\n")
    f = by(inv(tmp_path, cfg))
    assert f["job.cron"].status == "defaulted" and f["job.cron"].refs[0].default == "0 0 * * * *"
    assert f["api.base"].status == "external"
    assert f["my.list[0]"].status == "defined" and f["my.props[1]"].status == "defined"


def test_yaml_multi_document_profiles_and_list_key(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/resources/application.yml",
          "base: 1\n---\nspring:\n  config:\n    activate:\n      on-profile: prod\nprod:\n  url: x\n"
          "---\nspring:\n  config:\n    activate:\n      on-profile: [qa, stage]\nqa: {k: 1}\n")
    jw(tmp_path, "A", '@Value("${prod.url}") String a; @Value("${qa.k}") String b; @Value("${my.list}") String c;')
    write(tmp_path, "src/main/resources/application-x.yml", "my:\n  list: [a, b]\n")
    i = inv(tmp_path, cfg)
    f = by(i)
    assert f["prod.url"].status == "defined" and f["qa.k"].status == "defined"
    assert {"prod", "qa", "stage", "x"} <= set(i.profiles)
    assert f["my.list"].status == "external"            # by design: a YAML list defines my.list[0], my.list[1]


def test_bootstrap_counts_as_defined_with_note(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/resources/bootstrap.yml", "spring:\n  cloud:\n    config:\n      uri: http://cfg\n")
    jw(tmp_path, "A", '@Value("${spring.cloud.config.uri}") String u;')
    i = inv(tmp_path, cfg)
    assert by(i)["spring.cloud.config.uri"].status == "defined"
    assert any("bootstrap" in n and "spring-cloud-starter-bootstrap" in n for n in i.notes)
    write(tmp_path, "pom.xml", pom("app", deps=(dep("org.springframework.cloud", "spring-cloud-starter-bootstrap"),)))
    assert not any("spring-cloud-starter-bootstrap" in n for n in inv(tmp_path, cfg).notes)


def test_no_trailing_newline_crlf_and_bom(tmp_path: Path, cfg) -> None:
    (tmp_path / "src/main/resources").mkdir(parents=True)
    (tmp_path / APP).write_bytes("﻿first.key=1\r\nsecond.key = ${SECOND}\r\nlong=a \\\r\n  b\r\nlast=x".encode())
    jw(tmp_path, "A", '@Value("${first.key}") String a; @Value("${last}") String b;')
    (tmp_path / "src/main/java/com/acme/B.java").write_bytes(
        b'package com.acme;\r\npublic class B {\r\n  @Value("${crlf.key}") String k;\r\n}')
    f = by(inv(tmp_path, cfg))
    assert f["first.key"].status == "defined" and f["last"].status == "defined"
    assert f["SECOND"].refs[0].line == 2 and f["SECOND"].refs[0].source == "config-placeholder"
    assert f["crlf.key"].refs[0].line == 3
    assert parse_properties("﻿a=1")[0][0] == "﻿a"     # the parser is raw; _read strips the BOM


def test_yaml_with_bom(tmp_path: Path, cfg) -> None:
    (tmp_path / "src/main/resources").mkdir(parents=True)
    (tmp_path / "src/main/resources/application.yml").write_bytes("﻿app:\n  name: 상점\n".encode())
    jw(tmp_path, "A", '@Value("${app.name}") String n;')
    assert by(inv(tmp_path, cfg))["app.name"].status == "defined"


def test_property_source_ignore_resource_not_found(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "C", "", head='@PropertySource(value = "classpath:a.properties", ignoreResourceNotFound = true)')
    jw(tmp_path, "U", '@Value("${a.key}") String a;')
    write(tmp_path, "src/main/resources/a.properties", "a.key=1\n")
    i = inv(tmp_path, cfg)
    assert by(i)["a.key"].status == "defined" and "src/main/resources/a.properties" in i.packaged_files


def test_property_source_named_but_missing_is_a_note(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "C", "", head='@PropertySource(value = "classpath:conf/ext.properties", ignoreResourceNotFound = true)')
    assert any("classpath:conf/ext.properties" in n and "not packaged" in n for n in inv(tmp_path, cfg).notes)


def test_typed_default_and_non_literal_keys(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "E", 'static final String PREFIX = "app.";\n@Autowired Environment env;\n'
                      'void a() { env.getProperty("typed.key", Integer.class, 5); }\n'
                      'void b() { System.getProperty(PREFIX + "x"); env.getProperty(PREFIX + "y"); }\n'
                      'void c() { System.getProperty("lit" + suffix); env.getProperty(KEY, "d"); }\n')
    f = by(inv(tmp_path, cfg))
    assert set(f) == {"typed.key"} and f["typed.key"].status == "defaulted"


def test_empty_and_escaped_placeholders(tmp_path: Path, cfg) -> None:
    assert [k for k, _, _ in iter_placeholders(r"a ${} b \${esc} c $${dollar}")] == ["", "dollar"]
    jw(tmp_path, "A", '@Value("${}") String a; @Value("\\\\${esc.key}") String b; @Value("$${dollar.key}") String c;')
    write(tmp_path, APP, "v=${}\nw=$${x.y}\n")
    f = by(inv(tmp_path, cfg))
    assert set(f) == {"dollar.key", "x.y"}


def test_korean_values_and_legacy_encoding(tmp_path: Path, cfg) -> None:
    write(tmp_path, APP, "site.name=한국 상점\nsite.title=\\uD55C\\uAD6D\nsite.url=${SITE_URL:http://예.kr}\n")
    (tmp_path / "src/main/resources/messages.properties").write_bytes("greet=안녕\n".encode("euc-kr"))
    jw(tmp_path, "A", '@Value("${site.name}") String n; @Value("${site.url}") String u;')
    i = inv(tmp_path, cfg)
    f = by(i)
    assert f["site.name"].status == "defined" and f["SITE_URL"].status == "defaulted"
    assert f["SITE_URL"].refs[0].default == "http://예.kr"


def test_properties_separators_and_continuations(tmp_path: Path, cfg) -> None:
    write(tmp_path, APP, "a.key = 1\nb.key: 2\nc.key 3\nd.key=one,\\\n    ${D_PART}\n  e.key\t=\t5\n")
    jw(tmp_path, "A", '@Value("${a.key}${b.key}${c.key}${d.key}${e.key}") String all;')
    f = by(inv(tmp_path, cfg))
    assert {k: f[k].status for k in ("a.key", "b.key", "c.key", "d.key", "e.key")} == dict.fromkeys(
        ("a.key", "b.key", "c.key", "d.key", "e.key"), "defined")
    assert f["D_PART"].refs[0].line == 4                  # line of the logical entry


def test_test_resources_do_not_define_main_refs(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Value("${only.in.test}") String a;')
    write(tmp_path, "src/test/resources/application.properties", "only.in.test=1\n")
    assert by(inv(tmp_path, cfg))["only.in.test"].status == "external"


def test_relaxed_binding_variants_stay_separate_findings(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Value("${my-key}") String a; @Value("${myKey}") String b; @Value("${MY_KEY}") String c;')
    f = by(inv(tmp_path, cfg))
    assert set(f) == {"my-key", "myKey", "MY_KEY"} and {x.status for x in f.values()} == {"external"}
    write(tmp_path, APP, "my-key=1\n")
    f = by(inv(tmp_path, cfg))
    assert {x.status for x in f.values()} == {"defined"}


def test_configuration_properties_on_bean_method(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "Cfg", '@Bean\n@ConfigurationProperties(prefix = "pool.main")\n'
                        'public DataSource ds() { return null; }')
    write(tmp_path, APP, "pool.main.url=x\n")
    assert by(inv(tmp_path, cfg))["pool.main.*"].status == "defined"


def test_kotlin_and_groovy_sources_are_ignored(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/kotlin/com/acme/A.kt", 'class A { @Value("\\${kt.key}") lateinit var k: String }\n')
    write(tmp_path, "src/main/groovy/com/acme/B.groovy", 'class B { @Value(\'${gr.key}\') String k }\n')
    assert inv(tmp_path, cfg).findings == []
