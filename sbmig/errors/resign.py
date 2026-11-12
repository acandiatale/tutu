"""KB 재서명(re-sign)의 순수한 부분: 옛 규칙으로 서명된 기록의 L2 를 지금 규칙으로 다시 계산하고, 어느 케이스가
어디로 옮겨 가는지 계획을 세운다. 파일은 건드리지 않는다 — 쓰기는 :meth:`sbmig.errors.cases.CaseStore.resign` 이 KB
잠금 안에서 한다. 왜 필요한가: 케이스 ID 는 L2 문자열의 해시라서 서명 규칙(``sbmig.build.signatures``)이 바뀌면 같은
오류가 다른 ID 를 받고 저장된 케이스·스크립트가 고아가 된다. 그래서 기록마다 자기를 서명한 규칙 버전(``sig``)을
갖고, 규칙 표(``SIGNATURE_CHANGES``)가 그 버전 뒤로 바뀌었다고 말하는 분류의 기록만 다시 계산한다(나머지는 버전
도장만). 다시 계산할 재료는 기록에 남은 사실(``symbol`` / ``location`` / ``artifact`` / ``goal``), 없으면(옛 기록)
``raw`` 의 javac 이어지는 줄(``symbol:`` / ``location:``)이다. 결과가 다른 분류이거나 키가 비면 저장된 L2 를 지킨다.
``signature_for`` 는 프로세스 전역의 reactor 모듈 이름 목록을 읽는다 — 재계산 동안 그것을 비워 두고 끝나면 되돌린다
(실행 중인 프로젝트의 이름에 기대지도, 그것을 망가뜨리지도 않게).

The pure parts of the KB re-sign: recompute the L2 of records signed by older signature rules and plan which case
moves where. No file is touched here — :meth:`sbmig.errors.cases.CaseStore.resign` writes, under the KB lock.

* :func:`rebuild_error` — a ``BuildError`` from an entry's stored facts (``raw`` continuation lines for old entries).
* :func:`resigned_l2` — the current L2 of an entry (only for classes the rules changed since the entry's ``sig``).
* :func:`plan_entries` — every entry's new L2 / stamp, as indexes into the list given.
* :class:`ResignReport` — what a re-sign did (or would do), with :meth:`ResignReport.lines` for the operator.
* :func:`older_line` — a cheap test of one ``errors.jsonl`` line (no JSON parse) for the triggers and the fast path.

The reactor-module set ``signature_for`` reads (``sbmig.build.signatures.reactor_modules``) is saved, emptied and
restored around every recomputation.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Collection, Iterable, Iterator, Mapping, Optional, Union

from sbmig.build import signatures as _sigs
from sbmig.core.models import GAV, BuildError, ErrorEntry

log = logging.getLogger("sbmig.errors")

Signer = Callable[[BuildError], str]

# javac 의 이어지는 줄('  symbol:   method keySet()'). sbmig.build.log_parser._CONT_RE 와 같은 식 — 그 모듈의 비공개
# 이름에 기대지 않으려고 여기 둔다. 저장된 raw 의 줄은 '[ERROR]' 접두어를 달고 있을 수 있어 먼저 뗀다.
# the javac continuation line, the same expression as sbmig.build.log_parser._CONT_RE (mirrored: a private name)
_CONT_RE = re.compile(r"^\s+(?P<key>symbol|location|required|found|reason)\s*:\s*(?P<val>.*)$")
_LEVEL_RE = re.compile(r"^\s*\[(?:ERROR|FATAL|WARNING|WARN|INFO)\]")
# errors.jsonl 한 줄에서 맨 위 'l2' 와 'sig' 값만 정규식으로(JSON 파싱 없이) 읽는다. 문자열 안의 따옴표는 \" 로
# 이스케이프되므로 메시지 속 '"l2":' 글자는 걸리지 않는다; ErrorEntry 의 하위 객체에는 l2 / sig 키가 없다.
# the top-level "l2" (kind, class) and "sig" of one stored line, without a JSON parse
_LINE_L2_RE = re.compile(r'"l2"\s*:\s*"([^"|\\]*)\|([^"|\\]*)')
_LINE_SIG_RE = re.compile(r'"sig"\s*:\s*(\d+)')
_LINE_CASE_RE = re.compile(r'"case_id"\s*:\s*"([^"\\]*)"')
_ANY_CASE_RE = re.compile(rb'"case_id"\s*:\s*"([^"\\]*)"')
ID_HEAD = '{"id":"'
# 정리기(Cleaner)가 자른 텍스트 끝의 표식('…[N more characters]') — 잘린 사실은 없는 것으로 본다
_CUT_RE = re.compile(r"…\[\d+ more characters\]\s*$")
L2_LOG_CHARS = 100


def current_version() -> int:
    """지금 서명 규칙 버전(``sbmig.build.signatures.SIGNATURE_VERSION``; 테스트가 바꿔 끼울 수 있게 매번 읽는다).

    The current signature rule version, read at call time."""
    return int(getattr(_sigs, "SIGNATURE_VERSION", 1) or 1)


def changed_since(version: int) -> frozenset[str]:
    """``version`` 규칙과 지금 규칙 사이에 L2 가 달라질 수 있는 분류들(``classes_changed_since``).

    The error classes whose L2 may differ between rule ``version`` and now."""
    try:
        return frozenset(_sigs.classes_changed_since(int(version or 0)))
    except Exception:  # noqa: BLE001 - a broken table changes nothing
        return frozenset()


def l2_parts(l2: str) -> tuple[str, str, str]:
    """L2 의 (종류, 분류, 키) — ``compile|cannot_find_symbol|keySet`` → ("compile", "cannot_find_symbol", "keySet").
    두 칸짜리 L2 는 키가 "".

    (kind, class, key) of an L2; the key is "" for a two-segment L2."""
    parts = (l2 or "").split("|", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def cut(text: str, limit: int = L2_LOG_CHARS) -> str:
    """로그·보고용으로 자른 L2(``limit`` 자, 넘으면 …).

    An L2 cut for a log line."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def record_text(line: str) -> str:
    """줄에서 레코드 부분: 찢긴 레코드 뒤에 온전한 레코드가 붙은 줄이면 그 뒤쪽(``parse_record`` 와 같은 규칙).

    The record part of a line: the whole record glued after a torn one, else the line (``parse_record``'s rule)."""
    line = line or ""
    i = line.rfind(ID_HEAD, 1)
    return line[i:] if i > 0 else line


def line_case_id(line: str) -> str:
    """줄의 레코드가 가진 맨 위 ``case_id``(정규식; 없으면 "").

    The top-level ``case_id`` of the line's record (regex; "" when none)."""
    m = _LINE_CASE_RE.search(record_text(line))
    return m.group(1) if m else ""


def line_case_ids(line: bytes) -> set[str]:
    """줄에 나오는 모든 ``case_id`` 값(맨 위 + ``resolved_by`` 들; 정규식, 바이트).

    Every ``case_id`` value in a line (top level and ``resolved_by``), from the bytes."""
    return {m.decode("utf-8", "replace") for m in _ANY_CASE_RE.findall(line or b"")}


def older_record(line: str, version: Optional[int] = None,
                 skipped: Collection[str] = ()) -> Optional[ErrorEntry]:
    """``errors.jsonl`` 한 줄이 옛 규칙으로 서명됐고 그 L2 분류가 그 뒤로 바뀐 레코드면 그 항목, 아니면 None. 먼저
    정규식으로 거르고(대부분의 줄은 JSON 파싱 없이 끝난다) 후보만 ``parse_record`` 로 읽는다 — 파싱되지 않는 줄은 옛
    줄이 아니다(찢긴 접두어 뒤에 붙은 온전한 레코드는 읽힌다). 케이스 ID 가 ``skipped``(지난 재서명이 건너뛴 케이스)에
    있으면 None.

    The entry when the line holds a record signed by older rules whose class changed since, else None: a regex test
    first (most lines end there without a JSON parse), then ``parse_record`` for the candidates only — a line that
    does not parse is not an older record (the whole record glued after a torn one is read); a case id in
    ``skipped`` (a case the last re-sign skipped) is None too."""
    text = record_text(line)
    m = _LINE_L2_RE.search(text)
    if not m:
        return None
    s = _LINE_SIG_RE.search(text)
    sig = int(s.group(1)) if s else 0
    v = current_version() if version is None else version
    if sig >= v or m.group(2) not in changed_since(sig):
        return None
    from sbmig.kb.jsonl import parse_record

    e = parse_record(line, ErrorEntry)
    if e is None or not e.l2 or int(e.sig or 0) >= v or l2_parts(e.l2)[1] not in changed_since(e.sig):
        return None
    if e.case_id in skipped:
        return None
    return e


def older_line(line: str, version: Optional[int] = None, skipped: Collection[str] = ()) -> bool:
    """:func:`older_record` 가 항목을 주는가.

    Whether :func:`older_record` finds an older record in the line."""
    return older_record(line, version, skipped) is not None


def _fact(text: str) -> str:
    # 정리기가 자른 사실은 믿지 않는다 — raw 에서 다시 읽는다
    return "" if not text or _CUT_RE.search(text) else text


def continuation(raw: str) -> dict[str, str]:
    """raw 블록의 javac 이어지는 줄들(``symbol`` / ``location`` / …): 키마다 처음 나온 값.

    The javac continuation lines of a raw block, the first value per key."""
    out: dict[str, str] = {}
    for ln in (raw or "").splitlines():
        body = _LEVEL_RE.sub("", ln, count=1)
        m = _CONT_RE.match(body)
        if m and m.group("key") not in out:
            out[m.group("key")] = m.group("val").strip()
    return out


def rebuild_error(entry: ErrorEntry) -> Optional[BuildError]:
    """저장된 항목에서 빌드 오류를 다시 만든다(서명 규칙이 읽는 사실들). 사실이 없는 옛 항목(``sig`` 0)은
    ``raw`` 의 ``symbol:`` / ``location:`` 줄에서 읽는다; ``raw`` 는 그대로 넣어 ``no_suitable_method`` 의 후보 줄을
    규칙이 볼 수 있게 한다. L2 가 없는 항목은 None.

    A ``BuildError`` from an entry's stored facts; an entry without them is read from ``raw`` (``symbol:`` /
    ``location:``); ``raw`` is kept so the ``no_suitable_method`` candidate lines are there. A fact the cleaner cut
    (``…[N more characters]``) counts as absent and is read from ``raw``. None without an L2. Known limit: a
    ``no_suitable_method`` whose library candidate line lies beyond the stored ``raw`` (600 characters) keeps its
    stored L2."""
    if not entry.l2:
        return None
    symbol, location = _fact(entry.symbol), _fact(entry.location)
    cont = continuation(entry.raw) if not (symbol and location) else {}
    artifact = None
    if _fact(entry.artifact):
        try:
            artifact = GAV.parse(entry.artifact)
        except Exception:  # noqa: BLE001 - a hand-edited value: no artifact
            artifact = None
    try:
        return BuildError(kind=entry.kind, message=entry.message or "", file=entry.file, line=entry.line,
                          symbol=symbol or cont.get("symbol") or None,
                          location=location or cont.get("location") or None, module=entry.module,
                          artifact=artifact, goal=_fact(entry.goal) or None, raw=entry.raw or "",
                          signature=_fact(entry.l1) or "",
                          signature_generic=entry.l2)
    except Exception:  # noqa: BLE001 - an odd record keeps its L2
        return None


# 분류마다 규칙이 읽는 사실 — 없으면 다시 계산해도 옛 키로 떨어질 뿐이라 저장된 L2 를 지킨다
# the facts the rule of a class reads; without them the recomputation is not trusted
_NEEDS: dict[str, Callable[[BuildError], bool]] = {
    "cannot_find_symbol": lambda err: bool((err.symbol or "").strip()),
    "no_suitable_method": lambda err: bool((err.raw or "").strip()),
}


@contextmanager
def no_reactor_modules() -> Iterator[None]:
    """재계산 동안 ``signature_for`` 가 읽는 reactor 모듈 이름을 비우고, 끝나면(예외여도) 원래대로 돌린다.

    Empty the process-global reactor-module set for the recomputation and restore it afterwards (also on an
    exception): a re-sign inside a migration neither depends on nor disturbs the running project's names."""
    saved = _sigs.reactor_modules()
    _sigs.set_reactor_modules(())
    try:
        yield
    finally:
        _sigs.set_reactor_modules(saved)


def default_signer(err: BuildError) -> str:
    """지금 규칙의 L2: ``signature_for`` 다음 ``refine_signature``(트리아지가 클러스터를 만들 때와 같은 순서).

    The current rules' L2: ``signature_for`` then ``refine_signature``, the order the triage uses."""
    from sbmig.triage.cluster import refine_signature

    l1, l2 = _sigs.signature_for(err)
    err.signature, err.signature_generic = l1, l2
    refine_signature(err)
    return err.signature_generic or ""


def resigned_l2(entry: ErrorEntry, signer: Optional[Signer] = None) -> str:
    """항목의 지금 L2. 항목의 분류(L2 둘째 칸)가 ``classes_changed_since(entry.sig)`` 에 있을 때만 다시 계산하고,
    다시 만든 오류에 규칙이 읽는 사실이 있고 결과가 같은 종류·같은 분류·빈 키가 아닐 때만 받아들인다 — 아니면
    저장된 L2 그대로. 예외를 내지 않는다.

    The current L2 of an entry: recomputed only when its class is in ``classes_changed_since(entry.sig)``; the
    result is accepted only when the rebuilt error has the facts the rule reads and the result keeps the kind prefix
    and the class and has a key — otherwise the stored L2. Never raises."""
    l2 = entry.l2 or ""
    if not l2:
        return l2
    kind, cls, _key = l2_parts(l2)
    if not cls or cls not in changed_since(entry.sig):
        return l2
    err = rebuild_error(entry)
    if err is None:
        return l2
    need = _NEEDS.get(cls)
    if need is not None and not need(err):
        return l2
    try:
        with no_reactor_modules():
            new = (signer or default_signer)(err) or ""
    except Exception as e:  # noqa: BLE001 - one odd record keeps its L2
        log.debug("kb re-sign: %s kept its L2 (%s: %s)", entry.id, type(e).__name__, e)
        return l2
    nk, ncls, nkey = l2_parts(new)
    if nk != kind or ncls != cls or not nkey.strip():
        return l2
    return new


@dataclass
class EntryPlan:
    """항목들의 재서명 계획: ``new_l2`` = L2 가 바뀌는 항목(위치 → 새 L2), ``stamp`` = 다시 계산된 항목 위치(옛
    버전이고 그 뒤로 규칙이 바뀐 분류 — L2 가 그대로여도 도장을 받아 다시 보지 않는다). 규칙이 바뀌지 않은 분류의
    기록은 어느 쪽에도 없다(바이트 그대로 남는다).

    ``new_l2``: position → new L2 for the entries whose L2 changes; ``stamp``: positions of every recomputed entry
    (older version, class changed since — stamped even when the L2 stays, so it is never looked at again). Records of
    unchanged classes are in neither (left byte for byte)."""
    new_l2: dict[int, str] = field(default_factory=dict)
    stamp: set[int] = field(default_factory=set)


def plan_entries(entries: Union[Mapping[int, Optional[ErrorEntry]], list[Optional[ErrorEntry]]],
                 signer: Optional[Signer] = None, version: Optional[int] = None) -> EntryPlan:
    """``entries``(위치 → 항목, 또는 목록; None 은 못 읽은 줄 — 건너뜀)마다 도장·새 L2 를 정한다. 규칙이 바뀐 분류의
    옛 기록만 본다.

    Stamp / new L2 for every entry (a mapping position → entry, or a list; None = an unreadable line, skipped); only
    older records of a class the rules changed are looked at."""
    v = current_version() if version is None else version
    plan = EntryPlan()
    items: Iterable = entries.items() if isinstance(entries, Mapping) else enumerate(entries)
    for i, e in items:
        if e is None or not e.l2 or int(e.sig or 0) >= v or l2_parts(e.l2)[1] not in changed_since(e.sig):
            continue
        plan.stamp.add(i)
        new = resigned_l2(e, signer)
        if new and new != e.l2:
            plan.new_l2[i] = new
    return plan


@dataclass
class ResignReport:
    """재서명 결과(또는 ``dry_run`` 이면 할 일): 도장 찍은 항목 수, L2 가 바뀐 항목 수, 옮긴 케이스
    ``moves``(옛 ID, 새 ID, L2), 나뉜 케이스 ``splits``(옛 ID, 새 ID 들), 이미 있던 케이스에 합친 수 ``merged``, 도장
    찍은 케이스 수(이번에 고쳐 쓴 케이스만), 못 읽어 건너뛴 케이스 ``unreadable``(이유는 ``reasons``), 실패 문장 ``error``.

    What a re-sign did (or, ``dry_run``, would do)."""
    version: int = 0
    dry_run: bool = False
    entries_stamped: int = 0
    entries_resigned: int = 0
    moves: list[tuple[str, str, str]] = field(default_factory=list)
    splits: list[tuple[str, list[str]]] = field(default_factory=list)
    merged: int = 0
    cases_stamped: int = 0
    unreadable: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)       # skipped case id → why (kb.json "skipped")
    error: str = ""

    @property
    def changed(self) -> bool:
        """무엇이든 바뀌었(바뀔 것이)나.

        Anything stamped, re-signed, moved or split."""
        return bool(self.entries_stamped or self.entries_resigned or self.moves or self.splits or self.cases_stamped)

    def summary(self) -> str:
        """한 줄 요약(로그·CLI 첫 줄).

        The one-line summary (log, CLI)."""
        if self.error:
            return f"kb re-sign failed: {self.error}"
        verb = "would be" if self.dry_run else ""
        head = f"kb re-sign (rules v{self.version}){' — dry run' if self.dry_run else ''}: "
        if not self.changed and not self.unreadable:
            return head + "nothing signed by older rules"
        return (head + f"{self.entries_stamped} record(s) {verb + ' ' if verb else ''}stamped, "
                f"{self.entries_resigned} re-signed; {len(self.moves)} case(s) moved ({self.merged} into an existing "
                f"case), {len(self.splits)} split, {self.cases_stamped} case(s) stamped"
                + (f"; {len(self.unreadable)} unreadable (skipped: {', '.join(self.unreadable[:5])})"
                   if self.unreadable else ""))

    def lines(self, limit: int = 50) -> list[str]:
        """운영자에게 보일 줄들: 요약, 옮긴 케이스마다 ``<옛 ID> → <새 ID>  <l2>``(``limit`` 줄까지, 넘으면
        ``… N more``), 나뉜 케이스마다 ``<옛 ID> → <ID>, <ID>``.

        The operator text: the summary, one ``<old id> → <new id>  <l2>`` line per moved case (at most ``limit``,
        then ``… N more``), one line per split."""
        out = [self.summary()]
        rows = [f"  {o} → {n}  {cut(l2, 160)}" for o, n, l2 in self.moves]
        rows += [f"  {o} → {', '.join(ns)}  (split)" for o, ns in self.splits]
        out += rows[:limit]
        if len(rows) > limit:
            out.append(f"  … {len(rows) - limit} more")
        return out
