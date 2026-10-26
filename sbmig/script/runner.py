"""ScriptRunner (ScriptRunnerLike): static check, then one `python -I` process per fix script.

`run` writes the code and a JSON payload into a fresh temp directory (never inside the project), starts
`<python> -I -X utf8 sbmig/script/harness.py <payload>` with that directory as cwd in its own process group, waits at
most `timeout_s` (default `script.timeout_seconds`) and kills the whole tree after it (`taskkill /T /F` on Windows,
the process group elsewhere — the same kill as Maven's, `sbmig.build.mvn`). The child's result JSON becomes a
`ScriptRunResult`; a killed or crashed child still reports the files it changed and its log from the journal it
flushes line by line. Changes stay uncommitted in the working tree: the caller commits or restores. Never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

from sbmig.build.mvn import _kill_group, _register, _unregister
from sbmig.core.config import Config
from sbmig.core.models import BuildError, ScriptRunResult
from sbmig.core.workspace import Workspace

from .check import ALLOWED_IMPORTS, check_script

log = logging.getLogger("sbmig.script")

HARNESS = Path(__file__).resolve().with_name("harness.py")
SBMIG_ROOT = Path(__file__).resolve().parents[2]          # the directory that holds the `sbmig` package
STDERR_TAIL_CHARS = 2000
IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000
# never handed to the script process (it cannot read the environment anyway; this keeps keys out of reach)
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH)|^LLM_", re.IGNORECASE)


def _popen_kwargs() -> dict[str, Any]:
    """Own process group so the whole tree can be killed: POSIX a new session; Windows a new process group with
    its own hidden console (the operator's Ctrl+C / Ctrl+Break never reach it; `taskkill /T /F` kills it)."""
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(subprocess, "CREATE_NO_WINDOW",
                                                                                   _CREATE_NO_WINDOW)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"         # ignored under -I; `-X utf8` and the harness's reconfigure decide
    env["PYTHONUTF8"] = "1"
    return env


def _error_dict(e: Any) -> dict[str, Any]:
    """p.errors entry: the contract's keys (+ `artifact` "g:a[:v]" for resolution errors)."""
    if isinstance(e, dict):
        return json.loads(json.dumps(e, default=str))
    if isinstance(e, BuildError):
        kind = e.kind.value if hasattr(e.kind, "value") else str(e.kind)
        return {"file": e.file, "line": e.line, "column": e.column, "message": e.message, "symbol": e.symbol,
                "location": e.location, "kind": kind, "module": e.module, "signature": e.signature,
                "signature_generic": e.signature_generic,
                "artifact": e.artifact.coordinates if e.artifact is not None else None}
    return {"message": str(e)}


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")


def _rel_posix(paths: Iterable[Any]) -> list[str]:
    out = {str(p).replace("\\", "/").lstrip("/") for p in paths if p}
    return sorted(p for p in out if p)


def _cap_log(lines: list[str], dropped: int, cap: int) -> list[str]:
    """At most `cap` lines; when lines were dropped the last kept one says how many."""
    cap = max(1, cap)
    lines = [str(x) for x in lines]
    extra = max(0, len(lines) - cap) + max(0, dropped)
    if not extra:
        return lines
    kept = lines[:cap - 1]
    return kept + [f"… {len(lines) - len(kept) + max(0, dropped)} more log line(s) not kept"]


def _read_journal(path: Path) -> tuple[list[str], list[str]]:
    """(log lines, changed files) a killed / crashed child flushed before it died."""
    logs: list[str] = []
    changed: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return logs, changed
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:                        # the last line of a killed process may be torn
            continue
        if "log" in ev:
            logs.append(str(ev["log"]))
        if "changed" in ev:
            changed.append(str(ev["changed"]))
    return logs, changed


class ScriptRunner:
    """Runs LLM fix scripts against a workspace (docs/INTERFACES.md "Script contract (S)")."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(self, code: str) -> list[str]:
        s = self.cfg.script
        return check_script(code, extra_imports=s.extra_allowed_imports, max_chars=s.max_code_chars)

    def run(self, ws: Workspace, code: str, *, errors: Iterable[BuildError] = (),
            timeout_s: Optional[int] = None) -> ScriptRunResult:
        t0 = time.monotonic()
        try:
            problems = self.check(code)
            if problems:
                more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
                result = ScriptRunResult(status="rejected", problems=problems,
                                         error=f"rejected by the static check: {problems[0]}{more}")
            else:
                result = self._execute(ws, code, list(errors or ()), timeout_s)
        except Exception as exc:  # never raises: an internal failure is the run's result
            log.debug("fix script runner failed", exc_info=True)
            result = ScriptRunResult(status="error", error=f"sbmig script runner failed: {type(exc).__name__}: {exc}")
        result.duration_s = round(time.monotonic() - t0, 3)
        first = _first_line(result.error)
        log.info("fix script %s in %.1fs: %d file(s) changed%s", result.status, result.duration_s,
                 len(result.changed_files), f" — {first}" if first else "")
        return result

    # ------------------------------------------------------------------ internals
    def _execute(self, ws: Workspace, code: str, errors: list[Any], timeout_s: Optional[int]) -> ScriptRunResult:
        s = self.cfg.script
        limit = timeout_s or s.timeout_seconds                # 0 / None → the config value; config 0 = no limit
        timeout = float(limit) if limit else None
        python = s.python or sys.executable
        if not python:
            return ScriptRunResult(status="error", error="no Python interpreter for the fix script "
                                                         "(set script.python)")
        root = Path(ws.root).resolve()
        tmp = Path(tempfile.mkdtemp(prefix="sbmig-script-"))
        try:
            code_path, result_path, journal_path = tmp / "fix_script.py", tmp / "result.json", tmp / "journal.jsonl"
            code_path.write_text(code, encoding="utf-8")
            payload = {
                "root": str(root), "code_path": str(code_path), "result_path": str(result_path),
                "journal_path": str(journal_path), "errors": [_error_dict(e) for e in errors],
                "encoding_candidates": list(self.cfg.build.encoding_candidates or ()),
                "log_lines": int(s.log_lines), "extra_imports": list(s.extra_allowed_imports or ()),
                "preload": [*ALLOWED_IMPORTS, *(s.extra_allowed_imports or ())], "sbmig_root": str(SBMIG_ROOT),
            }
            payload_path = tmp / "payload.json"
            payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            cmd = [str(python), "-I", "-X", "utf8", str(HARNESS), str(payload_path)]
            exit_code, timed_out, stderr = self._spawn(cmd, tmp, timeout)
            return self._collect(result_path, journal_path, exit_code, timed_out, stderr, timeout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _spawn(self, cmd: list[str], cwd: Path, timeout: Optional[float]) -> tuple[Optional[int], bool, str]:
        """(exit code, timed out, stderr+stdout tail)."""
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), env=_child_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_kwargs())
        except (OSError, ValueError) as exc:
            return None, False, f"cannot start {cmd[0]!r}: {exc}"
        _register(proc)                       # the web shutdown / interpreter exit kills it too
        timed_out = False
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                log.warning("fix script still running after %.0fs, killing its process tree (pid %s)",
                            timeout or 0, proc.pid)
                _kill_group(proc)
                try:
                    out, err = proc.communicate(timeout=10)
                except (subprocess.TimeoutExpired, ValueError, OSError):   # pragma: no cover - pipes held open
                    out, err = b"", b""
        except BaseException:                 # Ctrl+C in the waiting thread: never leave the child behind
            _kill_group(proc)
            raise
        finally:
            _unregister(proc)
        text = (err or b"").decode("utf-8", errors="replace") + (out or b"").decode("utf-8", errors="replace")
        return proc.returncode, timed_out, text[-STDERR_TAIL_CHARS:]

    def _collect(self, result_path: Path, journal_path: Path, exit_code: Optional[int], timed_out: bool,
                 stderr: str, timeout: Optional[float]) -> ScriptRunResult:
        cap = int(self.cfg.script.log_lines)
        data: Optional[dict[str, Any]] = None
        if not timed_out and result_path.is_file():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
        if isinstance(data, dict):
            status = data.get("status") if data.get("status") in ("ok", "error") else "error"
            return ScriptRunResult(status=status, changed_files=_rel_posix(data.get("changed_files") or ()),
                                   log=_cap_log(list(data.get("log") or ()), int(data.get("log_dropped") or 0), cap),
                                   error=str(data.get("error") or "") if status != "ok" else "")
        logs, changed = _read_journal(journal_path)
        if timed_out:
            error = f"timed out after {timeout or 0:.0f}s (process tree killed)"
            status = "timeout"
        elif exit_code is None:                   # the interpreter did not start
            error, status = stderr.strip() or "the script process could not be started", "error"
        else:
            tail = stderr.strip()
            error = (f"script process ended without a result (exit code {exit_code})"
                     + (f":\n{tail}" if tail else ""))
            status = "error"
        return ScriptRunResult(status=status, changed_files=_rel_posix(changed), log=_cap_log(logs, 0, cap),
                               error=error)
