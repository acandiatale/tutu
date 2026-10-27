"""The file KB facade (v2.1, docs/v2/PLAN-v2.1.md §3.4) — what the orchestrator (``self.kb``), the fleet and the web
server hold. Everything lives in :class:`sbmig.errors.cases.CaseStore`; this class adds the cluster lookup
(``match``), the export / import of the whole KB and the never-raise wrappers a run needs.

Layout under ``cfg.kb_dir`` (``kb.dir``, default ``kb-v2/``): ``errors.jsonl``, ``cases.md``,
``cases/<case_id>/case.json`` + ``fix_v<N>.py``, ``.lock``. Nothing else is read or written (the round-10 / v2
catalog, views, audit, quarantine, reviews, fix_attempts, scripts/*/meta.json and index.json are gone; an old KB
directory is simply not looked at).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.models import CaseAction, CaseHit, ErrorCluster, ErrorEntry, KBMatch, utcnow
from sbmig.errors.cases import CASES_DIR, CASES_MD, KB_ERRORS, CaseStore
from sbmig.errors.common import atomic_write, case_id_of

from .lock import LOCK_NAME, kb_lock

log = logging.getLogger("sbmig.kb")

MAX_MATCH_HINTS = 10
HINT_CHARS = 400
EXPORT_FILES = (KB_ERRORS, CASES_MD)

__all__ = ["FileKnowledgeBase", "atomic_write"]


class FileKnowledgeBase:
    """``kb.cases`` (the CaseStore), ``match``, ``record_run``, ``reindex``, ``stats``, ``validate``,
    ``export_zip`` / ``import_zip``, ``lock``."""

    def __init__(self, cfg: Config, kb_dir: Optional[Path] = None):
        self.cfg = cfg
        self.root = Path(kb_dir) if kb_dir else cfg.kb_dir
        self._cases: Optional[CaseStore] = None

    @property
    def lock_timeout(self) -> float:
        return float(getattr(self.cfg.kb, "lock_timeout_seconds", 300) or 0)

    def lock(self):
        """The cross-process KB lock (``<kb>/.lock``, re-entrant)."""
        return kb_lock(self.root, self.lock_timeout)

    @property
    def script_runs(self) -> int:
        """``errors.script_runs``: ScriptRun records kept per stored script version (v2.1e; 0 = no cap)."""
        try:
            return max(0, int(getattr(getattr(self.cfg, "errors", None), "script_runs", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def cases(self) -> CaseStore:
        if self._cases is None or self._cases.root != self.root:
            self._cases = CaseStore(self.root, self.cfg, lock_timeout=self.lock_timeout,
                                    script_runs=self.script_runs)
        return self._cases

    # ------------------------------------------------------------------ the fix loop
    def match(self, cluster: ErrorCluster, ws: Any = None, profile: Any = None) -> KBMatch:
        """What the KB has for the cluster's case(s): a CaseHit per case with a usable stored script (action
        ``script`` with the version :meth:`CaseStore.best` picks; status active = proven, candidate = unverified) or
        a person's ``block`` pin (``pinned``; the triage obeys it), and hint lines (the case's last solution). Empty
        for an unknown case. Never raises."""
        out = KBMatch()
        try:
            if not self.cases.cases_dir.is_dir():
                return out
            ids = list(dict.fromkeys([c for c in [cluster.case_id] if c] + [case_id_of(e) for e in cluster.errors]))
            for cid in ids:
                case = self.cases.find(cid)
                if case is None:
                    continue
                errs = [e for e in cluster.errors if case_id_of(e) == cid] or \
                    (list(cluster.errors) if cid == cluster.case_id else [])
                pin = self.cases.pin_of(cid)
                if pin:
                    reason = pin.split(":", 1)[1].strip() if ":" in pin else ""
                    out.case_hits.append(CaseHit(case_id=cid, name=case.name, status="observed",
                                                 action=CaseAction(kind="block", block_reason=reason),
                                                 errors=errs, pinned=True))
                    continue
                if case.last_solution:
                    text = " ".join(case.last_solution.split())[:HINT_CHARS]
                    out.hints.append(f"case {cid} ({case.name or case.l2}; seen in {len(case.projects)} project(s)): "
                                     f"last solution — {text}")
                best = self.cases.best(cid)
                if best is None:
                    continue
                s = best[0]
                out.case_hits.append(CaseHit(case_id=cid, name=case.name,
                                             status="active" if s.status == "proven" else "candidate",
                                             action=CaseAction(kind="script", script_version=s.version),
                                             errors=errs, hints=out.hints[-1:] if case.last_solution else []))
            out.hints = out.hints[:MAX_MATCH_HINTS]
        except Exception as e:  # noqa: BLE001 - a KB lookup never breaks the loop
            log.warning("kb.match failed for %s: %s: %s", getattr(cluster, "id", "?"), type(e).__name__, e)
        return out

    def record_run(self, entries: Iterable[ErrorEntry]) -> int:
        """The run's finished error list into the KB (:meth:`CaseStore.record_run`). Never raises (0 + a warning)."""
        try:
            return self.cases.record_run(entries)
        except Exception as e:  # noqa: BLE001
            log.warning("kb.record_run failed: %s: %s", type(e).__name__, e)
            return 0

    def reindex(self) -> int:
        """Recount every case from ``errors.jsonl`` and rewrite ``cases.md`` (v2.1e: also every stored script's run
        history, augment lineage and last failure, and the as-is / to-be hunks of entries stored without them —
        :meth:`CaseStore.recount`). Returns the number of cases (0 for a KB that does not exist yet — nothing is
        created). Never raises."""
        if not self.root.exists():
            return 0
        try:
            return self.cases.recount()
        except Exception as e:  # noqa: BLE001
            log.warning("kb reindex failed: %s: %s", type(e).__name__, e)
            return 0

    def stats(self) -> dict[str, Any]:
        return self.cases.stats()

    def validate(self, limit: int = 20) -> list[str]:
        return self.cases.validate(limit)

    # ------------------------------------------------------------------ transfer
    def export_zip(self, path: str | Path) -> Path:
        """Zip ``cases/``, ``errors.jsonl`` and ``cases.md`` (paths relative to the KB root) under the KB lock."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        existed = self.root.exists()                      # (taking the lock creates the directory)
        with self.lock():
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                if existed:
                    for p in sorted(self.root.rglob("*")):
                        if p.is_dir() or p.name.startswith(".") or p.suffix == ".tmp":
                            continue
                        rel = p.relative_to(self.root)
                        if rel.parts[0] == CASES_DIR or rel.as_posix() in EXPORT_FILES:
                            zf.write(p, rel.as_posix())
        os.replace(tmp, path)
        return path

    def import_zip(self, path: str | Path, merge: bool = True) -> dict[str, Any]:
        """Import an exported KB. ``merge`` (default): new error lines, unknown cases, scripts of known cases by their
        code (:meth:`CaseStore.merge_from`; importing the same zip twice changes nothing). ``merge=False``: the KB is
        replaced — built in a sibling temp dir and swapped in by rename, the old one kept as
        ``<kb>.bak-<timestamp>``. Returns counters."""
        if not merge:
            return self._import_replace(path)
        with self.lock():
            return self._import_into(self, path)

    @staticmethod
    def _import_into(kb: "FileKnowledgeBase", path: str | Path) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="sbmig-kb-import-") as tmpd:
            tmp = Path(tmpd).resolve()
            with zipfile.ZipFile(path) as zf:
                for m in zf.infolist():
                    target = (tmp / m.filename).resolve()
                    if not str(target).startswith(str(tmp) + os.sep):
                        raise ValueError(f"zip slip blocked: {m.filename}")
                zf.extractall(tmp)
            kb.root.mkdir(parents=True, exist_ok=True)
            return kb.cases.merge_from(CaseStore(tmp, kb.cfg, lock_timeout=kb.lock_timeout,
                                                 script_runs=kb.script_runs))

    def _import_replace(self, path: str | Path) -> dict[str, Any]:
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        parent = self.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        build_dir = Path(tempfile.mkdtemp(prefix=f".{self.root.name}.import-{stamp}-", dir=str(parent)))
        with self.lock():
            try:
                counters = self._import_into(FileKnowledgeBase(self.cfg, kb_dir=build_dir), path)
                old = self.root
                for keep in (old.rglob(".gitkeep") if old.exists() else []):   # the repo's tracked layout files
                    dst = build_dir / keep.relative_to(old)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copyfile(keep, dst)
                for stray in build_dir.glob(LOCK_NAME + "*"):                  # the temp KB's own lock
                    stray.unlink(missing_ok=True)
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
                    (backup / LOCK_NAME).unlink(missing_ok=True)    # our lock moved with the old KB
                    counters["backup"] = str(backup)
                    log.info("kb import --replace: the previous KB is kept in %s", backup)
            except BaseException:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise
            self._cases = None
        return counters
