"""Run-wide registry of blocked clusters (docs/ERROR-HANDLING.md: "never loop on what we cannot fix, never hide it").

The orchestrator owns one registry per run and hands it to every BuildFixLoop, so a cluster blocked in the spring
phase is skipped in restructure/verify too. Keys are L1 signatures, plus the file (normalized to its ``src/…`` suffix
so the restructure move into ``module/comp-jar`` keeps the key) for compile errors.

``BlockedItem.suggestion`` is composed by :func:`compose_suggestion` from the reason, the attributed artifact
(``sbmig.build.classpath`` — optional, lazily imported) and ``cfg.artifacts.optional``.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import re
from typing import Any, Iterable, Optional

from sbmig.core.models import (GAV, BlockedItem, BlockReason, BuildError, ErrorCluster, ErrorKind, Phase)

log = logging.getLogger("sbmig.loop")

FILE_KINDS = (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE)
MAX_ERRORS = 10
MAX_RAW = 2000


def normalize_file(path: Optional[str]) -> Optional[str]:
    """'module/comp-jar/src/main/java/a/B.java' → 'src/main/java/a/B.java' (stable across the restructure move)."""
    if not path:
        return path
    p = path.replace("\\", "/")
    m = re.search(r"(?:^|/)(src/(?:main|test)/.*)$", p)
    return m.group(1) if m else p


def error_key(e: BuildError) -> str:
    sig = e.signature or f"{e.kind.value}|{e.message}"
    if e.kind in FILE_KINDS and e.file:
        return f"{sig}@{normalize_file(e.file)}"
    return sig


def sanitize_errors(errors: Iterable[BuildError], limit: int = MAX_ERRORS) -> list[BuildError]:
    out: list[BuildError] = []
    for e in list(errors)[:limit]:
        raw = e.raw if len(e.raw or "") <= MAX_RAW else e.raw[:MAX_RAW] + " …"
        out.append(e.model_copy(update={"raw": raw}))
    return out


class BlockedRegistry:
    def __init__(self) -> None:
        self._items: dict[str, BlockedItem] = {}
        self._by_key: dict[str, str] = {}

    # -- queries -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items.values())

    def items(self) -> list[BlockedItem]:
        return list(self._items.values())

    def get(self, item_id: str) -> Optional[BlockedItem]:
        return self._items.get(item_id)

    def item_for_error(self, e: BuildError) -> Optional[BlockedItem]:
        iid = self._by_key.get(error_key(e))
        return self._items.get(iid) if iid else None

    def is_blocked_error(self, e: BuildError) -> bool:
        return error_key(e) in self._by_key

    def partition(self, cluster: ErrorCluster) -> tuple[list[BuildError], list[BuildError]]:
        """(errors already blocked, errors still to work on)."""
        blocked, rest = [], []
        for e in cluster.errors:
            (blocked if self.is_blocked_error(e) else rest).append(e)
        return blocked, rest

    def covering(self, cluster: ErrorCluster) -> Optional[BlockedItem]:
        """The (first) item blocking this cluster when *every* error of it is blocked, else None."""
        if not cluster.errors:
            return None
        items = [self.item_for_error(e) for e in cluster.errors]
        if any(i is None for i in items):
            return None
        return items[0]

    # -- mutation ------------------------------------------------------------
    def block(self, cluster: ErrorCluster, reason: BlockReason, phase: Phase, *,
              errors: Optional[list[BuildError]] = None, iteration: int = 0, attempts: int = 0,
              required_type: Optional[str] = None, found_type: Optional[str] = None,
              owner_class: Optional[str] = None, artifact: Optional[GAV] = None, suggestion: str = "",
              notes: Optional[list[str]] = None) -> tuple[BlockedItem, bool]:
        """Register (part of) a cluster as blocked. Returns (item, created). Errors already known to an item of the
        same reason are merged into it (one row per library problem, stable id across phases)."""
        errs = list(errors if errors is not None else cluster.errors) or list(cluster.errors)
        keys = [error_key(e) for e in errs]
        existing = next((self._items[self._by_key[k]] for k in keys if k in self._by_key
                         and self._items[self._by_key[k]].reason == reason), None)
        if existing is not None:
            self._merge(existing, errs, keys)
            existing.attempts = max(existing.attempts, attempts)
            return existing, False
        digest = hashlib.sha1(("|".join([reason.value, *sorted(set(keys))])).encode("utf-8")).hexdigest()[:10]
        files: list[str] = []
        for e in errs:
            f = e.file or (e.artifact.coordinates if e.artifact else None)
            if e.file and e.line:
                f = f"{e.file}:{e.line}"
            if f and f not in files:
                files.append(f)
        item = BlockedItem(id=f"blk-{digest}", reason=reason, phase=phase, cluster_id=cluster.id,
                           signatures=list(dict.fromkeys(e.signature or e.message for e in errs)), files=files,
                           errors=sanitize_errors(errs), artifact=artifact, owner_class=owner_class,
                           required_type=required_type, found_type=found_type, suggestion=suggestion,
                           attempts=attempts, first_seen_iteration=iteration, notes=list(notes or []))
        self._items[item.id] = item
        for k in keys:
            self._by_key.setdefault(k, item.id)
        return item, True

    def _merge(self, item: BlockedItem, errs: list[BuildError], keys: list[str]) -> None:
        for e, k in zip(errs, keys):
            if k not in self._by_key:
                self._by_key[k] = item.id
            sig = e.signature or e.message
            if sig not in item.signatures:
                item.signatures.append(sig)
            f = f"{e.file}:{e.line}" if e.file and e.line else (e.file or None)
            if f and f not in item.files:
                item.files.append(f)
            if len(item.errors) < MAX_ERRORS and all(x.raw != e.raw or x.file != e.file for x in item.errors):
                item.errors += sanitize_errors([e], 1)

    def remove(self, item_id: str) -> Optional[BlockedItem]:
        item = self._items.pop(item_id, None)
        if item is not None:
            for k in [k for k, v in self._by_key.items() if v == item_id]:
                del self._by_key[k]
        return item


# ---------------------------------------------------------------------------------------------------------------------
# attribution (owner class → jar → GAV) — sbmig.build.classpath is optional (workstream G1)
# ---------------------------------------------------------------------------------------------------------------------
def default_locator() -> Any:
    try:
        return importlib.import_module("sbmig.build.classpath")
    except Exception as e:  # not there yet / broken → attribution simply stays empty
        log.debug("sbmig.build.classpath unavailable: %s", e)
        return None


def attribute(locator: Any, ws, cfg, owner_class: Optional[str], errors: list[BuildError],
              module: Optional[str] = None) -> tuple[Optional[GAV], Optional[str], list[str]]:
    """Return (artifact, owner_class, notes) using `locator.locate_class` / `locator.locate_type_owner`."""
    notes: list[str] = []
    if locator is None:
        return None, owner_class, notes
    loc = None
    try:
        if owner_class and hasattr(locator, "locate_class"):
            loc = locator.locate_class(ws, cfg, owner_class, module)
        if loc is None and hasattr(locator, "locate_type_owner"):
            for e in errors[:3]:
                loc = locator.locate_type_owner(ws, cfg, e, module)
                if loc is not None:
                    break
    except Exception as e:
        notes.append(f"classpath attribution failed: {type(e).__name__}: {e}")
        return None, owner_class, notes
    if loc is None:
        return None, owner_class, notes
    if getattr(loc, "in_project", False):
        notes.append(f"owner class located in the project ({getattr(loc, 'jar', '')})")
        return None, owner_class, notes
    owner = getattr(loc, "owner_class", None) or getattr(loc, "fqcn", None) or owner_class
    jar = getattr(loc, "jar", None)
    if jar:
        notes.append(f"owner jar: {jar}")
    return getattr(loc, "artifact", None), owner, notes


# ---------------------------------------------------------------------------------------------------------------------
# suggestion text
# ---------------------------------------------------------------------------------------------------------------------
def _optional_spec(cfg, artifact: Optional[GAV]):
    if cfg is None or artifact is None:
        return None
    try:
        return cfg.optional_keys().get(artifact.key)
    except Exception:  # pragma: no cover
        return None


def compose_suggestion(item: BlockedItem, cfg=None, *, settings_file: Optional[str] = None,
                       repositories: Optional[list[str]] = None, extra: str = "") -> str:
    r = item.reason
    a = item.artifact
    if r in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
        what = (f"still takes `{item.required_type}`" if r == BlockReason.EXTERNAL_JAVAX_API and item.required_type
                else "exposes an API this code no longer matches")
        spec = _optional_spec(cfg, a)
        if a is not None and spec is not None:
            v = spec.version or "the configured version"
            return (f"`{spec.key}` is a configured optional artifact; its version bump to `{v}` happens in step "
                    f"spring.optional_bump — if this is still blocked after the bump, `{v}` itself still exposes "
                    f"javax: ask the library owner for a Jakarta build")
        if a is not None:
            owner = f" (class `{item.owner_class}`)" if item.owner_class else ""
            if r == BlockReason.EXTERNAL_JAVAX_API:
                return (f"`{a.coordinates}`{owner} {what}; upgrade `{a.key}` to a Jakarta build (add it to "
                        f"artifacts.optional with that version) or wrap the call with a shim; left unchanged on purpose")
            return (f"`{a.coordinates}`{owner} {what}; upgrade `{a.key}` to a compatible version or adapt the call by "
                    f"hand; left unchanged on purpose")
        if item.owner_class:
            return (f"`{item.owner_class}` {what}; owner class `{item.owner_class}` could not be attributed to an "
                    f"artifact; check the module classpath (mvn dependency:tree) and upgrade that library"
                    + (" to a Jakarta build" if r == BlockReason.EXTERNAL_JAVAX_API else ""))
        return (f"a library API {what}" + (f" (project supplies `{item.found_type}`)" if item.found_type else "")
                + "; the owning class could not be determined — check the module classpath (mvn dependency:tree) "
                  "for a javax-based library and upgrade it to a Jakarta build")
    if r == BlockReason.ARTIFACT_NOT_FOUND:
        coords = a.coordinates if a else "the artifact"
        repos = ", ".join(repositories or []) or "the configured repositories"
        return (f"`{coords}` is not in any repository: proxy it in Nexus or fix the coordinates "
                f"(checked repositories: {repos}); the LLM is never asked to invent versions")
    if r == BlockReason.PARENT_NOT_FOUND:
        coords = a.coordinates if a else "the parent POM"
        s = settings_file or "default ~/.m2/settings.xml"
        repos = f" (checked: {', '.join(repositories)})" if repositories else ""
        return (f"parent `{coords}` is not resolvable{repos} with settings `{s}`: check build.settings_file / the "
                f"repository mirror and artifacts.parent; nothing else can build until it resolves")
    if r == BlockReason.PLUGIN_INCOMPATIBLE:
        p = extra or "the plugin"
        return (f"build plugin `{p}` fails on JDK 21: upgrade it (add a version floor to knowledge/versions.yml) or "
                f"remove/replace it; left unchanged on purpose")
    if r == BlockReason.UNSUPPORTED_SOURCE:
        return ("non-Java / generated sources (Kotlin, Groovy, Scala, protobuf/jaxb/openapi generators) are not "
                "migrated by sbmig: migrate this module or generator configuration by hand")
    if r == BlockReason.ENCODING:
        return "the source encoding could not be determined: set project.build.sourceEncoding or convert to UTF-8"
    if r == BlockReason.BUDGET_EXHAUSTED:
        return ("the run's LLM/time budget was used up before this cluster could be fixed: rerun with a higher "
                "loop.max_llm_calls / loop.max_total_minutes or fix it by hand")
    if r == BlockReason.LLM_UNAVAILABLE:
        return ("the LLM was unavailable for this run (handlers and recipes still ran): fix the LLM endpoint / key "
                "and rerun, or fix it by hand" + (f" — last error: {extra}" if extra else ""))
    if r == BlockReason.REPEATED_FAILURE:
        return (f"{item.attempts} fix attempt(s) did not resolve it; needs a human (see the fix attempts and the "
                "journal for what was tried)")
    if r == BlockReason.OSCILLATION:
        return "fixes kept toggling between the same error sets; needs a human (see the fix attempts)"
    return "needs a human"
