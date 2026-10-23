"""E13 jakarta-namespace-sweep + E14 spring-data-crud-repository (sbmig/triage/handlers/source_handlers.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.models import BuildError, CaseType, ErrorKind, LibraryUsage, ProjectProfile, SourceStats
from sbmig.plan.pre_handlers import pre_handler_reasons
from sbmig.triage import HandlerRegistry, cluster_errors
from sbmig.triage.handlers.source_handlers import (
    jakarta_packages,
    javax_key_in_table,
    rename_javax,
    rename_javax_jsp,
)

from .conftest import make_profile


def _java(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _err(message: str, file: str = "src/main/java/com/acme/Svc.java", raw: str = "", location=None, symbol=None,
         kind=ErrorKind.COMPILE) -> BuildError:
    return BuildError(kind=kind, message=message, file=file, line=3, raw=raw or message, location=location,
                      symbol=symbol)


def _cluster(*errs):
    cl = cluster_errors(list(errs))
    assert len(cl) == 1
    return cl[0]


def _javax_profile(javax=None, libs=(), packages=("com.acme",)) -> ProjectProfile:
    p = make_profile(libs=libs)
    p.source = SourceStats(javax_imports=javax or {}, packages=list(packages), base_package="com.acme")
    return p


# ---------------------------------------------------------------------------------------------------------------------
# table semantics
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("src,expected", [
    ("import javax.annotation.PostConstruct;", "import jakarta.annotation.PostConstruct;"),
    ("import javax.annotation.processing.Generated;", None),
    ("import javax.annotation.security.RolesAllowed;", "import jakarta.annotation.security.RolesAllowed;"),
    ("import javax.transaction.Transactional;", "import jakarta.transaction.Transactional;"),
    ("import javax.transaction.xa.XAResource;", None),
    ("import javax.sql.DataSource;", None),
    ("import javax.naming.InitialContext;", None),
    ("import javax.servlet.http.HttpServletRequest;", "import jakarta.servlet.http.HttpServletRequest;"),
    ("import javax.servlet.jsp.JspWriter;", "import jakarta.servlet.jsp.JspWriter;"),
    ("import javax.persistence.*;", "import jakarta.persistence.*;"),
    ("import javax.annotation.*;", "import jakarta.annotation.*;"),
    ("import javax.ws.rs.GET;", "import jakarta.ws.rs.GET;"),
    ("import javax.xml.bind.JAXBContext;", "import jakarta.xml.bind.JAXBContext;"),
    ("import javax.xml.parsers.DocumentBuilder;", None),
    ("import javax.security.auth.Subject;", None),
    ("import javax.security.auth.message.AuthStatus;", "import jakarta.security.auth.message.AuthStatus;"),
    ("import javax.security.cert.X509Certificate;", "import java.security.cert.X509Certificate;"),
    ("import static javax.transaction.Transactional.TxType.REQUIRED;",
     "import static jakarta.transaction.Transactional.TxType.REQUIRED;"),
    ("javax.persistence.Query q = em.createQuery(s);", "jakarta.persistence.Query q = em.createQuery(s);"),
    ("Object o = foo.javax.persistence.Query;", None),          # preceded by '.': not a package reference
])
def test_rename_table_semantics(src, expected):
    r = rename_javax(src)
    assert r.text == (expected if expected is not None else src)
    assert bool(r.replacements) == (expected is not None)


def test_literals_and_comments_untouched():
    """Comments are prose (a Javadoc naming javax.persistence.Query is not a leftover — the QLRM e2e fixture raised the
    flag for exactly that); literals are kept and reported; only code tokens are renamed."""
    src = ('// uses javax.persistence.Entity\n'
           '/** takes javax.persistence.Query */\n'
           'String k = "javax.persistence.jdbc.url";\n'
           "char c = 'x';\n"
           'String tb = """\n  javax.servlet.http.HttpServlet\n  """;\n'
           'javax.persistence.EntityManager em;\n')
    r = rename_javax(src)
    assert "// uses javax.persistence.Entity" in r.text
    assert "/** takes javax.persistence.Query */" in r.text
    assert '"javax.persistence.jdbc.url"' in r.text
    assert "  javax.servlet.http.HttpServlet\n" in r.text
    assert "jakarta.persistence.EntityManager em;" in r.text
    assert r.literal_hits == ["javax.persistence.jdbc.url", "javax.servlet.http.HttpServlet"]
    assert r.replacements["javax.persistence"] == 1
    assert rename_javax('/** only javax.persistence.Query in a comment */\nclass A {}\n').replacements == {}


def test_table_loaded_and_keys():
    t = jakarta_packages()
    froms = [r.from_ for r in t]
    assert "javax.persistence" in froms and "javax.security.cert" in froms
    assert len(froms[0]) >= len(froms[-1])                          # longest first
    assert javax_key_in_table("javax.servlet") and javax_key_in_table("javax.xml.bind")
    assert not javax_key_in_table("javax.sql") and not javax_key_in_table("javax.xml.parsers")


# ---------------------------------------------------------------------------------------------------------------------
# jakarta-namespace-sweep handler
# ---------------------------------------------------------------------------------------------------------------------
JAKARTA_SVC = ("package com.acme;\n\nimport jakarta.persistence.EntityManager;\n\n"
               "public class Svc {\n  EntityManager em;\n}\n")
LEFTOVER = ("package com.acme;\n\nimport javax.persistence.Entity;\nimport javax.annotation.processing.Generated;\n\n"
            "@Entity\npublic class Member {\n  String k = \"javax.persistence.lock.timeout\";\n}\n")


def test_preemptive_guard_without_jakarta(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/Member.java", LEFTOVER)
    h = fake_kit.handler("jakarta-namespace-sweep")
    assert h.apply(fake_kit.ws, None, _javax_profile({"javax.persistence": 1}), None) == []
    assert "javax.persistence.Entity" in (fake_kit.root / "src/main/java/com/acme/Member.java").read_text()


def test_preemptive_renames_leftovers_and_notes(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/Svc.java", JAKARTA_SVC)
    f = _java(fake_kit.root, "src/main/java/com/acme/Member.java", LEFTOVER)
    _java(fake_kit.root, "src/main/resources/application.properties",
          "spring.jpa.properties.javax.persistence.validation.mode=none\n")
    _java(fake_kit.root, "target/generated-sources/X.java", "import javax.persistence.Entity;\n")
    h = fake_kit.handler("jakarta-namespace-sweep")
    changes = h.apply(fake_kit.ws, None, _javax_profile({"javax.persistence": 1}), None)
    assert [c.path for c in changes] == [f]
    text = (fake_kit.root / f).read_text()
    assert "import jakarta.persistence.Entity;" in text
    assert "import javax.annotation.processing.Generated;" in text
    assert '"javax.persistence.lock.timeout"' in text
    assert h.flags == ["jakarta-leftovers-renamed"]
    assert h.notes[0].startswith("renamed javax.* → jakarta.* in 1 file(s)") and "javax.persistence" in h.notes[0]
    assert any(n.startswith("follow-up: 1 string literal(s)") and "javax.persistence.lock.timeout" in n
               for n in h.notes)
    assert any(n.startswith("follow-up: javax.* names remain in 1 resource file(s) "
                            "(src/main/resources/application.properties)") for n in h.notes)
    assert "javax.persistence.Entity" in (fake_kit.root / "target/generated-sources/X.java").read_text()
    # idempotent
    assert h.apply(fake_kit.ws, None, _javax_profile({"javax.persistence": 1}), None) == []
    assert h.flags == []


def test_reactive_package_does_not_exist(fake_kit):
    f = _java(fake_kit.root, "src/main/java/com/acme/Member.java", LEFTOVER)       # no jakarta import anywhere
    cl = _cluster(_err("package javax.persistence does not exist", file=f))
    prof = _javax_profile({"javax.persistence": 1})
    h = fake_kit.handler("jakarta-namespace-sweep")
    assert h.matches(cl, prof, None)
    assert [c.path for c in h.apply(fake_kit.ws, cl, prof, None)] == [f]
    assert "import jakarta.persistence.Entity;" in (fake_kit.root / f).read_text()


CONVERT = "incompatible types: jakarta.persistence.Query cannot be converted to javax.persistence.Query"


def test_reactive_conversion_with_javax_left(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/Svc.java", JAKARTA_SVC)
    f = _java(fake_kit.root, "src/main/java/com/acme/Dao.java",
              "package com.acme;\n\npublic class Dao {\n  javax.persistence.Query q() { return null; }\n}\n")
    cl = _cluster(_err(CONVERT, file="src/main/java/com/acme/Svc.java"))
    prof = _javax_profile({"javax.persistence": 2})
    h = fake_kit.handler("jakarta-namespace-sweep")
    assert h.matches(cl, prof, None)
    assert [c.path for c in h.apply(fake_kit.ws, cl, prof, None)] == [f]
    assert "jakarta.persistence.Query q()" in (fake_kit.root / f).read_text()


def test_reactive_conversion_nothing_left(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/Svc.java", JAKARTA_SVC)
    cl = _cluster(_err(CONVERT, file="src/main/java/com/acme/Svc.java",
                       location="class com.lib.LegacyDao"))
    h = fake_kit.handler("jakarta-namespace-sweep")
    with_javax = _javax_profile({"javax.persistence": 1})
    without = _javax_profile({"javax.sql": 1})
    assert h.matches(cl, with_javax, None)
    assert not h.matches(cl, without, None)                       # library owner, no javax EE imports
    assert h.apply(fake_kit.ws, cl, with_javax, None) == []
    # project owner (X1 "project class" case) → applicable without javax imports
    cl2 = _cluster(_err(CONVERT, file="src/main/java/com/acme/Svc.java", location="class com.acme.Svc"))
    assert h.matches(cl2, without, None)


def test_crlf_and_cp949_preserved(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/Svc.java", JAKARTA_SVC)
    crlf = fake_kit.root / "src/main/java/com/acme/A.java"
    crlf.write_bytes(b"package com.acme;\r\nimport javax.servlet.Filter;\r\nclass A {}\r\n")
    legacy = fake_kit.root / "src/main/java/com/acme/B.java"
    legacy.write_bytes("package com.acme;\n// 한글 주석\nimport javax.validation.Valid;\nclass B { String s = \"가나다\"; }\n"
                       .encode("cp949"))
    h = fake_kit.handler("jakarta-namespace-sweep")
    changes = h.apply(fake_kit.ws, None, None, None)
    assert sorted(c.path for c in changes) == ["src/main/java/com/acme/A.java", "src/main/java/com/acme/B.java"]
    assert crlf.read_bytes() == b"package com.acme;\r\nimport jakarta.servlet.Filter;\r\nclass A {}\r\n"
    assert legacy.read_bytes() == ("package com.acme;\n// 한글 주석\nimport jakarta.validation.Valid;\n"
                                   "class B { String s = \"가나다\"; }\n").encode("cp949")


# ---------------------------------------------------------------------------------------------------------------------
# JSP / tag files
# ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("src,expected", [
    ('<%@ page contentType="text/html" import="javax.servlet.http.Cookie, java.util.*,javax.servlet.jsp.*" %>',
     '<%@ page contentType="text/html" import="jakarta.servlet.http.Cookie, java.util.*,jakarta.servlet.jsp.*" %>'),
    ("<%@ tag import='javax.servlet.http.HttpSession' %>", "<%@ tag import='jakarta.servlet.http.HttpSession' %>"),
    ("<% javax.servlet.http.HttpSession s = request.getSession(); %>",
     "<% jakarta.servlet.http.HttpSession s = request.getSession(); %>"),
    ("<%! private javax.servlet.jsp.JspWriter w; %>", "<%! private jakarta.servlet.jsp.JspWriter w; %>"),
    ("<%= ((javax.servlet.http.HttpServletRequest) request).getMethod() %>",
     "<%= ((jakarta.servlet.http.HttpServletRequest) request).getMethod() %>"),
    ('<jsp:useBean id="m" class="javax.mail.Session" type="javax.mail.Session" scope="page"/>',
     '<jsp:useBean id="m" class="jakarta.mail.Session" type="jakarta.mail.Session" scope="page"/>'),
    ('<jsp:directive.page import="javax.persistence.EntityManager"/>',
     '<jsp:directive.page import="jakarta.persistence.EntityManager"/>'),
    ("<jsp:scriptlet>javax.validation.Validator v = null;</jsp:scriptlet>",
     "<jsp:scriptlet>jakarta.validation.Validator v = null;</jsp:scriptlet>"),
])
def test_rename_javax_jsp(src, expected):
    r = rename_javax_jsp(src)
    assert r.text == expected and r.replacements and r.literal_hits == []


@pytest.mark.parametrize("src", [
    '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>',
    "<p>see javax.servlet.http.Cookie in the docs</p>",
    "<c:out value=\"${javax.servlet.forward.request_uri}\"/>",
    "<%-- <% javax.servlet.http.HttpSession s; %> --%>",
    '<% javax.sql.DataSource ds = null; String k = "javax.servlet.error.exception"; %>',
    '<%@ page import="javax.sql.DataSource, javax.naming.InitialContext" %>',
    '<%@ include file="javax.servlet.jspf" %>',
])
def test_rename_javax_jsp_leaves_non_code_alone(src):
    r = rename_javax_jsp(src)
    assert r.text == src and not r.replacements


JSP_PAGE = ('<%@ page contentType="text/html; charset=EUC-KR" import="javax.servlet.http.Cookie" %>\n'
            '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n<p>javax.servlet</p>\n')


def test_preemptive_renames_jsp_and_notes(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/Svc.java", JAKARTA_SVC)
    page = fake_kit.root / "src/main/webapp/WEB-INF/jsp/a.jsp"
    page.parent.mkdir(parents=True)
    page.write_bytes(("<%-- 한글 --%>\r\n" + JSP_PAGE.replace("\n", "\r\n")).encode("cp949"))
    tag = _java(fake_kit.root, "src/main/webapp/WEB-INF/tags/t.tag",
                "<%@ tag import=\"javax.servlet.jsp.PageContext\" %>\n")
    _java(fake_kit.root, "src/main/webapp/plain.jsp", "<p>hello</p>\n")
    _java(fake_kit.root, "target/x/b.jsp", JSP_PAGE)
    h = fake_kit.handler("jakarta-namespace-sweep")
    changes = h.apply(fake_kit.ws, None, _javax_profile({"javax.servlet": 1}), None)
    assert sorted(c.path for c in changes) == ["src/main/webapp/WEB-INF/jsp/a.jsp", tag]
    assert page.read_bytes() == ("<%-- 한글 --%>\r\n" + JSP_PAGE.replace("\n", "\r\n")
                                 .replace("javax.servlet.http.Cookie", "jakarta.servlet.http.Cookie")).encode("cp949")
    assert "jakarta.servlet.jsp.PageContext" in (fake_kit.root / tag).read_text()
    assert "javax.servlet.http.Cookie" in (fake_kit.root / "target/x/b.jsp").read_text()
    assert h.flags == ["jsp-javax-renamed"]
    assert h.notes[0].startswith("renamed javax.* → jakarta.* in 2 JSP / tag file(s) (page imports, scriptlets, "
                                 "useBean) — JSPs are compiled by Tomcat at runtime, not by the build: smoke-test")
    assert "src/main/webapp/WEB-INF/jsp/a.jsp" in h.notes[0]
    assert h.apply(fake_kit.ws, None, _javax_profile({"javax.servlet": 1}), None) == []       # idempotent


def test_jsp_only_project_is_swept(fake_kit):
    f = _java(fake_kit.root, "src/main/webapp/index.jsp", JSP_PAGE)
    h = fake_kit.handler("jakarta-namespace-sweep")
    prof = _javax_profile({})
    assert not h.applicable(None, prof, None)
    prof.source.jsp_javax_files = 1
    assert h.applicable(None, prof, None)
    assert [c.path for c in h.apply(fake_kit.ws, None, prof, None)] == [f]
    assert "jakarta.servlet.http.Cookie" in (fake_kit.root / f).read_text()
    assert h.flags == ["jsp-javax-renamed"]


def test_jsp_left_alone_while_java_is_unmigrated(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/Member.java", LEFTOVER)       # javax EE, no jakarta import
    f = _java(fake_kit.root, "src/main/webapp/index.jsp", JSP_PAGE)
    h = fake_kit.handler("jakarta-namespace-sweep")
    assert h.apply(fake_kit.ws, None, _javax_profile({"javax.persistence": 1}), None) == []
    assert "javax.servlet.http.Cookie" in (fake_kit.root / f).read_text()


# ---------------------------------------------------------------------------------------------------------------------
# spring-data-crud-repository handler
# ---------------------------------------------------------------------------------------------------------------------
REPO = ("package com.acme.repo;\n\nimport com.acme.domain.Member;\n"
        "import org.springframework.data.repository.PagingAndSortingRepository;\n\n"
        "public interface MemberRepository extends PagingAndSortingRepository<Member, Long> {\n}\n")
REPO_PATH = "src/main/java/com/acme/repo/MemberRepository.java"


def _crud(kit):
    return kit.handler("spring-data-crud-repository")


def test_crud_simple(fake_kit):
    _java(fake_kit.root, REPO_PATH, REPO)
    h = _crud(fake_kit)
    assert [c.path for c in h.apply(fake_kit.ws, None, None, None)] == [REPO_PATH]
    text = (fake_kit.root / REPO_PATH).read_text()
    assert ("import org.springframework.data.repository.PagingAndSortingRepository;\n"
            "import org.springframework.data.repository.ListCrudRepository;\n") in text
    assert "extends PagingAndSortingRepository<Member, Long>, ListCrudRepository<Member, Long> {" in text
    assert h.flags == ["spring-data-crud-restored"]
    assert h.notes == ["added ListCrudRepository<T, ID> to 1 repository interface(s) that extended only "
                       "PagingAndSortingRepository (Spring Data 3 removed its CRUD methods): MemberRepository"]
    assert h.apply(fake_kit.ws, None, None, None) == []           # idempotent


def test_crud_base_repository_generic_and_child(fake_kit):
    base = _java(fake_kit.root, "src/main/java/com/acme/repo/BaseRepository.java",
                 "package com.acme.repo;\n\nimport java.io.Serializable;\n"
                 "import org.springframework.data.repository.NoRepositoryBean;\n"
                 "import org.springframework.data.repository.PagingAndSortingRepository;\n\n"
                 "@NoRepositoryBean\npublic interface BaseRepository<T, ID extends Serializable> "
                 "extends PagingAndSortingRepository<T, ID> {\n}\n")
    child = _java(fake_kit.root, "src/main/java/com/acme/repo/OrderRepository.java",
                  "package com.acme.repo;\n\npublic interface OrderRepository extends BaseRepository<Order, Long> {}\n")
    child2 = _java(fake_kit.root, "src/main/java/com/acme/repo/ItemRepository.java",
                   "package com.acme.repo;\nimport org.springframework.data.repository.PagingAndSortingRepository;\n"
                   "public interface ItemRepository extends BaseRepository<Item, Long>, "
                   "PagingAndSortingRepository<Item, Long> {}\n")
    h = _crud(fake_kit)
    assert [c.path for c in h.apply(fake_kit.ws, None, None, None)] == [base]
    assert "extends PagingAndSortingRepository<T, ID>, ListCrudRepository<T, ID> {" in (fake_kit.root / base).read_text()
    assert "ListCrud" not in (fake_kit.root / child).read_text()
    assert "ListCrud" not in (fake_kit.root / child2).read_text()


@pytest.mark.parametrize("decl,expected", [
    ("extends JpaRepository<Member, Long>", None),
    ("extends PagingAndSortingRepository<A, Long>, QuerydslPredicateExecutor<A>",
     "extends PagingAndSortingRepository<A, Long>, ListCrudRepository<A, Long>, QuerydslPredicateExecutor<A>"),
    ("extends org.springframework.data.repository.PagingAndSortingRepository<A, Long>",
     "extends org.springframework.data.repository.PagingAndSortingRepository<A, Long>, "
     "org.springframework.data.repository.ListCrudRepository<A, Long>"),
    ("extends PagingAndSortingRepository", "extends PagingAndSortingRepository, ListCrudRepository"),
    ("extends ListPagingAndSortingRepository<A, Long>",
     "extends ListPagingAndSortingRepository<A, Long>, ListCrudRepository<A, Long>"),
    ("extends PagingAndSortingRepository<A, Long>, CrudRepository<A, Long>", None),
])
def test_crud_variants(fake_kit, decl, expected):
    src = (f"package com.acme.repo;\n\nimport java.util.List;\n\n/* interface X extends PagingAndSortingRepository<Y, Z> {{ */\n"
           f"public interface ARepository {decl} {{\n  List<A> findByName(String n);\n}}\n")
    _java(fake_kit.root, "src/main/java/com/acme/repo/ARepository.java", src)
    changes = _crud(fake_kit).apply(fake_kit.ws, None, None, None)
    text = (fake_kit.root / "src/main/java/com/acme/repo/ARepository.java").read_text()
    if expected is None:
        assert changes == [] and text == src
        return
    assert f"public interface ARepository {expected} {{" in text
    has_import = "import org.springframework.data.repository.ListCrudRepository;" in text
    assert has_import == ("org.springframework.data.repository.PagingAndSortingRepository<" not in decl)
    if has_import:      # no paging import → after the last import
        assert "import java.util.List;\nimport org.springframework.data.repository.ListCrudRepository;\n" in text
    assert "/* interface X extends PagingAndSortingRepository<Y, Z> { */" in text


def test_crud_project_ancestor_already_crud(fake_kit):
    _java(fake_kit.root, "src/main/java/com/acme/repo/BaseRepository.java",
          "package com.acme.repo;\nimport org.springframework.data.repository.CrudRepository;\n"
          "public interface BaseRepository<T> extends CrudRepository<T, Long> {}\n")
    _java(fake_kit.root, REPO_PATH, REPO.replace("PagingAndSortingRepository<Member, Long> {",
                                                 "PagingAndSortingRepository<Member, Long>, BaseRepository<Member> {"))
    assert _crud(fake_kit).apply(fake_kit.ws, None, None, None) == []


def test_crud_crlf_preserved(fake_kit):
    p = fake_kit.root / REPO_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(REPO.replace("\n", "\r\n").encode())
    _crud(fake_kit).apply(fake_kit.ws, None, None, None)
    data = p.read_bytes()
    assert b"\r\nimport org.springframework.data.repository.ListCrudRepository;\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n")


def test_crud_preemptive_guard_javax_sources(fake_kit):
    _java(fake_kit.root, REPO_PATH, REPO)
    _java(fake_kit.root, "src/main/java/com/acme/domain/Member.java",
          "package com.acme.domain;\nimport javax.persistence.Entity;\n@Entity public class Member {}\n")
    assert _crud(fake_kit).apply(fake_kit.ws, None, None, None) == []       # Spring Data 2 still: no ListCrudRepository


def test_crud_reactive_fqcn_location(fake_kit):
    _java(fake_kit.root, REPO_PATH, REPO)
    other = _java(fake_kit.root, "src/main/java/com/acme/repo/OtherRepository.java",
                  REPO.replace("MemberRepository", "OtherRepository"))
    svc = "src/main/java/com/acme/svc/MemberService.java"
    _java(fake_kit.root, svc, "package com.acme.svc;\nclass MemberService {}\n")
    cl = _cluster(_err("cannot find symbol", file=svc, symbol="method save(com.acme.domain.Member)",
                       location="variable repository of type com.acme.repo.MemberRepository",
                       raw="cannot find symbol\n  symbol:   method save(com.acme.domain.Member)\n"
                           "  location: variable repository of type com.acme.repo.MemberRepository"))
    h = _crud(fake_kit)
    prof = make_profile(libs=[("spring-data-jpa", None)])
    assert h.matches(cl, prof, None)
    assert [c.path for c in h.apply(fake_kit.ws, cl, prof, None)] == [REPO_PATH]
    assert "ListCrud" not in (fake_kit.root / other).read_text()            # only the type named by the error


def test_crud_reactive_find_all_simple_name_via_import(fake_kit):
    _java(fake_kit.root, REPO_PATH, REPO)
    _java(fake_kit.root, "src/main/java/com/acme/other/MemberRepository.java",
          REPO.replace("package com.acme.repo;", "package com.acme.other;"))          # same simple name elsewhere
    svc = _java(fake_kit.root, "src/main/java/com/acme/svc/MemberService.java",
                "package com.acme.svc;\nimport com.acme.repo.MemberRepository;\nclass MemberService {}\n")
    raw = ("no suitable method found for findAll(no arguments)\n"
           "    method PagingAndSortingRepository.findAll(Sort) is not applicable\n"
           "  location: variable repo of type MemberRepository")
    cl = _cluster(_err("no suitable method found for findAll(no arguments)", file=svc, raw=raw,
                       location="variable repo of type MemberRepository"))
    h = _crud(fake_kit)
    assert h.matches(cl, make_profile(libs=[("spring-data-jpa", None)]), None)
    assert [c.path for c in h.apply(fake_kit.ws, cl, None, None)] == [REPO_PATH]
    assert h.apply(fake_kit.ws, cl, None, None) == []


def test_crud_reactive_nothing_found(fake_kit):
    svc = _java(fake_kit.root, "src/main/java/com/acme/svc/MemberService.java", "package com.acme.svc;\nclass X {}\n")
    cl = _cluster(_err("cannot find symbol", file=svc, raw="cannot find symbol\n  symbol:   method save(Foo)\n"
                       "  location: variable r of type FooRepository"))
    assert _crud(fake_kit).apply(fake_kit.ws, cl, None, None) == []


def test_crud_applicable():
    h = HandlerRegistry.default(None).get("spring-data-crud-repository")
    cl = _cluster(_err("cannot find symbol", raw="cannot find symbol\n  symbol:   method findById(Long)\n"
                       "  location: variable r of type FooRepository"))
    assert h.matches(cl, make_profile(libs=[("spring-data-jpa", None)]), None)
    assert h.matches(cl, make_profile(libs=[("jpa", None)]), None)
    assert not h.matches(cl, make_profile(libs=[("lombok", None)]), None)


# ---------------------------------------------------------------------------------------------------------------------
# registry precedence + pre-emptive selection
# ---------------------------------------------------------------------------------------------------------------------
def test_registry_precedence_for_missing_javax_package():
    reg = HandlerRegistry.default(None)
    cl = _cluster(_err("package javax.persistence does not exist"))
    ids = [h.id for h in reg.matching(cl, _javax_profile({"javax.persistence": 3}), None)]
    assert ids[0] == "jakarta-namespace-sweep"
    for later in ("dependency-relocation", "add-known-dependency"):
        if later in ids:
            assert ids.index("jakarta-namespace-sweep") < ids.index(later)
    order = reg.ids()
    assert order.index("jakarta-namespace-sweep") < order.index("missing-dependency-version")
    assert order.index("spring-data-crud-repository") < order.index("missing-dependency-version")


def test_pre_handler_reasons(cfg):
    boot = ProjectProfile(root="/x", name="x", case_type=CaseType.SPRING_BOOT,
                          libraries=[LibraryUsage(name="spring-data-jpa")],
                          source=SourceStats(javax_imports={"javax.persistence": 2, "javax.servlet": 1}))
    reasons = dict(pre_handler_reasons(boot, cfg))
    assert "javax.persistence, javax.servlet" in reasons["jakarta-namespace-sweep"]
    assert "spring-data-crud-repository" in reasons
    pure = ProjectProfile(root="/x", name="x", case_type=CaseType.PURE_JAVA,
                          source=SourceStats(javax_imports={"javax.sql": 1, "javax.swing": 2}))
    ids = [i for i, _ in pre_handler_reasons(pure, cfg)]
    assert "jakarta-namespace-sweep" not in ids and "spring-data-crud-repository" not in ids


# ---------------------------------------------------------------------------------------------------------------------
# E14b: CRUD lost through a library base interface (reactive only)
# ---------------------------------------------------------------------------------------------------------------------
LIB_REPO_PATH = "src/main/java/com/example/MemberRepository.java"
LIB_REPO = ("package com.example;\n\nimport com.company.jpa.BaseRepository;\n\n"
            "public interface MemberRepository extends BaseRepository<Member, Long> {\n}\n")
FIND_ALL_RAW = ("no suitable method found for findAll(no arguments)\n"
                "    method PagingAndSortingRepository.findAll(Sort) is not applicable\n"
                "  location: variable repository of type com.example.MemberRepository")


def _find_all_cluster(kit):
    svc = _java(kit.root, "src/main/java/com/example/MemberService.java", "package com.example;\nclass MemberService {}\n")
    return _cluster(_err("no suitable method found for findAll(no arguments)", file=svc, raw=FIND_ALL_RAW,
                         location="variable repository of type com.example.MemberRepository"))


def test_crud_library_base_gets_list_crud_repository(fake_kit):
    _java(fake_kit.root, LIB_REPO_PATH, LIB_REPO)
    cl = _find_all_cluster(fake_kit)
    h = _crud(fake_kit)
    assert h.matches(cl, make_profile(libs=[("spring-data-jpa", None)]), None)
    assert [c.path for c in h.apply(fake_kit.ws, cl, None, None)] == [LIB_REPO_PATH]
    text = (fake_kit.root / LIB_REPO_PATH).read_text()
    assert "extends BaseRepository<Member, Long>, ListCrudRepository<Member, Long> {" in text
    assert ("import com.company.jpa.BaseRepository;\n"
            "import org.springframework.data.repository.ListCrudRepository;\n") in text
    assert h.flags == ["spring-data-crud-restored"]
    assert h.notes == ["added ListCrudRepository<T, ID> to 1 repository interface(s) whose base interface comes from "
                       "a library built on Spring Data 2 (PagingAndSortingRepository lost its CRUD methods in Spring "
                       "Data 3): MemberRepository"]
    assert h.apply(fake_kit.ws, cl, None, None) == []                       # idempotent (now CRUD-bearing)


def test_crud_library_base_with_one_type_argument_is_a_follow_up(fake_kit):
    _java(fake_kit.root, LIB_REPO_PATH, LIB_REPO.replace("BaseRepository<Member, Long>", "BaseRepository<Member>"))
    h = _crud(fake_kit)
    assert h.apply(fake_kit.ws, _find_all_cluster(fake_kit), None, None) == []
    assert "ListCrud" not in (fake_kit.root / LIB_REPO_PATH).read_text()
    assert h.flags == []
    assert h.notes == ["follow-up: MemberRepository lost its CRUD methods through a library base interface "
                       "(BaseRepository<Member>) whose type arguments could not be mapped — add ListCrudRepository<T, "
                       "ID> by hand or upgrade the library"]


def test_crud_project_base_with_crud_is_left_alone(fake_kit):
    _java(fake_kit.root, LIB_REPO_PATH, LIB_REPO.replace("import com.company.jpa.BaseRepository;\n", ""))
    base = _java(fake_kit.root, "src/main/java/com/example/BaseRepository.java",
                 "package com.example;\n\nimport org.springframework.data.jpa.repository.JpaRepository;\n\n"
                 "public interface BaseRepository<T, ID> extends JpaRepository<T, ID> {\n}\n")
    h = _crud(fake_kit)
    assert h.apply(fake_kit.ws, _find_all_cluster(fake_kit), None, None) == []
    assert "ListCrud" not in (fake_kit.root / LIB_REPO_PATH).read_text()
    assert "ListCrud" not in (fake_kit.root / base).read_text()
    assert h.notes == []


def test_crud_spring_data_non_crud_supers_are_not_library_bases(fake_kit):
    _java(fake_kit.root, LIB_REPO_PATH,
          "package com.example;\n\nimport org.springframework.data.repository.Repository;\n\n"
          "public interface MemberRepository extends Repository<Member, Long> {\n}\n")
    h = _crud(fake_kit)
    assert h.apply(fake_kit.ws, _find_all_cluster(fake_kit), None, None) == []
    assert h.notes == []


def test_crud_library_base_never_pre_emptive(fake_kit):
    _java(fake_kit.root, LIB_REPO_PATH, LIB_REPO.replace("package com.example;\n",
                                                         "package com.example;\nimport jakarta.persistence.Id;\n"))
    h = _crud(fake_kit)
    assert h.apply(fake_kit.ws, None, make_profile(libs=[("spring-data-jpa", None)]), None) == []
    assert "ListCrud" not in (fake_kit.root / LIB_REPO_PATH).read_text()
    assert h.notes == []
