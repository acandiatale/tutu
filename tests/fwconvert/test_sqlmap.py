"""SQL file -> MyBatis mapper conversion, mapper reading and the mapper file set (docs/FW-CONVERT.md 3.5, A1-A5)."""
from __future__ import annotations

import io
import time

import pytest
from lxml import etree

from sbmig.fwconvert.model import Layout, MapperIndex, ModuleNames, ProjectIdentity
from sbmig.fwconvert.rules import load_rules
from sbmig.fwconvert.sqlmap import (
    MAPPER_DOCTYPE,
    TOLERANT_MAX_CHARS,
    convert_mappers,
    convert_sql_text,
    open_at_end,
    read_mapper,
    replace_params,
    sql_verb,
)

RULES = load_rules("Example")
NS = "com.example.d1.shopProject.store.OrderStore"

# The declarations of the MyBatis 3 mapper DTD for the elements the converter writes (copied from the DTD shipped
# in the MyBatis jar), so that the output is validated offline the way MyBatis validates it.
MAPPER_DTD = """
<!ELEMENT mapper (cache-ref | cache | resultMap* | parameterMap* | sql* | insert* | update* | delete* | select* )+>
<!ATTLIST mapper namespace CDATA #IMPLIED>
<!ELEMENT select (#PCDATA | include | trim | where | set | foreach | choose | if | bind)*>
<!ATTLIST select id CDATA #REQUIRED parameterMap CDATA #IMPLIED parameterType CDATA #IMPLIED
  resultMap CDATA #IMPLIED resultType CDATA #IMPLIED>
<!ELEMENT insert (#PCDATA | selectKey | include | trim | where | set | foreach | choose | if | bind)*>
<!ATTLIST insert id CDATA #REQUIRED parameterMap CDATA #IMPLIED parameterType CDATA #IMPLIED
  keyProperty CDATA #IMPLIED useGeneratedKeys (true|false) #IMPLIED keyColumn CDATA #IMPLIED>
<!ELEMENT update (#PCDATA | selectKey | include | trim | where | set | foreach | choose | if | bind)*>
<!ATTLIST update id CDATA #REQUIRED parameterMap CDATA #IMPLIED parameterType CDATA #IMPLIED>
<!ELEMENT delete (#PCDATA | include | trim | where | set | foreach | choose | if | bind)*>
<!ATTLIST delete id CDATA #REQUIRED parameterMap CDATA #IMPLIED parameterType CDATA #IMPLIED>
<!ELEMENT sql (#PCDATA | include | trim | where | set | foreach | choose | if | bind)*>
<!ATTLIST sql id CDATA #REQUIRED>
"""


def _parse(xml: str):
    """Parse produced XML without loading the DOCTYPE from the network, validate it, return the root."""
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n' + MAPPER_DOCTYPE + "\n")
    assert "\r" not in xml and xml.endswith("</mapper>\n")
    parser = etree.XMLParser(no_network=True, load_dtd=False, resolve_entities=False)
    root = etree.fromstring(xml.encode("utf-8"), parser)
    dtd = etree.DTD(io.StringIO(MAPPER_DTD))
    assert dtd.validate(root), dtd.error_log.filter_from_errors()
    ids = [el.get("id") for el in root if isinstance(el.tag, str)]
    assert len(ids) == len(set(ids)), ids
    return root


def _sql_file(*queries: str, root_attrs: str = "") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<queryMap{root_attrs}>\n' + "\n".join(queries) + "\n</queryMap>\n"


def _convert(*queries: str, **kw):
    conv = convert_sql_text(_sql_file(*queries, **kw), namespace=NS, rules=RULES)
    return conv, _parse(conv.xml)


def _layout() -> Layout:
    ident = ProjectIdentity("shopProject", "d1", "com.example.d1.shopProject",
                            ModuleNames("shopProject-boot", "shopProject-domain", "shopProject-service",
                                        "shopProject-store"))
    return Layout(ident)


# ---- acceptance scenarios A1-A5 -----------------------------------------------------------------------------


def test_a1_positional_parameters():
    conv, root = _convert('<query id="find" isNamed="false"><![CDATA[ SELECT * FROM T WHERE A=? AND B=? ]]></query>')
    el = root[0]
    assert el.tag == "select" and el.get("parameterType") == "map" and el.get("resultType") == "java.util.Map"
    assert "A=#{param1} AND B=#{param2}" in el.text
    assert "<![CDATA[" in conv.xml and conv.error == ""
    assert conv.statements[0].params == ["param1", "param2"]


def test_a2_named_parameter_and_cast():
    conv, root = _convert('<query id="q" isNamed="true">SELECT x::int FROM t WHERE n = :name AND a:b = 1</query>')
    assert "x::int" in root[0].text and "n = #{name}" in root[0].text and "a:b = 1" in root[0].text
    assert conv.statements[0].params == ["name"]


def test_a3_insert_without_id_gets_generated_keys():
    _, root = _convert('<query id="add">INSERT INTO T_ORDER (NAME, QTY) VALUES (?, ?)</query>')
    el = root[0]
    assert el.tag == "insert" and el.get("useGeneratedKeys") == "true" and el.get("keyProperty") == "id"


def test_a4_dotted_id_keeps_the_last_part_and_the_original_as_comment():
    conv, root = _convert('<query id="order-item.getList">SELECT 1 FROM dual</query>')
    comment, el = root[0], root[1]
    assert isinstance(comment, etree._Comment) and comment.text.strip() == "order-item.getList"
    assert el.get("id") == "getList"
    st = conv.statements[0]
    assert (st.id, st.original_id, st.verb, st.tag) == ("getList", "order-item.getList", "select", "select")


def test_a5_raw_markup_characters_in_the_body_still_give_a_mapper():
    text = ('<queryMap xmlns="http://host.example/q">\n'
            '<query id="cmp">SELECT * FROM T WHERE A < ? AND B > 2 AND C = \'x&y\' AND D &lt;&gt; 1</query>\n'
            '<!-- <query id="dead">SELECT 0</query> -->\n'
            '<query id="mixed">SELECT 1 FROM T WHERE A < 1 <![CDATA[ AND B < 2 ]]></query>\n'
            '</queryMap>')
    conv = convert_sql_text(text, namespace=NS, rules=RULES)
    root = _parse(conv.xml)
    assert conv.error == ""
    assert [s.id for s in conv.statements] == ["cmp", "mixed"]
    assert "A < #{param1} AND B > 2 AND C = 'x&y' AND D <> 1" in root[0].text
    assert "A < 1  AND B < 2" in root[1].text


# ---- parameters -----------------------------------------------------------------------------------------------


def test_named_and_positional_mixed_in_text_order():
    sql, names = replace_params("SELECT * FROM t WHERE a = ? AND b = :bee AND c = ?")
    assert sql == "SELECT * FROM t WHERE a = #{param1} AND b = #{bee} AND c = #{param2}"
    assert names == ["param1", "bee", "param2"]


def test_a_parameter_used_twice_is_listed_once():
    sql, names = replace_params("SELECT * FROM t WHERE a = :id OR b = :id")
    assert sql.count("#{id}") == 2 and names == ["id"]


def test_literals_and_both_comment_styles_are_left_alone():
    sql = ("SELECT TO_CHAR(d, 'HH24:MI:SS'), 'it''s ? :x' FROM t -- why? :no\n"
           "WHERE /* ? :neither */ a = ? AND \"col?\" = :yes")
    out, names = replace_params(sql)
    assert "'HH24:MI:SS'" in out and "'it''s ? :x'" in out and "-- why? :no" in out
    assert "/* ? :neither */" in out and '"col?"' in out
    assert out.endswith("a = #{param1} AND \"col?\" = #{yes}") and names == ["param1", "yes"]


def test_assignment_and_numeric_binds_stay():
    out, names = replace_params("BEGIN v := :1; END")
    assert out == "BEGIN v := :1; END" and names == []


# ---- statement kind -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sql, verb", [
    ("select * from t", "select"),
    ("with a as (select 1 from dual) select * from a", "select"),
    ("WITH a AS (SELECT 1 FROM dual) INSERT INTO t SELECT * FROM a", "insert"),
    ("-- list of orders\n/* note */ SELECT 1", "select"),
    ("(SELECT 1) UNION (SELECT 2)", "select"),
    ("update t set a = 1", "update"),
    ("Delete from t", "delete"),
    ("MERGE INTO t USING s ON (t.k = s.k) WHEN MATCHED THEN UPDATE SET a = 1", "insert"),
    ("CALL proc()", None),
    ("", None),
])
def test_sql_verb(sql, verb):
    assert sql_verb(sql) == verb


def test_lower_case_and_leading_comment_choose_the_element():
    conv, root = _convert('<query id="u"><![CDATA[ -- change a row\n update t set a = :a where k = :k ]]></query>',
                          '<query id="d">/* gone */ delete from t where k = ?</query>',
                          '<query id="p">CALL proc(?)</query>')
    assert [el.tag for el in root] == ["update", "delete", "select"]
    assert root[0].get("resultType") is None and root[2].get("resultType") == "java.util.Map"
    assert [s.verb for s in conv.statements] == ["update", "delete", "select"]


def test_merge_is_written_as_insert_without_generated_keys():
    conv, root = _convert('<query id="up">MERGE INTO t USING d ON (t.k = d.k) '
                          'WHEN NOT MATCHED THEN INSERT (k, v) VALUES (:k, :v)</query>')
    assert root[0].tag == "insert" and root[0].get("useGeneratedKeys") is None
    assert conv.statements[0].verb == "insert"


@pytest.mark.parametrize("sql, generated", [
    ("INSERT INTO t (id, name) VALUES (?, ?)", False),
    ("INSERT INTO t (ID, NAME) VALUES (?, ?)", False),
    ('INSERT INTO s.t ("Id", name) VALUES (?, ?)', False),
    ("INSERT INTO t (user_id, name) VALUES (?, ?)", True),
    ("insert into t(a) values (1)", True),
    ("INSERT INTO t VALUES (1, 2)", False),
    ("INSERT INTO t SELECT * FROM u", False),
    ("INSERT INTO t (SELECT * FROM u)", False),
])
def test_generated_keys_by_column_list(sql, generated):
    _, root = _convert(f'<query id="i"><![CDATA[{sql}]]></query>')
    assert (root[0].get("useGeneratedKeys") == "true") is generated


# ---- ids, bodies, odd files -----------------------------------------------------------------------------------


def test_duplicate_ids_after_cutting_are_numbered_and_reported():
    conv, root = _convert('<query id="a.getList">SELECT 1</query>', '<query id="b.getList">SELECT 2</query>',
                          '<query id="getList_2">SELECT 3</query>')
    ids = [el.get("id") for el in root if isinstance(el.tag, str)]
    assert ids == ["getList", "getList_3", "getList_2"]
    assert "b.getList" in conv.error and "getList_3" in conv.error
    assert conv.statements[1].original_id == "b.getList"


def test_cdata_and_plain_bodies_are_both_written_as_cdata():
    conv, root = _convert('<query id="c"><![CDATA[SELECT a FROM t WHERE a < 3]]></query>',
                          '<query id="p">SELECT a FROM t WHERE a &lt; 3</query>')
    assert root[0].text.strip() == root[1].text.strip() == "SELECT a FROM t WHERE a < 3"
    assert conv.xml.count("<![CDATA[") == 2 and "&lt;" not in conv.xml


def test_body_with_cdata_end_marker_stays_well_formed():
    _, root = _convert("<query id=\"x\">SELECT ']]&gt;' FROM dual</query>")
    assert root[0].text.strip() == "SELECT ']]>' FROM dual"


def test_root_with_default_namespace_is_matched_by_local_name():
    conv, _ = _convert('<query id="q">SELECT 1</query>', root_attrs=' xmlns="http://host.example/ns" desc="x"')
    assert [s.id for s in conv.statements] == ["q"] and "host.example" not in conv.xml


def test_query_without_id_or_body():
    conv, _ = _convert('<query>SELECT 1</query>', '<query id="blank">  </query>')
    assert [s.id for s in conv.statements] == ["query1"]
    assert "no id" in conv.error and "blank" in conv.error


@pytest.mark.parametrize("text, has_error", [
    ("<queryMap><query id='a'>SELECT 1</query></queryMap", False),     # broken root end tag, the query is clean
    ("", True),
    ("<queryMap><query id='a'>SELECT 1", True),      # unterminated: read tolerantly, the missing end tag reported
    ("this is not xml at all", True),
    ("<other><query id='a'>SELECT 1</query></other>", True),
    ("<queryMap desc='empty'/>", False),
])
def test_empty_and_broken_files_give_a_loadable_mapper(text, has_error):
    conv = convert_sql_text(text, namespace=NS, rules=RULES)
    root = _parse(conv.xml)
    assert root.get("namespace") == NS
    assert bool(conv.error) is has_error
    if has_error and not conv.statements:
        assert root.xpath("//comment()")


def test_same_input_gives_identical_output():
    text = _sql_file('<query id="a.b">SELECT ? FROM t</query>', '<query id="c">INSERT INTO t (x) VALUES (:x)</query>')
    a = convert_sql_text(text, namespace=NS, rules=RULES).xml
    assert a == convert_sql_text(text, namespace=NS, rules=RULES).xml


def test_comment_text_is_made_safe():
    _, root = _convert('<query id="odd--name.x-">SELECT 1</query>')
    assert isinstance(root[0], etree._Comment) and root[1].get("id") == "x-"


# ---- read_mapper ----------------------------------------------------------------------------------------------

DYNAMIC = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "https://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.example.legacy.OrderMapper">
  <sql id="cols">a, b</sql>
  <select id="search" parameterType="map" resultType="map">
    <!-- leading comment -->
    <if test="x != null">SELECT <include refid="cols"/> FROM t WHERE a = #{x}</if>
    <foreach collection="ids" item="item" open="AND id IN (" separator="," close=")">#{item, jdbcType=NUMERIC}</foreach>
  </select>
  <insert id="add">
    <selectKey keyProperty="id" resultType="long" order="BEFORE">SELECT seq.nextval FROM dual</selectKey>
    INSERT INTO t (id, a) VALUES (#{id}, #{a})
  </insert>
  <update id="wrongTag">DELETE FROM t WHERE id = #{id}</update>
  <delete id="byInclude"><include refid="cols"/></delete>
</mapper>
"""


def test_read_mapper_flattens_dynamic_tags():
    ns, sts = read_mapper(DYNAMIC)
    assert ns == "com.example.legacy.OrderMapper"
    assert [(s.id, s.tag, s.verb) for s in sts] == [("search", "select", "select"), ("add", "insert", "insert"),
                                                    ("wrongTag", "update", "delete"),
                                                    ("byInclude", "delete", "delete")]
    assert sts[0].params == ["x", "item"] and sts[1].params == ["id", "a"]


@pytest.mark.parametrize("text", ["", "not xml", "<mapper namespace='x'><select id='a'>", "<queryMap/>", None])
def test_read_mapper_never_raises(text):
    assert read_mapper(text) == ("", [])


def test_read_mapper_reads_its_own_output():
    conv, _ = _convert('<query id="o.list">SELECT * FROM t WHERE a = :a</query>')
    ns, sts = read_mapper(conv.xml)
    assert ns == NS and [(s.id, s.verb, s.params) for s in sts] == [("list", "select", ["a"])]


# ---- convert_mappers ------------------------------------------------------------------------------------------


def _write(root, rel: str, data: bytes) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return rel


def test_convert_mappers_paths_namespaces_and_keys(tmp_path):
    lay = _layout()
    sql = _write(tmp_path, "src/sql/order.example_sql",
                 _sql_file('<query id="getList">SELECT 1</query>',
                           '<query id="order-item.getOne">SELECT 2 FROM t WHERE k = ?</query>').encode())
    mapper = _write(tmp_path, "src/mybatis/legacy-mapper.xml", DYNAMIC.encode())
    res = convert_mappers(tmp_path, [sql], [mapper], lay, RULES)
    assert res.errors == []
    conv, copied = res.mappers
    assert conv.origin == "converted" and conv.resource == "order" and conv.x_name == "Order"
    assert conv.namespace == "com.example.d1.shopProject.store.OrderStore"
    assert conv.path == "shopProject-boot/src/main/resources/mybatis/mapper/order-mapper.xml"
    assert conv.source == sql and conv.keys == ["order", "order-item"]
    assert copied.origin == "copied" and copied.namespace == "com.example.legacy.OrderMapper"
    assert copied.resource == "Order" and copied.path.endswith("/mybatis/mapper/legacy-mapper.xml")
    assert copied.keys == ["Order", "legacy-mapper", "com.example.legacy.OrderMapper", "OrderMapper"]
    assert [f.path for f in res.files] == [conv.path, copied.path]
    _parse(res.files[0].text)
    assert res.files[1].text == DYNAMIC
    idx = MapperIndex([conv])
    assert idx.find("order.getList")[1].id == "getList"
    assert idx.find("order-item.getOne")[1].id == "getOne"
    assert idx.find("order.getOne")[1].original_id == "order-item.getOne"


def test_convert_mappers_ms949_sql_and_mapper(tmp_path):
    sql_text = ('<?xml version="1.0" encoding="EUC-KR"?>\n<queryMap desc="주문">\n'
                '<query id="getList"><![CDATA[\n-- 주문 목록을 조회한다\nSELECT 이름 FROM 주문 WHERE 상태 = ?\n]]></query>\n'
                '</queryMap>\n')
    sql = _write(tmp_path, "order.example_sql", sql_text.encode("cp949"))
    mapper_text = DYNAMIC.replace('encoding="UTF-8"', "encoding='EUC-KR'").replace("leading comment", "주석")
    mapper = _write(tmp_path, "res/mybatis/order.mapper", mapper_text.encode("cp949"))
    res = convert_mappers(tmp_path, [sql], [mapper], _layout(), RULES)
    assert res.errors == []
    root = _parse(res.files[0].text)
    assert "-- 주문 목록을 조회한다" in root[0].text and "상태 = #{param1}" in root[0].text
    copied = res.files[1]
    assert copied.path.endswith("/mybatis/mapper/order.xml")
    assert copied.text.startswith("<?xml version=\"1.0\" encoding='UTF-8'?>") and "주석" in copied.text
    etree.fromstring(copied.text.encode("utf-8"), etree.XMLParser(no_network=True, load_dtd=False))
    assert res.mappers[1].resource == "Order"


def test_copied_resource_falls_back_to_the_file_stem(tmp_path):
    text = DYNAMIC.replace("com.example.legacy.OrderMapper", "com.example.legacy.Billing")
    rel = _write(tmp_path, "mybatis/billing-mapper.xml", text.encode())
    res = convert_mappers(tmp_path, [], [rel], _layout(), RULES)
    assert res.mappers[0].resource == "billing" and res.mappers[0].x_name == "Billing"


def test_convert_mappers_path_collision(tmp_path):
    lay = _layout()
    a = _write(tmp_path, "a/order.example_sql", _sql_file('<query id="x">SELECT 1</query>').encode())
    b = _write(tmp_path, "b/order.example_sql", _sql_file('<query id="x">SELECT 2</query>').encode())
    m = _write(tmp_path, "mybatis/order-mapper.xml", DYNAMIC.encode())
    res = convert_mappers(tmp_path, [b, a], [m], lay, RULES)
    paths = [mf.path.rsplit("/", 1)[-1] for mf in res.mappers]
    assert paths == ["order-mapper.xml", "order-2-mapper.xml", "order-mapper-2.xml"]
    assert res.mappers[0].source == a and res.mappers[1].source == b
    assert res.mappers[1].namespace.endswith(".Order2Store") and res.mappers[1].keys[0] == "order"
    assert len({f.path for f in res.files}) == 3
    assert len(res.errors) == 2 and all("taken by" in e for e in res.errors)


def test_convert_mappers_reports_unreadable_inputs(tmp_path):
    bad = _write(tmp_path, "mybatis/broken.xml", b"<mapper namespace='x'><select id='a'>")
    broken_sql = _write(tmp_path, "x.example_sql", b"nothing here")
    res = convert_mappers(tmp_path, ["missing.example_sql", broken_sql], [bad], _layout(), RULES)
    assert len(res.files) == 1 and res.mappers[0].error
    _parse(res.files[0].text)
    assert any("missing.example_sql" in e for e in res.errors)
    assert any("broken.xml" in e and "not copied" in e for e in res.errors)
    assert any(e.startswith("x.example_sql:") for e in res.errors)


# ---- review follow-up: hostile tolerant input, q-quotes, entities, isNamed ------------------------------------

MB2 = 2 * 1024 * 1024


@pytest.mark.parametrize("text", [
    "<queryMap><query id='a'>" + "<![CDATA[" * (MB2 // 9),
    "<queryMap><query id='a'>x</query>&" + "<!--" * (MB2 // 4),
    "<queryMap>&" + "<query " * (MB2 // 7),
    "<queryMap>&<query " + "a" * MB2,
    "<queryMap>&<query id='a'>" + "<query id='b'>" * (MB2 // 14),
    "<queryMap>&" + "<query id='b'>x</query>" * (MB2 // 23),
], ids=["unclosed-cdata", "unclosed-comment", "query-without-gt", "one-long-tag", "queries-without-end",
        "many-duplicate-ids"])
def test_hostile_tolerant_input_is_linear(text):
    t0 = time.monotonic()
    conv = convert_sql_text(text, namespace=NS, rules=RULES)
    assert time.monotonic() - t0 < 5
    _parse(conv.xml)
    assert conv.error and len(conv.error) < 5000


def test_oversized_broken_file_is_not_read_tolerantly():
    text = "<queryMap>&<query id='a'>SELECT 1</query>" + " " * TOLERANT_MAX_CHARS
    conv = convert_sql_text(text, namespace=NS, rules=RULES)
    assert not conv.statements and "larger than" in conv.error
    _parse(conv.xml)


def test_well_formed_large_file_is_still_read():
    text = _sql_file('<query id="a">SELECT 1</query>') + " " * (TOLERANT_MAX_CHARS + 1)
    assert [s.id for s in convert_sql_text(text, namespace=NS, rules=RULES).statements] == ["a"]


@pytest.mark.parametrize("lit", ["q'[it's]'", "Q'{a}b'}'", "q'(x)'", "q'<a'b>'", "nq'!it's!'", "NQ'#?#'"])
def test_oracle_q_quote_is_one_literal(lit):
    out, names = replace_params(f"SELECT {lit} FROM dual WHERE a = ? AND b = :bee")
    assert out == f"SELECT {lit} FROM dual WHERE a = #{{param1}} AND b = #{{bee}}" and names == ["param1", "bee"]


def test_q_inside_an_identifier_is_not_a_q_quote():
    out, names = replace_params("SELECT seq'x' FROM t WHERE a = ?")
    assert names == ["param1"] and "'x'" in out


@pytest.mark.parametrize("sql, kind", [
    ("SELECT 'open FROM t WHERE a = ?", "string literal"),
    ("SELECT q'[open FROM t", "string literal"),
    ("SELECT 1 /* open", "comment"),
    ('SELECT "open', "quoted identifier"),
    ("SELECT 'a''b' FROM t -- trailing", ""),
    ("SELECT 'x'", ""),
])
def test_open_at_end(sql, kind):
    assert open_at_end(sql) == kind


def test_statement_ending_inside_a_literal_is_reported_and_written():
    conv, root = _convert("<query id=\"q\">SELECT * FROM t WHERE a = 'open AND b = ?</query>")
    assert root[0].get("id") == "q" and "q ends inside an open string literal" in conv.error
    assert conv.statements[0].params == []


@pytest.mark.parametrize("decl", ['<!ENTITY cols "A, B, C">', '<!ENTITY cols SYSTEM "file:///etc/hostname">'])
def test_internal_and_external_entities_are_kept_as_text_and_reported(decl):
    text = (f'<?xml version="1.0"?><!DOCTYPE queryMap [{decl}]>'
            '<queryMap><query id="q">SELECT &cols; FROM T WHERE a = ?</query></queryMap>')
    conv = convert_sql_text(text, namespace=NS, rules=RULES)
    root = _parse(conv.xml)
    assert root[0].text.strip() == "SELECT &cols; FROM T WHERE a = #{param1}"
    assert "query q" in conv.error and "&cols;" in conv.error


def test_unknown_entity_in_a_tolerant_read_is_reported():
    conv = convert_sql_text('<queryMap><query id="q">SELECT &cols; FROM T WHERE a < 1</query></queryMap>',
                            namespace=NS, rules=RULES)
    assert "&cols;" in _parse(conv.xml)[0].text and "&cols;" in conv.error


@pytest.mark.parametrize("flag", ['isNamed="true"', 'isNamed="TRUE"', "isNamed='True'"])
def test_is_named_query_replaces_only_names(flag):
    conv, root = _convert(f"<query id=\"q\" {flag}>SELECT * FROM t WHERE doc ? 'k' AND id = :id AND x = :x</query>")
    assert "doc ? 'k' AND id = #{id} AND x = #{x}" in root[0].text
    assert conv.statements[0].params == ["id", "x"] and "statement q" in conv.error


def test_named_query_reports_a_bare_question_mark_only():
    conv, root = _convert("<query id=\"bare\" isNamed=\"true\">SELECT * FROM t WHERE a = ? AND id = :id</query>",
                          "<query id=\"quoted\" isNamed=\"true\">SELECT '?' FROM t /* ? */ WHERE id = :id -- ?\n"
                          "</query>")
    assert "a = ? AND id = #{id}" in root[0].text
    assert "statement bare" in conv.error and "parameter marker" in conv.error
    assert "quoted" not in conv.error


@pytest.mark.parametrize("flag", ["", 'isNamed="false"', 'isNamed="yes"'])
def test_not_named_query_replaces_both(flag):
    conv, root = _convert(f"<query id=\"q\" {flag}>SELECT * FROM t WHERE a = ? AND id = :id</query>")
    assert "a = #{param1} AND id = #{id}" in root[0].text
    assert conv.statements[0].params == ["param1", "id"]


def test_named_only_in_replace_params():
    out, names = replace_params("SELECT j ?| array['a'] FROM t WHERE id = :id", named_only=True)
    assert out == "SELECT j ?| array['a'] FROM t WHERE id = #{id}" and names == ["id"]


@pytest.mark.parametrize("make", [
    lambda n: replace_params("SELECT 1 FROM t WHERE " + "x = ? AND " * n + "1 = 1"),
    lambda n: replace_params("SELECT 1 FROM t WHERE " + "".join(f"x = :p{i} AND " for i in range(n)) + "1 = 1"),
    lambda n: read_mapper("<mapper namespace='a'><select id='s'>SELECT "
                          + "".join(f"#{{p{i}}}," for i in range(n)) + "1</select></mapper>"),
    lambda n: convert_sql_text("<queryMap><query id='q'>SELECT " + "".join(f"&e{i};" for i in range(n))
                               + " a < 1</query></queryMap>", namespace=NS, rules=RULES),
], ids=["positional", "named", "mapper-params", "tolerant-entities"])
def test_many_distinct_parameters_are_linear(make):
    t0 = time.monotonic()
    out = make(200_000)
    assert time.monotonic() - t0 < 5
    if hasattr(out, "error"):
        assert "&e0;" in out.error and len(out.error) < 5000
