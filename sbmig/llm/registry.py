"""Plugin loading for the two adapter tiers (see docs/ADAPTERS.md).

    load_plugin("pkg.module:attr")             -> the object (class, factory or instance)
    resolve_llm_plugin(cfg)                    -> LLMClientLike   (tier 1: llm.provider = plugin | <entry point>)
    resolve_fixer_plugin(cfg, llm, notes_dir)  -> FixerLike | None (tier 2: fix.plugin)
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
    """A configured plugin cannot be imported, built, or does not implement the expected contract."""


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_plugin(spec: str) -> Any:
    """Import `"pkg.module:attr"` (attr may be dotted, e.g. `Factory.create`) or the fallback `"pkg.module.attr"`."""
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
    """Build a plugin object.

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
    """`llm.plugin_options` + `api_key` from the `llm.api_key_env` variable when it is set (never written anywhere)."""
    opts = dict(cfg.llm.plugin_options or {})
    env = cfg.llm.api_key_env
    if env and os.environ.get(env):
        opts.setdefault("api_key", os.environ[env])
    return opts


def _llm_source(cfg: Config) -> tuple[Any, str]:
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
    """Built adapter/client → LLMClientLike (adapters are wrapped in AdapterClient)."""
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
    """Tier 1. `llm.provider: plugin` → `llm.plugin`; a non-builtin provider name → entry point of that name in
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
class ApplyDelegatingFixer:
    """Adds `apply` (our atomic PatchApplier) to a fixer plugin that only implements `propose`."""

    def __init__(self, inner: Any):
        self.inner = inner

    def propose(self, ws, cluster, ctx):
        return self.inner.propose(ws, cluster, ctx)

    def apply(self, ws, changes):
        from sbmig.fix.apply import PatchApplier

        return PatchApplier(ws).apply(list(changes))

    def __getattr__(self, item: str) -> Any:
        if item == "inner":
            raise AttributeError(item)
        return getattr(self.inner, item)


def resolve_fixer_plugin(cfg: Config, llm: Any = None, notes_dir: Optional[str | Path] = None) -> Optional[Any]:
    """Tier 2. `fix.plugin` → FixerLike, or None when not configured. Factory signature
    `(options, cfg, llm)` (any prefix of it), optionally with a `notes_dir` keyword. A plain name without ':' or
    '.' is looked up as an entry point in group `sbmig.fixers`."""
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
    fixer = instantiate(obj, [dict(cfg.fix.plugin_options or {}), cfg, llm], optional,
                        required_method="propose", spec=spec)
    if not callable(getattr(fixer, "apply", None)):
        fixer = ApplyDelegatingFixer(fixer)
    return fixer


__all__ = ["PluginLoadError", "load_plugin", "load_entry_point", "instantiate", "resolve_llm_plugin",
           "resolve_fixer_plugin", "wrap_llm", "llm_plugin_options", "ApplyDelegatingFixer",
           "LLM_ENTRY_POINT_GROUP", "FIXER_ENTRY_POINT_GROUP"]
