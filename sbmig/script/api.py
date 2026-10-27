"""The helper API `p` a fix script works through (runs inside the script process, `sbmig.script.harness`).

Every path is relative to the project folder; `\\` and `/` are both accepted and results always use `/`. The path
guard refuses absolute paths, drive letters, `..` leaving the folder, anything under `.git/`, writes into Maven
build output (`target/` beside a pom.xml, see `sbmig.core.workspace.is_build_output_path`) and any path whose real
location (symlinks) is outside the project. A refusal raises `PathRefused` and is reported even when the script
catches it — a refused path is a script bug the next attempt has to see.

Files are read in their detected encoding (`build.encoding_candidates`, the same detection as the workspace) with
`\\n` line ends; `write` restores the original encoding, line-end style and UTF-8 BOM, so a diff shows only what the
script meant to change. Like the static check, this guards against mistakes, not against a hostile author.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Optional

from sbmig.core.workspace import (
    DEFAULT_ENCODING_CANDIDATES,
    _java_unicode_escape,
    detect_encoding,
    is_build_output_path,
)

from .check import ALLOWED_IMPORTS, REFUSED_MODULES, REFUSED_NAMES

MAX_LOG_LINE_CHARS = 1000                 # one p.log line; longer lines are cut
_SKIP_DIRS = (".git", "node_modules")     # never listed (Maven build output is pruned by its own rule)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ESCAPED_SUFFIXES = (".java", ".properties")   # characters the legacy encoding lacks → \uXXXX (valid in both)


class PathRefused(Exception):
    """A helper call named a path outside what a fix script may touch."""


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """`**/` = zero or more folders (so `**/*.java` matches `A.java` at the root too), `**` at the end = everything
    below, `*` / `?` / `[…]` stay inside one path segment. Case-sensitive, matches hidden names."""
    segments = pattern.split("/")
    out: list[str] = []
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "**":
            out.append(".*" if last else "(?:.*/)?")
            continue
        out.append(_segment_regex(seg))
        if not last:
            out.append("/")
    return re.compile("".join(out))


def _segment_regex(seg: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            while i + 1 < len(seg) and seg[i + 1] == "*":
                i += 1
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = seg.find("]", i + 2 if seg[i + 1:i + 2] in ("!", "^") else i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = seg[i + 1:j]
                neg = body[:1] in ("!", "^")
                body = body[1:] if neg else body
                out.append("[" + ("^/" if neg else "") + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _inside(path: Path, root: Path) -> bool:
    """`path` (resolved) at or below `root` (resolved); case-insensitive where the OS is."""
    a, b = os.path.normcase(str(path)), os.path.normcase(str(root))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:                    # different drives (Windows)
        return False


def _newline_of(raw: bytes) -> str:
    """Line-end style of a file: CRLF when most line ends are CRLF (a mixed file keeps its majority)."""
    crlf = raw.count(b"\r\n")
    return "\r\n" if crlf and crlf * 2 >= raw.count(b"\n") else "\n"


class FixHelper:
    """`p` — what a fix script may do with the project folder. Public attributes / methods only (the static check
    refuses `_` names, so the script cannot reach `_root` or the pom editor behind `p.pom()`)."""

    def __init__(self, root: str | Path, *, errors: Iterable[dict[str, Any]] = (),
                 encoding_candidates: Optional[Iterable[str]] = None, log_lines: int = 200,
                 journal_path: Optional[str | Path] = None):
        self._root = Path(root).resolve()
        self._cands = tuple(encoding_candidates or ()) or DEFAULT_ENCODING_CANDIDATES
        self._log_cap = max(1, int(log_lines))
        self._log: list[str] = []
        self._dropped = 0
        self._changed: dict[str, None] = {}          # ordered set of relative paths
        self._refusals: list[str] = []
        self._listing: Optional[list[str]] = None    # cached file list; dropped when files appear / disappear
        self._encodings: dict[str, str] = {}         # rel → encoding it was read / written with
        # closed by _close(); flushed per line so a killed process still leaves its account behind
        self._journal = open(journal_path, "a", encoding="utf-8") if journal_path else None   # noqa: SIM115
        self.errors: list[dict[str, Any]] = [dict(e) for e in errors]

    # ------------------------------------------------------------------ bookkeeping (harness side)
    def _close(self) -> None:
        if self._journal is not None:
            self._journal.close()
            self._journal = None

    def _event(self, **kv: Any) -> None:
        """Journal line flushed at once: what a killed (timed out) process still reports."""
        if self._journal is not None:
            self._journal.write(json.dumps(kv, ensure_ascii=False) + "\n")
            self._journal.flush()

    def _mark(self, rel: str) -> None:
        if rel not in self._changed:
            self._changed[rel] = None
            self._event(changed=rel)

    def _refuse(self, path: Any, why: str) -> PathRefused:
        msg = f"path {path!r} refused: {why}"
        self._refusals.append(msg)
        return PathRefused(msg)

    def _path(self, path: Any, *, write: bool = False) -> tuple[str, Path]:
        """Guarded (relative posix path, absolute path)."""
        if not isinstance(path, str):
            raise TypeError(f"path must be a str, got {type(path).__name__}")
        s = path.strip().replace("\\", "/")
        if not s:
            raise self._refuse(path, "empty path")
        if "\0" in s:
            raise self._refuse(path, "NUL character")
        if s.startswith("/") or _DRIVE_RE.match(s) or ":" in s:
            raise self._refuse(path, "absolute paths and drive letters are not allowed; "
                                     "use a path relative to the project folder")
        rel = posixpath.normpath(s)
        if rel == ".":
            raise self._refuse(path, "the project folder itself")
        if rel == ".." or rel.startswith("../"):
            raise self._refuse(path, "it leaves the project folder")
        self._check_rel(path, rel, write)
        full = self._root.joinpath(*rel.split("/"))
        real = full.resolve()
        if not _inside(real, self._root):
            raise self._refuse(path, "it resolves outside the project folder (symlink)")
        real_rel = Path(os.path.relpath(real, self._root)).as_posix()
        if real_rel != rel:
            if real_rel == ".":
                raise self._refuse(path, "it resolves to the project folder itself")
            self._check_rel(path, real_rel, write)
        return rel, full

    def _check_rel(self, path: Any, rel: str, write: bool) -> None:
        if any(part.lower() == ".git" for part in rel.split("/")):
            raise self._refuse(path, "files under .git/ are off limits")
        if write and is_build_output_path(rel, self._root):
            raise self._refuse(path, "it is Maven build output (target/); change the sources instead")

    def _file(self, path: Any) -> tuple[str, Path]:
        rel, full = self._path(path)
        if full.is_dir():
            raise IsADirectoryError(f"{rel!r} is a folder")
        if not full.is_file():
            raise FileNotFoundError(f"no such file: {rel!r}")
        return rel, full

    def _encoding_for(self, rel: str, full: Path) -> str:
        enc = detect_encoding(full, self._cands)
        seen = self._encodings.get(rel)
        if enc == "utf-8" and seen not in (None, "utf-8", "utf-8-sig"):
            enc = seen                    # ASCII-only now, but read as cp949 before: keep cp949
        return enc

    def _walk(self, base: Path, rel_dir: str) -> Iterator[str]:
        """Project-relative paths of the files below `base` (`rel_dir` = base relative to the project root)."""
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            try:
                if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                    continue              # symlinks / junctions are not listed (they may point outside)
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                if e.is_dir():
                    if e.name in _SKIP_DIRS or (e.name == "target" and is_build_output_path(rel, self._root)):
                        continue
                    yield from self._walk(Path(e.path), rel)
                elif e.is_file():
                    yield rel
            except OSError:
                continue

    def _all_files(self) -> list[str]:
        if self._listing is None:
            self._listing = sorted(self._walk(self._root, ""))
        return self._listing

    # ------------------------------------------------------------------ the API
    def files(self, glob: str = "**/*") -> list[str]:
        """Sorted relative paths (`/`) of the project files matching `glob`; `.git/`, `node_modules/` and Maven
        build output are never listed."""
        if not isinstance(glob, str):
            raise TypeError(f"glob must be a str, got {type(glob).__name__}")
        pat = glob.strip().replace("\\", "/")
        while pat.startswith("./"):
            pat = pat[2:]
        if pat.startswith(("/", "../")) or _DRIVE_RE.match(pat) or pat == ".." or "/../" in pat:
            raise self._refuse(glob, "glob patterns are relative to the project folder")
        if not pat or pat == ".":
            pat = "**/*"
        if pat.endswith("/"):
            pat += "**"
        rx = _glob_regex(pat)
        return [f for f in self._all_files() if rx.fullmatch(f)]

    def exists(self, path: str) -> bool:
        return self._path(path)[1].exists()

    def read(self, path: str) -> str:
        """Text of a file in its detected encoding; line ends are `\\n` (CRLF is restored on write)."""
        rel, full = self._file(path)
        enc = self._encoding_for(rel, full)
        self._encodings[rel] = enc
        text = full.read_bytes().decode(enc, errors="replace")
        return text.replace("\r\n", "\n")

    def write(self, path: str, text: str) -> bool:
        """Replace (or create) a file's text. An existing file keeps its encoding, CRLF / LF style and UTF-8 BOM;
        a new file is UTF-8 with `\\n`. True when the bytes on disk changed."""
        if not isinstance(text, str):
            raise TypeError(f"text must be a str, got {type(text).__name__}")
        rel, full = self._path(path, write=True)
        if full.is_dir():
            raise IsADirectoryError(f"{rel!r} is a folder")
        text = text.replace("\r\n", "\n")
        old: Optional[bytes] = None
        if full.is_file():
            old = full.read_bytes()
            enc = self._encoding_for(rel, full)
            if _newline_of(old) == "\r\n":
                text = text.replace("\n", "\r\n")
        else:
            enc = "utf-8"
        data = self._encode(rel, text, enc)
        if old is not None and data == old:
            return False
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        if old is None:
            self._listing = None
        self._mark(rel)
        return True

    def _encode(self, rel: str, text: str, enc: str) -> bytes:
        try:
            data = text.encode(enc)
        except UnicodeEncodeError:
            if rel.endswith(_ESCAPED_SUFFIXES):
                data = _java_unicode_escape(text, enc).encode(enc)
            else:
                self.log(f"{rel}: text not representable in {enc}, written as UTF-8")
                enc = "utf-8"
                data = text.encode(enc)
        except LookupError:
            enc = "utf-8"
            data = text.encode(enc)
        self._encodings[rel] = enc
        return data

    def replace(self, path: str, old: str, new: str, count: int = -1) -> int:
        """Plain-text replacement in one file (`\\n` line ends, as `read` returns them). Number of replacements."""
        if not isinstance(old, str) or not isinstance(new, str):
            raise TypeError("old and new must be str")
        if not old:
            raise ValueError("old must not be empty")
        text = self.read(path)
        n = text.count(old) if count is None or count < 0 else min(text.count(old), count)
        if n:
            self.write(path, text.replace(old, new, -1 if count is None else count))
        return n

    def sub(self, path: str, pattern: Any, repl: Any, count: int = 0, flags: int = 0) -> int:
        """`re.subn(pattern, repl, text, count, flags)` on one file. Number of replacements."""
        text = self.read(path)
        if isinstance(pattern, re.Pattern):
            new, n = pattern.subn(repl, text, count=count)
        else:
            new, n = re.subn(pattern, repl, text, count=count, flags=flags)
        if n and new != text:
            self.write(path, new)
        return n

    def create(self, path: str, text: str = "") -> bool:
        """A new file (UTF-8, `\\n`); an existing one is refused (FileExistsError)."""
        rel, full = self._path(path, write=True)
        if full.exists():
            raise FileExistsError(f"{rel!r} already exists (use p.write to change it)")
        self.write(rel, text)
        return True

    def delete(self, path: str) -> bool:
        """Delete a file or a folder (with everything in it). False when nothing was there."""
        rel, full = self._path(path, write=True)
        if full.is_dir() and not full.is_symlink():
            gone = sorted(self._walk(full, rel))
            shutil.rmtree(full)
            for f in gone:
                self._mark(f)
        elif full.exists() or full.is_symlink():
            full.unlink()
            self._mark(rel)
        else:
            return False
        self._listing = None
        return True

    def move(self, src: str, dst: str) -> bool:
        """Move / rename a file or folder; `dst` must not exist (its parent folders are created)."""
        s_rel, s_full = self._path(src, write=True)
        d_rel, d_full = self._path(dst, write=True)
        if not s_full.exists():
            raise FileNotFoundError(f"no such file or folder: {s_rel!r}")
        if d_full.exists():
            raise FileExistsError(f"{d_rel!r} already exists")
        if s_full.is_dir():
            if d_rel == s_rel or d_rel.startswith(s_rel + "/"):
                raise ValueError(f"cannot move {s_rel!r} into itself")
            pairs = [(f, d_rel + f[len(s_rel):]) for f in sorted(self._walk(s_full, s_rel))]
        else:
            pairs = [(s_rel, d_rel)]
        d_full.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s_full), str(d_full))
        for a, b in pairs:
            self._mark(a)
            self._mark(b)
            if a in self._encodings:
                self._encodings[b] = self._encodings.pop(a)
        self._listing = None
        return True

    def pom(self, path: str = "pom.xml") -> PomHelper:
        """Editor for one pom.xml that keeps its formatting and comments; nothing is written before `save()`."""
        rel, full = self._file(path)
        return PomHelper(self, rel, full)

    def log(self, msg: Any) -> None:
        """One log line per text line (kept with the attempt in the KB)."""
        for line in str(msg).splitlines() or [""]:
            if len(line) > MAX_LOG_LINE_CHARS:
                line = line[:MAX_LOG_LINE_CHARS] + " …"
            if len(self._log) < self._log_cap:
                self._log.append(line)
                self._event(log=line)
            else:
                self._dropped += 1

    def _print(self, *args: Any, sep: Optional[str] = " ", end: Optional[str] = "\n", file: Any = None,
               flush: bool = False) -> None:
        """`print` inside the script: goes to the log (whatever `file` says)."""
        self.log((" " if sep is None else str(sep)).join(str(a) for a in args))


class PomHelper:
    """`p.pom(path)` — sbmig.restructure.pom_editor.PomEditor behind a small, path-guarded surface. Mutators return
    True when the pom changed; `save()` writes it (once per change) and counts it as a changed file."""

    def __init__(self, helper: FixHelper, rel: str, full: Path):
        from sbmig.restructure.pom_editor import PomEditor  # lazily: lxml + models only when a pom is edited
        self._helper = helper
        self._rel = rel
        self._full = full
        self._ed = PomEditor(full)

    @staticmethod
    def _gav(g: str, a: str, version: Optional[str] = None, scope: Optional[str] = None,
             type: Optional[str] = None, classifier: Optional[str] = None) -> Any:
        from sbmig.core.models import GAV
        return GAV(group_id=str(g), artifact_id=str(a), version=version, scope=scope, type=type,
                   classifier=classifier)

    def add_dependency(self, g: str, a: str, version: Optional[str] = None, scope: Optional[str] = None,
                       type: Optional[str] = None, classifier: Optional[str] = None) -> bool:
        """Declare g:a in <dependencies>; an existing declaration gets the given fields."""
        return self._ed.add_dependency(self._gav(g, a, version, scope, type, classifier), if_absent=False)

    def add_managed_dependency(self, g: str, a: str, version: Optional[str] = None, scope: Optional[str] = None,
                               type: Optional[str] = None, classifier: Optional[str] = None) -> bool:
        """Declare g:a in <dependencyManagement> (a BOM: type="pom", scope="import")."""
        return self._ed.add_managed_dependency(self._gav(g, a, version, scope, type, classifier), if_absent=False)

    def remove_dependency(self, g: str, a: str) -> bool:
        return self._ed.remove_dependency(g, a)

    def remove_managed_dependency(self, g: str, a: str) -> bool:
        return self._ed.remove_managed_dependency(g, a)

    def set_dependency_version(self, g: str, a: str, version: Optional[str]) -> bool:
        """Direct and managed declarations (through a local ${property} when the version uses one);
        None drops <version> (the parent manages it)."""
        return self._ed.set_dependency_version(g, a, version)

    def add_exclusion(self, g: str, a: str, ex_g: str, ex_a: str) -> bool:
        """<exclusion> ex_g:ex_a on every declaration of g:a (direct and managed). False when g:a is not declared
        here or the exclusion is already there."""
        from lxml import etree
        ed = self._ed
        before = ed.to_string()
        for managed in (False, True):
            for dep in ed.dependency_elements(managed):
                if ed.text_of(dep, "groupId") != g or ed.text_of(dep, "artifactId") != a:
                    continue
                excl = ed.child(dep, "exclusions")
                if any(ed.text_of(x, "groupId") == ex_g and ed.text_of(x, "artifactId") == ex_a
                       for x in ed.children(excl, "exclusion")):
                    continue
                if excl is None:
                    excl = ed.copy_into(dep, etree.Element("exclusions"), ordered=True)
                item = etree.Element("exclusion")
                etree.SubElement(item, "groupId").text = ex_g
                etree.SubElement(item, "artifactId").text = ex_a
                ed.copy_into(excl, item)
        return ed.to_string() != before

    def set_property(self, key: str, value: str) -> bool:
        return self._ed.set_property(key, str(value))

    def remove_property(self, key: str) -> bool:
        return self._ed.remove_property(key)

    def set_plugin_version(self, g: str, a: str, version: str) -> bool:
        """Every declaration of plugin g:a (build and pluginManagement); g "" = org.apache.maven.plugins."""
        return self._ed.set_plugin_version(g, a, version)

    def has_dependency(self, g: str, a: str) -> bool:
        return self._ed.has_dependency(g, a)

    def dependencies(self, managed: bool = False) -> list[dict[str, Any]]:
        """Declared dependencies as dicts (group_id, artifact_id, version, scope, type, classifier, optional)."""
        gavs = self._ed.managed_dependencies() if managed else self._ed.dependencies()
        return [g.model_dump() for g in gavs]

    def get_property(self, key: str) -> Optional[str]:
        return self._ed.get_property(key)

    def text(self) -> str:
        """The pom as it would be saved (`\\n` line ends)."""
        return self._ed.to_string().replace("\r\n", "\n")

    def save(self) -> bool:
        """Write the pom (its encoding, BOM and line ends kept). False when nothing changed."""
        self._helper._path(self._rel, write=True)          # build output / .git / symlink rules for writes
        if not self._ed.changed:
            return False
        self._ed.save()
        self._helper._mark(self._rel)
        return True


# (API, what it does) — the contract table of docs/INTERFACES.md "Script contract (S)", worded for the LLM
_API_ROWS: tuple[tuple[str, str], ...] = (
    ("`p.errors`",
     ("this case's build errors: list of dicts with `file`, `line`, `column`, `message`, `symbol`, `location`, "
      "`kind`, `module`, `signature`, `signature_generic`, `artifact`")),
    ('`p.files(glob="**/*")`',
     ("sorted relative paths of the files matching `glob` (`**/` = any folder depth incl. the root, `*` stays "
      "inside one folder); `.git/`, `target/`, `node_modules/` are never listed")),
    ("`p.read(path)` / `p.exists(path)`",
     "file text (encoding detected, line ends `\\n`) / does the file or folder exist"),
    ("`p.write(path, text)`",
     "replace a file's text; keeps its encoding, CRLF / LF and BOM (a new file: UTF-8, `\\n`); True when it changed"),
    ("`p.replace(path, old, new, count=-1)` / `p.sub(path, pattern, repl, count=0, flags=0)`",
     "plain / `re` replacement in one file; returns the number of replacements"),
    ("`p.create(path, text)` / `p.delete(path)` / `p.move(src, dst)`",
     ("new file (an existing one is refused) / delete a file or folder / move a file or folder (`dst` must not "
      "exist)")),
    ('`p.pom(path="pom.xml")`',
     ("pom editor keeping formatting and comments: `add_dependency(g, a, version=None, scope=None, type=None, "
      "classifier=None)`, `remove_dependency(g, a)`, `set_dependency_version(g, a, version)` (None = drop "
      "`<version>`), `add_exclusion(g, a, ex_g, ex_a)`, `add_managed_dependency(g, a, version=None, scope=None, "
      "type=None, classifier=None)`, `remove_managed_dependency(g, a)`, `set_property(k, v)`, "
      "`remove_property(k)`, `set_plugin_version(g, a, v)`, `has_dependency(g, a)`, `dependencies(managed=False)`, "
      "`get_property(k)`, `text()`, `save()`; mutators return True when the pom changed, nothing is written before "
      "`save()`")),
    ("`p.log(msg)`", "a log line kept with the attempt; `print(...)` logs too"),
)


def helper_api_doc(extra_imports: Iterable[str] = (), timeout_s: Optional[int] = None) -> str:
    """The helper API and the script rules as a markdown block for the fixer prompt (`script.extra_allowed_imports`
    and `script.timeout_seconds` filled in when given)."""
    allowed = ", ".join(f"`{m}`" for m in (*ALLOWED_IMPORTS, *[e for e in extra_imports if e]))
    names = ", ".join(f"`{n}`" for n in REFUSED_NAMES)
    modules = ", ".join(f"`{m}`" for m in REFUSED_MODULES)
    limit = f"a limit of {timeout_s} s" if timeout_s else "a time limit"
    rows = "\n".join(f"| {api} | {what} |" for api, what in _API_ROWS)
    return (
        "The script defines `def fix(p)` and changes files only through `p` (paths relative to the project folder, "
        "`/` separators):\n\n"
        f"| API | what it does |\n|---|---|\n{rows}\n\n"
        f"Rules: imports only {allowed}. Not available: the builtins {names} (`re.compile` is fine), the modules "
        f"{modules}, attributes starting with `_`. Absolute paths, drive letters, `..` leaving the project, `.git/` "
        "and writes under `target/` raise `PathRefused` and fail the run. The script runs in a separate process "
        f"with {limit}; an exception fails the run and its changes are rolled back.")


HELPER_API_DOC = helper_api_doc()
