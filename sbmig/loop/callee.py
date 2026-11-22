"""호출 대상이 키워드 인자를 받는지 시그니처로 미리 판단하는 모듈.
빌드처럼 다시 실행하면 안 되는 호출에서 TypeError가 난 뒤 키워드를 빼고 다시 호출하지 않으려고 쓴다.
같은 대상은 한 번만 검사한다.

The call sites that pass a newer keyword to a builder (`skip_tests`, `extra_args`) used to retry without it on
`TypeError`; a TypeError raised INSIDE a real build then started a second Maven build. They now ask
`takes_keyword` before the one call. The answer is computed once per callee (a bound method is keyed by its
function and its instance's class, so every access to `runner.run` hits the same entry). A callee whose signature
does not inspect is taken to accept the keyword (the current interface), so the call happens once and any
TypeError travels.

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
_DEPTH = 8                                      # base methods followed at most (a cycle is impossible, a bound is cheap)


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
    except Exception:  # noqa: BLE001 - builtins / C callables / a broken __signature__: the current interface
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

    True when `fn` accepts the keyword `name` (by name, or through `**kwargs`; see the module text for a method that
    forwards `**kwargs` to the base method it overrides). Bound methods, `functools.partial`, mocks and callable
    objects are read with `inspect.signature`; an uninspectable callee counts as accepting. Cached per callee
    (weakly; a callee that cannot be weakly referenced is checked every time). Never raises."""
    key = getattr(fn, "__func__", fn)
    owner = getattr(fn, "__self__", None)
    sub = (name, type(owner) if owner is not None and getattr(fn, "__func__", None) is not None else None)
    try:
        with _LOCK:
            hit = _SEEN.get(key)
            if hit is not None and sub in hit:
                return hit[sub]
    except TypeError:                                   # not weakly referenceable / not hashable
        return _decide(fn, name)
    ok = _decide(fn, name)
    try:
        with _LOCK:
            _SEEN.setdefault(key, {})[sub] = ok
    except TypeError:
        pass
    return ok
