"""파일 기반 KB를 다루는 모듈. 오케스트레이터(``self.kb``), 플릿, 웹 서버가 이 모듈로 KB를 읽고 쓴다.
데이터는 :class:`sbmig.errors.cases.CaseStore`가 관리하고, 여기서는 아래 기능을 제공한다.
- ``match``: 빌드 오류에 맞는 저장된 수정 스크립트를 찾는다
- ``record_run``: 실행이 끝난 뒤 오류 목록을 KB에 기록한다
- ``reindex``: 재서명한 뒤 KB 통계를 다시 계산한다
- ``export_zip`` / ``import_zip``: KB 전체를 내보내고 가져온다
실행 중에는 예외를 밖으로 던지지 않는다.
KB는 파일로만 관리한다(DB 사용 금지).

The file KB facade (v2.1, docs/v2/PLAN-v2.1.md §3.4) — what the orchestrator (``self.kb``), the fleet and the web
server hold. Everything lives in :class:`sbmig.errors.cases.CaseStore`; this class adds the cluster lookup
(``match``), the export / import of the whole KB and the never-raise wrappers a run needs.

Layout under ``cfg.kb_dir`` (``kb.dir``, default ``kb-v2/``): ``errors.jsonl``, ``cases.md``,
``cases/<case_id>/case.json`` + ``fix_v<N>.py``, ``.lock``. Nothing else is read or written (the round-10 / v2
catalog, views, audit, quarantine, reviews, fix_attempts, scripts/*/meta.json and index.json are gone; an old KB
directory is simply not looked at).

재서명은 이전 서명 규칙으로 기록된 항목을 현재 케이스 ID로 옮기는 작업이다.
- ``reindex``는 재서명을 먼저 하고 다시 집계한다
- 프로세스의 첫 ``match``와 ``record_run`` 직후에도 필요하면 자동으로 실행한다.
  실패해도 WARNING만 남기고 실행은 계속한다

Re-sign (v2.1z-ai): ``reindex`` re-signs first (:meth:`FileKnowledgeBase.resign`), then recounts. Automatically too,
never failing a run: on the first ``match`` of a process per KB directory (``kb.json`` missing / older while the KB
has records) and after a ``record_run`` that leaves a line signed by older rules. ``match`` leaves out a stored case
whose L2 is not the L2 of the cluster's errors (the exact-key guard, :meth:`CaseStore.same_case`).
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
from sbmig.errors.resign import ResignReport, current_version

from .lock import LOCK_NAME, kb_lock, keep_alive

log = logging.getLogger("sbmig.kb")

MAX_MATCH_HINTS = 10
HINT_CHARS = 400
EXPORT_FILES = (KB_ERRORS, CASES_MD)

__all__ = ["FileKnowledgeBase", "atomic_write"]

# 같은 KB 디렉토리는 프로세스당 한 번만 확인하면 되므로 확인한 경로를 기억해 둔다
_FIRST_MATCH_CHECKED: set[str] = set()


class FileKnowledgeBase:
    """KB 디렉토리 하나를 읽고 쓰는 클래스. 실제 데이터는 ``cases``(CaseStore)가 관리한다.

    ``kb.cases`` (the CaseStore), ``match``, ``record_run``, ``reindex``, ``stats``, ``validate``,
    ``export_zip`` / ``import_zip``, ``lock``."""

    def __init__(self, cfg: Config, kb_dir: Optional[Path] = None):
        self.cfg = cfg
        self.root = Path(kb_dir) if kb_dir else cfg.kb_dir
        self._cases: Optional[CaseStore] = None
        self.last_resign: Optional[ResignReport] = None   # v2.1z-ai: the last re-sign's report (`sbmig kb reindex`)

    @property
    def lock_timeout(self) -> float:
        """KB 잠금을 기다리는 최대 시간(초)을 반환한다.
        설정 ``kb.lock_timeout_seconds``를 따르고 기본값은 300이다."""
        return float(getattr(self.cfg.kb, "lock_timeout_seconds", 300) or 0)

    def lock(self):
        """프로세스 간에 KB를 동시에 수정하지 않도록 잠금(``<kb>/.lock``)을 건다.
        같은 프로세스 안에서는 다시 잡을 수 있다.

        The cross-process KB lock (``<kb>/.lock``, re-entrant)."""
        return kb_lock(self.root, self.lock_timeout)

    @property
    def script_runs(self) -> int:
        """저장된 스크립트 버전마다 남길 실행 기록 수의 상한을 반환한다.
        설정 ``errors.script_runs``를 따르고 0이면 제한이 없다.

        ``errors.script_runs``: ScriptRun records kept per stored script version (v2.1e; 0 = no cap)."""
        try:
            return max(0, int(getattr(getattr(self.cfg, "errors", None), "script_runs", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def cases(self) -> CaseStore:
        """CaseStore를 처음 쓸 때 생성한다. KB 경로가 바뀌면 다시 생성한다."""
        if self._cases is None or self._cases.root != self.root:
            self._cases = CaseStore(self.root, self.cfg, lock_timeout=self.lock_timeout,
                                    script_runs=self.script_runs)
        return self._cases

    # ------------------------------------------------------------------ the fix loop
    def match(self, cluster: ErrorCluster, ws: Any = None, profile: Any = None) -> KBMatch:
        """빌드 오류 묶음에 맞는 저장된 수정 스크립트를 찾는다. 수정 루프가 라운드마다 호출한다.
        쓸 만한 스크립트가 있으면 CaseHit를, 사람이 ``block``으로 고정한 케이스면 그 고정 정보를 반환하고,
        마지막 해결 내용을 힌트로 함께 넘긴다.
        모르는 케이스면 빈 결과를 반환한다.
        KB 조회가 실패해도 수정 루프는 계속돼야 하므로 예외를 던지지 않는다.

        What the KB has for the cluster's case(s): a CaseHit per case with a usable stored script (action
        ``script`` with the version :meth:`CaseStore.best` picks; status active = proven, candidate = unverified) or
        a person's ``block`` pin (``pinned``; the triage obeys it), and hint lines (the case's last solution). Empty
        for an unknown case. Never raises."""
        out = KBMatch()
        self._first_match_check()
        try:
            if not self.cases.cases_dir.is_dir():
                return out
            # 클러스터의 case_id와 각 오류의 case_id를 순서대로, 중복 없이 모은다
            ids = list(dict.fromkeys([c for c in [cluster.case_id] if c] + [case_id_of(e) for e in cluster.errors]))
            for cid in ids:
                case = self.cases.find(cid)
                if case is None:
                    continue
                errs = [e for e in cluster.errors if case_id_of(e) == cid] or \
                    (list(cluster.errors) if cid == cluster.case_id else [])
                # 케이스 ID가 같아도 오류 서명(L2)이 다르면 다른 케이스이므로 건너뛴다
                asked = next((e.signature_generic for e in errs if e.signature_generic), "")
                if asked and not self.cases.same_case(cid, asked, case):
                    continue
                # 사람이 case.json에 "block: <이유>"로 고정한 케이스는 스크립트를 찾지 않고 차단 결정을 그대로 넘긴다
                pin = self.cases.pin_of(cid)
                if pin:
                    reason = pin.split(":", 1)[1].strip() if ":" in pin else ""
                    out.case_hits.append(CaseHit(case_id=cid, name=case.name, status="observed",
                                                 action=CaseAction(kind="block", block_reason=reason),
                                                 errors=errs, pinned=True))
                    continue
                # 마지막 해결 요약(HINT_CHARS자)을 힌트로 넘기면 새 스크립트를 쓰는 LLM 프롬프트에 들어간다
                if case.last_solution:
                    text = " ".join(case.last_solution.split())[:HINT_CHARS]
                    out.hints.append(f"case {cid} ({case.name or case.l2}; seen in {len(case.projects)} project(s)): "
                                     f"last solution — {text}")
                # 성공이 많고 최신인 저장 스크립트를 고르고, 없으면 힌트만 넘겨 LLM이 새로 쓰게 한다
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
        """실행이 끝난 오류 목록을 :meth:`CaseStore.record_run`으로 KB에 기록한다.
        KB 문제로 보고서 작성이 멈추면 안 되므로 실패하면 경고를 남기고 0을 반환한다.

        The run's finished error list into the KB (:meth:`CaseStore.record_run`). Never raises (0 + a warning)."""
        try:
            n = self.cases.record_run(entries)
        except Exception as e:  # noqa: BLE001
            log.warning("kb.record_run failed: %s: %s", type(e).__name__, e)
            return 0
        # 기록한 뒤에도 이전 규칙으로 서명된 줄이 남아 있으면 지금 재서명하고, 실패해도 n은 그대로 반환한다
        try:
            if getattr(self.cases, "older_lines", 0):
                log.info("kb: %d line(s) of %s signed by older rules — re-signing", self.cases.older_lines, KB_ERRORS)
                self._resign(dry_run=False, recount=True, retry=False)
        except Exception as e:  # noqa: BLE001 - the run's record stands
            log.warning("kb re-sign after record_run failed: %s: %s", type(e).__name__, e)
        return n

    def resign(self, dry_run: bool = False) -> ResignReport:
        """KB를 :meth:`CaseStore.resign`으로 재서명한다.
        ``dry_run``이면 옮길 내용만 보고하고 아무것도 쓰지 않는다.
        예외를 던지지 않고, 실패하면 WARNING 한 줄과 ``error``가 든 빈 보고를 반환한다.
        보고는 ``last_resign``에도 남는다.

        Re-sign (:meth:`CaseStore.resign`); ``dry_run`` reports what would move and writes nothing. Never raises: a
        failure is one WARNING and an empty report with ``error``. The report is kept in ``last_resign``. An explicit
        call retries the cases the last re-sign skipped; the automatic triggers do not."""
        return self._resign(dry_run=dry_run, recount=True, retry=True)

    def _resign(self, dry_run: bool, recount: bool, retry: bool) -> ResignReport:
        try:
            rep = self.cases.resign(dry_run=dry_run, recount=recount, retry_skipped=retry)
        except Exception as e:  # noqa: BLE001 - a re-sign never fails a run
            log.warning("kb re-sign failed: %s: %s — the KB stays as it is", type(e).__name__, e)
            rep = ResignReport(version=current_version(), dry_run=dry_run, error=f"{type(e).__name__}: {e}")
        self.last_resign = rep
        return rep

    def _first_match_check(self) -> None:
        """이 프로세스에서 KB 디렉토리를 처음 조회할 때 재서명이 필요한지 확인한다.
        ``kb.json``이 현재 버전이 아니고 기록이 있으면 한 번 재서명한다.
        예외를 던지지 않는다.

        The first ``match`` of a process for this KB directory: re-sign once when ``kb.json`` is missing / older
        and the KB has records; later calls do not look again. Never raises."""
        key = os.path.abspath(str(self.root))
        if key in _FIRST_MATCH_CHECKED:
            return
        _FIRST_MATCH_CHECKED.add(key)
        try:
            if self.cases.marker_sig() >= current_version() or not self.cases.has_records():
                return
            log.info("kb: %s was not re-signed to the current signature rules (v%d) — re-signing once", self.root,
                     current_version())
            self._resign(dry_run=False, recount=True, retry=False)
        except Exception as e:  # noqa: BLE001 - the lookup goes on with the KB as it is
            log.warning("kb re-sign on first lookup failed: %s: %s", type(e).__name__, e)

    def reindex(self) -> int:
        """``sbmig kb reindex``를 처리한다.
        ``errors.jsonl``로 모든 케이스를 다시 집계하고 ``cases.md``를 다시 쓴다.
        케이스 수를 반환하고, KB가 없으면 만들지 않고 0을 반환한다. 예외를 던지지 않는다.

        v2.1z-ai: re-signs first (:meth:`resign`, its report in ``last_resign``).

        Recount every case from ``errors.jsonl`` and rewrite ``cases.md`` (v2.1e: also every stored script's run
        history, augment lineage and last failure, and the as-is / to-be hunks of entries stored without them —
        :meth:`CaseStore.recount`). Returns the number of cases (0 for a KB that does not exist yet — nothing is
        created). Never raises."""
        if not self.root.exists():
            return 0
        # v2.1z-ai: records of older rules to their current ids (the skipped cases retried); a case it still cannot
        # read is not recounted (no fresh case.json over a damaged one)
        rep = self._resign(dry_run=False, recount=False, retry=True)
        try:
            return self.cases.recount(skip=rep.unreadable)
        except Exception as e:  # noqa: BLE001
            log.warning("kb reindex failed: %s: %s", type(e).__name__, e)
            return 0

    def stats(self) -> dict[str, Any]:
        """``sbmig kb stats``에 보여 줄 통계를 반환한다."""
        return self.cases.stats()

    def validate(self, limit: int = 20) -> list[str]:
        """``sbmig kb validate``를 처리한다.
        깨진 case.json, 스크립트 파일 같은 문제를 최대 ``limit``개까지 문장으로 반환한다."""
        return self.cases.validate(limit)

    # ------------------------------------------------------------------ transfer
    def export_zip(self, path: str | Path) -> Path:
        """``sbmig kb export``를 처리하며, 잠금을 잡은 채 KB를 zip 파일 하나로 내보낸다.
        임시 파일에 쓴 뒤 rename하므로 반쯤 쓴 zip이 남지 않는다.

        Zip ``cases/``, ``errors.jsonl`` and ``cases.md`` (paths relative to the KB root) under the KB lock."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # 잠금을 잡으면 KB 디렉토리가 생기므로 그 전에 원래 있었는지 기억하고, 없던 KB는 빈 zip으로 내보낸다
        existed = self.root.exists()                      # (taking the lock creates the directory)
        with self.lock():
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                if existed:
                    for p in sorted(self.root.rglob("*")):
                        keep_alive(self.root)              # a big KB over NFS: keep the lock fresh (throttled)
                        if p.is_dir() or p.name.startswith(".") or p.suffix == ".tmp":
                            continue
                        rel = p.relative_to(self.root)
                        if rel.parts[0] == CASES_DIR or rel.as_posix() in EXPORT_FILES:
                            zf.write(p, rel.as_posix())
        os.replace(tmp, path)
        return path

    def import_zip(self, path: str | Path, merge: bool = True) -> dict[str, Any]:
        """내보낸 KB zip을 가져오고 처리 건수를 담은 dict를 반환한다.
        - ``merge``(기본): 새 오류 줄, 모르는 케이스, 아는 케이스의 새 스크립트만 더한다.
          같은 zip을 두 번 넣어도 변화가 없다
        - ``merge=False``: 통째로 교체하고 기존 KB는 ``<kb>.bak-<시각>``으로 남긴다

        Import an exported KB. ``merge`` (default): new error lines, unknown cases, scripts of known cases by their
        code (:meth:`CaseStore.merge_from`; importing the same zip twice changes nothing). ``merge=False``: the KB is
        replaced — built in a sibling temp dir and swapped in by rename, the old one kept as
        ``<kb>.bak-<timestamp>``. Returns counters."""
        if not merge:
            return self._import_replace(path)
        with self.lock():
            return self._import_into(self, path)

    @staticmethod
    def _import_into(kb: "FileKnowledgeBase", path: str | Path) -> dict[str, Any]:
        """zip을 임시 디렉토리에 풀어 CaseStore를 만들고 ``kb``에 병합한다."""
        with tempfile.TemporaryDirectory(prefix="sbmig-kb-import-") as tmpd:
            tmp = Path(tmpd).resolve()
            with zipfile.ZipFile(path) as zf:
                # zip slip 공격을 막기 위해 항목 경로가 임시 디렉토리 밖을 가리키면 거부한다
                for m in zf.infolist():
                    target = (tmp / m.filename).resolve()
                    if not str(target).startswith(str(tmp) + os.sep):
                        raise ValueError(f"zip slip blocked: {m.filename}")
                zf.extractall(tmp)
            kb.root.mkdir(parents=True, exist_ok=True)
            return kb.cases.merge_from(CaseStore(tmp, kb.cfg, lock_timeout=kb.lock_timeout,
                                                 script_runs=kb.script_runs))

    def _import_replace(self, path: str | Path) -> dict[str, Any]:
        """KB를 zip 내용으로 통째로 교체한다.
        새 KB를 옆 임시 디렉토리에 만들고 기존 KB를 ``<kb>.bak-<시각>``으로 옮긴 뒤 rename으로 바꿔 넣는다.
        rename이 실패하면 기존 KB를 되돌리고, 어떤 예외가 나도 임시 디렉토리는 지운다."""
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        parent = self.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        build_dir = Path(tempfile.mkdtemp(prefix=f".{self.root.name}.import-{stamp}-", dir=str(parent)))
        with self.lock():
            try:
                counters = self._import_into(FileKnowledgeBase(self.cfg, kb_dir=build_dir), path)
                old = self.root
                # 레포가 추적하는 .gitkeep이 새 KB에도 있어야 git status가 깨끗하게 유지된다
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
                except OSError as e:
                    if backup is not None:                # put the previous KB back where it was
                        self._restore_backup(old, backup, stamp, e)
                    raise
                if backup is not None:
                    # 우리 잠금 파일이 기존 KB와 함께 .bak로 옮겨졌으므로 거기서 지운다
                    (backup / LOCK_NAME).unlink(missing_ok=True)    # our lock moved with the old KB
                    counters["backup"] = str(backup)
                    log.info("kb import --replace: the previous KB is kept in %s", backup)
            except BaseException:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise
            self._cases = None
        return counters

    @staticmethod
    def _restore_backup(old: Path, backup: Path, stamp: str, cause: OSError) -> None:
        """교체 가져오기의 두 번째 rename이 실패했을 때 기존 KB를 원래 위치로 되돌린다.
        그 사이 다른 파드가 만든 빈 ``<kb>``는 ``<kb>.intruder-<시각>``으로 치운다.
        결과 위치는 ERROR 로그 한 줄로 남긴다.

        The second rename of ``import --replace`` failed: a pod may have re-created an empty ``<kb>`` meanwhile (its
        lock / an atomic write makes the parents) — move that folder to ``<kb>.intruder-<stamp>`` and put the previous
        KB back; one ERROR line says exactly where everything is (and what to move by hand if even that fails)."""
        intruder: Optional[Path] = None
        if old.exists():
            intruder = old.parent / f"{old.name}.intruder-{stamp}"
            n = 1
            while intruder.exists():
                n += 1
                intruder = old.parent / f"{old.name}.intruder-{stamp}-{n}"
            try:
                os.replace(old, intruder)
            except OSError as e:
                log.error("kb import --replace failed (%s) and %s, created there meanwhile, could not be moved aside "
                          "(%s) — the previous KB is in %s: stop the pods, then move it back to %s by hand", cause, old,
                          e, backup, old)
                return
        try:
            os.replace(backup, old)
        except OSError as e:
            log.error("kb import --replace failed (%s) and the previous KB could not be put back (%s) — it is in %s: "
                      "stop the pods, then move it back to %s by hand%s", cause, e, backup, old,
                      f" (what was created there meanwhile is in {intruder})" if intruder else "")
            return
        log.error("kb import --replace failed (%s) — the previous KB is back in %s%s", cause, old,
                  f"; {intruder} holds what a running pod created there meanwhile (look there for errors.jsonl lines "
                  "or cases before deleting it)" if intruder else "")
