"""File-based knowledge base (no DB, invariant 3): error cases, the case catalog, v2 fix scripts, structure reviews.

Layout under ``cfg.kb_dir`` (created on first use)::

    errors.jsonl                        append-only ErrorCaseRecords: every build error of every run (2026-10-23)
    fix_attempts.jsonl                  append-only FixAttemptRecords: every fix attempt, failures included, + diff
    catalog/<kind>/<class>/<symbol>--<id>.yml   one reusable case per L2 signature (sbmig.kb.catalog; `human:`
                                        block edited by people, the rest derived from errors.jsonl + scripts/)
    catalog/index.json                  case id → file, L2 / L1 / symbol → case id (derived)
    views/cases.md, views/cases.csv     generated case tables for people (derived)
    scripts/<case-id>/v<N>.py + .meta.json   v2 fix scripts per case (sbmig.kb.scripts, ``FileKnowledgeBase.scripts``)
    reviews.jsonl                       v2 structure reviews, one line per run (``record_review``)
    audit.jsonl                         human case edits + automatic case status / action transitions (round 10),
                                        fix-script versions created / credited / retired / activated (v2)
    quarantine.jsonl                    notes about errors.jsonl lines that could not be read (round 10)
    *.jsonl.corrupt                     torn last lines moved aside before the next append (round 10)
    .lock                               the cross-process write lock (sbmig.kb.lock)

PLAN-v2 step 4 removed the v1 stores (``cases/`` patch cases, ``index.json``, ``recipes/``, ``patterns/``,
``ledger.jsonl``, ``symbols.json``, ``error_hints.json``): nothing reads or writes them any more and an export leaves
them out.

All writes are atomic (temp file + ``os.replace``) or torn-line safe appends (``sbmig.kb.jsonl``), and run under the
cross-process lock ``.lock`` (round 10: the web server, a fleet and ``sbmig kb …`` share one KB; re-entrant, waits
up to ``kb.lock_timeout_seconds``, never fails a run).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterator, Optional

from sbmig.core.config import Config
from sbmig.core.models import (
    ErrorCaseRecord,
    ErrorCluster,
    FixAttemptRecord,
    KBMatch,
    ProjectProfile,
    StructureReview,
    utcnow,
)
from sbmig.core.workspace import Workspace

from .catalog import CATALOG_DIR, VIEWS_DIR, CaseCatalog, _parse_case_text
from .catalog import INDEX_VERSION as CATALOG_INDEX_VERSION
from .catalog import clear_caches as clear_catalog_caches
from .error_cases import ErrorCaseStore
from .jsonl import append_lines, line_ids, parse_record
from .lock import LOCK_NAME, kb_lock
from .redact import get_redactor
from .scripts import SCRIPTS_DIR, ScriptStore

log = logging.getLogger("sbmig.kb")

AUDIT_FILES = ("audit.jsonl", "quarantine.jsonl")
ERROR_CASE_FILES = ("errors.jsonl", "fix_attempts.jsonl")
REVIEWS_FILE = "reviews.jsonl"           # v2 structure reviews (one line per run)
REVIEW_TEXT_LIMIT = 4000
MAX_MATCH_HINTS = 10                     # hint lines of the case hits in one KBMatch
EXPORT_DIRS = (CATALOG_DIR, VIEWS_DIR, SCRIPTS_DIR)
EXPORT_FILES = (*ERROR_CASE_FILES, REVIEWS_FILE, *AUDIT_FILES)


def _line_ok(line: str, model: Any) -> bool:
    """A jsonl line reads: as ``model`` (a torn record glued in front is tolerated, as the readers do), or as a JSON
    object when ``model`` is None."""
    if model is not None:
        return parse_record(line, model) is not None
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


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


class FileKnowledgeBase:
    """The file KB under ``cfg.kb_dir``: ``KnowledgeBaseLike.match`` (case hits of the catalog) and ``reindex``, plus
    the v2 ``ScriptKnowledgeLike`` part (``scripts``, ``record_review``) and the error-case store."""

    def __init__(self, cfg: Config, kb_dir: Optional[Path] = None):
        self.cfg = cfg
        self.root = Path(kb_dir) if kb_dir else cfg.kb_dir
        self._error_store: Optional[ErrorCaseStore] = None   # cached: keeps the known-id sets warm across a fleet
        self._catalog: Optional[CaseCatalog] = None
        self._scripts: Optional[ScriptStore] = None

    # ------------------------------------------------------------------ layout
    @property
    def lock_timeout(self) -> float:
        return float(getattr(self.cfg.kb, "lock_timeout_seconds", 300) or 0)

    def lock(self):
        """The cross-process KB lock (``<kb>/.lock``, re-entrant) — hold it around every read-modify-write."""
        return kb_lock(self.root, self.lock_timeout)

    def error_case_store(self) -> ErrorCaseStore:
        """errors.jsonl / fix_attempts.jsonl (one instance per KB: the known-id sets are read once and again only
        when a file changes)."""
        if self._error_store is None or self._error_store.root != self.root:
            self._error_store = ErrorCaseStore(self.root, self.lock_timeout)
        return self._error_store

    def catalog(self) -> CaseCatalog:
        """The case catalog (kb/catalog/, kb/views/) of this KB."""
        if self._catalog is None or self._catalog.root != self.root:
            self._catalog = CaseCatalog(self.root, self.cfg)
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
        """Append a run's prepared error / attempt records (ids already stored are skipped) and update (``kb.catalog``)
        the case catalog + views. Returns the number of new records (errors + attempts). Never raises."""
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

    # ------------------------------------------------------------------ catalog: reindex / match
    def reindex(self) -> int:
        """Rebuild the case catalog (``catalog/`` + its index, human blocks kept) and the views from
        ``errors.jsonl`` and the fix-script store. Returns the number of cases (0 with ``kb.catalog: false`` or
        nothing to build from). Never raises (a warning instead)."""
        with self.lock():
            cat = self.catalog()
            if not self.cfg.kb.catalog or not (self.error_case_store().errors_path.exists()
                                               or cat.catalog_dir.exists()):
                return 0
            try:
                n = cat.rebuild()
                cat.write_views()
                return n
            except Exception as e:
                log.warning("case catalog rebuild failed: %s: %s", type(e).__name__, e)
                return 0

    def match(self, cluster: ErrorCluster, ws: Workspace, profile: ProjectProfile) -> KBMatch:
        """The catalog's case hits of the cluster's errors (``case_hits``: effective status / action, ``pinned`` when
        a person set the action — the fix loop obeys a pinned ``block``) and their hint lines (people's hints
        first; ``hints``). Empty without a catalog. Never raises."""
        out = KBMatch()
        if not self.cfg.kb.catalog or not self.root.exists():
            return out
        cat = self.catalog()
        if not cat.index_path.exists():
            return out
        out.case_hits = cat.match(cluster.errors)     # never raises
        out.hints = self._case_hit_hints(out.case_hits)[:MAX_MATCH_HINTS]
        return out

    def _case_hit_hints(self, hits: list) -> list[str]:
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
                generated += cat.hint_lines(case, human=False, auto=True)
        except Exception as e:  # pragma: no cover - hints are best effort
            log.debug("case hints failed: %s", e)
        return human + generated

    # ------------------------------------------------------------------ stats / transfer
    def stats(self) -> dict[str, Any]:
        """Counts, streamed (no list of every record in memory — a fleet KB holds tens of thousands): error cases,
        fix attempts, catalog cases by status, fix-script cases / versions by status, structure reviews."""
        store = self.error_case_store()
        return {
            "kb_dir": str(self.root),
            "error_cases": store.count_errors(),
            "fix_attempts": store.count_attempts(),
            **self._catalog_stats(),
            **self._script_stats(),
            "reviews": store._count(self.reviews_path),
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

    def validate(self, limit: int = 20) -> list[str]:
        """Integrity problems of the KB files, one line each (``sbmig kb validate``): lines of errors.jsonl /
        fix_attempts.jsonl / reviews.jsonl that do not validate against their record model, lines of audit.jsonl /
        quarantine.jsonl that are no JSON object, an unreadable catalog index, case files whose generated part or
        ``human:`` block does not read, and the fix-script store's own check (:meth:`ScriptStore.check`). At most
        ``limit`` lines per file are listed (then a count). Read-only."""
        problems: list[str] = []
        models = ((ERROR_CASE_FILES[0], ErrorCaseRecord), (ERROR_CASE_FILES[1], FixAttemptRecord),
                  (REVIEWS_FILE, StructureReview), *((name, None) for name in AUDIT_FILES))
        for name, model in models:
            path, bad = self.root / name, []
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if line.strip() and not _line_ok(line, model):
                            bad.append(n)
            except OSError as e:
                problems.append(f"{name}: unreadable ({e})")
                continue
            what = f"does not validate as {model.__name__}" if model is not None else "is no JSON object"
            problems += [f"{name}: line {n} {what}" for n in bad[:limit]]
            if len(bad) > limit:
                problems.append(f"{name}: … {len(bad) - limit} more unreadable line(s)")
        cat = self.catalog()
        if cat.index_path.exists():
            try:
                idx = json.loads(cat.index_path.read_text(encoding="utf-8"))
                if not isinstance(idx, dict) or idx.get("version") != CATALOG_INDEX_VERSION:
                    problems.append(f"{CATALOG_DIR}/index.json: unknown format (`sbmig kb reindex` rebuilds it)")
            except (OSError, ValueError) as e:
                problems.append(f"{CATALOG_DIR}/index.json: does not parse ({type(e).__name__}; `sbmig kb reindex`)")
        if cat.catalog_dir.exists():
            bad_cases = []
            for p in sorted(cat.catalog_dir.rglob("*.yml")):
                rel = p.relative_to(self.root).as_posix()
                try:
                    ld = _parse_case_text(p.read_text(encoding="utf-8-sig"), p)
                except OSError as e:
                    bad_cases.append(f"{rel}: unreadable ({e})")
                    continue
                if ld.case is None:
                    bad_cases.append(f"{rel}: the generated part does not read (`sbmig kb reindex` regenerates it)")
                if not ld.human_valid:
                    bad_cases.append(f"{rel}: the `human:` block is invalid (ignored until fixed)")
            problems += bad_cases[:limit]
            if len(bad_cases) > limit:
                problems.append(f"{CATALOG_DIR}: … {len(bad_cases) - limit} more problem(s)")
        try:
            problems += self.scripts.check()
        except Exception as e:  # noqa: BLE001 - the check must report, not crash
            problems.append(f"{SCRIPTS_DIR}: check failed ({type(e).__name__}: {e})")
        return problems

    def export_zip(self, path: str | Path) -> Path:
        """Zip catalog/, views/, scripts/ and the event files (errors / fix attempts / reviews / audit / quarantine;
        paths relative to the kb root) after a reindex — a consistent snapshot under the KB lock."""
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
                        if rel.parts[0] not in EXPORT_DIRS and rel.as_posix() not in EXPORT_FILES:
                            continue
                        zf.write(p, rel.as_posix())
        os.replace(tmp, path)
        return path

    def import_zip(self, path: str | Path, merge: bool = True) -> dict[str, int]:
        """Import a kb zip. merge=True merges the events, fix scripts and reviews into the local KB; merge=False
        replaces the local KB: the new KB is built in a sibling temp dir and swapped in by rename,
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
            self._error_store, self._catalog, self._scripts = None, None, None
            clear_catalog_caches()
        return counters

    def _import_merge(self, path: str | Path, merge: bool) -> dict[str, int]:
        counters = {"error_cases_added": 0, "fix_attempts_added": 0, "case_humans_added": 0,
                    "script_versions_added": 0, "script_versions_merged": 0, "reviews_added": 0}
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
            self.root.mkdir(parents=True, exist_ok=True)
            # error cases: lines whose id is not stored yet
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
