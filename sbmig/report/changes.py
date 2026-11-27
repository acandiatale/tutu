"""자동 변환 뒤에 sbmig가 고친 파일을 모아 보고서의 "코드 수정 내역"을 만드는 모듈.
변환 직후 커밋과 지금 파일을 git으로 비교해서 파일마다 ``CodeChange`` 하나를 만든다.

Base = the commit right after the automatic conversion (OpenRewrite, restructure). Notes = the kept tries'
LLM-written `EditNote`s, reasons = the last kept try's `analysis` per (file, case).

A file several tries changed is one entry; a reverted change does not appear. Build output, binaries and
files under .git are left out. Text is redacted the way errors.md is. A git failure raises nothing and leaves
the list empty."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sbmig.core.models import CodeChange
from sbmig.report.errors import diff_stats, entries_of, kept

log = logging.getLogger(__name__)

DIFF_FILES = 30            # 정렬 순서로 앞 N개 파일만 diff 본문을 남긴다
DIFF_LINES = 80            # 파일 하나에 남기는 diff 줄 수. 넘치면 자른다
MAX_FILES = 500            # 목록에 넣는 최대 파일 수. code_changes_total은 전체를 센다
REASON_CHARS = 300         # 수정 이유는 이 길이 근처의 문장 경계에서 자른다

_STATUS = {"M": "modified", "A": "added", "D": "deleted"}
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip().removeprefix("./").strip("/")


def _order_key(path: str) -> tuple[int, str]:
    if path.endswith(".java"):
        return 0, path
    if path.rsplit("/", 1)[-1] == "pom.xml":
        return 1, path
    return 2, path


def _excluded(ws: Any, path: str) -> bool:
    parts = path.split("/")
    if ".git" in parts:
        return True
    try:
        if ws.is_build_output(path):
            return True
        is_ignored = getattr(ws, "is_ignored", None)
        return bool(is_ignored and is_ignored(path))
    except Exception:  # noqa: BLE001 - 판단할 수 없는 경로는 목록에 그대로 넣는다
        return False


def _decode(ws: Any, path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        enc = ws.encoding_of(path) if ws.exists(path) else "cp949"
    except Exception:  # noqa: BLE001
        enc = "cp949"
    try:
        return data.decode(enc or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def hunks_only(diff: str) -> list[str]:
    """파일 하나의 unified diff에서 헤더(diff --git, index, ---, +++)를 빼고 첫 @@ 줄부터 반환한다."""
    # splitlines는 \f, \x1c 같은 문자에서도 줄을 나누므로 \n으로만 나눈다
    lines = [ln.removesuffix("\r") for ln in (diff or "").split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            return lines[i:]
    return []


def first_sentences(text: str, limit: int = REASON_CHARS) -> str:
    """텍스트를 앞 문장들만 남겨 ``limit``자 안쪽에서 문장 경계로 자른다.
    첫 문장이 이미 길면 그 문장을 ``limit``자에서 자른다."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    out = ""
    for s in (p for p in _SENTENCE_END.split(t) if p and p.strip()):
        cand = (out + " " + s.strip()).strip() if out else s.strip()
        if len(cand) > limit:
            break
        out = cand
    return out or (t[:limit].rstrip() + "…")


def _note_sentence(n: Any) -> str:
    where = " ".join(str(getattr(n, "where", "") or "").split())
    change = " ".join(str(getattr(n, "change", "") or "").split())
    if not change:
        return ""
    if not where or where.lower() in change.lower():
        return change
    return f"{where}: {change}"


def _kept_tries(report: Any) -> list[tuple[str, Any]]:
    """오류 목록의 시도 가운데 최종 결과에 남은 것을 (케이스, 시도)로 한 번씩 반환한다."""
    out: list[tuple[str, Any]] = []
    seen: set[Any] = set()
    for e in entries_of(report):
        cid = str(getattr(e, "case_id", "") or getattr(e, "id", "") or "")
        for t in getattr(e, "tries", None) or []:
            key = (cid, getattr(t, "round", 0), getattr(t, "n", 0), getattr(t, "commit", ""))
            if key in seen or id(t) in seen:
                continue
            seen.add(key)
            seen.add(id(t))
            if kept(t):
                out.append((cid, t))
    return out


def _try_files(t: Any) -> set[str]:
    files = {_norm(f) for f in getattr(t, "files", None) or [] if f}
    files |= {_norm(f) for f in diff_stats(str(getattr(t, "diff", "") or "")) if f and f != "?"}
    return files


def _notes_and_reasons(report: Any, clean: Any) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    notes: dict[str, list[str]] = {}
    reasons: dict[str, list[str]] = {}
    case_reason: dict[tuple[str, str], str] = {}        # (경로, 케이스)마다 최종 결과에 남은 마지막 시도의 원인 분석
    for cid, t in _kept_tries(report):
        for n in getattr(t, "edits", None) or []:
            if getattr(n, "source", "llm") != "llm":
                continue
            s = clean(_note_sentence(n))
            p = _norm(getattr(n, "path", ""))
            if s and p and s not in notes.setdefault(p, []):
                notes[p].append(s)
        analysis = str(getattr(t, "analysis", "") or "").strip()
        if analysis:
            for p in _try_files(t):
                case_reason[(p, cid)] = analysis
    for (p, _cid), text in case_reason.items():
        r = first_sentences(clean(text))
        if r and r not in reasons.setdefault(p, []):
            reasons[p].append(r)
    return notes, reasons


def _cleaner(ws: Any, cfg: Any) -> Any:
    try:
        from sbmig.errors.common import Cleaner
        return Cleaner(cfg, (ws.root, ws.run_dir))
    except Exception as e:  # noqa: BLE001 - 마스킹할 수 없으면 원문을 남기지 않고 아무것도 저장하지 않는다
        log.warning("code changes: text cleaner unavailable: %s", e)
        return None


def collect_code_changes(ws: Any, report: Any, base_sha: Optional[str], cfg: Any = None) -> None:
    """변환 직후 커밋(``base_sha``)과 지금 파일을 비교해서 ``report.code_changes`` / ``code_changes_total`` /
    ``code_changes_base``를 채운다. base가 없으면 빈 목록으로 둔다. git이 실패하면 WARNING 한 줄을 남긴다.
    예외는 던지지 않는다.

    Statuses come from `git diff --no-renames`: a moved file is the old path `deleted` + the new path `added`."""
    report.code_changes = []
    report.code_changes_total = 0
    report.code_changes_base = ""
    if not base_sha:
        return
    try:
        rows = ws.numstat_since(base_sha)
    except Exception as e:  # noqa: BLE001 - 목록 없이 보고서를 만든다
        log.warning("code changes not collected (git diff from %s failed): %s", str(base_sha)[:10], e)
        return
    clean = _cleaner(ws, cfg)
    if clean is None:
        return
    # 줄이 하나도 바뀌지 않은 수정(실행 권한만 바뀐 파일)은 코드 수정이 아니므로 뺀다
    files = sorted(((_norm(p), st, a, r) for p, st, a, r in rows
                    if _norm(p) and a is not None and r is not None and not _excluded(ws, _norm(p))
                    and not (_STATUS.get(st, "modified") == "modified" and not int(a or 0) and not int(r or 0))),
                   key=lambda x: _order_key(x[0]))
    try:
        notes, reasons = _notes_and_reasons(report, clean)
    except Exception as e:  # noqa: BLE001 - 설명이 없어도 목록은 쓸모가 있다
        log.warning("code changes: notes / reasons not read: %s", e)
        notes, reasons = {}, {}
    out: list[CodeChange] = []
    for i, (path, st, added, removed) in enumerate(files[:MAX_FILES]):
        diff, truncated = "", False
        if i < DIFF_FILES:
            try:
                lines = hunks_only(_decode(ws, path, ws.diff_file_bytes(base_sha, path)))
                if len(lines) > DIFF_LINES:
                    lines, truncated = lines[:DIFF_LINES], True
                diff = clean("\n".join(lines)) if lines else ""
            except Exception as e:  # noqa: BLE001 - 이 파일만 diff 없이 넣는다
                log.debug("code changes: diff of %s not read: %s", path, e)
                diff, truncated = "", False
        out.append(CodeChange(path=clean(path) or path, status=_STATUS.get(st, "modified"),  # type: ignore[arg-type]
                              added=int(added or 0), removed=int(removed or 0),
                              notes=list(notes.get(path, [])), reasons=list(reasons.get(path, [])),
                              diff=diff, truncated=truncated))
    report.code_changes = out
    report.code_changes_total = len(files)
    report.code_changes_base = str(base_sha)

