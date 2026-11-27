"""호출 대상이 어떤 키워드 인자를 받는지 시그니처로 미리 확인하는 모듈.
빌드처럼 다시 실행하면 안 되는 호출을 TypeError 때문에 한 번 더 실행하지 않게 한다.

Call sites that pass a newer keyword to a builder (`skip_tests`, `extra_args`) ask `takes_keyword` before the
one call, so a TypeError raised INSIDE a real build is never answered by a second Maven build. The answer is
computed once per callee (a bound method is keyed by its function and its instance's class, so every access to
`runner.run` hits the same entry). A callee whose signature does not inspect is taken to accept the keyword
(the current interface), so the call happens once and any TypeError travels.

`**kwargs`: a callee that takes the keyword only through `**kwargs` accepts it (a wrapper such as the remediation's
`_DeadlineRunner.run(goal, **kw)` forwards it to the real runner). One exception: a method that overrides a method
of the same name in a base class of its instance and reaches the keyword only through `**kwargs` is read as
forwarding to that base method (`super().run(recipes, **kw)`), so the base method's signature decides.
"""
from __future__ import annotations

import inspect
import threading
import weakref
from collections.abc import Callable
from typing import Any

_SEEN: weakref.WeakKeyDictionary[Any, dict[tuple[str, Any], bool]] = weakref.WeakKeyDictionary()
_LOCK = threading.Lock()
_DEPTH = 8                                      # 따라가는 기반 클래스 메서드 수 상한


def _overridden(fn: Any) -> Any | None:
    """메서드가 덮어쓴 기반 클래스의 같은 이름 메서드를 인스턴스에 묶어 반환한다. 없으면 None이다."""
    obj, func = getattr(fn, "__self__", None), getattr(fn, "__func__", None)
    if obj is None or func is None or isinstance(obj, type) or not inspect.isfunction(func):
        return None
    attr = func.__name__
    mro = type(obj).__mro__
    at = next((i for i, cls in enumerate(mro) if cls.__dict__.get(attr) is func), None)
    if at is None:
        return None
    for cls in mro[at + 1:]:
        if attr in cls.__dict__:
            base = cls.__dict__[attr]
            return base.__get__(obj, type(obj)) if inspect.isfunction(base) else None
    return None


def _decide(fn: Callable[..., Any], name: str, depth: int = 0) -> bool:
    try:
        sig = inspect.signature(fn)
    except Exception:  # noqa: BLE001 - 시그니처를 읽지 못하면 지금 인터페이스라고 보고, 받는다고 판단한다
        return True
    p = sig.parameters.get(name)
    if p is not None and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
        return True
    if not any(q.kind is inspect.Parameter.VAR_KEYWORD for q in sig.parameters.values()):
        return False
    base = _overridden(fn) if depth < _DEPTH else None
    return True if base is None else _decide(base, name, depth + 1)


def takes_keyword(fn: Callable[..., Any], name: str) -> bool:
    """``fn``이 ``name`` 키워드 인자를 받는지 반환한다. 시그니처를 읽지 못하면 받는다고 본다.
    같은 대상은 한 번만 검사하고 결과를 기억한다. 예외를 던지지 않는다.

    By name, or through `**kwargs` (see the module text for a method that forwards `**kwargs` to the base
    method it overrides). Cached weakly; a callee that cannot be weakly referenced is checked every time."""
    key = getattr(fn, "__func__", fn)
    owner = getattr(fn, "__self__", None)
    sub = (name, type(owner) if owner is not None and getattr(fn, "__func__", None) is not None else None)
    try:
        with _LOCK:
            hit = _SEEN.get(key)
            if hit is not None and sub in hit:
                return hit[sub]
    except TypeError:                                   # 약한 참조나 해시를 만들 수 없는 대상

        return _decide(fn, name)
    ok = _decide(fn, name)
    try:
        with _LOCK:
            _SEEN.setdefault(key, {})[sub] = ok
    except TypeError:
        pass
    return ok
