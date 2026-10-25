"""Run-wide registry of blocked clusters (docs/ERROR-HANDLING.md: "never loop on what we cannot fix, never hide it").

The orchestrator owns one registry per run and hands it to every BuildFixLoop, so a cluster blocked in the spring
phase is skipped in restructure/verify too. Keys are (L1 signature, file) — the file as the full project-relative
posix path for compile errors ('' for module / artifact errors). Round 10: the full path keeps MSA services apart
(svc-a's `SwaggerConfig.java` block never covers svc-b's file with the same relative path — services keep their
directories); the restructure move (`src/…` → `{name}-back/src/…`, `module/comp-jar/src/…`, merged modules) is
followed through :meth:`BlockedRegistry.note_moves`, which the orchestrator calls with the step's git renames, so a
file blocked before the move stays blocked after it (:meth:`canonical_file` maps an old path to its new one).

``BlockedItem.suggestion`` is composed by :func:`compose_suggestion` from the reason, the attributed artifact
(``sbmig.build.classpath`` — optional, lazily imported) and ``cfg.artifacts.optional``. After the final build,
:func:`fill_via` adds the library that drags a javax API jar in (``BlockedItem.via`` / ``via_direct``, from the
``sbmig.build.deptree`` inventory) and recomposes the suggestion (ladder (1)–(5), docs/ERROR-HANDLING.md X1).
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
MAX_MOVE_HOPS = 8                    # a file moved by several restructure steps (wrap, then split) — chain cap


def normalize_file(path: Optional[str]) -> Optional[str]:
    """'module/comp-jar/src/main/java/a/B.java' → 'src/main/java/a/B.java' (the `src/…` suffix). No longer the
    registry key (round 10: full paths + `note_moves`); kept for callers that want the suffix."""
    if not path:
        return path
    p = path.replace("\\", "/")
    m = re.search(r"(?:^|/)(src/(?:main|test)/.*)$", p)
    return m.group(1) if m else p


def posix_path(path: Optional[str]) -> str:
    """Project-relative posix form of a path ('' for none): backslashes → '/', no leading './'."""
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def error_key(e: BuildError) -> str:
    """String form of an error's registry key (signature, plus `@<file>` for compile errors) — full path, no move
    mapping; `BlockedRegistry.key` is the move-aware tuple the registry itself uses."""
    sig = e.signature or f"{e.kind.value}|{e.message}"
    if e.kind in FILE_KINDS and e.file:
        return f"{sig}@{posix_path(e.file)}"
    return sig


def sanitize_errors(errors: Iterable[BuildError], limit: int = MAX_ERRORS) -> list[BuildError]:
    out: list[BuildError] = []
    for e in list(errors)[:limit]:
        raw = e.raw if len(e.raw or "") <= MAX_RAW else e.raw[:MAX_RAW] + " …"
        out.append(e.model_copy(update={"raw": raw}))
    return out


Key = tuple[str, str]                # (L1 signature, file or '')


class BlockedRegistry:
    def __init__(self) -> None:
        self._items: dict[str, BlockedItem] = {}
        self._by_key: dict[Key, str] = {}
        self._moves: dict[str, str] = {}             # old project path → new one (restructure renames, round 10)

    # -- keys / moves --------------------------------------------------------
    def canonical_file(self, path: Optional[str]) -> str:
        """The current path of a file the run may have moved: posix form, then the restructure renames (chained)."""
        p = posix_path(path)
        for _ in range(MAX_MOVE_HOPS):
            nxt = self._moves.get(p)
            if not nxt or nxt == p:
                break
            p = nxt
        return p

    def key(self, e: BuildError) -> Key:
        sig = e.signature or f"{e.kind.value}|{e.message}"
        if e.kind in FILE_KINDS and e.file:
            return sig, self.canonical_file(e.file)
        return sig, ""

    def note_moves(self, moves: dict[str, str]) -> int:
        """Files moved by a restructure step (`old → new`, project-relative): blocked keys follow them. Returns how
        many keys were re-keyed. MSA / boot_pom runs keep their directories → nothing to note."""
        clean = {posix_path(a): posix_path(b) for a, b in (moves or {}).items() if a and b and a != b}
        if not clean:
            return 0
        self._moves.update(clean)
        moved = 0
        for (sig, f), iid in list(self._by_key.items()):
            if not f:
                continue
            nf = self.canonical_file(f)
            if nf != f:
                del self._by_key[(sig, f)]
                self._by_key.setdefault((sig, nf), iid)
                moved += 1
        return moved

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
        iid = self._by_key.get(self.key(e))
        return self._items.get(iid) if iid else None

    def is_blocked_error(self, e: BuildError) -> bool:
        return self.key(e) in self._by_key

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
        keys = [self.key(e) for e in errs]
        existing = next((self._items[self._by_key[k]] for k in keys if k in self._by_key
                         and self._items[self._by_key[k]].reason == reason), None)
        if existing is not None:
            self._merge(existing, errs, keys)
            existing.attempts = max(existing.attempts, attempts)
            return existing, False
        flat = sorted({f"{s}@{f}" if f else s for s, f in keys})
        digest = hashlib.sha1(("|".join([reason.value, *flat])).encode("utf-8")).hexdigest()[:10]
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

    def _merge(self, item: BlockedItem, errs: list[BuildError], keys: list[Key]) -> None:
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
# via: the library that drags a javax API jar in (sbmig.build.deptree inventory, 2026-10-22)
# ---------------------------------------------------------------------------------------------------------------------
# javax package → the API artifact that ships it (when the attribution found no jar, the required type still says
# which API the library expects); the other groups of the same API family count as the same jar
JAVAX_PACKAGE_ARTIFACTS: dict[str, str] = {
    "javax.persistence": "javax.persistence:javax.persistence-api",
    "javax.servlet": "javax.servlet:javax.servlet-api",
    "javax.validation": "javax.validation:validation-api",
    "javax.annotation": "javax.annotation:javax.annotation-api",
    "javax.transaction": "javax.transaction:javax.transaction-api",
    "javax.inject": "javax.inject:javax.inject",
    "javax.xml.bind": "javax.xml.bind:jaxb-api",
    "javax.mail": "javax.mail:javax.mail-api",
    "javax.jms": "javax.jms:javax.jms-api",
    "javax.websocket": "javax.websocket:javax.websocket-api",
    "javax.el": "javax.el:javax.el-api",
}
_API_FAMILY_GROUPS: dict[str, tuple[str, ...]] = {
    "javax.persistence": ("javax.persistence", "org.hibernate.javax.persistence", "jakarta.persistence"),
    "javax.servlet": ("javax.servlet", "jakarta.servlet"),
    "javax.validation": ("javax.validation", "jakarta.validation"),
    "javax.annotation": ("javax.annotation", "jakarta.annotation"),
    "javax.transaction": ("javax.transaction", "jakarta.transaction"),
    "javax.inject": ("javax.inject", "jakarta.inject"),
    "javax.xml.bind": ("javax.xml.bind", "jakarta.xml.bind"),
    "javax.mail": ("javax.mail", "com.sun.mail", "jakarta.mail"),
    "javax.jms": ("javax.jms", "jakarta.jms"),
    "javax.websocket": ("javax.websocket", "jakarta.websocket"),
    "javax.el": ("javax.el", "jakarta.el"),
}
_VIA_REASONS = (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE)


def is_javax_api_artifact(gav: Optional[GAV]) -> bool:
    """A javax EE API jar: group `javax.*` / `org.hibernate.javax.persistence` / `com.sun.mail`, or a Jakarta EE 8
    re-publication (`jakarta.*:*-api` 1.x / 2.x — servlet 4.x, el 3.x — still the javax namespace)."""
    if gav is None:
        return False
    g = gav.group_id
    if g.startswith("javax.") or g == "javax" or g.startswith("org.hibernate.javax.persistence") \
            or g.startswith("com.sun.mail"):
        return True
    if g.startswith("jakarta.") and gav.artifact_id.endswith("-api") and gav.version:
        major = gav.version.split(".", 1)[0]
        if major in ("1", "2") or (major == "4" and "servlet" in gav.artifact_id) \
                or (major == "3" and gav.artifact_id == "jakarta.el-api"):
            return True
    return False


def _javax_package(item: BlockedItem) -> Optional[str]:
    for t in (item.required_type, item.owner_class, item.found_type):
        if not t or not t.startswith("javax."):
            continue
        best = max((p for p in JAVAX_PACKAGE_ARTIFACTS if t == p or t.startswith(p + ".")), key=len, default=None)
        if best:
            return best
    return None


def _item_module(item: BlockedItem) -> Optional[str]:
    return next((e.module for e in item.errors if e.module), None)


def _jar_module_names(jar: Any) -> set[str]:
    names = {getattr(jar, "module", None) or ""}
    mod = getattr(jar, "module", None)
    if mod:
        names.add(mod.rstrip("/").rsplit("/", 1)[-1])
    path = list(getattr(jar, "path", None) or [])
    if path:
        parts = path[0].split(":")
        if len(parts) > 1:
            names.add(parts[1])
    return {n for n in names if n}


def _jar_rank(jar: Any) -> int:
    via = getattr(jar, "via", None)
    if via is not None:
        try:
            from sbmig.core.jakarta_libraries import jakarta_library_for
            if jakarta_library_for(via.key) is not None:
                return 0
        except Exception:  # pragma: no cover - knowledge table unreadable
            pass
        return 1
    return 2 if getattr(jar, "declared", False) else 3


def match_javax_jar(item: BlockedItem, inventory: Iterable[Any]) -> Any:
    """The inventory entry (`JavaxApiJar`) for a blocked X1 / X2 item, or None. The item's artifact first (same
    key), else the API artifact of the javax package the error names; the item's module preferred."""
    jars = list(inventory or [])
    if not jars:
        return None
    a = item.artifact
    keys: set[str] = set()
    groups: tuple[str, ...] = ()
    if a is not None:
        keys.add(a.key)
    pkg = _javax_package(item)
    if pkg:
        keys.add(JAVAX_PACKAGE_ARTIFACTS[pkg])
        groups = _API_FAMILY_GROUPS.get(pkg, ())
    if not keys:
        return None
    cands = [j for j in jars if j.artifact.key in keys]
    if not cands and groups:
        cands = [j for j in jars if j.artifact.group_id in groups]
    if not cands:
        return None
    mod = _item_module(item)
    if mod:
        m = mod.strip("/")
        local = [j for j in cands if m in _jar_module_names(j) or m.rsplit("/", 1)[-1] in _jar_module_names(j)]
        cands = local or cands
    return sorted(cands, key=_jar_rank)[0]


def fill_via(items: Iterable[BlockedItem], inventory: Iterable[Any], cfg=None) -> int:
    """Set `via` / `via_direct` on X1 / X2 items whose artifact is None or a javax API jar (the library that drags
    the jar in, from the `dependency:tree` inventory) and recompose their suggestion. Returns the items changed."""
    inv = list(inventory or [])
    changed = 0
    for item in items:
        if item.reason not in _VIA_REASONS:
            continue
        if item.artifact is not None and not is_javax_api_artifact(item.artifact):
            continue
        jar = match_javax_jar(item, inv)
        if jar is not None:
            item.via = list(jar.path)
            item.via_direct = jar.via
            if item.artifact is None:
                item.artifact = jar.artifact.model_copy(update={"scope": None})
            if item.via_direct is not None:
                note = "via: " + " → ".join(item.via)
                if note not in item.notes:
                    item.notes.append(note)
        new = compose_suggestion(item, cfg, javax_jar=jar)
        if jar is not None or new != item.suggestion:
            changed += 1
        item.suggestion = new
    return changed


def _in_house_groups(cfg) -> list[str]:
    arts = getattr(cfg, "artifacts", None) if cfg is not None else None
    if arts is None:
        return []
    out: list[str] = []
    try:
        for s in [*arts.parent_specs(), *arts.mandatory, *arts.optional]:
            if s.group_id and s.group_id not in out:
                out.append(s.group_id)
    except Exception:  # pragma: no cover
        pass
    return out


def is_in_house(gav: Optional[GAV], cfg) -> bool:
    """Group equal to / under the configured parent's group, or any mandatory / optional / role-parent group."""
    if gav is None:
        return False
    g = gav.group_id
    return any(g == x or g.startswith(x + ".") for x in _in_house_groups(cfg))


def _module_label(item: BlockedItem, jar: Any) -> str:
    mod = getattr(jar, "module", None) if jar is not None else None
    if mod:
        return f"module `{mod}`"
    path = list(getattr(jar, "path", None) or item.via or [])
    if path:
        parts = path[0].split(":")
        if len(parts) > 1:
            return f"`{parts[1]}`"
    m = _item_module(item)
    return f"module `{m}`" if m else "the root module"


def _api_jar_text(item: BlockedItem, jar: Any) -> str:
    gav = getattr(jar, "artifact", None) if jar is not None else None
    gav = gav or item.artifact
    return gav.coordinates if gav is not None else "the javax API jar"


def _via_suggestion(item: BlockedItem, cfg, jar: Any = None) -> str:
    """Suggestion ladder (1)–(4) for an item whose javax API jar was traced in the dependency tree; "" otherwise."""
    via = item.via_direct
    declared = bool(getattr(jar, "declared", False)) if jar is not None else (
        via is None and len(item.via) == 2)
    if via is None and not declared:
        return ""
    api = _api_jar_text(item, jar)
    where = _module_label(item, jar)
    if via is not None:
        lib = None
        try:
            from sbmig.core.jakarta_libraries import jakarta_library_for
            lib = jakarta_library_for(via.key)
        except Exception as e:  # pragma: no cover - knowledge table unreadable
            log.debug("jakarta_libraries unavailable: %s", e)
        if lib is not None and via.key != lib.key:                                           # (1) relocated
            java = f", requires Java {lib.requires_java}" if lib.requires_java else ""
            return (f"`{via.coordinates}` is `{lib.key}` under its earlier coordinates → `{lib.key}:{lib.version}` "
                    f"(jakarta.* since {lib.jakarta_since}{java}); handler jakarta-library-floor relocates it when "
                    f"the pom declares it (it drags in `{api}`, direct dependency of {where})")
        if lib is not None:                                                                   # (1)
            java = f" (requires Java {lib.requires_java})" if lib.requires_java else ""
            note = (lib.note or "").split(";", 1)[0].strip()
            return (f"`{api}` comes through `{via.coordinates}` (direct dependency of {where}): `{lib.key}` moved to "
                    f"jakarta.* in {lib.jakarta_since} — raise it to {lib.version}{java}; handler "
                    f"jakarta-library-floor does this when the pom declares it" + (f" ({note})" if note else ""))
        if is_in_house(via, cfg):                                                             # (2)
            line = getattr(getattr(getattr(cfg, "artifacts", None), "parent", None), "version", None)
            on = f"on the {line} line" if line else "on the configured in-house version line"
            return (f"`{via.coordinates}` is an in-house artifact still compiled against javax (it drags in "
                    f"`{api}`): it needs a Jakarta build {on}; add it to `artifacts.optional` with that version so "
                    f"the optional bump moves it")
        return (f"`{api}` comes through `{via.coordinates}` (direct dependency of {where}), which is still compiled "  # (3)
                f"against javax: upgrade it to a Jakarta release (check its changelog) or replace it; left unchanged "
                f"on purpose")
    pom = f"`{jar.module}/pom.xml`" if jar is not None and getattr(jar, "module", None) else (
        "the root `pom.xml`" if jar is not None else f"the pom of {where}")
    return (f"`{api}` is declared in {pom} — remove it (its jakarta.* successor comes with the Spring Boot "           # (4)
            f"starters) — handler dependency-relocation does this after the spring recipes")


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
                       repositories: Optional[list[str]] = None, extra: str = "", javax_jar: Any = None) -> str:
    r = item.reason
    a = item.artifact
    if r in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
        what = (f"still takes `{item.required_type}`" if r == BlockReason.EXTERNAL_JAVAX_API and item.required_type
                else "exposes an API this code no longer matches")
        via_text = _via_suggestion(item, cfg, javax_jar)
        if via_text:
            return via_text
        spec = _optional_spec(cfg, a)
        if a is not None and spec is not None:
            v = spec.version or "the configured version"
            return (f"`{spec.key}` is a configured optional artifact; its version bump to `{v}` happens in step "
                    f"spring.optional_bump — if this is still blocked after the bump, `{v}` itself still exposes "
                    f"javax: ask the library owner for a Jakarta build")
        if a is not None:
            owner = f" (class `{item.owner_class}`)" if item.owner_class else ""
            if is_javax_api_artifact(a):
                # X1 attributed the *type's* jar (the error line names no library callee): say how to find the
                # library behind it instead of "upgrade the javax API jar to a Jakarta build" (2026-10-22)
                return (f"`{a.coordinates}`{owner} {what}; the javac error only names the javax type; run "
                        f"`mvn dependency:tree -Dincludes={a.key}` in the module to find the library that still "
                        f"uses it, then upgrade that library to a Jakarta release or replace it")
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
