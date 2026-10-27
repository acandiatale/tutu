"""Disk lifecycle (round 10): ~500 run copies on one Windows drive.

* :func:`cleanup_run` — what the fleet does after each project (``fleet.cleanup``), AFTER the report and
  ``result.zip`` were written: ``build-output`` deletes the ``target/`` directories of the run copy (only a ``target``
  that sits next to a ``pom.xml`` — a Java package named ``target`` is never touched); ``workspace`` also deletes the
  project copy ``<run dir>/project`` (git history included) and keeps ``result.zip``, ``report.*``, ``errors.*``,
  ``changes.patch``, ``journal.jsonl``, ``sbmig.log``, ``logs/`` — only when ``result.zip`` exists, otherwise it falls back to
  ``build-output`` (the migrated code must survive somewhere).
* :func:`plan_clean` / :func:`execute` — ``sbmig clean``: finished run copies under an ``--out`` directory (or a
  fleet's), the web UI's ``_uploads/`` and the LLM records (``llm-NNNN.json``), with ``--older-than`` /
  ``--keep-failed`` / ``--dry-run``.

Deletion is best effort and Windows-proof (read-only git objects are made writable first); what could not be removed
is logged and counted as not freed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("sbmig.fleet")

CLEANUP_MODES = ("none", "build-output", "workspace")
KEEP_FILES = ("result.zip", "report.md", "report.json", "errors.jsonl", "errors.md", "changes.patch", "journal.jsonl",
              "sbmig.log", "plan.json", "profile.json")      # = workspace.RUN_DIR_FILES (+ logs/), PLAN-v2.1 §3.5
_SKIP_DIRS = {".git", "node_modules", ".idea", ".svn"}
_UPLOAD_RE = re.compile(r"^(web-\d{8}-\d{6}-[0-9a-f]{6})-")
STALE_PART_S = 24 * 3600          # an `.incoming-*.part` older than this is a dead upload


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _size(path: Path) -> int:
    try:
        return dir_size(path) if path.is_dir() else path.stat().st_size
    except OSError:
        return 0


def _on_rm_error(func, path, _exc_info) -> None:
    """Windows: git object files are read-only, `rmtree` cannot unlink them — make writable, retry once."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError as e:
        log.debug("cleanup: cannot remove %s: %s", path, e)


def remove(path: Path) -> int:
    """Delete a file or a directory tree; returns the bytes actually freed."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return 0
    before = _size(path)
    try:
        if path.is_dir() and not path.is_symlink():
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_on_rm_error)
            else:  # pragma: no cover - Python 3.10 / 3.11
                shutil.rmtree(path, onerror=_on_rm_error)
        else:
            try:
                path.unlink()
            except PermissionError:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                path.unlink()
    except OSError as e:
        log.warning("cleanup: could not remove %s: %s", path, e)
    return max(0, before - (_size(path) if path.exists() else 0))


def build_output_dirs(project_dir: Path) -> list[Path]:
    """`target/` directories that belong to a Maven module (a sibling `pom.xml`) under `project_dir`."""
    out: list[Path] = []
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        return out
    for root, dirs, files in os.walk(project_dir):
        if "target" in dirs and "pom.xml" in files:
            out.append(Path(root) / "target")
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not (d == "target" and "pom.xml" in files)]
    return out


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"  # pragma: no cover


def cleanup_run(run_dir: str | Path, mode: str) -> dict:
    """`fleet.cleanup` for one finished run directory; returns {"mode", "freed", "removed": [paths]}."""
    run_dir = Path(run_dir)
    result = {"mode": mode, "freed": 0, "removed": []}
    project = run_dir / "project"
    if mode not in ("build-output", "workspace") or not project.is_dir():
        return result
    if mode == "workspace":
        zp = run_dir / "result.zip"
        if zp.is_file() and zp.stat().st_size > 0:
            result["freed"] = remove(project)
            result["removed"].append(str(project))
            return result
        log.warning("cleanup: %s has no result.zip — keeping the project copy, deleting only target/", run_dir.name)
        result["mode"] = "build-output"
    for t in build_output_dirs(project):
        result["freed"] += remove(t)
        result["removed"].append(str(t))
    return result


# ------------------------------------------------------------------ sbmig clean
@dataclass
class CleanItem:
    kind: str                 # project-copy | upload | partial-upload | llm-record
    path: Path
    size: int
    owner: str = ""           # run id (project copies / uploads)


def _report(run_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _failed(data: Optional[dict], run_dir: Path) -> bool:
    """Exit code ≠ 0 (see `sbmig.cli.run_exit_code`): no green final build, something blocked, or aborted."""
    if data is None:
        try:
            st = json.loads((run_dir / "web-status.json").read_text(encoding="utf-8"))
            return st.get("exit_code") not in (0,)
        except (OSError, ValueError, AttributeError):
            return True
    flags = data.get("flags") or []
    aborted = any(f == "aborted" or str(f).startswith("aborted:") or f == "mvn-not-found" for f in flags)
    return aborted or not data.get("success") or bool(data.get("blocked"))


def _finished(run_dir: Path) -> bool:
    return (run_dir / "report.json").is_file() or (run_dir / "web-status.json").is_file()


def _age_s(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 0.0


def _fleet_prefix(fleet_out: Path) -> Optional[str]:
    try:
        return json.loads((fleet_out / "fleet" / "state.json").read_text(encoding="utf-8")).get("fleet_id")
    except (OSError, ValueError, AttributeError):
        return None


def plan_clean(out_dir: str | Path, *, older_than_days: float = 0.0, keep_failed: bool = False,
               fleet: bool = False, record_dirs: Iterable[str | Path] = (), what: Iterable[str] = ("runs", "uploads",
                                                                                                     "records")
               ) -> list[CleanItem]:
    """What `sbmig clean` would delete. Only FINISHED runs (report.json / web-status.json present) are touched;
    their `project/` copy goes (result.zip, report, patch, logs stay). `fleet=True`: only the run directories of the
    fleet whose `--out` is `out_dir` (`<fleet id>-…`)."""
    out_dir = Path(out_dir)
    what = set(what)
    min_age = max(0.0, float(older_than_days or 0)) * 86400
    items: list[CleanItem] = []
    prefix = _fleet_prefix(out_dir) if fleet else None
    run_dirs = sorted(p for p in out_dir.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))) \
        if out_dir.is_dir() else []
    if fleet:
        run_dirs = [p for p in run_dirs if prefix and p.name.startswith(prefix + "-")]
    if "runs" in what:
        for rd in run_dirs:
            project = rd / "project"
            if not project.is_dir() or not _finished(rd):
                continue
            marker = rd / "report.json" if (rd / "report.json").is_file() else rd / "web-status.json"
            if _age_s(marker) < min_age:
                continue
            if keep_failed and _failed(_report(rd), rd):
                continue
            items.append(CleanItem("project-copy", project, _size(project), rd.name))
    if "uploads" in what and not fleet:
        up = out_dir / "_uploads"
        for f in sorted(up.iterdir()) if up.is_dir() else []:
            if not f.is_file():
                continue
            if f.name.startswith(".incoming-"):
                if _age_s(f) >= max(min_age, STALE_PART_S):
                    items.append(CleanItem("partial-upload", f, _size(f)))
                continue
            m = _UPLOAD_RE.match(f.name)
            rd = out_dir / m.group(1) if m else None
            if rd is not None and rd.is_dir() and not _finished(rd):
                continue                                   # the run is still going (or queued)
            if _age_s(f) < min_age:
                continue
            if keep_failed and rd is not None and rd.is_dir() and _failed(_report(rd), rd):
                continue
            items.append(CleanItem("upload", f, _size(f), m.group(1) if m else ""))
    if "records" in what:
        for d in record_dirs:
            d = Path(d)
            for f in sorted(d.glob("llm-*.json")) if d.is_dir() else []:
                if _age_s(f) >= min_age:
                    items.append(CleanItem("llm-record", f, _size(f)))
    return items


def execute(items: Iterable[CleanItem]) -> int:
    return sum(remove(i.path) for i in items)


def summarize(items: list[CleanItem]) -> list[str]:
    """One line per kind: `project-copy  12  3.4 GB`."""
    by: dict[str, list[CleanItem]] = {}
    for i in items:
        by.setdefault(i.kind, []).append(i)
    return [f"  {k:<15} {len(v):>5}  {human(sum(i.size for i in v))}" for k, v in by.items()]
