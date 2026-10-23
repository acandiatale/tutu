"""Handler ``case-replace`` — apply the text replace learned for an error case (case catalog, 2026-10-23).

The KB keeps one reusable case per error signature (``sbmig.kb.catalog``). When past fixes of a case were pure text
replaces (``clientIp`` → ``remoteClientIp``), the case's action is ``replace`` with those token pairs; this handler
applies them deterministically — no LLM — when a later build reports an error of such a case:

* match: ``cfg.kb.catalog and cfg.kb.case_replace``, the catalog hit for the cluster's errors has the effective action
  ``replace`` with non-empty pairs, status ``candidate`` / ``active``, and at least one hit error with file + line;
* scope ``kb.case_replace_scope``: ``error-lines`` (default) = the lines javac reported for that case's errors in the
  file, plus the file's ``import`` lines; ``file`` = every line of the file;
* replace: ``(?<![\\w$])old(?![\\w$])`` when ``old`` is a Java identifier; any other ``old`` (a dotted name, a version)
  is replaced literally, bounded only at an end that is an identifier character (the pairs were learned on whole
  tokens, so ``1.0.0`` never hits ``11.0.0``); all pairs of a case in one pass (no chaining), only where ``old`` occurs.

Registered last in ``default_handlers`` so every specific built-in handler wins. Reactive only (``apply(ws, None)`` →
``[]``). Files are read / written through ``Workspace.read`` / ``Workspace.write`` (detected encoding, ``\\r\\n`` kept).
The build scores the attempt like any handler fix; the catalog counts the outcome for promotion (not this module).
"""
from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import Any

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, ProjectProfile
from sbmig.core.workspace import Workspace

from .edge_handlers import EdgeHandler

log = logging.getLogger("sbmig.triage.handlers.case_replace")

_KINDS = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE})
_STATUSES = frozenset({"candidate", "active"})
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_IDENT_CHAR_RE = re.compile(r"[\w$]")
_IMPORT_RE = re.compile(r"^\s*import\s")
# javac numbers lines by \n, \r\n and \r only (str.splitlines would also split on \f, \x1c,  , …)
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")

# one CaseCatalog per resolved kb dir (the catalog reloads itself when its index changes)
_CATALOGS: dict[str, Any] = {}


def clear_catalog_cache() -> None:
    _CATALOGS.clear()


def _catalog(cfg) -> Any:
    """The CaseCatalog of `cfg.kb_dir`, or None (module missing, no catalog/ yet, construction failed)."""
    try:
        from sbmig.kb.catalog import CaseCatalog
    except ImportError:
        return None
    kb_dir = Path(cfg.kb_dir)
    if not (kb_dir / "catalog").is_dir():          # no case was ever written: nothing to match, touch nothing
        return None
    key = str(kb_dir.resolve())
    cat = _CATALOGS.get(key)
    if cat is None or not isinstance(CaseCatalog, type) or type(cat) is not CaseCatalog:
        cat = CaseCatalog(kb_dir, cfg)
        _CATALOGS[key] = cat
    return cat


def _pairs(hit) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in getattr(hit.action, "replace_pairs", None) or []:
        old, new = getattr(p, "old", "") or "", getattr(p, "new", "")
        if new is None:
            new = ""
        if old and old != new and "\n" not in old and "\r" not in old and (old, new) not in out:
            out.append((old, new))
    return out


def _located(errors: list[BuildError]) -> list[BuildError]:
    return [e for e in errors or [] if e.file and e.line]


def _strong_errors(cat, hit) -> list[BuildError]:
    """The hit's errors whose L2 is the case key or whose L1 is one of its variants: a learned text replace is never
    applied on a symbol-only match (same symbol name, different error)."""
    errors = list(getattr(hit, "errors", None) or [])
    getter = getattr(cat, "get", None)
    try:
        case = getter(hit.case_id) if callable(getter) else None
    except Exception:  # noqa: BLE001
        case = None
    key = getattr(case, "signature_generic", "") if case is not None else ""
    if not key:
        return errors                                  # no case details (a stand-in catalog): keep the hit as is
    l1s = {getattr(v, "signature", "") for v in getattr(case, "variants", None) or []} - {""}
    return [e for e in errors if e.signature_generic == key or (e.signature and e.signature in l1s)]


def replace_hits(cluster: ErrorCluster | None, cfg) -> list:
    """Catalog hits of `cluster` this handler can apply (replace action, candidate / active, located errors)."""
    if cluster is None or cfg is None or cluster.kind not in _KINDS:
        return []
    kb = getattr(cfg, "kb", None)
    if kb is None or not getattr(kb, "catalog", False) or not getattr(kb, "case_replace", False):
        return []
    try:
        cat = _catalog(cfg)
        if cat is None:
            return []
        hits = list(cat.match(list(cluster.errors)) or [])
    except Exception as e:  # noqa: BLE001 - a broken / half-written KB must never break triage
        log.debug("case-replace: catalog match failed: %s: %s", type(e).__name__, e)
        return []
    out = []
    for h in hits:
        try:
            if getattr(h.action, "kind", None) == "replace" and h.status in _STATUSES and _pairs(h):
                kept = _strong_errors(cat, h)          # only errors of exactly this case (L2 / a variant's L1)
                if _located(kept):
                    out.append(h.model_copy(update={"errors": kept}) if hasattr(h, "model_copy") else h)
        except Exception as e:  # noqa: BLE001
            log.debug("case-replace: unusable hit %r: %s", getattr(h, "case_id", "?"), e)
    return out


def _pattern(old: str) -> str:
    if _IDENT_RE.fullmatch(old):
        return rf"(?<![\w$]){re.escape(old)}(?![\w$])"
    pre = r"(?<![\w$])" if _IDENT_CHAR_RE.match(old[0]) else ""
    post = r"(?![\w$])" if _IDENT_CHAR_RE.match(old[-1]) else ""
    return pre + re.escape(old) + post


def _compile(pairs: list[tuple[str, str]]) -> tuple[re.Pattern, dict[str, str]]:
    """One alternation over all pairs (longest `old` first), so a replacement is never re-replaced by another pair."""
    ordered = sorted(enumerate(pairs), key=lambda x: -len(x[1][0]))
    rx = re.compile("|".join(f"(?P<p{i}>{_pattern(old)})" for i, (old, _) in ordered))
    return rx, {f"p{i}": new for i, (_, new) in ordered}


def _rel(ws: Workspace, file: str) -> str | None:
    norm = (file or "").replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        try:
            norm = Path(norm).resolve().relative_to(Path(ws.root).resolve()).as_posix()
        except (ValueError, OSError):
            return None
    norm = posixpath.normpath(norm) if norm else ""
    if not norm or norm.startswith("../") or norm == ".." or "target" in norm.split("/"):
        return None
    return norm if ws.abs(norm).is_file() else None


def _split_lines(text: str) -> list[str]:
    return _LINE_RE.findall(text)


class CaseReplace(EdgeHandler):
    id = "case-replace"
    description = "apply the text replace learned for this error case (case catalog)"
    kinds = _KINDS

    def triggered(self, cluster: ErrorCluster) -> bool:
        return cluster.kind in self.kinds              # the decision comes from the catalog, not from regexes

    def matches(self, cluster: ErrorCluster, profile: ProjectProfile | None, cfg) -> bool:
        try:
            return self.triggered(cluster) and bool(replace_hits(cluster, cfg))
        except Exception as e:  # noqa: BLE001  # pragma: no cover - replace_hits already guards
            log.debug("case-replace: matches failed: %s", e)
            return False

    def edit(self, ws: Workspace, cluster: ErrorCluster | None, profile, cfg) -> list[str]:
        if cluster is None:
            return []                                  # reactive only
        hits = replace_hits(cluster, cfg)
        if not hits:
            return []
        whole_file = getattr(getattr(cfg, "kb", None), "case_replace_scope", "error-lines") == "file"
        texts: dict[str, str] = {}
        originals: dict[str, str] = {}
        for hit in hits:
            by_file: dict[str, set[int]] = {}
            for e in _located(hit.errors):
                rel = _rel(ws, e.file)
                if rel is not None:
                    by_file.setdefault(rel, set()).add(int(e.line))
            if not by_file:
                continue
            rx, repl = _compile(_pairs(hit))
            used: list[str] = []
            lines_changed: dict[str, int] = {}

            def sub(m: re.Match, repl=repl, used=used) -> str:
                new = repl[m.lastgroup]
                pair = f"{m.group(0)}→{new}"
                if pair not in used:
                    used.append(pair)
                return new

            for rel, line_nos in by_file.items():
                if rel not in texts:
                    try:
                        texts[rel] = originals[rel] = ws.read(rel)
                    except OSError as e:
                        log.debug("case-replace: cannot read %s: %s", rel, e)
                        continue
                lines = _split_lines(texts[rel])
                n = 0
                for i, line in enumerate(lines):
                    if not (whole_file or (i + 1) in line_nos or _IMPORT_RE.match(line)):
                        continue
                    new_line = rx.sub(sub, line)
                    if new_line != line:
                        lines[i] = new_line
                        n += 1
                if n:
                    texts[rel] = "".join(lines)
                    lines_changed[rel] = lines_changed.get(rel, 0) + n
            if lines_changed:
                total = sum(lines_changed.values())
                files = ", ".join(posixpath.basename(f) for f in lines_changed)
                name = f" ({hit.name})" if getattr(hit, "name", "") else ""
                scope = " [scope: file]" if whole_file else ""
                self.note(f"case {hit.case_id}{name}: replaced {', '.join(used)} on {total} line(s) of {files}{scope}")
                log.info("case-replace: case %s replaced %s in %s", hit.case_id, used, sorted(lines_changed))
        changed: list[str] = []
        for rel, text in texts.items():
            if text != originals.get(rel):
                ws.write(rel, text)
                changed.append(rel)
        return changed


def case_replace_handlers(cfg=None) -> list:
    return [CaseReplace()]
