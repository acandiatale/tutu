"""Compose the run-specific ``rewrite.yml``.

All project-owned base recipes (``knowledge/recipes/base/*.yml``), the KB's active recipes and any extra files are
concatenated (after templating ``{{SPRING_BOOT_VERSION}}``, ``{{JAVA_VERSION}}``, ``{{SPRING_FRAMEWORK_VERSION}}``),
then one composite recipe ``com.company.migrate.Run`` is appended whose ``recipeList`` is the requested recipe ids in
order. Only that composite is activated, so ordering is deterministic.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml

from sbmig.core.config import TargetConfig
from sbmig.core.models import RecipeRef

log = logging.getLogger("sbmig.rewrite")

COMPOSITE_RECIPE_ID = "com.company.migrate.Run"
RECIPE_TYPE = "specs.openrewrite.org/v1beta/recipe"
_DOC_SEP_RE = re.compile(r"^---[ \t]*(?:#.*)?$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


class RecipeCompositionError(ValueError):
    """Requested project/kb recipe ids are not defined in any composed file (a programming/config error)."""

    def __init__(self, missing: Sequence[str], searched: Sequence[str]):
        self.missing = list(missing)
        self.searched = list(searched)
        super().__init__(
            "recipe id(s) not defined in the composed rewrite config: " + ", ".join(self.missing)
            + (" (searched: " + ", ".join(self.searched) + ")" if self.searched else ""))


def template_values(target: TargetConfig | None) -> dict[str, str]:
    t = target or TargetConfig()
    return {
        "SPRING_BOOT_VERSION": t.spring_boot_version,
        "JAVA_VERSION": t.java_version,
        "SPRING_FRAMEWORK_VERSION": t.spring_framework_version,
    }


def render_template(text: str, values: dict[str, str], *, source: str = "") -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        log.warning("unknown placeholder {{%s}} in %s left as-is", key, source or "recipe file")
        return m.group(0)
    return _PLACEHOLDER_RE.sub(repl, text)


def split_documents(text: str) -> list[str]:
    """Split a multi-document YAML text on ``---`` lines, dropping comment-only/empty chunks."""
    out = []
    for chunk in _DOC_SEP_RE.split(text):
        body = "\n".join(ln for ln in chunk.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))
        if body.strip():
            out.append(chunk.strip("\n"))
    return out


def recipe_documents(text: str) -> dict[str, dict]:
    """name → parsed document for every ``specs.openrewrite.org/v1beta/recipe`` document in ``text``."""
    out: dict[str, dict] = {}
    for doc in yaml.safe_load_all(text):
        if isinstance(doc, dict) and doc.get("type") == RECIPE_TYPE and doc.get("name"):
            out[str(doc["name"])] = doc
    return out


def recipe_list_ids(doc: dict) -> list[str]:
    """Recipe ids referenced by a declarative recipe's ``recipeList`` (string items or single-key mappings)."""
    ids: list[str] = []
    for item in doc.get("recipeList") or []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item:
            ids.append(str(next(iter(item))))
    return ids


def _source_files(base_dir: Path | None, kb_files: Iterable[Path], extra_files: Iterable[Path],
                  recipes: Sequence[RecipeRef]) -> list[Path]:
    files: list[Path] = []
    if base_dir is not None and Path(base_dir).is_dir():
        files += sorted(Path(base_dir).glob("*.yml")) + sorted(Path(base_dir).glob("*.yaml"))
    files += [Path(p) for p in kb_files]
    files += [Path(r.file) for r in recipes if r.file]
    files += [Path(p) for p in extra_files]
    seen: set[str] = set()
    out = []
    for f in files:
        key = str(f.resolve()) if f.exists() else str(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def compose_rewrite_yaml(recipes: Sequence[RecipeRef], *, base_dir: Path | str | None = None,
                         kb_files: Iterable[Path | str] = (), extra_files: Iterable[Path | str] = (),
                         target: TargetConfig | None = None,
                         composite_id: str = COMPOSITE_RECIPE_ID) -> str:
    """Return the full rewrite.yml text. Raises ``RecipeCompositionError`` for undefined project/kb recipe ids
    and ``FileNotFoundError`` for an explicitly given (kb/extra/RecipeRef.file) file that does not exist."""
    values = template_values(target)
    files = _source_files(Path(base_dir) if base_dir else None, [Path(p) for p in kb_files],
                          [Path(p) for p in extra_files], recipes)
    by_name: dict[str, tuple[str, str]] = {}           # name → (doc text, source)
    other_docs: list[tuple[str, str]] = []
    for f in files:
        if not f.exists():
            raise FileNotFoundError(f"recipe file not found: {f}")
        text = render_template(f.read_text(encoding="utf-8"), values, source=str(f))
        for chunk in split_documents(text):
            try:
                doc = yaml.safe_load(chunk)
            except yaml.YAMLError as exc:
                raise ValueError(f"invalid YAML in recipe file {f}: {exc}") from exc
            if isinstance(doc, dict) and doc.get("name"):
                name = str(doc["name"])
                if name == composite_id:
                    log.warning("ignoring recipe %s from %s: reserved composite id", name, f)
                    continue
                if name in by_name:
                    log.warning("recipe %s defined in %s overrides the definition from %s", name, f, by_name[name][1])
                by_name[name] = (chunk, str(f))
            elif doc is not None:
                other_docs.append((chunk, str(f)))

    ids: list[str] = []
    for r in recipes:
        if r.id not in ids:
            ids.append(r.id)
    missing = [r.id for r in recipes if r.source in ("project", "kb") and r.id not in by_name]
    if missing:
        raise RecipeCompositionError(sorted(set(missing), key=missing.index), [str(f) for f in files])

    parts = [f"# generated by sbmig — composite {composite_id}; sources:"]
    parts += [f"#   {f}" for f in files]
    body = [chunk for chunk, _src in by_name.values()] + [chunk for chunk, _src in other_docs]
    composite = {
        "type": RECIPE_TYPE,
        "name": composite_id,
        "displayName": "sbmig migration run",
        "description": "Generated by sbmig: runs the requested recipes in order.",
        "recipeList": ids,
    }
    body.append(yaml.safe_dump(composite, sort_keys=False, default_flow_style=False).rstrip("\n"))
    return "\n".join(parts) + "\n" + "".join(f"---\n{chunk}\n" for chunk in body)
