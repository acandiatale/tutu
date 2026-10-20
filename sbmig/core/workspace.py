"""Workspace: the migrated project copy + run output directory + git snapshots + journal.

Layout of one run:
    <out_dir>/<run_id>/
        project/            ← working copy (git repo; one commit per step/attempt)
        logs/               ← build-*.log, rewrite-*.log, llm-*.json
        journal.jsonl       ← append-only event log (every step, build, decision, attempt)
        profile.json, plan.json, report.json, report.md, result.zip, changes.patch
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

log = logging.getLogger("sbmig.core.workspace")

DEFAULT_ENCODING_CANDIDATES: tuple[str, ...] = ("utf-8", "cp949", "euc-kr", "shift_jis", "latin-1")
_UTF8_BOM = b"\xef\xbb\xbf"
_ZIP_UTF8_FLAG = 0x800

# (resolved path) -> (mtime_ns, size, candidates, encoding)
_ENCODING_CACHE: dict[str, tuple[int, int, tuple[str, ...], str]] = {}


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
            shutil.copytree(src, project_dir, symlinks=False,
                            ignore=shutil.ignore_patterns(".git", "target", ".idea", "*.iml", ".DS_Store", "__MACOSX"))
            root = project_dir
        elif zipfile.is_zipfile(src):
            tmp = Path(tempfile.mkdtemp(prefix="sbmig-unzip-", dir=str(run_dir)))
            with zipfile.ZipFile(src) as zf:
                _safe_extract(zf, tmp)
            detected = _detect_project_root(tmp)
            shutil.copytree(detected, project_dir, symlinks=False,
                            ignore=shutil.ignore_patterns(".git", "target", ".idea", "*.iml", ".DS_Store", "__MACOSX"))
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
        p = Path(path)
        try:
            return str(p.resolve().relative_to(self.root)) if p.is_absolute() else str(p)
        except ValueError:
            return str(p)

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

    def glob(self, pattern: str) -> list[str]:
        return sorted(self.rel(p) for p in self.root.rglob(pattern)
                      if ".git" not in p.parts and "target" not in p.parts)

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
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "sbmig", "GIT_AUTHOR_EMAIL": "sbmig@local",
               "GIT_COMMITTER_NAME": "sbmig", "GIT_COMMITTER_EMAIL": "sbmig@local"}
        cp = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, env=env)
        if check and cp.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {cp.stderr.strip() or cp.stdout.strip()}")
        return cp

    def git_init(self) -> str:
        """Ensure the project dir is a git repo on branch sbmig/<run_id>, with everything committed."""
        if not (self.root / ".git").exists():
            self._git("init", "-q")
            self._git("checkout", "-q", "-b", self.git_branch)
        else:
            self._git("checkout", "-q", "-B", self.git_branch)
        ignore = self.root / ".gitignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        for line in ("target/", ".sbmig/", "*.class"):
            if line not in existing:
                existing += ("\n" if existing and not existing.endswith("\n") else "") + line + "\n"
        ignore.write_text(existing, encoding="utf-8")
        self._git_ready = True
        return self.commit("sbmig: as-is snapshot", allow_empty=True)

    def commit(self, message: str, allow_empty: bool = False) -> str:
        if not self._git_ready:
            self.git_init()
        self._git("add", "-A")
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        cp = self._git(*args, check=False)
        if cp.returncode != 0 and "nothing to commit" not in (cp.stdout + cp.stderr):
            raise GitError(cp.stderr.strip() or cp.stdout.strip())
        return self.head()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain").stdout.strip())

    def revert_to(self, sha: str) -> None:
        """Hard reset the working copy to a previous snapshot (used on regressions)."""
        self._git("reset", "-q", "--hard", sha)
        self._git("clean", "-qfd", "-e", "target/")

    def discard_uncommitted(self) -> None:
        self._git("checkout", "-q", "--", ".")
        self._git("clean", "-qfd", "-e", "target/")

    def diff(self, from_sha: str, to_sha: Optional[str] = None, stat: bool = False) -> str:
        args = ["diff", "--no-color"]
        if stat:
            args.append("--stat")
        args.append(from_sha)
        if to_sha:
            args.append(to_sha)
        return self._git(*args).stdout

    def changed_files_since(self, sha: str) -> list[str]:
        out = self._git("diff", "--name-only", sha).stdout
        untracked = self._git("ls-files", "--others", "--exclude-standard").stdout
        files = {l.strip() for l in (out + untracked).splitlines() if l.strip()}
        return sorted(files)

    def first_commit(self) -> str:
        return self._git("rev-list", "--max-parents=0", "HEAD").stdout.strip().splitlines()[-1]

    # ------------------------------------------------------------------ outputs
    def write_patch(self, name: str = "changes.patch") -> Path:
        p = self.run_dir / name
        p.write_text(self.diff(self.first_commit()), encoding="utf-8")
        return p

    def zip_result(self, name: str = "result.zip") -> Path:
        out = self.run_dir / name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in self.root.rglob("*"):
                if path.is_dir():
                    continue
                parts = path.relative_to(self.root).parts
                if parts and (parts[0] == ".git" or "target" in parts):
                    continue
                zf.write(path, str(Path(self.root.name) / path.relative_to(self.root)))
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
