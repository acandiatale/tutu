"""시도에서 바꾼 내용을 현업이 읽을 수 있게 기록하는 모듈.
수정 기록(``EditNote``)과 변경 조각(``EditHunk``)을 다룬다.
LLM은 ``where`` / ``change``를 쓰고, sbmig는 적용된 diff에서 줄 범위(``lines``)와 변경 조각을 붙인다.
답변 파서, 수정 루프(``sbmig.loop.notes``), ``sbmig.fix.fixer``의 ``merge_notes``,
재색인(``attach_hunks``)이 호출한다.
- LLM이 설명하지 않은 파일도 diff로 기록을 만들어, 설명 없이 diff만 남는 파일이 없게 한다
- 기록마다 그 파일의 모든 변경 조각을 diff 순서로 붙인다. ``where``가 한 곳만 가리켜도 diff 전체를 따른다
- 줄 번호는 추가와 수정이면 새 파일, 삭제이면 이전 파일 기준이다
- 잘린 diff는 읽을 수 있는 데까지 읽고, 이상한 입력에도 예외를 던지지 않는다

The LLM writes `where` / `change` (each `<edits>` item's `desc`, the `<changes>` list for a
script, `EditNote(source="llm")`); sbmig adds `lines` from the applied unified diff and, for every
file the diff touched that the LLM did not describe, a note from the diff itself (`source="diff"`).

* answer_notes(changes, edits): the `<changes>` list first, then each edit's `desc` for a path the
  list did not describe (the fix / review parsers).
* notes_from_diff(diff_text): one note per file; `lines` "+42–47" added, "~12" changed (a run with
  both - and +), "-3" removed; `change` "pom.xml 42–47행 추가 (6줄)" / "Foo.java 12행 수정" /
  "… 삭제"; new / deleted / renamed / binary files.
* merge_notes(llm_notes, diff_text): the LLM's notes with `lines` from the diff (matched by path, a
  short path by its case-insensitive basename when unambiguous), plus the diff's notes for files the
  LLM did not mention; never empty when the diff touched a file.
* hunks_from_diff(diff_text): path -> `EditHunk` per change run (as-is `before` / to-be `after`, the
  run's text as in the diff without `+` / `-`, LF line ends).
* attach_hunks(notes, diff_text): copies of `notes`; a note without hunks gets its file's hunks
  (reindex backfill of older records).

Hunks: every note made or matched here carries ALL the hunks of its file in diff order. `max_lines`
(callers pass `errors.snippet_lines`, 0 = whole) keeps that many lines per side, adds a last line
`… (+N lines)` and sets `truncated`. A new file = one hunk per run with `before == ""`, a deletion
`after == ""`, a binary / mode-only change none.

A diff cut short (`errors.diff_chars`) is read as far as it goes; the run it cut ends with DIFF_CUT_LINE.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from sbmig.core.models import EditHunk, EditNote

MAX_RUNS_SHOWN = 8                  # 파일당 `lines` / `change`에 보여 줄 변경 구간 수, 넘으면 "… +N"
WHERE_CHARS = 300
CHANGE_CHARS = 600
DIFF_CUT_LINE = "… (diff cut: errors.diff_chars)"   # diff가 중간에 잘린 변경 조각의 마지막 줄

_HUNK_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER_RE = re.compile(r'^diff --git (?:"?a/)(.*?)"? (?:"?b/)(.*?)"?$')
_OCTAL_RE = re.compile(r"\\([0-7]{3})")
_SIMPLE_ESC = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "r": "\r", "v": "\v"}


def describe_language(cfg: Any) -> str:
    """프롬프트에서 요구할 변경 설명(`desc`, `<changes>`)의 언어를 정한다.
    ``report.language``가 ``ko``이면 한국어, 아니면 프로젝트 주석의 언어를 따르고 판단하기 어려우면 한국어로 한다."""
    lang = str(getattr(getattr(cfg, "report", None), "language", "") or "").lower()
    if lang == "ko":
        return "Korean (한국어)"
    return ("the language of the project's code comments — Korean (한국어) when they are Korean or when unsure, "
            "English when they are English")


def one_line(text: Any, limit: int = CHANGE_CHARS) -> str:
    """``text``의 줄바꿈과 연속된 공백을 한 칸으로 줄여 최대 ``limit``자의 한 줄로 만든다."""
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _norm(path: str) -> str:
    p = (path or "").strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def same_path(a: str, b: str) -> bool:
    """두 경로가 같은 파일을 가리키는지 반환한다.
    대소문자와 구분자를 무시하고, 폴더를 포함한 짧은 경로가 긴 경로의 뒷부분과 같아도 같은 파일로 본다.
    폴더 없이 ``pom.xml``만 있으면 어느 파일인지 모호하므로 뒷부분 일치로 보지 않는다.

    `comp-jar/pom.xml` matches `module/comp-jar/pom.xml`; a bare `pom.xml` does not."""
    x, y = _norm(a).lower(), _norm(b).lower()
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) < len(y) else (y, x)
    return "/" in short and long_.endswith("/" + short)


def _base(path: str) -> str:
    return posixpath.basename(_norm(path)).lower()


# ---------------------------------------------------------------------------
# 답변 하나의 LLM 수정 기록
# ---------------------------------------------------------------------------
def answer_notes(changes: Iterable[EditNote], edits: Iterable[Any]) -> list[EditNote]:
    """답변 하나에서 LLM이 쓴 수정 기록을 모은다.
    ``<changes>`` 목록을 먼저 두고, 목록에 없는 경로의 편집은 ``desc``로 기록을 하나씩 더한다. 순서는 유지한다."""
    out = [x for x in (_as_note(n) for n in changes or ()) if x is not None]
    edits = list(edits or ())
    described = [n.path for n in out if n.path]
    edit_bases = [_base(str(getattr(e, "path", "") or "")) for e in edits]
    for e in edits:
        desc = one_line(getattr(e, "desc", "") or "")
        path = str(getattr(e, "path", "") or "")
        if not desc:
            continue
        if any(same_path(path, d) or ("/" not in _norm(d) and _base(d) == _base(path)
                                      and edit_bases.count(_base(path)) == 1) for d in described):
            continue
        if any(n.path == path and n.change == desc for n in out):
            continue                                       # 같은 편집이 두 번 온 경우
        out.append(EditNote(path=path, change=desc, source="llm"))
    return out


_PATH_KEYS = ("path", "file", "filename", "pom")
_WHERE_KEYS = ("where", "location", "position", "place", "section", "at", "line", "lines")
_CHANGE_KEYS = ("change", "what", "desc", "description", "summary", "how", "modification", "action", "edit", "note")
_LINE_ITEM_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])?\s*`?(?P<path>[^\s`:]*[/.][^\s`:]*?)`?\s*"
                           r"(?P<sep>:|：|—|–|->|→|\s-\s|\s)\s*(?P<rest>\S.*)$")
MAX_NOTES = 60


def _pick(item: dict, keys: Sequence[str]) -> str:
    for k in keys:
        v = item.get(k)
        if v is None or isinstance(v, dict):
            continue
        if isinstance(v, list):
            v = "; ".join(str(x) for x in v if x is not None and not isinstance(x, (dict, list)))
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and k in ("line", "lines"):
            v = f"{v}행 부근"
        text = one_line(v)
        if text:
            return text
    return ""


def _clean_path(raw: str) -> str:
    """기록의 경로를 편집 경로처럼 정리한다. `./`, `\\`, 따옴표, 끝의 "(new file)" 같은 괄호를 뺀다."""
    p = one_line(raw, 400).strip("`'\"* ")
    p = re.sub(r"\s+\(.*\)$", "", p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def note_from_text(text: str) -> Optional[EditNote]:
    """JSON 대신 글머리표로 온 한 줄을 수정 기록 하나로 만든다.
    경로가 문장의 주어로만 쓰였으면 문장 전체를 설명으로 둔다.

    Forms: "path: what", "path — what", "path 의 … 수정합니다"."""
    line = one_line(text)
    if not line:
        return None
    m = _LINE_ITEM_RE.match(line)
    if m and re.search(r"\.[A-Za-z0-9]{1,10}$|/", m.group("path")):
        sep = m.group("sep").strip()
        change = m.group("rest") if sep else line.lstrip("-*\u2022 ").strip()
        return EditNote(path=_clean_path(m.group("path")), change=one_line(change), source="llm")
    return EditNote(path="", change=line.lstrip("-*\u2022 ").strip(), source="llm")


def notes_from_json(obj: Any) -> tuple[list[EditNote], list[str]]:
    """파싱한 ``<changes>`` JSON을 (기록 목록, 문제 목록)으로 바꾼다.
    텍스트가 없는 항목은 문제로 기록하고 건너뛴다. 기록은 설명일 뿐이므로 거부하는 항목은 없다.

    Accepted: a list of `{"path", "where", "change"}` (also `file` / `location` / `desc` … spellings), a
    `{"changes": [...]}` wrapper, one object, or plain strings."""
    if isinstance(obj, dict):
        obj = obj["changes"] if isinstance(obj.get("changes"), list) else [obj]
    if obj is None:
        return [], []
    if isinstance(obj, str):
        obj = [ln for ln in obj.splitlines() if ln.strip()]
    if not isinstance(obj, list):
        return [], [(f"<changes> must be a JSON list of {{\"path\", \"where\", \"change\"}} objects, got "
                     f"{type(obj).__name__}")]
    out: list[EditNote] = []
    problems: list[str] = []
    for n, item in enumerate(obj, 1):
        if isinstance(item, str):
            note = note_from_text(item)
            if note is not None:
                out.append(note)
            continue
        if not isinstance(item, dict):
            problems.append(f"change {n}: not an object ({type(item).__name__})")
            continue
        path = _pick(item, _PATH_KEYS)
        where = _pick(item, _WHERE_KEYS)
        change = _pick(item, _CHANGE_KEYS)
        if not where and not change:
            problems.append(f"change {n}: no 'where' / 'change' text")
            continue
        out.append(EditNote(path=_clean_path(path), where=one_line(where, WHERE_CHARS),
                            change=one_line(change), source="llm"))
    if len(out) > MAX_NOTES:
        problems.append(f"{len(out) - MAX_NOTES} change(s) dropped (more than {MAX_NOTES})")
        out = out[:MAX_NOTES]
    return out, problems


# ---------------------------------------------------------------------------
# unified diff로 수정 기록 만들기
# ---------------------------------------------------------------------------
@dataclass
class _FileDiff:
    """diff에 나온 파일 하나의 경로, 상태, 변경 구간, 수정 전후 텍스트를 담는 클래스."""

    path: str = ""                        # 변경 후 경로, 삭제면 이전 경로
    old_path: str = ""
    new_file: bool = False
    deleted: bool = False
    renamed_from: str = ""
    binary: bool = False
    mode_only: bool = False
    runs: list[tuple[str, int, int]] = field(default_factory=list)   # (종류 "+" | "~" | "-", 첫 줄, 줄 수)
    # 변경 구간마다 (삭제된 줄 = as-is, 추가된 줄 = to-be, diff가 구간 중간에서 잘렸는지)
    texts: list[tuple[list[str], list[str], bool]] = field(default_factory=list)
    hunks: int = 0
    added: int = 0
    removed: int = 0

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.path, self.old_path, self.renamed_from) if p]


def _unquote(raw: str) -> str:
    """git이 출력한 경로를 원래 문자열로 복원한다.
    한글 파일 이름처럼 따옴표와 8진 이스케이프로 나온 경로도 처리한다.

    Quoted form from core.quotepath: `"a/\\355\\225\\234.java"`; a plain path may carry a trailing tab."""
    s = raw.rstrip("\t\r\n ")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        try:
            data = bytearray()
            i = 0
            while i < len(body):
                ch = body[i]
                if ch == "\\" and i + 1 < len(body):
                    m = _OCTAL_RE.match(body, i)
                    if m:
                        data.append(int(m.group(1), 8))
                        i += 4
                        continue
                    data += _SIMPLE_ESC.get(body[i + 1], body[i + 1]).encode("utf-8")
                    i += 2
                    continue
                data += ch.encode("utf-8")
                i += 1
            s = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 복원하지 못하면 출력된 그대로 둔다
            s = body
    return s


def _side(raw: str, prefix: str) -> str:
    """diff 헤더 경로에서 `a/` 같은 앞부분을 뺀다. `/dev/null`이면 빈 문자열을 반환한다."""
    s = _unquote(raw)
    if s == "/dev/null":
        return ""
    return s.removeprefix(prefix)


def _parse_diff(diff_text: str) -> list[_FileDiff]:
    """unified diff를 파일과 변경 구간으로 나눈다.
    변경 구간은 헤더에 표시된 줄 수를 다 읽으면 끝나므로 ``-- a/x``로 시작하는 삭제 줄을 파일 헤더로 읽지 않는다.
    줄 수를 다 읽기 전에 헤더가 나오면 잘린 diff로 보고 표시를 남긴다."""
    files: list[_FileDiff] = []
    cur: Optional[_FileDiff] = None
    old_ln = new_ln = old_left = new_left = 0
    run_plus = run_minus = run_start = run_old_start = 0
    run_rm: list[str] = []
    run_add: list[str] = []

    def close_run(cut: bool = False) -> None:
        nonlocal run_plus, run_minus, run_rm, run_add
        if cur is not None and (run_plus or run_minus):
            if run_plus and run_minus:
                cur.runs.append(("~", run_start, run_plus))
            elif run_plus:
                cur.runs.append(("+", run_start, run_plus))
            else:
                cur.runs.append(("-", run_old_start, run_minus))
            cur.texts.append((run_rm, run_add, cut))
        run_plus = run_minus = 0
        run_rm, run_add = [], []

    def new_file() -> _FileDiff:
        nonlocal cur
        close_run()
        cur = _FileDiff()
        files.append(cur)
        return cur

    for line in (diff_text or "").replace("\r\n", "\n").split("\n"):
        cut_short = False
        if cur is not None and (old_left > 0 or new_left > 0):          # inside a hunk
            tag = line[:1]
            if tag == "\\":                                          # "\ No newline at end of file"
                continue
            if tag in (" ", ""):                                     # context (an empty line: trailing space cut)
                close_run()
                old_ln, new_ln = old_ln + 1, new_ln + 1
                old_left, new_left = old_left - 1, new_left - 1
                continue
            if tag in ("+", "-"):
                if not (run_plus or run_minus):
                    run_start, run_old_start = max(new_ln, 1), max(old_ln, 1)
                if tag == "+":
                    run_plus += 1
                    run_add.append(line[1:])
                    new_ln, new_left = new_ln + 1, new_left - 1
                    cur.added += 1
                else:
                    run_minus += 1
                    run_rm.append(line[1:])
                    old_ln, old_left = old_ln + 1, old_left - 1
                    cur.removed += 1
                continue
            old_left = new_left = 0                                  # 줄 수를 다 읽기 전에 헤더가 나왔으므로 잘린 구간이다
            cut_short = True
        close_run(cut_short)
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            f = new_file()
            m = _GIT_HEADER_RE.match(line)
            if m:
                f.old_path, f.path = _unquote(m.group(1)), _unquote(m.group(2))
            continue
        if line.startswith("--- "):
            if cur is None or cur.hunks:                             # git 헤더 없는 diff는 `--- a/x`가 다음 파일의 시작이다
                new_file()
            assert cur is not None
            old = _side(line[4:], "a/")
            if old:
                cur.old_path = old
            else:
                cur.new_file = True
            continue
        if line.startswith("+++ "):
            if cur is None:
                new_file()
            assert cur is not None
            new = _side(line[4:], "b/")
            if new:
                cur.path = new
            else:
                cur.deleted = True
                cur.path = cur.old_path or cur.path
            continue
        m = _HUNK_RE.match(line)
        if m:
            if cur is None:
                new_file()
            assert cur is not None
            cur.hunks += 1
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.new_file = True
        elif line.startswith("deleted file mode"):
            cur.deleted = True
        elif line.startswith("rename from "):
            cur.renamed_from = _unquote(line[len("rename from "):])
        elif line.startswith(("rename to ", "copy to ")):
            cur.path = _unquote(line.split(" ", 2)[2])
        elif line.startswith(("Binary files ", "GIT binary patch")):
            cur.binary = True
        elif line.startswith(("old mode", "new mode")):
            cur.mode_only = True
    close_run(old_left > 0 or new_left > 0)
    for f in files:
        if not f.path:
            f.path = f.old_path or f.renamed_from
        if f.renamed_from and f.old_path in ("", f.path):
            f.old_path = f.renamed_from
        if f.runs or f.binary or f.new_file or f.deleted or f.renamed_from:
            f.mode_only = False
    return [f for f in files if f.path]


def _span(first: int, count: int) -> str:
    return f"{first}" if count <= 1 else f"{first}–{first + count - 1}"


def _run_text(kind: str, first: int, count: int) -> str:
    word = {"+": "추가", "~": "수정", "-": "삭제"}[kind]
    tail = f" ({count}줄)" if count > 1 else ""
    return f"{_span(first, count)}행 {word}{tail}"


def _clip(lines: list[str], max_lines: int) -> tuple[list[str], bool]:
    """줄 목록을 최대 ``max_lines``줄로 자르고, 잘렸으면 남은 줄 수를 알리는 줄을 끝에 붙인다.
    (줄 목록, 잘림 여부)를 반환한다. ``max_lines``가 0이면 자르지 않는다."""
    if max_lines > 0 and len(lines) > max_lines:
        return lines[:max_lines] + [f"… (+{len(lines) - max_lines} lines)"], True
    return list(lines), False


def _limit(max_lines: Any) -> int:
    try:
        return max(int(max_lines or 0), 0)
    except (TypeError, ValueError):
        return 0


def _hunks_of(f: _FileDiff, max_lines: int = 0) -> list[EditHunk]:
    """``f``의 변경 구간마다 ``EditHunk``를 하나씩 diff 순서로 만든다.
    ``max_lines``가 0보다 크면 수정 전후 텍스트를 자르고 ``truncated``로 표시한다.

    `lines` is spelled like the note's (`~12`, `+42–47`, `-3`); `before` / `after` are joined by LF, no
    trailing newline."""
    out: list[EditHunk] = []
    for (kind, first, count), (rm, add, cut) in zip(f.runs, f.texts):
        before, cut_b = _clip(rm, max_lines)
        after, cut_a = _clip(add, max_lines)
        if cut:                                             # diff가 이 구간 중간에서 끊겼다
            (after if add else before).append(DIFF_CUT_LINE)
        out.append(EditHunk(lines=f"{kind}{_span(first, count)}", before="\n".join(before), after="\n".join(after),
                            truncated=cut_b or cut_a))
    return out


def _note_for(f: _FileDiff, max_lines: int = 0) -> EditNote:
    """diff의 파일 하나로 수정 기록(source="diff")을 만든다. 한국어 ``change`` 설명과 변경 조각을 담는다."""
    name = posixpath.basename(f.path) or f.path
    shown = f.runs[:MAX_RUNS_SHOWN]
    more = len(f.runs) - len(shown)
    lines = ", ".join(f"{k}{_span(a, n)}" for k, a, n in shown) + (f", … +{more}" if more else "")
    if f.deleted:
        change = f"{name} 파일 삭제" + (f" ({f.removed}줄)" if f.removed else "")
    elif f.new_file:
        change = f"{name} 새 파일 생성" + (f" ({f.added}줄)" if f.added else "")
    else:
        parts: list[str] = []
        if f.renamed_from:
            parts.append(f"{posixpath.basename(f.renamed_from) or f.renamed_from} → {name} 이동"
                         if posixpath.basename(f.renamed_from) != name else f"{f.renamed_from} → {f.path} 이동")
        if f.binary:
            parts.append(f"{name} 바이너리 파일 변경")
        if shown:
            runs = ", ".join(_run_text(k, a, n) for k, a, n in shown) + (f" 외 {more}곳" if more else "")
            parts.append(f"{name} {runs}" if not f.renamed_from else runs)
        if f.mode_only and not parts:
            parts.append(f"{name} 파일 권한 변경")
        change = ", ".join(parts) or f"{name} 변경"
    return EditNote(path=f.path, where="", change=one_line(change), lines=lines, source="diff",
                    hunks=_hunks_of(f, max_lines))


def notes_from_diff(diff_text: str, *, max_lines: int = 0) -> list[EditNote]:
    """unified diff의 파일마다 변경 조각을 담은 ``EditNote(source="diff")``를 하나씩 만든다.
    diff가 비어 있거나 읽을 수 없으면 빈 목록을 반환한다. ``max_lines``는 한쪽당 줄 수이고 0이면 전체다."""
    try:
        limit = _limit(max_lines)
        return [_note_for(f, limit) for f in _parse_diff(diff_text or "")]
    except Exception:  # noqa: BLE001 - 기록 생성 실패로 시도 기록이 깨지면 안 된다
        return []


def hunks_from_diff(diff_text: str, *, max_lines: int = 0) -> dict[str, list[EditHunk]]:
    """unified diff의 변경 구간별 수정 전후 텍스트를 파일 경로별로 모아 반환한다. 예외를 던지지 않는다.
    바이너리 파일이나 권한만 바뀐 파일은 빈 목록이다. 읽을 수 없는 diff면 빈 dict를 반환한다.

    Key = the path as the notes name it: the "after" path, the old path for a deletion, the new path for a
    rename."""
    out: dict[str, list[EditHunk]] = {}
    try:
        limit = _limit(max_lines)
        for f in _parse_diff(diff_text or ""):
            out.setdefault(f.path, []).extend(_hunks_of(f, limit))
    except Exception:  # noqa: BLE001, S110 - 읽은 데까지는 기록에 남긴다
        pass
    return out


# ---------------------------------------------------------------------------
# LLM 기록과 diff 합치기
# ---------------------------------------------------------------------------
def _match(note_path: str, files: Sequence[_FileDiff]) -> Optional[int]:
    """LLM 기록이 가리키는 diff 파일의 번호를 찾는다.
    정확한 일치, 뒷부분 일치, 파일 이름 순으로 찾고, 각 단계에서 후보가 하나일 때만 고른다.
    대소문자는 구분하지 않는다. 찾지 못하거나 모호하면 None을 반환한다."""
    want = _norm(note_path).lower()
    if not want:
        return None
    for test in (lambda p: _norm(p).lower() == want,
                 lambda p: same_path(p, want),
                 lambda p: _base(p) == posixpath.basename(want)):
        hits = [i for i, f in enumerate(files) if any(test(p) for p in f.paths)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            exact = [i for i in hits if _norm(files[i].path).lower() == want]
            if len(exact) == 1:
                return exact[0]
            return None                                     # 바뀐 pom이 셋인데 "pom.xml"만 쓴 경우처럼 모호하다
    return None


def _as_note(n: Any) -> Optional[EditNote]:
    """EditNote나 dict를 EditNote로 바꾼다. 읽을 수 없으면 None을 반환한다."""
    if isinstance(n, EditNote):
        return n
    if isinstance(n, dict):
        try:
            return EditNote.model_validate(n)
        except Exception:  # noqa: BLE001
            return None
    return None


def _copies(hunks: Sequence[EditHunk]) -> list[EditHunk]:
    return [h.model_copy() for h in hunks]


def merge_notes(llm_notes: Iterable[EditNote], diff_text: str, *, max_lines: int = 0) -> list[EditNote]:
    """시도 하나의 수정 기록을 만든다. LLM 기록의 복사본에 diff의 ``lines``와 그 파일의 변경 조각을 붙이고,
    어느 기록에도 없는 파일은 diff로 만든 기록을 뒤에 더한다.
    diff에 바뀐 파일이 있으면 결과는 비어 있지 않다. diff가 없으면 LLM 기록을 그대로 반환한다.

    A short path matched by its basename takes the diff's full path; two notes on one file both get its hunks.
    `max_lines` as in `notes_from_diff`."""
    llm = [x for x in (_as_note(n) for n in llm_notes or ()) if x is not None]
    try:
        files = _parse_diff(diff_text or "")
    except Exception:  # noqa: BLE001
        files = []
    limit = _limit(max_lines)
    diff_notes: dict[int, EditNote] = {}

    def diff_note(i: int) -> EditNote:
        if i not in diff_notes:
            diff_notes[i] = _note_for(files[i], limit)
        return diff_notes[i]

    out: list[EditNote] = []
    seen: set[tuple[str, str, str]] = set()
    claimed: set[int] = set()
    for n in llm:
        key = (_norm(n.path).lower(), one_line(n.where), one_line(n.change))
        if key in seen:
            continue
        seen.add(key)
        i = _match(n.path, files) if files else None
        if i is None:
            out.append(n.model_copy())
            continue
        claimed.add(i)
        d = diff_note(i)
        out.append(n.model_copy(update={"path": files[i].path, "lines": d.lines or n.lines,
                                        "hunks": _copies(d.hunks) or _copies(n.hunks)}))
    for i, _f in enumerate(files):
        if i not in claimed:
            out.append(diff_note(i))
    return out


def attach_hunks(notes: Iterable[Any], diff_text: str, *, max_lines: int = 0) -> list[EditNote]:
    """변경 조각이 없는 이전 기록을 재색인할 때 ``diff_text``에서 그 파일의 조각을 찾아 붙인다.
    파일을 찾는 방식은 ``merge_notes``와 같다.
    조각이 이미 있는 기록, diff에 없는 파일, 기록이 아닌 항목은 그대로 둔다. 예외를 던지지 않는다.
    diff가 비었거나 읽을 수 없으면 입력을 그대로 반환한다."""
    items = list(notes or ())
    try:
        files = _parse_diff(diff_text) if diff_text else []
    except Exception:  # noqa: BLE001
        files = []
    if not files:
        return items
    out: list[Any] = []
    try:
        limit = _limit(max_lines)
        cache: dict[int, list[EditHunk]] = {}
        for x in items:
            n = _as_note(x)
            if n is None:
                out.append(x)
                continue
            i = _match(n.path, files) if not n.hunks else None
            if i is None:
                out.append(n.model_copy())
                continue
            if i not in cache:
                cache[i] = _hunks_of(files[i], limit)
            out.append(n.model_copy(update={"hunks": _copies(cache[i])}))
        return out
    except Exception:  # noqa: BLE001 - 보충하다 실패해도 기록은 잃지 않는다
        return items


__all__ = ["answer_notes", "attach_hunks", "describe_language", "hunks_from_diff", "merge_notes", "note_from_text",
           "notes_from_diff", "notes_from_json", "one_line", "same_path"]
