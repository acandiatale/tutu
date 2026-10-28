"""수정 루프와 오케스트레이터가 실행의 오류 목록(``sbmig.errors.entries.ErrorList``)과 KB 케이스 저장소
(``kb.cases``)에 말을 거는 유일한 통로(어댑터) — v2.1. 흐름상 위치: transform.compile 부터 모든 빌드 오류,
재구성의 pom 교정(pom-verify), 구조 검토 결과, 수정 루프의 시도가 ``RunErrors`` 로 들어가고(실행 디렉터리의
errors.jsonl / errors.md), 저장된 스크립트의 조회·저장·실적 기록은 ``CaseScripts`` 로 간다. 결정: 반대편
이름이 바뀌어도 여기 한 줄만 고치면 되고, 반대편이 깨져도 루프 동작은 바뀌지 않는다 — 예외는 WARNING 한 줄 +
"없음" 값. 새 키워드(v2.1d/e 필드)를 모르는 옛 반대편에는 그 키워드만 빼고 다시 부른다.

v2.1 (2026-10-27, docs/v2/PLAN-v2.1.md §3.3): the ONE place the loop and the orchestrator talk to the run's error
list (`sbmig.errors.entries.ErrorList`, workstream E1) and to the KB's case store (`kb.cases`, E1).

Every call goes through these adapters, so a rename on the other side costs one line here, and a problem there never
changes what the loop does: an exception is logged (WARNING) and the call returns its "nothing" value.

`RunErrors` wraps the run's error list (None = off: dry run, or `sbmig.errors` not importable):
    add_build(build, seen, round=, void=) → new entries (count) every build of the run (compile, build, fix rN)
    add_pom_verify(file, note)       → the entry                one per pom correction of the restructure
    add_note(file, text, seen)       → the entry                review warnings
    review_fixed(file, text, try)    → the entry                a critical review issue the review's edits fixed
                                                                (try fields incl. `edits`, the 수정 notes)
    verify_poms(build_errors)        → pom-verify entries resolved by that build
    begin_try / finish_try / resolve / block                    the fix loop's tries (finish_try: v2.1d
                                                                major_upgrades / verify, optional)
    end_of_run / write / to_report / entries / open_count       finalize

`CaseScripts` wraps `kb.cases` (None = no KB): best / code / find / add_script / credit.


"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("sbmig.loop")


def _count(x: Any) -> int:
    """반대편이 무엇을 돌려주든(정수, 항목 목록, None) 개수로 바꾼다.

    A count from whatever the other side returned: an int, a list of entries, None."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    if isinstance(x, (list, tuple, set, dict)):
        return len(x)
    return 0


class _Guarded:
    """어댑터 공통 바탕: ``_target``(반대편 객체)이 없으면 비활성, 있으면 ``_call`` 로 안전하게 부른다."""
    _target: Any = None
    _what = "?"

    @property
    def active(self) -> bool:
        """반대편 객체가 있는가(False 면 모든 호출이 "없음" 값을 돌려준다)."""
        return self._target is not None

    def _call(self, name: str, *args: Any, optional: Optional[dict[str, Any]] = None, default: Any = None,
              **kwargs: Any) -> Any:
        """``target.name(...)`` 을 부른다. ``optional`` 키워드를 받지 않는 옛 반대편이면(TypeError 메시지에
        그 이름) 그 키워드만 빼고 다시 부른다. 어떤 예외도 올리지 않는다(WARNING + ``default``).

        `target.name(*args, **kwargs, **optional)`; `optional` kwargs are dropped once when the callee does not
        take them (TypeError naming the keyword). Never raises."""
        t = self._target
        fn = getattr(t, name, None) if t is not None else None
        if not callable(fn):
            return default
        opt = dict(optional or {})
        try:
            while True:
                try:
                    return fn(*args, **kwargs, **opt)
                # 주의: 메시지에 그 키워드 이름이 따옴표로 들어간 TypeError 만 재시도한다(다른 것은 실패)
                except TypeError as e:          # drop only the optional keyword(s) the callee does not take
                    named = [k for k in opt if f"'{k}'" in str(e)]
                    if not named:
                        raise
                    for k in named:
                        opt.pop(k)
        except Exception as e:  # noqa: BLE001 - the error list / KB never stops a run
            log.warning("%s.%s failed: %s: %s", self._what, name, type(e).__name__, e)
            return default


# 구조 검토가 스스로 고친 것은 이 L2 하나의 케이스로 모인다(오류 항목도, 저장된 스크립트도)
REVIEW_L2 = "review|structure"      # the case of the structure review's own fixes (entries + stored scripts)
OPTIONAL_TRY_FIELDS = ("edits", "major_upgrades", "verify", "verify_source")   # finish_try fields an older error list may not take


class RunErrors(_Guarded):
    """실행의 오류 목록(errors.jsonl / errors.md). 목록이 없으면(드라이런, 패키지 없음) 모든 메서드가
    아무것도 하지 않는다 — 기록이 없어도 실행은 계속된다.

    The run's error list (E1 `sbmig.errors.entries.ErrorList`); every method is a no-op without one."""
    _what = "errors"

    def __init__(self, target: Any = None):
        self._target = target
        self.pom_verify_added = 0

    @classmethod
    def create(cls, ws: Any, cfg: Any, project: str, factory: Optional[Callable[..., Any]] = None) -> "RunErrors":
        """실행의 오류 목록을 만든다(테스트는 ``factory``). 못 만들면 비활성 목록 + WARNING 한 줄: 그 실행은
        errors.jsonl 없이 진행된다.

        `factory(ws, cfg, project)` (tests) or `ErrorList.for_workspace(ws, cfg, project)`; a missing / broken
        package gives an inactive list (a WARNING line: the run goes on without errors.jsonl)."""
        try:
            if factory is None:
                from sbmig.errors.entries import ErrorList
                return cls(ErrorList.for_workspace(ws, cfg, project=project))
            return cls(factory(ws, cfg, project))
        except Exception as e:  # noqa: BLE001
            log.warning("error list unavailable (%s: %s) — no errors.jsonl in this run", type(e).__name__, e)
            return cls(None)

    # ---------------------------------------------------------------- builds
    def add_build(self, build: Any, seen: str, *, round: Optional[int] = None, void: bool = False,
                  scope: Optional[str] = None) -> int:
        """빌드 한 번을 목록에 반영: 새 오류만 추가하고, 더 이상 안 보이는 열린 항목은 해결(``build``)
        처리한다 — 단 이번 라운드의 대기 중 시도가 걸린 항목은 루프의 ``resolve`` 가 판정한다. 그래서 채점할
        시도의 ``resolve`` 보다 반드시 먼저 부른다. ``round`` = 이 빌드가 채점하는 라운드(v2.1f: 시도 없이
        사라진 오류의 ``resolved_by`` 는 이 라운드에 그 파일·위쪽 pom 을 바꾼 시도), ``void`` = 아무것도 말해
        주지 않는 빌드(타임아웃·파싱된 오류 없음·되돌린 트리).

        One build (the BuildResult: ok / timed out / modules built) → the list adds its NEW errors and resolves
        the open entries it no longer shows (except this round's pending tries: the loop's `resolve` decides those).
        `round` = the fix round this build scores; `void` = the build says nothing. Returns the new entries' count.
        Call it BEFORE `resolve` for the tries it scores."""
        opt: dict[str, Any] = {"void": void}
        if round is not None:
            opt["round"] = round
        if scope:
            opt["scope"] = scope
        return _count(self._call("add_build", build, seen, optional=opt))

    def verify_poms(self, build: Any) -> int:
        """이 빌드가 검증해 준 pom-verify 항목들(재구성이 고친 pom)을 해결 처리하고 그 수를 돌려준다.

        The pom-verify entries this build settles (resolved "build"); returns how many."""
        return _count(self._call("verify_poms", build))

    def add_pom_verify(self, file: str, note: str) -> Any:
        """재구성의 pom 교정 하나 = 항목 하나(status ``pom-verify``, 다음 빌드가 검증)."""
        e = self._call("add_pom_verify", file, note)
        if self.active:
            self.pom_verify_added += 1
        return e

    def size(self) -> int:
        """목록의 항목 수(로그 줄의 ``#n``). 목록이 없으면 0.

        How many entries the list holds (the `#n` of a log line); 0 without a list."""
        return len(self.entries()) if self.active else 0

    def add_note(self, file: str, text: str, seen: str = "review") -> Any:
        """메모 항목(status ``note``) — 구조 검토의 warning, 고치지 못한 critical."""
        return self._call("add_note", file, text, seen)

    def review_fixed(self, file: str, text: str, **try_fields: Any) -> Any:
        """구조 검토가 직접 고친 critical 문제 하나: 항목(seen ``review``, 케이스 ``REVIEW_L2``) + 시도
        1개(source ``review``, 라운드 0)를 만들고 ``review`` 로 해결 처리한다.

        A critical review issue the review's own fix addressed: an entry (seen `review`, case `REVIEW_L2`) with
        one try (source `review`, round 0) resolved `review`."""
        if self._target is None:
            return None
        entry = self._call("add_note", file, text, "review", status="open", l2=REVIEW_L2)
        eid, case_id = str(getattr(entry, "id", "") or ""), str(getattr(entry, "case_id", "") or "")
        if entry is None or not eid:
            return entry
        tr = self._call("begin_try", case_id, 0, "review", entry_ids=[eid])
        n = int(getattr(tr, "n", 0) or 0)
        if n:
            self.finish_try(case_id, n, **try_fields)
            self._call("resolve", case_id, 0, n, entry_ids=[eid])
        return entry

    # ---------------------------------------------------------------- tries
    def begin_try(self, case_id: str, round: int, source: str, script_version: int = 0) -> Any:
        """케이스의 항목들에 새 시도(FixTry, 번호 ``n``)를 연다. 목록이 없으면 None.

        A new FixTry (its `n`) for the case's entries; None = no list."""
        return self._call("begin_try", case_id, round, source, script_version=script_version)

    def finish_try(self, case_id: str, n: int, *, optional: Optional[dict[str, Any]] = None, **fields: Any) -> None:
        """시도의 필드(원인·해결·스크립트 없는 이유·적용·파일·수정 기록·diff·커밋·결과·메모·LLM 호출·초)를
        채운다. 적용 직후, 채점·되돌리기 뒤에 다시 부른다. 옛 목록이 모르는 필드(v2.1b 수정 기록, v2.1d 메이저
        변경·운영자 확인)는 뺀다.

        Set the try's fields (analysis, solution, no_script_reason, applied, files, edits, diff, commit, result,
        note, llm_calls, seconds); called after the apply, again after the scoring / a rollback. `optional` fields
        (script_version, edits — the v2.1b 수정 notes —, major_upgrades / verify — v2.1d 메이저 변경 + 운영자 확인)
        are dropped when the list does not take them."""
        opt = dict(optional or {})
        for k in OPTIONAL_TRY_FIELDS:
            if k in fields:
                opt[k] = fields.pop(k)
        self._call("finish_try", case_id, n, optional=opt, **fields)

    def resolve(self, case_id: str, round: int, n: int) -> None:
        """그 시도로 사라진 케이스 항목들을 해결 처리(``r<round> try <n>``). 개선·성공·부분 해결 뒤에."""
        self._call("resolve", case_id, round, n)

    def block(self, case_id: str, reason: str) -> None:
        """케이스의 열린 항목들을 차단(blocked) 처리 — 보고서 인수인계 섹션으로 간다."""
        self._call("block", case_id, reason)

    # ---------------------------------------------------------------- views / finalize
    def entries(self) -> list:
        """목록의 모든 항목(ErrorEntry). 반대편이 ``entries`` 를 못 주면 ``to_report`` 로 대신한다."""
        t = self._target
        if t is None:
            return []
        got = getattr(t, "entries", None)
        try:
            got = got() if callable(got) else got
        except Exception as e:  # noqa: BLE001
            log.warning("errors.entries failed: %s: %s", type(e).__name__, e)
            got = None
        if got is None:
            got = self.to_report()
        return list(got or [])

    def counts(self) -> dict[str, int]:
        """상태별 항목 수(open / pom-verify / resolved / blocked / unresolved / note).

        status → entries (open / pom-verify / resolved / blocked / unresolved / note)."""
        got = self._call("counts", default=None)
        if isinstance(got, dict):
            return {str(k): int(v) for k, v in got.items()}
        out: dict[str, int] = {}
        for e in self.entries():
            st = str(getattr(e, "status", "") or "")
            out[st] = out.get(st, 0) + 1
        return out

    def open_count(self) -> Optional[int]:
        """아직 열린 항목 수(목록이 없으면 None — 로그는 그때 빌드의 오류 수를 쓴다).

        Entries still open (None = no list)."""
        return self.counts().get("open", 0) if self.active else None

    def open_cases(self) -> dict:
        """열린 항목이 있는 케이스들(케이스 ID → 항목)."""
        got = self._call("open_cases", default={})
        return dict(got) if isinstance(got, dict) else {}

    def entries_for(self, case_id: str) -> list:
        """한 케이스의 항목들."""
        return list(self._call("entries_for", case_id, default=[]) or [])

    def end_of_run(self) -> dict[str, int]:
        """실행 끝: 아직 열린 항목은 unresolved 로 닫는다(판정이 끝내 오지 않은 것도). 상태별 개수를
        돌려준다.

        Open entries → unresolved (or resolved by a verdict that never came); returns the counts by status."""
        got = self._call("end_of_run")
        return dict(got) if isinstance(got, dict) else self.counts()

    def write(self) -> dict[str, str]:
        """실행 디렉터리의 errors.jsonl(+ errors.md)을 다시 쓰고, 쓴 경로를 돌려준다.

        Rewrite <run dir>/errors.jsonl (+ errors.md); returns the paths written ({'errors': …, 'errors_md': …})."""
        self._call("write")
        out: dict[str, str] = {}
        for key, attr in (("errors", "path"), ("errors_md", "md_path")):
            try:
                p = getattr(self._target, attr, None) if self._target is not None else None
                if p is not None and hasattr(p, "exists") and p.exists():
                    out[key] = str(p)
            except Exception:  # noqa: BLE001
                continue
        return out

    @property
    def problems(self) -> list[str]:
        """목록이 스스로 겪은 문제들(마무리에서 ``errors-failed: …`` 플래그가 된다)."""
        return list(getattr(self._target, "problems", None) or [])

    def to_report(self) -> list:
        """보고서용 항목 사본(diff 캡, as-is / to-be 변경 조각은 뺌 — report.json 크기를 묶어 두려고)."""
        return list(self._call("to_report", default=[]) or [])


class CaseScripts(_Guarded):
    """KB 의 케이스 저장소(``kb.cases``) 어댑터: 저장된 스크립트 고르기(best)·코드·케이스 조회·저장·실적
    기록. KB 가 없으면 비활성 — 재사용할 것이 없고, LLM 스크립트는 저장 없이(v0) 실행된다.

    The KB's case store (`kb.cases`, E1 `CaseStore`); None = no KB (nothing reused, LLM scripts run unstored)."""
    _what = "kb.cases"

    def __init__(self, target: Any = None):
        self._target = target if target is not None and callable(getattr(target, "best", None)) else None

    @classmethod
    def of(cls, kb: Any) -> "CaseScripts":
        """``kb.cases`` 로 어댑터를 만든다. KB 가 깨져 있어도 예외 없이 비활성 어댑터."""
        try:
            return cls(getattr(kb, "cases", None))
        except Exception as e:  # noqa: BLE001 - a broken KB never stops the loop
            log.warning("kb.cases unavailable: %s: %s", type(e).__name__, e)
            return cls(None)

    def best(self, case_id: str) -> Optional[tuple[Any, str]]:
        """재사용할 버전의 (CaseScript, 코드): 다른 프로젝트에서 검증된(proven) 최신 → 미검증(unverified)
        최신. 저장본이 없으면 None(→ 사다리는 LLM 신규로).

        (CaseScript, code) of the version to reuse (proven newest → unverified newest), None = nothing stored."""
        got = self._call("best", case_id)
        if not got:
            return None
        try:
            meta, code = got
        except (TypeError, ValueError):
            return None
        return (meta, code or "") if meta is not None else None

    def code(self, case_id: str, version: int) -> str:
        """저장된 ``fix_v<version>.py`` 의 코드("" = 없음)."""
        return str(self._call("code", case_id, version, default="") or "")

    def find(self, case_id: str) -> Any:
        """케이스 정보(case.json 의 CaseInfo) 또는 None.

        The case (CaseInfo) or None."""
        return self._call("find", case_id)

    def describe(self, case_id: str) -> str:
        """KB 탐색 결과를 로그용 한 구절로(저장소가 말할 수 없으면 "").

        The KB lookup result as one log phrase (`저장된 스크립트 없음 (처음 보는 케이스)`, `저장된 스크립트 v2 (proven,
        성공 3 / 실패 0, 프로젝트 2)`); "" when the store cannot say."""
        return str(self._call("describe", case_id, default="") or "")

    def add_script(self, case_id: str, l2: str, code: str, *, source: str, run_id: str, project: str,
                   analysis: str = "", solution: str = "", **optional: Any) -> Any:
        """스크립트를 케이스 폴더에 저장(``fix_v<N>.py``) → CaseScript(버전·파일·상태). 같은 코드는 한 번만
        저장된다. None = 저장 실패. v2.1e 계보(``parent``, 왜 만들었는지 ``reason``)는 옛 저장소면 뺀다.

        Store a script → its CaseScript (version, file, status); None = not stored. `optional` (v2.1e `parent`,
        `reason`) is dropped when the store does not take it."""
        return self._call("add_script", case_id, l2, code, source=source, run_id=run_id, project=project,
                          analysis=analysis, solution=solution, optional=dict(optional))

    def credit(self, case_id: str, version: int, result: str, *, project: str, run_id: str, **optional: Any) -> bool:
        """저장된 버전의 실행 한 번을 실적으로 기록(성공 / 실패 횟수, 프로젝트, v2.1e 실행 기록 ScriptRun:
        실패 이유·라운드· 시도 번호·오류 수·파일). KB 가 없으면 False.

        Credit a run of a stored version. `optional` (v2.1e ScriptRun fields: reason, round, n, errors_before,
        errors_left, errors_new, files) is dropped when the store does not take it."""
        if not self.active:
            return False
        self._call("credit", case_id, version, result, project=project, run_id=run_id, optional=dict(optional))
        return True


def script_path(case_id: str, cs: Any) -> str:
    """저장된 스크립트의 KB 안 경로(로그 줄, 항목에 쓰임).

    'cases/<case_id>/fix_v2.py' — where a stored script lives in the KB (log lines, entries)."""
    f = str(getattr(cs, "file", "") or f"fix_v{int(getattr(cs, 'version', 0) or 0)}.py")
    return f if f.startswith("cases/") else f"cases/{case_id}/{f}"
