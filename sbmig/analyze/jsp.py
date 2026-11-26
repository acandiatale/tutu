"""JSP와 태그 파일에서 자바 타입 이름이 나올 수 있는 영역만 골라내는 모듈.
소스 스캐너가 javax EE 타입을 쓰는 JSP를 셀 때 쓴다(``SourceStats.jsp_javax_files``).

A JSP is compiled by the servlet container at run time, not by the Maven build, and OpenRewrite skips it, so a
build can pass while the JSP fails on Tomcat 10 or later. Entry points: :func:`jsp_code_spans`,
:func:`jsp_javax_refs`, :func:`javax_ee_token`. The javax -> jakarta package table is ``jakarta_packages.yml``
beside this module.

Pure, dependency-free. OpenRewrite does not parse JSPs (`openrewrite.exclusions` lists `**/*.jsp`). The only
places where a JSP names Java types are:

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

# 왼쪽부터 한 번에 읽는다. JSP 주석은 통째로 건너뛰므로 그 안은 영역이 되지 않는다
_JSP_RE = re.compile(
    r"<%--[\s\S]*?--%>"                                                            # JSP 주석, 건너뛴다
    r"|<%@(?P<dir>[\s\S]*?)%>"                                                      # 지시자
    r"|<%[!=]?(?P<code>[\s\S]*?)%>"                                                 # 스크립틀릿, 식, 선언
    r"|<jsp:directive\.(?P<xdir>page|tag)\b(?P<xattrs>[^>]*)>"                     # XML 지시자
    r"|<jsp:(?P<xtag>scriptlet|expression|declaration)\s*>(?P<xcode>[\s\S]*?)</jsp:(?P=xtag)\s*>"
    r"|<jsp:useBean\b(?P<bean>[^>]*)>")
_DIRECTIVE_KIND_RE = re.compile(r"\s*(?:page|tag)\b")
_IMPORT_ATTR_RE = re.compile(r"""\bimport\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_BEAN_ATTR_RE = re.compile(r"""\b(?:class|type)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# 코드 영역을 검사하기 전에 가릴 자바 주석(그룹 1)과 문자열, 텍스트 블록, 문자 리터럴(그룹 2)
_JAVA_LIT_RE = re.compile(r'(//[^\n]*|/\*[\s\S]*?\*/)|("""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')')
_JAVAX_TOKEN_RE = re.compile(r"(?<![\w$.])javax(?:\.[A-Za-z_$][\w$]*)+(?:\.\*)?")


def _attr_spans(attrs: str, base: int, rx: re.Pattern, kind: str) -> list[tuple[int, int, str]]:
    """속성 문자열에서 ``rx``가 찾은 따옴표 안 값의 (시작, 끝, kind) 목록을 반환한다.
    위치는 파일 전체 기준이다."""
    out = []
    for m in rx.finditer(attrs):
        g = 1 if m.group(1) is not None else 2
        out.append((base + m.start(g), base + m.end(g), kind))
    return out


def jsp_code_spans(text: str) -> list[tuple[int, int, str]]:
    """JSP와 태그 파일에서 자바 타입 이름이 나오는 영역을 파일 순서대로 반환한다.
    각 항목은 ``(시작, 끝, kind)``다. kind는 아래 중 하나다.
    - ``import``: page, tag 지시자의 import 속성
    - ``code``: 스크립틀릿, 식, 선언
    - ``bean``: useBean의 class, type 속성"""
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
    """JSP 계열 파일(.jsp, .jspf, .jspx, .tag, .tagx)이면 True를 반환한다. 대소문자는 구분하지 않는다."""
    return rel.lower().endswith(JSP_SUFFIXES)


# ---------------------------------------------------------------------------------------------------------------------
# 분석기에서 쓰는 javax 이름 탐지
# ---------------------------------------------------------------------------------------------------------------------
_JAKARTA_TABLE = Path(__file__).resolve().with_name("jakarta_packages.yml")


@lru_cache(maxsize=1)
def jakarta_rules() -> tuple[tuple[str, bool], ...]:
    """javax에서 jakarta로 바꿀 패키지 표를 (원래 패키지, 하위 패키지도 바꾸는지) 목록으로 반환한다.
    한 번만 읽어 캐시한다. 파일이 없으면 빈 목록을 반환한다."""
    try:
        doc = yaml.safe_load(_JAKARTA_TABLE.read_text(encoding="utf-8")) or {}
    except OSError:
        return ()
    return tuple((str(p["from"]), bool(p.get("recursive", True))) for p in doc.get("packages") or [])


def javax_ee_token(token: str, rules: Iterable[tuple[str, bool]]) -> bool:
    """``token``(예: ``javax.servlet.http.Cookie``)이 표에 있는 javax EE 이름인지 확인한다.
    가장 긴 접두어 규칙을 따른다. 하위를 포함하지 않는 규칙은 ``*``나 대문자로 시작하는 클래스 이름만 받는다."""
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
    """JSP의 코드 영역 안에서 표에 있는 javax 이름 목록을 반환한다.
    스크립틀릿 안의 문자열과 주석은 제외한다."""
    rules = tuple(rules) if rules is not None else jakarta_rules()
    out: list[str] = []
    for s, e, kind in jsp_code_spans(text):
        chunk = text[s:e]
        # 문자열 안의 "javax.servlet"은 참조가 아니다. 문자열과 주석을 같은 길이의 공백으로 가린다
        if kind == "code":
            chunk = _JAVA_LIT_RE.sub(lambda m: " " * len(m.group(0)), chunk)
        out += [m.group(0) for m in _JAVAX_TOKEN_RE.finditer(chunk) if javax_ee_token(m.group(0), rules)]
    return out
