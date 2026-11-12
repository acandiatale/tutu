"""두 어댑터 단의 플러그인을 로드하는 모듈(docs/ADAPTERS.md).
회사 프레임워크는 sbmig 코드를 고치지 않고 여기서 연결된다.
provider가 `plugin`이거나 내장 provider가 아니면 `sbmig.fix.client.make_llm_client`가 `resolve_llm_plugin(cfg)`를
호출하고, 오케스트레이터는 처음 LLM 수정이 필요할 때 `resolve_fixer_plugin`을 호출한다.
- API 키는 `llm.api_key_env` 환경 변수에서만 읽고, 메모리의 options["api_key"]로만 넘긴다
- 2단 플러그인에는 `propose_script`가 있어야 하며, `propose`만 있는 fixer는 거부한다

Plugin loading for the two adapter tiers (see docs/ADAPTERS.md).

    load_plugin("pkg.module:attr")             -> the object (class, factory or instance)
    resolve_llm_plugin(cfg)                    -> LLMClientLike   (tier 1: llm.provider = plugin | <entry point>)
    resolve_fixer_plugin(cfg, llm, notes_dir)  -> fixer | None   (tier 2: fix.plugin; must provide propose_script)


"""
from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import BUILTIN_LLM_PROVIDERS, Config

from .adapter import AdapterClient, KwargFilterClient, is_request_adapter

log = logging.getLogger("sbmig.llm")

LLM_ENTRY_POINT_GROUP = "sbmig.llm_adapters"
FIXER_ENTRY_POINT_GROUP = "sbmig.fixers"


class PluginLoadError(ValueError):
    """플러그인을 import하거나 생성할 수 없거나 필요한 메서드가 없을 때 던지는 예외.
    ValueError를 상속하므로 설정 오류로 처리된다.

    A configured plugin cannot be imported, built, or does not implement the expected contract."""


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_plugin(spec: str) -> Any:
    """`"pkg.module:attr"` 또는 `"pkg.module.attr"`를 import해 그 객체를 반환한다.
    실패하면 원인을 담은 PluginLoadError를 던진다.

    Import `"pkg.module:attr"` (attr may be dotted, e.g. `Factory.create`) or the fallback `"pkg.module.attr"`."""
    spec = (spec or "").strip()
    if not spec:
        raise PluginLoadError("empty plugin spec (expected 'pkg.module:attr')")
    if ":" in spec:
        mod_name, _, attr_path = spec.partition(":")
    elif "." in spec:
        mod_name, _, attr_path = spec.rpartition(".")
    else:
        raise PluginLoadError(f"plugin spec {spec!r} must look like 'pkg.module:attr'")
    if not mod_name or not attr_path:
        raise PluginLoadError(f"plugin spec {spec!r} must look like 'pkg.module:attr'")
    try:
        obj: Any = importlib.import_module(mod_name)
    except Exception as exc:
        raise PluginLoadError(
            f"cannot import plugin module {mod_name!r} (from {spec!r}): {type(exc).__name__}: {exc} — "
            "is the package installed in the sbmig environment or on PYTHONPATH?") from exc
    for part in attr_path.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise PluginLoadError(f"plugin module {mod_name!r} has no attribute {attr_path!r} (from {spec!r})") from exc
    return obj


def _entry_points(group: str) -> list[Any]:
    try:
        eps = importlib.metadata.entry_points(group=group)
    except TypeError:  # pragma: no cover - very old importlib.metadata
        eps = importlib.metadata.entry_points().get(group, [])  # type: ignore[attr-defined]
    return list(eps)


def load_entry_point(name: str, group: str) -> Optional[Any]:
    """`group` 그룹에서 이름이 `name`인 entry point를 로드한다.
    없으면 None을 반환하고, 로드에 실패하면 PluginLoadError를 던진다."""
    for ep in _entry_points(group):
        if ep.name == name:
            try:
                return ep.load()
            except Exception as exc:
                raise PluginLoadError(f"entry point {name!r} in group {group!r} ({getattr(ep, 'value', '?')}) "
                                      f"failed to load: {type(exc).__name__}: {exc}") from exc
    return None


def instantiate(obj: Any, positional: list[Any], optional_kw: Optional[dict[str, Any]] = None, *,
                required_method: str, spec: str) -> Any:
    """플러그인 객체를 만든다.
    `required_method`가 있는 인스턴스는 그대로 쓰고, 클래스나 팩토리는 시그니처가 받는 인자만 넘겨 호출한다.
    필수 인자를 채울 수 없거나, 생성에 실패하거나, 결과에 `required_method`가 없으면 PluginLoadError를 던진다.

    Build a plugin object.

    - an instance that already has `required_method` → returned as is;
    - a class or factory → called with as many of `positional` as its signature accepts (e.g. `(options, cfg)`,
      `(options)` or `()`), plus each `optional_kw` entry whose name it declares explicitly."""
    optional_kw = dict(optional_kw or {})
    if not isinstance(obj, type) and callable(getattr(obj, required_method, None)):
        return obj
    if not callable(obj):
        raise PluginLoadError(f"plugin {spec!r} is neither a class, a factory nor an object with "
                              f"{required_method}()")
    args: list[Any] = list(positional)
    kwargs: dict[str, Any] = {}
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        params = list(sig.parameters.values())
        named = {p.name for p in params if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                      inspect.Parameter.KEYWORD_ONLY)}
        kwargs = {k: v for k, v in optional_kw.items() if k in named}
        pos = [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                               inspect.Parameter.POSITIONAL_OR_KEYWORD) and p.name not in kwargs]
        if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
            n = len(positional)
        else:
            n = min(len(positional), len(pos))
            required = [p for p in pos if p.default is inspect.Parameter.empty]
            if len(required) > len(positional):
                raise PluginLoadError(
                    f"plugin {spec!r} requires {len(required)} positional argument(s) "
                    f"({', '.join(p.name for p in required)}); sbmig passes at most {len(positional)}")
        missing_kw = [p.name for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY
                      and p.default is inspect.Parameter.empty and p.name not in kwargs]
        if missing_kw:
            raise PluginLoadError(f"plugin {spec!r} requires keyword argument(s) {missing_kw} that sbmig cannot "
                                  "provide")
        args = list(positional[:n])
    try:
        built = obj(*args, **kwargs)
    except PluginLoadError:
        raise
    except Exception as exc:
        raise PluginLoadError(f"plugin {spec!r} failed to initialise: {type(exc).__name__}: {exc}") from exc
    if not callable(getattr(built, required_method, None)):
        raise PluginLoadError(f"plugin {spec!r} returned {type(built).__name__}, which has no {required_method}()")
    return built


# ---------------------------------------------------------------------------
# tier 1: LLM client / adapter
# ---------------------------------------------------------------------------
def _unset(v: Optional[str]) -> bool:
    return not v or "${" in v or v == "default"


def llm_plugin_options(cfg: Config) -> dict[str, Any]:
    """`llm.plugin_options`를 반환하되, 키 환경 변수가 설정돼 있으면 `api_key`를 더한다.
    키는 메모리로만 넘긴다.

    `llm.plugin_options` + `api_key` from the `llm.api_key_env` variable when it is set (never written anywhere)."""
    opts = dict(cfg.llm.plugin_options or {})
    env = cfg.llm.api_key_env
    # 키는 환경 변수에서만 읽고 config, LLM 기록, 저널, 로그에는 남기지 않는다
    if env and os.environ.get(env):
        opts.setdefault("api_key", os.environ[env])
    return opts


def _llm_source(cfg: Config) -> tuple[Any, str]:
    """1단 플러그인을 어디서 가져올지 찾아 (객체, 설명 문자열)을 반환한다.
    provider가 `plugin`이면 `llm.plugin`을, 그 밖의 이름이면 같은 이름의 entry point를 먼저 찾는다.
    내장 provider 이름이거나 둘 다 없으면 등록 방법을 알려 주는 오류를 던진다."""
    llm = cfg.llm
    if llm.provider == "plugin":
        if not llm.plugin:
            raise PluginLoadError("llm.provider is 'plugin' but llm.plugin is not set "
                                  "(expected 'pkg.module:attr', e.g. 'mycompany.sbmig_adapter:make_adapter')")
        return load_plugin(llm.plugin), llm.plugin
    if llm.provider in BUILTIN_LLM_PROVIDERS:
        raise PluginLoadError(f"llm.provider {llm.provider!r} is built in, not a plugin")
    obj = load_entry_point(llm.provider, LLM_ENTRY_POINT_GROUP)
    if obj is None and llm.plugin:
        return load_plugin(llm.plugin), llm.plugin
    if obj is None:
        known = sorted(ep.name for ep in _entry_points(LLM_ENTRY_POINT_GROUP))
        raise PluginLoadError(
            f"unknown llm.provider {llm.provider!r}: not one of {', '.join(BUILTIN_LLM_PROVIDERS)} and no entry "
            f"point named {llm.provider!r} in group {LLM_ENTRY_POINT_GROUP!r}"
            + (f" (installed: {', '.join(known)})" if known else "")
            + ". Register it either (1) in config: llm.provider: plugin + llm.plugin: 'pkg.module:attr', or "
            f"(2) in your package's pyproject: [project.entry-points.\"{LLM_ENTRY_POINT_GROUP}\"] "
            f"{llm.provider} = \"pkg.module:attr\" (then pip install it next to sbmig)")
    return obj, f"{LLM_ENTRY_POINT_GROUP}:{llm.provider}"


def wrap_llm(obj: Any, cfg: Optional[Config] = None) -> Any:
    """생성한 어댑터나 클라이언트를 LLMClientLike로 맞춘다.
    어댑터는 AdapterClient로, 키워드 인자를 다 받지 못하는 클라이언트는 KwargFilterClient로 감싼다.

    Built adapter/client → LLMClientLike (adapters are wrapped in AdapterClient)."""
    if isinstance(obj, (AdapterClient, KwargFilterClient)):
        return obj
    if is_request_adapter(obj):
        default_model = None
        if cfg is not None and not _unset(cfg.llm.model):
            default_model = cfg.llm.model
        return AdapterClient(obj, default_model=default_model)
    if KwargFilterClient.needed(obj):
        return KwargFilterClient(obj)
    return obj


def resolve_llm_plugin(cfg: Config) -> Any:
    """설정에 따라 사내 LLM 플러그인(1단)을 로드하고 생성해 감싼 뒤 반환한다.

    Tier 1. `llm.provider: plugin` → `llm.plugin`; a non-builtin provider name → entry point of that name in
    `sbmig.llm_adapters` (falling back to `llm.plugin`). The object may be a class / factory taking
    `(options, cfg)`, `(options)` or `()`, or a ready instance; adapters (`complete(request)`) are wrapped."""
    obj, spec = _llm_source(cfg)
    built = instantiate(obj, [llm_plugin_options(cfg), cfg], required_method="complete", spec=spec)
    client = wrap_llm(built, cfg)
    log.info("llm plugin %s → %s", spec, type(client).__name__)
    return client


# ---------------------------------------------------------------------------
# tier 2: fixer
# ---------------------------------------------------------------------------
def resolve_fixer_plugin(cfg: Config, llm: Any = None, notes_dir: Optional[str | Path] = None) -> Optional[Any]:
    """`fix.plugin`에 지정된 사내 fixer(2단)를 로드해 반환한다. 설정이 없으면 None을 반환한다.
    `propose_script`가 반드시 있어야 하고, 스크립트가 스스로 적용되므로 `apply`는 필요 없다.

    Tier 2. `fix.plugin` → the in-house fixer, or None when not configured. The plugin implements the script seam
    (`ScriptFixerLike`): `propose_script(ws, cluster, ctx) -> ScriptProposal` — one Python fix script per case; the
    script applies itself (sbmig.script), so there is no `apply`. Factory signature `(options, cfg, llm)` (any prefix
    of it), optionally with a `notes_dir` keyword. A plain name without ':' or '.' is looked up as an entry point in
    group `sbmig.fixers`. A plugin without `propose_script` (a SEARCH/REPLACE-only `propose` fixer) is refused."""
    spec = (cfg.fix.plugin or "").strip()
    if not spec:
        return None
    if ":" not in spec and "." not in spec:
        obj = load_entry_point(spec, FIXER_ENTRY_POINT_GROUP)
        if obj is None:
            raise PluginLoadError(f"fix.plugin {spec!r}: no entry point of that name in group "
                                  f"{FIXER_ENTRY_POINT_GROUP!r}; use 'pkg.module:attr'")
    else:
        obj = load_plugin(spec)
    optional = {"notes_dir": notes_dir} if notes_dir is not None else {}
    return instantiate(obj, [dict(cfg.fix.plugin_options or {}), cfg, llm], optional,
                       required_method="propose_script", spec=spec)


__all__ = ["PluginLoadError", "load_plugin", "load_entry_point", "instantiate", "resolve_llm_plugin",
           "resolve_fixer_plugin", "wrap_llm", "llm_plugin_options",
           "LLM_ENTRY_POINT_GROUP", "FIXER_ENTRY_POINT_GROUP"]
