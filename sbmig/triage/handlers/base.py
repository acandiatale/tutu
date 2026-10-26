"""Deterministic handlers: base class + registry (implements HandlerLike)."""
from __future__ import annotations

import logging
import posixpath
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import ErrorCluster, ErrorKind, FileChange, Phase, ProjectProfile
from sbmig.core.workspace import Workspace

log = logging.getLogger("sbmig.triage.handlers")

PomEditorFactory = Callable[[Path], Any]


# ---------------------------------------------------------------------------
# small utilities shared by handlers
# ---------------------------------------------------------------------------
def parse_version(v: Optional[str]) -> Optional[tuple[int, ...]]:
    if not v or "${" in v:
        return None
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", v)
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def version_lt(a: Optional[str], b: str) -> Optional[bool]:
    """a < b ; None when a cannot be parsed (unknown / property reference)."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    w = max(len(pa), len(pb))
    return pa + (0,) * (w - len(pa)) < pb + (0,) * (w - len(pb))


def cluster_text(cluster: Optional[ErrorCluster]) -> str:
    if cluster is None:
        return ""
    parts: list[str] = []
    for e in cluster.errors:
        parts += [e.message or "", e.raw or "", e.symbol or "", e.location or "", e.signature or "",
                  e.signature_generic or "", e.goal or ""]
        if e.artifact is not None:
            parts.append(e.artifact.coordinates)
    return "\n".join(p for p in parts if p)


def cluster_artifact_keys(cluster: Optional[ErrorCluster]) -> set[str]:
    if cluster is None:
        return set()
    return {e.artifact.key for e in cluster.errors if e.artifact is not None}


def profile_declares(profile: Optional[ProjectProfile], keys: Iterable[str]) -> bool:
    """True when any pom in the profile declares one of `keys` (direct or managed). Unknown profile → True."""
    if profile is None or not profile.poms:
        return True
    keys = set(keys)
    return any(d.key in keys for p in profile.poms for d in [*p.dependencies, *p.dependency_management])


def all_poms(ws: Workspace) -> list[str]:
    """Every pom.xml of the project, excluding test resources / generated dirs."""
    return [p for p in ws.pom_files() if "/src/" not in f"/{p}" and not p.startswith("src/")]


def nearest_pom(ws: Workspace, rel_file: str) -> Optional[str]:
    """Module pom of a root-relative file (walk up). An absolute path is made root-relative when it lies under the
    workspace, else the walk starts at the root — `posixpath.dirname("/")` is `/`, so the old loop spun forever on
    absolute paths (round 9c, Windows Maven output)."""
    norm = (rel_file or "").replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        try:
            norm = Path(norm).resolve().relative_to(Path(ws.root).resolve()).as_posix()
        except (ValueError, OSError):
            norm = ""
    d = posixpath.dirname(norm)
    for _ in range(128):
        cand = posixpath.join(d, "pom.xml") if d else "pom.xml"
        if ws.exists(cand):
            return cand
        if not d or d in ("/", "."):
            return None
        nxt = posixpath.dirname(d)
        if nxt == d:
            return None
        d = nxt
    return None


def pom_for_module(ws: Workspace, profile: Optional[ProjectProfile], module: str) -> Optional[str]:
    if profile is not None:
        for p in profile.poms:
            d = posixpath.dirname(p.path)
            if module in (p.artifact_id, d, posixpath.basename(d)) and ws.exists(p.path):
                return p.path
    for p in all_poms(ws):
        d = posixpath.dirname(p)
        if d and (posixpath.basename(d) == module or d == module):
            return p
    # a module the restructure created (`{name}-back`, `member-api-comp` in `module/comp-jar/`) is unknown to the as-is
    # profile and its dir may not carry its artifactId: read the current poms' own artifactIds (round 10 follow-up)
    for p in all_poms(ws):
        try:
            text = re.sub(r"<parent>.*?</parent>", "", re.sub(r"<!--.*?-->", "", ws.read(p), flags=re.S), flags=re.S)
        except OSError:
            continue
        m = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", text)
        if m and m.group(1) == module:
            return p
    return None


def root_pom(ws: Workspace, profile: Optional[ProjectProfile] = None) -> Optional[str]:
    if ws.exists("pom.xml"):
        return "pom.xml"
    if profile is not None and profile.root_pom is not None and ws.exists(profile.root_pom.path):
        return profile.root_pom.path
    poms = all_poms(ws)
    return min(poms, key=lambda p: p.count("/")) if poms else None


def poms_for_cluster(ws: Workspace, cluster: Optional[ErrorCluster],
                     profile: Optional[ProjectProfile] = None) -> list[str]:
    """The module pom(s) a cluster belongs to (error file → nearest pom upward, else module), fallback root."""
    out: list[str] = []
    if cluster is not None:
        for e in cluster.errors:
            p: Optional[str] = None
            if e.file and e.file.endswith("pom.xml") and ws.exists(e.file):
                p = e.file
            elif e.file:
                p = nearest_pom(ws, e.file)
            if p is None and e.module:
                p = pom_for_module(ws, profile, e.module)
            if p and p not in out:
                out.append(p)
    if not out:
        r = root_pom(ws, profile)
        if r:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Handler base
# ---------------------------------------------------------------------------
class Handler(ABC):
    """Deterministic fix. Reactive via `matches(cluster, …)`; pre-emptive via `apply(ws, None, profile, cfg)`."""

    id: ClassVar[str] = ""
    description: ClassVar[str] = ""
    kinds: ClassVar[frozenset[ErrorKind]] = frozenset(ErrorKind)
    triggers: ClassVar[tuple[str, ...]] = ()          # regexes searched in the cluster text
    # phase before whose first build the handler may run pre-emptively (jakarta-dependent ones wait for SPRING)
    preemptive_phase: ClassVar[Phase] = Phase.JDK21

    def __init__(self) -> None:
        self.registry: Optional["HandlerRegistry"] = None

    # -- matching ------------------------------------------------------
    def triggered(self, cluster: ErrorCluster) -> bool:
        if cluster.kind not in self.kinds:
            return False
        if not self.triggers:
            return False
        text = cluster_text(cluster)
        return any(re.search(t, text) for t in self.triggers)

    def applicable(self, cluster: Optional[ErrorCluster], profile: Optional[ProjectProfile],
                   cfg: Optional[Config]) -> bool:
        return True

    def matches(self, cluster: ErrorCluster, profile: ProjectProfile, cfg: Config) -> bool:
        return self.triggered(cluster) and self.applicable(cluster, profile, cfg)

    # -- applying ------------------------------------------------------
    @abstractmethod
    def apply(self, ws: Workspace, cluster: Optional[ErrorCluster] = None,
              profile: Optional[ProjectProfile] = None, cfg: Optional[Config] = None) -> list[FileChange]: ...

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Handler {self.id}>"


def _default_pom_editor_factory() -> PomEditorFactory:
    try:
        from sbmig.restructure import PomEditor  # type: ignore[attr-defined]
    except Exception:
        try:
            from sbmig.restructure.pom_editor import PomEditor  # type: ignore[no-redef]
        except Exception as e:  # pragma: no cover - depends on workstream B
            raise RuntimeError(
                "pom handlers need sbmig.restructure.PomEditor (workstream B) which is not importable: "
                f"{type(e).__name__}: {e}") from e
    return PomEditor


class HandlerRegistry:
    def __init__(self, handlers: Iterable[Handler] = (), pom_editor_factory: Optional[PomEditorFactory] = None):
        self._handlers: dict[str, Handler] = {}
        self._pom_editor_factory = pom_editor_factory
        for h in handlers:
            self.register(h)

    # pom editor factory (lazily resolved so importing triage never requires workstream B)
    @property
    def pom_editor_factory(self) -> PomEditorFactory:
        if self._pom_editor_factory is None:
            self._pom_editor_factory = _default_pom_editor_factory()
        return self._pom_editor_factory

    @pom_editor_factory.setter
    def pom_editor_factory(self, factory: Optional[PomEditorFactory]) -> None:
        self._pom_editor_factory = factory

    def register(self, handler: Handler) -> Handler:
        handler.registry = self
        self._handlers[handler.id] = handler
        return handler

    def get(self, handler_id: str) -> Optional[Handler]:
        return self._handlers.get(handler_id)

    def __contains__(self, handler_id: object) -> bool:
        return handler_id in self._handlers

    def __iter__(self):
        return iter(self._handlers.values())

    def __len__(self) -> int:
        return len(self._handlers)

    def ids(self) -> list[str]:
        return list(self._handlers)

    def matching(self, cluster: ErrorCluster, profile: ProjectProfile, cfg: Config) -> list[Handler]:
        out: list[Handler] = []
        for h in self._handlers.values():
            try:
                if h.matches(cluster, profile, cfg):
                    out.append(h)
            except Exception as e:  # a broken matcher must never break triage
                log.warning("handler %s.matches failed: %s", h.id, e)
        return out

    @classmethod
    def default(cls, cfg: Optional[Config] = None,
                pom_editor_factory: Optional[PomEditorFactory] = None) -> "HandlerRegistry":
        from .pom_handlers import default_handlers

        return cls(default_handlers(cfg), pom_editor_factory=pom_editor_factory)
