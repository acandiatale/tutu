"""실행의 오류 목록(``RunReport.error_entries``, v2.1)을 보고서가 쓰기 좋은 형태로 가공하는 순수 데이터 모듈.
흐름상 위치: 보고서 단계에서 markdown.py(report.md 의 Error list · Fixes by case · Structure review)와
writer.py(콘솔 요약), result_line.py(``SBMIG-RESULT`` 의 open / resolved 개수)가 부른다. 핵심 진입점:
``status_counts`` / ``seen_groups``(단계별 묶음) / ``tries_text``(시도 칸) / ``diff_stats``(파일별 +/-) /
``case_rows``(케이스별 한 줄 — 결정적 시도의 원인·해결·수정 기록) / ``review_entries``(구조 검토 몫) /
``edits_label``(v2.1i ``fix.mode: edits`` 시도의 꼬리표 ``edits:<case>#<n>``).
렌더링·번역은 여기서 하지 않는다. 빈 목록과 필드가 빠진
항목도 예외 없이 받아야 한다(옛 KB 레코드, 플러그인 결과).

The run's error list (`RunReport.error_entries`, v2.1) as the report needs it: counts, `seen` groups, the tries cell,
diff +/- per file and one row per case ("Fixes by case", with the kept tries' 수정 notes — v2.1b `FixTry.edits`).
Pure data — rendering (and translation) is in markdown.py / writer.py; everything here tolerates an empty list and
entries with missing fields. v2.1i: in an edits-mode run (`RunReport.fix_mode == "edits"`) an `llm-edit` try reads
`edits:<case>#<n> <result>` (`edits_label`) instead of `<n> llm-edit edits <result>`; script-mode text is unchanged."""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sbmig.errors.common import note_text

STATUS_ORDER = ("unresolved", "open", "blocked", "pom-verify", "resolved", "note")
OPEN_STATUSES = ("open", "unresolved")         # SBMIG-RESULT `open=` (finalize turns open into unresolved)
SEEN_ORDER = ("compile", "restructure", "review", "build")
KEPT_RESULTS_EXCLUDED = ("regressed", "reverted", "apply_failed")   # rolled back / never applied: not in the tree
_FIX_ROUND_RE = re.compile(r"^fix r(\d+)\b")
_RESOLVED_TRY_RE = re.compile(r"^r(\d+) try (\d+)$")
_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
EDITS_FIX_MODE = "edits"                       # v2.1i `fix.mode` without fix scripts (RunReport.fix_mode)
EDITS_SOURCE = "llm-edit"                      # the source of every fix-loop try in that mode


def edits_label(case_id: str, n: Any) -> str:
    """v2.1i edits 모드 시도의 꼬리표 ``edits:<case>#<n>``(n = 이 실행에서 케이스의 시도 번호). script 모드의
    ``script:<case>:v<N>`` 자리에 쓴다 — 저장 버전이 없으니 시도 번호로 가리킨다(errors.md 의 그 시도).

    `edits:<case>#<n>` — an edits-mode try (n = the case's try number in this run, `FixTry.n`); where script mode
    names a stored version (`script:<case>:v<N>`), edits mode names the try (errors.md has it with its diff)."""
    return f"edits:{case_id or '?'}#{n or '?'}"


def is_edits_try(t: Any, fix_mode: str = "script") -> bool:
    """edits 모드 실행의 수정 루프 시도인가(출처 ``llm-edit``) — 구조 검토 시도(``review``)는 아니다.

    A fix-loop try of an edits-mode run (source `llm-edit`); a structure-review try (`review`) is not one."""
    return fix_mode == EDITS_FIX_MODE and str(getattr(t, "source", "") or "") == EDITS_SOURCE


def entries_of(report: Any) -> list[Any]:
    """보고서의 오류 목록(``error_entries``)을 리스트로. 없으면 빈 리스트."""
    return list(getattr(report, "error_entries", None) or [])


def status_counts(entries: Iterable[Any]) -> "OrderedDict[str, int]":
    """상태별 개수를 ``STATUS_ORDER`` 순서로(모르는 상태는 그 뒤에 알파벳순), 0 인 것은 뺀다.

    status → count in `STATUS_ORDER` (unknown statuses after it, alphabetically); zero counts left out."""
    c = Counter(str(getattr(e, "status", "") or "open") for e in entries)
    keys = [s for s in STATUS_ORDER if c.get(s)] + sorted(k for k in c if k not in STATUS_ORDER and c[k])
    return OrderedDict((k, c[k]) for k in keys)


def open_count(entries: Iterable[Any]) -> int:
    """아직 열린 오류(open + unresolved) 개수 — ``SBMIG-RESULT`` 의 ``open=``."""
    return sum(1 for e in entries if getattr(e, "status", "") in OPEN_STATUSES)


def resolved_count(entries: Iterable[Any]) -> int:
    """해결된(resolved) 오류 개수 — ``SBMIG-RESULT`` 의 ``resolved=``."""
    return sum(1 for e in entries if getattr(e, "status", "") == "resolved")


def counts_text(counts: "OrderedDict[str, int] | dict[str, int]") -> str:
    """``"resolved 23, unresolved 2"`` 형태의 개수 문장.
    상태 이름은 식별자라 어느 언어 보고서에서도 영어 그대로 둔다.

    "resolved 23, unresolved 2" (ASCII; the statuses are identifiers and stay English in every language)."""
    return ", ".join(f"{k} {v}" for k, v in counts.items() if v)


def seen_rank(seen: str) -> tuple[int, int, str]:
    """``seen``(오류가 처음 나온 단계)의 정렬 키:
    compile → restructure → review → build → fix r1, r2 … → 그 밖.
    ``fix rN`` 은 라운드 번호를 숫자로 비교한다(문자열로 비교하면 r10 이 r2 앞에 온다)."""
    s = (seen or "").strip()
    if s in SEEN_ORDER:
        return (SEEN_ORDER.index(s), 0, "")
    m = _FIX_ROUND_RE.match(s)
    if m:
        return (len(SEEN_ORDER), int(m.group(1)), s)
    return (len(SEEN_ORDER) + 1, 0, s)


def ordered(entries: Iterable[Any]) -> list[Any]:
    """오류들을 ``seen`` 의 파이프라인 순서로 정렬한다. 같은 단계 안에서는 원래 목록 순서를 지킨다(안정 정렬).

    The entries in pipeline order of `seen` (compile → restructure → review → build → fix r1, r2, …, then anything
    else); list order inside a group."""
    es = list(entries)
    return [e for _, e in sorted(enumerate(es), key=lambda ie: (seen_rank(str(getattr(ie[1], "seen", "") or "-")),
                                                                 ie[0]))]


def seen_groups(entries: Iterable[Any]) -> "OrderedDict[str, list[tuple[int, Any]]]":
    """``seen`` 단계별 묶음 ``{seen: [(번호, 오류)]}``. 번호는 정렬 순서대로 1부터 매겨
    보고서의 오류 표를 위에서 아래로 읽게 한다(Error list 의 # 칸).

    `seen` → [(#, entry)] in pipeline order (see `ordered`); # numbers the entries 1… in that order, so the
    report's error list reads top to bottom."""
    groups: "OrderedDict[str, list[tuple[int, Any]]]" = OrderedDict()
    for i, e in enumerate(ordered(entries), 1):
        groups.setdefault(str(getattr(e, "seen", "") or "-"), []).append((i, e))
    return groups


def try_text(t: Any, case_id: str = "", fix_mode: str = "script") -> str:
    """시도 하나를 한 칸 문장으로: ``번호 출처 [v버전] [edits] 결과`` (예 ``2 kb-script v2 build_ok``). v2.1i edits
    모드의 ``llm-edit`` 시도는 ``edits:<case>#<n> 결과``.

    `1 llm-new edits improved`, `2 kb-script v2 build_ok`; an edits-mode run's `llm-edit` try (v2.1i, `fix_mode`
    "edits", `case_id` = the entry's case) → `edits:<case>#1 improved`."""
    if is_edits_try(t, fix_mode):
        return f"{edits_label(case_id, getattr(t, 'n', 0))} {getattr(t, 'result', '') or 'pending'}"
    parts = [str(getattr(t, "n", "") or "?"), str(getattr(t, "source", "") or "?")]
    if getattr(t, "script_version", 0):
        parts.append(f"v{t.script_version}")
    if getattr(t, "applied", "none") == "edits":
        parts.append("edits")
    parts.append(str(getattr(t, "result", "") or "pending"))
    return " ".join(parts)


def tries_text(tries: Iterable[Any], case_id: str = "", fix_mode: str = "script") -> str:
    """오류 하나의 시도들을 `` · `` 로 이은 문장(오류 표의 tries 칸). edits 모드면 시도마다 ``edits:<case>#<n>``."""
    return " · ".join(try_text(t, case_id, fix_mode) for t in tries)


def deciding_label(row: Any, fix_mode: str = "script") -> str:
    """v2.1i edits 모드의 "Fixes by case" 스크립트 칸 대신: 케이스의 결정적 시도 꼬리표 ``edits:<case>#<n>``
    (그 시도가 ``llm-edit`` 일 때만, 아니면 "").

    The deciding try of a case row as `edits:<case>#<n>` (edits mode, an `llm-edit` try), else ""."""
    d = getattr(row, "deciding", None)
    return edits_label(row.case_id, getattr(d, "n", 0)) if d is not None and is_edits_try(d, fix_mode) else ""


def where(e: Any) -> str:
    """오류 위치: ``파일:줄``, 파일이 없으면 모듈, 둘 다 없으면 ""
    (pom-verify · note 레코드는 위치가 없을 수 있다).

    `file:line`, else the module, else "" (pom-verify / note entries may have neither)."""
    f = getattr(e, "file", None)
    if f:
        line = getattr(e, "line", None)
        return f"{f}:{line}" if line else str(f)
    return str(getattr(e, "module", None) or "")


def entry_text(e: Any) -> str:
    """오류 내용: 메시지, 없으면 note(pom-verify 는 재구성이 한 일, note 레코드는 구조 검토 경고).

    The message, else the note (pom-verify: what the restructure did; note entries: the warning)."""
    return str(getattr(e, "message", "") or getattr(e, "note", "") or "")


# ---------------------------------------------------------------------------- diffs
def diff_stats(diff: str, files: Iterable[str] = ()) -> "OrderedDict[str, list[int]]":
    """unified diff 에서 파일별 [추가, 삭제] 줄 수를 센다. 파일 머리가 없는 diff 는 ``files`` 에
    파일이 하나뿐일 때만 그 파일 몫으로 친다. ``files`` 에는 있는데 diff 에 안 보이는 파일은 [0, 0] —
    바뀌었지만 diff 가 6000자에서 잘렸을 뿐이다.

    file → [added, removed] lines of a unified diff (`git diff` headers or bare `---` / `+++` pairs). A diff
    without file headers is credited to the only file of `files`; listed files the diff does not mention get
    [0, 0] (they changed, the capped diff just does not show it)."""
    out: "OrderedDict[str, list[int]]" = OrderedDict()
    cur: Optional[str] = None
    in_hunk = False
    for line in (diff or "").splitlines():
        m = _GIT_HEADER_RE.match(line)
        if m:
            cur, in_hunk = m.group(2), False
            out.setdefault(cur, [0, 0])
            continue
        if not in_hunk and line.startswith("--- "):
            continue
        if not in_hunk and line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            if p != "/dev/null":
                cur = p.removeprefix("b/")
                out.setdefault(cur, [0, 0])
            continue
        if line.startswith("@@"):
            # 변경 조각 안에서는 "--- " / "+++ " 로 시작하는 줄도 내용(삭제·추가 줄)이므로 머리로 보지 않는다
            in_hunk = True
            if cur is None:
                only = [f for f in files if f]
                cur = only[0] if len(only) == 1 else "?"
                out.setdefault(cur, [0, 0])
            continue
        if not in_hunk or cur is None:
            continue
        if line.startswith("+"):
            out[cur][0] += 1
        elif line.startswith("-"):
            out[cur][1] += 1
    for f in files or ():
        if f and f not in out:
            out[f] = [0, 0]
    if out.get("?") == [0, 0]:
        del out["?"]
    return out


def merge_stats(stats: Iterable["OrderedDict[str, list[int]]"]) -> "OrderedDict[str, list[int]]":
    """여러 ``diff_stats`` 결과를 파일별로 더한다(케이스의 여러 시도 → 한 줄)."""
    out: "OrderedDict[str, list[int]]" = OrderedDict()
    for s in stats:
        for f, (a, r) in s.items():
            cur = out.setdefault(f, [0, 0])
            cur[0] += a
            cur[1] += r
    return out


def stats_text(stats: "OrderedDict[str, list[int]]", limit: int = 6, code: bool = True, minus: str = "−") -> str:
    """파일별 +/- 를 한 문장으로(앞 ``limit`` 개, 나머지는 "(+N more)").
    콘솔은 ``code=False, minus="-"`` 로 부른다 — 회사 Windows 콘솔(cp949)은 유니코드 마이너스를 못 찍는다.

    "`pom.xml` (+6 −0), `A.java` (+1 −1) (+2 more)"; a file whose capped diff shows nothing has no counts.
    The console passes `code=False, minus="-"` (cp949 consoles)."""
    q = "`" if code else ""
    items = []
    for f, (a, r) in list(stats.items())[:limit]:
        items.append(f"{q}{f}{q}" + (f" (+{a} {minus}{r})" if a or r else ""))
    if len(stats) > limit:
        items.append(f"(+{len(stats) - limit} more)")
    return ", ".join(items)


def kept(t: Any) -> bool:
    """이 시도의 변경이 지금 작업 트리에 남아 있는가(실제로 적용됐고 되돌려지지 않음).
    되돌린 시도의 diff 는 셈하지 않는다.

    The try's changes are still in the tree (not rolled back, actually applied)."""
    return getattr(t, "applied", "none") != "none" and getattr(t, "result", "") not in KEPT_RESULTS_EXCLUDED


# ---------------------------------------------------------------------------- fixes by case
@dataclass
class CaseRow:
    """"Fixes by case" 표의 한 줄 = 케이스 하나: 오류 수·해결 수, 시도들, 결정적 시도의
    원인·해결 방법, 남은 변경과 수정 기록, 해결한(또는 가장 최근) 저장 스크립트 경로,
    메이저 업그레이드 수(v2.1d)."""
    case_id: str
    name: str = ""                                 # the first entry's message
    entries: int = 0
    resolved: int = 0
    open: int = 0
    tries: list[Any] = field(default_factory=list)   # distinct tries of the case (first seen order)
    analysis: str = ""                             # 원인 (the deciding try's)
    solution: str = ""                             # 해결 방법
    no_script_reason: str = ""
    changes: "OrderedDict[str, list[int]]" = field(default_factory=OrderedDict)
    edits: list[Any] = field(default_factory=list)   # 수정: the kept tries' EditNotes (path / where / change / lines)
    script: str = ""                               # cases/<id>/fix_vN.py (resolving script, else the newest stored)
    script_version: int = 0
    seen: str = ""
    major_upgrades: int = 0                        # v2.1d: major upgrades / replacements of the deciding try
    deciding: Any = None                           # v2.1i: the deciding try (an edits-mode row names it edits:<case>#<n>)


def _try_key(t: Any) -> tuple:
    return (getattr(t, "round", 0), getattr(t, "n", 0), getattr(t, "source", ""), getattr(t, "script_version", 0),
            getattr(t, "commit", ""))


def _deciding_try(tries: list[Any], resolved_marks: list[str]) -> Optional[Any]:
    """케이스의 "결정적" 시도: 오류의 ``resolved`` 표시(``r3 try 1``)가 가리키는 시도가 먼저,
    없으면 원인·해결 글이 있는 마지막 시도, 그것도 없으면 마지막 시도.
    보고서의 원인 / 해결 방법 칸이 이 시도에서 온다."""
    for mark in resolved_marks:
        m = _RESOLVED_TRY_RE.match(mark or "")
        if m:
            rnd, n = int(m.group(1)), int(m.group(2))
            hit = next((t for t in tries if getattr(t, "round", 0) == rnd and getattr(t, "n", 0) == n), None)
            if hit is not None:
                return hit
    with_text = [t for t in tries if getattr(t, "analysis", "") or getattr(t, "solution", "")]
    return with_text[-1] if with_text else (tries[-1] if tries else None)


def case_rows(entries: Iterable[Any]) -> list[CaseRow]:
    """시도가 있었거나 해결된 케이스마다 한 줄(오류 목록 순서). 한 시도는 케이스의 모든 오류
    레코드에 똑같이 적히므로 여기서 중복을 뺀다. 변경 통계·수정 기록은 트리에 남은(``kept``) 시도만 센다.

    One row per case that was worked on (has tries) or resolved, in the error list's order (`ordered`). Tries are
    shared by the case's entries (one try fixes the whole case) and de-duplicated."""
    rows: "OrderedDict[str, CaseRow]" = OrderedDict()
    marks: dict[str, list[str]] = {}
    scripts: dict[str, list[str]] = {}
    for e in ordered(entries):
        tries = list(getattr(e, "tries", None) or [])
        status = getattr(e, "status", "")
        cid = str(getattr(e, "case_id", "") or "")
        # 케이스 없는 레코드(pom-verify, note)와 손대지 않은 열린 오류는 "Fixes by case" 에 넣지 않는다
        if not cid or (not tries and status != "resolved"):
            continue
        row = rows.get(cid)
        if row is None:
            row = rows[cid] = CaseRow(case_id=cid, name=entry_text(e), seen=str(getattr(e, "seen", "") or ""))
        row.entries += 1
        row.resolved += status == "resolved"
        row.open += status in OPEN_STATUSES
        have = {_try_key(t) for t in row.tries}
        row.tries += [t for t in tries if _try_key(t) not in have]
        if getattr(e, "resolved", ""):
            marks.setdefault(cid, []).append(e.resolved)
        if getattr(e, "script", ""):
            scripts.setdefault(cid, []).append(e.script)
    for cid, row in rows.items():
        row.tries.sort(key=lambda t: (getattr(t, "round", 0), getattr(t, "n", 0)))
        d = _deciding_try(row.tries, marks.get(cid, []))
        row.deciding = d
        if d is not None:
            row.analysis = getattr(d, "analysis", "") or ""
            row.solution = getattr(d, "solution", "") or ""
            row.no_script_reason = getattr(d, "no_script_reason", "") or ""
            row.major_upgrades = len(getattr(d, "major_upgrades", None) or []) if kept(d) else 0
        if not row.no_script_reason:
            row.no_script_reason = next((t.no_script_reason for t in reversed(row.tries)
                                         if getattr(t, "no_script_reason", "")), "")
        row.changes = merge_stats(diff_stats(getattr(t, "diff", "") or "", getattr(t, "files", None) or [])
                                  for t in row.tries if kept(t))
        row.edits = kept_notes(row.tries)
        # 스크립트 경로: 해결한 저장 스크립트(오류의 script 필드) 우선, 없으면 시도에 쓰인 가장 높은 버전
        if scripts.get(cid):
            row.script = scripts[cid][-1]
            m = re.search(r"fix_v(\d+)\.py$", row.script)
            row.script_version = int(m.group(1)) if m else 0
        else:
            versions = [getattr(t, "script_version", 0) for t in row.tries if getattr(t, "script_version", 0)]
            if versions:
                row.script_version = max(versions)
                row.script = f"cases/{cid}/fix_v{row.script_version}.py"
    return list(rows.values())


def kept_notes(tries: Iterable[Any]) -> list[Any]:
    """트리에 남은 시도들의 수정 기록(``FixTry.edits``)을 처음 본 순서로, 같은 (경로, 줄, 내용) 은 한 번만.

    The 수정 notes (`FixTry.edits`) of the tries whose changes are still in the tree, first-seen order, the same
    note (path, lines, change) once."""
    out: list[Any] = []
    seen: set[tuple[str, str, str]] = set()
    for t in tries:
        if not kept(t):
            continue
        for n in getattr(t, "edits", None) or []:
            k = (str(getattr(n, "path", "") or ""), str(getattr(n, "lines", "") or ""),
                 " ".join(str(getattr(n, "change", "") or "").split()))
            if k not in seen:
                seen.add(k)
                out.append(n)
    return out


def notes_text(notes: Iterable[Any], limit: int = 8, sep: str = "; ") -> str:
    """수정 기록을 ``경로 줄 — 내용`` 으로 이어 붙인다(앞 ``limit`` 개, 나머지는 "(+N more)").

    `path lines — change` per note (`sbmig.errors.common.note_text`), `limit` of them, then "(+N more)"."""
    ns = list(notes)
    items = [note_text(n) for n in ns[:limit]]
    if len(ns) > limit:
        items.append(f"(+{len(ns) - limit} more)")
    return sep.join(items)


def review_entries(entries: Iterable[Any]) -> list[Any]:
    """구조 검토가 만들었거나 고친 오류들(``seen=review`` 이거나 ``source=review`` 시도가 있는 것).

    Entries the structure review produced or fixed (seen=review, or a try with source=review)."""
    return [e for e in entries if getattr(e, "seen", "") == "review"
            or any(getattr(t, "source", "") == "review" for t in getattr(e, "tries", None) or [])]


def review_changes(entries: Iterable[Any]) -> "OrderedDict[str, list[int]]":
    """구조 검토 자신의 시도 중 트리에 남은 것의 파일별 [+, −] — 보고서 "Structure review" 의 변경 줄.

    file → [+, −] of the review's own kept tries."""
    seen: set[tuple] = set()
    stats = []
    for e in review_entries(entries):
        for t in getattr(e, "tries", None) or []:
            if getattr(t, "source", "") != "review" or not kept(t) or _try_key(t) in seen:
                continue
            seen.add(_try_key(t))
            stats.append(diff_stats(getattr(t, "diff", "") or "", getattr(t, "files", None) or []))
    return merge_stats(stats)
