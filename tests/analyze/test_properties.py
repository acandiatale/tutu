"""Round 8: externalized-config inventory (sbmig.analyze.properties)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.analyze import analyze_path, load_rules, scan_properties
from sbmig.analyze.analyzer import PathView
from sbmig.analyze.pom_parser import parse_poms
from sbmig.analyze.properties import iter_placeholders, norm_key, parse_properties
from sbmig.analyze.source_scanner import scan_sources
from tests.analyze.helpers import pom, write

REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "knowledge" / "config" / "externalized.yml"

BEANS = ('<beans xmlns="http://www.springframework.org/schema/beans" '
         'xmlns:context="http://www.springframework.org/schema/context" '
         'xmlns:jee="http://www.springframework.org/schema/jee">\n{body}\n</beans>\n')


def _java(pkg: str, name: str, body: str, imports: str = "", head: str = "") -> str:
    return f"package {pkg};\n{imports}\n{head}\npublic class {name} {{\n{body}\n}}\n"


def jw(root: Path, name: str, body: str, *, test: bool = False, imports: str = "", head: str = "") -> None:
    kind = "test" if test else "main"
    write(root, f"src/{kind}/java/com/acme/{name}.java", _java("com.acme", name, body, imports, head))


def inv(root: Path, cfg, rules_path: Path = RULES):
    if not (root / "pom.xml").exists():
        write(root, "pom.xml", pom("app"))
    ws = PathView(root)
    poms, _ = parse_poms(ws)
    return scan_properties(ws, poms, scan_sources(ws), load_rules(rules_path), cfg)


def by(inventory) -> dict:
    return {f.key: f for f in inventory.findings}


# ---------------------------------------------------------------------------
# unit helpers
# ---------------------------------------------------------------------------
def test_iter_placeholders_nested_and_default() -> None:
    got = [(k, d) for k, d, _ in iter_placeholders("x ${a} ${b:1} ${c:${d:z}} #{'${e}'}")]
    assert got == [("a", None), ("b", "1"), ("c", "${d:z}"), ("d", "z"), ("e", None)]


def test_parse_properties_forms() -> None:
    text = "# c\n! c\na=1\nb : 2\nc 3\nlong=one \\\n   two\nesc\\:key=v\nempty=\n"
    assert [(k, v) for k, v, _ in parse_properties(text)] == [
        ("a", "1"), ("b", "2"), ("c", "3"), ("long", "one two"), ("esc:key", "v"), ("empty", "")]
    assert [ln for *_, ln in parse_properties(text)] == [3, 4, 5, 6, 8, 9]


def test_norm_key_relaxed() -> None:
    assert norm_key("db.url") == norm_key("DB_URL") == norm_key("db-url") == norm_key("Db.Url")


# ---------------------------------------------------------------------------
# java sources
# ---------------------------------------------------------------------------
def test_value_annotation_defaults_and_spel(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Value("${db.url}") String url;\n'
                      '@Value("${pool.size:10}") int size;\n'
                      '@Value("${host}:${port:80}") String hp;\n'
                      "@Value(\"#{'${list.items}'.split(',')}\") java.util.List<String> items;\n"
                      '@Value("#{systemProperties[\'x\']}") String spel;\n'
                      '// @Value("${commented.out}") String c;\n'
                      '@Scheduled(cron = "${job.cron}") void run() {}')
    f = by(inv(tmp_path, cfg))
    assert set(f) == {"db.url", "pool.size", "host", "port", "list.items", "job.cron"}
    assert f["db.url"].status == "external" and f["db.url"].boot_keys == ["db.url"]
    r = f["db.url"].refs[0]
    assert r.source == "value-annotation" and r.line == 5 and r.scope == "main" and r.file.endswith("A.java")
    assert "${db.url}" in r.raw and "\n" not in r.raw
    assert f["pool.size"].status == "defaulted" and f["pool.size"].refs[0].default == "10"
    assert f["port"].status == "defaulted" and f["host"].status == "external"


def test_configuration_properties_prefix(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "P1", "", head='@ConfigurationProperties("app.mail")')
    jw(tmp_path, "P2", "", head='@ConfigurationProperties(prefix = "app.cache")')
    jw(tmp_path, "P3", "", head='@ConfigurationProperties(value = "other")')
    write(tmp_path, "src/main/resources/application.yml", "app:\n  mail:\n    host: smtp\n")
    f = by(inv(tmp_path, cfg))
    assert {k: v.status for k, v in f.items()} == {"app.mail.*": "defined", "app.cache.*": "external",
                                                    "other.*": "external"}
    assert f["app.mail.*"].refs[0].source == "configuration-properties"
    assert f["app.mail.*"].defined_in == ["src/main/resources/application.yml"]
    assert f["app.cache.*"].boot_keys == []


def test_prefix_needs_segment_boundary(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "P", "", head='@ConfigurationProperties("app")')
    write(tmp_path, "src/main/resources/application.properties", "application.name=x\n")
    assert by(inv(tmp_path, cfg))["app.*"].status == "external"


def test_environment_calls(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "E", "@Autowired Environment environment;\nprivate final PropertyResolver res = null;\n"
                      'void a() { environment.getProperty("e.one"); }\n'
                      'void b() { env.getRequiredProperty("e.two"); }\n'
                      'void c() { res.containsProperty("e.three"); }\n'
                      'void d() { ctx.getEnvironment().getProperty("e.four", "dflt"); }\n'
                      'void e() { environment.getProperty("e.five", Integer.class, 5); }\n'
                      'void f() { props.getProperty("not.env"); }\n',
       imports="import org.springframework.core.env.Environment;")
    f = by(inv(tmp_path, cfg))
    assert set(f) == {"e.one", "e.two", "e.three", "e.four", "e.five"}
    assert all(x.refs[0].source == "environment" for x in f.values())
    assert f["e.four"].refs[0].default == "dflt" and f["e.four"].status == "defaulted"
    assert f["e.five"].refs[0].default == "5"
    assert f["e.one"].status == "external"


def test_system_property_and_env(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "S", 'String a = System.getProperty("app.home");\n'
                      'String b = System.getProperty("app.mode", "dev");\n'
                      'String c = System.getProperty("catalina.base");\n'
                      'String d = System.getenv("DB_PASSWORD");\n')
    i = inv(tmp_path, cfg)
    f = by(i)
    assert set(f) == {"app.home", "app.mode", "DB_PASSWORD"}
    assert f["app.home"].refs[0].source == "system-property" and f["app.home"].supplied_by == "system-property"
    assert f["app.home"].status == "external" and f["app.home"].boot_keys == []   # -D only: no placeholder
    assert f["app.mode"].status == "defaulted"
    assert f["DB_PASSWORD"].refs[0].source == "system-env" and f["DB_PASSWORD"].boot_keys == []
    assert "environment variable" in f["DB_PASSWORD"].note
    assert any("catalina.base" in n for n in i.notes)


def test_jndi_java_and_rule_mapping(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "J", '@Resource(lookup = "java:comp/env/jdbc/orderDS") DataSource ds;\n'
                      'Object x() throws Exception { Context c = new InitialContext();\n'
                      '  c.lookup("mail/Session"); return c.lookup("java:comp/env"); }\n'
                      'Object y() throws Exception { return new InitialContext().lookup("java:comp/env/app/flag"); }\n',
       imports="import javax.naming.InitialContext;")
    f = by(inv(tmp_path, cfg))
    assert set(f) == {"java:comp/env/jdbc/orderDS", "java:comp/env/mail/Session", "java:comp/env/app/flag"}
    ds = f["java:comp/env/jdbc/orderDS"]
    assert ds.refs[0].source == "jndi" and ds.supplied_by == "jndi" and ds.status == "external"
    assert ds.boot_keys == ["spring.datasource.url", "spring.datasource.username", "spring.datasource.password",
                            "spring.datasource.driver-class-name"]
    assert "java:comp/env/jdbc/orderDS" in ds.note
    assert f["java:comp/env/mail/Session"].boot_keys[0] == "spring.mail.host"
    assert f["java:comp/env/app/flag"].boot_keys == ["flag"]


def test_jndi_datasource_defined_when_boot_keys_present(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "J", 'String n = "java:comp/env/jdbc/x";')
    write(tmp_path, "src/main/resources/application.properties",
          "spring.datasource.url=jdbc:h2:mem:x\nspring.datasource.username=sa\nspring.datasource.password=\n"
          "spring.datasource.driver-class-name=org.h2.Driver\n")
    assert by(inv(tmp_path, cfg))["java:comp/env/jdbc/x"].status == "defined"


def test_property_source_classpath_and_file(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "C", "", head='@PropertySource("classpath:props/app.properties")\n'
                               '@PropertySource(value = {"file:/etc/app/ext.properties", '
                               '"file:${conf.dir}/more.properties"}, ignoreResourceNotFound = true)')
    jw(tmp_path, "U", '@Value("${app.title}") String t; @Value("${ext.only}") String e;')
    write(tmp_path, "src/main/resources/props/app.properties", "app.title=Shop\n")
    write(tmp_path, "src/main/resources/props/unrelated.properties", "ext.only=nope\n")
    i = inv(tmp_path, cfg)
    f = by(i)
    assert f["app.title"].status == "defined"
    assert f["app.title"].defined_in == ["src/main/resources/props/app.properties"]
    assert "src/main/resources/props/app.properties" in i.packaged_files
    assert "src/main/resources/props/unrelated.properties" not in i.packaged_files
    assert f["ext.only"].status == "external" and f["ext.only"].supplied_by == "property-source:file"
    assert f["conf.dir"].refs[0].source == "property-source"
    assert any("file:/etc/app/ext.properties" in n for n in i.notes)


def test_profiles_refs_and_files(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "M", 'void m() { System.setProperty("spring.profiles.active", "local"); }')
    write(tmp_path, "src/main/resources/application.yml",
          "app:\n  name: x\n---\nspring:\n  config:\n    activate:\n      on-profile: qa\napp:\n  qa-only: 1\n"
          "---\nspring:\n  profiles: legacy\n")
    write(tmp_path, "src/main/resources/application-prod.yml", "app:\n  prod-only: 1\n")
    jw(tmp_path, "V", '@Value("${app.prod-only}") String p; @Value("${app.qa-only}") String q;')
    i = inv(tmp_path, cfg)
    assert i.profiles == ["legacy", "local", "prod", "qa"]
    f = by(i)
    assert f["spring.profiles.active"].refs[0].source == "profiles"
    assert f["app.prod-only"].status == "defined"
    assert f["app.prod-only"].defined_in == ["src/main/resources/application-prod.yml"]
    assert f["app.qa-only"].status == "defined"


# ---------------------------------------------------------------------------
# scopes, relaxed binding
# ---------------------------------------------------------------------------
def test_test_scope_and_merge(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Value("${shared.key}") String s;')
    jw(tmp_path, "ATest", '@Value("${shared.key}") String s; @Value("${test.only}") String t;\n'
                          '@Value("${test.res}") String r; @Value("${test.inline}") String i;', test=True,
       head='@SpringBootTest(properties = {"test.inline=1"})')
    write(tmp_path, "src/test/resources/application.properties", "test.res=1\nshared.key=1\n")
    f = by(inv(tmp_path, cfg))
    assert [r.scope for r in f["shared.key"].refs] == ["main", "test"]
    assert f["shared.key"].status == "external"         # main ref: test resources do not count
    assert f["test.only"].refs[0].scope == "test" and f["test.only"].status == "external"
    assert f["test.res"].status == "defined" and f["test.inline"].status == "defined"


@pytest.mark.parametrize("defined", ["db.url=x", "DB_URL=x", "db-url=x"])
def test_relaxed_binding(tmp_path: Path, cfg, defined: str) -> None:
    jw(tmp_path, "A", '@Value("${db.url}") String a; @Value("${DB_URL}") String b; @Value("${db-url}") String c;')
    write(tmp_path, "src/main/resources/application.properties", defined + "\n")
    f = by(inv(tmp_path, cfg))
    assert {f[k].status for k in ("db.url", "DB_URL", "db-url")} == {"defined"}


def test_config_subdir_and_placeholder_in_value(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Value("${spring.datasource.url}") String u;')
    write(tmp_path, "src/main/resources/config/application.properties",
          "spring.datasource.url=${DB_URL}\nlog.dir=${user.home}/logs\nx=${opt:1}\n")
    f = by(inv(tmp_path, cfg))
    assert f["spring.datasource.url"].status == "defined"
    assert f["DB_URL"].status == "external" and f["DB_URL"].refs[0].source == "config-placeholder"
    assert f["DB_URL"].refs[0].line == 1
    assert "user.home" not in f and f["opt"].status == "defaulted"


# ---------------------------------------------------------------------------
# maven filtering
# ---------------------------------------------------------------------------
def test_maven_filtering(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("app", props={"build.env": "dev"}).replace(
        "</project>", "<build><resources><resource><directory>src/main/resources</directory>"
                      "<filtering>true</filtering></resource></resources></build></project>"))
    write(tmp_path, "src/main/resources/application.properties",
          "app.env=${build.env}\napp.version=${project.version}\napp.secret=${SECRET}\n")
    write(tmp_path, "src/main/resources/ctx.xml",
          BEANS.format(body='<bean id="a" class="A"><property name="e" value="${build.env}"/></bean>'))
    jw(tmp_path, "A", '@Value("${app.env}") String e;')
    i = inv(tmp_path, cfg)
    f = by(i)
    assert i.filtered_resources == ["src/main/resources"]
    assert "build.env" not in f and "project.version" not in f
    assert f["app.env"].status == "defined"
    assert f["SECRET"].status == "external"             # not a pom property: Maven leaves it for Spring


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------
def test_xml_bean_placeholders_and_env(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/resources/spring/root.xml", BEANS.format(body=(
        '<context:property-placeholder location="classpath:conf/db.properties,'
        'file:${catalina.base}/conf/app.properties"/>\n'
        '<bean id="ds" class="X">\n  <property name="url" value="${jdbc.url}"/>\n'
        '  <property name="user" value="${jdbc.user:sa}"/>\n  <property name="pw"><value>${env.DB_PW}</value></property>\n'
        '  <property name="v" value="${pom.version}"/>\n</bean>\n<!-- <property value="${commented}"/> -->')))
    write(tmp_path, "src/main/resources/conf/db.properties", "jdbc.url=jdbc:h2:mem:x\n")
    write(tmp_path, "src/main/resources/mapper/OrderMapper.xml",
          '<!DOCTYPE mapper><mapper namespace="x"><select id="a">order by ${sort}</select></mapper>')
    i = inv(tmp_path, cfg)
    f = by(i)
    assert set(f) == {"jdbc.url", "jdbc.user", "DB_PW"}
    assert f["jdbc.url"].status == "defined" and f["jdbc.url"].refs[0].source == "xml-placeholder"
    assert f["jdbc.url"].refs[0].line == 4
    assert f["jdbc.user"].status == "defaulted"
    assert f["DB_PW"].refs[0].source == "system-env"
    assert "src/main/resources/conf/db.properties" in i.packaged_files
    assert any("catalina.base" in n and "property-source:file" in n for n in i.notes)


def test_web_xml_context_params(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/webapp/WEB-INF/web.xml", """<web-app>
  <context-param><param-name>contextConfigLocation</param-name><param-value>/WEB-INF/root.xml</param-value></context-param>
  <context-param>
    <param-name>spring.profiles.active</param-name>
    <param-value>prod</param-value>
  </context-param>
  <context-param><param-name>siteCode</param-name><param-value>  KR01 </param-value></context-param>
  <resource-ref><res-ref-name>jdbc/mainDS</res-ref-name><res-type>javax.sql.DataSource</res-type></resource-ref>
  <env-entry><env-entry-name>app/region</env-entry-name><env-entry-value>ap-ne-2</env-entry-value></env-entry>
</web-app>""")
    i = inv(tmp_path, cfg)
    f = by(i)
    cc = f["contextConfigLocation"]
    assert cc.boot_keys == [] and cc.supplied_by == "context-param" and "ImportResource" in cc.note
    assert f["spring.profiles.active"].boot_keys == ["spring.profiles.active"]
    site = f["siteCode"]
    assert site.status == "external" and site.boot_keys == ["server.servlet.context-parameters.siteCode"]
    assert site.refs[0].source == "context-param" and site.refs[0].default == "KR01" and site.refs[0].line == 7
    assert f["java:comp/env/jdbc/mainDS"].boot_keys[0] == "spring.datasource.url"
    region = f["java:comp/env/app/region"]
    assert region.boot_keys == ["region"] and region.refs[0].default == "ap-ne-2" and region.status == "external"
    assert "prod" in i.profiles


def test_xml_jndi_forms_and_context_xml(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/webapp/WEB-INF/spring/ctx.xml", BEANS.format(body=(
        '<jee:jndi-lookup id="ds" jndi-name="jdbc/orderDS" resource-ref="true"/>\n'
        '<bean id="q" class="org.springframework.jndi.JndiObjectFactoryBean">\n'
        '  <property name="jndiName" value="java:comp/env/jms/queue"/>\n</bean>')))
    write(tmp_path, "src/main/webapp/META-INF/context.xml", '<Context>\n'
          '<Resource name="jdbc/orderDS" auth="Container" url="jdbc:oracle:thin:@db:1521/X" password="s3cr3t"/>\n'
          '<Environment name="app/mode" value="live" type="java.lang.String"/>\n</Context>')
    f = by(inv(tmp_path, cfg))
    ds = f["java:comp/env/jdbc/orderDS"]
    assert {r.file.rsplit("/", 1)[-1] for r in ds.refs} == {"ctx.xml", "context.xml"}
    ctx_ref = next(r for r in ds.refs if r.file.endswith("context.xml"))
    assert "s3cr3t" not in ctx_ref.raw and ctx_ref.default == "jdbc:oracle:thin:@db:1521/X"
    assert f["java:comp/env/jms/queue"].boot_keys == [] and "JMS" in f["java:comp/env/jms/queue"].note
    assert f["java:comp/env/app/mode"].refs[0].default == "live"


def test_web_xml_placeholder_supplied_by_system_property(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/webapp/WEB-INF/web.xml",
          "<web-app><context-param><param-name>p</param-name><param-value>${site.url}</param-value>"
          "</context-param></web-app>")
    f = by(inv(tmp_path, cfg))
    assert f["site.url"].refs[0].source == "xml-placeholder" and f["site.url"].supplied_by == "system-property"


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def test_boot_defaults(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "A", '@Value("${server.port}") int port; @Value("${spring.application.name}") String n;')
    f = by(inv(tmp_path, cfg))
    assert f["server.port"].boot_keys == [] and f["server.port"].note.startswith("Boot default")
    assert f["spring.application.name"].boot_keys == []


def test_rules_file_missing(tmp_path: Path, cfg) -> None:
    r = load_rules(tmp_path / "nope.yml")
    assert r.jndi == [] and r.context_param_known == {} and r.notes and "not found" in r.notes[0]
    jw(tmp_path, "J", 'String n = "java:comp/env/jdbc/x";')
    i = inv(tmp_path, cfg, tmp_path / "nope.yml")
    assert any("not found" in n for n in i.notes)
    assert by(i)["java:comp/env/jdbc/x"].boot_keys == []


def test_rules_file_broken(tmp_path: Path) -> None:
    (tmp_path / "bad.yml").write_text("jndi: [\n", encoding="utf-8")
    assert "unreadable" in load_rules(tmp_path / "bad.yml").notes[0]


def test_load_repo_rules() -> None:
    r = load_rules(RULES)
    assert r.jndi[0].match == "java:comp/env/jdbc/*" and r.context_param_known["contextConfigLocation"].skip
    assert "catalina.home" in r.system_property_skip and "server.port" in r.boot_defaults and r.guide


# ---------------------------------------------------------------------------
# robustness + wiring
# ---------------------------------------------------------------------------
def test_broken_yaml_is_a_note(tmp_path: Path, cfg) -> None:
    write(tmp_path, "src/main/resources/application.yml", "a: [unclosed\n  b: :\n")
    jw(tmp_path, "A", '@Value("${a}") String a;')
    i = inv(tmp_path, cfg)
    assert any("unparsable YAML" in n for n in i.notes)
    assert by(i)["a"].status == "external"


def test_skips_target_and_large_files(tmp_path: Path, cfg) -> None:
    write(tmp_path, "target/classes/application.properties", "x=1\n")
    write(tmp_path, "src/main/resources/application.properties", "#" * 1_100_000 + "\nx=1\n")
    jw(tmp_path, "A", '@Value("${x}") String x;')
    i = inv(tmp_path, cfg)
    assert by(i)["x"].status == "external"
    assert any("larger than 1 MB" in n for n in i.notes)


def test_findings_sorted(tmp_path: Path, cfg) -> None:
    jw(tmp_path, "B", '@Value("${zeta}") String z;\n@Value("${alpha}") String a;')
    jw(tmp_path, "A", '@Value("${zeta}") String z;')
    f = inv(tmp_path, cfg).findings
    assert [x.key for x in f] == ["alpha", "zeta"]
    assert [r.file.rsplit("/", 1)[-1] for r in f[1].refs] == ["A.java", "B.java"]


def test_analyze_project_wiring(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("app"))
    jw(tmp_path, "A", '@Value("${db.url}") String u;')
    p = analyze_path(tmp_path, cfg)
    assert [f.key for f in p.properties.external] == ["db.url"]
    cfg.externalized.enabled = False
    assert analyze_path(tmp_path, cfg).properties.findings == []


def test_filtered_binary_resources(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("app").replace(
        "</project>", "<build><resources><resource><directory>src/main/resources</directory>"
                      "<filtering>true</filtering><excludes><exclude>skip/**</exclude></excludes></resource>"
                      "<resource><directory>src/main/raw</directory><filtering>false</filtering></resource>"
                      "</resources></build></project>"))
    for rel in ("src/main/resources/static/fonts/fa.woff2", "src/main/resources/cert/app.jks",
                "src/main/resources/skip/x.ttf", "src/main/resources/logo.png", "src/main/raw/other.eot"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00\x01binary")
    write(tmp_path, "src/main/resources/application.properties", "a=1\n")
    i = inv(tmp_path, cfg)
    assert i.filtered_resources == ["src/main/resources"]
    assert i.filtered_binary_files == ["src/main/resources/cert/app.jks", "src/main/resources/static/fonts/fa.woff2"]


def test_filtered_binary_resources_includes_and_copy_resources(tmp_path: Path, cfg) -> None:
    write(tmp_path, "pom.xml", pom("app").replace(
        "</project>",
        "<build><resources><resource><directory>src/main/resources</directory><filtering>true</filtering>"
        "<includes><include>**/*.properties</include><include>*.ttf</include></includes></resource></resources>"
        "<plugins><plugin><artifactId>maven-resources-plugin</artifactId><executions><execution><id>c</id>"
        "<goals><goal>copy-resources</goal></goals><configuration><resources><resource>"
        "<directory>${basedir}/dist</directory><filtering>true</filtering></resource></resources>"
        "</configuration></execution></executions></plugin></plugins></build></project>"))
    for rel in ("src/main/resources/static/fonts/fa.woff2", "src/main/resources/top.ttf",
                "src/main/resources/deep/d.ttf", "dist/bundle.zip"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00")
    assert inv(tmp_path, cfg).filtered_binary_files == ["dist/bundle.zip", "src/main/resources/top.ttf"]  # `*.ttf` = top level


def test_filtered_binary_resources_sniff_unknown_extensions(tmp_path: Path, cfg) -> None:
    """Company run 2026-10-26: `static/img/.dm/.items.dmdb` (no table extension) failed the filtered copy. Unknown
    extensions count when the content is binary (NUL / neither UTF-8 nor cp949); text never does — a cp949
    .properties, a UTF-8 file of an odd extension, a known text extension with stray bytes."""
    write(tmp_path, "pom.xml", pom("app").replace(
        "</project>", "<build><resources><resource><directory>src/main/resources</directory>"
                      "<filtering>true</filtering></resource></resources></build></project>"))
    base = tmp_path / "src/main/resources"
    (base / "static/img/.dm").mkdir(parents=True)
    (base / "static/img/.dm/.items.dmdb").write_bytes(bytes([0x53, 0x51, 0x4C, 0xFF, 0xFE, 0x00, 0x81]) * 40)
    (base / "static/img/.dm/raw.idx").write_bytes(bytes([0xFF, 0xFE, 0xFD, 0x80, 0x81]) * 40)   # no NUL, not text
    (base / "messages_ko.properties").write_bytes("title=회원 목록\n".encode("cp949"))
    (base / "notes.abc").write_text("plain text\n", encoding="utf-8")
    (base / "legacy.sql").write_bytes(b"select 1;\x00\n")                                  # text extension: kept
    i = inv(tmp_path, cfg)
    assert i.filtered_binary_files == ["src/main/resources/static/img/.dm/.items.dmdb",
                                       "src/main/resources/static/img/.dm/raw.idx"]
