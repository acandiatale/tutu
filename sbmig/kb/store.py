"""File-based knowledge base (no DB): cases, signature index, declarative recipes, hand-written patterns.

Layout under ``cfg.kb_dir`` (created on first use)::

    cases/YYYY-MM/<case-id>.json        KBCase (model_dump_json)
    index.json                          {"version":1, "l1": {sig: [case_id…]}, "l2": {sig: [case_id…]},
                                         "recipes": {sig: [recipe_id…]}, "updated_at": …}
    recipes/active/<name>.yml   + <name>.meta.json   (KBRecipe)
    recipes/candidates/<name>.yml + <name>.meta.json
    recipes/rejected/<name>.yml + <name>.meta.json
    patterns/*.yml                      {id, match: {signature_regex}, recipes: [...], hints: [...], handler_ids: [...]}
    ledger.jsonl                        append-only ResolutionRecords (resolved / blocked / unresolved)
    symbols.json                        error-case symbol → fix labels / replacement / projects / cases
    errors.jsonl                        append-only ErrorCaseRecords: every build error of every run (2026-10-23)
    fix_attempts.jsonl                  append-only FixAttemptRecords: every fix attempt, failures included, + diff
    error_hints.json                    L2 / symbol → past fixes and failures (derived; `kb reindex` rebuilds it)
    catalog/<kind>/<class>/<symbol>--<id>.yml   one reusable case per L2 signature (sbmig.kb.catalog; `human:`
                                        block edited by people, the rest derived from errors.jsonl)
    catalog/index.json                  case id → file, L2 / L1 / symbol → case id (derived)
    views/cases.md, views/cases.csv     generated case tables for people (derived)
    audit.jsonl                         human case edits + automatic case status / action transitions (round 10),
                                        fix-script versions created / credited / retired / activated (v2)
    scripts/<case-id>/v<N>.py + .meta.json   v2 fix scripts per case (sbmig.kb.scripts, ``FileKnowledgeBase.scripts``)
    reviews.jsonl                       v2 structure reviews, one line per run (``record_review``)
    quarantine.jsonl                    notes about errors.jsonl lines that could not be read (round 10)
    *.jsonl.corrupt                     torn last lines moved aside before the next append (round 10)
    .lock                               the cross-process write lock (sbmig.kb.lock)

All writes are atomic (temp file + ``os.replace``) or torn-line safe appends (``sbmig.kb.jsonl``), and run under the
cross-process lock ``.lock`` (round 10: the web server, a fleet and ``sbmig kb …`` share one KB; re-entrant, waits
up to ``kb.lock_timeout_seconds``, never fails a run). In-memory indexes are reloaded when their file changed
since they were read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

import yaml

from sbmig.core.config import Config
from sbmig.core.interfaces import LLMClientLike
from sbmig.core.models import (
    ErrorCluster,
    HarvestResult,
    KBCase,
    KBMatch,
    KBRecipe,
    PhaseResult,
    ProjectProfile,
    RunReport,
    StructureReview,
    utcnow,
)
from sbmig.core.workspace import Workspace

from .catalog import CATALOG_DIR, VIEWS_DIR, CaseCatalog
from .catalog import clear_caches as clear_catalog_caches
from .error_cases import ErrorCaseStore
from .harvest import NO_REPLAY_TAG
from .jsonl import append_lines, line_ids, parse_record
from .labels import symbol_for_error, symbol_for_errors
from .ledger import Ledger, SymbolIndex, advisory_record, blocked_record, unresolved_records
from .lock import LOCK_NAME, kb_lock
from .recipes import LEGACY_NAME_PREFIX, recipe_names_in_yaml
from .redact import get_redactor
from .scripts import SCRIPTS_DIR, ScriptStore

log = logging.getLogger("sbmig.kb")

INDEX_VERSION = 1
AUDIT_FILES = ("audit.jsonl", "quarantine.jsonl")
STATUSES = ("active", "candidate", "rejected")
_STATUS_DIR = {"active": "active", "candidate": "candidates", "rejected": "rejected"}
_UPDATED_TAG = "updated:"
MAX_MATCH_CASES = 10
ERROR_CASE_FILES = ("errors.jsonl", "fix_attempts.jsonl", "error_hints.json")
REVIEWS_FILE = "reviews.jsonl"           # v2 structure reviews (one line per run)
REVIEW_TEXT_LIMIT = 4000


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def case_content_hash(case: KBCase) -> str:
    h = hashlib.sha1()
    for s in sorted(set(case.signatures)):
        h.update(s.encode("utf-8") + b"\n")
    for ch in case.changes:
        h.update(ch.model_dump_json().encode("utf-8") + b"\n")
    return h.hexdigest()[:10]


def _basename(p: str) -> str:
    return PurePosixPath(p.replace("\\", "/")).name


def _stamp(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


class FileKnowledgeBase:
    """Implements :class:`sbmig.core.interfaces.KnowledgeBaseLike` on plain files under ``cfg.kb_dir``."""

    def __init__(self, cfg: Config, kb_dir: Optional[Path] = None):
        self.cfg = cfg
        self.root = Path(kb_dir) if kb_dir else cfg.kb_dir
        self._index: Optional[dict] = None
        self._index_stamp: Optional[tuple[int, int, int]] = None   # index.json as it was when _index was read
        self._promoted: list[str] = []                  # auto-promotions not yet reported (drain_promoted)
        self._ledger_blocked: set[tuple[str, str]] = set()   # (run_id, blocked id) already in the ledger
        self._error_store: Optional[ErrorCaseStore] = None   # cached: keeps the hints index loaded across a fleet
        self._catalog: Optional[CaseCatalog] = None
        self._scripts: Optional[ScriptStore] = None

    # ------------------------------------------------------------------ layout
    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def recipes_dir(self) -> Path:
        return self.root / "recipes"

    @property
    def patterns_dir(self) -> Path:
        return self.root / "patterns"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.jsonl"

    @property
    def symbols_path(self) -> Path:
        return self.root / "symbols.json"

    @property
    def lock_timeout(self) -> float:
        return float(getattr(self.cfg.kb, "lock_timeout_seconds", 300) or 0)

    def lock(self):
        """The cross-process KB lock (``<kb>/.lock``, re-entrant) — hold it around every read-modify-write."""
        return kb_lock(self.root, self.lock_timeout)

    def ledger(self) -> Ledger:
        return Ledger(self.ledger_path, self.lock_timeout)

    def symbol_index(self) -> SymbolIndex:
        return SymbolIndex(self.symbols_path, self.lock_timeout)

    def error_case_store(self) -> ErrorCaseStore:
        """errors.jsonl / fix_attempts.jsonl / error_hints.json (one instance per KB: the hints index is loaded
        once and reloaded only when the file changes)."""
        if self._error_store is None or self._error_store.root != self.root:
            self._error_store = ErrorCaseStore(self.root, self.lock_timeout)
        return self._error_store

    def catalog(self) -> CaseCatalog:
        """The case catalog (kb/catalog/, kb/views/) on this KB's recipes and patch cases."""
        if self._catalog is None or self._catalog.root != self.root:
            self._catalog = CaseCatalog(self.root, self.cfg, kb=self)
        return self._catalog

    # ------------------------------------------------------------------ v2: fix scripts + structure reviews
    @property
    def scripts(self) -> ScriptStore:
        """The per-case fix-script store ``<kb>/scripts/`` (``ScriptStoreLike``, v2; created on first use)."""
        if self._scripts is None or self._scripts.root != self.root:
            self._scripts = ScriptStore(self.root, self.cfg, lock_timeout=self.lock_timeout)
        return self._scripts

    @property
    def reviews_path(self) -> Path:
        return self.root / REVIEWS_FILE

    def _review_record(self, review: StructureReview) -> StructureReview:
        """The stored form of a review: issue texts, analysis, note, the script and its run log redacted (KB
        rules), the home directory as ``~``, free texts capped; the script is kept (short) but masked too."""
        from .error_cases import _roots, _strip

        red, roots = get_redactor(self.cfg), _roots(None)

        def clean(text: Optional[str], limit: int = REVIEW_TEXT_LIMIT) -> str:
            out = red.redact(_strip(text or "", roots)) or ""
            return out[:limit] + "…[truncated]" if limit > 0 and len(out) > limit else out

        issues = [i.model_copy(update={"file": clean(i.file, 500), "problem": clean(i.problem),
                                       "fix_hint": clean(i.fix_hint)}) for i in review.issues]
        res = review.script_result
        if res is not None:
            res = res.model_copy(update={"log": [clean(x, 500) for x in res.log], "error": clean(res.error),
                                         "problems": [clean(x, 500) for x in res.problems],
                                         "changed_files": [clean(x, 500) for x in res.changed_files]})
        rid = review.id or (f"{review.run_id}:review" if review.run_id else
                            f"review:{review.at.strftime('%Y%m%d%H%M%S')}:{review.project or '-'}")
        return review.model_copy(update={"id": rid, "issues": issues, "script_result": res,
                                         "analysis": clean(review.analysis), "note": clean(review.note),
                                         "script": clean(review.script, 0), "project": clean(review.project, 500)})

    def record_review(self, review: StructureReview) -> None:
        """One line in ``<kb>/reviews.jsonl`` per structure review (texts redacted, see :meth:`_review_record`); a
        review whose id is already stored is not written again. Never raises (a warning instead)."""
        try:
            rec = self._review_record(review)
            with self.lock():
                if self.reviews_path.exists():
                    with open(self.reviews_path, "r", encoding="utf-8", errors="replace") as fh:
                        if any(rec.id in line_ids(ln) for ln in fh if ln.strip()):
                            log.debug("structure review %s is already in %s", rec.id, self.reviews_path.name)
                            return
                append_lines(self.reviews_path, [rec.model_dump_json()])
        except Exception as e:  # noqa: BLE001 - the review log must never fail a run
            log.warning("kb.record_review failed: %s: %s", type(e).__name__, e)

    def iter_reviews(self) -> Iterator[StructureReview]:
        """The stored structure reviews (file order, first line of an id wins; unreadable lines skipped)."""
        if not self.reviews_path.exists():
            return
        seen: set[str] = set()
        with open(self.reviews_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                r = parse_record(line, StructureReview)
                if r is None or r.id in seen:
                    continue
                seen.add(r.id)
                yield r

    def append_error_cases(self, errors, attempts) -> int:
        """Append a run's prepared error / attempt records (ids already stored are skipped), update the hints
        index and (``kb.catalog``) the case catalog + views. Returns the number of new records (errors + attempts).
        Never raises."""
        errors, attempts = list(errors or []), list(attempts or [])
        if not errors and not attempts:
            return 0
        with self.lock():
            try:
                n = self.error_case_store().append(errors, attempts)
            except Exception as e:
                log.warning("kb.append_error_cases failed: %s: %s", type(e).__name__, e)
                return 0
            if self.cfg.kb.catalog and errors:
                try:
                    cat = self.catalog()
                    changed = cat.update_from(errors, attempts)
                    cat.write_views()
                    if changed:
                        log.info("case catalog: %d case(s) updated", len(changed))
                except Exception as e:  # the events are stored; `sbmig kb reindex` rebuilds the catalog
                    log.warning("case catalog update failed: %s: %s", type(e).__name__, e)
        return n

    def status_dir(self, status: str) -> Path:
        return self.recipes_dir / _STATUS_DIR[status]

    def ensure_layout(self) -> None:
        for d in (self.cases_dir, self.patterns_dir, *(self.status_dir(s) for s in STATUSES)):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ index
    @staticmethod
    def _empty_index() -> dict:
        return {"version": INDEX_VERSION, "l1": {}, "l2": {}, "recipes": {}, "updated_at": utcnow().isoformat()}

    def _load_index(self) -> dict:
        """index.json, cached — and read again when the file changed since (another process wrote it: a fleet
        must not save its stale copy over a web-server edit)."""
        stamp = _stamp(self.index_path)
        if self._index is not None and stamp == self._index_stamp:
            return self._index
        if stamp is not None:
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") == INDEX_VERSION:
                    for k in ("l1", "l2", "recipes"):
                        data.setdefault(k, {})
                    self._index, self._index_stamp = data, stamp
                    return data
                log.warning("kb index %s has unknown version; rebuilding", self.index_path)
            except (OSError, ValueError) as e:
                log.warning("kb index unreadable (%s); rebuilding", e)
            self.reindex()
            assert self._index is not None
            return self._index
        if self._index is not None and self._index_stamp is None:
            return self._index                       # built in memory, not saved yet
        if self.root.exists() and (any(self.cases_dir.glob("*/*.json")) if self.cases_dir.exists() else False):
            self.reindex()
            assert self._index is not None
            return self._index
        self._index, self._index_stamp = self._empty_index(), None
        return self._index

    def _save_index(self) -> None:
        with self.lock():
            idx = self._load_index()
            idx["updated_at"] = utcnow().isoformat()
            for k in ("l1", "l2", "recipes"):
                idx[k] = {s: v for s, v in sorted(idx[k].items()) if v}
            atomic_write(self.index_path, json.dumps(idx, indent=1, sort_keys=False, ensure_ascii=False) + "\n")
            self._index_stamp = _stamp(self.index_path)

    @staticmethod
    def _idx_add(table: dict, sig: str, value: str) -> None:
        if not sig:
            return
        lst = table.setdefault(sig, [])
        if value not in lst:
            lst.append(value)

    @staticmethod
    def _idx_remove_value(table: dict, value: str) -> None:
        for sig in list(table):
            if value in table[sig]:
                table[sig] = [v for v in table[sig] if v != value]

    def _index_case(self, case: KBCase) -> None:
        idx = self._load_index()
        for s in case.signatures:
            self._idx_add(idx["l1"], s, case.id)
        for s in case.signatures_generic:
            self._idx_add(idx["l2"], s, case.id)

    def _index_recipe(self, recipe: KBRecipe) -> None:
        idx = self._load_index()
        self._idx_remove_value(idx["recipes"], recipe.id)
        if recipe.status == "rejected":
            return
        for s in recipe.signatures:
            self._idx_add(idx["recipes"], s, recipe.id)

    def reindex(self) -> int:
        """Rebuild index.json (and symbols.json) from the case and recipe files. Returns the case count."""
        with self.lock():
            # the empty in-memory index stands for the file until it is saved below (no reload in between)
            self._index, self._index_stamp = self._empty_index(), _stamp(self.index_path)
            cases = list(self.iter_cases())
            for case in cases:
                self._index_case(case)
            for r in self.list_recipes():
                self._index_recipe(r)
            self.root.mkdir(parents=True, exist_ok=True)
            self._save_index()
            if self.cfg.kb.symbol_index:
                self.symbol_index().rebuild(cases)
            store = self.error_case_store()
            if store.errors_path.exists():
                try:
                    store.rebuild_hints()
                except Exception as e:  # the case index above is what reindex must not lose
                    log.warning("error hints rebuild failed: %s: %s", type(e).__name__, e)
            cat = self.catalog()
            if self.cfg.kb.catalog and (store.errors_path.exists() or cat.catalog_dir.exists()):
                try:
                    cat.rebuild()
                    cat.write_views()
                except Exception as e:
                    log.warning("case catalog rebuild failed: %s: %s", type(e).__name__, e)
            return len(cases)

    # ------------------------------------------------------------------ cases
    def case_path(self, case_id: str) -> Path:
        month = f"{case_id[:4]}-{case_id[4:6]}" if re.match(r"^\d{8}-", case_id) else "misc"
        return self.cases_dir / month / f"{case_id}.json"

    def iter_cases(self):
        if not self.cases_dir.exists():
            return
        for p in sorted(self.cases_dir.glob("*/*.json")):
            try:
                yield KBCase.model_validate_json(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("skipping unreadable kb case %s: %s", p, e)

    def get_case(self, case_id: str) -> Optional[KBCase]:
        p = self.case_path(case_id)
        if not p.exists():
            hits = list(self.cases_dir.glob(f"*/{case_id}.json")) if self.cases_dir.exists() else []
            if not hits:
                return None
            p = hits[0]
        try:
            return KBCase.model_validate_json(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("unreadable kb case %s: %s", p, e)
            return None

    def _write_case(self, case: KBCase) -> None:
        atomic_write(self.case_path(case.id), case.model_dump_json(indent=2) + "\n")

    def _find_case_by_hash(self, content_hash: str) -> Optional[KBCase]:
        idx = self._load_index()
        seen: set[str] = set()
        for table in (idx["l1"], idx["l2"]):
            for ids in table.values():
                for cid in ids:
                    if cid not in seen and cid.endswith("-" + content_hash):
                        seen.add(cid)
                        c = self.get_case(cid)
                        if c is not None:
                            return c
        if self.cases_dir.exists():
            for p in self.cases_dir.glob(f"*/*-{content_hash}.json"):
                return self.get_case(p.stem)
        return None

    @staticmethod
    def _touch_updated(case: KBCase) -> None:
        case.tags = [t for t in case.tags if not t.startswith(_UPDATED_TAG)] + [
            f"{_UPDATED_TAG}{utcnow().isoformat(timespec='seconds')}"]

    def record_case_ex(self, case: KBCase) -> tuple[str, bool]:
        """Record (or dedupe) a case. Returns (case_id, created)."""
        with self.lock():
            self.ensure_layout()
            content_hash = case_content_hash(case)
            existing = self._find_case_by_hash(content_hash)
            if existing is not None:
                existing.reuse_count += 1
                fp = case.project_fingerprint
                if fp and fp not in existing.reuse_projects and fp != existing.project_fingerprint:
                    existing.reuse_projects.append(fp)
                self._touch_updated(existing)
                self._write_case(existing)
                self._index_case(existing)
                self._save_index()
                return existing.id, False
            case = case.model_copy(deep=True)
            case.id = f"{case.created_at.strftime('%Y%m%d')}-{content_hash}"
            self._write_case(case)
            self._index_case(case)
            self._save_index()
            return case.id, True

    def record_case(self, case: KBCase) -> str:
        return self.record_case_ex(case)[0]

    def bump_case_reuse(self, case_id: str, project_fingerprint: str = "") -> Optional[KBCase]:
        with self.lock():
            case = self.get_case(case_id)
            if case is None:
                return None
            case.reuse_count += 1
            if project_fingerprint and project_fingerprint not in case.reuse_projects:
                case.reuse_projects.append(project_fingerprint)
            self._touch_updated(case)
            self._write_case(case)
            return case

    def update_case(self, case: KBCase) -> None:
        with self.lock():
            self._write_case(case)
            self._index_case(case)
            self._save_index()

    # ------------------------------------------------------------------ recipes
    def _meta_path(self, yml: Path) -> Path:
        return yml.with_name(yml.name[: -len(yml.suffix)] + ".meta.json")

    def _load_recipe_file(self, yml: Path, status: str) -> Optional[KBRecipe]:
        meta = self._meta_path(yml)
        data: dict[str, Any] = {}
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("unreadable recipe meta %s: %s", meta, e)
        if not data.get("id"):
            try:
                names = recipe_names_in_yaml(yml.read_text(encoding="utf-8"))
            except OSError:
                names = []
            data["id"] = names[0] if names else yml.stem
        data["status"] = status
        data["path"] = str(yml)
        try:
            return KBRecipe.model_validate(data)
        except ValueError as e:
            log.warning("invalid recipe meta %s: %s", meta, e)
            return None

    def _recipe_meta_extra(self, recipe: KBRecipe) -> dict:
        p = self._meta_path(Path(recipe.path))
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return {k: v for k, v in data.items() if k not in KBRecipe.model_fields}
            except (OSError, ValueError):
                return {}
        return {}

    def _write_recipe_meta(self, recipe: KBRecipe, extra: Optional[dict] = None) -> None:
        data = recipe.model_dump(mode="json")
        data.update(extra or {})
        atomic_write(self._meta_path(Path(recipe.path)), json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def list_recipes(self, status: Optional[str] = None) -> list[KBRecipe]:
        statuses = [status] if status else list(STATUSES)
        out: list[KBRecipe] = []
        for st in statuses:
            if st not in _STATUS_DIR:
                raise ValueError(f"unknown recipe status {st!r} (expected one of {', '.join(STATUSES)})")
            d = self.status_dir(st)
            if not d.exists():
                continue
            for yml in sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml"))):
                r = self._load_recipe_file(yml, st)
                if r is not None:
                    out.append(r)
        return out

    def get_recipe(self, recipe_id: str) -> Optional[KBRecipe]:
        """By id or file stem; `Foo` also finds a legacy `com.company.kb.Foo` (and the other way round)."""
        alt = (recipe_id[len(LEGACY_NAME_PREFIX):] if recipe_id.startswith(LEGACY_NAME_PREFIX)
               else LEGACY_NAME_PREFIX + recipe_id if recipe_id and "." not in recipe_id else None)
        recipes = self.list_recipes()
        for want in (recipe_id, alt):
            for r in recipes if want else ():
                if r.id == want or Path(r.path).stem == want:
                    return r
        return None

    def _active_with_names(self) -> list[tuple[KBRecipe, list[str]]]:
        """Active recipes whose yml parses to at least one recipe name (broken files are skipped)."""
        if not self.cfg.kb.use_active_recipes:
            return []
        out = []
        for r in self.list_recipes("active"):
            try:
                names = recipe_names_in_yaml(Path(r.path).read_text(encoding="utf-8"))
            except OSError:
                names = []
            if not names:
                log.warning("skipping active kb recipe file without recipe documents: %s", r.path)
                continue
            out.append((r, names))
        return out

    def active_recipe_files(self) -> list[Path]:
        return [Path(r.path) for r, _ in self._active_with_names()]

    def active_recipe_ids(self) -> list[str]:
        ids: list[str] = []
        for _, names in self._active_with_names():
            for n in names:
                if n not in ids:
                    ids.append(n)
        return ids

    def add_recipe(self, yaml_text: str, recipe: KBRecipe) -> KBRecipe:
        """Store a recipe yml + meta in the directory of ``recipe.status``. Returns the stored recipe."""
        with self.lock():
            self.ensure_layout()
            yml = self.status_dir(recipe.status) / f"{recipe.id}.yml"
            recipe = recipe.model_copy(update={"path": str(yml)})
            atomic_write(yml, yaml_text.rstrip("\n") + "\n")
            self._write_recipe_meta(recipe)
            self._index_recipe(recipe)
            self._save_index()
            return recipe

    def _move_recipe(self, recipe: KBRecipe, status: str, extra: Optional[dict] = None,
                     **updates: Any) -> KBRecipe:
        """Crash-safe order (round 10): the new meta is written beside the destination first, then the yml moves,
        then the old meta goes. A crash in between leaves either the old state (the yml still in its directory; an
        orphan meta is ignored — recipes are found by their yml) or the new one."""
        with self.lock():
            src_yml = Path(recipe.path)
            src_meta = self._meta_path(src_yml)
            merged_extra = {**self._recipe_meta_extra(recipe), **(extra or {})}
            dst_dir = self.status_dir(status)
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_yml = dst_dir / src_yml.name
            new = recipe.model_copy(update={"status": status, "path": str(dst_yml), **updates})
            self._write_recipe_meta(new, merged_extra)
            if src_yml != dst_yml:
                os.replace(src_yml, dst_yml)
                if src_meta.exists() and src_meta != self._meta_path(dst_yml):
                    src_meta.unlink()
            self._index_recipe(new)
            self._save_index()
            return new

    def promote(self, recipe_id: str) -> KBRecipe:
        with self.lock():
            r = self.get_recipe(recipe_id)
            if r is None:
                raise KeyError(f"unknown recipe {recipe_id}")
            if r.status == "active":
                return r
            new = self._move_recipe(r, "active", promoted_at=utcnow())
        log.info("kb recipe %s promoted to active", new.id)
        return new

    def reject(self, recipe_id: str, reason: str = "") -> KBRecipe:
        with self.lock():
            r = self.get_recipe(recipe_id)
            if r is None:
                raise KeyError(f"unknown recipe {recipe_id}")
            extra = {"rejected_at": utcnow().isoformat(), "rejected_reason": reason}
            new = self._move_recipe(r, "rejected", extra=extra)
        log.info("kb recipe %s rejected: %s", new.id, reason)
        return new

    def credit_recipe(self, recipe_id: str, success: bool, project_fingerprint: str) -> Optional[KBRecipe]:
        """Count a success/failure of a KB recipe; auto-promotes a candidate after success in
        ``kb.auto_promote_after_projects`` distinct projects without failure. Returns the (possibly promoted)
        recipe, None for unknown ids. Promotions are also queued for :meth:`drain_promoted`."""
        with self.lock():
            r = self.get_recipe(recipe_id)
            if r is None:
                log.debug("credit for unknown kb recipe %s ignored", recipe_id)
                return None
            if success:
                r.success_count += 1
                if project_fingerprint and project_fingerprint not in r.projects:
                    r.projects.append(project_fingerprint)
            else:
                r.fail_count += 1
            self._write_recipe_meta(r, self._recipe_meta_extra(r))
            n = self.cfg.kb.auto_promote_after_projects
            if r.status == "candidate" and n > 0 and r.fail_count == 0 and r.success_count > 0 \
                    and len(r.projects) >= n:
                r = self.promote(r.id)
                if r.id not in self._promoted:
                    self._promoted.append(r.id)
            return r

    def drain_promoted(self) -> list[str]:
        """Recipe ids auto-promoted since the last call (by the loop, the plan credit or the harvest)."""
        out, self._promoted = self._promoted, []
        return out

    def recipe_meta_extra(self, recipe_id: str) -> dict:
        r = self.get_recipe(recipe_id)
        return self._recipe_meta_extra(r) if r else {}

    # ------------------------------------------------------------------ patterns
    def load_patterns(self) -> list[dict]:
        out: list[dict] = []
        if not self.patterns_dir.exists():
            return out
        for p in sorted(list(self.patterns_dir.glob("*.yml")) + list(self.patterns_dir.glob("*.yaml"))):
            try:
                docs = list(yaml.safe_load_all(p.read_text(encoding="utf-8")))
            except (OSError, yaml.YAMLError) as e:
                log.warning("unreadable kb pattern %s: %s", p, e)
                continue
            for d in docs:
                items = d if isinstance(d, list) else [d]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    regex = (item.get("match") or {}).get("signature_regex") if isinstance(
                        item.get("match"), dict) else None
                    if not regex:
                        log.warning("kb pattern without match.signature_regex in %s", p)
                        continue
                    try:
                        item["_re"] = re.compile(regex)
                    except re.error as e:
                        log.warning("bad regex in kb pattern %s: %s", p, e)
                        continue
                    item.setdefault("id", p.stem)
                    item["_file"] = str(p)
                    out.append(item)
        return out

    # ------------------------------------------------------------------ match
    @staticmethod
    def _cluster_signatures(cluster: ErrorCluster) -> tuple[list[str], list[str]]:
        l1: list[str] = []
        for s in [*cluster.signatures, *(e.signature for e in cluster.errors)]:
            if s and s not in l1:
                l1.append(s)
        return l1, list(cluster.generic_signatures)

    def _resolve_path(self, ws: Workspace, path: str) -> Optional[str]:
        if ws.exists(path):
            return path
        hits = ws.glob(_basename(path))
        hits = [h for h in hits if _basename(h) == _basename(path)]
        return hits[0].replace("\\", "/") if len(hits) == 1 else None

    def replay_view(self, case: KBCase, ws: Workspace) -> Optional[KBCase]:
        """Return a copy of ``case`` with paths resolved against ``ws`` if its patch can be replayed, else None.
        A case whose text had a secret masked (tag ``no-replay:redacted``) is never replayed (hint only)."""
        if case.fix_kind != "llm_patch" or not case.changes or NO_REPLAY_TAG in case.tags:
            return None
        new_changes = []
        for ch in case.changes:
            if ch.op == "modify":
                if not ch.search:
                    return None
                p = self._resolve_path(ws, ch.path)
                if p is None:
                    return None
                try:
                    text = ws.read(p)
                except OSError:
                    return None
                if text.count(ch.search) != 1:
                    return None
                new_changes.append(ch.model_copy(update={"path": p}))
            elif ch.op == "create":
                if ws.exists(ch.path):
                    return None
                new_changes.append(ch)
            elif ch.op == "delete":
                p = self._resolve_path(ws, ch.path)
                if p is None:
                    return None
                new_changes.append(ch.model_copy(update={"path": p}))
            elif ch.op == "rename":
                p = self._resolve_path(ws, ch.path)
                if p is None or not ch.new_path or ws.exists(ch.new_path):
                    return None
                new_changes.append(ch.model_copy(update={"path": p}))
            else:  # pragma: no cover - Literal guards this
                return None
        return case.model_copy(update={"changes": new_changes})

    @staticmethod
    def case_hint(case: KBCase) -> str:
        files: list[str] = []
        for ch in case.changes:
            b = _basename(ch.path)
            if b not in files:
                files.append(b)
        summary = case.summary or (case.signatures_generic[0] if case.signatures_generic else
                                   case.signatures[0] if case.signatures else "fix")
        return f"[case {case.id}, reused {case.reuse_count}×] {summary} — changed: {', '.join(files) or '-'}"

    def match(self, cluster: ErrorCluster, ws: Workspace, profile: ProjectProfile) -> KBMatch:
        kbc = self.cfg.kb
        out = KBMatch()
        if not self.root.exists():
            return out
        l1, l2 = self._cluster_signatures(cluster)
        all_sigs = [*l1, *l2]
        catalog_on = bool(kbc.catalog) and self.catalog().index_path.exists()
        rejected: set[str] = set()
        if catalog_on:
            out.case_hits = self.catalog().match(cluster.errors)     # never raises
            # a case rejected by a person also switches off the KB patch replay, KB recipes and symbol hints of its
            # signatures (round 10) — not only its own action and hints
            rejected = self.catalog().rejected_signatures(all_sigs)
            if rejected:
                l1 = [s for s in l1 if s not in rejected]
                l2 = [s for s in l2 if s not in rejected]
        sigs = set(l1) | set(l2)
        idx = self._load_index()

        # ---- cases (L1 first, then L2)
        case_ids: list[str] = []
        for table, keys in ((idx["l1"], l1), (idx["l2"], l2)):
            for s in keys:
                for cid in table.get(s, []):
                    if cid not in case_ids:
                        case_ids.append(cid)
        cases = [c for c in (self.get_case(cid) for cid in case_ids) if c is not None]
        cases.sort(key=lambda c: c.created_at, reverse=True)
        cases.sort(key=lambda c: c.reuse_count, reverse=True)       # stable: reuse desc, then newest
        out.cases = cases[:MAX_MATCH_CASES]

        if kbc.use_patch_replay:
            for c in out.cases:
                view = self.replay_view(c, ws)
                if view is not None:
                    out.replayable.append(view)

        # ---- recipes
        if kbc.use_active_recipes and sigs:
            wanted: list[str] = []
            for s in [*l1, *l2]:
                for rid in idx["recipes"].get(s, []):
                    if rid not in wanted:
                        wanted.append(rid)
            by_id = {r.id: r for r in self.list_recipes()}
            # also catch recipes whose meta was edited by hand without reindex
            for r in by_id.values():
                if r.id not in wanted and sigs.intersection(r.signatures):
                    wanted.append(r.id)
            for rid in wanted:
                r = by_id.get(rid)
                if r is None or not sigs.intersection(r.signatures):
                    continue
                if r.status == "active":
                    out.recipe_ids.append(r.id)
                    out.recipe_files.append(r.path)
                elif r.status == "candidate":
                    out.candidate_recipe_ids.append(r.id)
                    out.candidate_recipe_files.append(r.path)

        # ---- patterns
        pattern_hints: list[str] = []
        for pat in self.load_patterns():                             # hand-written patterns: never switched off
            rx = pat["_re"]
            if not any(rx.search(s) for s in all_sigs):
                continue
            if kbc.use_active_recipes:
                for rid in pat.get("recipes") or []:
                    if isinstance(rid, str) and rid not in out.recipe_ids:
                        out.recipe_ids.append(rid)
            for hid in pat.get("handler_ids") or []:
                if isinstance(hid, str) and hid not in out.handler_ids:
                    out.handler_ids.append(hid)
            for h in pat.get("hints") or []:
                if isinstance(h, str):
                    pattern_hints.append(f"[pattern {pat['id']}] {h}")

        # ---- hints (symbol hints first: "error-case symbol S was fixed like this in N projects")
        if kbc.use_hints and kbc.max_hints > 0:
            symbol_hints: list[str] = []
            if kbc.symbol_index and self.symbols_path.exists():
                try:
                    errs = [e for e in cluster.errors
                            if not (e.signature_generic in rejected or (e.signature and e.signature in rejected))]
                    sym = symbol_for_errors(errs) if errs else ""
                    symbol_hints = self.symbol_index().hints(sym) if sym else []
                except Exception as e:  # pragma: no cover - hints are best effort
                    log.debug("symbol hints failed: %s", e)
            hints = symbol_hints + pattern_hints + [self.case_hint(c) for c in out.cases]
            # error cases (2026-10-23): past fixes AND past failures of the same L2 / error symbol, after the
            # existing hints; with the case catalog they come from the case hits (people's hints first), else from
            # the raw error-hints index; max_hints still caps the whole list
            if catalog_on:
                extra = self._case_hit_hints(out.case_hits, auto=kbc.error_hints)
            else:
                extra = self._error_hints(cluster, l2) if kbc.error_hints else []
            for line in extra:
                if line not in hints:
                    hints.append(line)
            out.hints = hints[: kbc.max_hints]
        return out

    def _case_hit_hints(self, hits: list, *, auto: bool = True) -> list[str]:
        """Hint lines of the case hits: every hit's ``human.hints`` first, then the generated ones."""
        cat = self.catalog()
        human: list[str] = []
        generated: list[str] = []
        try:
            for h in hits:
                case = cat.get(h.case_id)
                if case is None:
                    continue
                human += cat.hint_lines(case, human=True, auto=False)
                if auto:
                    generated += cat.hint_lines(case, human=False, auto=True)
        except Exception as e:  # pragma: no cover - hints are best effort
            log.debug("case hints failed: %s", e)
        return human + generated

    def _error_hints(self, cluster: ErrorCluster, l2: list[str]) -> list[str]:
        store = self.error_case_store()
        if not store.hints_path.exists():
            return []
        lines: list[str] = []
        try:
            symbols: list[str] = []
            for e in cluster.errors:
                sym = symbol_for_error(e)
                if sym and sym not in symbols:
                    symbols.append(sym)
            lines = store.hints_for_keys(l2, symbols)
        except Exception as e:  # pragma: no cover - hints are best effort
            log.debug("error-case hints failed: %s", e)
        return lines

    # ------------------------------------------------------------------ harvest
    def harvest(self, phase_result: PhaseResult, profile: ProjectProfile, ws: Workspace,
                llm: Optional[LLMClientLike] = None) -> HarvestResult:
        from .harvest import harvest_phase
        return harvest_phase(self, phase_result, profile, ws, llm)

    def record_run(self, report: RunReport, profile: Optional[ProjectProfile],
                   ws: Workspace) -> HarvestResult:
        """Run-level ledger lines (called once from Orchestrator._finalize): unresolved errors of the final
        state, blocked items not already written by a phase harvest, and auto-promotions of the verify
        phase. Never raises."""
        res = HarvestResult()
        try:
            res.recipes_promoted = self.drain_promoted()
            if not (self.cfg.kb.harvest and self.cfg.kb.ledger) or "dry-run" in report.flags:
                return res
            recs = []
            run_id = ws.run_id
            for b in report.blocked:
                if (run_id, b.id) not in self._ledger_blocked:
                    self._ledger_blocked.add((run_id, b.id))
                    recs.append(blocked_record(b, profile, ws))
            if report.final_build is not None and "aborted" not in report.flags:
                recs += unresolved_records(report.unresolved, profile, ws)   # only what the final build left behind
            adv_cfg = getattr(self.cfg, "advisories", None)
            if adv_cfg is None or adv_cfg.ledger:
                for f in report.advisories:
                    if (run_id, f"advisory:{f.advisory_id}") not in self._ledger_blocked:
                        self._ledger_blocked.add((run_id, f"advisory:{f.advisory_id}"))
                        recs.append(advisory_record(f, profile, ws))
            res.ledger_entries = self.ledger().append(recs)
        except Exception as e:
            log.warning("kb.record_run failed: %s", e)
            res.notes.append(f"ledger (run) failed: {e}")
        return res

    def mark_blocked_recorded(self, run_id: str, blocked_id: str) -> None:
        self._ledger_blocked.add((run_id, blocked_id))

    # ------------------------------------------------------------------ stats / transfer
    def stats(self) -> dict[str, Any]:
        """Counts, streamed (no list of every case / record in memory — a fleet KB holds tens of thousands)."""
        idx = self._load_index() if self.root.exists() else self._empty_index()
        n_cases = n_reused = 0
        for c in self.iter_cases():
            n_cases += 1
            n_reused += c.reuse_count > 0
        recipes = self.list_recipes() if self.root.exists() else []
        return {
            "kb_dir": str(self.root),
            "cases": n_cases,
            "cases_reused": n_reused,
            "recipes_active": sum(1 for r in recipes if r.status == "active"),
            "recipes_candidate": sum(1 for r in recipes if r.status == "candidate"),
            "recipes_rejected": sum(1 for r in recipes if r.status == "rejected"),
            "patterns": len(self.load_patterns()),
            "signatures_l1": len(idx["l1"]),
            "signatures_l2": len(idx["l2"]),
            "symbols": len(self.symbol_index()) if self.symbols_path.exists() else 0,
            "ledger_entries": self.ledger().count(),
            "error_cases": self.error_case_store().count_errors(),
            "fix_attempts": self.error_case_store().count_attempts(),
            **self._catalog_stats(),
            **self._script_stats(),
            "reviews": self.error_case_store()._count(self.reviews_path),
        }

    def _script_stats(self) -> dict[str, int]:
        """v2: fix-script cases / versions by status (``ScriptStore.counts``)."""
        try:
            return self.scripts.counts()
        except Exception as e:  # pragma: no cover - stats are informative
            log.debug("fix script stats failed: %s", e)
            return {}

    def _catalog_stats(self) -> dict[str, int]:
        """catalog_cases + counts by effective status (catalog_observed / _candidate / _active / _rejected)."""
        out = {"catalog_cases": 0, **{f"catalog_{s}": 0 for s in ("observed", "candidate", "active", "rejected")}}
        try:
            counts = self.catalog().status_counts()
        except Exception as e:  # pragma: no cover - stats are informative
            log.debug("catalog stats failed: %s", e)
            return out
        out["catalog_cases"] = sum(counts.values())
        out.update({f"catalog_{k}": v for k, v in counts.items()})
        return out

    def export_zip(self, path: str | Path) -> Path:
        """Zip cases/, recipes/, patterns/, catalog/, views/, scripts/ (v2), index.json and the event files
        (reviews.jsonl included; paths relative to the kb root)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        existed = self.root.exists()                     # (taking the lock creates the directory)
        with self.lock():                                # a consistent snapshot: no writer in between
            if existed:
                self.reindex()
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                if existed:
                    for p in sorted(self.root.rglob("*")):
                        if p.is_dir() or p.name.startswith(".") or p.suffix == ".tmp":
                            continue
                        rel = p.relative_to(self.root)
                        if rel.parts[0] not in ("cases", "recipes", "patterns", CATALOG_DIR, VIEWS_DIR,
                                                SCRIPTS_DIR) and rel.as_posix() not in (
                                "index.json", "ledger.jsonl", "symbols.json", *ERROR_CASE_FILES, *AUDIT_FILES,
                                REVIEWS_FILE):
                            continue
                        zf.write(p, rel.as_posix())
        os.replace(tmp, path)
        return path

    def import_zip(self, path: str | Path, merge: bool = True) -> dict[str, int]:
        """Import a kb zip. merge=True merges cases/recipes into the local KB (local status wins);
        merge=False replaces the local KB: the new KB is built in a sibling temp dir and swapped in by rename,
        the old one kept as ``<kb>.bak-<timestamp>`` (round 10 — a crash half-way never leaves a half-deleted KB).
        The case catalog is rebuilt from the merged events: local ``human`` blocks are kept, an imported one is
        taken only for a case the local KB does not have. Returns counters."""
        if not merge:
            return self._import_replace(path)
        with self.lock():
            return self._import_merge(path, merge=True)

    def _import_replace(self, path: str | Path) -> dict[str, int]:
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        parent = self.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        build_dir = Path(tempfile.mkdtemp(prefix=f".{self.root.name}.import-{stamp}-", dir=str(parent)))
        with self.lock():
            try:
                fresh = FileKnowledgeBase(self.cfg, kb_dir=build_dir)
                counters = fresh._import_merge(path, merge=False)
                old = self.root
                for keep in (old.rglob(".gitkeep") if old.exists() else []):   # the repo's tracked layout files
                    dst = build_dir / keep.relative_to(old)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copyfile(keep, dst)
                backup = None
                if old.exists():
                    backup = parent / f"{old.name}.bak-{stamp}"
                    n = 1
                    while backup.exists():
                        n += 1
                        backup = parent / f"{old.name}.bak-{stamp}-{n}"
                    os.replace(old, backup)
                try:
                    os.replace(build_dir, old)
                except OSError:
                    if backup is not None:                # put the previous KB back where it was
                        os.replace(backup, old)
                    raise
                if backup is not None:
                    try:                                  # our lock moved with the old KB
                        (backup / LOCK_NAME).unlink()
                    except OSError:
                        pass
                    counters["backup"] = str(backup)      # type: ignore[assignment]
                    log.info("kb import --replace: the previous KB is kept in %s", backup)
            except BaseException:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise
            self._index, self._index_stamp, self._error_store, self._catalog = None, None, None, None
            self._scripts = None
            clear_catalog_caches()
        return counters

    def _import_merge(self, path: str | Path, merge: bool) -> dict[str, int]:
        counters = {"cases_added": 0, "cases_merged": 0, "recipes_added": 0, "recipes_merged": 0,
                    "patterns_added": 0, "ledger_added": 0, "error_cases_added": 0, "fix_attempts_added": 0,
                    "case_humans_added": 0, "script_versions_added": 0, "script_versions_merged": 0,
                    "reviews_added": 0}
        local_case_ids = set(self.catalog().ids_on_disk()) if merge else set()
        with tempfile.TemporaryDirectory(prefix="sbmig-kb-import-") as tmpd:
            tmp = Path(tmpd).resolve()
            with zipfile.ZipFile(path) as zf:
                for m in zf.infolist():
                    target = (tmp / m.filename).resolve()
                    if not str(target).startswith(str(tmp) + os.sep):
                        raise ValueError(f"zip slip blocked: {m.filename}")
                zf.extractall(tmp)
            other = FileKnowledgeBase(self.cfg, kb_dir=tmp)
            self._index, self._index_stamp = None, None
            self.ensure_layout()

            for case in other.iter_cases():
                local = self.get_case(case.id)
                if local is None:
                    self._write_case(case)
                    counters["cases_added"] += 1
                else:
                    local.reuse_count = max(local.reuse_count, case.reuse_count)
                    for fp in case.reuse_projects:
                        if fp not in local.reuse_projects:
                            local.reuse_projects.append(fp)
                    if case.recipe_candidate_id and not local.recipe_candidate_id:
                        local.recipe_candidate_id = case.recipe_candidate_id
                    self._write_case(local)
                    counters["cases_merged"] += 1

            for r in other.list_recipes():
                local_r = self.get_recipe(r.id)
                if local_r is None:
                    text = Path(r.path).read_text(encoding="utf-8")
                    extra = other._recipe_meta_extra(r)
                    stored = self.add_recipe(text, r.model_copy(update={"path": ""}))
                    if extra:
                        self._write_recipe_meta(stored, extra)
                    counters["recipes_added"] += 1
                else:
                    local_r.success_count = max(local_r.success_count, r.success_count)
                    local_r.fail_count = max(local_r.fail_count, r.fail_count)
                    for fp in r.projects:
                        if fp not in local_r.projects:
                            local_r.projects.append(fp)
                    for s in r.signatures:
                        if s not in local_r.signatures:
                            local_r.signatures.append(s)
                    for cid in r.source_case_ids:
                        if cid not in local_r.source_case_ids:
                            local_r.source_case_ids.append(cid)
                    self._write_recipe_meta(local_r, self._recipe_meta_extra(local_r))
                    counters["recipes_merged"] += 1

            if other.patterns_dir.exists():
                for p in sorted(other.patterns_dir.iterdir()):
                    if p.is_file() and not (self.patterns_dir / p.name).exists():
                        shutil.copy2(p, self.patterns_dir / p.name)
                        counters["patterns_added"] += 1
            if other.ledger_path.exists():
                known = self.ledger().ids()
                counters["ledger_added"] = self.ledger().append(
                    r for r in other.ledger().iter() if r.id not in known)
            # error cases: lines whose id is not stored yet (old zips have none); reindex rebuilds the hints
            src = other.error_case_store()
            if src.errors_path.exists() or src.attempts_path.exists():
                ne, na = self.error_case_store().append_counts(src.iter_errors(), src.iter_attempts())
                counters["error_cases_added"], counters["fix_attempts_added"] = ne, na
            # v2 fix scripts: into an empty KB as they are; into a local one by sha1 (same code = counts merged,
            # other code = the next local version, parents mapped)
            if other.scripts.scripts_dir.exists():
                if not merge and not self.scripts.scripts_dir.exists():
                    shutil.copytree(other.scripts.scripts_dir, self.scripts.scripts_dir)
                    counters["script_versions_added"] = other.scripts.counts()["script_versions"]
                else:
                    counters.update(self.scripts.merge_from(other.scripts))
            if other.reviews_path.exists():              # structure reviews: ids not stored yet
                have = {r.id for r in self.iter_reviews()}
                new = [r.model_dump_json() for r in other.iter_reviews() if r.id not in have]
                counters["reviews_added"] = append_lines(self.reviews_path, new)
            for name in AUDIT_FILES:                     # audit / quarantine notes: whole lines not stored yet
                src_p, dst_p = other.root / name, self.root / name
                if src_p.exists():
                    have = set(dst_p.read_text(encoding="utf-8", errors="replace").splitlines()) \
                        if dst_p.exists() else set()
                    lines = [ln for ln in src_p.read_text(encoding="utf-8", errors="replace").splitlines()
                             if ln.strip() and ln not in have]
                    append_lines(dst_p, lines)
            counters["case_humans_added"] = self._import_case_humans(other, local_case_ids)
        self.reindex()
        return counters

    def _import_case_humans(self, other: "FileKnowledgeBase", local_ids: set[str]) -> int:
        """Copy the imported case files that carry a ``human`` block for cases the local KB does not have (the
        file is copied as it is, comments included); the reindex that follows recomputes their generated part."""
        n = 0
        try:
            theirs = CaseCatalog(other.root, self.cfg)
            for cid, path in theirs.ids_on_disk().items():
                if cid in local_ids:
                    continue
                human = theirs.human_of_file(path)
                if human == type(human)():
                    continue
                dst = self.catalog().catalog_dir / path.relative_to(theirs.catalog_dir)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dst)
                n += 1
        except Exception as e:  # pragma: no cover - the events are imported; human blocks are a bonus
            log.warning("case catalog import failed: %s: %s", type(e).__name__, e)
        return n
