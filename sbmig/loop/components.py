"""다른 패키지(워크스트림)의 협력 객체를 처음 쓸 때 불러오는 도구와, 없을 때 대신 서는 null / lazy 대역들.
오케스트레이터가 쓴다: ``lazy_attr`` 로 analyze / plan / rewrite / build / fix / kb 를 늦게 import 하고(없으면
``MissingComponentError`` 로 무엇이 빠졌는지 말한다), ``LazyFixer`` 는 첫 수정 스크립트를 요청할 때에야 fixer
와 LLM 클라이언트를 만들고, ``LazyScriptRunner`` 는 ``sbmig.script.ScriptRunner`` 를 만들 수 없으면 매번
``error`` 결과를 돌려주며(예외 없음 → 루프는 실패한 시도로 기록), ``NullKnowledgeBase`` 는 ``--no-kb`` 일 때의
빈 지식베이스다. 결정: v2 는 모든 오류를 수정 스크립트로 고친다 → ``propose_script`` 가 없는 fixer(v1 용
tier-2 플러그인)는 ``ScriptFixerMissing``.

Lazy resolution of collaborators from other workstreams + null/lazy stand-ins."""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

from sbmig.core.models import KBMatch, ScriptRunResult

log = logging.getLogger("sbmig.loop")


class MissingComponentError(RuntimeError):
    """협력 패키지(다른 워크스트림)가 없거나 깨졌다. 오케스트레이터는 이것을 실행 중단(abort) 사유로
    기록한다.

    A collaborator package (another workstream) is missing or broken."""


def lazy_attr(module: str, attr: str, workstream: str) -> Any:
    """``module.attr`` 을 지금 import 해서 돌려준다. 실패하면 어느 워크스트림의 무엇이 빠졌는지 담은
    ``MissingComponentError`` (import 자체가 깨진 경우와 이름이 아직 없는 경우를 구분해서 말한다)."""
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
    """v2: fixer(v1 용으로 쓴 tier-2 ``fix.plugin``)에 ``propose_script`` 가 없어 수정 스크립트를 쓸 수
    없다. 루프는 이를 ``fixer-without-scripts`` 플래그 +
    llm_unavailable 차단으로 처리한다(저장된 스크립트는 그대로 재사용).

    v2: the fixer (a tier-2 `fix.plugin` written for v1) has no `propose_script` — it cannot write fix scripts."""


class NullKnowledgeBase:
    """아무것도 모르는 지식베이스(``--no-kb``, 또는 sbmig.kb 를 못 불러올 때): 조회는 빈 결과, 재사용할
    스크립트 없음(LLM 스크립트는 저장 없이 v0 로 실행), 기록하지
    않음(오류 목록은 실행 디렉터리의 errors.jsonl 에만 남는다).

    The knowledge base that knows nothing (``--no-kb``, or sbmig.kb unavailable) — what the v2.1 flow calls on a KB:
    `match` (an empty KBMatch: no pinned case actions, no hints), `cases` None (nothing is reused, LLM scripts run
    unstored as v0) and `record_run` (nothing recorded: the run's errors.jsonl stays in the run directory)."""

    cases = None

    def match(self, cluster, ws, profile) -> KBMatch:
        """빈 조회 결과(고정된 케이스 결정도, 힌트도 없음)."""
        return KBMatch()

    def record_run(self, entries) -> int:
        """아무것도 기록하지 않는다(0건)."""
        return 0


class LazyFixer:
    """진짜 fixer(와 LLM 클라이언트)를 첫 수정 스크립트 요청 때에야 만드는 대리 객체. LLM 이 필요 없는
    실행(바로 빌드 성공, 저장본 재사용만)은 LLM 설정이 없어도 끝까지 돈다.

    Fixer proxy that creates the real fixer (and its LLM client) only when the first fix script is asked for."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._fixer: Any = None

    def _get(self) -> Any:
        if self._fixer is None:
            self._fixer = self._factory()
        return self._fixer

    def propose_script(self, ws, cluster, ctx):
        """진짜 fixer 의 ``propose_script`` 로 넘긴다. 없으면(v1 용 tier-2 플러그인) ``ScriptFixerMissing``.

        A fixer without `propose_script` (a tier-2 plugin written for v1) raises ScriptFixerMissing: v2 fixes every
        error with a fix script (docs/INTERFACES.md "Seams")."""
        fx = self._get()
        fn = getattr(fx, "propose_script", None)
        if not callable(fn):
            raise ScriptFixerMissing(f"the fixer {type(fx).__name__} has no propose_script — v2 fixes every error with "
                                     "a fix script; a fix.plugin must implement propose_script(ws, cluster, ctx)")
        return fn(ws, cluster, ctx)

    # v2.1d: 운영자 확인 가이드 호출(verify_guide)과 사용량 카운터도 진짜 fixer 로 넘긴다. 일부러 명시
    # 메서드만 둔다 — 만능 __getattr__ 를 두면 hasattr 같은 탐색만으로도 LLM 클라이언트가
    # 만들어진다(docs/INTERFACES.md v2.1d).
    # v2.1d: the operator-guide call (VerifyGuideLike) and its usage counters pass through to the real fixer, so the
    # loop sees them on the proxy as it would on an LLMFixer (a fixer without them → AttributeError, as before)
    def verify_guide(self, ws, cluster, ctx, changes, diff, **kw):
        """메이저 업그레이드의 운영자 확인 가이드(LLM 1회)를 진짜 fixer 에 요청한다. 없으면 AttributeError."""
        fx = self._get()
        fn = getattr(fx, "verify_guide", None)
        if not callable(fn):
            raise AttributeError(f"the fixer {type(fx).__name__} has no verify_guide")
        return fn(ws, cluster, ctx, changes, diff, **kw)

    @property
    def last_verify_usage(self):
        """마지막 verify_guide 호출의 사용량(LLMUsage). 아직 fixer 가 없으면 None(만들지 않는다)."""
        return getattr(self._fixer, "last_verify_usage", None) if self._fixer is not None else None

    @property
    def verify_usage(self):
        """verify_guide 호출들의 누적 사용량. fixer 를 아직 안 만들었으면 None."""
        return getattr(self._fixer, "verify_usage", None) if self._fixer is not None else None


def basic_profile_summary(profile) -> str:
    """프롬프트용 프로젝트 한 줄 요약의 기본판(케이스 타입·Java·Boot·Spring 버전·pom 수·라이브러리 20개)."""
    if profile is None:
        return ""
    libs = ", ".join(sorted({lib.name for lib in profile.libraries})[:20])
    return (f"project {profile.name}: case {profile.case_type.value}, java {profile.java_version or '?'}, "
            f"spring boot {profile.spring_boot_version or '-'}, spring {profile.spring_version or '-'}, "
            f"{len(profile.poms)} pom(s){', multi-module' if profile.is_multi_module else ''}; libs: {libs or '-'}")


def profile_summary(profile) -> str:
    """``sbmig.fix.profile_summary`` 가 있으면 그것, 없거나 실패하면 ``basic_profile_summary``."""
    try:
        fn = importlib.import_module("sbmig.fix").profile_summary  # type: ignore[attr-defined]
        return fn(profile)
    except Exception:
        return basic_profile_summary(profile)


class LazyScriptRunner:
    """스크립트 실행기 대리 객체(v2): 처음 쓸 때 ``sbmig.script.ScriptRunner(cfg)`` 를 만든다. 못 만들면 그
    이유를 매 실행의 ``error`` 결과로 돌려준다 — 예외를
    올리지 않으므로 루프는 실패한 시도로 기록하고 계속 간다.

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
        """스크립트 정적 검사(금지된 import·호출 등)의 문제 목록. 실행기가 없으면 그 이유 한 줄."""
        r = self._get()
        return [self._error or "script runner unavailable"] if r is None else list(r.check(code))

    def run(self, ws, code: str, *, errors=(), timeout_s: Optional[int] = None) -> ScriptRunResult:
        """스크립트를 별도 ``python -I`` 프로세스에서 돌린다(``errors``
        = 케이스의 오류들, 스크립트에서 ``p.errors``)."""
        r = self._get()
        if r is None:
            return ScriptRunResult(status="error", error=self._error or "script runner unavailable")
        return r.run(ws, code, errors=errors, timeout_s=timeout_s)
