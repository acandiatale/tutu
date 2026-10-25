"""Maven log → structured ``BuildError`` list.

A single pass, line-oriented state machine. It understands (Maven 3.6 … 3.9 output, ``-B`` batch mode):

* module / goal context: ``-----< g:a >-----`` headers, ``Building <name> <version>``,
  ``--- <plugin>:<ver>:<goal> (<id>) @ <artifactId> ---`` (short ``compiler:3.13.0:compile`` and the older
  ``maven-compiler-plugin:3.8.1:compile`` forms) and ``Failed to execute goal … on project X``;
* javac / kotlinc diagnostics with ``symbol:`` / ``location:`` / ``required:`` / ``found:`` / ``reason:``
  continuation lines (both the ``COMPILATION ERROR :`` block, where continuations are unprefixed, and the final
  ``[ERROR]``-prefixed repetition; duplicates are merged);
* annotation processor crashes (lombok on new JDKs);
* dependency / parent / plugin resolution, enforcer rules, invalid or non-parseable POMs, test failures, generic
  plugin execution failures.

Everything that is not understood but makes the build fail ends up as one ``UNKNOWN`` error so the caller never
gets an empty list for a failed build.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from sbmig.core.models import GAV, BuildError, ErrorKind

from .signatures import classify_javac, fill_signatures

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")
_TS_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[T ])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\s+(?=\[)")
_LEVEL_RE = re.compile(r"^\[(INFO|ERROR|WARNING|WARN|FATAL|DEBUG)\] ?")

_HEADER_RE = re.compile(r"^-+< (?P<g>[\w.\-]+):(?P<a>[\w.\-]+) >-+\s*$")
_BUILDING_RE = re.compile(r"^Building (?P<name>.+?) (?P<ver>[^\s:]+?)(?:\s+\[\d+/\d+\])?\s*$")
_MOJO_RE = re.compile(
    r"^--- (?:(?P<g>[\w.\-]+):)?(?P<p>[\w.\-]+):(?P<v>[\w.\-]+):(?P<goal>[\w.\-]+)"
    r"(?: \((?P<id>[^)]*)\))? @ (?P<a>[\w.\-]+) ---")
_FAILED_GOAL_RE = re.compile(
    r"^Failed to execute goal (?:(?P<coords>[\w.\-]+:[\w.\-]+:[\w.\-]+:[\w.\-]+)(?: \((?P<id>[^)]*)\))? )?"
    r"on project (?P<proj>[\w.\-]+)\s*:?\s*(?P<msg>.*)$")
_JAVAC_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^\s\[\]][^\[\]]*?\.(?:java|groovy|scala|kt)):\[(?P<line>\d+)(?:,(?P<col>\d+))?\]"
    r"\s*(?P<msg>.*)$")
_KOTLIN_RE = re.compile(
    r"^e: (?:file://)?(?P<file>\S+?\.kts?)(?::\s*\((?P<line>\d+),\s*(?P<col>\d+)\):|:(?P<line2>\d+):(?P<col2>\d+):?)"
    r"\s*(?P<msg>.*)$")
_CONT_RE = re.compile(r"^\s+(?P<key>symbol|location|required|found|reason)\s*:\s*(?P<val>.*)$")
_COORDS = r"[\w.\-]+:[\w.\-]+(?::[\w.\-]+){1,3}"
_DEP_START_RE = re.compile(r"Could not resolve dependencies for project|Failed to collect dependencies at|"
                           r"Failed to resolve requested artifacts")
_PLUGIN_RES_RE = re.compile(r"Plugin (?P<coords>[\w.\-]+:[\w.\-]+:[\w.\-]+) or one of its dependencies could not be "
                            r"resolved|Error resolving version for plugin '(?P<ga>[\w.\-]+:[\w.\-]+)'|"
                            r"No plugin found for prefix '(?P<prefix>[\w.\-]+)'|Plugin not found for prefix")
_RULE_RE = re.compile(r"^Rule (?P<n>\d+): (?P<rule>\S+?) failed with message:?\s*(?P<rest>.*)$")
_POM_PROBLEMS_RE = re.compile(r"Some problems were encountered while processing the POMs")
_PROJECT_PROBLEMS_RE = re.compile(
    r"^\s*The project (?P<gav>\S*) \((?P<path>[^)]*)\) has \d+ errors?")
_MISSING_VERSION_RE = re.compile(r"'(?P<field>[\w.]+\.version)' for (?P<coords>[\w.\-]+:[\w.\-]+)(?::[\w.\-]+)* is missing")
_NON_PARSEABLE_RE = re.compile(r"Non-parseable POM (?P<path>.+?\.xml|[^:]+):\s*(?P<msg>.*)$")
_PARENT_RE = re.compile(r"Non-resolvable parent POM(?: for (?P<child>[\w.\-]+:[\w.\-]+:[\w.\-]+))?")
_IMPORT_RE = re.compile(r"Non-resolvable import POM")
_TEST_ENTRY_RE = re.compile(r"^\s{2,}(?P<entry>(?:Run \d+: )?\S.*)$")
_HELP_SUFFIX_RE = re.compile(r"\s*->\s*\[Help \d+\]\s*$")

_BOILERPLATE = (
    "-> [Help", "[Help ", "To see the full stack trace", "Re-run Maven using", "For more information about the errors",
    "After correcting the problems", "mvn <args> -rf", "mvn <goals> -rf", "The build could not read",
    "Please refer to dump files", "Please refer to ",
)

_COMPILER_PLUGINS = {"compiler", "kotlin", "groovy", "gmavenplus", "scala", "aspectj", "ajc"}
_AP_MARKERS = ("com.sun.tools.javac", "lombok", "ExceptionInInitializerError", "TypeTag", "IllegalAccessError",
               "NoSuchFieldError", "annotation processor", "AnnotationProcessor", "Processor")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def plugin_short_name(artifact_id: str) -> str:
    """maven-compiler-plugin → compiler, kotlin-maven-plugin → kotlin, compiler → compiler."""
    m = re.match(r"^maven-(.+)-plugin$", artifact_id)
    if m:
        return m.group(1)
    m = re.match(r"^(.+?)-maven-plugin$", artifact_id)
    if m:
        return m.group(1)
    return artifact_id


def parse_coords(coords: str) -> GAV:
    """Maven coordinates g:a[:type[:classifier]]:version → GAV."""
    parts = coords.strip().split(":")
    if len(parts) == 2:
        return GAV(group_id=parts[0], artifact_id=parts[1])
    if len(parts) == 3:
        return GAV(group_id=parts[0], artifact_id=parts[1], version=parts[2])
    if len(parts) == 4:
        return GAV(group_id=parts[0], artifact_id=parts[1], type=parts[2], version=parts[3])
    return GAV(group_id=parts[0], artifact_id=parts[1], type=parts[2], classifier=parts[3], version=parts[-1])


def _split_level(line: str) -> tuple[str | None, str]:
    line = _TS_RE.sub("", line.rstrip("\r\n"))
    m = _LEVEL_RE.match(line)
    if not m:
        return None, line
    level = m.group(1)
    body = line[m.end():]
    while True:                                          # "[ERROR] [ERROR] Some problems..."
        m2 = _LEVEL_RE.match(body)
        if not m2:
            break
        body = body[m2.end():]
    if level == "WARN":
        level = "WARNING"
    return level, body


def _is_boilerplate(body: str) -> bool:
    b = body.strip()
    return not b or any(b.startswith(p) for p in _BOILERPLATE)


# ---------------------------------------------------------------------------
# path handling
# ---------------------------------------------------------------------------
def _root_prefixes(project_root: Path | str | None) -> list[str]:
    if project_root is None or str(project_root) == "":
        return []
    raw = _clean_path(str(project_root)).rstrip("/")
    cands = {raw}
    try:
        cands.add(_clean_path(str(Path(project_root).resolve())).rstrip("/"))
    except OSError:
        pass
    for c in sorted(cands):                              # copy: the set grows inside the loop
        if c.startswith("/private/"):
            cands.add(c[len("/private"):])
        elif c.startswith(("/tmp/", "/var/")):
            cands.add("/private" + c)
    return sorted(cands, key=len, reverse=True)


_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:/|$)")


def _clean_path(path: str) -> str:
    """One path as Maven / javac / kotlinc print it → forward slashes, no ``file:`` scheme, and a Windows drive path
    without the URI-style leading slash. On Windows the in-process javac (plexus ``toUri().getPath()``) prints
    ``/C:/work/proj/src/…``; kotlinc and some plugins print ``file:///C:/…`` or ``file:/C:/…`` (percent-encoded).
    All become ``C:/work/proj/src/…`` — a leading ``/C:/`` made ``ws.abs`` build ``C:\\C:\\…`` (round 10)."""
    p = path.strip().replace("\\", "/")
    if p[:5].lower() == "file:":
        p = unquote(p[5:])
        if p.startswith("///") or (p.startswith("//") and _DRIVE_RE.match(p[2:])):
            p = p[2:]                                   # file:///C:/x, file:///work/x, file://C:/x
    if p.startswith("/") and _DRIVE_RE.match(p[1:]):
        p = p[1:]                                       # /C:/work/x → C:/work/x
    return p


def _is_drive_path(p: str) -> bool:
    return bool(_DRIVE_RE.match(p))


def _relativize(path: str, prefixes: list[str]) -> str:
    """``path`` relative to the first matching project-root prefix (POSIX separators), else the cleaned path.
    Windows prefixes (``C:/…``) compare case-insensitively: ``c:/work`` / ``C:/Work`` name the same directory."""
    p = _clean_path(path)
    for pre in prefixes:
        fold = _is_drive_path(pre)
        a, b = (p.lower(), pre.lower()) if fold else (p, pre)
        if a == b:
            return "."
        if a.startswith(b + "/"):
            return str(PurePosixPath(p[len(pre) + 1:]))
    return p


@lru_cache(maxsize=64)
def _scrub_regex(prefixes: tuple[str, ...]) -> re.Pattern[str] | None:
    """One alternation for every project-root prefix in every spelling a log uses: ``/work/proj/``,
    ``file:///work/proj/``, ``C:/work/proj/``, ``C:\\work\\proj\\``, ``/C:/work/proj/``, ``file:///c:/…`` (Windows
    prefixes case-insensitively)."""
    alts: list[str] = []
    for pre in prefixes:                                # longest first (`_root_prefixes` sorts them)
        if not pre:
            continue
        body = "".join(r"[/\\]" if ch == "/" else re.escape(ch) for ch in pre)
        if _is_drive_path(pre):
            alts.append(r"(?i:(?:file:/{0,3})?/?" + body + r")[/\\]")
        else:
            alts.append(r"(?:file:(?://)?)?" + body + "/")
    return re.compile("|".join(alts)) if alts else None


def _scrub(text: str, prefixes: list[str]) -> str:
    rx = _scrub_regex(tuple(prefixes))
    return rx.sub("", text) if rx is not None and text else text


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
@dataclass
class _Block:
    kind: str                         # failed_goal | dep | plugin_res | pom | project | rule | tests
    lines: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    module: str | None = None
    goal: str | None = None
    plugin: str | None = None      # short name (compiler, enforcer, surefire...)
    coords: str | None = None
    msg: str = ""
    pom_file: str | None = None


class _Parser:
    def __init__(self, project_root: Path | str | None):
        self.prefixes = _root_prefixes(project_root)
        self.root = Path(project_root) if project_root not in (None, "") else None
        self.errors: list[BuildError] = []
        self.module: str | None = None
        self.goal: str | None = None
        self._pending_artifact: str | None = None
        self._javac: dict[tuple, BuildError] = {}
        self._last_javac: BuildError | None = None
        self._block: _Block | None = None
        self._pom_errors: dict[str, BuildError] = {}
        self._dep_keys: set[tuple] = set()
        self._parent_keys: set[str] = set()
        self._plugin_keys: set[str] = set()
        self._enforcer_emitted = False
        self._test_entries: list[str] = []
        self._in_results = False
        self._results_section: str | None = None
        self.meaningful_errors: list[str] = []

    # ------------------------------------------------------------- helpers
    def _add(self, err: BuildError) -> BuildError:
        err.raw = _scrub(err.raw, self.prefixes)
        err.message = _scrub(err.message, self.prefixes).strip()
        fill_signatures(err)
        self.errors.append(err)
        return err

    def _rel(self, p: str) -> str:
        return _relativize(p, self.prefixes)

    # ------------------------------------------------------------- main loop
    def feed(self, lines: Iterable[str]) -> None:
        for original in lines:
            line = strip_ansi(original).rstrip("\r\n")
            level, body = _split_level(line)
            self._line(line, level, body)
        self._close_block()

    def _line(self, line: str, level: str | None, body: str) -> None:
        # 1) javac continuation lines (prefixed or not)
        if self._last_javac is not None and level in (None, "ERROR"):
            m = _CONT_RE.match(body)
            if m:
                key, val = m.group("key"), m.group("val").strip()
                if key == "symbol" and not self._last_javac.symbol:
                    self._last_javac.symbol = val
                elif key == "location" and not self._last_javac.location:
                    self._last_javac.location = val
                self._last_javac.raw += "\n" + line
                return
            if level is None and body.startswith((" ", "\t")) and body.strip():
                self._last_javac.raw += "\n" + line
                return
            if level == "ERROR" and body.startswith("    ") and not _is_boilerplate(body) \
                    and not _JAVAC_RE.match(body.strip()):
                self._last_javac.raw += "\n" + line
                return
        self._last_javac = None

        if level == "ERROR" or level == "FATAL":
            if not _is_boilerplate(body) and not body.startswith("COMPILATION ERROR"):
                self.meaningful_errors.append(body.strip())

        # 2) context lines
        if level == "INFO" or level is None and self._block is None:
            if level == "INFO":
                self._context(body)
                self._close_block()
                self._results(level, body)
            return
        if level == "WARNING":
            m = _RULE_RE.match(body.strip())
            if m and (self._block is None or self._block.kind != "failed_goal"):
                self._close_block()
                self._block = _Block(kind="rule", module=self.module, goal=self.goal)
                self._block.lines.append(body.strip())
                self._block.raw.append(line)
                return
            if self._block is not None and self._block.kind == "rule":
                self._block.lines.append(body.strip())
                self._block.raw.append(line)
                return
            return

        # 3) javac / kotlin diagnostics
        if level == "ERROR":
            stripped = body.strip()
            m = _JAVAC_RE.match(stripped)
            if m:
                self._javac_error(m, line)
                return
            m = _KOTLIN_RE.match(stripped)
            if m:
                self._kotlin_error(m, line)
                return
            if self._results(level, body):
                return

        # 4) block handling
        self._blocks(line, level, body)

    def _context(self, body: str) -> None:
        m = _HEADER_RE.match(body)
        if m:
            self._pending_artifact = m.group("a")
            self.module = m.group("a")
            self.goal = None
            return
        m = _BUILDING_RE.match(body)
        if m and not m.group("name").endswith(":"):
            self.module = self._pending_artifact or m.group("name")
            self._pending_artifact = None
            self.goal = None
            return
        m = _MOJO_RE.match(body)
        if m:
            self.module = m.group("a")
            self.goal = f"{plugin_short_name(m.group('p'))}:{m.group('goal')}"

    def _results(self, level: str | None, body: str) -> bool:
        """Track surefire's 'Results:' section; collect failing test entries."""
        b = body.strip()
        if level == "INFO" and b == "Results:":
            self._in_results = True
            self._results_section = None
            return True
        if not self._in_results:
            return False
        if level == "ERROR" and b in ("Failures:", "Errors:"):
            self._results_section = b
            return True
        if level == "ERROR" and self._results_section and _TEST_ENTRY_RE.match(body):
            entry = body.strip()
            if not entry.startswith("Run "):
                self._test_entries.append(entry)
            return True
        if b.startswith("Tests run:"):
            self._in_results = False
            self._results_section = None
            return level == "ERROR"
        return False

    # ------------------------------------------------------------- javac
    def _kind_for(self, file: str, goal: str | None) -> ErrorKind:
        # Windows javac paths come with backslashes (D:\work\x\src\test\java\…): normalise before the path test, or
        # a test-source error is filed as COMPILE and the test-compile fallback (L7) / `scope test` never apply
        path = "/" + (file or "").replace("\\", "/")
        if (goal or "").endswith("testCompile") or (goal or "").endswith("test-compile") \
                or "/src/test/" in path or "/src/it/" in path:
            return ErrorKind.TEST_COMPILE
        return ErrorKind.COMPILE

    def _goal_module(self) -> tuple[str | None, str | None]:
        if self._block is not None and self._block.kind == "failed_goal":
            return self._block.module, self._block.goal
        return self.module, self.goal

    def _record_javac(self, file: str, line_no: int, col: int | None, msg: str, raw: str) -> None:
        msg = re.sub(r"^error:\s*", "", msg.strip())
        rel = self._rel(file)
        module, goal = self._goal_module()
        key = (rel, line_no, col, msg)
        existing = self._javac.get(key)
        if existing is not None:
            self._last_javac = _Shadow(existing)           # merge continuations into the first occurrence
            return
        kind = self._kind_for(rel, goal)
        err = BuildError(kind=kind, message=msg, file=rel, line=line_no, column=col, module=module,
                         goal=goal or ("compiler:testCompile" if kind == ErrorKind.TEST_COMPILE else None), raw=raw)
        self.errors.append(err)                             # signatures filled at the end (continuations pending)
        self._javac[key] = err
        self._last_javac = err

    def _javac_error(self, m: re.Match[str], line: str) -> None:
        col = int(m.group("col")) if m.group("col") else None
        self._record_javac(m.group("file"), int(m.group("line")), col, m.group("msg"), line)

    def _kotlin_error(self, m: re.Match[str], line: str) -> None:
        ln = m.group("line") or m.group("line2")
        col = m.group("col") or m.group("col2")
        self._record_javac(m.group("file"), int(ln), int(col) if col else None, m.group("msg"), line)

    # ------------------------------------------------------------- blocks
    def _blocks(self, line: str, level: str | None, body: str) -> None:
        b = body.strip()
        if level in ("ERROR", "FATAL"):
            m = _FAILED_GOAL_RE.match(b)
            if m:
                self._close_block()
                coords = m.group("coords")
                plugin = goal = None
                if coords:
                    parts = coords.split(":")
                    plugin = plugin_short_name(parts[1])
                    goal = f"{plugin}:{parts[3]}"
                msg = _HELP_SUFFIX_RE.sub("", m.group("msg")).strip()
                self._block = _Block(kind="failed_goal", module=m.group("proj"), goal=goal, plugin=plugin,
                                     coords=coords, msg=msg, lines=[msg] if msg else [], raw=[line])
                if _HELP_SUFFIX_RE.search(b):
                    self._close_block()
                return
            if _POM_PROBLEMS_RE.search(b):
                self._close_block()
                self._block = _Block(kind="pom", raw=[line])
                return
            m = _PROJECT_PROBLEMS_RE.match(b)
            if m:
                self._close_block()
                gav = m.group("gav")
                module = gav.split(":")[1] if gav.count(":") >= 2 else None
                self._block = _Block(kind="project", module=module, pom_file=self._rel(m.group("path")), raw=[line])
                return
            if self._block is None or self._block.kind not in ("failed_goal",):
                if _PLUGIN_RES_RE.search(b) and not (self._block and self._block.kind == "pom"):
                    self._close_block()
                    self._block = _Block(kind="plugin_res", module=self.module, goal=self.goal, lines=[b], raw=[line])
                    if _HELP_SUFFIX_RE.search(b):
                        self._close_block()
                    return
                if _DEP_START_RE.search(b) and not (self._block and self._block.kind in ("pom", "project")):
                    self._close_block()
                    self._block = _Block(kind="dep", module=self.module, lines=[b], raw=[line])
                    if _HELP_SUFFIX_RE.search(b):
                        self._close_block()
                    return
                m = _RULE_RE.match(b)
                if m and self._block is None:
                    self._block = _Block(kind="rule", module=self.module, goal=self.goal, lines=[b], raw=[line])
                    return
            if self._block is None:
                if level == "FATAL" or b.startswith(("Non-resolvable parent POM", "Non-parseable POM")):
                    self._pom_line(b, line, module=self.module, pom_file=None)
                return
            if _is_boilerplate(body):
                if self._block.kind == "project" and not b and self._block.lines or self._block.kind not in ("pom", "project") or b.startswith(("-> [Help", "To see the full", "The build could not read")):
                    self._close_block()
                return
            self._block.lines.append(_HELP_SUFFIX_RE.sub("", b))
            self._block.raw.append(line)
            if _HELP_SUFFIX_RE.search(b) and self._block.kind not in ("pom", "project"):
                self._close_block()
            return
        # level None: unprefixed continuation (legacy enforcer messages, " @ " markers, stack traces)
        if self._block is not None and b and b != "@":
            if self._block.kind in ("rule", "failed_goal", "dep", "plugin_res"):
                if b.startswith("at ") or b.startswith("Caused by"):
                    return
                self._block.lines.append(b)
                self._block.raw.append(line)

    def _close_block(self) -> None:
        blk, self._block = self._block, None
        if blk is None:
            return
        handler = getattr(self, f"_finish_{blk.kind}")
        handler(blk)

    # ------------------------------------------------------------- finishers
    def _finish_pom(self, blk: _Block) -> None:
        for b, raw in zip(blk.lines, blk.raw[1:] or blk.raw):
            self._pom_line(b, raw, module=None, pom_file=None)

    def _finish_project(self, blk: _Block) -> None:
        for b, raw in zip(blk.lines, blk.raw[1:]):
            self._pom_line(b, raw, module=blk.module, pom_file=blk.pom_file)

    def _pom_line(self, b: str, raw: str, module: str | None, pom_file: str | None) -> None:
        msg = _HELP_SUFFIX_RE.sub("", b).strip()
        if not msg or msg == "@" or _is_boilerplate(msg):
            return
        key = _scrub(msg, self.prefixes)
        existing = self._pom_errors.get(key)
        if existing is not None:
            if module and not existing.module:
                existing.module = module
                fill_signatures(existing)
            if pom_file and not existing.file:
                existing.file = pom_file
            return
        if _PARENT_RE.search(msg):
            arts = _artifacts_in(msg)
            parent = arts[0] if arts else None
            child = _PARENT_RE.search(msg).group("child")
            if parent is not None:
                parent = GAV(group_id=parent.group_id, artifact_id=parent.artifact_id, version=parent.version)
            k = parent.coordinates if parent else msg
            if k in self._parent_keys:
                return
            self._parent_keys.add(k)
            err = self._add(BuildError(kind=ErrorKind.PARENT_RESOLUTION, message=msg, artifact=parent,
                                       module=module or (child.split(":")[1] if child else None),
                                       file=pom_file, raw=raw))
            self._pom_errors[key] = err
            return
        if _IMPORT_RE.search(msg):
            arts = _artifacts_in(msg)
            err = self._add(BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message=msg,
                                       artifact=arts[0] if arts else None, module=module, file=pom_file, raw=raw))
            self._pom_errors[key] = err
            return
        artifact = None
        file = pom_file
        m = _MISSING_VERSION_RE.search(msg)
        if m:
            artifact = parse_coords(m.group("coords"))
        m2 = _NON_PARSEABLE_RE.search(msg)
        if m2 and m2.group("path").endswith(".xml"):
            file = self._rel(m2.group("path"))
        err = self._add(BuildError(kind=ErrorKind.POM_INVALID, message=msg, artifact=artifact, module=module,
                                   file=file, raw=raw))
        self._pom_errors[key] = err

    def _finish_dep(self, blk: _Block) -> None:
        text = "\n".join(blk.lines)
        module = blk.module
        m = re.search(r"Could not resolve dependencies for project (?P<p>" + _COORDS + ")", text)
        if m and not module:
            module = parse_coords(m.group("p")).artifact_id
        found: dict[str, tuple[GAV, str, int]] = {}

        def put(gav: GAV, detail: str, prio: int) -> None:
            k = f"{gav.group_id}:{gav.artifact_id}:{gav.version or ''}"
            if k not in found or prio > found[k][2]:
                found[k] = (gav, detail.strip(), prio)

        for ln in blk.lines:
            for mm in re.finditer(r"Could not (?:find|transfer) artifact (" + _COORDS + r")(?: (?:in|from/to) \S+ \([^)\s]*\))?"
                                  r"(?:: (?!Could not)[^\n]+?(?= -> |$))?", ln):
                put(parse_coords(mm.group(1)), mm.group(0), 3)
            for mm in re.finditer(r"Failure to find (" + _COORDS + r") in \S+", ln):
                put(parse_coords(mm.group(1)), mm.group(0), 3)
            mm = re.search(r"The following artifacts could not be resolved: (.*?)(?:: Could not|: Failure|$)", ln)
            if mm:
                for item in re.findall(r"(" + _COORDS + r")(?: \(([^)]*)\))?", mm.group(1)):
                    put(parse_coords(item[0]), f"Could not resolve artifact {item[0]}" +
                        (f" ({item[1]})" if item[1] else ""), 1)
            mm = re.match(r"^dependency: (" + _COORDS + r")(?: \((\w+)\))?", ln.strip())
            if mm:
                put(parse_coords(mm.group(1)), f"Could not resolve dependency {mm.group(1)}", 1)
            mm = re.search(r"Failed to read artifact descriptor for (" + _COORDS + r")", ln)
            if mm:
                put(parse_coords(mm.group(1)), mm.group(0), 2)
            elif "Failed to collect dependencies at" in ln:
                chain = re.findall(_COORDS, ln.split("Failed to collect dependencies at", 1)[1])
                if chain:
                    put(parse_coords(chain[-1]), f"Failed to collect dependencies at {' -> '.join(chain)}", 2)
        if m:
            project_key = parse_coords(m.group("p"))
            found.pop(f"{project_key.group_id}:{project_key.artifact_id}:{project_key.version or ''}", None)
        raw = "\n".join(blk.raw)
        if not found:
            self._add(BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message=blk.lines[0] if blk.lines else text,
                                 module=module, raw=raw))
            return
        for gav, detail, _prio in found.values():
            gav = GAV(group_id=gav.group_id, artifact_id=gav.artifact_id, version=gav.version,
                      type=gav.type if gav.type not in (None, "jar") else None, classifier=gav.classifier)
            k = (module, gav.coordinates)
            if k in self._dep_keys:
                continue
            self._dep_keys.add(k)
            self._add(BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message=_HELP_SUFFIX_RE.sub("", detail).strip(),
                                 artifact=gav, module=module, raw=raw))

    def _finish_plugin_res(self, blk: _Block) -> None:
        text = "\n".join(blk.lines)
        m = _PLUGIN_RES_RE.search(text)
        artifact = None
        if m and m.group("coords"):
            artifact = parse_coords(m.group("coords"))
        elif m and m.group("ga"):
            artifact = parse_coords(m.group("ga"))
        key = artifact.coordinates if artifact else text
        if key in self._plugin_keys:
            return
        self._plugin_keys.add(key)
        message = blk.lines[0].rstrip(":") if blk.lines else text
        detail = next((ln for ln in blk.lines[1:] if ln.strip()), "")
        if detail:
            message = f"{message}: {detail.strip()}"
        self._add(BuildError(kind=ErrorKind.PLUGIN_RESOLUTION, message=message, artifact=artifact,
                             module=blk.module, goal=blk.goal, raw="\n".join(blk.raw)))

    def _finish_rule(self, blk: _Block) -> None:
        self._emit_rules(blk.lines, blk.raw, blk.module, blk.goal or "enforcer:enforce")

    def _emit_rules(self, lines: list[str], raw: list[str], module: str | None, goal: str | None) -> int:
        rules: list[tuple[str, list[str]]] = []
        for ln in lines:
            m = _RULE_RE.match(ln.strip())
            if m:
                rules.append((m.group("rule"), [m.group("rest")] if m.group("rest") else []))
            elif rules and ln.strip():
                if ln.strip().startswith(("Failed to execute goal", "Some Enforcer rules have failed")):
                    continue
                rules[-1][1].append(ln.strip())
        for rule, msg_lines in rules:
            simple = rule.rsplit(".", 1)[-1]
            msg = f"{simple}: " + " ".join(msg_lines) if msg_lines else f"{simple} failed"
            self._add(BuildError(kind=ErrorKind.ENFORCER, message=msg, module=module, goal=goal,
                                 raw="\n".join(raw)))
        if rules:
            self._enforcer_emitted = True
        return len(rules)

    def _finish_failed_goal(self, blk: _Block) -> None:
        text = "\n".join(blk.lines)
        raw = "\n".join(blk.raw)
        if _DEP_START_RE.search(text) or "The following artifacts could not be resolved" in text:
            self._finish_dep(_Block(kind="dep", lines=blk.lines, raw=blk.raw, module=blk.module))
            return
        if _PLUGIN_RES_RE.search(text):
            self._finish_plugin_res(_Block(kind="plugin_res", lines=[ln for ln in blk.lines if ln], raw=blk.raw,
                                           module=blk.module, goal=blk.goal))
            return
        message = blk.msg or next((ln for ln in blk.lines if ln.strip()), "") or "plugin execution failed"
        plugin = blk.plugin or ""
        if plugin in _COMPILER_PLUGINS:
            if True:
                if any(e.module == blk.module or blk.module is None for e in self._javac.values()) or \
                        (self._javac and "Compilation failure" in text):
                    return
                kind = ErrorKind.TEST_COMPILE if (blk.goal or "").endswith("testCompile") else ErrorKind.COMPILE
                if "Fatal error compiling" in text and any(mk in text for mk in _AP_MARKERS) \
                        and "release version" not in text and "invalid target release" not in text:
                    detail = text.split("Fatal error compiling:", 1)[-1].strip().splitlines()[0].strip()
                    msg = f"annotation-processor: {detail}"
                else:
                    msg = message.replace("Compilation failure:", "").strip() or message
                    if msg.startswith("Fatal error compiling:"):
                        msg = msg.split(":", 1)[1].strip() or msg
                self._add(BuildError(kind=kind, message=msg, module=blk.module, goal=blk.goal, raw=raw))
                return
        if plugin == "enforcer":
            if self._emit_rules(blk.lines, blk.raw, blk.module, blk.goal) or self._enforcer_emitted:
                return
            self._add(BuildError(kind=ErrorKind.ENFORCER, message=message, module=blk.module, goal=blk.goal, raw=raw))
            return
        if plugin in ("surefire", "failsafe") and re.search(r"test failures|There was a timeout|"
                                                            r"There are test failures|fork", text):
            entries = self._test_entries or []
            if not entries:
                self._add(BuildError(kind=ErrorKind.TEST_FAILURE, message=message, module=blk.module,
                                     goal=blk.goal, raw=raw))
            for entry in entries:
                sym = entry.split(" ", 1)[0].split(":", 1)[0]
                self._add(BuildError(kind=ErrorKind.TEST_FAILURE, message=entry, symbol=sym, module=blk.module,
                                     goal=blk.goal, raw=raw + "\n" + entry))
            self._test_entries = []
            return
        if not message.strip() and len(blk.lines) > 1:
            message = blk.lines[1]
        self._add(BuildError(kind=ErrorKind.PLUGIN_EXECUTION, message=message, module=blk.module, goal=blk.goal,
                             raw=raw))

    def finish(self, text: str) -> list[BuildError]:
        sources = _SourceLines(self.root)
        for err in self._javac.values():
            err.raw = _scrub(err.raw, self.prefixes)
            if not err.symbol:
                _symbol_from_source(err, sources)
            fill_signatures(err)
        failed = "BUILD FAILURE" in text or (bool(self.meaningful_errors) and "BUILD SUCCESS" not in text)
        if not self.errors and failed:
            first = next((e for e in self.meaningful_errors if not e.startswith("COMPILATION")), "BUILD FAILURE")
            tail = "\n".join(strip_ansi(text).splitlines()[-40:])
            self._add(BuildError(kind=ErrorKind.UNKNOWN, message=first, module=self.module, raw=tail))
        return self.errors


# ---------------------------------------------------------------------------
# symbols javac does not print (round 10: the L2 signature is the case-catalog key)
# ---------------------------------------------------------------------------
MAX_SOURCE_BYTES = 2_000_000
_OVERRIDE_RE = re.compile(r"@\s*(?:java\.lang\.)?Override\b")
_ANNOTATION_RE = re.compile(r"@\s*[\w$.]+\s*")
_DECL_NAME_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_STATIC_IMPORT_RE = re.compile(r"^\s*import\s+static\s+([\w$.]+?)\s*\.\s*(?:\*|[\w$]+)\s*;")
_JAVA_NOISE_RE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
_NOT_A_NAME = frozenset({"if", "for", "while", "switch", "catch", "synchronized", "return", "new", "throw", "super",
                         "this", "try", "do", "else", "case", "assert"})


class _SourceLines:
    """Lines of project sources, read once per parse (UTF-8, else MS949, else latin-1 — identifiers are ASCII)."""

    def __init__(self, root: Path | None):
        self.root = root
        self._cache: dict[str, list[str] | None] = {}

    def lines(self, file: str | None) -> list[str] | None:
        if not file:
            return None
        if file in self._cache:
            return self._cache[file]
        out: list[str] | None = None
        p = Path(file)
        if not p.is_absolute() and not _is_drive_path(file):
            p = self.root / file if self.root is not None else None
        try:
            if p is not None and p.is_file() and p.stat().st_size <= MAX_SOURCE_BYTES:
                data = p.read_bytes()
                for enc in ("utf-8", "cp949", "latin-1"):
                    try:
                        out = data.decode(enc).splitlines()
                        break
                    except UnicodeDecodeError:
                        continue
        except OSError:
            out = None
        self._cache[file] = out
        return out


def _overriding_method(lines: list[str], line_no: int, col: int | None) -> str:
    """Name of the method an ``@Override`` at (line, col) annotates: the annotations after it (with their arguments)
    are skipped, the first ``name(`` of the declaration header is the method."""
    if not (1 <= line_no <= len(lines)):
        return ""
    first = lines[line_no - 1]
    hits = [m.start() for m in _OVERRIDE_RE.finditer(first)]
    start = min(hits, key=lambda h: abs(h - ((col or 1) - 1))) if hits else 0
    text = _JAVA_NOISE_RE.sub(" ", "\n".join([first[start:], *lines[line_no:line_no + 20]]))
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text[i] != "@":
            break
        m = _ANNOTATION_RE.match(text, i)
        if not m:
            return ""
        i = m.end()
        if i < n and text[i] == "(":                    # @RequestMapping(value = "/x", method = GET)
            depth = 0
            while i < n:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
    header = re.split(r"[{;=]", text[i:], maxsplit=1)[0]
    m = _DECL_NAME_RE.search(header)
    return m.group(1) if m and m.group(1) not in _NOT_A_NAME else ""


def _symbol_from_source(err: BuildError, sources: _SourceLines) -> None:
    """``method does not override …`` and ``static import only from classes and interfaces`` never name what they are
    about; without it every such error in the fleet shared one L2 (= one catalog case). Fills ``symbol`` from the
    reported source line: ``method <name>`` of the annotated declaration, ``class <qualifier>`` of the static import.
    Nothing when the source cannot be read (the L2 then stays the bare class)."""
    cls = classify_javac(err.message)
    if cls not in ("method_does_not_override", "static_import_only") or not err.line:
        return
    lines = sources.lines(err.file)
    if not lines:
        return
    if cls == "method_does_not_override":
        name = _overriding_method(lines, err.line, err.column)
        if name:
            err.symbol = f"method {name}"
        return
    if 1 <= err.line <= len(lines):
        m = _STATIC_IMPORT_RE.match(lines[err.line - 1])
        if m:
            err.symbol = f"class {m.group(1)}"


class _Shadow:
    """Continuation sink for a duplicate javac error: fills missing symbol/location of the original only."""

    def __init__(self, target: BuildError):
        self._t = target

    @property
    def symbol(self) -> str | None:
        return self._t.symbol

    @symbol.setter
    def symbol(self, v: str) -> None:
        self._t.symbol = v

    @property
    def location(self) -> str | None:
        return self._t.location

    @location.setter
    def location(self, v: str) -> None:
        self._t.location = v

    @property
    def raw(self) -> str:
        return ""

    @raw.setter
    def raw(self, v: str) -> None:                      # the original already carries its raw block
        pass


def _artifacts_in(msg: str) -> list[GAV]:
    out: list[GAV] = []
    for rx in (r"Could not find artifact (" + _COORDS + ")", r"Failure to find (" + _COORDS + ")",
               r"Could not transfer artifact (" + _COORDS + ")",
               r"The following artifacts could not be resolved: (" + _COORDS + ")"):
        for m in re.finditer(rx, msg):
            g = parse_coords(m.group(1))
            if all(o.coordinates != g.coordinates for o in out):
                out.append(g)
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def parse_maven_log(text: str, project_root: Path | str | None = None) -> list[BuildError]:
    """Parse a full Maven log into structured errors (signatures filled). Never raises on odd input."""
    parser = _Parser(project_root)
    parser.feed(text.splitlines())
    return parser.finish(text)


_REACTOR_LINE_RE = re.compile(r"^(?P<name>\S.*?)\s+(?:\.+\s*)?(?P<status>SUCCESS|FAILURE|SKIPPED)\b")


def parse_reactor_summary(text: str) -> tuple[list[str], str | None]:
    """(modules_built, failed_module) — artifactIds where known (from the ``< g:a >`` headers), else names.

    Uses the Reactor Summary for multi-module builds; for single-module builds the ``Building <name>`` lines.
    ``modules_built`` lists modules that finished with SUCCESS.
    """
    names: dict[str, str] = {}
    building: list[str] = []
    pending: str | None = None
    summary: list[tuple[str, str]] = []
    in_summary = False
    failed_project: str | None = None
    for raw in text.splitlines():
        level, body = _split_level(strip_ansi(raw))
        b = body.strip()
        if level == "INFO":
            m = _HEADER_RE.match(b)
            if m:
                pending = m.group("a")
                continue
            m = _BUILDING_RE.match(b)
            if m and not m.group("name").endswith(":") and not in_summary:
                name = m.group("name").strip()
                art = pending or name
                names[name] = art
                building.append(art)
                pending = None
                continue
            if b.startswith("Reactor Summary"):
                in_summary = True
                continue
            if in_summary:
                if b.startswith(("BUILD ", "-----")):
                    if summary:
                        in_summary = False
                    continue
                m = _REACTOR_LINE_RE.match(b)
                if m:
                    summary.append((m.group("name").strip(), m.group("status")))
        elif level in ("ERROR", "FATAL") and failed_project is None:
            m = _FAILED_GOAL_RE.match(b)
            if m:
                failed_project = m.group("proj")
    if summary:
        built = [names.get(n, n) for n, s in summary if s == "SUCCESS"]
        failed = next((names.get(n, n) for n, s in summary if s == "FAILURE"), None)
        return built, failed
    ok = "BUILD SUCCESS" in text
    if ok:
        return building, None
    failed = failed_project or (building[-1] if building else None)
    return [b for b in building if b != failed], failed
