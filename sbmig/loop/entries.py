"""수정 루프와 오케스트레이터가 실행의 오류 목록(``sbmig.errors.entries.ErrorList``)과 KB 케이스
저장소(``kb.cases``)를 다룰 때 거치는 어댑터 모듈.
- ``RunErrors``: 빌드 오류, 재구성 단계의 pom 수정, 구조 검토 결과, 수정 시도를 errors.jsonl / errors.md에 기록한다
- ``CaseScripts``: 저장된 수정 스크립트를 조회하고 저장하며, 실행 결과를 기록한다
두 대상 중 하나가 깨져도 수정 루프는 똑같이 동작한다.
예외는 WARNING 한 줄을 남기고 빈 값을 반환하는 것으로 끝난다.
대상의 이전 구현이 새 키워드 인자를 모르면 그 키워드만 빼고 다시 호출한다.

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

import inspect
import logging
from typing import Any, Callable, Optional

log = logging.getLogger("sbmig.loop")


def _count(x: Any) -> int:
    """호출 대상이 정수, 항목 목록, None 중 무엇을 반환하든 개수로 바꿔 반환한다.

    A count from whatever the other side returned: an int, a list of entries, None."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    if isinstance(x, (list, tuple, set, dict)):
        return len(x)
    return 0


def _accepted(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any],
              opt: dict[str, Any]) -> dict[str, Any] | None:
    """``opt``의 선택 키워드 중 ``fn``이 받을 수 있는 것만 남겨 반환한다.
    시그니처를 알 수 없는 함수면 None을 반환한다.
    ``fn``이 가변 키워드 인자를 받으면 모두 남기고, 위치 인자로 이미 채운 이름과 위치 전용 인자는 뺀다.

    ``opt`` reduced to the keywords ``fn``'s signature takes (all of them when it takes ``**kwargs``; never a name
    the positional ``args`` already fill, never a positional-only one); None when the signature does not
    inspect (some builtins / C callables) — the caller then falls back to the TypeError retry."""
    try:
        sig = inspect.signature(fn)
    except Exception:  # noqa: BLE001 - TypeError / ValueError (builtins), or a broken __signature__: old way
        return None
    params = list(sig.parameters.values())
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return dict(opt)
    positional = [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                                  inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    filled = {p.name for p in positional[:len(args)]}
    names = {p.name for p in params if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                  inspect.Parameter.KEYWORD_ONLY)}
    return {k: v for k, v in opt.items() if k in names and k not in filled and k not in kwargs}


class _Guarded:
    """어댑터의 공통 기반 클래스.
    호출 대상(``_target``)이 없으면 아무 일도 하지 않고, 있으면 ``_call``로 예외 없이 호출한다."""
    _target: Any = None
    _what = "?"

    @property
    def active(self) -> bool:
        """호출 대상 객체가 있는지 반환한다. 없으면 모든 호출이 빈 값을 반환한다."""
        return self._target is not None

    def _call(self, name: str, *args: Any, optional: Optional[dict[str, Any]] = None, default: Any = None,
              **kwargs: Any) -> Any:
        """``target.name(...)``을 호출한다.
        예외를 던지지 않고, 실패하면 WARNING을 남기고 ``default``를 반환한다.
        대상의 시그니처가 받지 않는 ``optional`` 키워드는 호출 전에 뺀다.
        대상 함수 안에서 난 TypeError 때문에 다시 호출하는 일을 막기 위해서다.

        `target.name(*args, **kwargs, **optional)`; the `optional` kwargs the callee's signature does not take are
        dropped BEFORE the one call (a TypeError raised inside the callee is never retried: it is the call's
        failure, logged). Only a callable whose signature does not inspect keeps the old way: dropped once when
        the TypeError names the keyword. Never raises."""
        t = self._target
        fn = getattr(t, name, None) if t is not None else None
        if not callable(fn):
            return default
        opt = dict(optional or {})
        try:
            ok = _accepted(fn, args, kwargs, opt)
            if ok is not None:
                return fn(*args, **kwargs, **ok)
            # 시그니처를 알 수 없는 함수는 TypeError 메시지를 보고 받지 않는 키워드만 빼고 다시 시도한다
            while True:
                try:
                    return fn(*args, **kwargs, **opt)
                # 메시지에 키워드 이름이 따옴표로 들어간 TypeError만 재시도하고 나머지는 실패로 처리한다
                except TypeError as e:          # drop only the optional keyword(s) the callee does not take
                    named = [k for k in opt if f"'{k}'" in str(e)]
                    if not named:
                        raise
                    for k in named:
                        opt.pop(k)
        except Exception as e:  # noqa: BLE001 - the error list / KB never stops a run
            log.warning("%s.%s failed: %s: %s", self._what, name, type(e).__name__, e)
            return default


# 구조 검토가 직접 고친 것은 오류 항목과 저장 스크립트 모두 이 L2 케이스 하나로 모은다
REVIEW_L2 = "review|structure"      # the case of the structure review's own fixes (entries + stored scripts)
OPTIONAL_TRY_FIELDS = ("edits", "major_upgrades", "verify", "verify_source")   # finish_try fields an older error list may not take


class RunErrors(_Guarded):
    """실행의 오류 목록(errors.jsonl / errors.md)을 기록하는 어댑터 클래스.
    드라이런이거나 패키지가 없어 목록을 만들지 못하면 모든 메서드가 아무
    일도 하지 않으며, 기록이 없어도 실행은 계속된다.

    The run's error list (E1 `sbmig.errors.entries.ErrorList`); every method is a no-op without one."""
    _what = "errors"

    def __init__(self, target: Any = None):
        self._target = target
        self.pom_verify_added = 0

    @classmethod
    def create(cls, ws: Any, cfg: Any, project: str, factory: Optional[Callable[..., Any]] = None) -> "RunErrors":
        """실행의 오류 목록을 만든다. 테스트는 ``factory``로 목록을 바꿔 넣는다.
        만들지 못하면 WARNING 한 줄을 남기고 비활성 목록을 반환하므로, 그 실행은 errors.jsonl 없이 진행된다.

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
        """빌드 한 번의 결과를 오류 목록에 반영하고 새로 추가한 항목 수를 반환한다.
        새 오류는 추가하고, 더 이상 나오지 않는 열린 항목은 해결(``build``)로 닫는다.
        이번 라운드에 결과를 기다리는 시도가 걸린 항목은 수정 루프의 ``resolve``가 처리하므로, 이 메서드를
        그보다 먼저 호출해야 한다.
        ``void``는 타임아웃이나 되돌린 트리처럼 결과를 판단할 근거가 없는 빌드라는 뜻이다.

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
        """이 빌드로 확인된 pom-verify 항목(재구성 단계가 고친 pom)을 해결로 닫고 그 수를 반환한다.

        The pom-verify entries this build settles (resolved "build"); returns how many."""
        return _count(self._call("verify_poms", build))

    def add_pom_verify(self, file: str, note: str) -> Any:
        """재구성 단계의 pom 수정 하나를 항목 하나로 기록한다.
        상태는 ``pom-verify``이며 다음 빌드에서 확인한다."""
        e = self._call("add_pom_verify", file, note)
        if self.active:
            self.pom_verify_added += 1
        return e

    def size(self) -> int:
        """오류 목록의 항목 수를 반환한다. 로그 줄의 ``#n``에 쓰이고, 목록이 없으면 0을 반환한다.

        How many entries the list holds (the `#n` of a log line); 0 without a list."""
        return len(self.entries()) if self.active else 0

    def add_note(self, file: str, text: str, seen: str = "review") -> Any:
        """구조 검토의 warning이나 고치지 못한 critical을 메모 항목(상태 ``note``)으로 기록한다."""
        return self._call("add_note", file, text, seen)

    def review_fixed(self, file: str, text: str, **try_fields: Any) -> Any:
        """구조 검토가 직접 고친 critical 문제 하나를 기록한다.
        케이스 ``REVIEW_L2``의 항목과 출처가 ``review``인 시도 하나를 만들고, 바로 ``review``로 해결 처리한다.

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
        """케이스의 항목에 새 시도(FixTry, 번호 ``n``)를 연다. 오류 목록이 없으면 None을 반환한다.

        A new FixTry (its `n`) for the case's entries; None = no list."""
        return self._call("begin_try", case_id, round, source, script_version=script_version)

    def finish_try(self, case_id: str, n: int, *, optional: Optional[dict[str, Any]] = None, **fields: Any) -> None:
        """시도의 원인, 해결 방법, 적용 여부, 수정 파일, diff, 커밋, 결과, LLM 호출 수, 소요 시간 같은 필드를 채운다.
        적용 직후에 한 번 호출하고, 시도 결과를 평가하거나 되돌린 뒤에 다시 호출한다.
        이전 버전의 목록이 모르는 선택 필드는 뺀다.

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
        """그 시도로 오류가 사라진 케이스 항목을 해결로 닫는다(``r<round> try <n>``).
        시도 결과가 개선, 성공, 부분 해결일 때 호출한다."""
        self._call("resolve", case_id, round, n)

    def block(self, case_id: str, reason: str) -> None:
        """케이스의 열린 항목을 차단(blocked)으로 닫는다. 이 항목은 보고서의 인수인계 섹션에 나온다."""
        self._call("block", case_id, reason)

    # ---------------------------------------------------------------- views / finalize
    def entries(self) -> list:
        """오류 목록의 모든 항목(ErrorEntry)을 반환한다.
        대상이 ``entries``를 제공하지 않으면 ``to_report`` 결과를 대신 쓴다."""
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
        """상태(open / pom-verify / resolved / blocked / unresolved / note)별 항목 수를 반환한다.

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
        """아직 열린 항목 수를 반환한다.
        오류 목록이 없으면 None을 반환하며, 이때 로그에는 빌드의 오류 수를 쓴다.

        Entries still open (None = no list)."""
        return self.counts().get("open", 0) if self.active else None

    def open_cases(self) -> dict:
        """열린 항목이 있는 케이스를 케이스 ID별 항목 목록으로 반환한다."""
        got = self._call("open_cases", default={})
        return dict(got) if isinstance(got, dict) else {}

    def entries_for(self, case_id: str) -> list:
        """한 케이스의 항목 목록을 반환한다."""
        return list(self._call("entries_for", case_id, default=[]) or [])

    def end_of_run(self) -> dict[str, int]:
        """실행이 끝날 때 아직 열린 항목을 unresolved로 닫고 상태별 개수를 반환한다.
        결과가 끝내 나오지 않은 항목도 여기서 닫는다.

        Open entries → unresolved (or resolved by a verdict that never came); returns the counts by status."""
        got = self._call("end_of_run")
        return dict(got) if isinstance(got, dict) else self.counts()

    def write(self) -> dict[str, str]:
        """실행 디렉토리의 errors.jsonl과 errors.md를 새로 쓰고, 쓴 파일 경로를 반환한다.

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
        """오류 목록을 기록하다 생긴 문제를 반환한다. 마무리 단계에서 ``errors-failed: …`` 플래그로 붙는다."""
        return list(getattr(self._target, "problems", None) or [])

    def to_report(self) -> list:
        """보고서에 넣을 항목 목록을 만든다.
        report.json이 너무 커지지 않도록 diff를 자르고 as-is / to-be 변경 조각은 뺀다."""
        return list(self._call("to_report", default=[]) or [])


class CaseScripts(_Guarded):
    """KB 케이스 저장소(``kb.cases``)를 다루는 어댑터 클래스.
    재사용할 스크립트를 고르고, 코드와 케이스를 조회하고, 스크립트와 실행 결과를 저장한다.
    KB가 없으면 아무 일도 하지 않으므로 재사용할 스크립트가 없고, LLM이 만든 스크립트는 저장하지 않고 v0로 실행한다.

    The KB's case store (`kb.cases`, E1 `CaseStore`); None = no KB (nothing reused, LLM scripts run unstored)."""
    _what = "kb.cases"

    def __init__(self, target: Any = None):
        self._target = target if target is not None and callable(getattr(target, "best", None)) else None

    @classmethod
    def of(cls, kb: Any) -> "CaseScripts":
        """``kb.cases``로 어댑터를 만든다. KB가 깨져 있어도 예외를 던지지 않고 비활성 어댑터를 반환한다."""
        try:
            return cls(getattr(kb, "cases", None))
        except Exception as e:  # noqa: BLE001 - a broken KB never stops the loop
            log.warning("kb.cases unavailable: %s: %s", type(e).__name__, e)
            return cls(None)

    def best(self, case_id: str) -> Optional[tuple[Any, str]]:
        """재사용할 스크립트 버전의 (CaseScript, 코드)를 반환한다.
        검증된(proven) 최신 버전을, 없으면 미검증(unverified) 최신 버전을 고른다.
        저장된 스크립트가 없으면 None을 반환하고, 수정 루프는 LLM에 새 스크립트를 요청하는 단계로 넘어간다.

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
        """저장된 ``fix_v<version>.py``의 코드를 반환한다. 없으면 빈 문자열을 반환한다."""
        return str(self._call("code", case_id, version, default="") or "")

    def find(self, case_id: str) -> Any:
        """케이스 정보(case.json의 CaseInfo)를 반환한다. 없으면 None을 반환한다.

        The case (CaseInfo) or None."""
        return self._call("find", case_id)

    def describe(self, case_id: str) -> str:
        """KB 조회 결과를 로그에 쓸 한 구절로 반환한다. 저장소가 알려 주지 못하면 빈 문자열을 반환한다.

        The KB lookup result as one log phrase (`저장된 스크립트 없음 (처음 보는 케이스)`, `저장된 스크립트 v2 (proven,
        성공 3 / 실패 0, 프로젝트 2)`); "" when the store cannot say."""
        return str(self._call("describe", case_id, default="") or "")

    def add_script(self, case_id: str, l2: str, code: str, *, source: str, run_id: str, project: str,
                   analysis: str = "", solution: str = "", **optional: Any) -> Any:
        """스크립트를 케이스 폴더에 ``fix_v<N>.py``로 저장하고 CaseScript(버전, 파일, 상태)를 반환한다.
        저장에 실패하면 None을 반환한다.
        같은 코드는 한 번만 저장한다.
        이전 버전의 저장소가 모르는 이력 필드(``parent``, ``reason``)는 빼고 저장한다.

        Store a script → its CaseScript (version, file, status); None = not stored. `optional` (v2.1e `parent`,
        `reason`) is dropped when the store does not take it."""
        return self._call("add_script", case_id, l2, code, source=source, run_id=run_id, project=project,
                          analysis=analysis, solution=solution, optional=dict(optional))

    def credit(self, case_id: str, version: int, result: str, *, project: str, run_id: str, **optional: Any) -> bool:
        """저장된 스크립트 버전을 한 번 실행한 결과를 기록한다.
        성공, 실패 횟수와 프로젝트, 실행 기록(ScriptRun)을 남기며, KB가 없으면 False를 반환한다.

        Credit a run of a stored version. `optional` (v2.1e ScriptRun fields: reason, round, n, errors_before,
        errors_left, errors_new, files) is dropped when the store does not take it."""
        if not self.active:
            return False
        self._call("credit", case_id, version, result, project=project, run_id=run_id, optional=dict(optional))
        return True


    def mark_non_idempotent(self, case_id: str, version: int, note: str = "") -> bool:
        """다시 실행할 때마다 트리를 또 바꾸는 스크립트 버전을 비멱등으로 표시해 재사용 대상에서 뺀다.
        KB가 없거나 저장소에 이 기능이 없으면 False를 반환한다.

        v2.1y: mark a stored version `non_idempotent` (`CaseStore.mark_non_idempotent`: never selected for reuse
        again); False without a KB or when the store has no such call."""
        if not self.active or not callable(getattr(self._target, "mark_non_idempotent", None)):
            return False
        return self._call("mark_non_idempotent", case_id, version, optional={"note": note}) is not None


def script_path(case_id: str, cs: Any) -> str:
    """저장된 스크립트의 KB 안 경로를 반환한다. 로그와 오류 항목에 쓴다.

    'cases/<case_id>/fix_v2.py' — where a stored script lives in the KB (log lines, entries)."""
    f = str(getattr(cs, "file", "") or f"fix_v{int(getattr(cs, 'version', 0) or 0)}.py")
    return f if f.startswith("cases/") else f"cases/{case_id}/{f}"
