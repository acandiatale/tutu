"""JSP / 태그 파일에서 자바 타입 이름이 나올 수 있는 영역만 골라내는 순수 함수들 — 소스 스캐너가 "아직 javax
EE 타입을 쓰는 JSP" 를 세는 데 쓴다(``SourceStats.jsp_javax_files``, 표는 옆의 ``jakarta_packages.yml``). 왜
따로 보나: JSP 는 Maven 빌드가 아니라 서블릿 컨테이너가 실행 시에 컴파일하고 OpenRewrite 도 JSP 를 건너뛴다
(``openrewrite.exclusions``) — 그래서 빌드는 통과해도 Tomcat 10+ 에서 실행 시 실패한다. v2 는 JSP 안의 javax
를 바꾸지 않는다(알려진 후속 과제, 현재는 프로필에 개수만 남음). 핵심 진입점: :func:`jsp_code_spans` (import /
code / bean 영역), :func:`jsp_javax_refs` (영역 안의 표에 있는 javax 이름), :func:`javax_ee_token`. HTML·EL
``${…}``·taglib uri·JSP 주석·include 는 자바 참조가 아니므로 절대 영역에 넣지 않는다.

JSP / tag-file code regions (pure, dependency-free) — used by the source scanner (`SourceStats.jsp_javax_files`),
with the javax → jakarta package table `jakarta_packages.yml` next to this module.

JSPs are compiled by the servlet container at runtime, never by the Maven build, and OpenRewrite does not parse them
(`openrewrite.exclusions` lists `**/*.jsp`). The only places where a JSP names Java types are:

* the `import` attribute of `<%@ page … %>` / `<%@ tag … %>` and of the XML forms `<jsp:directive.page …/>` /
  `<jsp:directive.tag …/>` (comma-separated list of names),
* scriptlets `<% … %>`, expressions `<%= … %>`, declarations `<%! … %>` and the XML forms `<jsp:scriptlet>`,
  `<jsp:expression>`, `<jsp:declaration>` (Java code),
* the `class=` / `type=` attributes of `<jsp:useBean>`.

Everything else (HTML, EL `${…}`, taglib `uri` values, `<%-- … --%>` comments, include directives) is never a Java
reference. `jsp_code_spans` returns exactly these regions as `(start, end, kind)` with kind `import` | `code` | `bean`.


"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import yaml

JSP_SUFFIXES = (".jsp", ".jspf", ".jspx", ".tag", ".tagx")
JSP_GLOBS = tuple(f"*{s}" for s in JSP_SUFFIXES)

# one left-to-right pass; a JSP comment is consumed whole so nothing inside it is ever a span
_JSP_RE = re.compile(
    r"<%--[\s\S]*?--%>"                                                            # JSP comment (skipped)
    r"|<%@(?P<dir>[\s\S]*?)%>"                                                      # directive
    r"|<%[!=]?(?P<code>[\s\S]*?)%>"                                                 # scriptlet / expression / decl.
    r"|<jsp:directive\.(?P<xdir>page|tag)\b(?P<xattrs>[^>]*)>"                     # XML directive
    r"|<jsp:(?P<xtag>scriptlet|expression|declaration)\s*>(?P<xcode>[\s\S]*?)</jsp:(?P=xtag)\s*>"
    r"|<jsp:useBean\b(?P<bean>[^>]*)>")
_DIRECTIVE_KIND_RE = re.compile(r"\s*(?:page|tag)\b")
_IMPORT_ATTR_RE = re.compile(r"""\bimport\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_BEAN_ATTR_RE = re.compile(r"""\b(?:class|type)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# Java comments (group 1) | string / text-block / char literals (group 2) — masked before scanning code spans
_JAVA_LIT_RE = re.compile(r'(//[^\n]*|/\*[\s\S]*?\*/)|("""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')')
_JAVAX_TOKEN_RE = re.compile(r"(?<![\w$.])javax(?:\.[A-Za-z_$][\w$]*)+(?:\.\*)?")


def _attr_spans(attrs: str, base: int, rx: re.Pattern, kind: str) -> list[tuple[int, int, str]]:
    """속성 문자열에서 ``rx`` 가 잡은 따옴표 안 값의 (시작,
    끝, kind) — 위치는 파일 전체 기준(``base`` 더함)."""
    out = []
    for m in rx.finditer(attrs):
        g = 1 if m.group(1) is not None else 2
        out.append((base + m.start(g), base + m.end(g), kind))
    return out


def jsp_code_spans(text: str) -> list[tuple[int, int, str]]:
    """JSP / 태그 파일에서 자바 타입을 이름으로 쓰는 영역들(파일 순서):
    ``(시작, 끝, kind)``, kind = ``import`` (page / tag 지시자의 import 속성) | ``code`` (스크립틀릿·식·선언)
    | ``bean`` (useBean class / type).

    Regions of a JSP / tag file that name Java types, in file order: `(start, end, kind)`."""
    spans: list[tuple[int, int, str]] = []
    for m in _JSP_RE.finditer(text):
        if m.group("dir") is not None:
            if _DIRECTIVE_KIND_RE.match(m.group("dir")):
                spans += _attr_spans(m.group("dir"), m.start("dir"), _IMPORT_ATTR_RE, "import")
        elif m.group("code") is not None:
            spans.append((m.start("code"), m.end("code"), "code"))
        elif m.group("xattrs") is not None:
            spans += _attr_spans(m.group("xattrs"), m.start("xattrs"), _IMPORT_ATTR_RE, "import")
        elif m.group("xcode") is not None:
            spans.append((m.start("xcode"), m.end("xcode"), "code"))
        elif m.group("bean") is not None:
            spans += _attr_spans(m.group("bean"), m.start("bean"), _BEAN_ATTR_RE, "bean")
    return spans


def is_jsp_path(rel: str) -> bool:
    """JSP 류 파일(.jsp / .jspf / .jspx / .tag / .tagx, 대소문자 무시)이면 True."""
    return rel.lower().endswith(JSP_SUFFIXES)


# ---------------------------------------------------------------------------------------------------------------------
# detection (the analyzer side)
# ---------------------------------------------------------------------------------------------------------------------
_JAKARTA_TABLE = Path(__file__).resolve().with_name("jakarta_packages.yml")


@lru_cache(maxsize=1)
def jakarta_rules() -> tuple[tuple[str, bool], ...]:
    """javax → jakarta 패키지 표(이 모듈 옆 ``jakarta_packages.yml``)의 (원래 패키지, 하위 포함 여부) 목록.
    한 번만 읽어 캐시한다. 파일이 없으면 빈 표(= 아무것도 javax EE 로 보지 않음).

    (from-package, recursive) of the javax → jakarta package table (`jakarta_packages.yml` next to this module)."""
    try:
        doc = yaml.safe_load(_JAKARTA_TABLE.read_text(encoding="utf-8")) or {}
    except OSError:
        return ()
    return tuple((str(p["from"]), bool(p.get("recursive", True))) for p in doc.get("packages") or [])


def javax_ee_token(token: str, rules: Iterable[tuple[str, bool]]) -> bool:
    """``token`` (예: ``javax.servlet.http.Cookie``)이 표에 있는 javax EE 이름인지.
    가장 긴 접두어 규칙이 정하고, 하위 미포함 규칙은 ``*`` 나 대문자로 시작하는 클래스 이름만
    받는다(``javax.annotation.PostConstruct`` 는 대상, JDK 의 ``javax.annotation.processing`` 은 아님).

    `token` (e.g. `javax.servlet.http.Cookie`) is a javax EE name of the table: the longest prefix decides,
    non-recursive rules take only `*` / an upper-case class name."""
    by_from = dict(rules)
    parts = token.split(".")
    for i in range(len(parts) - 1, 1, -1):
        rec = by_from.get(".".join(parts[:i]))
        if rec is None:
            continue
        rest = parts[i:]
        return rec or rest[0] == "*" or rest[0][:1].isupper()
    return False


def jsp_javax_refs(text: str, rules: Optional[Iterable[tuple[str, bool]]] = None) -> list[str]:
    """JSP 의 코드 영역 안에서 표에 있는 javax.* 이름들(스크립틀릿 안의 자바 문자열·주석은 제외).

    Table javax.* names inside the code regions of a JSP (Java literals / comments in scriptlets excluded)."""
    rules = tuple(rules) if rules is not None else jakarta_rules()
    out: list[str] = []
    for s, e, kind in jsp_code_spans(text):
        chunk = text[s:e]
        # 문자열·주석을 같은 길이의 공백으로 가린다 — "javax.servlet" 이라는 문자열은 참조가 아니다
        if kind == "code":
            chunk = _JAVA_LIT_RE.sub(lambda m: " " * len(m.group(0)), chunk)
        out += [m.group(0) for m in _JAVAX_TOKEN_RE.finditer(chunk) if javax_ee_token(m.group(0), rules)]
    return out
