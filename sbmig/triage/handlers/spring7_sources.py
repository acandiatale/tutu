"""Deterministic Java-source handlers for Spring Boot 4 / Spring Security 7 / Spring Data 4 leftovers (round 10).

* ``boot4-type-relocation-sweep`` — the Boot 4 type moves ``UpgradeSpringBoot_4_0`` (rewrite-spring 6.37.0) does not
  make (``web.servlet.error`` → ``webmvc.error``, ``web.server.ErrorPage*`` → ``web.error``, ``web.servlet.server`` →
  ``web.server.servlet``, ``MultipartConfigFactory`` / ``Ordered*Filter`` → ``boot.servlet``, ``ServerProperties``,
  ``HttpMessageConverters``, ``ServletComponentScan``), table ``boot4_types.yml``. The project recipe
  ``com.company.migrate.Boot4TypeRelocations`` renames them in every file OpenRewrite attributed; this handler covers
  the files it skipped and wildcard imports. Pre-emptive after the spring recipes of a Boot project (only when no pom
  still pins a Boot < 4 parent / BOM) and reactive on the javac errors.
* ``security-path-pattern-matcher`` — Spring Security 7 removed ``AntPathRequestMatcher`` / ``MvcRequestMatcher``
  (absent from spring-security-web 7.1.0): ``new AntPathRequestMatcher(p[, m])`` / ``AntPathRequestMatcher.antMatcher``
  / ``new MvcRequestMatcher(x, p)`` → ``PathPatternRequestMatcher.pathPattern([HttpMethod, ]p)``, remaining type
  references → ``RequestMatcher``. A literal pattern with ``**`` anywhere but as the last segment gets a
  ``// TODO sbmig:`` line (PathPattern rejects it at startup). Reactive only.
* ``spring-data-specification-unrestricted`` — Spring Data JPA 4 added ``Specification.where(PredicateSpecification)``,
  so ``Specification.where(null)`` is ambiguous: → ``Specification.unrestricted()`` (verified with javap,
  spring-data-jpa 4.1.0). Reactive only.

Every handler edits through ``Workspace.read`` / ``write`` (detected encoding and ``\\r\\n`` kept), leaves string /
char literals and comments alone, is idempotent and reports flags / notes like every ``EdgeHandler``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional

import yaml

from sbmig.core.models import CaseType, ErrorCluster, ErrorKind, Phase, ProjectProfile
from sbmig.core.workspace import Workspace

from .base import all_poms, cluster_text, version_lt
from .edge_handlers import EdgeHandler
from .source_handlers import _LIT_RE, _mask, _under_target

log = logging.getLogger("sbmig.triage.handlers.spring7")

_DATA_DIR = Path(__file__).parent
_KINDS = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE})
_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+(static[ \t]+)?([\w$.]+?)(\.\*)?[ \t]*;[^\r\n]*", re.M)
_PACKAGE_RE = re.compile(r"^[ \t]*package\s+([\w.]+)\s*;[^\r\n]*", re.M)


# ---------------------------------------------------------------------------------------------------------------------
# shared source helpers
# ---------------------------------------------------------------------------------------------------------------------
def _literal_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every comment and string / char / text-block literal."""
    return [(m.start(), m.end()) for m in _LIT_RE.finditer(text)]


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def imports_of(text: str) -> list[tuple[int, int, bool, str, bool]]:
    """(start, end, static, name, wildcard) of every import line (comment / literal safe)."""
    masked = _mask(text)
    return [(m.start(), m.end(), bool(m.group(1)), m.group(2), bool(m.group(3))) for m in _IMPORT_RE.finditer(masked)]


def _imported(text: str, fqcn: str) -> bool:
    pkg = fqcn.rsplit(".", 1)[0]
    for _a, _b, static, name, star in imports_of(text):
        if not static and ((not star and name == fqcn) or (star and name == pkg)):
            return True
    pm = _PACKAGE_RE.search(_mask(text))
    return bool(pm and pm.group(1) == pkg)


def add_imports(text: str, fqcns: list[str]) -> str:
    """`text` with `import <fqcn>;` for every name not imported yet (after the last import, else the package line)."""
    todo = [f for f in dict.fromkeys(fqcns) if not _imported(text, f)]
    if not todo:
        return text
    nl = _nl(text)
    block = "".join(f"{nl}import {f};" for f in todo)
    imps = imports_of(text)
    if imps:
        pos = imps[-1][1]
        return text[:pos] + block + text[pos:]
    pm = _PACKAGE_RE.search(_mask(text))
    if pm:
        pos = pm.end()
        return text[:pos] + nl + block + text[pos:]
    return block.lstrip("\r\n") + nl + text


def remove_import_lines(text: str, pred) -> str:
    """Drop every import line for which `pred(static, name, wildcard)` holds (with its line break)."""
    out = text
    for a, b, static, name, star in reversed(imports_of(text)):
        if pred(static, name, star):
            end = b
            if out[end:end + 2] == "\r\n":
                end += 2
            elif out[end:end + 1] == "\n":
                end += 1
            out = out[:a] + out[end:]
    return out


def _arg_mask(text: str) -> str:
    """Same length as `text`: comments blanked, literal contents replaced by `x` (quotes and newlines kept) — so
    parentheses / commas inside strings never count while every argument keeps a non-blank span."""
    def repl(m: re.Match) -> str:
        s = m.group(0)
        if m.group(1) is not None:                                     # comment
            return re.sub(r"[^\n]", " ", s)
        q = 3 if s.startswith('"""') else 1
        inner = re.sub(r"[^\n]", "x", s[q:-q]) if len(s) > 2 * q else ""
        return s[:q] + inner + s[-q:]
    return _LIT_RE.sub(repl, text)


def _body_mask(text: str) -> str:
    """Masked text (comments / literals blank) with the package and import lines blanked too."""
    masked = _mask(text)
    for rx in (_IMPORT_RE, _PACKAGE_RE):
        masked = rx.sub(lambda m: " " * len(m.group(0)), masked)
    return masked


def _uses_simple(body: str, simple: str) -> bool:
    return re.search(rf"(?<![\w$.]){re.escape(simple)}(?![\w$])", body) is not None


def _java_files(ws: Workspace) -> list[str]:
    return [f for f in ws.java_files() if not _under_target(f)]


def _matching_paren(masked: str, open_idx: int) -> int:
    """Index of the `)` closing the `(` at `open_idx` (on masked text), -1 when unbalanced."""
    depth = 0
    for i in range(open_idx, len(masked)):
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_args(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """Top-level argument spans between `start` (after `(`) and `end` (the `)`), stripped. Only ( [ { nest (a `<` may
    be a comparison); generic type arguments with a comma inside a call argument are not expected here."""
    spans: list[tuple[int, int]] = []
    depth, seg = 0, start
    for i in range(start, end):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            spans.append((seg, i))
            seg = i + 1
    spans.append((seg, end))
    out = []
    for a, b in spans:
        while a < b and masked[a].isspace():
            a += 1
        while b > a and masked[b - 1].isspace():
            b -= 1
        if a < b:
            out.append((a, b))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# boot4-type-relocation-sweep
# ---------------------------------------------------------------------------------------------------------------------
class TypeMove(NamedTuple):
    from_: str
    to: str

    @property
    def old_package(self) -> str:
        return self.from_.rsplit(".", 1)[0]

    @property
    def simple(self) -> str:
        return self.from_.rsplit(".", 1)[1]


@lru_cache(maxsize=1)
def boot4_type_table() -> tuple[tuple[TypeMove, ...], frozenset[str]]:
    """(moves, packages_gone) of boot4_types.yml."""
    doc = yaml.safe_load((_DATA_DIR / "boot4_types.yml").read_text(encoding="utf-8")) or {}
    moves = tuple(TypeMove(str(t["from"]), str(t["to"])) for t in doc.get("types") or [])
    return moves, frozenset(str(p) for p in doc.get("packages_gone") or [])


@dataclass
class RelocationResult:
    text: str
    renamed: list[str] = field(default_factory=list)           # old FQCNs replaced (one entry per occurrence)
    imports_added: list[str] = field(default_factory=list)
    wildcards_dropped: list[str] = field(default_factory=list)


def relocate_types(text: str, moves: tuple[TypeMove, ...], packages_gone: frozenset[str] = frozenset()
                   ) -> RelocationResult:
    """Rename fully-qualified references (imports included) of the table's old types outside literals / comments,
    then, for a wildcard import of an old package, import the moved types the file uses by their simple name and
    drop the wildcard when the package is gone."""
    res = RelocationResult(text)
    if not moves:
        return res
    by_from = {m.from_: m for m in moves}
    rx = re.compile(r"(?<![\w$.])(" + "|".join(re.escape(m.from_) for m in sorted(moves, key=lambda x: -len(x.from_)))
                    + r")(?![\w$])")
    spans = _literal_spans(text)
    out: list[str] = []
    last = 0
    for m in rx.finditer(text):
        if _in_spans(m.start(), spans):
            continue
        out.append(text[last:m.start()])
        out.append(by_from[m.group(1)].to)
        last = m.end()
        res.renamed.append(m.group(1))
    if res.renamed:
        out.append(text[last:])
        text = "".join(out)
    wild = {name for _a, _b, static, name, star in imports_of(text) if star and not static}
    old_pkgs = {m.old_package for m in moves}
    hit = wild & old_pkgs
    if hit:
        body = _body_mask(text)
        need = [m.to for m in moves if m.old_package in hit and _uses_simple(body, m.simple)]
        gone = hit & packages_gone
        if gone:
            text = remove_import_lines(text, lambda static, name, star: star and not static and name in gone)
            res.wildcards_dropped = sorted(gone)
        before = text
        text = add_imports(text, need)
        if text != before:
            res.imports_added = [f for f in need if f"import {f};" in text and f"import {f};" not in before]
    res.text = text
    return res


_BOOT_POM_VERSION_RE = re.compile(
    r"<artifactId>\s*spring-boot-(?:starter-parent|dependencies)\s*</artifactId>\s*<version>\s*([^<\s]+)\s*</version>")


def boot_below_4_declared(ws: Workspace) -> Optional[str]:
    """`<pom>: <version>` of the first pom that still pins a Boot parent / BOM below 4 (local ${prop} resolved), or
    None. The pre-emptive sweep must not move types before the Boot 4 jars are on the classpath."""
    for rel in all_poms(ws):
        try:
            text = ws.read(rel)
        except OSError:
            continue
        for m in _BOOT_POM_VERSION_RE.finditer(_mask_xml(text)):
            v = m.group(1)
            pm = re.fullmatch(r"\$\{([^}]+)\}", v)
            if pm:
                pv = re.search(rf"<{re.escape(pm.group(1))}>\s*([^<\s]+)\s*</{re.escape(pm.group(1))}>", text)
                v = pv.group(1) if pv else v
            if version_lt(v, "4.0.0") is True:
                return f"{rel}: {v}"
    return None


def _mask_xml(text: str) -> str:
    return re.sub(r"<!--.*?-->", lambda m: " " * len(m.group(0)), text, flags=re.S)


def _boot4_triggers() -> tuple[str, ...]:
    moves, _gone = boot4_type_table()
    pkgs = sorted({m.old_package for m in moves}, key=len, reverse=True)
    simples = sorted({m.simple for m in moves}, key=len, reverse=True)
    live = sorted({m.simple for m in moves if m.old_package not in _gone}, key=len, reverse=True)
    return (r"package (?:" + "|".join(re.escape(p) for p in pkgs) + r") does not exist",
            r"symbol:\s+class (?:" + "|".join(simples) + r")\s+location:\s+package org\.springframework\.boot\.",
            r"symbol:\s+class (?:" + "|".join(live) + r")\b")


class Boot4TypeRelocationSweep(EdgeHandler):
    id = "boot4-type-relocation-sweep"
    description = ("Spring Boot 4 type moves the 4.0 umbrella misses (error controller / attributes, ErrorPage, "
                   "servlet web server factory, MultipartConfigFactory, Ordered*Filter, ServerProperties, "
                   "HttpMessageConverters, ServletComponentScan) — table sbmig/triage/handlers/boot4_types.yml")
    kinds = _KINDS
    triggers = _boot4_triggers()
    preemptive_phase = Phase.SPRING

    def applicable(self, cluster: Optional[ErrorCluster], profile: Optional[ProjectProfile], cfg) -> bool:
        if cluster is not None or profile is None:
            return True
        return bool(profile.spring_boot_version or profile.boot_modules
                    or profile.case_type in (CaseType.SPRING_BOOT, CaseType.MSA))

    def edit(self, ws: Workspace, cluster, profile, cfg) -> list[str]:
        if cluster is None:
            if not self.applicable(None, profile, cfg):
                return []
            pinned = boot_below_4_declared(ws)
            if pinned:
                log.info("boot4-type-relocation-sweep: Boot < 4 still pinned (%s) — not run pre-emptively", pinned)
                return []
        moves, gone = boot4_type_table()
        changed: list[str] = []
        renamed: list[str] = []
        for f in _java_files(ws):
            try:
                text = ws.read(f)
            except OSError as e:
                log.debug("boot4-type-relocation-sweep: cannot read %s: %s", f, e)
                continue
            if "org.springframework.boot." not in text:
                continue
            res = relocate_types(text, moves, gone)
            if res.text != text:
                ws.write(f, res.text)
                changed.append(f)
                renamed += res.renamed + res.imports_added
        if changed:
            self.flag("boot4-types-relocated")
            kinds = sorted({n.rsplit(".", 1)[-1] for n in renamed})
            self.note(f"moved Spring Boot 4 relocated types in {len(changed)} file(s) the recipes had not converted "
                      f"(e.g. {', '.join(changed[:3])}): {', '.join(kinds[:8])}" + (" …" if len(kinds) > 8 else ""))
            log.info("boot4-type-relocation-sweep: %d file(s): %s", len(changed), kinds)
        return changed


# ---------------------------------------------------------------------------------------------------------------------
# security-path-pattern-matcher
# ---------------------------------------------------------------------------------------------------------------------
ANT = "org.springframework.security.web.util.matcher.AntPathRequestMatcher"
MVC = "org.springframework.security.web.servlet.util.matcher.MvcRequestMatcher"
PPRM = "org.springframework.security.web.servlet.util.matcher.PathPatternRequestMatcher"
REQUEST_MATCHER = "org.springframework.security.web.util.matcher.RequestMatcher"
HTTP_METHOD = "org.springframework.http.HttpMethod"
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")
_SIMPLE_EXPR_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*")
_STRING_RE = re.compile(r'"((?:\\.|[^"\\\n])*)"')
_HTTP_METHOD_CONST_RE = re.compile(r"(?:org\.springframework\.http\.)?HttpMethod\s*\.\s*(" + "|".join(HTTP_METHODS)
                                   + r")\b(?:\s*\.\s*(?:name|toString)\s*\(\s*\))?")


def pathpattern_problem(pattern: str) -> bool:
    """True when a literal Ant pattern cannot be a PathPattern: `**` anywhere except as the whole last segment."""
    if "**" not in pattern:
        return False
    if pattern.count("**") > 1:
        return True
    return not (pattern in ("**", "/**") or pattern.endswith("/**"))


@dataclass
class MatcherResult:
    text: str
    rewritten: int = 0
    left: list[str] = field(default_factory=list)              # call sites not rewritten (shown in a note)
    todos: list[str] = field(default_factory=list)             # literal patterns flagged with a TODO line


def _method_arg(arg: str) -> Optional[str]:
    """HttpMethod expression for an AntPathRequestMatcher `httpMethod` String argument; '' = no method (null);
    None = cannot convert safely."""
    a = arg.strip()
    if a == "null":
        return ""
    sm = _STRING_RE.fullmatch(a)
    if sm:
        v = sm.group(1).strip().upper()
        return f"HttpMethod.{v}" if v in HTTP_METHODS else None
    hm = _HTTP_METHOD_CONST_RE.fullmatch(a)
    if hm:
        return f"HttpMethod.{hm.group(1)}"
    return None


def rewrite_request_matchers(text: str) -> MatcherResult:
    """Rewrite AntPathRequestMatcher / MvcRequestMatcher usages of one Java file (see the module doc)."""
    res = MatcherResult(text)
    if "AntPathRequestMatcher" not in text and "MvcRequestMatcher" not in text:
        return res
    # fully-qualified references in code (not the import lines) → simple names (the import logic below adds what is needed)
    for fq in (ANT, MVC):
        body = _body_mask(text)
        hits = [m for m in re.finditer(rf"(?<![\w$.]){re.escape(fq)}(?![\w$])", body)]
        for m in reversed(hits):
            text = text[:m.start()] + fq.rsplit(".", 1)[1] + text[m.end():]
        if hits and not _imported(text, fq):
            text = add_imports(text, [fq])          # keeps an unconvertible call compiling-as-before (removed below)
    edits: list[tuple[int, int, str]] = []
    todo_lines: set[int] = set()
    masked = _arg_mask(text)
    for rx in (_IMPORT_RE, _PACKAGE_RE):
        masked = rx.sub(lambda mm: " " * len(mm.group(0)), masked)
    calls = [(m, "new-ant") for m in re.finditer(r"\bnew\s+AntPathRequestMatcher\s*\(", masked)]
    calls += [(m, "static-ant") for m in re.finditer(r"(?<![\w$.])AntPathRequestMatcher\s*\.\s*antMatcher\s*\(", masked)]
    calls += [(m, "new-mvc") for m in re.finditer(r"\bnew\s+MvcRequestMatcher\s*\(", masked)]
    for m, kind in calls:
        open_idx = m.end() - 1
        close = _matching_paren(masked, open_idx)
        if close < 0:
            continue
        args = [text[a:b] for a, b in _split_args(masked, open_idx + 1, close)]
        new = _rewrite_call(kind, args)
        if new is None:
            res.left.append(re.sub(r"\s+", " ", text[m.start():close + 1])[:100])
            continue
        edits.append((m.start(), close + 1, new))
        pattern_arg = _pattern_arg(kind, args)
        sm = _STRING_RE.fullmatch(pattern_arg.strip()) if pattern_arg else None
        if sm and pathpattern_problem(sm.group(1)):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if line_start not in todo_lines:
                todo_lines.add(line_start)
                res.todos.append(sm.group(1))
    if not edits:
        res.text = text
        return res
    nl = _nl(text)
    inserts = []
    for line_start in todo_lines:
        indent = re.match(r"[ \t]*", text[line_start:]).group(0)
        inserts.append((line_start, line_start, f"{indent}// TODO sbmig: PathPattern allows ** only as the last path "
                                                f"segment — rewrite this pattern (e.g. /{{*rest}} or explicit "
                                                f"segments), it fails at startup{nl}"))
    for a, b, s in sorted(edits + inserts, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:a] + s + text[b:]
    res.rewritten = len(edits)
    # remaining type references (declarations, generics, casts) → RequestMatcher, unless call sites were left
    if not res.left:
        spans = _literal_spans(text)
        body_rx = re.compile(r"(?<![\w$.])(?:AntPathRequestMatcher|MvcRequestMatcher)(?![\w$])(?!\s*(?:::|\.\s*Builder))")
        body = _body_mask(text)
        if body_rx.search(body):
            pieces = []
            last = 0
            for mm in body_rx.finditer(body):
                if _in_spans(mm.start(), spans):
                    continue
                pieces.append(text[last:mm.start()])
                pieces.append("RequestMatcher")
                last = mm.end()
            pieces.append(text[last:])
            text = "".join(pieces)
        text = remove_import_lines(text, lambda static, name, star: not static and not star and name in (ANT, MVC))
    need = [PPRM]
    body = _body_mask(text)
    other_http_method = any(not static and not star and name.endswith(".HttpMethod") and name != HTTP_METHOD
                            for _a, _b, static, name, star in imports_of(text))
    if re.search(r"(?<![\w$.])HttpMethod\s*\.", body) and not other_http_method:
        need.append(HTTP_METHOD)
    if _uses_simple(body, "RequestMatcher"):
        need.append(REQUEST_MATCHER)
    res.text = add_imports(text, need)
    return res


def _replace_outside(text: str, rx: re.Pattern, repl: str, spans: list[tuple[int, int]]) -> list[str]:
    out, last = [], 0
    for m in rx.finditer(text):
        if _in_spans(m.start(), spans):
            continue
        out.append(text[last:m.start()])
        out.append(repl)
        last = m.end()
    out.append(text[last:])
    return out


def _is_http_method(arg: str) -> bool:
    return arg.strip().startswith(("HttpMethod", "org.springframework.http.HttpMethod"))


def _pattern_arg(kind: str, args: list[str]) -> Optional[str]:
    """The URL-pattern argument of a matcher call (None when there is none)."""
    if kind == "new-ant":
        return args[0] if args else None
    if kind == "static-ant":
        if len(args) == 1:
            return None if _is_http_method(args[0]) else args[0]
        return args[1] if len(args) == 2 else None
    if kind == "new-mvc":
        return args[1] if len(args) == 2 else None
    return None


def _rewrite_call(kind: str, args: list[str]) -> Optional[str]:
    pp = "PathPatternRequestMatcher.pathPattern"
    if kind == "new-ant":
        if len(args) == 1:
            return f"{pp}({args[0]})"
        if len(args) == 2:
            method = _method_arg(args[1])
            if method == "":
                return f"{pp}({args[0]})"
            if method:
                return f"{pp}({method}, {args[0]})"
            m = args[1].strip()
            if _SIMPLE_EXPR_RE.fullmatch(m):        # a variable / field: no text = any method, exactly as before
                return (f"(org.springframework.util.StringUtils.hasText({m}) ? {pp}(HttpMethod.valueOf({m}), "
                        f"{args[0]}) : {pp}({args[0]}))")
        return None                                  # caseSensitive / UrlPathHelper variants: semantics differ
    if kind == "static-ant":
        if len(args) == 1:
            a = args[0].strip()
            if _is_http_method(a):
                return f'{pp}({a}, "/**")'
            return f"{pp}({a})"
        if len(args) == 2:
            return f"{pp}({args[0]}, {args[1]})"
        return None
    if kind == "new-mvc":
        if len(args) == 2:                            # (HandlerMappingIntrospector, pattern)
            return f"{pp}({args[1]})"
        return None
    return None


class SpringSecurityPathPatternMatcher(EdgeHandler):
    id = "security-path-pattern-matcher"
    description = ("Spring Security 7 removed AntPathRequestMatcher / MvcRequestMatcher: rewrite them to "
                   "PathPatternRequestMatcher.pathPattern(...) (TODO line for ** inside a pattern)")
    kinds = _KINDS
    triggers = (r"\bAntPathRequestMatcher\b", r"\bMvcRequestMatcher\b")

    def applicable(self, cluster, profile, cfg) -> bool:
        if profile is None or cluster is not None:
            return True
        return profile.has_library("spring-security")

    def edit(self, ws: Workspace, cluster, profile, cfg) -> list[str]:
        if cluster is None:
            return []                                 # reactive only: the API exists until Security 7 is on the classpath
        changed: list[str] = []
        todos: list[str] = []
        left: list[str] = []
        n = 0
        for f in _java_files(ws):
            try:
                text = ws.read(f)
            except OSError:
                continue
            res = rewrite_request_matchers(text)
            left += [f"{f}: {x}" for x in res.left]
            if res.text != text:
                ws.write(f, res.text)
                changed.append(f)
                n += res.rewritten
                todos += res.todos
        if changed:
            self.flag("security-path-matchers-rewritten")
            self.note(f"AntPathRequestMatcher / MvcRequestMatcher → PathPatternRequestMatcher in {len(changed)} file(s) "
                      f"({n} call site(s), e.g. {', '.join(changed[:3])}); PathPattern matches the path within the "
                      "application and allows ** only as the last segment — check URL patterns read from the database")
        if todos:
            self.note(f"follow-up: {len(todos)} literal security pattern(s) are not valid PathPatterns "
                      f"(e.g. {todos[0]}) — marked `// TODO sbmig:` in the source; they fail at startup until rewritten")
        if left:
            self.note(f"follow-up: {len(left)} request-matcher call(s) left as they were (case-sensitive / UrlPathHelper "
                      f"variants or a computed method), e.g. {left[0]}")
        return changed


# ---------------------------------------------------------------------------------------------------------------------
# spring-data-specification-unrestricted
# ---------------------------------------------------------------------------------------------------------------------
_SPEC_WHERE_NULL_RE = re.compile(
    r"(?<![\w$])((?:org\.springframework\.data\.jpa\.domain\.)?Specification\s*\.\s*(<[^<>()]*(?:<[^<>()]*>[^<>()]*)*>\s*)?)"
    r"where\s*\(\s*null\s*\)")
_STATIC_WHERE_IMPORT = "org.springframework.data.jpa.domain.Specification.where"


def rewrite_specification_where_null(text: str) -> tuple[str, int]:
    """`Specification.where(null)` (with an optional type witness) → `Specification.unrestricted()`; a statically
    imported `where(null)` → `Specification.unrestricted()` (import of Specification added). Returns (text, count)."""
    spans = _literal_spans(text)
    out, last, n = [], 0, 0
    for m in _SPEC_WHERE_NULL_RE.finditer(text):
        if _in_spans(m.start(), spans):
            continue
        out.append(text[last:m.start()])
        out.append(m.group(1) + "unrestricted()")
        last = m.end()
        n += 1
    out.append(text[last:])
    text = "".join(out)
    if any(static and not star and name == _STATIC_WHERE_IMPORT for _a, _b, static, name, star in imports_of(text)):
        spans = _literal_spans(text)
        body = _body_mask(text)
        rx = re.compile(r"(?<![\w$.])where\s*\(\s*null\s*\)")
        pieces, last, k = [], 0, 0
        for m in rx.finditer(body):
            if _in_spans(m.start(), spans):
                continue
            pieces.append(text[last:m.start()])
            pieces.append("Specification.unrestricted()")
            last = m.end()
            k += 1
        if k:
            pieces.append(text[last:])
            text = add_imports("".join(pieces), ["org.springframework.data.jpa.domain.Specification"])
            n += k
    return text, n


class SpringDataSpecificationUnrestricted(EdgeHandler):
    id = "spring-data-specification-unrestricted"
    description = ("Spring Data JPA 4: Specification.where(null) is ambiguous (where(PredicateSpecification) was "
                   "added) → Specification.unrestricted()")
    kinds = _KINDS
    triggers = (r"reference to where is ambiguous", r"where\(org\.springframework\.data\.jpa\.domain\.PredicateSpecification",
                r"where\(PredicateSpecification")

    def matches(self, cluster: ErrorCluster, profile: ProjectProfile, cfg) -> bool:
        if not self.triggered(cluster):
            return False
        text = cluster_text(cluster)
        return "Specification" in text or "where is ambiguous" in text

    def edit(self, ws: Workspace, cluster, profile, cfg) -> list[str]:
        if cluster is None:
            return []                                 # reactive only: unrestricted() exists from Spring Data JPA 4.0 on
        changed: list[str] = []
        total = 0
        for f in _java_files(ws):
            try:
                text = ws.read(f)
            except OSError:
                continue
            if "where" not in text or "null" not in text:
                continue
            new, n = rewrite_specification_where_null(text)
            if n and new != text:
                ws.write(f, new)
                changed.append(f)
                total += n
        if changed:
            self.flag("specification-unrestricted")
            self.note(f"Specification.where(null) → Specification.unrestricted() ({total} call(s) in "
                      f"{', '.join(changed[:3])}{' …' if len(changed) > 3 else ''})")
        return changed


def spring7_source_handlers(cfg=None) -> list:
    return [Boot4TypeRelocationSweep(), SpringSecurityPathPatternMatcher(), SpringDataSpecificationUnrestricted()]


__all__ = [
    "Boot4TypeRelocationSweep",
    "SpringDataSpecificationUnrestricted",
    "SpringSecurityPathPatternMatcher",
    "add_imports",
    "boot4_type_table",
    "boot_below_4_declared",
    "imports_of",
    "pathpattern_problem",
    "relocate_types",
    "remove_import_lines",
    "rewrite_request_matchers",
    "rewrite_specification_where_null",
    "spring7_source_handlers",
]
