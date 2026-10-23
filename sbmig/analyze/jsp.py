"""JSP / tag-file code regions (pure, dependency-free) — shared by the source scanner (`SourceStats.jsp_javax_files`)
and the `jakarta-namespace-sweep` handler (`rename_javax_jsp`).

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
    out = []
    for m in rx.finditer(attrs):
        g = 1 if m.group(1) is not None else 2
        out.append((base + m.start(g), base + m.end(g), kind))
    return out


def jsp_code_spans(text: str) -> list[tuple[int, int, str]]:
    """Regions of a JSP / tag file that name Java types, in file order: `(start, end, kind)`."""
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
    return rel.lower().endswith(JSP_SUFFIXES)


# ---------------------------------------------------------------------------------------------------------------------
# detection (the analyzer side; the handler renames with the same spans + rename_javax)
# ---------------------------------------------------------------------------------------------------------------------
_JAKARTA_TABLE = Path(__file__).resolve().parent.parent / "triage" / "handlers" / "jakarta_packages.yml"


@lru_cache(maxsize=1)
def jakarta_rules() -> tuple[tuple[str, bool], ...]:
    """(from-package, recursive) of the handler's table (read directly: importing sbmig.triage here is too heavy)."""
    try:
        doc = yaml.safe_load(_JAKARTA_TABLE.read_text(encoding="utf-8")) or {}
    except OSError:
        return ()
    return tuple((str(p["from"]), bool(p.get("recursive", True))) for p in doc.get("packages") or [])


def javax_ee_token(token: str, rules: Iterable[tuple[str, bool]]) -> bool:
    """`token` (e.g. `javax.servlet.http.Cookie`) is renamed by the table — same semantics as the handler's
    `_rename_token`: the longest prefix decides, non-recursive rules take only `*` / an upper-case class name."""
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
    """Table javax.* names inside the code regions of a JSP (Java literals / comments in scriptlets excluded)."""
    rules = tuple(rules) if rules is not None else jakarta_rules()
    out: list[str] = []
    for s, e, kind in jsp_code_spans(text):
        chunk = text[s:e]
        if kind == "code":
            chunk = _JAVA_LIT_RE.sub(lambda m: " " * len(m.group(0)), chunk)
        out += [m.group(0) for m in _JAVAX_TOKEN_RE.finditer(chunk) if javax_ee_token(m.group(0), rules)]
    return out
