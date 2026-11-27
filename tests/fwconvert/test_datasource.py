"""read_datasource / scan_datasource: section 2.6 of docs/FW-CONVERT.md (datasource bean, placeholders, vendor, JNDI,
several beans, the password never carried)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sbmig.fwconvert.datasource import parse_properties, read_datasource, scan_datasource, vendor_of
from sbmig.fwconvert.rules import load_rules

RULES = load_rules("Example", {"naming": {"package_root": "com.example"}})
SECRET = "s3cr3t-Value"

BEANS_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<beans xmlns="http://www.springframework.org/schema/beans"\n'
              '       xmlns:p="http://www.springframework.org/schema/p"\n'
              '       xmlns:jee="http://www.springframework.org/schema/jee">\n')


def _write(root: Path, files: dict[str, str | bytes]) -> list[str]:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body, encoding="utf-8")
    return sorted(files)


def _all_text(obj) -> str:
    return repr(obj)


def test_a_dbcp_bean_with_placeholders_from_a_properties_file(tmp_path):
    files = _write(tmp_path, {
        "src/main/resources/applicationContext.xml": BEANS_HEAD + """
  <bean id="dataSource" class="org.apache.commons.dbcp2.BasicDataSource" destroy-method="close">
    <property name="driverClassName" value="${db.driver}"/>
    <property name="url" value="${db.url}"/>
    <property name="username" value="${db.user}"/>
    <property name="password" value="${db.password}"/>
  </bean>
</beans>""",
        "src/main/resources/db.properties": f"db.driver=oracle.jdbc.OracleDriver\n"
                                            f"db.url=jdbc:oracle:thin:@${{db.host}}:1521:ORCL\n"
                                            f"db.host=dbhost.invalid\ndb.user=app\ndb.password={SECRET}\n",
    })
    scan = scan_datasource(tmp_path, files, RULES)
    info = scan.info
    assert info.configured and info.reason == ""
    assert info.vendor == "oracle"
    assert info.url == "jdbc:oracle:thin:@dbhost.invalid:1521:ORCL"
    assert info.driver_class == "oracle.jdbc.OracleDriver" and info.username == "app"
    assert info.has_password
    assert info.source == "src/main/resources/applicationContext.xml"
    assert scan.password_files == ["src/main/resources/applicationContext.xml", "src/main/resources/db.properties"]
    assert SECRET not in _all_text(scan)


def test_p_namespace_attributes_and_a_value_element(tmp_path):
    files = _write(tmp_path, {
        "WEB-INF/config/datasource-context.xml": BEANS_HEAD + """
  <bean id="ds" class="com.zaxxer.hikari.HikariDataSource"
        p:jdbcUrl="jdbc:postgresql://db.invalid:5432/app" p:driverClassName="org.postgresql.Driver">
    <property name="username"><value>reader</value></property>
  </bean>
</beans>"""})
    info = read_datasource(tmp_path, files, RULES)
    assert info.configured and info.vendor == "postgresql"
    assert info.url == "jdbc:postgresql://db.invalid:5432/app" and info.username == "reader"
    assert not info.has_password


def test_the_vendor_comes_from_the_driver_when_the_url_does_not_say(tmp_path):
    assert vendor_of("jdbc:log4jdbc:mysql://h/db", "") == "mysql"
    assert vendor_of("jdbc:odbc:x", "com.tmax.tibero.jdbc.TbDriver") == "tibero"
    assert vendor_of("jdbc:odbc:x", "sun.jdbc.odbc.JdbcOdbcDriver") == ""
    for url, vendor in [("jdbc:mariadb://h/d", "mariadb"), ("jdbc:sqlserver://h;databaseName=d", "sqlserver"),
                        ("jdbc:h2:mem:t", "h2"), ("jdbc:tibero:thin:@h:8629:d", "tibero")]:
        assert vendor_of(url, "") == vendor


def test_an_unknown_vendor_is_not_configured(tmp_path):
    files = _write(tmp_path, {"applicationContext.xml": BEANS_HEAD + """
  <bean id="ds" class="x.CustomDataSource"><property name="url" value="jdbc:odbc:legacy"/></bean>
</beans>"""})
    info = read_datasource(tmp_path, files, RULES)
    assert not info.configured and info.reason == "unknown_vendor"


def test_a_jndi_lookup_is_not_configured(tmp_path):
    files = _write(tmp_path, {"applicationContext.xml": BEANS_HEAD + """
  <jee:jndi-lookup id="dataSource" jndi-name="jdbc/app"/>
</beans>"""})
    info = read_datasource(tmp_path, files, RULES)
    assert not info.configured and info.reason == "jndi" and info.source == "applicationContext.xml"


def test_no_datasource_at_all(tmp_path):
    files = _write(tmp_path, {"applicationContext.xml": BEANS_HEAD + "<bean id='x' class='a.B'/></beans>",
                              "app.properties": "a=b\n"})
    info = read_datasource(tmp_path, files, RULES)
    assert not info.configured and info.reason == "not_found"
    assert read_datasource(tmp_path, [], RULES).reason == "not_found"


def test_a_proxy_without_a_url_is_not_a_datasource_and_the_first_real_one_wins(tmp_path):
    files = _write(tmp_path, {
        "a-context.xml": BEANS_HEAD + """
  <bean id="proxy" class="org.springframework.jdbc.datasource.TransactionAwareDataSourceProxy"/>
  <bean id="lazy" class="org.springframework.jdbc.datasource.LazyConnectionDataSource"/>
  <bean id="main" class="org.springframework.jdbc.datasource.DriverManagerDataSource">
    <property name="url" value="jdbc:mysql://one.invalid/app"/>
  </bean>
</beans>""",
        "b-context.xml": BEANS_HEAD + """
  <bean id="second" class="org.apache.commons.dbcp.BasicDataSource">
    <property name="url" value="jdbc:mariadb://two.invalid/app"/>
  </bean>
</beans>"""})
    scan = scan_datasource(tmp_path, files, RULES)
    assert scan.info.configured and scan.info.url == "jdbc:mysql://one.invalid/app"
    assert scan.info.source == "a-context.xml"
    assert len(scan.notes) == 1 and "b-context.xml#second" in scan.notes[0] and "a-context.xml#main" in scan.notes[0]


def test_an_unresolved_placeholder_in_the_url_is_not_configured(tmp_path):
    files = _write(tmp_path, {"applicationContext.xml": BEANS_HEAD + """
  <bean id="ds" class="a.BasicDataSource">
    <property name="url" value="${JDBC_URL}"/><property name="username" value="${who}"/>
  </bean>
</beans>"""})
    scan = scan_datasource(tmp_path, files, RULES)
    assert not scan.info.configured and scan.info.reason == "unresolved"
    assert scan.info.username == ""
    assert any("placeholder" in n for n in scan.notes)


def test_a_default_in_a_placeholder_and_the_five_round_limit(tmp_path):
    chain = "".join(f"k{i}=${{k{i + 1}}}\n" for i in range(10)) + "k10=jdbc:h2:mem:deep\n"
    files = _write(tmp_path, {
        "applicationContext.xml": BEANS_HEAD + """
  <bean id="ds" class="a.BasicDataSource"><property name="url" value="${missing:jdbc:h2:mem:fallback}"/></bean>
  <bean id="deep" class="a.BasicDataSource"><property name="url" value="${k0}"/></bean>
</beans>""",
        "c.properties": chain})
    scan = scan_datasource(tmp_path, files, RULES)
    assert scan.info.url == "jdbc:h2:mem:fallback" and scan.info.vendor == "h2"
    _write(tmp_path, {"only-deep.xml": BEANS_HEAD + """
  <bean id="deep" class="a.BasicDataSource"><property name="url" value="${k0}"/></bean></beans>"""})
    deep = scan_datasource(tmp_path, ["c.properties", "only-deep.xml"], RULES)
    # ten levels of indirection are more than five rounds: the URL stays unresolved
    assert deep.info.reason == "unresolved"


def test_a_cp949_properties_file_and_unicode_escapes(tmp_path):
    files = _write(tmp_path, {
        "applicationContext.xml": BEANS_HEAD + """
  <bean id="ds" class="a.BasicDataSource">
    <property name="url" value="${url}"/><property name="username" value="${user}"/>
  </bean>
</beans>""",
        "ko.properties": "# 데이터베이스 설정\nurl = jdbc:postgresql://h.invalid/앱\n".encode("cp949")
                         + b"user : \\uC0AC\\uC6A9\\uC790\n"})
    info = read_datasource(tmp_path, files, RULES)
    assert info.url == "jdbc:postgresql://h.invalid/앱" and info.username == "사용자"


def test_parse_properties_follows_the_java_rules():
    text = "a=1\n! comment\n# comment\nb : two \\\n    lines\nc  three\nkey\\ with\\ space=v\nempty=\nlast\\\n"
    got = parse_properties(text)
    assert got == {"a": "1", "b": "two lines", "c": "three", "key with space": "v", "empty": "", "last": ""}


def test_a_password_in_the_url_is_taken_out(tmp_path):
    files = _write(tmp_path, {"applicationContext.xml": BEANS_HEAD + f"""
  <bean id="ds" class="a.BasicDataSource">
    <property name="url" value="jdbc:oracle:thin:scott/{SECRET}@db.invalid:1521:ORCL"/>
  </bean>
  <bean id="ds2" class="a.BasicDataSource">
    <property name="url" value="jdbc:mysql://h.invalid/app?password={SECRET}&amp;useSSL=false"/>
  </bean>
</beans>"""})
    scan = scan_datasource(tmp_path, files, RULES)
    assert scan.info.url == "jdbc:oracle:thin:@db.invalid:1521:ORCL"
    assert scan.info.username == "scott" and scan.info.has_password
    assert SECRET not in _all_text(scan)
    _write(tmp_path, {"applicationContext.xml": BEANS_HEAD + f"""
  <bean id="ds2" class="a.BasicDataSource">
    <property name="url" value="jdbc:mysql://h.invalid/app?password={SECRET}&amp;useSSL=false"/>
  </bean></beans>"""})
    scan = scan_datasource(tmp_path, files, RULES)
    assert scan.info.url == "jdbc:mysql://h.invalid/app?useSSL=false" and scan.info.has_password
    assert SECRET not in _all_text(scan)


def test_bad_files_never_raise(tmp_path):
    files = _write(tmp_path, {
        "broken-context.xml": "<beans><bean class='a.BasicDataSource'>",
        "entity-context.xml": '<?xml version="1.0"?>\n<!DOCTYPE beans [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
                              '<beans><bean id="e" class="a.BasicDataSource"><property name="url" value="&x;"/>'
                              '</bean></beans>',
        "big-datasource.xml": "<beans>" + " " * (4 * 1024 * 1024 + 10) + "</beans>",
    })
    scan = scan_datasource(tmp_path, files + ["../outside.xml", "/abs.xml", "missing.xml"], RULES)
    assert not scan.info.configured
    assert any("broken-context.xml" in n for n in scan.notes)
    assert any("big-datasource.xml" in n and "larger" in n for n in scan.notes)
    assert any("../outside.xml" in n for n in scan.notes) and any("missing.xml" in n for n in scan.notes)
    assert "root:" not in _all_text(scan)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_a_named_pipe_and_a_link_leaving_the_root_are_not_read(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    os.mkfifo(root / "pipe-context.xml")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x-context.xml").write_text(BEANS_HEAD + """
  <bean id="ds" class="a.BasicDataSource"><property name="url" value="jdbc:h2:mem:x"/></bean></beans>""")
    (root / "linked").symlink_to(outside)
    scan = scan_datasource(root, ["pipe-context.xml", "linked/x-context.xml"], RULES)
    assert not scan.info.configured
    assert any("pipe-context.xml" in n and "regular" in n for n in scan.notes)
    assert any("linked/x-context.xml" in n and "outside" in n for n in scan.notes)


# ---- credentials inside the JDBC URL, for every vendor and wrapper -------------------------------------------------

CTX = BEANS_HEAD + """
  <bean id="dataSource" class="org.apache.commons.dbcp2.BasicDataSource">
    <property name="url" value="{url}"/>
  </bean>
</beans>"""
PW = "S3cr3tPw"
STRIPPED = {
    # case: (url, the URL that may be written, the user name found in it)
    "log4jdbc_oracle": (f"jdbc:log4jdbc:oracle:thin:scott/{PW}@dbhost:1521:ORCL",
                        "jdbc:log4jdbc:oracle:thin:@dbhost:1521:ORCL", "scott"),
    "p6spy_oracle": (f"jdbc:p6spy:oracle:thin:scott/{PW}@dbhost:1521:ORCL",
                     "jdbc:p6spy:oracle:thin:@dbhost:1521:ORCL", "scott"),
    "mysql_userinfo": (f"jdbc:mysql://root:{PW}@dbhost:3306/shop", "jdbc:mysql://dbhost:3306/shop", "root"),
    "mariadb_userinfo": (f"jdbc:mariadb://root:{PW}@dbhost:3306/shop", "jdbc:mariadb://dbhost:3306/shop", "root"),
    "postgresql_userinfo": (f"jdbc:postgresql://app:{PW}@dbhost/shop?ssl=true", "jdbc:postgresql://dbhost/shop?ssl=true",
                            "app"),
    "oracle_at_in_password": (f"jdbc:oracle:thin:scott/{PW}@x@dbhost:1521:ORCL", "jdbc:oracle:thin:@dbhost:1521:ORCL",
                              "scott"),
    "tibero_userpass": (f"jdbc:tibero:thin:scott/{PW}@dbhost:8629:tibero", "jdbc:tibero:thin:@dbhost:8629:tibero",
                        "scott"),
    "sqlserver_braced": (f"jdbc:sqlserver://h;databaseName=x;password={{ab;{PW}}}", "jdbc:sqlserver://h;databaseName=x",
                         ""),
    "oracle_upper_prefix": (f"JDBC:ORACLE:THIN:scott/{PW}@dbhost:1521:ORCL", "JDBC:ORACLE:THIN:@dbhost:1521:ORCL",
                            "scott"),
    "oracle_plain": (f"jdbc:oracle:thin:scott/{PW}@dbhost:1521:ORCL", "jdbc:oracle:thin:@dbhost:1521:ORCL", "scott"),
    "mysql_param": (f"jdbc:mysql://dbhost/shop?user=root&password={PW}", "jdbc:mysql://dbhost/shop?user=root", ""),
    "mysql_param_first": (f"jdbc:mysql://dbhost/shop?PWD={PW}&useSSL=false", "jdbc:mysql://dbhost/shop?useSSL=false", ""),
    "quoted_param": (f"jdbc:sqlserver://h;Passwd=\"a;{PW}\";databaseName=x", "jdbc:sqlserver://h;databaseName=x", ""),
    "pass_param": (f"jdbc:postgresql://h/db?pass='{PW}'", "jdbc:postgresql://h/db", ""),
    "oracle_special_chars": (f"jdbc:oracle:thin:scott/a@b/c;d&{PW}@dbhost:1521:ORCL",
                             "jdbc:oracle:thin:@dbhost:1521:ORCL", "scott"),
    "mysql_ipv6_userinfo": (f"jdbc:mysql://app:{PW}@[::1]:3306/shop", "jdbc:mysql://[::1]:3306/shop", "app"),
    "h2_upper_params": (f"jdbc:h2:tcp://db/~/shop;USER=sa;PASSWORD={PW}", "jdbc:h2:tcp://db/~/shop;USER=sa", ""),
    "postgresql_ipv6_params": (f"jdbc:postgresql://[::1]:5432/shop?user=app&password={PW}",
                               "jdbc:postgresql://[::1]:5432/shop?user=app", ""),
    "sqlserver_braced_first": (f"jdbc:sqlserver://db:1433;databaseName=shop;user=sa;password={{{PW};x}}",
                               "jdbc:sqlserver://db:1433;databaseName=shop;user=sa", ""),
}


@pytest.mark.parametrize("name", sorted(STRIPPED))
def test_a_password_in_the_url_never_survives(tmp_path, name):
    url, kept, user = STRIPPED[name]
    _write(tmp_path, {"ctx.xml": CTX.format(url=url.replace("&", "&amp;").replace('"', "&quot;"))})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert PW not in repr(scan), scan.info
    assert scan.info.has_password and scan.password_files == ["ctx.xml"]
    assert scan.info.url == kept and scan.info.configured
    assert scan.info.username == user


def test_a_tns_descriptor_and_a_url_with_a_user_only_are_kept(tmp_path):
    tns = "jdbc:oracle:thin:@(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db.invalid)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME=X)))"
    _write(tmp_path, {"ctx.xml": CTX.format(url=tns)})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert scan.info.configured and scan.info.url == tns and not scan.info.has_password
    _write(tmp_path, {"ctx.xml": CTX.format(url="jdbc:mysql://dbhost/shop?user=root&amp;useSSL=false")})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert scan.info.configured and scan.info.url == "jdbc:mysql://dbhost/shop?user=root&useSSL=false"
    assert not scan.info.has_password and scan.password_files == []
    _write(tmp_path, {"ctx.xml": CTX.format(url="jdbc:postgresql://reader@dbhost/shop")})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert scan.info.configured and scan.info.url == "jdbc:postgresql://dbhost/shop"
    assert scan.info.username == "reader" and not scan.info.has_password


def test_a_url_that_may_still_carry_credentials_is_not_written(tmp_path):
    for url in (f"jdbc:mysql://dbhost/shop?db_password1={PW}", f"jdbc:mysql://x@y:{PW}@dbhost/shop?a=b@c",
                f"jdbc:oracle:thin:scott@{PW}@dbhost:1521:X",
                # the password holds a / : the user info ends at the last @ before the first /, the rest is withheld
                f"jdbc:mysql://root:a@b/c;d&{PW}@dbhost:3306/shop",
                # MySQL host forms the strip does not know: the final wall withholds them
                f"jdbc:mysql://address=(host=db)(port=3306)(user=app)(password={PW})/shop",
                f"jdbc:mysql://(host=db,port=3306,user=app,password={PW})/shop",
                f"jdbc:mysql:replication://db1,(host=db2,password={PW})/shop",
                f"jdbc:mysql://db/shop?secretKey={PW}", f"jdbc:mysql://db/shop?credentialsFile={PW}",
                f"jdbc:mysql://db/shop?user=scott/{PW}", f"jdbc:mysql://db/shop?uid=a:{PW}",
                f"jdbc:mysql://app:{PW}@db:3306/shop?connectionAttributes=owner:a@b.com"):
        _write(tmp_path, {"ctx.xml": CTX.format(url=url.replace("&", "&amp;"))})
        scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
        assert not scan.info.configured and scan.info.reason == "unresolved" and scan.info.url == "", url
        assert scan.url_withheld and PW not in repr(scan), url


def test_a_password_inside_a_url_placeholder_names_the_properties_file(tmp_path):
    files = _write(tmp_path, {"ctx.xml": CTX.format(url="${db.url}"),
                              "db.properties": f"db.url=jdbc:mysql://root:{PW}@dbhost/shop\n"})
    scan = scan_datasource(tmp_path, files, RULES)
    assert scan.info.url == "jdbc:mysql://dbhost/shop" and PW not in repr(scan)
    assert scan.password_files == ["ctx.xml", "db.properties"]


@pytest.mark.parametrize("url", [
    "jdbc:oracle:thin:@(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME=X)))",
    "jdbc:oracle:thin:@//db.example.com:1521/svc",
    "jdbc:oracle:oci:@db",
    "jdbc:oracle:thin:@ldap://ldap.example.com:389/orcl,cn=OracleContext,dc=example,dc=com",
    "jdbc:postgresql://[::1]:5432/shop",
    "jdbc:postgresql://db:5432/shop?ssl=true&sslmode=require&ApplicationName=shop",
    "jdbc:sqlserver://db:1433;databaseName=shop;user=sa;userName=sa;encrypt=true",
    "jdbc:mysql://db1,db2:3306/shop?useSSL=false&serverTimezone=Asia/Seoul",
])
def test_urls_without_credentials_pass_the_final_wall(tmp_path, url):
    _write(tmp_path, {"ctx.xml": CTX.format(url=url.replace("&", "&amp;"))})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert scan.info.configured and scan.info.url == url and not scan.url_withheld, scan.info


def test_the_user_info_ends_at_the_last_at_before_the_path():
    from sbmig.fwconvert.datasource import _strip_url_password
    url, user, had = _strip_url_password(f"jdbc:mysql://app:{PW}@db:3306/shop?connectionAttributes=owner:a@b.com")
    assert url == "jdbc:mysql://db:3306/shop?connectionAttributes=owner:a@b.com" and user == "app" and had


def test_a_withheld_url_appears_nowhere(tmp_path):
    url = f"jdbc:mysql://(host=db,port=3306,user=app,password={PW})/shop"
    _write(tmp_path, {"ctx.xml": CTX.format(url=url)})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert scan.url_withheld and "host=db" not in repr(scan) and PW not in repr(scan)


@pytest.mark.parametrize("user", [f"scott/{PW}", f"scott:{PW}", f"a@{PW}", f"two {PW}", "x" * 129])
def test_a_user_name_that_is_not_a_plain_name_is_not_written(tmp_path, user):
    files = _write(tmp_path, {
        "ctx.xml": BEANS_HEAD + """
  <bean id="ds" class="a.BasicDataSource">
    <property name="url" value="jdbc:oracle:thin:@db:1521:ORCL"/><property name="username" value="${db.user}"/>
  </bean></beans>""",
        "db.properties": f"db.user={user}\n"})
    scan = scan_datasource(tmp_path, files, RULES)
    assert scan.info.configured and scan.info.username == ""
    assert PW not in repr(scan) and user not in repr(scan)
    assert scan.info.has_password and scan.password_files == ["ctx.xml", "db.properties"]


def test_plain_user_names_are_written(tmp_path):
    for user in ("scott", "app_user-1.x", "C##ADMIN", "a$b"):
        _write(tmp_path, {"ctx.xml": BEANS_HEAD + f"""
  <bean id="ds" class="a.BasicDataSource">
    <property name="url" value="jdbc:oracle:thin:@db:1521:ORCL"/><property name="username" value="{user}"/>
  </bean></beans>"""})
        scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
        assert scan.info.username == user and not scan.info.has_password


def test_a_driver_that_is_not_a_class_name_is_not_written(tmp_path):
    for driver, kept in (("oracle.jdbc.OracleDriver", "oracle.jdbc.OracleDriver"), ("oracle.jdbc.Driver;x=1", ""),
                         (f"pw={PW}", ""), ("a..b", "")):
        _write(tmp_path, {"ctx.xml": BEANS_HEAD + f"""
  <bean id="ds" class="a.BasicDataSource">
    <property name="url" value="jdbc:oracle:thin:@db:1521:ORCL"/><property name="driverClassName" value="{driver}"/>
  </bean></beans>"""})
        scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
        assert scan.info.driver_class == kept and PW not in repr(scan), driver


# ---- the normalised copy, the wider wall, the Oracle-only @ ----------------------------------------------------------

WITHHELD = [
    f"jdbc:mysql://db:3306/shop?useSSL=true&pass%77ord={PW}",
    f"jdbc:mysql://db:3306/shop?%70assword={PW}",
    f"jdbc:mariadb://db:3306/shop?p%61ssword={PW}",
    f"jdbc:mysql://db:3306/shop?pass%2577ord={PW}",            # encoded twice
    f"jdbc:mysql://db:3306/shop?ｐａｓｓｗｏｒｄ={PW}",             # full-width letters, NFKC folds them
    "jdbc:mysql://db:3306/shop?%75ser=app",                    # an encoded key withholds even without a secret
    f"jdbc:postgresql://db/shop?accessToken={PW}",
    f"jdbc:mysql://db/shop?apiKey={PW}",
    f"jdbc:postgresql://db/shop?token={PW}",
    f"jdbc:postgresql://db/shop?sslkey={PW}",
    f"jdbc:mysql://db/shop?api_key={PW}", f"jdbc:mysql://db/shop?accessKey={PW}",
    f"jdbc:mysql://db/shop?access_key={PW}", f"jdbc:mysql://db/shop?privateKey={PW}",
    f"jdbc:mysql://db/shop?private_key={PW}", f"jdbc:mysql://db/shop?keyPass={PW}",
    f"jdbc:mysql://db/shop?trustStorePass={PW}", f"jdbc:mysql://db/shop?auth={PW}",
    f"jdbc:mysql://app:{PW}/x:@db:3306/shop",
    f"jdbc:oracle:thin:a b/{PW}:@db:1521:ORCL",
    f"jdbc:oracle:thin:@db:1521:ORCL?x={PW}@y",                 # a second @
    f"jdbc:h2:mem:x:@{PW}",                                     # :@ outside an Oracle scheme
    f"jdbc:mysql://db/shop\t?x={PW}", f"jdbc:mysql://db/shop\x85{PW}", f"jdbc:mysql://db/shop {PW}",
]


@pytest.mark.parametrize("url", WITHHELD)
def test_the_normalised_wall_withholds(tmp_path, url):
    _write(tmp_path, {"ctx.xml": CTX.format(url=url.replace("&", "&amp;").replace("\t", "&#9;")
                                            .replace("\x85", "&#133;"))})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert not scan.info.configured and scan.url_withheld and scan.info.url == "", (url, scan.info)
    assert PW not in repr(scan)


@pytest.mark.parametrize("url", [
    "jdbc:mysql://db:3306/shop?allowPublicKeyRetrieval=true&useSSL=false",
    "jdbc:log4jdbc:oracle:thin:@db:1521:ORCL",
    "JDBC:ORACLE:OCI8:@db",
    "jdbc:p6spy:oracle:thin:@//db:1521/svc",
    "jdbc:mysql://db/shop?serverTimezone=Asia%2FSeoul",
])
def test_ordinary_urls_still_pass(tmp_path, url):
    _write(tmp_path, {"ctx.xml": CTX.format(url=url.replace("&", "&amp;"))})
    scan = scan_datasource(tmp_path, ["ctx.xml"], RULES)
    assert scan.info.configured and scan.info.url == url, scan.info


def test_a_url_split_over_properties_is_judged_whole(tmp_path):
    files = _write(tmp_path, {"ctx.xml": CTX.format(url="${url.base}${url.params}"),
                              "db.properties": f"url.base=jdbc:mysql://db/shop\nurl.params=?user=app&pass%77ord={PW}\n"})
    scan = scan_datasource(tmp_path, files, RULES)
    assert scan.url_withheld and PW not in repr(scan)
