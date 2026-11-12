"""오케스트레이터가 쓰는 다른 패키지의 객체를 처음 필요할 때 불러오고, 불러올 수 없으면 대신 쓸 객체를 제공하는 모듈.
- ``lazy_attr``: analyze, plan, rewrite, build, fix, kb 패키지를 쓰는 시점에 import하고, 실패하면
  ``MissingComponentError``로 무엇이 빠졌는지 알려 준다
- ``LazyFixer``: 첫 수정 스크립트를 요청할 때 fixer와 LLM 클라이언트를 만든다
- ``LazyScriptRunner``: ``sbmig.script.ScriptRunner``를 만들 수 없으면 예외 대신 매번 ``error`` 결과를 반환한다
- ``NullKnowledgeBase``: sbmig.kb를 import할 수 없거나 ``sbmig.api.migrate(kb=False)``로 호출했을 때 쓰는 빈 KB
모든 오류는 수정 스크립트로 고치므로, ``propose_script``가 없는 fixer는 ``ScriptFixerMissing``으로 처리한다.

Lazy resolution of collaborators from other workstreams + null/lazy stand-ins."""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

from sbmig.core.models import KBMatch, ScriptRunResult

log = logging.getLogger("sbmig.loop")


class MissingComponentError(RuntimeError):
    """필요한 패키지가 없거나 import가 깨졌을 때 던지는 예외 클래스.
    오케스트레이터는 이것을 실행 중단(abort) 사유로 기록한다.

    A collaborator package (another workstream) is missing or broken."""


def lazy_attr(module: str, attr: str, workstream: str) -> Any:
    """``module.attr``을 지금 import해서 반환한다.
    실패하면 무엇이 빠졌는지 담은 ``MissingComponentError``를 던지며, 모듈 import가 깨진 경우와 이름이 없는
    경우를 구분해서 알려 준다."""
    try:
        mod = importlib.import_module(module)
    except Exception as e:
        raise MissingComponentError(
            f"{module}.{attr} (workstream {workstream}) cannot be imported: {type(e).__name__}: {e}") from e
    try:
        return getattr(mod, attr)
    except AttributeError as e:
        raise MissingComponentError(
            f"{module} (workstream {workstream}) does not expose '{attr}' yet — see docs/INTERFACES.md") from e


class ScriptFixerMissing(RuntimeError):
    """fixer에 ``propose_script``가 없어 수정 스크립트를 만들 수 없을 때 던지는 예외 클래스.
    수정 루프는 ``fixer-without-scripts`` 플래그를 붙이고 해당 케이스를 llm_unavailable로 차단한다.
    저장된 스크립트는 그대로 재사용한다.

    v2: the fixer (a tier-2 `fix.plugin` written for v1) has no `propose_script` — it cannot write fix scripts."""


class NullKnowledgeBase:
    """아무 데이터도 없는 빈 KB 클래스.
    sbmig.kb를 import할 수 없을 때, ``sbmig.api.migrate(kb=False)``로 호출했을 때, 테스트에서 쓴다.
    조회하면 항상 빈 결과를 주므로 재사용할 스크립트가 없다.
    LLM이 만든 스크립트는 저장하지 않고 v0로 실행하며, 오류 목록은 실행 디렉토리의 errors.jsonl에만 남는다.

    The knowledge base that knows nothing (sbmig.kb unavailable, `sbmig.api.migrate(kb=False)`, test stubs) — what the v2.1 flow calls on a KB:
    `match` (an empty KBMatch: no pinned case actions, no hints), `cases` None (nothing is reused, LLM scripts run
    unstored as v0) and `record_run` (nothing recorded: the run's errors.jsonl stays in the run directory)."""

    cases = None

    def match(self, cluster, ws, profile) -> KBMatch:
        """빈 조회 결과를 반환한다. 미리 정해 둔 케이스 결정도 힌트도 없다."""
        return KBMatch()

    def record_run(self, entries) -> int:
        """아무것도 기록하지 않고 0을 반환한다."""
        return 0


class LazyFixer:
    """실제 fixer와 LLM 클라이언트를 첫 수정 스크립트 요청 때 만드는 프록시 클래스.
    그래서 LLM이 필요 없는 실행은 LLM 설정이 없어도 끝까지 실행된다.

    Fixer proxy that creates the real fixer (and its LLM client) only when the first fix script is asked for."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._fixer: Any = None

    def _get(self) -> Any:
        if self._fixer is None:
            self._fixer = self._factory()
        return self._fixer

    def propose_script(self, ws, cluster, ctx):
        """실제 fixer의 ``propose_script``를 호출한다.
        fixer에 그 메서드가 없으면 ``ScriptFixerMissing``을 던진다.

        A fixer without `propose_script` (a tier-2 plugin written for v1) raises ScriptFixerMissing: v2 fixes every
        error with a fix script (docs/INTERFACES.md "Seams")."""
        fx = self._get()
        fn = getattr(fx, "propose_script", None)
        if not callable(fn):
            raise ScriptFixerMissing(f"the fixer {type(fx).__name__} has no propose_script — v2 fixes every error with "
                                     "a fix script; a fix.plugin must implement propose_script(ws, cluster, ctx)")
        return fn(ws, cluster, ctx)

    # 만능 __getattr__를 두면 hasattr 확인만으로도 LLM 클라이언트가 생성되므로
    # 운영자 확인 가이드 호출과 사용량 조회 메서드를 하나씩 명시해 실제 fixer에 전달한다
    # v2.1d: the operator-guide call (VerifyGuideLike) and its usage counters pass through to the real fixer, so the
    # loop sees them on the proxy as it would on an LLMFixer (a fixer without them → AttributeError, as before)
    def verify_guide(self, ws, cluster, ctx, changes, diff, **kw):
        """메이저 업그레이드에 대한 운영자 확인 가이드를 실제 fixer에 요청한다(LLM 1회 호출).
        fixer에 그 기능이 없으면 AttributeError가 난다."""
        fx = self._get()
        fn = getattr(fx, "verify_guide", None)
        if not callable(fn):
            raise AttributeError(f"the fixer {type(fx).__name__} has no verify_guide")
        return fn(ws, cluster, ctx, changes, diff, **kw)

    @property
    def last_verify_usage(self):
        """마지막 verify_guide 호출의 사용량(LLMUsage)을 반환한다.
        fixer가 아직 없으면 새로 만들지 않고 None을 반환한다."""
        return getattr(self._fixer, "last_verify_usage", None) if self._fixer is not None else None

    @property
    def verify_usage(self):
        """verify_guide 호출의 누적 사용량을 반환한다. fixer가 아직 없으면 None을 반환한다."""
        return getattr(self._fixer, "verify_usage", None) if self._fixer is not None else None


def basic_profile_summary(profile) -> str:
    """프롬프트에 넣을 프로젝트 한 줄 요약의 기본 버전을 만든다.
    케이스 타입, Java, Boot, Spring 버전, pom 수, 라이브러리 20개를 담는다."""
    if profile is None:
        return ""
    libs = ", ".join(sorted({lib.name for lib in profile.libraries})[:20])
    return (f"project {profile.name}: case {profile.case_type.value}, java {profile.java_version or '?'}, "
            f"spring boot {profile.spring_boot_version or '-'}, spring {profile.spring_version or '-'}, "
            f"{len(profile.poms)} pom(s){', multi-module' if profile.is_multi_module else ''}; libs: {libs or '-'}")


def profile_summary(profile) -> str:
    """프롬프트에 넣을 프로젝트 요약을 만든다.
    ``sbmig.fix.profile_summary``가 없거나 실패하면 ``basic_profile_summary``를 쓴다."""
    try:
        fn = importlib.import_module("sbmig.fix").profile_summary  # type: ignore[attr-defined]
        return fn(profile)
    except Exception:
        return basic_profile_summary(profile)


class LazyScriptRunner:
    """수정 스크립트 실행기를 처음 쓸 때 ``sbmig.script.ScriptRunner(cfg)``로 만드는 프록시 클래스.
    실행기를 만들 수 없으면 실행할 때마다 그 이유를 ``error`` 결과로 반환한다.
    예외를 던지지 않으므로 수정 루프는 실패한 시도로 기록하고 계속 진행한다.

    ScriptRunnerLike proxy (v2): the real `sbmig.script.ScriptRunner(cfg)` is built on first use; when it cannot
    be built, every run is an `error` result naming why (never raises — the loop records a failed attempt)."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._runner: Any = None
        self._error: Optional[str] = None

    def _get(self) -> Any:
        if self._runner is None and self._error is None:
            try:
                self._runner = self._factory()
            except Exception as e:  # noqa: BLE001 - reported on every run instead
                self._error = f"script runner unavailable: {type(e).__name__}: {e}"
                log.error("%s", self._error)
        return self._runner

    def check(self, code: str) -> list[str]:
        """스크립트를 정적 검사해 금지된 import나 호출 같은 문제 목록을 반환한다.
        실행기가 없으면 그 이유 한 줄을 반환한다."""
        r = self._get()
        return [self._error or "script runner unavailable"] if r is None else list(r.check(code))

    def run(self, ws, code: str, *, errors=(), timeout_s: Optional[int] = None) -> ScriptRunResult:
        """스크립트를 별도 ``python -I`` 프로세스에서 실행한다.
        ``errors``는 이 케이스의 오류 목록이며 스크립트에서는 ``p.errors``로 읽는다."""
        r = self._get()
        if r is None:
            return ScriptRunResult(status="error", error=self._error or "script runner unavailable")
        return r.run(ws, code, errors=errors, timeout_s=timeout_s)
