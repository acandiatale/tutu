"""Workspace: the migrated project copy + run output directory + git snapshots + journal.

Layout of one run (v2.1, docs/v2/PLAN-v2.1.md §3.5 — nothing else is kept):
    <out_dir>/<run_id>/
        project/            ← working copy (git repo; one commit per step/attempt)
        logs/               ← every Maven / OpenRewrite / deptree / classpath / effective-pom log (NNN-<label>.log)
                              and their work files: rewrite-N.yml, rewrite-artifact-versions.json,
                              deptree-<module>.txt, classpath-<module>.txt (+ .meta.json caches),
                              effective-depmgmt*.json
        errors.jsonl        ← the run's error list (one ErrorEntry per line), errors.md its human view
        journal.jsonl       ← append-only event log (every step, build, decision, attempt; the web stream)
        sbmig.log           ← DEBUG log, `[stage]` per line
        profile.json, plan.json, report.json, report.md, changes.patch, result.zip (the migrated project)
    (+ merge-conflicts/ when a multi-module merge dropped differing copies, web-status.json for web runs)
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Iterable, Optional

from pydantic import BaseModel

log = logging.getLogger("sbmig.core.workspace")

GIT_TIMEOUT_S = 600.0      # one workspace git command (add / commit / status / reset)
# per-call git config (round 10, Windows): fleet run dirs exceed 260 chars; a global `core.autocrlf=true` rewrote
# `.sh` files with CRLF on `revert_to`; `core.quotepath` printed Korean names as "\355\225\234…" in name lists; a
# run dir owned by another account ("dubious ownership") made every git call fail
GIT_CONFIG: tuple[str, ...] = ("commit.gpgsign=false", "tag.gpgsign=false", "core.longpaths=true",
                               "core.autocrlf=false", "core.quotepath=false", "safe.directory=*")
# .git/info/attributes (beats every .gitattributes of the project): no EOL / filter / encoding conversion, ever —
# a project's `* text=auto` / `eol=crlf` / LFS `filter=lfs` would otherwise rewrite files on `git reset --hard`
GIT_INFO_ATTRIBUTES = "* -text -eol -filter -ident -working-tree-encoding\n"
# build-generated files outside target/ (B8): ignored unless the as-is tree already has them (then they stay tracked)
GENERATED_IGNORES: tuple[str, ...] = (".flattened-pom.xml", "**/src/main/generated/", "**/src/test/generated/",
                                      ".apt_generated/", ".apt_generated_tests/")
_CREATE_NO_WINDOW = 0x08000000


def _on_windows() -> bool:
    return os.name == "nt"


def _child_creationflags() -> int:
    """Windows: children get their own (hidden) console, so the operator's Ctrl+Break (thread dump, `sbmig.core.diag`)
    and Ctrl+C never reach git / cmd.exe / java directly — sbmig decides what to kill."""
    return getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW) if _on_windows() else 0


def _as_text(x) -> str:
    """subprocess output → str: bytes are UTF-8 with replacement characters (never the console code page),
    None (a reader thread that died) is empty."""
    if x is None:
        return ""
    return x.decode("utf-8", errors="replace") if isinstance(x, (bytes, bytearray)) else str(x)


def _as_bytes(x) -> bytes:
    if x is None:
        return b""
    return x.encode("utf-8") if isinstance(x, str) else bytes(x)
DEFAULT_ENCODING_CANDIDATES: tuple[str, ...] = ("utf-8", "cp949", "euc-kr", "shift_jis", "latin-1")
_UTF8_BOM = b"\xef\xbb\xbf"
_ZIP_UTF8_FLAG = 0x800

ENCODING_CACHE_MAX = 20_000     # entries (~8 MB); the fleet touches ~1.5 M files, an unbounded dict grew ~40 MB/100k


class _LRU:
    """A small thread-safe LRU map (OrderedDict); the encoding cache is shared by every workspace of a fleet."""

    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self._d: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def __len__(self) -> int:
        return len(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


# (resolved path) -> (mtime_ns, size, candidates, encoding)
_ENCODING_CACHE = _LRU(ENCODING_CACHE_MAX)


class GitError(RuntimeError):
    pass


def _norm_encoding(name: str) -> str:
    import codecs
    try:
        return codecs.lookup(name).name
    except LookupError:
        return name.lower()


def _sniff_bytes(data: bytes, candidates: tuple[str, ...]) -> str:
    if data.startswith(_UTF8_BOM):
        return "utf-8-sig"
    for enc in candidates:
        try:
            data.decode(enc)                 # strict
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    return "utf-8"                           # nothing decodes strictly → utf-8 (read with errors="replace")


def detect_encoding(path: str | Path, candidates: Optional[list[str] | tuple[str, ...]] = None) -> str:
    """Encoding of a text file: UTF-8 BOM → "utf-8-sig"; else the first candidate that decodes the whole file
    strictly (default utf-8 → cp949 → euc-kr → shift_jis → latin-1). Cached per path + mtime + size.
    Pure ASCII / empty / missing files → "utf-8"."""
    cands = tuple(candidates) if candidates else DEFAULT_ENCODING_CANDIDATES
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return "utf-8"
    key = str(p.resolve())
    hit = _ENCODING_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size and hit[2] == cands:
        return hit[3]
    try:
        data = p.read_bytes()
    except OSError:
        return "utf-8"
    enc = _sniff_bytes(data, cands)
    _ENCODING_CACHE[key] = (st.st_mtime_ns, st.st_size, cands, enc)
    return enc


def _remember_encoding(p: Path, enc: str, cands: tuple[str, ...]) -> None:
    try:
        st = p.stat()
    except OSError:
        return
    _ENCODING_CACHE[str(p.resolve())] = (st.st_mtime_ns, st.st_size, cands, enc)


def _java_unicode_escape(text: str, enc: str) -> str:
    """Replace characters the target encoding cannot represent by Java \\uXXXX escapes (valid anywhere in a
    .java source, including comments and string literals)."""
    out: list[str] = []
    for ch in text:
        try:
            ch.encode(enc)
            out.append(ch)
        except UnicodeEncodeError:
            b = ch.encode("utf-16-be")
            out.extend(f"\\u{b[i]:02x}{b[i + 1]:02x}" for i in range(0, len(b), 2))
    return "".join(out)


def fix_zip_name(info: zipfile.ZipInfo) -> str:
    """Entry name with legacy (non-UTF-8-flagged) names re-decoded: Python decodes them as cp437, Korean Windows
    zip tools wrote cp949 (sometimes raw UTF-8 without the flag). Falls back to the name as decoded."""
    name = info.filename
    if info.flag_bits & _ZIP_UTF8_FLAG:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    if raw.isascii():
        return name
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def posix_rel(p: str | PurePath) -> str:
    """A relative path with forward slashes on every OS (`str(WindowsPath)` has backslashes, every consumer compares
    with "/")."""
    return p.as_posix() if isinstance(p, PurePath) else str(p).replace(os.sep, "/")


def _relative_posix(p: PurePath, root: PurePath) -> Optional[str]:
    """`p` relative to `root` as a POSIX string; None when it is not below `root` (Windows: case-insensitive)."""
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


_MAVEN_OUTPUT_MARKERS = ("maven-status", "maven-archiver")      # Maven's own bookkeeping inside target/


def _is_output_target(parent: Path, parent_parts: tuple[str, ...] | list[str], has_pom: Optional[bool] = None) -> bool:
    """Does `parent/target` hold Maven build output? Never below a `src` directory; otherwise when a pom.xml sits
    beside it — or Maven's bookkeeping dirs are inside it (a module whose pom the restructure moved away)."""
    if "src" in parent_parts:
        return False
    if has_pom is None:
        has_pom = (parent / "pom.xml").is_file()
    return has_pom or any((parent / "target" / m).is_dir() for m in _MAVEN_OUTPUT_MARKERS)


def is_build_output_path(rel: str | PurePath, root: Optional[str | Path] = None) -> bool:
    """Is the project-relative `rel` a Maven build-output directory (or inside one)? A path component `target`
    counts only when no `src` component comes before it and — with `root` given — its parent directory holds a
    pom.xml (or the dir has Maven's `maven-status/` / `maven-archiver/`). A package `com/x/target/` (under src/, or
    without a pom.xml beside it) is source: the old rule (`"target" in parts`) dropped it silently from the run copy,
    the KB scans and result.zip (round 10)."""
    parts = [x for x in str(rel).replace("\\", "/").split("/") if x not in ("", ".")]
    for i, part in enumerate(parts):
        if part == "src":
            return False
        if part == "target" and (root is None or _is_output_target(Path(root).joinpath(*parts[:i]), parts[:i])):
            return True
    return False


_INTAKE_PATTERNS = shutil.ignore_patterns(".git", ".idea", "*.iml", ".DS_Store", "__MACOSX")


def _intake_ignore(base: Path):
    """copytree `ignore`: IDE / VCS noise everywhere, `target` only where it is build output (B4)."""
    base = Path(base)

    def ignore(dirpath: str, names: list[str]) -> set[str]:
        out = set(_INTAKE_PATTERNS(dirpath, names))
        if "target" in names:
            d = Path(dirpath)
            try:
                rel_parts = d.relative_to(base).parts
            except ValueError:
                rel_parts = ()
            if (d / "target").is_dir() and _is_output_target(d, rel_parts):
                out.add("target")
        return out

    return ignore


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    plan: list[tuple[zipfile.ZipInfo, Path]] = []
    for member in zf.infolist():
        name = fix_zip_name(member)
        if name != member.filename:
            log.info("zip entry name re-decoded: %r -> %r", member.filename, name)
        target = (dest / name).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise ValueError(f"zip slip blocked: {name}")
        plan.append((member, target))
    for member, target in plan:
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


_ROOT_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                     "build.xml")


def _detect_project_root(extracted: Path) -> Path:
    """If the archive has a single top-level folder (and no build file at top), descend into it. Gradle / Ant
    build files stop the descent too, so intake rejects them naming the real tool (catalog S4)."""
    entries = [p for p in extracted.iterdir() if not p.name.startswith("__MACOSX") and p.name != ".DS_Store"]
    if any((extracted / n).exists() for n in _ROOT_BUILD_FILES):
        return extracted
    if len(entries) == 1 and entries[0].is_dir():
        return _detect_project_root(entries[0])
    return extracted


RUN_DIR_FILES = ("errors.jsonl", "errors.md", "report.md", "report.json", "sbmig.log", "changes.patch",
                 "journal.jsonl", "plan.json", "profile.json", "result.zip")   # + logs/, project/ (PLAN-v2.1 §3.5)


def logs_dir(ws: Any) -> Path:
    """`<run dir>/logs` of a Workspace — or of a stand-in that only has `run_dir` (tests, views)."""
    d = getattr(ws, "logs_dir", None)
    return Path(d) if d is not None else Path(ws.run_dir) / "logs"


class Workspace:
    def __init__(self, root: Path, run_dir: Path, run_id: str):
        self.root = Path(root).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.run_id = run_id
        self.logs_dir = self.run_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.run_dir / "journal.jsonl"
        self._log_counter = 0
        self.git_branch = f"sbmig/{run_id}"
        self._git_ready = False
        self.encoding_candidates: tuple[str, ...] = DEFAULT_ENCODING_CANDIDATES
        self._read_encodings: dict[str, str] = {}      # resolved path → encoding it was last read/written with
        try:                                           # `dump-threads` file in the run dir → thread dump (diag)
            from sbmig.core.diag import watch_dump_trigger
            watch_dump_trigger(self.run_dir)
        except Exception:  # noqa: BLE001  # pragma: no cover - diagnostics never break a run
            log.debug("dump-threads trigger not registered for %s", self.run_dir, exc_info=True)

    # ------------------------------------------------------------------ factory
    @classmethod
    def from_input(cls, src: str | Path, out_dir: str | Path, run_id: Optional[str] = None) -> "Workspace":
        """Copy a directory or extract a zip into <out_dir>/<run_id>/project and return a Workspace."""
        src = Path(src).resolve()
        run_id = run_id or new_run_id()
        run_dir = Path(out_dir).resolve() / run_id
        project_dir = run_dir / "project"
        if project_dir.exists():
            raise FileExistsError(project_dir)
        project_dir.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, project_dir, symlinks=False, ignore=_intake_ignore(src))
            root = project_dir
        elif zipfile.is_zipfile(src):
            tmp = Path(tempfile.mkdtemp(prefix="sbmig-unzip-", dir=str(run_dir)))
            with zipfile.ZipFile(src) as zf:
                _safe_extract(zf, tmp)
            detected = _detect_project_root(tmp)
            shutil.copytree(detected, project_dir, symlinks=False, ignore=_intake_ignore(detected))
            shutil.rmtree(tmp, ignore_errors=True)
            root = project_dir
        else:
            raise ValueError(f"input must be a directory or a zip: {src}")
        ws = cls(root, run_dir, run_id)
        ws.journal({"event": "intake", "source": str(src), "project_dir": str(project_dir)})
        return ws

    # ------------------------------------------------------------------ files
    def abs(self, rel: str | Path) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def rel(self, path: str | Path) -> str:
        """Project-relative path with forward slashes on every OS (round 10: `str(Path)` gave backslashes on Windows
        while every consumer compares with "/"). A path outside the project comes back absolute (also "/")."""
        p = Path(path)
        if not p.is_absolute():
            return p.as_posix()
        rel = _relative_posix(p.resolve(), self.root)
        return rel if rel is not None else p.as_posix()

    def is_build_output(self, rel: str | Path) -> bool:
        """`rel` is (inside) a Maven build-output `target/` of this project (see `is_build_output_path`)."""
        return is_build_output_path(self.rel(rel), self.root)

    def exists(self, rel: str | Path) -> bool:
        return self.abs(rel).exists()

    # -- encodings (catalog E1): every file is read and written back in its own detected encoding
    def set_encoding_candidates(self, candidates: Optional[list[str] | tuple[str, ...]]) -> None:
        """Candidate encodings tried in order (cfg.build.encoding_candidates). Empty/None → defaults."""
        self.encoding_candidates = tuple(candidates) if candidates else DEFAULT_ENCODING_CANDIDATES

    def encoding_of(self, rel: str | Path) -> str:
        """Detected encoding of an existing file ("utf-8-sig" when it has a UTF-8 BOM); "utf-8" when missing."""
        return detect_encoding(self.abs(rel), self.encoding_candidates)

    def read(self, rel: str | Path, encoding: Optional[str] = None) -> str:
        """Text of a file decoded with `encoding` (None → detected). Newlines are returned untouched (\\r\\n kept)."""
        p = self.abs(rel)
        enc = encoding or detect_encoding(p, self.encoding_candidates)
        with open(p, "r", encoding=enc, errors="replace", newline="") as fh:
            text = fh.read()
        self._read_encodings[str(p.resolve())] = enc
        return text

    def write(self, rel: str | Path, text: str, encoding: Optional[str] = None) -> None:
        """Write `text` (newlines untouched). encoding None → the encoding the file was read with / is detected as
        (UTF-8 BOM kept); new files → utf-8. Characters the legacy encoding cannot hold become \\uXXXX escapes in
        .java files; other files are then re-encoded as UTF-8 (logged)."""
        p = self.abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        key = str(p.resolve())
        enc = encoding
        if enc is None:
            if p.is_file():
                enc = detect_encoding(p, self.encoding_candidates)
                if enc == "utf-8" and self._read_encodings.get(key) not in (None, "utf-8"):
                    enc = self._read_encodings[key]       # e.g. ascii-only now, but read as cp949 before
            else:
                enc = self._read_encodings.get(key, "utf-8")
        try:
            data = text.encode(enc)
        except UnicodeEncodeError:
            if p.suffix == ".java":
                data = _java_unicode_escape(text, enc).encode(enc)
            else:
                log.warning("%s: text not representable in %s, writing UTF-8 instead", self.rel(p), enc)
                enc = "utf-8"
                data = text.encode(enc)
        except LookupError:
            enc = "utf-8"
            data = text.encode(enc)
        p.write_bytes(data)
        self._read_encodings[key] = enc
        _remember_encoding(p, enc, self.encoding_candidates)

    def delete(self, rel: str | Path) -> None:
        p = self.abs(rel)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def move(self, src_rel: str | Path, dst_rel: str | Path) -> None:
        src, dst = self.abs(src_rel), self.abs(dst_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    def walk(self) -> Iterator[tuple[str, list[str], list[str]]]:
        """os.walk over the project: (rel dir with "/" — "" for the root, dir names, file names). `.git` and Maven
        build output (`target` beside a pom.xml or with `maven-status/` inside, never below src/) are pruned, so their
        contents are never listed."""
        root = str(self.root)
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else posix_rel(rel_dir)
            parts = rel_dir.split("/") if rel_dir else []
            dirnames[:] = sorted(d for d in dirnames if d != ".git" and not (
                d == "target" and _is_output_target(Path(dirpath), parts, "pom.xml" in filenames)))
            yield rel_dir, dirnames, filenames

    def glob(self, pattern: str) -> list[str]:
        """Project-relative paths (POSIX) of files and directories whose *name* matches `pattern` (like
        `rglob(pattern)`), outside `.git` and build output."""
        if "/" in pattern or "\\" in pattern:
            return sorted(r for r in (self.rel(p) for p in self.root.rglob(pattern))
                          if ".git" not in r.split("/") and not is_build_output_path(r, self.root))
        out: list[str] = []
        for rel_dir, dirnames, filenames in self.walk():
            for name in (*dirnames, *filenames):
                if fnmatch.fnmatch(name, pattern):
                    out.append(f"{rel_dir}/{name}" if rel_dir else name)
        return sorted(out)

    def java_files(self) -> list[str]:
        return self.glob("*.java")

    def pom_files(self) -> list[str]:
        return [p for p in self.glob("pom.xml")]

    # ------------------------------------------------------------------ logs / journal
    def new_log_path(self, label: str, ext: str = "log") -> Path:
        self._log_counter += 1
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
        return self.logs_dir / f"{self._log_counter:03d}-{safe}.{ext}"

    def journal(self, event: dict[str, Any]) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id, **event}
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=_json_default) + "\n")

    def save_model(self, name: str, model: BaseModel) -> Path:
        p = self.run_dir / name
        p.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        return p

    # ------------------------------------------------------------------ git snapshots
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Workspace git: never interactive. The user's global git config may ask for a GPG passphrase
        (`commit.gpgsign`), run hooks or call a credential manager — in a service process on Windows that waits
        forever, which is what a run "stuck after llm_fix" looked like (round 9c). So: stdin closed, prompts off,
        signing / hooks disabled per call, and a hard timeout (`GIT_TIMEOUT_S`).
        Output is decoded as UTF-8 with replacement characters, never with the console code page: git emits UTF-8
        (and raw file bytes in diffs), and a `text=True` cp949 decode on Windows died inside subprocess's reader
        thread, which left `stdout=None` ("could not write changes.patch: data must be str, not NoneType",
        2026-10-21 company run). `_git_bytes` keeps the raw bytes (patch files)."""
        cp = self._git_bytes(*args, check=check)
        return subprocess.CompletedProcess(cp.args, cp.returncode, _as_text(cp.stdout), _as_text(cp.stderr))

    def _git_bytes(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GCM_INTERACTIVE": "never",
               "GIT_EDITOR": "true", "GIT_AUTHOR_NAME": "sbmig", "GIT_AUTHOR_EMAIL": "sbmig@local",
               "GIT_COMMITTER_NAME": "sbmig", "GIT_COMMITTER_EMAIL": "sbmig@local"}
        no_hooks = str(self.run_dir / "no-hooks")                       # a dir that does not exist = no hooks
        cmd = ["git"]
        for kv in (*GIT_CONFIG, f"core.hooksPath={no_hooks}"):
            cmd += ["-c", kv]
        cmd += list(args)
        try:
            cp = subprocess.run(cmd, cwd=self.root, capture_output=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S, creationflags=_child_creationflags())
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S:.0f}s in {self.root} — something "
                           "outside sbmig is blocking git (antivirus / indexer lock, a global hook, a credential or "
                           "GPG prompt); sbmig already disables prompts, hooks and signing per call") from e
        if check and cp.returncode != 0:
            err = _as_text(cp.stderr).strip() or _as_text(cp.stdout).strip()
            raise GitError(f"git {' '.join(args)} failed: {err}")
        return cp

    def git_init(self) -> str:
        """Ensure the project dir is a git repo on branch sbmig/<run_id>, with everything committed."""
        if not (self.root / ".git").exists():
            self._git("init", "-q")
            self._git("checkout", "-q", "-b", self.git_branch)
        else:
            self._git("checkout", "-q", "-B", self.git_branch)
        info = self.root / ".git" / "info"
        if info.parent.is_dir():
            info.mkdir(exist_ok=True)
            (info / "attributes").write_text(GIT_INFO_ATTRIBUTES, encoding="utf-8")
        ignore = self.root / ".gitignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        present = set(existing.splitlines())
        for line in self._gitignore_lines():
            if line not in present:
                existing += ("\n" if existing and not existing.endswith("\n") else "") + line + "\n"
                present.add(line)
        ignore.write_text(existing, encoding="utf-8")
        self._git_ready = True
        return self.commit("sbmig: as-is snapshot", allow_empty=True)

    def _gitignore_lines(self) -> list[str]:
        """The lines git_init makes sure the root .gitignore has. `target/` is build output only where a pom.xml sits
        beside it: `!**/src/**/target/` keeps packages such as `src/main/java/com/x/target/`, and every other
        `target` dir of the as-is tree that is not build output gets its own `!/<dir>/target/` (B4); the `…/**` twins
        re-include the files too when the project's own .gitignore says `**/target/**`. Generated files outside
        target/ (`.flattened-pom.xml`, `src/main/generated/` …) are ignored unless the as-is tree has them (B8: they
        made `is_dirty()` true after a build, so "no file changed" was never detected)."""
        keep_targets: list[str] = []
        seen_generated: set[str] = set()
        for rel_dir, dirnames, filenames in self.walk():
            parts = rel_dir.split("/") if rel_dir else []
            if "target" in dirnames and "src" not in parts:
                d = f"!/{rel_dir}/target/" if rel_dir else "!/target/"
                keep_targets += [d, d + "**"]
            if ".flattened-pom.xml" in filenames:
                seen_generated.add(".flattened-pom.xml")
            for d in (".apt_generated", ".apt_generated_tests"):
                if d in dirnames:
                    seen_generated.add(d + "/")
            if "generated" in dirnames and len(parts) >= 2 and parts[-2] == "src" and parts[-1] in ("main", "test"):
                seen_generated.add(f"**/src/{parts[-1]}/generated/")
        lines = ["target/", "!**/src/**/target/", "!**/src/**/target/**", *keep_targets, ".sbmig/", "*.class"]
        lines += [g for g in GENERATED_IGNORES if g not in seen_generated]
        return lines

    def _clean_excludes(self) -> list[str]:
        """`git clean` arguments: build output stays, `target` dirs that are sources are cleaned like any other."""
        out = ["-e", "target/", "-e", "!**/src/**/target/"]
        try:
            text = (self.root / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("!/") and line.endswith("/target/"):
                out += ["-e", line]
        return out

    def commit(self, message: str, allow_empty: bool = False, *, paths: Optional[Iterable[str]] = None) -> str:
        """Commit the working tree (`paths` None: everything, incl. ignored jars the as-is tree keeps) or ONLY the
        given root-relative paths (v2.1b, 2026-10-27: an LLM attempt stages what its script / edits reported — a
        module jar the build copied into `lib/` must not ride along into the attempt and its `files`)."""
        if not self._git_ready:
            self.git_init()
        if paths is None:
            self._git("add", "-A")
            self._force_add_ignored_jars()
        else:
            wanted = [p for p in dict.fromkeys(str(x).replace("\\", "/").strip("/") for x in paths)
                      if p and not Path(p).is_absolute() and ".." not in p.split("/")]   # inside the project only
            if not wanted and not allow_empty:
                return self.head()
            for i in range(0, len(wanted), 200):          # -f: a path the script changed on purpose, even if ignored
                chunk = wanted[i:i + 200]
                if self._git("add", "-A", "-f", "--", *chunk, check=False).returncode != 0:
                    for one in chunk:                     # one bad pathspec must not drop the others
                        self._git("add", "-A", "-f", "--", one, check=False)
            if not allow_empty and self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return self.head()                        # the reported files are back as they were: nothing staged
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        cp = self._git(*args, check=False)
        if cp.returncode != 0 and "nothing to commit" not in (cp.stdout + cp.stderr) \
                and "no changes added to commit" not in (cp.stdout + cp.stderr):
            raise GitError(cp.stderr.strip() or cp.stdout.strip())
        return self.head()

    def _force_add_ignored_jars(self) -> None:
        """A project .gitignore saying `*.jar` / `lib/` kept legacy WEB-INF/lib and systemPath jars out of every
        snapshot: changes.patch missed them and revert_to could not bring back one a step had moved (round 10). Jars
        outside build output are force-added; nothing else the project ignores is (logs, node_modules, local env)."""
        cp = self._git("ls-files", "-z", "--others", "--ignored", "--exclude-standard", check=False)
        jars = [f for f in (cp.stdout or "").split("\0")
                if f.lower().endswith(".jar") and "node_modules/" not in f and not is_build_output_path(f, self.root)]
        for i in range(0, len(jars), 200):
            self._git("add", "-f", "--", *jars[i:i + 200], check=False)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def status_paths(self) -> set[str]:
        """Root-relative posix paths `git status` reports (modified / deleted / untracked; ignored files excluded)."""
        out = self._git("status", "--porcelain", "-z", "--untracked-files=all", check=False).stdout or ""
        paths: set[str] = set()
        items = out.split("\0")
        i = 0
        while i < len(items):
            item = items[i]
            if len(item) > 3:
                paths.add(item[3:].replace("\\", "/"))
                if item[0] in "RC":                      # a rename / copy carries the old name in the next item
                    i += 1
            i += 1
        return paths

    def restore_build_byproducts(self, before: set[str]) -> list[str]:
        """v2.1b (company run, 2026-10-27): a build writes files OUTSIDE `target/` too — a module jar copied into
        `lib/` by maven-dependency-plugin, generated sources, a rewritten `.flattened-pom.xml` — and `git add -A`
        in the next commit (an LLM attempt, the restructure) then carried them along: the attempt's `files` named
        `xxx-swing/lib/xxx-swing-1.0-SNAPSHOT.jar`, its diff held a jar, the review refused to run on a dirty tree.
        Called right after a build with the `status_paths()` taken before it: every path that became dirty DURING the
        build is a build product — a tracked one is checked out again, an untracked one is excluded through
        `.git/info/exclude`. Returns the paths handled. Never raises."""
        try:
            after = self.status_paths()
        except Exception:  # noqa: BLE001 - git trouble: the next commit behaves as before
            return []
        new = sorted(p for p in after - set(before) if p and not is_build_output_path(p, self.root))
        if not new:
            return []
        tracked: set[str] = set()
        for i in range(0, len(new), 200):
            cp = self._git("ls-files", "-z", "--", *new[i:i + 200], check=False)
            tracked |= {x.replace("\\", "/") for x in (cp.stdout or "").split("\0") if x}
        restored = [p for p in new if p in tracked]
        for i in range(0, len(restored), 200):
            self._git("checkout", "-q", "--", *restored[i:i + 200], check=False)
        untracked = [p for p in new if p not in tracked]
        if untracked:
            exclude = self.root / ".git" / "info" / "exclude"
            try:
                exclude.parent.mkdir(exist_ok=True)
                present = set(exclude.read_text(encoding="utf-8").splitlines()) if exclude.exists() else set()
                lines = [f"/{p}" for p in untracked if f"/{p}" not in present]
                if lines:
                    with exclude.open("a", encoding="utf-8") as fh:
                        fh.write(("" if not present else "\n") + "\n".join(lines) + "\n")
            except OSError as e:
                log.warning("could not exclude build products %s: %s", untracked[:3], e)
        log.info("build products outside target/ set aside: %s%s", ", ".join(new[:5]),
                 f" (+{len(new) - 5} more)" if len(new) > 5 else "")
        return new

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain").stdout.strip())

    def revert_to(self, sha: str) -> None:
        """Hard reset the working copy to a previous snapshot (used on regressions)."""
        self._git("reset", "-q", "--hard", sha)
        self._git("clean", "-qfd", *self._clean_excludes())

    def discard_uncommitted(self) -> None:
        self._git("checkout", "-q", "--", ".")
        self._git("clean", "-qfd", *self._clean_excludes())

    def diff(self, from_sha: str, to_sha: Optional[str] = None, stat: bool = False) -> str:
        args = ["diff", "--no-color"]
        if stat:
            args.append("--stat")
        args.append(from_sha)
        if to_sha:
            args.append(to_sha)
        return self._git(*args).stdout

    def diff_bytes(self, from_sha: str, to_sha: Optional[str] = None) -> bytes:
        """`git diff` as emitted: file content bytes untouched (an MS949 source stays MS949 in the patch)."""
        args = ["diff", "--no-color", from_sha] + ([to_sha] if to_sha else [])
        return _as_bytes(self._git_bytes(*args).stdout)

    def changed_files_since(self, sha: str) -> list[str]:
        """Files changed since `sha` (committed or not) plus untracked ones; NUL-separated name lists, so Korean and
        other non-ASCII names come back as they are (never `"\\355\\225…"`)."""
        out = self._git("diff", "--name-only", "-z", sha).stdout
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
        return sorted({f for f in (out + "\0" + untracked).split("\0") if f.strip()})

    def changed_between(self, from_sha: str, to_sha: str) -> list[str]:
        """Files that differ between two commits (POSIX paths, non-ASCII names verbatim), in git's order."""
        out = self._git("diff", "--name-only", "-z", from_sha, to_sha).stdout
        return [f for f in out.split("\0") if f.strip()]

    def first_commit(self) -> str:
        return self._git("rev-list", "--max-parents=0", "HEAD").stdout.strip().splitlines()[-1]

    # ------------------------------------------------------------------ outputs
    def write_patch(self, name: str = "changes.patch") -> Path:
        p = self.run_dir / name
        p.write_bytes(self.diff_bytes(self.first_commit()))
        return p

    def zip_result(self, name: str = "result.zip") -> Path:
        out = self.run_dir / name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_dir, _dirs, filenames in self.walk():           # .git and build output pruned (B4)
                for fname in sorted(filenames):
                    rel = f"{rel_dir}/{fname}" if rel_dir else fname
                    zf.write(self.root / rel, f"{self.root.name}/{rel}")
        return out


def _json_default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    return str(o)


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.t0
