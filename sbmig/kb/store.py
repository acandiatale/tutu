"""파일 KB(지식베이스)의 바깥 창구 — 오케스트레이터의 ``self.kb``, 플릿, 웹 서버가 들고 있는 객체.
데이터는 전부 :class:`sbmig.errors.cases.CaseStore` 가 관리하고, 이 클래스는 그 위에 (1) 수정 루프가
라운드마다 부르는 케이스 조회 ``match`` (저장된 스크립트가 있으면 CaseHit, 사람이 ``block`` 으로 고정한
케이스는 그대로 전달), (2) 실행이 끝난 오류 목록을 KB 에 적는 ``record_run``, (3) ``sbmig kb reindex`` 의
``reindex``, (4) KB 통째 내보내기 / 가져오기(zip), (5) 실행 중 절대 예외를 올리지 않는 래퍼를 더한다. 흐름상
위치: verify.build 실패 → 수정 루프 매 라운드 ``match`` → 실행 종료 시 ``record_run``. KB 는 파일뿐이다(DB
금지, CLAUDE.md 불변조건 3): ``kb.dir`` 아래 ``errors.jsonl``, ``cases.md``, ``cases/<case_id>/case.json`` +
``fix_v<N>.py``, ``.lock`` 만 읽고 쓴다.

The file KB facade (v2.1, docs/v2/PLAN-v2.1.md §3.4) — what the orchestrator (``self.kb``), the fleet and the web
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
    """KB 창구 하나 = KB 디렉토리 하나. ``cases`` 가 실제 저장소(CaseStore)이고 나머지는 그 위의 얇은 래퍼다.

    ``kb.cases`` (the CaseStore), ``match``, ``record_run``, ``reindex``, ``stats``, ``validate``,
    ``export_zip`` / ``import_zip``, ``lock``."""

    def __init__(self, cfg: Config, kb_dir: Optional[Path] = None):
        self.cfg = cfg
        self.root = Path(kb_dir) if kb_dir else cfg.kb_dir
        self._cases: Optional[CaseStore] = None

    @property
    def lock_timeout(self) -> float:
        """``kb.lock_timeout_seconds``: 잠금을 기다리는 최대 초(기본
        300). 플릿이 앞 프로젝트를 끝내는 동안 기다린다."""
        return float(getattr(self.cfg.kb, "lock_timeout_seconds", 300) or 0)

    def lock(self):
        """프로세스 간 KB 잠금(``<kb>/.lock``, 재진입 가능). 플릿·웹 서버가 같은 KB 를 동시에 쓸 때를 위한 것.

        The cross-process KB lock (``<kb>/.lock``, re-entrant)."""
        return kb_lock(self.root, self.lock_timeout)

    @property
    def script_runs(self) -> int:
        """``errors.script_runs``: 저장된 스크립트 버전마다 남기는
        실행 기록(ScriptRun) 개수 상한(v2.1e, 0 = 무제한).

        ``errors.script_runs``: ScriptRun records kept per stored script version (v2.1e; 0 = no cap)."""
        try:
            return max(0, int(getattr(getattr(self.cfg, "errors", None), "script_runs", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def cases(self) -> CaseStore:
        """실제 저장소(CaseStore)를 지연 생성한다. KB 경로가 바뀌면(``import --replace`` 뒤) 새로 만든다."""
        if self._cases is None or self._cases.root != self.root:
            self._cases = CaseStore(self.root, self.cfg, lock_timeout=self.lock_timeout,
                                    script_runs=self.script_runs)
        return self._cases

    # ------------------------------------------------------------------ the fix loop
    def match(self, cluster: ErrorCluster, ws: Any = None, profile: Any = None) -> KBMatch:
        """수정 루프가 라운드마다 부르는 조회: 이 클러스터의 케이스에 대해 KB 가 가진 것.
        케이스마다 쓸 만한 저장 스크립트가 있으면 CaseHit(action ``script``, :meth:`CaseStore.best` 가 고른
        버전; status active = 전에 성공한 적 있음, candidate = 아직 검증 안 됨), 사람이 ``block`` 으로
        고정했으면 그 핀을 그대로(트리아지가 따른다), 그리고 힌트 줄(케이스의 마지막 해결 내용). 모르는
        케이스면 빈 결과. 절대 예외를 내지 않는다 — KB 조회가 깨져도 수정 루프는 계속 돌아야 한다.

        What the KB has for the cluster's case(s): a CaseHit per case with a usable stored script (action
        ``script`` with the version :meth:`CaseStore.best` picks; status active = proven, candidate = unverified) or
        a person's ``block`` pin (``pinned``; the triage obeys it), and hint lines (the case's last solution). Empty
        for an unknown case. Never raises."""
        out = KBMatch()
        try:
            if not self.cases.cases_dir.is_dir():
                return out
            # 클러스터 자체의 case_id 와 각 오류의 case_id 를, 순서는 지키고 중복은 빼서 모은다
            ids = list(dict.fromkeys([c for c in [cluster.case_id] if c] + [case_id_of(e) for e in cluster.errors]))
            for cid in ids:
                case = self.cases.find(cid)
                if case is None:
                    continue
                errs = [e for e in cluster.errors if case_id_of(e) == cid] or \
                    (list(cluster.errors) if cid == cluster.case_id else [])
                # 사람이 case.json 에 "block: <이유>" 로 고정한 케이스:
                # 스크립트를 찾지 않고 그대로 차단 결정을 넘긴다
                pin = self.cases.pin_of(cid)
                if pin:
                    reason = pin.split(":", 1)[1].strip() if ":" in pin else ""
                    out.case_hits.append(CaseHit(case_id=cid, name=case.name, status="observed",
                                                 action=CaseAction(kind="block", block_reason=reason),
                                                 errors=errs, pinned=True))
                    continue
                # 힌트 = 마지막 해결 요약(공백 정리, HINT_CHARS
                # 자): 새 스크립트를 쓰는 LLM 프롬프트에 들어간다
                if case.last_solution:
                    text = " ".join(case.last_solution.split())[:HINT_CHARS]
                    out.hints.append(f"case {cid} ({case.name or case.l2}; seen in {len(case.projects)} project(s)): "
                                     f"last solution — {text}")
                # 저장 스크립트 중 가장 좋은 버전(성공 많고 최신) — 없으면 힌트만 남고 LLM 이 새로 쓴다
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
        """실행이 끝난 오류 목록을 KB 에 기록(:meth:`CaseStore.record_run`).
        실패해도 예외 대신 0 + 경고 — KB 가 깨졌다고 보고서 작성까지 멈추면 안 된다.

        The run's finished error list into the KB (:meth:`CaseStore.record_run`). Never raises (0 + a warning)."""
        try:
            return self.cases.record_run(entries)
        except Exception as e:  # noqa: BLE001
            log.warning("kb.record_run failed: %s: %s", type(e).__name__, e)
            return 0

    def reindex(self) -> int:
        """``sbmig kb reindex``: ``errors.jsonl`` 로부터 모든 케이스를 다시 세고 ``cases.md`` 를 다시 쓴다
        (v2.1e: 저장 스크립트의 실행 기록·보강 계보·마지막 실패,
        hunk 없이 저장된 옛 항목의 as-is / to-be 코드까지 —
        :meth:`CaseStore.recount`). 케이스 수를 돌려주고, 없는 KB 는 만들지 않고 0. 절대 예외를 내지 않는다.

        Recount every case from ``errors.jsonl`` and rewrite ``cases.md`` (v2.1e: also every stored script's run
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
        """``sbmig kb stats`` 가 보여 주는 숫자들(케이스·오류·스크립트 수 등)."""
        return self.cases.stats()

    def validate(self, limit: int = 20) -> list[str]:
        """``sbmig kb validate``: 깨진 case.json / 스크립트 파일
        등 문제를 최대 ``limit`` 개까지 문장으로 돌려준다."""
        return self.cases.validate(limit)

    # ------------------------------------------------------------------ transfer
    def export_zip(self, path: str | Path) -> Path:
        """KB 를 zip 하나로(``cases/``, ``errors.jsonl``, ``cases.md``;
        KB 루트 기준 상대 경로), 잠금을 잡은 채 만든다. 다른 PC 로 KB 를 옮길 때 쓴다(``sbmig kb export``).
        임시 파일에 쓴 뒤 rename 하므로 반쪽짜리 zip 이 남지 않는다.

        Zip ``cases/``, ``errors.jsonl`` and ``cases.md`` (paths relative to the KB root) under the KB lock."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # 잠금을 잡으면 KB 디렉토리가 생기므로, 그 전에 "원래 있었는지" 를 기억해 둔다(없던 KB 는 빈 zip)
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
        """내보낸 KB(zip)를 가져온다. ``merge``(기본): 새 오류 줄,
        모르는 케이스, 아는 케이스의 새 스크립트(코드 내용으로 비교)만 더한다 — 같은 zip 을 두 번 넣어도 변화
        없음. ``merge=False``: 통째 교체 — 옆 임시 디렉토리에 만든 뒤 rename 으로 바꿔 넣고 예전 KB 는
        ``<kb>.bak-<시각>`` 으로 남긴다. 카운터 dict 를 돌려준다.

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
        """zip 을 임시 디렉토리에 풀어 거기서 CaseStore 를 만들고 ``kb`` 에
        합친다(:meth:`CaseStore.merge_from`)."""
        with tempfile.TemporaryDirectory(prefix="sbmig-kb-import-") as tmpd:
            tmp = Path(tmpd).resolve()
            with zipfile.ZipFile(path) as zf:
                # zip slip 방어: 항목 경로가 임시 디렉토리 밖(../ 등)을 가리키면 풀지 않고 거부
                for m in zf.infolist():
                    target = (tmp / m.filename).resolve()
                    if not str(target).startswith(str(tmp) + os.sep):
                        raise ValueError(f"zip slip blocked: {m.filename}")
                zf.extractall(tmp)
            kb.root.mkdir(parents=True, exist_ok=True)
            return kb.cases.merge_from(CaseStore(tmp, kb.cfg, lock_timeout=kb.lock_timeout,
                                                 script_runs=kb.script_runs))

    def _import_replace(self, path: str | Path) -> dict[str, Any]:
        """교체 가져오기: 새 KB 를 옆의 임시 디렉토리(``.<kb>.import-<시각>-*``)에
        만들고, 레포가 추적하는 ``.gitkeep`` 을 복사한 뒤, 예전 KB 를 ``<kb>.bak-<시각>`` 으로 옮기고 새 KB 를
        제자리로 rename 한다. rename 이 실패하면 예전 KB 를 되돌리고, 어떤 예외든 임시
        디렉토리는 지운다 — 절반만 바뀐 KB 는 남지 않는다."""
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        parent = self.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        build_dir = Path(tempfile.mkdtemp(prefix=f".{self.root.name}.import-{stamp}-", dir=str(parent)))
        with self.lock():
            try:
                counters = self._import_into(FileKnowledgeBase(self.cfg, kb_dir=build_dir), path)
                old = self.root
                # 레포가 추적하는 빈 디렉토리 표식(.gitkeep)은 새 KB 에도 있어야 git status 가 깨끗하다
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
                    # 우리가 잡은 잠금 파일이 예전 KB 와 함께 .bak 로 옮겨졌으므로 거기서 지운다
                    (backup / LOCK_NAME).unlink(missing_ok=True)    # our lock moved with the old KB
                    counters["backup"] = str(backup)
                    log.info("kb import --replace: the previous KB is kept in %s", backup)
            except BaseException:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise
            self._cases = None
        return counters
