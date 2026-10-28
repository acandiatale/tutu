"""디스크 정리(round 10): 윈도우 드라이브 하나에 실행 사본이 500 개쯤 쌓이므로 끝난 실행의 큰 덩어리를 지운다.
흐름상 위치: (1) 일괄 실행 — 프로젝트마다 보고서와 ``result.zip`` 을 쓴 **뒤에** 러너가
:func:`cleanup_run` 을 부른다(``fleet.cleanup``: ``none`` / ``build-output`` / ``workspace``).
(2) ``sbmig clean`` — :func:`plan_clean` 이 지울 목록을 만들고(``--dry-run`` 은 여기까지)
:func:`execute` 가 지운다. 실행 중·대기 중인 실행은 절대 건드리지 않는다.
``workspace`` 모드도 ``result.zip`` 이 있을 때만 프로젝트 사본을 지운다(이관된 코드가 어딘가 남아야 함).
``target`` 은 옆에 ``pom.xml`` 이 있는 것만 지운다(``target`` 이라는 자바 패키지는 그대로). 삭제는 최선
노력이며(윈도우의 읽기 전용 git 객체 포함), 못 지운 것은 로그만 남기고 해제 용량에 넣지 않는다.

Disk lifecycle (round 10): ~500 run copies on one Windows drive.

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
# 실행 폴더에 남는 파일 목록(workspace 모드는 ``project/`` 만 지우므로 이것들은 그대로 남는다). 여기서는 쓰지
# 않고, workspace.RUN_DIR_FILES 와 같은지 테스트가 지킨다(tests/core/test_workspace_paths.py)
KEEP_FILES = ("result.zip", "report.md", "report.json", "errors.jsonl", "errors.md", "changes.patch", "journal.jsonl",
              "sbmig.log", "plan.json", "profile.json")      # = workspace.RUN_DIR_FILES (+ logs/), PLAN-v2.1 §3.5
_SKIP_DIRS = {".git", "node_modules", ".idea", ".svn"}
_UPLOAD_RE = re.compile(r"^(web-\d{8}-\d{6}-[0-9a-f]{6})-")
# 웹 업로드가 받는 중에 쓰는 임시 파일: 하루가 지나도 남아 있으면 끊긴 업로드로 본다
STALE_PART_S = 24 * 3600          # an `.incoming-*.part` older than this is a dead upload


def dir_size(path: Path) -> int:
    """디렉토리 아래 파일 크기 합(바이트). 읽을 수 없는 파일은 건너뛴다(링크는 따라가지 않음)."""
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
    """윈도우: git 객체 파일은 읽기 전용이라 ``rmtree`` 가 못 지운다 — 쓰기 가능으로 바꾸고 한 번 더 시도.

    Windows: git object files are read-only, `rmtree` cannot unlink them — make writable, retry once."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError as e:
        log.debug("cleanup: cannot remove %s: %s", path, e)


def remove(path: Path) -> int:
    """파일 또는 디렉토리 트리를 지우고 실제로 해제된 바이트를 돌려준다(일부만 지워졌으면 그만큼만).

    Delete a file or a directory tree; returns the bytes actually freed."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return 0
    before = _size(path)
    try:
        if path.is_dir() and not path.is_symlink():
            # Python 3.12 에서 onerror 가 폐기 예정 → onexc, 그 아래 버전은 onerror (둘 다 같은 처리기)
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
    """``project_dir`` 아래에서 Maven 모듈의 ``target/`` (같은 폴더에 ``pom.xml`` 이 있는 것)만 모은다.
    ``.git`` 등과 찾은 ``target`` 안으로는 내려가지 않는다.

    `target/` directories that belong to a Maven module (a sibling `pom.xml`) under `project_dir`."""
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
    """바이트 수를 사람이 읽는 크기로(``512 B``, ``3.4 GB``)."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"  # pragma: no cover


def cleanup_run(run_dir: str | Path, mode: str) -> dict:
    """끝난 실행 폴더 하나에 ``fleet.cleanup`` 을 적용하고 ``{"mode", "freed", "removed"}`` 를 돌려준다.
    ``workspace`` 인데 ``result.zip`` 이 없거나 비었으면 사본을
    지키고 ``build-output`` 으로 낮춰 ``target/`` 만 지운다.

    `fleet.cleanup` for one finished run directory; returns {"mode", "freed", "removed": [paths]}."""
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
    """``sbmig clean`` 이 지울 것 하나: 종류(프로젝트 사본 /
    업로드 / 끊긴 업로드 / LLM 기록), 경로, 크기, 실행 ID."""
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
    """``--keep-failed`` 의 기준: 종료 코드가 0 이 아닌 실행(최종 빌드 실패, 차단 항목 있음, 중단).
    report.json 이 없으면 웹의 web-status.json 을 보고, 그것도 없으면 실패로 쳐서 남긴다.

    Exit code ≠ 0 (see `sbmig.cli.run_exit_code`): no green final build, something blocked, or aborted."""
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
    """끝난 실행인가: report.json 또는 web-status.json 이 있어야 한다.
    없으면 실행 중·대기 중으로 보고 손대지 않는다."""
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
    """``sbmig clean`` 이 지울 목록(아직 지우지 않음). 끝난 실행만 대상이고, 그 ``project/`` 사본만 지운다
    (result.zip·보고서·패치·로그는 남음). ``fleet=True`` 면 그 일괄 실행 ID 로 시작하는 실행 폴더만 본다. 웹
    업로드는 실행이 끝난 것만, ``.part`` 는 하루 넘게 지난 것만, LLM 기록은 ``record_dirs`` 의 ``llm-*.json``.

    What `sbmig clean` would delete. Only FINISHED runs (report.json / web-status.json present) are touched;
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
    """:func:`plan_clean` 의 목록을 실제로 지우고 해제된 바이트 합을 돌려준다."""
    return sum(remove(i.path) for i in items)


def summarize(items: list[CleanItem]) -> list[str]:
    """종류별 한 줄 요약: 개수와 크기.

    One line per kind: `project-copy  12  3.4 GB`."""
    by: dict[str, list[CleanItem]] = {}
    for i in items:
        by.setdefault(i.kind, []).append(i)
    return [f"  {k:<15} {len(v):>5}  {human(sum(i.size for i in v))}" for k, v in by.items()]
