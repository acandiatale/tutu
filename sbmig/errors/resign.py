"""KB 재서명에서 파일을 건드리지 않는 계산 부분을 맡는 모듈.
이전 규칙으로 서명된 기록의 L2를 현재 규칙으로 다시 계산하고 케이스를 어디로 옮길지 정한다.
파일 쓰기는 :meth:`sbmig.errors.cases.CaseStore.resign`이 KB 잠금 안에서 한다.
- 케이스 ID는 L2의 해시이므로 서명 규칙이 바뀌면 같은 오류가 다른 ID를 받는다.
  그래서 기록마다 서명할 때의 규칙 버전(``sig``)을 남긴다
- ``SIGNATURE_CHANGES``에서 그 버전 이후 바뀐 오류 분류만 다시 계산하고, 나머지는 버전만 갱신한다
- 다시 계산할 때는 기록의 ``symbol`` / ``location`` / ``artifact`` / ``goal``을 쓰고, 없으면 ``raw``에 있는
  javac의 이어지는 줄을 읽는다
- 결과의 분류가 달라지거나 키가 비면 저장된 L2를 그대로 둔다
- 계산하는 동안 프로세스 전역의 reactor 모듈 이름 목록을 비워 두고 끝나면 되돌린다

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

# javac의 이어지는 줄을 찾는 식으로 log_parser._CONT_RE와 같으며, 비공개 이름에 의존하지 않으려고 여기 따로 둔다
# 저장된 raw의 줄 앞에는 '[ERROR]'가 붙어 있을 수 있으므로 먼저 뗀다
# the javac continuation line, the same expression as sbmig.build.log_parser._CONT_RE (mirrored: a private name)
_CONT_RE = re.compile(r"^\s+(?P<key>symbol|location|required|found|reason)\s*:\s*(?P<val>.*)$")
_LEVEL_RE = re.compile(r"^\s*\[(?:ERROR|FATAL|WARNING|WARN|INFO)\]")
# JSON 파싱 없이 errors.jsonl 한 줄에서 최상위 'l2'와 'sig'만 읽는다
# 문자열 안 따옴표는 이스케이프되고 하위 객체에는 l2 / sig 키가 없으므로 잘못 찾을 일이 없다
# the top-level "l2" (kind, class) and "sig" of one stored line, without a JSON parse
_LINE_L2_RE = re.compile(r'"l2"\s*:\s*"([^"|\\]*)\|([^"|\\]*)')
_LINE_SIG_RE = re.compile(r'"sig"\s*:\s*(\d+)')
_LINE_CASE_RE = re.compile(r'"case_id"\s*:\s*"([^"\\]*)"')
_ANY_CASE_RE = re.compile(rb'"case_id"\s*:\s*"([^"\\]*)"')
ID_HEAD = '{"id":"'
# 텍스트를 정리할 때 잘라 낸 값 끝에 붙는 표시이며, 잘린 값은 없는 것으로 본다
_CUT_RE = re.compile(r"…\[\d+ more characters\]\s*$")
L2_LOG_CHARS = 100


def current_version() -> int:
    """현재 서명 규칙 버전(``sbmig.build.signatures.SIGNATURE_VERSION``)을 반환한다.
    테스트에서 바꿀 수 있도록 매번 새로 읽는다.

    The current signature rule version, read at call time."""
    return int(getattr(_sigs, "SIGNATURE_VERSION", 1) or 1)


def changed_since(version: int) -> frozenset[str]:
    """``version`` 규칙과 현재 규칙 사이에 L2가 달라질 수 있는 오류 분류 목록을 반환한다.

    The error classes whose L2 may differ between rule ``version`` and now."""
    try:
        return frozenset(_sigs.classes_changed_since(int(version or 0)))
    except Exception:  # noqa: BLE001 - a broken table changes nothing
        return frozenset()


def l2_parts(l2: str) -> tuple[str, str, str]:
    """L2를 (종류, 분류, 키)로 나눈다. 두 부분으로 된 L2는 키가 빈 문자열이다.

    (kind, class, key) of an L2; the key is "" for a two-segment L2."""
    parts = (l2 or "").split("|", 2)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def cut(text: str, limit: int = L2_LOG_CHARS) -> str:
    """로그에 출력하기 위해 L2를 ``limit``자까지 자른다.

    An L2 cut for a log line."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def record_text(line: str) -> str:
    """한 줄에서 레코드 부분을 꺼낸다.
    깨진 레코드 뒤에 온전한 레코드가 붙어 있으면 뒤쪽을 쓴다(``parse_record``와 같은 규칙).

    The record part of a line: the whole record glued after a torn one, else the line (``parse_record``'s rule)."""
    line = line or ""
    i = line.rfind(ID_HEAD, 1)
    return line[i:] if i > 0 else line


def line_case_id(line: str) -> str:
    """한 줄의 레코드에서 최상위 ``case_id``를 꺼낸다. 없으면 빈 문자열을 반환한다.

    The top-level ``case_id`` of the line's record (regex; "" when none)."""
    m = _LINE_CASE_RE.search(record_text(line))
    return m.group(1) if m else ""


def line_case_ids(line: bytes) -> set[str]:
    """한 줄에 나오는 모든 ``case_id`` 값을 찾는다.
    최상위 값과 ``resolved_by`` 안의 값을 바이트에서 정규식으로 찾는다.

    Every ``case_id`` value in a line (top level and ``resolved_by``), from the bytes."""
    return {m.decode("utf-8", "replace") for m in _ANY_CASE_RE.findall(line or b"")}


def older_record(line: str, version: Optional[int] = None,
                 skipped: Collection[str] = ()) -> Optional[ErrorEntry]:
    """이전 규칙으로 서명됐고 그 뒤 분류 규칙이 바뀐 레코드면 그 항목을 반환하고, 아니면 None을 반환한다.
    정규식으로 먼저 거르고 후보만 ``parse_record``로 읽는다.
    파싱되지 않는 줄과 ``skipped``에 있는 케이스는 None이다.

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
    """이 줄에서 :func:`older_record`가 다시 계산할 항목을 찾는지 확인한다.

    Whether :func:`older_record` finds an older record in the line."""
    return older_record(line, version, skipped) is not None


def _fact(text: str) -> str:
    # 텍스트 정리 때 잘린 값은 믿지 않고 raw에서 다시 읽는다
    return "" if not text or _CUT_RE.search(text) else text


def continuation(raw: str) -> dict[str, str]:
    """raw 블록에서 javac의 이어지는 줄들을 읽어 키별로 처음 나온 값을 반환한다.

    The javac continuation lines of a raw block, the first value per key."""
    out: dict[str, str] = {}
    for ln in (raw or "").splitlines():
        body = _LEVEL_RE.sub("", ln, count=1)
        m = _CONT_RE.match(body)
        if m and m.group("key") not in out:
            out[m.group("key")] = m.group("val").strip()
    return out


def rebuild_error(entry: ErrorEntry) -> Optional[BuildError]:
    """저장된 항목의 정보로 빌드 오류를 다시 만든다.
    정보가 없는 이전 항목은 ``raw``의 ``symbol:`` / ``location:`` 줄에서 읽는다.
    ``no_suitable_method``의 후보 줄을 규칙이 볼 수 있게 ``raw``도 그대로 넣는다.
    L2가 없으면 None을 반환한다.

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


# 분류별 규칙이 읽는 값이 없으면 다시 계산해도 이전 키가 나올 뿐이므로 저장된 L2를 유지한다
# the facts the rule of a class reads; without them the recomputation is not trusted
_NEEDS: dict[str, Callable[[BuildError], bool]] = {
    "cannot_find_symbol": lambda err: bool((err.symbol or "").strip()),
    "no_suitable_method": lambda err: bool((err.raw or "").strip()),
}


@contextmanager
def no_reactor_modules() -> Iterator[None]:
    """재계산하는 동안 ``signature_for``가 읽는 reactor 모듈 이름을 비워 두고, 끝나면 예외가 나도 원래대로 되돌린다.

    Empty the process-global reactor-module set for the recomputation and restore it afterwards (also on an
    exception): a re-sign inside a migration neither depends on nor disturbs the running project's names."""
    saved = _sigs.reactor_modules()
    _sigs.set_reactor_modules(())
    try:
        yield
    finally:
        _sigs.set_reactor_modules(saved)


def default_signer(err: BuildError) -> str:
    """현재 규칙으로 L2를 계산한다.
    트리아지와 같은 순서로 ``signature_for`` 다음에 ``refine_signature``를 적용한다.

    The current rules' L2: ``signature_for`` then ``refine_signature``, the order the triage uses."""
    from sbmig.triage.cluster import refine_signature

    l1, l2 = _sigs.signature_for(err)
    err.signature, err.signature_generic = l1, l2
    refine_signature(err)
    return err.signature_generic or ""


def resigned_l2(entry: ErrorEntry, signer: Optional[Signer] = None) -> str:
    """항목의 현재 L2를 반환한다. 분류가 ``classes_changed_since(entry.sig)``에 있을 때만 다시 계산한다.
    규칙이 읽는 값이 있고, 종류와 분류가 같고, 키가 비지 않은 결과만 받아들이고, 아니면 저장된 L2를 쓴다.
    예외를 던지지 않는다.

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
    """항목들의 재서명 계획을 담는 데이터 클래스.
    ``new_l2``는 L2가 바뀌는 항목, ``stamp``는 다시 계산한 모든 항목의 위치다.
    L2가 그대로여도 버전을 갱신해서 다음에 다시 계산하지 않게 한다.

    ``new_l2``: position → new L2 for the entries whose L2 changes; ``stamp``: positions of every recomputed entry
    (older version, class changed since — stamped even when the L2 stays, so it is never looked at again). Records of
    unchanged classes are in neither (left byte for byte)."""
    new_l2: dict[int, str] = field(default_factory=dict)
    stamp: set[int] = field(default_factory=set)


def plan_entries(entries: Union[Mapping[int, Optional[ErrorEntry]], list[Optional[ErrorEntry]]],
                 signer: Optional[Signer] = None, version: Optional[int] = None) -> EntryPlan:
    """항목마다 버전을 갱신할지와 새 L2를 정한다.
    규칙이 바뀐 분류의 이전 기록만 보고, None인 항목은 읽지 못한 줄이므로 건너뛴다.

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
    """재서명 결과를 담는 데이터 클래스. ``dry_run``이면 실제로 바꾸지 않고 할 일만 담는다.
    옮긴 케이스 ``moves``, 나뉜 케이스 ``splits``, 합친 수 ``merged``, 읽지 못한 케이스 ``unreadable``과
    ``reasons``, 실패 메시지 ``error``를 가진다.

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
        """바뀐 것(``dry_run``이면 바뀔 것)이 있는지 확인한다.

        Anything stamped, re-signed, moved or split."""
        return bool(self.entries_stamped or self.entries_resigned or self.moves or self.splits or self.cases_stamped)

    def summary(self) -> str:
        """로그와 CLI 첫 줄에 쓸 한 줄 요약을 만든다.

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
        """운영자에게 보여 줄 줄 목록을 만든다.
        요약, 옮긴 케이스마다 한 줄(``limit``줄까지), 나뉜 케이스마다 한 줄을 담는다.

        The operator text: the summary, one ``<old id> → <new id>  <l2>`` line per moved case (at most ``limit``,
        then ``… N more``), one line per split."""
        out = [self.summary()]
        rows = [f"  {o} → {n}  {cut(l2, 160)}" for o, n, l2 in self.moves]
        rows += [f"  {o} → {', '.join(ns)}  (split)" for o, ns in self.splits]
        out += rows[:limit]
        if len(rows) > limit:
            out.append(f"  … {len(rows) - limit} more")
        return out
