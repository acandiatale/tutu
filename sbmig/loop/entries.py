"""실행의 오류 목록(errors.jsonl)과 KB의 수정 스크립트를 수정 루프가 다룰 때 거치는 모듈.
오류 목록이나 KB가 깨져도 수정 루프는 똑같이 동작한다.

The ONE place the loop and the orchestrator talk to `sbmig.errors.entries.ErrorList` and to `kb.cases`, so a
rename on the other side costs one line here.

An exception on the other side leaves one warning and returns an empty value. A newer keyword the other side
does not take is dropped before the call.

`RunErrors` wraps the run's error list (None = off: dry run, or `sbmig.errors` not importable):
    add_build(build, seen, round=, void=) → new entries (count) every build of the run (compile, build, fix rN)
    add_pom_verify(file, note)       → the entry                one per pom correction of the restructure
    add_note(file, text, seen)       → the entry                review warnings
    review_fixed(file, text, try)    → the entry                a critical review issue the review's edits fixed
                                                                (try fields incl. `edits`, the 수정 notes)
    verify_poms(build_errors)        → pom-verify entries resolved by that build
    begin_try / finish_try / resolve / block                    the fix loop's tries (finish_try:
                                                                major_upgrades / verify optional)
    end_of_run
 / write / to_report / entries / open_count       finalize

`CaseScripts` wraps `kb.cases` (None = no KB): best / code / find / add_script / credit.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

log = logging.getLogger("sbmig.loop")


def _count(x: Any) -> int:
    """호출 대상이 반환한 값(정수, 항목 목록, None)을 개수로 바꿔 반환한다."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    if isinstance(x, (list, tuple, set, dict)):
        return len(x)
    return 0


def _accepted(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any],
              opt: dict[str, Any]) -> dict[str, Any] | None:
    """``opt``의 선택 키워드 중 ``fn``이 받을 수 있는 것만 남겨 반환한다. 시그니처를 알 수 없으면 None을 반환한다.
    ``fn``이 가변 키워드 인자를 받으면 모두 남긴다. 위치 인자로 이미 채운 이름과 위치 전용 인자는 뺀다.

    None (some builtins / C callables) makes the caller fall back to the TypeError retry."""
    try:
        sig = inspect.signature(fn)
    except Exception:  # noqa: BLE001 - 내장 함수나 깨진 __signature__다. TypeError를 보고 다시 시도하는 방식으로 간다
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
    호출 대상(``_target``)이 없으면 아무 일도 하지 않는다. 있으면 ``_call``로 호출하고 예외는 밖으로 내지 않는다."""
    _target: Any = None
    _what = "?"

    @property
    def active(self) -> bool:
        """호출 대상 객체가 있는지 반환한다. 없으면 모든 호출이 빈 값을 반환한다."""
        return self._target is not None

    def _call(self, name: str, *args: Any, optional: Optional[dict[str, Any]] = None, default: Any = None,
              **kwargs: Any) -> Any:
        """``target.name(...)``을 호출한다. 실패하면 경고를 남기고 ``default``를 반환한다. 예외를 던지지 않는다.
        대상이 받지 않는 ``optional`` 키워드는 호출 전에 뺀다. 대상 안에서 난 TypeError 때문에 다시 호출하지 않는다.

        Only a callable whose signature does not inspect keeps the retry: a keyword is dropped when the
        TypeError names it in quotes."""
        t = self._target
        fn = getattr(t, name, None) if t is not None else None
        if not callable(fn):
            return default
        opt = dict(optional or {})
        try:
            ok = _accepted(fn, args, kwargs, opt)
            if ok is not None:
                return fn(*args, **kwargs, **ok)
            # 시그니처를 모르는 함수다. TypeError 메시지를 보고 받지 않는 키워드만 빼서 다시 시도한다
            while True:
                try:
                    return fn(*args, **kwargs, **opt)
                # 메시지에 키워드 이름이 따옴표로 들어간 TypeError만 다시 시도한다. 나머지는 실패다
                except TypeError as e:
                    named = [k for k in opt if f"'{k}'" in str(e)]
                    if not named:
                        raise
                    for k in named:
                        opt.pop(k)
        except Exception as e:  # noqa: BLE001 - 오류 목록이나 KB 때문에 실행을 멈추지 않는다
            log.warning("%s.%s failed: %s: %s", self._what, name, type(e).__name__, e)
            return default


# 구조 검토가 직접 고친 것은 오류 항목과 저장 스크립트 모두 이 L2 케이스 하나로 모은다
REVIEW_L2 = "review|structure"
# 이전 오류 목록 구현이 받지 않을 수 있는 finish_try 필드
OPTIONAL_TRY_FIELDS = ("edits", "major_upgrades", "verify", "verify_source")


class RunErrors(_Guarded):
    """실행의 오류 목록(errors.jsonl / errors.md)을 기록하는 어댑터 클래스.
    dry-run이거나 목록을 만들지 못하면 모든 메서드가 아무 일도 하지 않고 실행은 계속된다."""
    _what = "errors"

    def __init__(self, target: Any = None):
        self._target = target
        self.pom_verify_added = 0

    @classmethod
    def create(cls, ws: Any, cfg: Any, project: str, factory: Optional[Callable[..., Any]] = None) -> "RunErrors":
        """실행의 오류 목록을 만든다. 테스트는 ``factory``로 목록을 바꿔 넣는다.
        만들지 못하면 경고 한 줄을 남기고 비활성 목록을 반환한다. 그 실행에는 errors.jsonl이 없다.

        Default: `ErrorList.for_workspace(ws, cfg, project)`."""
        try:
            if factory is None:
                from sbmig.errors.entries import ErrorList
                return cls(ErrorList.for_workspace(ws, cfg, project=project))
            return cls(factory(ws, cfg, project))
        except Exception as e:  # noqa: BLE001
            log.warning("error list unavailable (%s: %s) — no errors.jsonl in this run", type(e).__name__, e)
            return cls(None)

    # ---------------------------------------------------------------- 빌드
    def add_build(self, build: Any, seen: str, *, round: Optional[int] = None, void: bool = False,
                  scope: Optional[str] = None) -> int:
        """빌드 한 번의 결과를 오류 목록에 반영하고, 새로 추가한 항목 수를 반환한다.
        새 오류는 추가한다. 더 이상 나오지 않는 열린 항목은 해결(``build``)로 닫는다.
        이번 라운드에 시도한 항목은 수정 루프의 ``resolve``가 처리한다. 그래서 ``resolve``보다 먼저 호출해야 한다.
        ``void``는 타임아웃처럼 결과를 판단할 근거가 없는 빌드라는 뜻이다.

        `round` = the fix round this build scores."""
        opt: dict[str, Any] = {"void": void}
        if round is not None:
            opt["round"] = round
        if scope:
            opt["scope"] = scope
        return _count(self._call("add_build", build, seen, optional=opt))

    def verify_poms(self, build: Any) -> int:
        """이 빌드로 확인된 pom-verify 항목을 해결로 닫고 그 수를 반환한다."""
        return _count(self._call("verify_poms", build))

    def add_pom_verify(self, file: str, note: str, seen: str = "restructure") -> Any:
        """sbmig가 고친 pom 하나를 항목 하나로 기록한다. 상태는 ``pom-verify``이고 다음 빌드에서 확인한다.
        ``seen``은 고친 단계다."""
        opt = {"seen": seen} if seen != "restructure" else None
        e = self._call("add_pom_verify", file, note, optional=opt)
        if self.active:
            self.pom_verify_added += 1
        return e

    def size(self) -> int:
        """오류 목록의 항목 수를 반환한다. 로그 줄의 ``#n``에 쓴다. 목록이 없으면 0을 반환한다."""
        return len(self.entries()) if self.active else 0

    def add_note(self, file: str, text: str, seen: str = "review") -> Any:
        """구조 검토의 warning이나 고치지 못한 critical을 메모 항목(상태 ``note``)으로 기록한다."""
        return self._call("add_note", file, text, seen)

    def review_fixed(self, file: str, text: str, **try_fields: Any) -> Any:
        """구조 검토가 직접 고친 critical 문제 하나를 기록하고 항목을 반환한다.
        케이스 ``REVIEW_L2``의 항목 하나와 ``review`` 시도 하나를 만들고, 바로 해결로 닫는다.

        The try is round 0."""
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

    # ---------------------------------------------------------------- 시도
    def begin_try(self, case_id: str, round: int, source: str, script_version: int = 0) -> Any:
        """케이스의 항목에 새 시도(FixTry)를 열고 반환한다. 시도 번호는 ``n``이다. 오류 목록이 없으면 None을 반환한다."""
        return self._call("begin_try", case_id, round, source, script_version=script_version)

    def finish_try(self, case_id: str, n: int, *, optional: Optional[dict[str, Any]] = None, **fields: Any) -> None:
        """시도 기록을 채운다. 원인, 해결 방법, 적용했는지, 수정 파일, diff, 커밋, 결과, LLM 호출 수, 걸린 시간 등이다.
        적용 직후에 한 번 호출한다. 시도 결과를 평가하거나 되돌린 뒤에 다시 호출한다.
        목록이 받지 않는 선택 필드는 뺀다.

        Optional fields: `OPTIONAL_TRY_FIELDS` (edits = the 수정 notes, major_upgrades / verify / verify_source)
        plus whatever `optional` carries (e.g. script_version)."""
        opt = dict(optional or {})
        for k in OPTIONAL_TRY_FIELDS:
            if k in fields:
                opt[k] = fields.pop(k)
        self._call("finish_try", case_id, n, optional=opt, **fields)

    def resolve(self, case_id: str, round: int, n: int) -> None:
        """그 시도로 오류가 사라진 케이스 항목을 해결로 닫는다. 해결 표시는 ``r<round> try <n>``이다.
        시도 결과가 개선, 성공, 부분 해결일 때 호출한다."""
        self._call("resolve", case_id, round, n)

    def block(self, case_id: str, reason: str) -> None:
        """케이스의 열린 항목을 차단으로 닫는다. 이 항목은 보고서의 인수인계 섹션에 나온다."""
        self._call("block", case_id, reason)

    # ---------------------------------------------------------------- 조회, 마무리
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
        """상태(open, pom-verify, resolved, blocked, unresolved, note)별 항목 수를 반환한다."""
        got = self._call("counts", default=None)
        if isinstance(got, dict):
            return {str(k): int(v) for k, v in got.items()}
        out: dict[str, int] = {}
        for e in self.entries():
            st = str(getattr(e, "status", "") or "")
            out[st] = out.get(st, 0) + 1
        return out

    def open_count(self) -> Optional[int]:
        """아직 열린 항목 수를 반환한다. 오류 목록이 없으면 None이다. 그때 로그에는 빌드의 오류 수를 쓴다."""
        return self.counts().get("open", 0) if self.active else None

    def note_open(self, text: str) -> int:
        """열린 항목마다 ``note``에 ``text``를 덧붙이고 그 항목 수를 반환한다. 같은 문장은 한 번만 붙인다.

        The list writes the notes at finalize."""
        n = 0
        for e in self.entries():
            if str(getattr(e, "status", "") or "") != "open":
                continue
            try:
                note = str(getattr(e, "note", "") or "")
                if text not in note:
                    e.note = f"{note}; {text}" if note else text
                n += 1
            except Exception as ex:  # noqa: BLE001 - 기록 실패로 실행을 멈추지 않는다
                log.warning("errors.note_open failed: %s: %s", type(ex).__name__, ex)
        return n

    def open_cases(self) -> dict:
        """열린 항목이 있는 케이스를 케이스 ID별 항목 목록으로 반환한다."""
        got = self._call("open_cases", default={})
        return dict(got) if isinstance(got, dict) else {}

    def entries_for(self, case_id: str) -> list:
        """한 케이스의 항목 목록을 반환한다."""
        return list(self._call("entries_for", case_id, default=[]) or [])

    def end_of_run(self) -> dict[str, int]:
        """실행이 끝날 때 아직 열린 항목을 unresolved로 닫고 상태별 개수를 반환한다.
        결과가 끝내 나오지 않은 시도의 항목도 여기서 닫는다."""
        got = self._call("end_of_run")
        return dict(got) if isinstance(got, dict) else self.counts()

    def write(self) -> dict[str, str]:
        """실행 디렉토리의 errors.jsonl과 errors.md를 새로 쓰고 쓴 파일 경로를 반환한다.

        Keys of the result: `errors`, `errors_md` (only for files that exist)."""
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
        """오류 목록을 기록하다 생긴 문제 목록을 반환한다. 마무리 단계에서 ``errors-failed: …`` 플래그로 붙는다."""
        return list(getattr(self._target, "problems", None) or [])

    def to_report(self) -> list:
        """보고서에 넣을 항목 목록을 만든다.
        diff는 자르고 as-is / to-be 변경 조각은 뺀다. report.json이 너무 커지지 않게 한다."""
        return list(self._call("to_report", default=[]) or [])


class CaseScripts(_Guarded):
    """KB 케이스 저장소(``kb.cases``)를 다루는 어댑터 클래스.
    재사용할 스크립트를 고르고, 스크립트와 실행 결과를 저장한다.
    KB가 없으면 재사용할 스크립트가 없다. LLM이 만든 스크립트는 저장하지 않고 v0로 실행한다."""
    _what = "kb.cases"

    def __init__(self, target: Any = None):
        self._target = target if target is not None and callable(getattr(target, "best", None)) else None

    @classmethod
    def of(cls, kb: Any) -> "CaseScripts":
        """``kb.cases``로 어댑터를 만든다. KB가 깨져 있어도 예외를 던지지 않고 비활성 어댑터를 반환한다."""
        try:
            return cls(getattr(kb, "cases", None))
        except Exception as e:  # noqa: BLE001 - 깨진 KB 때문에 수정 루프를 멈추지 않는다
            log.warning("kb.cases unavailable: %s: %s", type(e).__name__, e)
            return cls(None)

    def best(self, case_id: str) -> Optional[tuple[Any, str]]:
        """재사용할 스크립트 버전의 (CaseScript, 코드)를 반환한다.
        검증된(proven) 최신 버전을, 없으면 미검증(unverified) 최신 버전을 고른다.
        저장된 스크립트가 없으면 None을 반환한다. 그러면 수정 루프는 LLM에 새 스크립트를 요청한다."""
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
        """케이스 정보(case.json의 CaseInfo)를 반환한다. 없으면 None을 반환한다."""
        return self._call("find", case_id)

    def describe(self, case_id: str) -> str:
        """KB 조회 결과를 로그에 쓸 한 구절로 반환한다. 저장소가 알려 주지 못하면 빈 문자열을 반환한다.

        e.g. `저장된 스크립트 없음 (처음 보는 케이스)`, `저장된 스크립트 v2 (proven, 성공 3 / 실패 0, 프로젝트 2)`."""
        return str(self._call("describe", case_id, default="") or "")

    def add_script(self, case_id: str, l2: str, code: str, *, source: str, run_id: str, project: str,
                   analysis: str = "", solution: str = "", **optional: Any) -> Any:
        """스크립트를 케이스 폴더에 ``fix_v<N>.py``로 저장하고, CaseScript(버전, 파일, 상태)를 반환한다.
        저장에 실패하면 None을 반환한다. 같은 코드는 한 번만 저장한다.
        저장소가 받지 않는 이력 필드(``parent``, ``reason``)는 빼고 저장한다."""
        return self._call("add_script", case_id, l2, code, source=source, run_id=run_id, project=project,
                          analysis=analysis, solution=solution, optional=dict(optional))

    def credit(self, case_id: str, version: int, result: str, *, project: str, run_id: str, **optional: Any) -> bool:
        """저장된 스크립트 버전을 한 번 실행한 결과를 기록한다. KB가 없으면 False를 반환한다.
        성공, 실패 횟수와 프로젝트, 실행 기록(ScriptRun)을 남긴다.

        `optional` ScriptRun fields (reason, round, n, errors_before, errors_left, errors_new, files, scope,
        code, reused) are dropped when the store does not take them."""
        if not self.active:
            return False
        self._call("credit", case_id, version, result, project=project, run_id=run_id, optional=dict(optional))
        return True


    def mark_non_idempotent(self, case_id: str, version: int, note: str = "") -> bool:
        """실행할 때마다 프로젝트를 또 바꾸는 스크립트 버전을 비멱등으로 표시한다. 그 버전은 재사용하지 않는다.
        KB가 없거나 저장소에 이 기능이 없으면 False를 반환한다.

        `CaseStore.mark_non_idempotent`."""
        if not self.active or not callable(getattr(self._target, "mark_non_idempotent", None)):
            return False
        return self._call("mark_non_idempotent", case_id, version, optional={"note": note}) is not None


def script_path(case_id: str, cs: Any) -> str:
    """저장된 스크립트의 KB 안 경로(``cases/<case_id>/fix_v2.py`` 꼴)를 반환한다. 로그와 오류 항목에 쓴다."""
    f = str(getattr(cs, "file", "") or f"fix_v{int(getattr(cs, 'version', 0) or 0)}.py")
    return f if f.startswith("cases/") else f"cases/{case_id}/{f}"
