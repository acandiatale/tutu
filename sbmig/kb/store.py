"""KB를 읽고 쓰는 모듈. KB는 빌드 오류와 그 오류를 고친 수정 스크립트를 파일로 쌓아 두는 저장소다.
오케스트레이터, 플릿, 웹 서버가 모두 이 모듈로 KB를 쓴다.

The facade over :class:`sbmig.errors.cases.CaseStore` (which owns the data): ``match`` (the stored fix script for a
build error), ``record_run`` (a finished run's errors into the KB), ``reindex`` (re-sign, then recount),
``export_zip`` / ``import_zip``. Nothing here raises during a run. Files only, never a database.
Re-signing moves what was recorded under older signature rules to the current case ids; a failed re-sign leaves a
WARNING and the run goes on.

Layout under ``cfg.kb_dir`` (``kb.dir``, default ``kb-v2/``): ``errors.jsonl``, ``cases.md``,
``cases/<case_id>/case.json`` + ``fix_v<N>.py``, ``.lock``. Nothing else is read or written (older KB files
such as catalog, views, audit, quarantine, reviews, fix_attempts, scripts/*/meta.json, index.json are never
looked at).

Automatic re-sign: on the first ``match`` of a process per KB directory (``kb.json`` missing / older while the
KB has records) and after a ``record_run`` that leaves a line signed by older rules. ``match`` leaves out a
stored case whose L2 is not the L2 of the cluster's errors (the exact-key guard, :meth:`CaseStore.same_case`).
Contract: ``docs/INTERFACES.md "v2.1z-ai"``.
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
from sbmig.errors.cases import CASES_DIR, CASES_MD, KB_ERRORS, SCOPE_FULL, SCOPE_REDUCED, CaseStore, scope_of
from sbmig.errors.common import atomic_write, case_id_of
from sbmig.errors.resign import ResignReport, current_version

from .lock import LOCK_NAME, kb_lock, keep_alive

log = logging.getLogger("sbmig.kb")

MAX_MATCH_HINTS = 10
HINT_CHARS = 400
EXPORT_FILES = (KB_ERRORS, CASES_MD)

__all__ = ["FileKnowledgeBase", "atomic_write"]

# 힌트 줄에 쓸 범위 이름
SOURCE_LABELS = {SCOPE_FULL: "full-migration", SCOPE_REDUCED: "jdk-jakarta (reduced-scope)"}


def run_scope(profile: Any) -> str:
    """이번 실행의 이관 범위를 반환한다.
    범위를 줄인 실행(``profile.reduced_scope``)이면 jdk-jakarta, 아니면 full이다."""
    try:
        return SCOPE_REDUCED if profile is not None and getattr(profile, "reduced_scope", False) is True else SCOPE_FULL
    except Exception:  # noqa: BLE001 - 다른 형식의 프로필 객체
        return SCOPE_FULL


# 재서명이 필요한지 이미 확인한 KB 경로. 같은 KB는 프로세스당 한 번만 확인한다
_FIRST_MATCH_CHECKED: set[str] = set()


class FileKnowledgeBase:
    """KB 디렉토리 하나를 읽고 쓰는 클래스. 실제 데이터는 ``cases``(CaseStore)가 관리한다."""

    def __init__(self, cfg: Config, kb_dir: Optional[Path] = None):
        self.cfg = cfg
        self.root = Path(kb_dir) if kb_dir else cfg.kb_dir
        self._cases: Optional[CaseStore] = None
        self.last_resign: Optional[ResignReport] = None   # 마지막 재서명 결과. `sbmig kb reindex`가 출력한다

    @property
    def lock_timeout(self) -> float:
        """KB 잠금을 최대 몇 초 기다릴지 반환한다.
        설정 ``kb.lock_timeout_seconds``를 따르고 기본값은 300이다."""
        return float(getattr(self.cfg.kb, "lock_timeout_seconds", 300) or 0)

    def lock(self):
        """여러 프로세스가 KB를 동시에 고치지 않도록 잠금을 건다.
        같은 프로세스는 잠금을 다시 잡을 수 있다."""
        return kb_lock(self.root, self.lock_timeout)

    @property
    def script_runs(self) -> int:
        """스크립트 버전 하나에 실행 기록을 몇 개까지 남길지 반환한다.
        설정 ``errors.script_runs``를 따른다. 0이면 제한이 없고, 값이 잘못됐으면 30이다."""
        try:
            return max(0, int(getattr(getattr(self.cfg, "errors", None), "script_runs", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def cases(self) -> CaseStore:
        """CaseStore를 반환한다. 처음 쓸 때 만들고, KB 경로가 바뀌면 다시 만든다."""
        if self._cases is None or self._cases.root != self.root:
            self._cases = CaseStore(self.root, self.cfg, lock_timeout=self.lock_timeout,
                                    script_runs=self.script_runs)
        return self._cases

    # ------------------------------------------------------------------ 수정 루프
    def match(self, cluster: ErrorCluster, ws: Any = None, profile: Any = None) -> KBMatch:
        """이 빌드 오류를 고친 적이 있는지 KB에서 찾는다. 수정 루프가 라운드마다 호출한다.
        저장된 스크립트가 있으면 그 스크립트와 지난번 해결 내용을 반환한다. 처음 보는 오류면 빈 결과다.
        사람이 차단해 둔 케이스는 스크립트 대신 차단 결정을 반환한다.
        조회에 실패해도 예외를 던지지 않는다. 수정 루프는 계속돼야 한다.

        CaseHit status: active = proven, candidate = unverified; the version is
        ``CaseStore.best(case_id, scope)`` with the scope of :func:`run_scope`. A last solution written by a
        run of the other scope is kept but labelled (``last solution [from a full-migration run] — …``): most
        javax -> jakarta solutions hold in both scopes, and the label tells the LLM what it may not take over.
        Contract: ``docs/INTERFACES.md "v2.1z-ap"``."""
        out = KBMatch()
        scope = run_scope(profile)
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
                # 케이스 ID가 같아도 오류 서명(L2)이 다르면 다른 케이스다. 건너뛴다
                asked = next((e.signature_generic for e in errs if e.signature_generic), "")
                if asked and not self.cases.same_case(cid, asked, case):
                    continue
                # 사람이 case.json에서 "block: <이유>"로 막아 둔 케이스다. 스크립트를 찾지 않고 차단 결정만 넘긴다
                pin = self.cases.pin_of(cid)
                if pin:
                    reason = pin.split(":", 1)[1].strip() if ":" in pin else ""
                    out.case_hits.append(CaseHit(case_id=cid, name=case.name, status="observed",
                                                 action=CaseAction(kind="block", block_reason=reason),
                                                 errors=errs, pinned=True))
                    continue
                # 지난번 해결 내용을 힌트로 넘긴다. LLM이 새 스크립트를 쓸 때 프롬프트에 들어간다
                if case.last_solution:
                    text = " ".join(case.last_solution.split())[:HINT_CHARS]
                    # 이관 범위가 다른 실행에서 나온 해결 내용이면 어느 범위였는지 표시한다
                    came = scope_of(self.cases.extra(cid).get("solution_scope"))
                    label = f" [from a {SOURCE_LABELS.get(came, came)} run]" if came != scope else ""
                    out.hints.append(f"case {cid} ({case.name or case.l2}; seen in {len(case.projects)} project(s)): "
                                     f"last solution{label} — {text}")
                # 성공 횟수가 많고 최신인 스크립트를 고른다. 없으면 힌트만 넘겨서 LLM이 새로 쓰게 한다
                best = self.cases.best(cid, scope)
                if best is None:
                    continue
                s = best[0]
                out.case_hits.append(CaseHit(case_id=cid, name=case.name,
                                             status="active" if s.status == "proven" else "candidate",
                                             action=CaseAction(kind="script", script_version=s.version),
                                             errors=errs, hints=out.hints[-1:] if case.last_solution else []))
            out.hints = out.hints[:MAX_MATCH_HINTS]
        except Exception as e:  # noqa: BLE001 - KB 조회 실패로 수정 루프를 멈추지 않는다
            log.warning("kb.match failed for %s: %s: %s", getattr(cluster, "id", "?"), type(e).__name__, e)
        return out

    def record_run(self, entries: Iterable[ErrorEntry]) -> int:
        """실행이 끝난 뒤 오류 목록을 KB에 기록하고, 추가한 줄 수를 반환한다.
        실패하면 경고만 남기고 0을 반환한다. KB 문제로 보고서 작성이 멈추면 안 된다."""
        try:
            n = self.cases.record_run(entries)
        except Exception as e:  # noqa: BLE001
            log.warning("kb.record_run failed: %s: %s", type(e).__name__, e)
            return 0
        # 이전 규칙으로 서명된 줄이 남아 있으면 바로 재서명한다. 재서명이 실패해도 n은 그대로 반환한다
        try:
            if getattr(self.cases, "older_lines", 0):
                log.info("kb: %d line(s) of %s signed by older rules — re-signing", self.cases.older_lines, KB_ERRORS)
                self._resign(dry_run=False, recount=True, retry=False)
        except Exception as e:  # noqa: BLE001 - 실행 기록은 이미 남았다
            log.warning("kb re-sign after record_run failed: %s: %s", type(e).__name__, e)
        return n

    def resign(self, dry_run: bool = False) -> ResignReport:
        """KB를 재서명한다. 이전 서명 규칙으로 기록된 항목을 지금 규칙의 케이스로 옮기는 작업이다.
        ``dry_run``이면 무엇을 옮길지만 알려 주고 아무것도 쓰지 않는다.
        실패해도 예외를 던지지 않는다. 경고를 남기고 ``error``가 채워진 보고를 반환한다.

        The report is also kept in ``last_resign``. An explicit call retries the cases the last re-sign skipped; the automatic triggers do not."""
        return self._resign(dry_run=dry_run, recount=True, retry=True)

    def _resign(self, dry_run: bool, recount: bool, retry: bool) -> ResignReport:
        try:
            rep = self.cases.resign(dry_run=dry_run, recount=recount, retry_skipped=retry)
        except Exception as e:  # noqa: BLE001 - 재서명 실패로 실행을 실패시키지 않는다
            log.warning("kb re-sign failed: %s: %s — the KB stays as it is", type(e).__name__, e)
            rep = ResignReport(version=current_version(), dry_run=dry_run, error=f"{type(e).__name__}: {e}")
        self.last_resign = rep
        return rep

    def _first_match_check(self) -> None:
        """이 KB를 처음 조회할 때 재서명이 필요한지 확인하고, 필요하면 한 번 재서명한다.
        프로세스당 한 번만 확인한다. 예외를 던지지 않는다.

        Needed = ``kb.json`` is missing or older than the current signature version while the KB has records."""
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
        except Exception as e:  # noqa: BLE001 - KB를 그대로 두고 조회를 계속한다
            log.warning("kb re-sign on first lookup failed: %s: %s", type(e).__name__, e)

    def reindex(self) -> int:
        """KB 통계를 처음부터 다시 계산한다. ``sbmig kb reindex``가 호출한다.
        재서명을 먼저 하고, ``errors.jsonl``을 읽어 케이스를 다시 집계한 뒤 ``cases.md``를 새로 쓴다.
        케이스 수를 반환한다. KB가 없으면 0이다. 예외를 던지지 않는다.

        :meth:`CaseStore.recount` also rebuilds every stored script's run history, augment lineage and last
        failure, and the as-is / to-be hunks of entries stored without them."""
        if not self.root.exists():
            return 0
        # 재서명 때 읽지 못한 케이스는 집계에서 뺀다. 깨진 case.json을 덮어쓰지 않기 위해서다
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
        """KB에 깨진 파일이 있는지 검사한다. ``sbmig kb validate``가 호출한다.
        찾은 문제를 문장으로 ``limit``개까지 반환한다."""
        return self.cases.validate(limit)

    # ------------------------------------------------------------------ 내보내기와 가져오기
    def export_zip(self, path: str | Path) -> Path:
        """KB 전체를 zip 파일 하나로 내보낸다. ``sbmig kb export``가 호출한다.
        임시 파일에 먼저 쓰고 이름을 바꾸므로, 중간에 실패해도 반쯤 쓴 zip이 남지 않는다."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # 잠금을 잡으면 KB 디렉토리가 생긴다. 그 전에 있었는지 기억해 두고, 없던 KB는 빈 zip으로 내보낸다
        existed = self.root.exists()
        with self.lock():
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                if existed:
                    for p in sorted(self.root.rglob("*")):
                        keep_alive(self.root)              # NFS의 큰 KB는 오래 걸리므로 잠금을 갱신한다
                        if p.is_dir() or p.name.startswith(".") or p.suffix == ".tmp":
                            continue
                        rel = p.relative_to(self.root)
                        if rel.parts[0] == CASES_DIR or rel.as_posix() in EXPORT_FILES:
                            zf.write(p, rel.as_posix())
        os.replace(tmp, path)
        return path

    def import_zip(self, path: str | Path, merge: bool = True) -> dict[str, Any]:
        """내보낸 KB zip을 가져온다. 몇 건을 처리했는지 dict로 반환한다.
        - ``merge=True``(기본): 지금 KB에 없는 것만 더한다. 같은 zip을 두 번 넣어도 달라지지 않는다
        - ``merge=False``: KB를 통째로 바꾼다. 기존 KB는 ``<kb>.bak-<시각>``으로 남긴다

        Merge = :meth:`CaseStore.merge_from` (scripts of known cases matched by their code). Replace builds
        the KB in a sibling temp dir and swaps it in by rename."""
        if not merge:
            return self._import_replace(path)
        with self.lock():
            return self._import_into(self, path)

    @staticmethod
    def _import_into(kb: "FileKnowledgeBase", path: str | Path) -> dict[str, Any]:
        """zip을 임시 디렉토리에 풀고 그 내용을 ``kb``에 병합한다."""
        with tempfile.TemporaryDirectory(prefix="sbmig-kb-import-") as tmpd:
            tmp = Path(tmpd).resolve()
            with zipfile.ZipFile(path) as zf:
                # 경로가 임시 디렉토리 밖을 가리키는 항목은 거부한다(zip slip 공격 방지)
                for m in zf.infolist():
                    target = (tmp / m.filename).resolve()
                    if not str(target).startswith(str(tmp) + os.sep):
                        raise ValueError(f"zip slip blocked: {m.filename}")
                zf.extractall(tmp)
            kb.root.mkdir(parents=True, exist_ok=True)
            return kb.cases.merge_from(CaseStore(tmp, kb.cfg, lock_timeout=kb.lock_timeout,
                                                 script_runs=kb.script_runs))

    def _import_replace(self, path: str | Path) -> dict[str, Any]:
        """KB를 zip 내용으로 통째로 바꾼다.
        새 KB를 옆 임시 디렉토리에 만든다. 기존 KB를 ``<kb>.bak-<시각>``으로 옮기고 새 KB를 그 위치에 넣는다.
        넣다가 실패하면 기존 KB를 되돌린다. 임시 디렉토리는 어떤 경우에도 지운다."""
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        parent = self.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        build_dir = Path(tempfile.mkdtemp(prefix=f".{self.root.name}.import-{stamp}-", dir=str(parent)))
        with self.lock():
            try:
                counters = self._import_into(FileKnowledgeBase(self.cfg, kb_dir=build_dir), path)
                old = self.root
                # 기존 KB의 .gitkeep을 새 KB에도 넣는다. 없으면 git status에 삭제된 파일로 나온다
                for keep in (old.rglob(".gitkeep") if old.exists() else []):
                    dst = build_dir / keep.relative_to(old)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copyfile(keep, dst)
                for stray in build_dir.glob(LOCK_NAME + "*"):                  # 임시 KB가 만든 잠금 파일은 지운다
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
                    if backup is not None:                # 기존 KB를 원래 위치로 되돌린다
                        self._restore_backup(old, backup, stamp, e)
                    raise
                if backup is not None:
                    # 잠금 파일이 기존 KB와 함께 .bak으로 옮겨졌다. 거기서 지운다
                    (backup / LOCK_NAME).unlink(missing_ok=True)
                    counters["backup"] = str(backup)
                    log.info("kb import --replace: the previous KB is kept in %s", backup)
            except BaseException:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise
            self._cases = None
        return counters

    @staticmethod
    def _restore_backup(old: Path, backup: Path, stamp: str, cause: OSError) -> None:
        """KB 교체가 실패했을 때 기존 KB를 원래 위치로 되돌린다.
        그 사이 다른 파드가 ``<kb>``를 다시 만들었으면 ``<kb>.intruder-<시각>``으로 옮겨 둔다.
        결과는 ERROR 로그로 남긴다. 되돌리지 못했으면 기존 KB가 어디 있는지도 함께 남긴다.

        A pod re-creates ``<kb>`` when its lock or an atomic write makes the parent directories."""
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
