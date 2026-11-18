"""자동 변환 뒤에 sbmig가 고친 파일을 보고서의 "코드 수정 내역"용으로 모으는 모듈.
변환 직후 커밋과 최종 작업 트리를 git으로 비교해 파일마다 ``CodeChange`` 하나를 만든다.
- 여러 시도가 같은 파일을 고쳐도 한 건이고, 되돌린 변경은 나오지 않는다
- 빌드 산출물, 바이너리, .git 아래 파일은 넣지 않는다
- 텍스트는 errors.md와 같은 방식으로 마스킹한다
- git이 실패해도 예외를 던지지 않고 빈 목록으로 둔다

The net change of every file between the commit right after the automatic conversion (OpenRewrite, restructure)
and the final tree, one `CodeChange` per file, for the Korean report's "코드 수정 내역" (`RunReport.code_changes`).
Notes come from the kept tries' LLM-written `EditNote`s, reasons from the cases' `analysis`."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sbmig.core.models import CodeChange
from sbmig.report.errors import diff_stats, entries_of, kept

log = logging.getLogger(__name__)

DIFF_FILES = 30            # diff text kept for the first N files of the order
DIFF_LINES = 80            # diff lines kept per file (truncated beyond)
MAX_FILES = 500            # files listed at most (code_changes_total counts all)
REASON_CHARS = 300         # a reason is cut at a sentence boundary near this length

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
    except Exception:  # noqa: BLE001 - an odd path is simply listed
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
    """unified diff에서 파일 헤더(diff --git, index, ---, +++)를 빼고 @@ 줄부터의 내용만 반환한다.

    The lines of a one-file unified diff from the first `@@` on (git headers dropped)."""
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
    첫 문장이 이미 길면 그 문장을 ``limit``자에서 자른다.

    The first sentence(s) of `text` within about `limit` characters, cut at a sentence boundary."""
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
    """오류 목록의 시도 중 트리에 남은 것을 (케이스, 시도)로 한 번씩 반환한다."""
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
    case_reason: dict[tuple[str, str], str] = {}        # (path, case) → the last kept try's analysis
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
    except Exception as e:  # noqa: BLE001 - no cleaner: nothing is stored rather than unmasked text
        log.warning("code changes: text cleaner unavailable: %s", e)
        return None


def collect_code_changes(ws: Any, report: Any, base_sha: Optional[str], cfg: Any = None) -> None:
    """``base_sha``와 최종 작업 트리를 비교해 ``report.code_changes`` / ``code_changes_total`` /
    ``code_changes_base``를 채운다. base가 없으면 빈 목록으로 두고, git이 실패하면 WARNING 한 줄을 남긴다.
    예외를 던지지 않는다.

    Fill `report.code_changes`, `code_changes_total`, `code_changes_base` from the net diff `base_sha` → the final
    working tree. Statuses: `git diff --no-renames`, so a moved file is the old path `deleted` + the new path
    `added`. Never raises: no base or a git failure leaves the list empty (one WARNING for the failure)."""
    report.code_changes = []
    report.code_changes_total = 0
    report.code_changes_base = ""
    if not base_sha:
        return
    try:
        rows = ws.numstat_since(base_sha)
    except Exception as e:  # noqa: BLE001 - the report goes out without the list
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
    except Exception as e:  # noqa: BLE001 - the list without notes is still useful
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
            except Exception as e:  # noqa: BLE001 - this file goes without its diff
                log.debug("code changes: diff of %s not read: %s", path, e)
                diff, truncated = "", False
        out.append(CodeChange(path=clean(path) or path, status=_STATUS.get(st, "modified"),  # type: ignore[arg-type]
                              added=int(added or 0), removed=int(removed or 0),
                              notes=list(notes.get(path, [])), reasons=list(reasons.get(path, [])),
                              diff=diff, truncated=truncated))
    report.code_changes = out
    report.code_changes_total = len(files)
    report.code_changes_base = str(base_sha)

