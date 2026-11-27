"""취약점 조치 흐름을 LangGraph로 실행하는 모듈. 이관 그래프(``sbmig/graph/engine.py``)와는 별개다.

Nodes in order: baseline_build -> baseline_scan -> inventory -> plan -> apply -> build -> freeze_delivery ->
rescan -> evaluate -> report. evaluate goes back to inventory while findings remain and rounds are left. A
resume starts again at `state.next_node`. What the fix loop changed reaches the candidate only through
`_overlay_fix`.

Rules the flow keeps:
- The state is saved at every node boundary.
- A failed baseline build never reaches scan / plan. Only a `state.baseline_tolerated` run goes on; it
  commits a round only when the candidate build adds no error the baseline lacked, and it never ends
  SUCCEEDED with a change no build confirmed.
- An upload whose outcome is unknown is never sent again; a remote IVAS analysis is never recorded as
  cancelled.
- The candidate is built in a separate copy and committed to accepted/ only after it was verified.
- The rescan scans the exact bytes (sha256) of the result ZIP; SUCCEEDED only when every evaluate
  condition holds.
- Failed candidate build: `remediation.failed_candidate: include` (default) freezes the failing tree as
  the result ZIP (build_ok=False, no rescan, FAILED) and keeps an earlier round's verified ZIP as
  `delivery/result-r<k>-verified.zip`; `discard` delivers nothing of it.
- A candidate with a change outside version text, or whose resolution differs from the plan, is never
  delivered, whatever the setting.
- Every proposal of the plan is applied; nothing waits for a person.
- After every candidate build the manifest files the build removed or changed are put back to the bytes of
  accepted/ + the applied proposals + the reported fix.
- The result ZIP is written under a temporary name in delivery/ first; result.zip is replaced and recorded
  in the state only after that succeeded.
- When the intake left build output out, the baseline scan and a no-change delivery use
  original/scan-source.zip.

Run folder (`RunPaths`): `original/source.zip` (the upload, never changed), `original/manifest.json`,
`original/scan-source.zip` (only when the intake left build output out: a deterministic ZIP of accepted/ over the
manifest paths, scanned at baseline and delivered as the no-change result; its sha256 is `state.scan_source_sha256`),
`accepted/`, `baseline/`, `rounds/<n>/`, `scans/`, `delivery/` (`result.zip`, `delivery.json`, a kept
`result-r<k>-verified.zip`, and only during a swap `.result.next.zip` + `.result.next.json`), `state.json`,
`journal.jsonl`, `remediation.log`, `report.md`, `report.json`, `fix.patch`.

Independent LangGraph of the remediation feature (not the migration graph). Only small JSON values live in the graph
state; the `RemediationState`, the run paths and the `Components` live in a holder closed over by the node functions.
Entry point: `run_remediation`; helpers for the service: `new_state`, `can_resume`, `approval_removed`;
`build_graph` (node list for tests); `default_components(cfg)` wires the real implementations lazily.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import shutil
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from sbmig.core import scrub as _scrub

from . import fsops
from .inhouse import inhouse_keys
from .interfaces import Components, Journal, StopCheck
from .inventory import is_file_residual
from .ivas import files_reported, split_files
from .matching import finding_key, ga_key, version_key
from .models import (
    AWAITING_CONFIRMATION,
    FAILED,
    INCOMPLETE,
    LEGACY_APPROVAL_MESSAGE,
    LEGACY_APPROVAL_MESSAGE_KO,
    LEGACY_AWAITING_APPROVAL,
    NODES,
    QUEUED,
    R_BASELINE_BUILD_FAILED,
    R_BOUNDARY_REFUSED,
    R_CANDIDATE_BUILD_FAILED,
    R_INHOUSE_ARTIFACT,
    R_INTERNAL_ERROR,
    R_MANUAL_REVIEW,
    R_NO_CANDIDATE_VERSION,
    R_NOT_READY,
    R_RESIDUAL_FINDINGS,
    R_RESOLUTION_MISMATCH,
    R_ROUNDS_EXHAUSTED,
    R_SCAN_INCOMPLETE,
    R_STOP_REQUESTED,
    R_TEXT_ONLY_VIOLATION,
    R_UPLOAD_UNCERTAIN,
    R_ZIP_HASH_MISMATCH,
    RESUMABLE,
    RUNNING,
    STOPPED,
    SUCCEEDED,
    TERMINAL,
    DeliveryInfo,
    FixResult,
    IntakeResult,
    Inventory,
    IvasUploadUncertain,
    NotReady,
    Proposal,
    RemediationState,
    Residual,
    ResolvedModel,
    RoundPlan,
    RoundState,
    RunPaths,
    ScanResult,
    StateConflict,
    UploadReceipt,
    read_json,
    sha256_file,
    write_json_atomic,
)

# 효과가 없어 수정본에서 다시 뺀 override 제안의 decision 값이다. 적용한 뒤 빌드 결과로 정해진다
D_INEFFECTIVE = "ineffective"
log = logging.getLogger("sbmig.remediations.graph")

ROUTE = "route"
BASELINE_LABEL = "baseline"
# 수정본 복사와 적용이 끝났다는 표시 파일이다. 수정본 폴더 밖(rounds/<n>/)에 두어 결과 ZIP에 들어가지 않게 한다
CANDIDATE_READY = "candidate.ready.json"
EXPECTED_FILE = "expected.json"         # 라운드 수정본의 manifest 경로마다 기대하는 크기와 sha256(rounds/<r>/ 아래)
# 수정본이 state에 기록된 변경을 다 담고 있는지 알 수 없어서 내보내지 않았을 때의 문장
CANDIDATE_NOT_VOUCHED = ("the round {r} candidate was not delivered: the files on disk are not the complete change set "
                         "this state records")
# 빌드 뒤 수정본을 기대한 상태로 되돌리지 못했을 때의 문장
CANDIDATE_NOT_RESTORED = ("the round {r} candidate could not be put back to its expected state after the build ({n} "
                          "path(s), first: {p}: {t}); nothing of this round was delivered")
# 결과 ZIP을 쓰지 못했을 때의 문장. 앞의 결과 ZIP이 있으면 그것이 그대로 남았다고 쓴다
DELIVERY_NOT_WRITTEN = "the result ZIP of round {r} could not be written ({t}: {e}); {left}"
DELIVERY_LEFT_KEPT = "delivery/result.zip is still the result ZIP of round {k}"
DELIVERY_LEFT_NONE = "no result ZIP exists; the original is preserved as original/source.zip"
# result.zip은 바꿨지만 기록 파일을 쓰지 못했을 때의 문장. 결과 ZIP은 이미 새 라운드의 결과다
DELIVERY_RECORD_NOT_WRITTEN = ("delivery/result.zip is now the result ZIP of round {r}, but {what} could not be written "
                               "({t}: {e}); the record is completed the next time this run is opened")
REPORT_NOT_WRITTEN = "report not written"   # 보고서 노드가 실패했을 때 상태 메시지에 한 번만 붙이는 말
NEXT_ZIP = ".result.next.zip"           # 새 결과 ZIP을 먼저 쓰는 임시 이름(delivery/ 아래)
NEXT_JSON = ".result.next.json"         # 결과 ZIP을 바꾸는 중이라는 기록(delivery/ 아래)
WHY_NO_MARKER = "no completeness marker"
WHY_OTHER_APPLIED = "the completeness marker names other proposals than the state"
WHY_NO_CANDIDATE = "the candidate folder is missing"
WHY_NO_PROOF = "no record that the applied files were stored completely"
# 라운드마다 노드가 7개 실행된다. LangGraph 기본 한도 25로는 모자라서 넉넉히 둔다
RECURSION_BASE = 100
RECURSION_PER_ROUND = 20
# 성공이 아닌 사유의 우선순위. 결과를 믿을 수 없는 사유가 먼저고, state.reason에는 첫 번째 사유가 들어간다
_REASON_ORDER = (R_SCAN_INCOMPLETE, R_ZIP_HASH_MISMATCH, R_CANDIDATE_BUILD_FAILED, R_RESIDUAL_FINDINGS,
                 R_MANUAL_REVIEW, R_NO_CANDIDATE_VERSION, R_INHOUSE_ARTIFACT)
# 예외 메시지에서 마스킹할 값을 고르는 단어. 이름에 이 단어가 든 환경 변수의 값과 IVAS 아이디를 가린다
_SECRET_WORDS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "CREDENTIAL", "PRIVATE_KEY")
_SECRET_NAMES = ("IVAS_USER_ID", "IVAS_PASSWORD")
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^\s/@'\"<>]+@")
_MIN_SECRET_LEN = 4


class RemediationGraphState(TypedDict, total=False):
    """그래프 상태를 담는 TypedDict. 실행 id, 방금 실행한 노드, 라운드, 멈춤 표시, 다음 노드만 둔다."""
    id: str
    node: str
    round: int
    halt: bool
    next: str


# ---------------------------------------------------------------------------- helpers
def _now() -> float:
    return time.time()


def scrub_message(text: str, root: Path | None = None, limit: int = 2000) -> str:
    """사람이 읽을 메시지에서 비밀 값, URL의 계정 정보, 실행 폴더의 절대 경로를 지운다. 예외를 던지지 않는다.
    마스킹에 실패하면 ``[message withheld]``를 반환한다."""
    try:
        out = str(text)
        values = set()
        for k, v in os.environ.items():
            up = k.upper()
            if (up in _SECRET_NAMES or any(w in up for w in _SECRET_WORDS)) and v and len(v) >= _MIN_SECRET_LEN:
                values.add(v)
        for v in sorted(values, key=len, reverse=True):   # 짧은 값이 긴 값의 일부만 지우지 않도록 긴 값부터 처리한다
            out = out.replace(v, "***")
        out = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}***@", out)
        if root is not None:
            for r in {str(Path(root).resolve()), str(root)}:
                out = out.replace(r + os.sep, "").replace(r, ".")
        return _scrub.scrub(out)[:limit]
    except Exception:  # noqa: BLE001 - 마스킹에 실패하면 원문 대신 정해진 문구를 쓴다
        return "[message withheld]"


def _safe_rmtree(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        fsops.unlink(p)
    elif p.exists():
        fsops.rmtree(p)


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _copy_atomic(src: Path, dst: Path) -> None:
    """``src``를 같은 폴더의 임시 파일에 복사하고 fsync한 뒤 ``dst``로 바꾼다."""
    tmp = dst.with_name(f".{dst.name}.copy-tmp")
    try:
        shutil.copyfile(src, tmp)
        # Windows의 fsync는 읽기 전용 핸들을 거부한다. 그래서 쓰기 가능한 핸들로 연다
        with open(tmp, "r+b") as f:
            os.fsync(f.fileno())
        fsops.replace(tmp, dst)
    except BaseException:
        _unlink_quiet(tmp)
        raise


class DeliveryNotWritten(Exception):
    """새 결과 ZIP을 쓰지 못했을 때 던지는 예외. 이전 결과 ZIP은 그대로 남는다.

    Raised by `_Holder.install_delivery` when the new result ZIP could not be written (full disk, read-only folder);
    the previous delivery is untouched. The message is `DELIVERY_NOT_WRITTEN`."""

    def __init__(self, round: int, error: BaseException, left: str, root: Path | None = None):
        self.round, self.left = round, left
        text = scrub_message(str(error), root, 300)
        super().__init__(DELIVERY_NOT_WRITTEN.format(r=round, t=type(error).__name__, e=text, left=left))


class DeliveryRecordNotWritten(Exception):
    """result.zip을 바꾼 뒤 ``delivery.json``이나 state를 쓰지 못했을 때 던지는 예외.
    result.zip은 이미 새 라운드의 결과다. 기록은 다음에 실행 폴더를 열 때 채운다.

    Raised by `_Holder.install_delivery` after `os.replace` succeeded when `delivery/delivery.json` and / or the
    state file could not be written. The swap is rolled forward: `state.delivery` (in memory) is the new ZIP, the
    marker `delivery/.result.next.json` stays so that `recover_delivery` completes the records on the next entry.
    `failed` = [(run-relative record path, exception)]; the message is `DELIVERY_RECORD_NOT_WRITTEN`."""

    def __init__(self, round: int, failed: list[tuple[str, BaseException]], root: Path | None = None):
        self.round, self.failed = round, list(failed)
        first = self.failed[0][1]
        what = " and ".join(f[0] for f in self.failed)
        text = scrub_message(str(first), root, 300)
        super().__init__(DELIVERY_RECORD_NOT_WRITTEN.format(r=round, what=what, t=type(first).__name__, e=text))


class ScanSourceChanged(Exception):
    """검사용 ZIP(``original/scan-source.zip``)의 sha256이 기록한 값과 다를 때 던지는 예외.
    ZIP을 다시 만들었을 때와 결과 폴더로 복사했을 때 확인한다.

    The scan source ZIP cannot be made again with the sha256 the state recorded; the run ends FAILED
    `zip_hash_mismatch` with `SCAN_SOURCE_CHANGED`."""


SCAN_SOURCE_CHANGED = ("the scan source ZIP (original/scan-source.zip) could not be made again with the sha256 recorded "
                       "in the state")


def recover_delivery(paths: RunPaths, state: RemediationState) -> bool:
    """결과 ZIP을 바꾸던 중에 끝난 실행을 정리한다. state를 바꿨으면 True를 반환한다.
    - result.zip이 이미 새 ZIP이면 state와 ``delivery.json``을 새 ZIP에 맞춘다
    - 아니면 임시 파일을 지우고 이전 결과 ZIP을 그대로 둔다

    Repair a swap `_Holder.install_delivery` left half done (`delivery/.result.next.json` present): result.zip already
    has the new sha256 → `state.delivery` and `delivery.json` become the recorded new info (the old bytes, when they
    were a verified delivery, are in the kept copy); otherwise the swap is rolled back (the temporary ZIP removed, a
    verified copy this swap made removed when it is a byte copy of the result.zip that is still there). Leftover
    temporary files of a frozen ZIP (`.result.next.zip*`) and of a verified copy a kill interrupted
    (`.result-r<k>-verified.zip.copy-tmp`) are removed. Never raises for an unreadable marker. Called at every entry
    that can see a run folder again: `run_remediation`, `repair_final` (`chain.resume`, `chain._repair_report`, the
    service's startup recovery and its repair of a final state whose owner is gone)."""
    folder = paths.delivery_zip.parent
    nz, nj = folder / NEXT_ZIP, folder / NEXT_JSON
    if not folder.is_dir():
        return False
    changed = False
    data = read_json(nj) if nj.exists() else None
    info = None
    if isinstance(data, dict) and isinstance(data.get("delivery"), dict):
        try:
            info = DeliveryInfo.from_dict(data["delivery"])
        except TypeError:
            info = None
    if info is not None:
        cur = sha256_file(paths.delivery_zip) if paths.delivery_zip.is_file() else ""
        if cur and cur == info.sha256:
            if state.delivery is None or state.delivery.sha256 != info.sha256:
                state.delivery = info
                changed = True
            if read_json(paths.delivery_json) != state.delivery.to_dict():
                write_json_atomic(paths.delivery_json, state.delivery.to_dict())
            log.warning("remediation %s: a result ZIP swap that the run did not finish was completed (round %s)",
                        state.id, info.round)
        else:
            vrel = str(data.get("verified") or "")
            vz = paths.root / vrel if vrel else None
            if vz is not None and vz.is_file() and cur and sha256_file(vz) == cur:
                _unlink_quiet(vz)
                _unlink_quiet(vz.with_suffix(".json"))
            log.warning("remediation %s: a result ZIP swap that the run did not finish was rolled back (round %s)",
                        state.id, info.round)
    # 결과 ZIP 임시 파일과, 검증한 ZIP을 복사하던 중에 끝나 남은 임시 파일을 지운다
    for f in [nz, *folder.glob(f"{NEXT_ZIP}.*"), *folder.glob(f".{NEXT_ZIP}.*"),
              *folder.glob(".result-r*-verified.zip.copy-tmp")]:
        _unlink_quiet(f)
    _unlink_quiet(nj)
    return changed


def delivery_swap_pending(paths: RunPaths) -> bool:
    """결과 ZIP을 바꾸다 남은 기록(``delivery/.result.next.json``)이나 임시 파일이 있는지 확인한다.

    Whether `recover_delivery` has something to do: the swap marker or a temporary file of a swap is left."""
    folder = paths.delivery_zip.parent
    if not folder.is_dir():
        return False
    return ((folder / NEXT_JSON).exists() or (folder / NEXT_ZIP).exists()
            or any(folder.glob(".result-r*-verified.zip.copy-tmp")) or any(folder.glob(f".{NEXT_ZIP}.*")))


class _Holder:
    """노드 함수들이 공유하는 무거운 객체를 그래프 상태 밖에 모아 두는 클래스."""

    def __init__(self, state: RemediationState, paths: RunPaths, cfg: Any, components: Components,
                 save: Callable[[RemediationState], None], journal: Journal, stop: StopCheck):
        self.state, self.paths, self.cfg, self.c = state, paths, cfg, components
        self._save, self._journal, self._stop = save, journal, stop
        self._paths_list: list[str] | None = None
        self._unknown_noted: set[str] = set()
        # deliver_failed_candidate가 남겨 둔 이전 결과 ZIP이 빌드로 확인되지 않은 것이면 True
        self.kept_unconfirmed = False

    # -- plumbing
    def save(self) -> None:
        self.state.updated = _now()
        self._save(self.state)

    def journal(self, event: dict[str, Any]) -> None:
        try:
            self._journal({"remediation": self.state.id, **event})
        except Exception:  # 저널 한 줄을 못 써도 노드는 실패시키지 않는다
            log.debug("journal line not written: %s", event.get("event"), exc_info=True)

    def stop(self) -> bool:
        try:
            return bool(self._stop())
        except Exception:  # noqa: BLE001 - 중지 표시를 읽지 못하면 중지 요청이 없는 것으로 본다
            return False

    def time_left(self) -> float | None:
        """실행 시간 한도에서 남은 초를 반환한다. 한도가 없으면(중지 확인 함수에 ``time_left``가 없으면) None이다.

        Seconds left of the run's time budget (`chain._Budget.wrap` attaches `time_left` to the stop check)."""
        fn = getattr(self._stop, "time_left", None)
        if not callable(fn):
            return None
        try:
            left = fn()
        except Exception:  # noqa: BLE001 - 남은 시간을 알 수 없으면 한도가 없는 것으로 본다
            return None
        return float(left) if left is not None else None

    @property
    def rcfg(self) -> Any:
        return self.cfg.remediation

    @property
    def paths_list(self) -> list[str]:
        if self._paths_list is None:
            self._paths_list = list(self.c.intake.manifest_paths(self.paths.manifest))
        return self._paths_list

    @property
    def project_root(self) -> str:
        return self.state.intake.project_root if self.state.intake else "."

    def project(self, base: Path) -> Path:
        return self.paths.project(base, self.project_root)

    @property
    def project_paths(self) -> list[str]:
        """프로젝트 루트 기준의 manifest 파일 경로 목록. ``verify_text_only``에서 쓴다."""
        pr = self.project_root
        if pr in ("", "."):
            return list(self.paths_list)
        prefix = pr.rstrip("/") + "/"
        return [p[len(prefix):] for p in self.paths_list if p.startswith(prefix)]

    @property
    def outside_paths(self) -> list[str]:
        """프로젝트 폴더 밖에 있는 manifest 파일 경로 목록.
        ZIP이 최상위 폴더로 감싸여 있을 때 그 폴더 옆의 파일들이다."""
        pr = self.project_root
        if pr in ("", "."):
            return []
        prefix = pr.rstrip("/") + "/"
        return [p for p in self.paths_list if not p.startswith(prefix)]

    @property
    def target_kw(self) -> dict[str, Any]:
        """Maven 게이트웨이에 넘길 빌드 대상(이관의 최종 빌드 대상 폴더)을 반환한다.
        비어 있으면 넘기지 않고 루트에서 한 번 빌드한다."""
        targets = list(getattr(self.state, "build_targets", None) or [])
        return {"targets": targets} if targets else {}

    def build_outcome(self, outcome: Any, log_path: Path) -> Any:
        """빌드 결과를 정리해서 반환한다. 로그 경로는 실행 폴더 기준 상대 경로로 바꾼다.
        요약에서는 비밀 값과 절대 경로를 지운다.
        빌드 대상이 여럿이면 게이트웨이가 알려 준 실패한 대상의 로그를 쓴다."""
        given = Path(outcome.log) if getattr(outcome, "log", "") else None
        outcome.log = self.rel(given if given is not None and given.is_absolute() and given.exists() else log_path)
        outcome.summary = scrub_message(outcome.summary or "", self.paths.root, 4000)
        return outcome

    @property
    def baseline_tree(self) -> Path:
        return self.paths.root / "baseline" / "tree"

    def rel(self, p: Path) -> str:
        try:
            return self.paths.rel(p)
        except ValueError:
            return Path(p).name

    # -- status helpers
    def halt(self, status: str, reason: str, next_node: str, message: str = "") -> None:
        """확인, 중지 같은 대기 상태로 작업을 멈춘다. 보고서 노드는 실행하지 않는다."""
        st = self.state
        st.status, st.reason, st.next_node, st.message = status, reason, next_node, message
        st.reasons = [reason]

    @property
    def baseline_failed_tolerated(self) -> bool:
        """조치 전 빌드가 실패했지만 그 실패를 받아들이고 이어 가는 실행인지 확인한다."""
        st = self.state
        return bool(st.baseline_tolerated and st.baseline_build is not None and not st.baseline_build.ok)

    @property
    def nothing_unconfirmed(self) -> bool:
        """확정한 라운드 가운데 빌드로 확인하지 못한 변경이 하나도 없는지 확인한다.
        바꾼 버전이 없거나, 버전을 바꾼 수정본의 빌드가 모두 통과했을 때 True다."""
        return not any(rs.committed and rs.applied and rs.build_tolerated for rs in self.state.rounds)

    def finish(self, status: str, reason: str, message: str = "", reasons: list[str] | None = None) -> str:
        """최종 상태(FAILED / INCOMPLETE / SUCCEEDED)를 정하고 다음 노드로 보고서 노드를 반환한다.
        조치 전 빌드 실패를 받아들인 실행은 아래처럼 끝낸다.
        - FAILED면 상태는 그대로 두고 사유 끝에 ``baseline_build_failed``를 붙인다
        - 빌드로 확인하지 못한 변경이 있으면 INCOMPLETE로 끝낸다. 첫 사유는 ``baseline_build_failed``다
        - 확인하지 못한 변경이 없으면 그 사유를 빼고 원래대로 끝낸다

        Tolerated baseline failure: FAILED keeps its reasons and gets `baseline_build_failed` appended. Otherwise, when
        a committed round applied a proposal whose candidate build failed with the baseline's errors (changes no build
        confirmed), the status is INCOMPLETE with `baseline_build_failed` first; when none did (the delivery is the
        original, or every candidate build passed), `baseline_build_failed` is dropped and the run ends as it would
        without the tolerance: SUCCEEDED when nothing else is left (the reason list empty), INCOMPLETE with its own
        reasons otherwise."""
        st = self.state
        reasons = list(reasons) if reasons is not None else ([reason] if reason else [])
        if self.baseline_failed_tolerated and status != FAILED and self.nothing_unconfirmed:
            # 빌드로 확인하지 못한 변경이 없다. 조치 전 빌드 실패는 사유에서 뺀다
            reasons = [x for x in reasons if x != R_BASELINE_BUILD_FAILED]
            status = status if reasons else SUCCEEDED
            reason = reasons[0] if reasons else ""
        elif self.baseline_failed_tolerated:
            # 빌드로 확인하지 못한 변경이 있어서 성공으로 끝내지 않는다. 실패가 아니면 조치 전 빌드 실패를 첫 사유로 둔다
            reasons = [x for x in reasons if x != R_BASELINE_BUILD_FAILED]
            if status == FAILED:
                reasons.append(R_BASELINE_BUILD_FAILED)
            else:
                status, reasons = INCOMPLETE, [R_BASELINE_BUILD_FAILED, *reasons]
            reason = reasons[0]
        st.status, st.reason = status, reason
        st.reasons = reasons
        if message:
            st.message = message
        self.fill_counts()
        return "report"

    def discard_delivery(self) -> None:
        """실패한 수정본 정책이 discard일 때 결과 ZIP을 남기지 않는다."""
        self.state.delivery = None
        for p in (self.paths.delivery_zip, self.paths.delivery_json):
            if p.exists():
                p.unlink()

    def verified_earlier(self, r: int) -> bool:
        """지금 delivery/result.zip이 라운드 ``r`` 이전에 빌드, 확정, 재검사까지 마친 결과 ZIP인지 확인한다.
        재검사는 완료됐고 sha256이 같아야 한다."""
        st = self.state
        d = st.delivery
        committed = [rs.round for rs in st.rounds if rs.committed and rs.round < r]
        final = self.load_scan(st.final_scan)
        return bool(d is not None and (d.build_ok or d.build_tolerated) and committed and d.round == max(committed) and final is not None
                    and final.complete and final.zip_sha256 == d.sha256 and self.paths.delivery_zip.is_file()
                    and sha256_file(self.paths.delivery_zip) == d.sha256)

    def discard_candidate(self, r: int) -> None:
        """라운드 ``r``의 수정본을 버린다.
        정책이 discard이거나, 버전 문자열 밖의 변경이 있거나, 의존성 결과가 계획과 다를 때 호출한다.
        이전 라운드에서 빌드, 확정, 재검사까지 마친 결과 ZIP이 있으면 그대로 둔다. 없으면 결과 ZIP을 지운다."""
        if self.verified_earlier(r):
            self.journal({"event": "earlier_delivery_kept", "round": r, "delivery_round": self.state.delivery.round})
            return
        self.discard_delivery()

    # -- candidate completeness marker
    def _ready_path(self, r: int) -> Path:
        return self.paths.round_dir(r) / CANDIDATE_READY

    def mark_candidate(self, r: int) -> None:
        """라운드 ``r``의 수정본이 복사와 적용을 모두 마쳤다는 표시를 남긴다.
        표시에는 적용한 제안 id 목록과 ``expected.json``의 sha256이 들어간다.

        Written after copy + apply (and the text-only check) of round `r`'s candidate and after `applied/` and
        `expected.json` were stored (`_write_expected`): `{round, applied, expected}` where `expected` is the sha256 of
        `rounds/<r>/expected.json`. The `expected` key is the proof that the applied files are stored completely; a
        marker without it (written by an earlier version) does not vouch for the candidate."""
        data: dict[str, Any] = {"round": r, "applied": list(self.round_state(r).applied)}
        exp = self.paths.round_dir(r) / EXPECTED_FILE
        if exp.is_file():
            data["expected"] = sha256_file(exp)
        write_json_atomic(self._ready_path(r), data)

    def unmark_candidate(self, r: int) -> None:
        """수정본을 지우거나 다시 만들기 전에 완료 표시를 지운다."""
        try:
            self._ready_path(r).unlink()
        except FileNotFoundError:
            pass

    def vouch_problem(self, r: int) -> str:
        """라운드 ``r``의 수정본을 빌드나 전달에 쓸 수 없는 이유를 반환한다. 쓸 수 있으면 빈 문자열이다.

        "" when the candidate exists, its completeness marker names exactly the state's `applied` of round `r`, and the
        marker carries the sha256 of `rounds/<r>/expected.json`, which names the same round and `applied` and lists
        `applied_files` that all exist under `rounds/<r>/applied/`. Else the reason (`WHY_*`), journaled as `why`."""
        data = read_json(self._ready_path(r))
        applied = list(self.round_state(r).applied)
        if not isinstance(data, dict):
            return WHY_NO_MARKER
        if data.get("applied") != applied:
            return WHY_OTHER_APPLIED
        if not self.paths.candidate(r).is_dir():
            return WHY_NO_CANDIDATE
        exp = self.paths.round_dir(r) / EXPECTED_FILE
        want = data.get("expected")
        if not isinstance(want, str) or not want or not exp.is_file() or sha256_file(exp) != want:
            return WHY_NO_PROOF
        rec = read_json(exp)
        if (not isinstance(rec, dict) or rec.get("round") != r or rec.get("applied") != applied
                or _rows(rec) is None):
            # 실행 비트가 없는 표(배포되지 않은 이전 코드가 쓴 것)도 증명으로 보지 않고 다시 적용한다
            return WHY_NO_PROOF
        ad = _applied_dir(self, r)
        if any(not (ad / str(x)).is_file() for x in rec.get("applied_files") or []):
            return WHY_NO_PROOF
        return ""

    def candidate_vouched(self, r: int) -> bool:
        """라운드 ``r``의 수정본을 빌드나 전달에 쓸 수 있는지 확인한다. ``vouch_problem``이 빈 문자열이면 True다."""
        return not self.vouch_problem(r)

    def deliver_failed_candidate(self, r: int) -> str | None:
        """빌드에 실패한 라운드 ``r``의 수정본을 결과 ZIP으로 만든다. 정책이 include일 때 호출한다.
        accepted/는 바꾸지 않는다.
        - 이전 라운드에서 검증한 결과 ZIP은 ``delivery/result-r<k>-verified.zip``으로 복사해 두고 그 경로를 반환한다
        - 검증한 ZIP이 없으면 빈 문자열을 반환한다
        - 수정본 완료 표시가 없으면 아무것도 만들지 않고 None을 반환한다

        The delivery gets build_ok=False, scanned=False.
        None (nothing frozen, nothing moved) when the state does not vouch for the candidate (`candidate_vouched`).
        The candidate is synchronised with its expected bytes right before it is frozen (`_sync_candidate`, recorded
        in `build_changed`); a path that cannot be put back raises `CandidateNotRestored` before anything is moved.
        The ZIP is written through `install_delivery` (frozen under a temporary name first, the verified ZIP COPIED
        aside only after that); a freeze that fails raises `DeliveryNotWritten` with the previous delivery intact."""
        st, p = self.state, self.paths
        if not self.candidate_vouched(r):
            self.journal({"event": "candidate_not_vouched", "round": r})
            return None
        # 마지막 빌드 뒤에 Maven이 또 실행됐을 수 있다. 내보내기 직전에 기대하는 바이트로 다시 맞춘다.
        # 맞추지 못하면 CandidateNotRestored를 던진다. 이전 라운드의 결과 ZIP은 그대로 남는다
        _sync_candidate(self, r, p.candidate(r))
        earlier = st.delivery if self.verified_earlier(r) else None
        # 이 라운드의 실패한 수정본을 이미 결과 ZIP으로 두고 상태를 저장한 뒤 끝났던 노드에 다시 들어온 경우다
        again = (earlier is None and st.delivery is not None and st.delivery.round == r and not st.delivery.build_ok
                 and not st.delivery.build_tolerated)
        self.kept_unconfirmed = bool(earlier is not None and earlier.build_tolerated and not earlier.build_ok)
        # 새 ZIP을 임시 이름으로 다 쓴 뒤에만 검증한 ZIP을 복사해 두고 result.zip을 바꾼다. 쓰지 못하면 이전 결과가 그대로 남는다
        kept = self.install_delivery(
            lambda out: self.c.intake.freeze_delivery(p.candidate(r), out, r, self.paths_list), r,
            build_ok=False, keep_verified=earlier is not None)
        if earlier is not None:
            self.journal({"event": "verified_delivery_kept", "round": r, "delivery_round": earlier.round, "file": kept,
                          "sha256": earlier.sha256})
        elif again:
            # 앞선 진입이 복사해 둔 검증한 ZIP을 그대로 알린다
            vz = find_verified_zip(p)
            m = re.fullmatch(r"result-r([0-9]{1,4})-verified\.zip", vz.name) if vz is not None else None
            if m is not None and int(m.group(1)) < r:
                kept = self.rel(vz)
                info = read_json(vz.with_suffix(".json"))
                self.kept_unconfirmed = bool(isinstance(info, dict) and info.get("build_tolerated")
                                             and not info.get("build_ok"))
        self.journal({"event": "failed_candidate_delivered", "round": r, "sha256": st.delivery.sha256})
        return kept

    # -- writing result.zip (one durable order for every delivery)
    def delivery_left(self) -> str:
        """결과 ZIP을 쓰지 못했을 때 지금 남아 있는 결과를 알리는 영어 문장을 만든다."""
        d = self.state.delivery
        if d is not None and self.paths.delivery_zip.is_file():
            return DELIVERY_LEFT_KEPT.format(k=d.round)
        return DELIVERY_LEFT_NONE

    def install_delivery(self, make: Callable[[Path], DeliveryInfo], r: int, *, build_ok: bool = True,
                         build_tolerated: bool = False, keep_verified: bool = False,
                         after: Callable[[DeliveryInfo], None] | None = None) -> str:
        """새 결과 ZIP을 임시 이름(``delivery/.result.next.zip``)으로 다 만든 뒤 result.zip과 바꾼다.
        그다음 ``delivery.json``과 state에 기록한다.
        - 만들지 못하면 이전 결과는 그대로 두고 ``DeliveryNotWritten``을 던진다
        - ``keep_verified``면 이전에 검증한 ZIP을 ``result-r<k>-verified.zip``으로 복사해 두고 그 경로를 반환한다

        The durable order (each step atomic; `recover_delivery` repairs a kill between them on the next entry):
        1. `make(delivery/.result.next.zip)` freezes the new ZIP under a temporary name;
        2. `delivery/.result.next.json` = `{delivery: <new info>, verified: <kept path or "">}` (the swap is pending);
        3. with `keep_verified`, the current result.zip is COPIED to `result-r<k>-verified.zip` (+ `.json`);
        4. `os.replace(.result.next.zip, result.zip)`;
        5. `state.delivery` = the new info and `after(info)` (the caller's other fields that describe the new ZIP,
        e.g. the no-change delivery's `final_scan`), so the state saved next is complete for the node's re-entry;
        6. `delivery.json` = the new info; 7. the state saved; 8. `.result.next.json` removed.
        An exception in steps 1–4 removes what this call made and raises `DeliveryNotWritten` (the previous delivery,
        its `delivery.json` and `state.delivery` untouched); a `ScanSourceChanged` from `make` is cleaned up the same
        way and raised as it is (`zip_hash_mismatch`). Once the replace happened the swap is rolled FORWARD: every
        later step is tried; when `delivery.json` or the state cannot be written the marker stays (so
        `recover_delivery` completes the records on the next entry) and `DeliveryRecordNotWritten` is raised, with
        `state.delivery` already the new ZIP. Returns the kept verified ZIP's path or "".

        Re-entry (v2.1z-bo): the state saved in step 7 is a mid-node save; every caller sets what its node's re-entry
        needs before it (the node functions' docstrings and INTERFACES "v2.1z-bo" list what a kill right after it
        does)."""
        st, p = self.state, self.paths
        folder = p.delivery_zip.parent
        nz, nj = folder / NEXT_ZIP, folder / NEXT_JSON
        made: list[Path] = []
        kept = ""
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for f in (nz, nj):
                _unlink_quiet(f)
            made.append(nz)
            info = make(nz)
            info.path = self.rel(p.delivery_zip)
            info.build_ok, info.build_tolerated, info.scanned = bool(build_ok), bool(build_tolerated), False
            d = st.delivery if keep_verified else None
            vz = verified_zip_path(p, d.round) if d is not None else None
            kept = self.rel(vz) if vz is not None else ""
            # 바꾸는 중이라는 기록을 검증한 ZIP 복사 전에 남긴다. 여기서 끝나도 다음 진입 때 복사본까지 정리된다
            made.append(nj)
            write_json_atomic(nj, {"delivery": info.to_dict(), "verified": kept})
            if d is not None and vz is not None:
                made += [vz, vz.with_suffix(".json")]
                _copy_atomic(p.delivery_zip, vz)
                vinfo = DeliveryInfo.from_dict(d.to_dict())
                vinfo.path, vinfo.scanned = kept, True
                write_json_atomic(vz.with_suffix(".json"), vinfo.to_dict())
            fsops.replace(nz, p.delivery_zip)
        except ScanSourceChanged:
            # 검사용 ZIP과 다른 바이트를 결과 ZIP으로 두지 않는다. 쓰기 실패가 아니므로 그대로 던진다(zip_hash_mismatch)
            for f in reversed(made):
                _unlink_quiet(f)
            raise
        except Exception as e:  # noqa: BLE001 - 어떤 이유로든 새 ZIP을 쓰지 못하면 이전 결과를 그대로 둔다
            for f in reversed(made):
                _unlink_quiet(f)
            err = DeliveryNotWritten(r, e, self.delivery_left(), self.paths.root)
            log.error("remediation round %s: %s", r, err)
            self.journal({"event": "delivery_not_written", "round": r, "error": type(e).__name__,
                          "kept_round": st.delivery.round if st.delivery is not None else None})
            raise err from e
        # result.zip을 바꾼 뒤에는 되돌리지 않는다. state가 새 ZIP을 가리키도록 남은 기록을 모두 시도한다.
        # 기록을 쓰지 못하면 표시 파일을 남긴다. 다음 진입 때 recover_delivery가 마저 맞춘다
        st.delivery = info
        if after is not None:
            after(info)
        failed: list[tuple[str, BaseException]] = []
        try:
            write_json_atomic(p.delivery_json, st.delivery.to_dict())
        except Exception as e:  # noqa: BLE001 - 디스크가 가득 차도 state는 저장해 본다
            failed.append((self.rel(p.delivery_json), e))
        try:
            self.save()
        except Exception as e:  # noqa: BLE001 - 저장하지 못하면 다음 진입 때 표시 파일로 맞춘다
            failed.append((self.rel(p.state), e))
        if failed:
            err = DeliveryRecordNotWritten(r, failed, self.paths.root)
            log.error("remediation round %s: %s", r, err)
            self.journal({"event": "delivery_record_not_written", "round": r, "records": [f[0] for f in failed],
                          "error": type(failed[0][1]).__name__})
            raise err
        try:
            _unlink_quiet(nj)
        except OSError:
            log.warning("remediation round %s: the result ZIP swap marker was not removed; the next entry removes it", r)
        return kept

    def mark_scanned(self) -> None:
        """재검사가 완료됐고 검사한 ZIP이 지금 결과 ZIP과 같으면(sha256) ``delivery.scanned``를 켠다. 아니면 끈다."""
        d = self.state.delivery
        if d is None:
            return
        final = self.load_scan(self.state.final_scan)
        d.scanned = bool((d.build_ok or d.build_tolerated) and final is not None and final.complete
                         and final.zip_sha256 == d.sha256)

    # -- data helpers
    def load_scan(self, rel: str) -> ScanResult | None:
        """저장한 검사 결과를 읽어 반환한다. 경로가 비었거나 읽을 수 없으면 None을 반환한다.
        ``remediation.file_components``가 count면 파일 목록을 다 읽지 못한 검사를 미완료로 본다.

        A stored scan through `ivas.count_view` with the current `remediation.file_components`."""
        if not rel:
            return None
        data = read_json(self.paths.root / rel)
        if not isinstance(data, dict):
            return None
        from .ivas import count_view
        return count_view(ScanResult.from_dict(data), self.file_mode)

    def load_plan(self, n: int) -> RoundPlan | None:
        data = read_json(self.paths.plan(n))
        return RoundPlan.from_dict(data) if isinstance(data, dict) else None

    def round_state(self, n: int) -> RoundState:
        for rs in self.state.rounds:
            if rs.round == n:
                return rs
        rs = RoundState(round=n)
        self.state.rounds.append(rs)
        self.state.rounds.sort(key=lambda r: r.round)
        return rs

    def before_tree(self, n: int) -> Path:
        """라운드 n에서 비교 기준이 되는 이전 빌드 폴더를 반환한다.
        그 전에 확정된 마지막 수정본이다. 없으면 조치 전 빌드에 쓴 폴더다."""
        committed = [rs.round for rs in self.state.rounds if rs.committed and rs.round < n]
        return self.paths.candidate(max(committed)) if committed else self.baseline_tree

    def all_plans(self) -> list[RoundPlan]:
        out = []
        for rs in self.state.rounds:
            p = self.load_plan(rs.round)
            if p is not None:
                out.append(p)
        return out

    def fill_counts(self) -> None:
        """``state.counts``를 채운다. 취약점 수는 검사 결과의 행에서 센다.
        남은 항목 수는 직접 확인 항목과 사내 항목의 행에서 센다.

        What each count counts:
        - `baseline_findings` / `final_findings`: actionable rows of the baseline / final scan (IVAS findings, not
          inventory items), so a finding whose library was split into an in-house item and a project item is one row;
          `*_ignored`: the rows of an `ignore_actions` code; `*_file_findings` (only under
          `remediation.file_components: report`): the file-component rows, listed in the report and counted nowhere else;
          left out for a scan whose file lists were not read (no rows), which `file_lists_unread` counts instead.
        - `fixed`: baseline actionable findings (kind, CVE id, identifier compared case-insensitively) absent from the
          complete final scan: a finding still reported for any module is not fixed, whichever part was changed.
        - `residual`: residual ROWS in the state (one per g:a, version, reason after `_merge_residuals`); a library split
          into an in-house part and a project part left for review is two rows, one per section.
        - `residual_inhouse`: the `inhouse_artifact` rows among them (one per in-house item).
        - `proposals` / `proposals_applied`: proposals of every round and the applied ones; a variant override is its
          own proposal."""
        st = self.state
        base = self.load_scan(st.baseline_scan)
        final = self.load_scan(st.final_scan)
        plans = self.all_plans()
        props = [p for pl in plans for p in pl.proposals]
        applied = {pid for rs in st.rounds for pid in rs.applied}
        counts: dict[str, int] = {
            "rounds": len(st.rounds),
            "residual": len(st.residuals),
            "proposals": len(props),
            "proposals_applied": len(applied),
            # 사내 아티팩트 담당자가 처리해야 할 항목 수다. 보고서가 담당자 연락 안내와 함께 보여 준다
            "residual_inhouse": sum(1 for x in st.residuals if x.reason == R_INHOUSE_ARTIFACT),
        }
        # *_findings는 조치 대상만 센다. ignore_actions 코드의 항목은 *_ignored로 따로 센다
        # report 모드의 파일 컴포넌트는 *_file_findings로 따로 센다
        report_files = files_reported(self.file_mode)
        # 파일 목록을 끝까지 읽지 못한 검사는 0건으로 세지 않고 file_lists_unread로 따로 센다
        unread = 0
        base_act: list[Any] = []
        if base is not None and base.complete:
            base_act, base_ign = self.split(base)
            counts["baseline_findings"] = len(base_act)
            counts["baseline_ignored"] = len(base_ign)
            if report_files:
                unread += not base.file_complete
                if base.file_complete or self.files(base):
                    counts["baseline_file_findings"] = len(self.files(base))
        if final is not None and final.complete:
            final_act, final_ign = self.split(final)
            counts["final_findings"] = len(final_act)
            counts["final_ignored"] = len(final_ign)
            if report_files and st.final_scan != st.baseline_scan:
                unread += not final.file_complete
            if report_files and (final.file_complete or self.files(final)):
                counts["final_file_findings"] = len(self.files(final))
            if base is not None and base.complete:
                def k(f: Any) -> str:
                    # IVAS가 같은 라이브러리를 다른 대소문자로 보고해도 같은 취약점으로 센다. 보고서와 같은 키를 쓴다
                    return finding_key(f.kind, f.id, f.identifier)
                left = {k(f) for f in final_act}
                counts["fixed"] = len({k(f) for f in base_act} - left)
        if unread:
            counts["file_lists_unread"] = unread
        st.counts = counts

    # -- file components (remediation.file_components)
    @property
    def file_mode(self) -> str:
        """``remediation.file_components`` 값을 반환한다. 이전 설정 객체에 없으면 report다."""
        return str(getattr(self.rcfg, "file_components", "report") or "report")

    def files(self, scan: ScanResult) -> list[Any]:
        """report 모드에서 보고서에만 싣는 파일 컴포넌트 항목을 반환한다. count 모드면 빈 목록을 반환한다."""
        return split_files(list(scan.findings), self.file_mode)[1]

    # -- action codes (remediation.ignore_actions)
    @property
    def ignored_codes(self) -> frozenset[str]:
        from .ivas import ignore_set
        return ignore_set(getattr(self.rcfg, "ignore_actions", None))

    def split(self, scan: ScanResult) -> tuple[list[Any], list[Any]]:
        """검사 결과를 (조치 대상, 제외 항목)으로 나눈다.
        제외 항목은 ``ignore_actions`` 코드의 항목이다. 보고서에는 남지만 계획과 남은 취약점 수에는 들어가지 않는다.
        모르는 코드나 빈 코드는 조치 대상으로 보고 저널에 한 번만 남긴다.

        Under `remediation.file_components: report` the file findings are in neither list (`files`)."""
        from .ivas import split_findings, unknown_actions
        rest, _files = split_files(list(scan.findings), self.file_mode)
        act, ign = split_findings(rest, self.ignored_codes)
        new = [c for c in unknown_actions(rest) if c not in self._unknown_noted]
        if new:
            self._unknown_noted.update(new)
            log.info("IVAS action codes outside the known table treated as actionable: %s", ", ".join(new))
            self.journal({"event": "ivas_action_unknown", "codes": new, "label": scan.label})
        return act, ign

    # -- the original ZIP as the delivery (nothing changed)
    def deliver_original(self) -> None:
        """바꾼 것이 없을 때 조치 전에 검사한 ZIP을 그대로 결과 ZIP으로 둔다. 재검사 결과는 조치 전 검사 결과를 쓴다.
        빌드 산출물을 뺀 업로드면 ``original/scan-source.zip``을, 아니면 원본 ZIP을 쓴다.

        The no-change delivery (round 0) is the bytes the baseline scan uploaded (`scan_source`): the upload itself when
        the intake left nothing out (byte for byte as before), else `original/scan-source.zip`. Written through
        `install_delivery`, which sets `final_scan` (= the baseline scan) and `delivery.scanned` before its mid-node
        state save (v2.1z-bo): a kill right after that save re-enters the node with a state that already says what the
        uninterrupted node would have left (the callers skip `deliver_original` when a delivery exists). A
        `ScanSourceChanged` raised by the copy reaches the node as it is (`zip_hash_mismatch`)."""
        st, p = self.state, self.paths
        src, sha = self.scan_source()

        def copy(out: Path) -> DeliveryInfo:
            _copy_atomic(src, out)
            got = sha256_file(out)
            if got != sha:
                raise ScanSourceChanged(SCAN_SOURCE_CHANGED)
            return DeliveryInfo(path=self.rel(p.delivery_zip), sha256=got, size=out.stat().st_size,
                                files=st.intake.files if st.intake else 0,
                                tree_hash=st.intake.tree_hash if st.intake else "", round=0)
        def scanned(_info: DeliveryInfo) -> None:
            # 결과 ZIP이 조치 전에 검사한 ZIP과 같은 바이트다. 그 sha256을 빠짐없이 검사했으면 scanned로 본다.
            # install_delivery가 state를 저장하기 전에 채운다. 노드 도중에 끝나도 다시 들어온 노드가 이어서 판단한다
            st.final_scan = st.baseline_scan
            self.mark_scanned()
        # 원본도 빌드되지 않았으면 빌드로 확인한 결과 ZIP이 아니다
        tolerated = self.baseline_failed_tolerated
        self.install_delivery(copy, 0, build_ok=not tolerated, build_tolerated=tolerated, after=scanned)

    # -- what the baseline scan uploads (and a no-change delivery is)
    def _scanned_original_before(self) -> bool:
        """이전 버전이 시작한 실행이 조치 전 검사에 원본 ZIP을 이미 올렸거나 올리는 중인지 확인한다."""
        st = self.state
        sha = st.intake.zip_sha256 if st.intake else ""
        if not sha:
            return False
        if st.pending_upload.get(BASELINE_LABEL) == sha:
            return True
        rec = st.pending_receipt.get(BASELINE_LABEL)
        if isinstance(rec, dict) and rec.get("zip_sha256") == sha:
            return True
        data = read_json(self.paths.root / st.baseline_scan) if st.baseline_scan else None
        return isinstance(data, dict) and data.get("zip_sha256") == sha

    def scan_source(self) -> tuple[Path, str]:
        """조치 전 검사에 올리고, 바꾼 것이 없을 때 결과 ZIP으로 쓰는 ZIP의 경로와 sha256을 반환한다.
        빌드 산출물을 뺀 업로드면 accepted/로 ``original/scan-source.zip``을 만들어 쓴다.
        처음 만든 sha256은 state에 기록한다. 다시 만든 ZIP의 sha256이 다르면 ``ScanSourceChanged``를 던진다.

        `(original/source.zip, intake.zip_sha256)` when `intake.build_output_files == 0` (today's bytes) or when a run
        started by an earlier version already sent the original to the baseline scan (`_scanned_original_before`: its
        recorded upload / receipt / scan names the original's sha256 and no scan-source sha was recorded). Otherwise
        `original/scan-source.zip` = `intake.freeze_delivery(accepted/, …, 0, manifest paths)`, deterministic: without a
        recorded `state.scan_source_sha256` it is (re)made and its sha256 recorded and saved; with one, the file is used
        when it has that sha256, else made again, and a different sha256 raises `ScanSourceChanged`."""
        st, p = self.state, self.paths
        orig = (p.original_zip, st.intake.zip_sha256 if st.intake else "")
        if st.intake is None or not st.intake.build_output_files:
            return orig
        if not st.scan_source_sha256 and self._scanned_original_before():
            return orig
        out = p.scan_source
        want = st.scan_source_sha256
        if want and out.is_file() and sha256_file(out) == want:
            return out, want
        info = self.c.intake.freeze_delivery(p.accepted, out, 0, self.paths_list)
        if want and info.sha256 != want:
            log.error("remediation %s: %s", st.id, SCAN_SOURCE_CHANGED)
            raise ScanSourceChanged(SCAN_SOURCE_CHANGED)
        if not want:
            st.scan_source_sha256 = info.sha256
            self.save()
            self.journal({"event": "scan_source_made", "sha256": info.sha256, "files": info.files,
                          "left_out": st.intake.build_output_files})
            log.info("remediation %s: the scan source ZIP leaves the upload's %d build output file(s) out (%s)", st.id,
                     st.intake.build_output_files, info.sha256)
        return out, info.sha256

    # -- one IVAS scan with the upload-safety rules
    def scan(self, node: str, label: str, zip_path: Path, sha: str, project_name: str) -> ScanResult | None:
        """IVAS 검사를 한 번 실행하고 결과를 반환한다. 확인 대기로 멈췄으면 None을 반환한다.
        - 업로드 전에 ``pending_upload``, 접수 정보를 받으면 ``pending_receipt``를 저장한다. 검사가 끝나면 둘 다 지운다
        - 접수 정보가 있으면 다시 올리지 않고 그 분석을 이어서 조회한다
        - 결과를 모르는 업로드가 있으면 ``AWAITING_CONFIRMATION``으로 멈춘다

        Order of the records: pending_upload -> pending_receipt -> cleared."""
        st = self.state
        receipt: UploadReceipt | None = None
        if label in st.pending_receipt:
            receipt = UploadReceipt.from_dict(st.pending_receipt[label])
            self.journal({"event": "scan_resume", "label": label, "analysis": receipt.analysis_id})
        elif label in st.pending_upload:
            # 이전 업로드의 결과를 모른다. IVAS에 같은 ZIP이 두 번 들어가지 않도록 자동으로 다시 올리지 않는다
            self.halt(AWAITING_CONFIRMATION, R_UPLOAD_UNCERTAIN, node,
                      f"the upload of scan '{label}' was sent but its result is unknown; check IVAS and confirm before resuming")
            self.journal({"event": "upload_uncertain", "label": label})
            return None
        else:
            st.pending_upload[label] = sha
            self.save()

        def on_receipt(r: UploadReceipt) -> None:
            st.pending_receipt[label] = r.to_dict()
            st.pending_upload.pop(label, None)
            self.save()
            self.journal({"event": "scan_receipt", "label": label, "analysis": r.analysis_id})

        try:
            res = self.c.scan(self.c.ivas, zip_path, sha, label, project_name, receipt=receipt, on_receipt=on_receipt,
                              stop=self.stop, journal=self.journal)
        except IvasUploadUncertain as e:
            st.pending_upload[label] = sha
            self.halt(AWAITING_CONFIRMATION, R_UPLOAD_UNCERTAIN, node,
                      scrub_message(f"the upload of scan '{label}' has an unknown result: {e}", self.paths.root))
            self.journal({"event": "upload_uncertain", "label": label})
            return None

        if not res.complete and R_STOP_REQUESTED in res.incomplete_reasons:
            # 원격 분석은 IVAS에서 계속 진행된다. 접수 정보를 남겨서 재개할 때 다시 올리지 않고 이어서 조회한다
            if res.receipt is not None:
                st.pending_receipt[label] = res.receipt.to_dict()
                st.pending_upload.pop(label, None)
            elif label not in st.pending_receipt:
                st.pending_upload.pop(label, None)      # 접수 정보도 불확실 예외도 없으면 올리기 전에 멈춘 경우다
            msg = (f"stop requested during scan '{label}'; the remote IVAS analysis keeps running and resume continues "
                   "polling it without uploading again") if label in st.pending_receipt else \
                f"stop requested before scan '{label}' uploaded anything"
            self.halt(STOPPED, R_STOP_REQUESTED, node, msg)
            self.journal({"event": "scan_stopped", "label": label})
            return None

        st.pending_upload.pop(label, None)
        st.pending_receipt.pop(label, None)
        write_json_atomic(self.paths.scan(label), res.to_dict())
        self.journal({"event": "scan_done", "label": label, "complete": res.complete, "findings": len(res.findings),
                      "incomplete_reasons": list(res.incomplete_reasons)})
        files = self.files(res)
        if files or not res.file_complete:
            # 파일 컴포넌트는 보고서에만 싣는다. 몇 건이고 무엇을 읽지 못했는지 남긴다
            self.journal({"event": "file_findings_reported", "label": label, "findings": len(files),
                          "unread": sum(1 for f in files if f.files_unread), "file_complete": res.file_complete})
        from .ivas import count_view
        # 저장한 파일은 그대로 두고, 이 실행의 판단은 지금 설정으로 본 결과로 한다
        return count_view(res, self.file_mode)


# ---------------------------------------------------------------------------- nodes
def _baseline_build(h: _Holder) -> str | None:
    st, c = h.state, h.c
    problems = list(c.maven.readiness())
    if problems:
        return h.finish(FAILED, R_NOT_READY, "; ".join(scrub_message(p) for p in problems))
    accepted_project = h.project(h.paths.accepted)
    st.boundary = list(c.intake.check_boundary(accepted_project, h.cfg, list(c.maven.allowed_hosts())))
    refused = [b for b in st.boundary if b.action == "refuse"]
    if refused:
        # 읽을 수 없는 pom은 화면에서도 이유가 보이도록 경로 뒤에 이유를 붙인다
        return h.finish(FAILED, R_BOUNDARY_REFUSED,
                        "; ".join(f"{b.code}: {b.path}" + (f": {b.detail}" if b.code == "unparsable_pom" and b.detail
                                                           else "") for b in refused))
    # 따로 복사한 폴더에서 빌드한다. accepted/에 target/이 생기면 결과 ZIP의 파일 목록이 원본과 달라진다
    tree = h.baseline_tree
    _safe_rmtree(tree)
    c.intake.copy_tree(h.paths.accepted, tree, h.paths_list)
    outcome = h.build_outcome(c.maven.package(h.project(tree), h.paths.baseline_log, h.stop, **h.target_kw), h.paths.baseline_log)
    st.baseline_build = outcome
    if not outcome.ok:
        if h.stop():
            h.halt(STOPPED, R_STOP_REQUESTED, "baseline_build", "stop requested during the baseline build")
            return None
        if st.baseline_tolerated and outcome.exit_code is not None and not outcome.timed_out:
            # 취약점 검사는 오픈소스 라이브러리를 보므로 빌드 오류가 있어도 이어 간다. 시간 초과나 실행 실패는 비교할 로그가 없어서 뺀다
            from . import maven as maven_mod
            errors = maven_mod.build_errors(_read_log(h, outcome), h.project(tree))
            h.journal({"event": "remediation_baseline_failed", "tolerated": True, "errors": errors,
                       "log": outcome.log, "exit_code": outcome.exit_code})
            log.warning("remediation %s: the baseline build failed (%d error(s), %s); going on with the scan because "
                        "the failure is tolerated for this run", st.id, errors, outcome.log)
            return "baseline_scan"
        return h.finish(FAILED, R_BASELINE_BUILD_FAILED, outcome.summary)
    return "baseline_scan"


def _read_log(h: _Holder, outcome: Any) -> str:
    """빌드 결과의 로그 파일(실행 폴더 기준 경로)을 읽는다. 없으면 빈 문자열을 반환한다."""
    try:
        p = h.paths.root / str(outcome.log or "")
        return p.read_text(encoding="utf-8", errors="replace") if outcome.log and p.is_file() else ""
    except OSError:
        return ""


def _baseline_scan(h: _Holder) -> str | None:
    st, p = h.state, h.paths
    # 업로드에서 빌드 산출물을 뺐으면 accepted/를 묶은 검사용 ZIP을, 아니면 원본 ZIP을 올린다
    zip_path, sha = h.scan_source()
    is_original = zip_path == p.original_zip
    # 올리기 전에 원본이 접수 때와 같은 바이트인지 확인한다. 검사용 ZIP은 scan_source가 sha256으로 이미 확인했다.
    # 접수 정보로 이어서 조회할 때는 이미 올린 뒤라서 확인하지 않는다
    if is_original and BASELINE_LABEL not in st.pending_receipt and sha256_file(p.original_zip) != sha:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "the original ZIP no longer has the sha256 recorded at intake")
    what = "the original ZIP" if is_original else "the scan source ZIP"
    name = f"{h.rcfg.ivas_project_prefix}-{st.id}-{BASELINE_LABEL}"
    stored = _stored_baseline(h, sha)
    if stored is not None:
        # 이 노드가 같은 바이트를 이미 끝까지 검사하고 저장했다(노드 도중에 상태를 저장한 뒤 끝난 경우). 다시 올리지 않는다
        h.journal({"event": "baseline_scan_reused", "sha256": sha})
        log.info("remediation %s: the stored baseline scan of the same bytes is used; nothing is uploaded again", st.id)
        res = stored
    else:
        res = h.scan("baseline_scan", BASELINE_LABEL, zip_path, sha, name)
    if res is None:
        return None
    st.baseline_scan = h.rel(p.scan(BASELINE_LABEL))
    if R_ZIP_HASH_MISMATCH in res.incomplete_reasons:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, f"IVAS analysed bytes whose sha256 differs from {what}")
    if not res.complete:
        # 끝나지 않은 검사를 0건으로 보지 않는다. 계획도 만들지 않는다
        return h.finish(INCOMPLETE, R_SCAN_INCOMPLETE, "baseline scan incomplete: " + ", ".join(res.incomplete_reasons))
    if res.zip_sha256 != sha:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "IVAS scanned a ZIP whose sha256 differs from "
                        + ("the original" if is_original else "the scan source ZIP"))
    actionable, _ignored = h.split(res)
    if not actionable:
        # 조치 대상이 없다(0건이거나 모두 ignore_actions 코드). 바꿀 것이 없으니 원본을 그대로 전달한다
        h.deliver_original()
        return "evaluate"
    st.round = max(st.round, 1)
    return "inventory"


def _stored_baseline(h: _Holder, sha: str) -> ScanResult | None:
    """같은 sha256을 끝까지 검사해 저장한 조치 전 검사 결과가 있으면 반환한다. 업로드나 접수가 남아 있으면 None이다.

    v2.1z-bo: `baseline_scan` never uploads again when the state names the stored baseline scan, no upload or
    receipt is pending for it, and that scan is complete with the sha256 about to be uploaded. That is the state a
    mid-node save leaves when the node delivered the original (nothing to fix) and the pod died before the node end."""
    st = h.state
    if (not st.baseline_scan or st.baseline_scan != h.rel(h.paths.scan(BASELINE_LABEL))
            or BASELINE_LABEL in st.pending_upload or BASELINE_LABEL in st.pending_receipt):
        return None
    res = h.load_scan(st.baseline_scan)
    if res is None or not res.complete or res.zip_sha256 != sha:
        return None
    return res


def _inventory(h: _Holder) -> str | None:
    st, c, p = h.state, h.c, h.paths
    r = max(st.round, 1)
    st.round = r
    rd = p.round_dir(r)
    rd.mkdir(parents=True, exist_ok=True)
    before = c.maven.resolve(h.project(h.before_tree(r)), rd / "work-before", **h.target_kw)
    write_json_atomic(rd / "before.json", before.to_dict())
    if not before.complete:
        return h.finish(INCOMPLETE, R_MANUAL_REVIEW, "Maven resolution incomplete: " + "; ".join(before.notes))
    scan = h.load_scan(st.final_scan or st.baseline_scan)
    if scan is None or not scan.complete:
        # 읽을 수 없거나 끝나지 않은 검사를 0건으로 보지 않는다. 빈 목록으로 계획하지 않는다
        return h.finish(INCOMPLETE, R_SCAN_INCOMPLETE, "the scan this round plans from is missing or incomplete")
    # ignore_actions 코드(기본값 없음, 예: PC 패치 완료, IG 조치 예외)의 항목은 인벤토리와 계획에 넘기지 않는다
    findings, _ignored = h.split(scan)
    inv = c.inventory(h.project(p.accepted), findings, before, inhouse=inhouse_keys(h.cfg))
    write_json_atomic(rd / "inventory.json", inv.to_dict())
    if not inv.complete:
        return h.finish(INCOMPLETE, R_MANUAL_REVIEW, "inventory incomplete: " + "; ".join(inv.notes))
    h.round_state(r)
    return "plan"


def _current_policy_hash(h: _Holder) -> str:
    """현재 서버 정책의 해시를 반환한다. 계획을 다시 만들지 않고도 기존 계획이 낡았는지 판단할 때 쓴다.
    계획 함수에 ``policy_hash(cfg)``가 있으면 그 값이다. 없거나 실패하면 state에 저장된 값을 반환한다."""
    fn = getattr(h.c.plan, "policy_hash", None)
    if callable(fn):
        try:
            return str(fn(h.cfg) or "")
        except Exception:  # noqa: BLE001 - 해시를 구하지 못하면 state에 저장된 값을 쓴다
            log.debug("policy hash of the plan function unavailable", exc_info=True)
    return h.state.policy_hash


def _residual_key(x: Residual) -> tuple[str, str, str]:
    return (ga_key(x.ga), version_key(x.version), x.reason)


def _merge_residuals(st: RemediationState, residuals: list[Residual]) -> None:
    """이번 라운드에서 조치하지 못한 항목을 state에 합친다. (g:a, 버전, 사유)마다 한 항목으로 둔다.
    g:a와 버전은 대소문자를 구분하지 않는다. 이미 있는 항목이면 모듈, 사내 아티팩트, 모듈별 출처를 합친다.
    경로와 IVAS 표기는 비어 있을 때만 채운다.

    Sources are unioned by (module, variant)."""
    by_key = {_residual_key(x): x for x in st.residuals}
    for x in residuals:
        key = _residual_key(x)
        old = by_key.get(key)
        if old is None:
            by_key[key] = x
            st.residuals.append(x)
            continue
        old.modules = sorted(set(old.modules or []) | set(x.modules or []))
        old.inhouse = sorted(set(old.inhouse or []) | set(x.inhouse or []))
        if not old.via and x.via:
            old.via = list(x.via)
        have = {(s.module, s.variant) for s in old.sources or []}
        extra = [s for s in x.sources or [] if (s.module, s.variant) not in have]
        if extra:
            old.sources = sorted([*(old.sources or []), *extra], key=lambda s: (s.module, s.variant))
        if not old.reported_version and x.reported_version:
            old.reported_version = x.reported_version


def _ineffective_key(p: Proposal) -> tuple[str, ...]:
    """효과가 없어 뺀 override 제안을 planner의 ``rejected`` 키로 바꾼다. 키는 (g:a, 목표 버전, 변종, pom)이다.
    같은 실행에서는 그 pom에 같은 제안을 다시 하지 않는다.

    Applies to proposals whose decision is `D_INEFFECTIVE`."""
    from .apply import override_variant
    pom = next((e.pom for e in p.edits if e.op == "insert"), "")
    return (p.ga, p.to_version, override_variant(p), pom)


def _inventory_items(h: _Holder, r: int) -> dict[tuple[str, str], Any]:
    """라운드의 인벤토리 항목을 (g:a, 버전) 비교 키로 묶은 dict를 반환한다. 파일이 없으면 빈 dict를 반환한다."""
    data = read_json(h.paths.round_dir(r) / "inventory.json")
    if not isinstance(data, dict):
        return {}
    inv = Inventory.from_dict(data)
    out: dict[tuple[str, str], Any] = {}
    for it in inv.items:
        out.setdefault((ga_key(it.ga), version_key(it.version)), it)
    return out


def _plan(h: _Holder) -> str | None:
    st, c, p = h.state, h.c, h.paths
    r = st.round
    rs = h.round_state(r)
    tree_hash = c.intake.tree_hash(p.accepted, h.paths_list)
    current = _current_policy_hash(h)
    policy_changed = bool(current and current != st.policy_hash)
    if policy_changed:
        # 서버 정책이 바뀌었으면 이 라운드의 계획을 지금 정책으로 다시 만든다
        h.journal({"event": "policy_changed", "round": r})
        st.policy_hash = current
    existing = h.load_plan(r)
    reuse = (existing is not None and existing.source_tree_hash == tree_hash and existing.policy_hash == st.policy_hash
             and all(x.source_tree_hash == tree_hash and x.policy_hash == st.policy_hash for x in existing.proposals))
    if reuse:
        plan = existing
        h.journal({"event": "plan_reused", "round": r, "proposals": len(plan.proposals)})
    else:
        rejected = {_ineffective_key(x) for k in range(1, r) if (pl := h.load_plan(k)) is not None
                    for x in pl.proposals if x.decision == D_INEFFECTIVE}
        if existing is not None:
            # 소스 해시나 정책 해시가 달라진 낡은 계획이다. 기록으로만 남기고 새로 만든다
            rejected |= {_ineffective_key(x) for x in existing.proposals if x.decision == D_INEFFECTIVE}
            stale = p.round_dir(r) / "plan.stale.json"
            write_json_atomic(stale, existing.to_dict())
            h.journal({"event": "plan_stale", "round": r})
        inv_data = read_json(p.round_dir(r) / "inventory.json")
        inv = Inventory.from_dict(inv_data) if isinstance(inv_data, dict) else Inventory(complete=False)
        plan = c.plan(h.project(p.accepted), inv, h.cfg, c.versions, round=r, source_tree_hash=tree_hash,
                      rejected=rejected)
        if not st.policy_hash and plan.policy_hash:
            st.policy_hash = plan.policy_hash
        elif plan.policy_hash != st.policy_hash:
            log.warning("planner policy hash %s differs from the run's %s", plan.policy_hash[:12], st.policy_hash[:12])
            h.journal({"event": "policy_hash_differs", "round": r})
        write_json_atomic(p.plan(r), plan.to_dict())
        h.journal({"event": "plan_written", "round": r, "proposals": len(plan.proposals),
                   "residuals": len(plan.residuals)})
    rs.plan = h.rel(p.plan(r))
    _merge_residuals(st, plan.residuals)
    if not plan.to_apply():
        if st.delivery is None:
            h.deliver_original()
        return "evaluate"
    return "apply"


def _apply(h: _Holder) -> str | None:
    st, c, p = h.state, h.c, h.paths
    r = st.round
    rs = h.round_state(r)
    plan = h.load_plan(r)
    if plan is None:
        raise RuntimeError(f"round {r} has no plan file")
    cand = p.candidate(r)
    h.unmark_candidate(r)
    _safe_rmtree(cand)
    _safe_rmtree(_applied_dir(h, r))
    _safe_rmtree(_expected_path(h, r))
    c.intake.copy_tree(p.accepted, cand, h.paths_list)
    to_apply = plan.to_apply()
    result = c.apply.apply_proposals(h.project(cand), to_apply)
    p.patch(r).parent.mkdir(parents=True, exist_ok=True)
    p.patch(r).write_text(result.patch or "", encoding="utf-8")
    write_json_atomic(p.round_dir(r) / "apply.json", result.to_dict())
    rs.applied = list(result.applied)
    by_id = {x.id: x for x in plan.proposals}
    # 건너뛴 제안은 적용되지 않았으니 직접 확인 항목으로 남긴다. 출처와 IVAS 표기는 인벤토리 항목에서 가져온다
    items = _inventory_items(h, r) if result.skipped else {}
    skipped: list[Residual] = []
    for pid, why in sorted(result.skipped.items()):
        prop = by_id.get(pid)
        it = items.get((ga_key(prop.ga), version_key(prop.from_version))) if prop is not None else None
        skipped.append(Residual(ga=prop.ga if prop else pid, version=prop.from_version if prop else "",
                                origin="proposal", reason=R_MANUAL_REVIEW, detail=f"proposal {pid} not applied: {why}",
                                finding_ids=list(prop.finding_ids) if prop else [],
                                modules=sorted(set(prop.modules) | set(prop.also_modules)) if prop else [],
                                # 제안에 출처가 없으면(이전 버전이 만든 계획) 인벤토리 항목의 출처를 쓴다
                                sources=(list(prop.sources) if prop is not None and prop.sources
                                         else list(it.sources or []) if it is not None else []),
                                reported_version=(it.reported_version or "") if it is not None else ""))
    _merge_residuals(st, skipped)
    applied = [by_id[pid] for pid in result.applied if pid in by_id]
    if not applied:
        if st.delivery is None:
            h.deliver_original()
        return "evaluate"
    violations = list(c.apply.verify_text_only(h.project(p.accepted), h.project(cand), applied, h.project_paths))
    # verify_text_only는 프로젝트 폴더만 본다. 그 밖의 파일(``__MACOSX/`` 등)은 여기서 확인한다
    outside = h.outside_paths
    if outside and c.intake.tree_hash(p.accepted, outside) != c.intake.tree_hash(cand, outside):
        violations.append("a file outside the project folder differs from the accepted tree")
    if violations:
        rs.verify_problems = violations
        h.discard_candidate(r)
        return h.finish(FAILED, R_TEXT_ONLY_VIOLATION, "; ".join(violations[:5]))
    _write_expected(h, r, _store_applied(h, r, cand, result, applied))
    h.mark_candidate(r)
    return "build"


def _applied_proposals(h: _Holder, r: int) -> list[Proposal]:
    plan = h.load_plan(r)
    rs = h.round_state(r)
    by_id = {x.id: x for x in (plan.proposals if plan else [])}
    return [by_id[pid] for pid in rs.applied if pid in by_id]


def _commit(h: _Holder, cand: Path) -> None:
    """수정본의 manifest 파일들로 새 accepted/ 폴더를 만든다.
    accepted.new에 먼저 만든 뒤 이름을 바꿔 교체한다."""
    acc = h.paths.accepted
    new, old = acc.with_name(acc.name + ".new"), acc.with_name(acc.name + ".old")
    _safe_rmtree(new)
    _safe_rmtree(old)
    h.c.intake.copy_tree(cand, new, h.paths_list)
    fsops.replace(acc, old)
    fsops.replace(new, acc)
    _safe_rmtree(old)


def _recover_accepted(paths: RunPaths) -> None:
    """두 번의 이름 바꾸기 사이에 서버가 내려갔을 때 accepted/ 폴더를 복구한다."""
    acc = paths.accepted
    new, old = acc.with_name(acc.name + ".new"), acc.with_name(acc.name + ".old")
    if not acc.exists():
        if new.exists() and old.exists():
            fsops.replace(new, acc)            # 새 폴더는 첫 이름 바꾸기 전에 다 복사했으니 완전하다
        elif old.exists():
            fsops.replace(old, acc)
    if acc.exists():
        _safe_rmtree(new)
        _safe_rmtree(old)


def _build_failed(h: _Holder, r: int, outcome: Any) -> str | None:
    """수정본 빌드가 실패했을 때 정책(``failed_candidate``)대로 끝낸다."""
    st, p = h.state, h.paths
    rs = h.round_state(r)
    if h.stop():
        h.halt(STOPPED, R_STOP_REQUESTED, "build", f"stop requested during the round {r} candidate build")
        return None
    if getattr(h.rcfg, "failed_candidate", "include") == "include":
        # 실패한 수정본도 결과 ZIP으로 내보낸다. 성공일 수 없으니 재검사하지 않는다. accepted/와 원본 ZIP은 그대로다
        try:
            kept = h.deliver_failed_candidate(r)
        except (DeliveryNotWritten, DeliveryRecordNotWritten) as e:
            # 실패한 수정본의 ZIP을 쓰지 못했거나, result.zip을 바꾼 뒤 기록을 쓰지 못한 경우다
            return h.finish(FAILED, R_INTERNAL_ERROR, f"{e} | {outcome.summary}" if outcome.summary else str(e))
        if kept is None:
            # 완성 표시가 없는 수정본은 일부만 복사됐을 수 있다. 결과 ZIP으로 내보내지 않는다
            h.discard_candidate(r)
            return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED,
                            f"{CANDIDATE_NOT_VOUCHED.format(r=r)} | {outcome.summary}")
        ids = ", ".join(rs.applied) or "-"
        msg = (f"delivery/result.zip contains the round {r} change set whose candidate build FAILED "
               f"(proposals: {ids}; build log {h.rel(p.build_log(r))}); it was not rescanned; "
               "the original is preserved as original/source.zip")
        if kept and h.kept_unconfirmed:
            # 조치 전 빌드 실패를 받아들인 실행의 이전 결과 ZIP은 재검사만 했고 빌드로 확인하지 못했다
            msg += f"; the scanned delivery of an earlier round (its build was not confirmed) is kept as {kept}"
        elif kept:
            msg += f"; the verified delivery of an earlier round is kept as {kept}"
        return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED, f"{msg} | {outcome.summary}")
    # 정책이 discard면 실패한 수정본은 결과 ZIP에 넣지 않는다. 이전 라운드에서 검증한 결과 ZIP은 남는다
    h.discard_candidate(r)
    msg = outcome.summary
    if st.delivery is not None:
        msg = (f"round {r} candidate discarded; delivery/result.zip is the verified state of round "
               f"{st.delivery.round}, without the failed change | {msg}")
    return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED, msg)


def _baseline_errors_only(h: _Holder, r: int, outcome: Any, cand: Path) -> bool:
    """조치 전 빌드도 실패한 실행에서 실패한 수정본 빌드의 오류를 조치 전 빌드와 비교한다.
    새 오류가 없으면 True를 반환한다.

    Only when the baseline build failed and that was tolerated (`state.baseline_tolerated`) and no stop is pending:
    `maven.compare_with_baseline` over the two logs (its docstring says what is compared and what it cannot show).
    The result goes to `RoundState.build_compare` / `build_tolerated` and the journal (`remediation_candidate_compared`).
    True = the round goes on (resolution check, commit, frozen delivery, rescan) although the build failed."""
    rs = h.round_state(r)
    rs.build_compare, rs.build_tolerated = {}, False
    if not h.baseline_failed_tolerated or h.stop():
        return False
    from . import maven as maven_mod
    bb = h.state.baseline_build
    cmp = maven_mod.compare_with_baseline(bb, _read_log(h, bb), h.project(h.baseline_tree),
                                          outcome, _read_log(h, outcome), h.project(cand))
    rs.build_compare, rs.build_tolerated = cmp, bool(cmp.get("same"))
    h.journal({"event": "remediation_candidate_compared", "round": r, "same": rs.build_tolerated,
               "baseline_errors": cmp.get("baseline_errors"), "candidate_errors": cmp.get("candidate_errors"),
               "new": list(cmp.get("new") or [])[:5], "why": cmp.get("why", "")})
    if rs.build_tolerated:
        log.info("remediation round %s: the candidate build failed with the baseline build's errors only (%s of %s); "
                 "going on without a build confirmation", r, cmp.get("candidate_errors"), cmp.get("baseline_errors"))
    return rs.build_tolerated


def _new_errors_text(cmp: dict[str, Any]) -> str:
    """수정본 빌드에 새로 생긴 오류를 알리는 영어 문장을 만든다. 비교하지 않았으면 빈 문자열을 반환한다."""
    if not cmp:
        return ""
    new = list(cmp.get("new") or [])
    if new:
        return (f"the candidate build failed with {len(new)} error(s) the baseline build did not have: "
                + "; ".join(new[:3]))
    return f"the candidate build failed differently from the baseline build: {cmp.get('why') or 'not comparable'}"


def _candidate_failed(h: _Holder, r: int, outcome: Any) -> str | None:
    """수정본 빌드 실패를 정책대로 끝낸다. 조치 전 빌드와 비교한 결과가 있으면 그 문장을 메시지 앞에 붙인다."""
    nxt = _build_failed(h, r, outcome)
    extra = _new_errors_text(h.round_state(r).build_compare)
    if extra and nxt == "report":
        h.state.message = f"{extra} | {h.state.message}" if h.state.message else extra
    return nxt


def _verify(h: _Holder, r: int, cand: Path, proposals: list[Proposal]) -> list[str]:
    """수정본의 의존성을 다시 해석해서 제안대로 바뀌었는지 확인한다. 검증 결과 줄 목록을 반환한다."""
    c, p = h.c, h.paths
    after = c.maven.resolve(h.project(cand), p.round_dir(r) / "work-after", **h.target_kw)
    write_json_atomic(p.resolved(r), after.to_dict())
    packaged = list(c.maven.packaged(h.project(cand), **h.target_kw))
    before_data = read_json(p.round_dir(r) / "before.json")
    before = ResolvedModel.from_dict(before_data) if isinstance(before_data, dict) else ResolvedModel(complete=False)
    return list(c.apply.verify_resolution(proposals, before, after, packaged))


class CandidateNotRestored(Exception):
    """빌드 뒤 수정본의 manifest 경로 일부를 기대하는 바이트로 되돌리지 못했을 때 던지는 예외.

    Raised by `_sync_candidate` when `sync_tree_table` could not put every path back; `_build` turns
    it into FAILED `internal_error` (`CANDIDATE_NOT_RESTORED`) without committing or delivering the candidate."""

    def __init__(self, round: int, failed: list[tuple[str, str, str]]):
        self.round, self.failed = round, list(failed)
        first = self.failed[0] if self.failed else ("-", "Error", "")
        super().__init__(CANDIDATE_NOT_RESTORED.format(r=round, n=len(self.failed), p=first[0], t=first[1]))


def _build(h: _Holder) -> str | None:
    """수정본 빌드 노드. 빌드 뒤 수정본을 기대하는 상태로 되돌리지 못하면 FAILED로 끝낸다.
    이때 이 라운드는 확정하지도 내보내지도 않는다.

    `_build_round`, with `CandidateNotRestored` → `discard_candidate` (an earlier verified delivery stays; a frozen
    failed candidate is never made because the sync before freezing raised first) and FAILED `internal_error` with
    `CANDIDATE_NOT_RESTORED`; journal `candidate_not_restored`."""
    try:
        return _build_round(h)
    except CandidateNotRestored as e:
        r = e.round
        log.error("remediation round %s: %s", r, e)
        h.journal({"event": "candidate_not_restored", "round": r, "count": len(e.failed),
                   "paths": [f[0] for f in e.failed[:5]], "errors": [f[1] for f in e.failed[:5]]})
        h.discard_candidate(r)
        return h.finish(FAILED, R_INTERNAL_ERROR, str(e))


def _build_round(h: _Holder) -> str | None:
    st, c, p = h.state, h.c, h.paths
    r = st.round
    rs = h.round_state(r)
    cand = p.candidate(r)
    # 수정 루프 도중에 파드가 끝났으면 무엇을 하든 먼저 그 LLM 사용량을 기록한다
    _settle_leftover_loop(h, r)
    why = h.vouch_problem(r)
    if why:
        # 복사나 적용 도중에 파드가 끝났거나 이전 버전이 만든 수정본이다. 완전한지 알 수 없으니 accepted/에서 다시 만든다
        h.journal({"event": "candidate_rebuilt", "round": r, "why": why})
        log.warning("remediation round %s: the candidate is made again from accepted/ (%s)", r, why)
        return "apply"
    outcome, nxt = _run_build(h, r, cand)
    if outcome is None:
        return nxt
    applied = _applied_proposals(h, r)
    lines = _verify(h, r, cand, applied)
    rs.verify_problems = lines
    problems = [ln for ln in lines if not ln.startswith("info:")]
    if problems:
        split = getattr(c.apply, "split_ineffective", None)
        drop, other = split(applied, lines) if callable(split) else ([], problems)
        plan = h.load_plan(r)
        dropped = [x.id for x in (plan.proposals if plan else []) if x.decision == D_INEFFECTIVE]
        if drop and not other and dropped:
            # 중단 뒤 재개로 이미 한 번 빼고 다시 만든 수정본이다. 중단이 없었을 때처럼 두 번째 문제는 실패로 끝낸다
            h.discard_candidate(r)
            return h.finish(FAILED, R_RESOLUTION_MISMATCH,
                            "; ".join(problems[:5]) + f" | after removing the ineffective override entr(ies) "
                            f"{', '.join(dropped)}")
        if drop and not other:
            return _drop_ineffective(h, r, applied, drop, problems)
        h.discard_candidate(r)
        return h.finish(FAILED, R_RESOLUTION_MISMATCH, "; ".join(problems[:5]))
    return _commit_round(h, r, cand)


def _package(h: _Holder, r: int, cand: Path, log_path: Path, *, confirming: bool = False) -> Any:
    """수정본을 빌드하고 빌드 결과를 반환한다. 빌드가 지우거나 바꾼 manifest 파일은 기대하는 바이트로 되돌린다.
    빌드 결과는 되돌리기 전에 라운드에 기록한다. 되돌리기에 실패해도 빌드 결과는 남는다.

    The one place a candidate is built (first build, confirming build, the build after a dropped override): the
    build, then `_sync_candidate` (v2.1z-bk), so verify, commit and freezing always see accepted + the applied edits
    (+ the reported fix). The outcome is recorded in the round BEFORE the sync, which may raise
    `CandidateNotRestored`: a first build sets `rs.build` (and clears `build_compare`, `build_tolerated`,
    `fix_first_build`); a confirming build that passed sets `rs.build` (a failed one leaves the first build there)."""
    outcome = h.build_outcome(h.c.maven.package(h.project(cand), log_path, h.stop, **h.target_kw), log_path)
    rs = h.round_state(r)
    if not confirming:
        rs.build, rs.build_compare, rs.build_tolerated = outcome, {}, False
        rs.fix_first_build = None
    elif outcome.ok:
        rs.build = outcome
    _sync_candidate(h, r, cand)
    return outcome


APPLIED_DIR = "applied"                 # 제안을 적용한 직후의 파일 바이트를 두는 폴더(rounds/<r>/ 아래)


def _applied_dir(h: _Holder, r: int) -> Path:
    return h.paths.round_dir(r) / APPLIED_DIR


def _store_applied(h: _Holder, r: int, cand: Path, result: Any,
                   proposals: list[Proposal]) -> dict[str, tuple[int, str, bool]]:
    """제안이 고친 파일의 적용 직후 바이트를 ``rounds/<r>/applied/<경로>``에 둔다. 이름이 pom.xml이 아닌 pom도 들어간다.

    v2.1z-bk: at the end of `apply` (and of the re-apply after a dropped override) the files the proposals edited —
    `ApplyResult.changed_files` and every edit's `pom`, whatever its name — are stored with the candidate's bytes after
    apply, at their manifest path (relative to the ZIP top). The folder is made anew each time, so a remade candidate
    keeps no file of a dropped proposal. Returns `{path: (size, sha256, exec)}` of the stored files (hashed from the
    bytes already in memory, no extra read)."""
    d = _applied_dir(h, r)
    _safe_rmtree(d)
    pr = h.project_root
    prefix = "" if pr in ("", ".") else pr.rstrip("/") + "/"
    edited = set(getattr(result, "changed_files", None) or [])
    edited |= {e.pom for x in proposals for e in (x.edits or []) if getattr(e, "pom", "")}
    listed = set(h.paths_list)
    stored: dict[str, tuple[int, str, bool]] = {}
    d.mkdir(parents=True, exist_ok=True)
    for prel in sorted(edited):
        rel = prefix + str(prel).replace("\\", "/").strip("/")
        src = cand / rel
        if rel not in listed or ".." in rel.split("/") or src.is_symlink() or not src.is_file():
            continue
        dst = d / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dst.write_bytes(data)
        exec_bit = bool(os.lstat(src).st_mode & 0o111)
        os.chmod(dst, 0o755 if exec_bit else 0o644)
        stored[rel] = (len(data), hashlib.sha256(data).hexdigest(), exec_bit)
    return stored


def _expected_path(h: _Holder, r: int) -> Path:
    return h.paths.round_dir(r) / EXPECTED_FILE


def _prefix(h: _Holder) -> str:
    pr = h.project_root
    return "" if pr in ("", ".") else pr.rstrip("/") + "/"


Row = tuple  # (크기, sha256, 실행 비트)


def _rows(data: Any) -> dict[str, Row] | None:
    """``expected.json``의 ``files``를 {경로: (크기, sha256, 실행 비트)}로 바꾼다.
    형식이 맞지 않거나 실행 비트가 없으면 None을 반환한다.

    Rows are `[size, sha256, exec]`; a table without the exec bit is not used: the candidate is applied again
    and the table rebuilt."""
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    out: dict[str, Row] = {}
    try:
        for k, v in data["files"].items():
            if not (isinstance(v, (list, tuple)) and len(v) == 3 and isinstance(v[2], bool)):
                return None
            out[str(k)] = (int(v[0]), str(v[1]), bool(v[2]))
    except (TypeError, ValueError):
        return None
    return out


def _with_rows(base: dict[str, Row], extra: dict[str, Row]) -> dict[str, Row]:
    """``base``에 ``extra``의 행을 넣는다. 실행 비트가 None인 행은 ``base``의 실행 비트를 이어받는다."""
    out = dict(base)
    for k, v in extra.items():
        exec_bit = v[2] if len(v) > 2 and v[2] is not None else (base[k][2] if k in base and len(base[k]) > 2 else False)
        out[k] = (v[0], v[1], exec_bit)
    return out


def _fix_rows(h: _Holder, fix: FixResult | None) -> dict[str, Row]:
    """수정 루프가 고친 파일(``fixloop/fixed/``)의 (크기, sha256, None)을 manifest 경로별로 반환한다.
    덮어써도 수정본 파일의 권한은 그대로다. 그래서 실행 비트는 None으로 두고 ``_with_rows``에서 원래 행의 값을 쓴다."""
    if fix is None or not fix.fixed or not fix.changed or not fix.fixed_dir:
        return {}
    root = h.paths.root / fix.fixed_dir
    out: dict[str, Row] = {}
    for x in fix.changed:
        f = root / str(x).strip("/")
        if f.is_file() and not f.is_symlink():
            out[_prefix(h) + str(x).strip("/")] = (f.stat().st_size, sha256_file(f), None)
    return out


def _accepted_table(h: _Holder, r: int) -> dict[str, Row]:
    """accepted/의 파일 표(manifest 경로별 크기, sha256, 실행 비트)를 만든다. 가능하면 accepted/를 다시 읽지 않는다.

    Candidates, each accepted only when its `intake.table_hash` equals the round plan's `source_tree_hash` (the tree
    hash `_plan` computed from accepted/ with its one walk of the round): the expected table of the last committed
    round (`rounds/<k>/expected.json` with that round's fixed files substituted, since a round commits its synced
    candidate), then `original/manifest.json` (size + sha256 of the upload; the exec bit then comes from an lstat of
    each accepted/ file, no read). When neither matches (a round committed by an earlier version, or a table without
    the exec bit), accepted/ is hashed once (`intake.file_table`)."""
    from .intake import file_table, table_hash
    paths = h.paths_list
    plan = h.load_plan(r)
    want = plan.source_tree_hash if plan is not None else ""
    tries: list[tuple[str, dict[str, Row]]] = []
    committed = [x for x in h.state.rounds if x.committed and x.round < r]
    if committed:
        last = max(committed, key=lambda x: x.round)
        rows = _rows(read_json(_expected_path(h, last.round)))
        if rows is not None:
            tries.append((f"round {last.round}", _with_rows(rows, _fix_rows(h, last.fix))))
    man = read_json(h.paths.manifest)
    if isinstance(man, dict) and isinstance(man.get("entries"), list):
        rows = {}
        for e in man["entries"]:
            if isinstance(e, dict) and "path" in e and "size" in e and "sha256" in e:
                rows[str(e["path"])] = (int(e["size"]), str(e["sha256"]), None)
        tries.append(("manifest", rows))
    listed = set(paths)
    for name, rows in tries:
        if want and table_hash(rows, paths) == want:
            log.debug("remediation round %s: the expected table starts from the %s table", r, name)
            out: dict[str, Row] = {}
            for k, v in rows.items():
                if k not in listed:
                    continue
                exec_bit = v[2]
                if exec_bit is None:
                    # manifest에는 실행 비트가 없다. accepted/의 권한만 읽고 내용은 읽지 않는다
                    try:
                        exec_bit = bool(os.lstat(h.paths.accepted / k).st_mode & 0o111)
                    except OSError:
                        exec_bit = False
                out[k] = (v[0], v[1], exec_bit)
            return out
    log.info("remediation round %s: no recorded table matches accepted/; hashing it once", r)
    return file_table(h.paths.accepted, paths)


def _write_expected(h: _Holder, r: int, stored: dict[str, Row]) -> None:
    """라운드 ``r``의 기대 값 표와 적용한 파일 목록을 ``expected.json``에 기록한다.
    표는 manifest 경로별 크기, sha256, 실행 비트다.

    `rounds/<r>/expected.json` = `{schema: 2, round, applied, applied_files, files: {path: [size, sha256, exec]}}`: accepted/
    (`_accepted_table`) with the files stored under `applied/` substituted. Written after `applied/` and before
    `mark_candidate`, whose marker carries this file's sha256: together they are the proof that the candidate's
    applied files are stored completely."""
    rs = h.round_state(r)
    files = _with_rows(_accepted_table(h, r), stored)
    write_json_atomic(_expected_path(h, r), {
        "schema": 2, "round": r, "applied": list(rs.applied), "applied_files": sorted(stored),
        "files": {k: [v[0], v[1], bool(v[2])] for k, v in sorted(files.items())}})


def _expected_table(h: _Holder, r: int, *, with_fix: bool = True) -> dict[str, Row] | None:
    """라운드 ``r``의 기대 값 표(크기, sha256, 실행 비트)를 반환한다. 표가 없으면 None을 반환한다.
    ``with_fix``면 수정 루프가 고친 파일의 값으로 바꿔 넣는다."""
    rows = _rows(read_json(_expected_path(h, r)))
    if rows is None:
        return None
    if with_fix:
        rows = _with_rows(rows, _fix_rows(h, h.round_state(r).fix))
    return rows


def _expected(h: _Holder, r: int, *, with_fix: bool = True) -> Callable[[str], Path]:
    """manifest 경로를 받아 수정본이 가져야 할 바이트가 든 파일의 경로를 반환하는 함수를 만든다.

    Lookup order: `fixloop/fixed/<path>` when the path is in `rs.fix.changed` of a fixed fix (and `with_fix`);
    else `rounds/<r>/applied/<path>` when stored (the `applied_files` of `expected.json`, or a walk of
    `applied/` without it); else `accepted/<path>`."""
    rs = h.round_state(r)
    acc, ad = h.paths.accepted, _applied_dir(h, r)
    prefix = _prefix(h)
    fixed: dict[str, Path] = {}
    fix = rs.fix
    if with_fix and fix is not None and fix.fixed and fix.changed and fix.fixed_dir:
        root = h.paths.root / fix.fixed_dir
        fixed = {prefix + str(x).strip("/"): root / str(x).strip("/") for x in fix.changed}
    applied: set[str] = set()
    rec = read_json(_expected_path(h, r))
    if isinstance(rec, dict) and isinstance(rec.get("applied_files"), list):
        applied = {str(x) for x in rec["applied_files"]}
    elif ad.is_dir():
        for dirpath, _dirs, files in os.walk(ad):
            base = Path(dirpath).relative_to(ad).as_posix()
            applied |= {f if base == "." else f"{base}/{f}" for f in files}

    def expected(rel: str) -> Path:
        if rel in fixed:
            return fixed[rel]
        return ad / rel if rel in applied else acc / rel
    return expected


def _sync_candidate(h: _Holder, r: int, cand: Path, *, with_fix: bool = True,
                    record: bool = True) -> tuple[list[str], list[str]]:
    """수정본의 manifest 파일을 기대하는 바이트로 맞추고 (없던 경로, 바뀐 경로)를 반환한다.
    ``record``면 되돌린 경로를 빌드가 바꾼 파일로 기록한다. 되돌리지 못한 경로가 있으면 ``CandidateNotRestored``를 던진다.

    With `rounds/<r>/expected.json` (always there for a vouched candidate): `intake.sync_tree_table` against that
    table (+ the fix's files), so each candidate file is read at most once and an expected file is opened only for a
    path that differs; a missing or unreadable table (never the case for a vouched candidate: every caller builds or
    freezes only after `vouch_problem` or `_write_expected`) counts as a path that could not be put back. A missing
    path is restored, a differing path overwritten. With `record` (after a build, before committing, before freezing
    a failed candidate) the paths go into `RoundState.build_changed` (sorted union) and the journal gets `build_side_changes` (`round`, `missing`,
    `changed` counts, the first 5 changed `paths`; `missing_paths` too) and, for restored files,
    `manifest_files_restored`. Every path is tried; when any could not be put back the paths that were are still
    recorded, the journal gets `candidate_sync_failed` (`round`, `count`, `paths` = first 5, `errors` = their error
    types) and `CandidateNotRestored` is raised: the round must neither commit nor deliver this candidate."""
    from .intake import SyncFailed
    rs = h.round_state(r)
    failed: list[tuple[str, str, str]] = []
    table = _expected_table(h, r, with_fix=with_fix)
    try:
        if table is None:
            # 완성 표시가 있는 수정본에는 표가 늘 있다. 표가 없으면 맞췄다고 할 수 없으니 되돌리지 못한 것으로 본다
            raise FileNotFoundError(f"the expected table of round {r} is missing")
        missing, changed = h.c.intake.sync_tree_table(cand, _expected(h, r, with_fix=with_fix), table,
                                                      h.paths_list)
        missing, changed = list(missing or []), list(changed or [])
    except SyncFailed as e:
        missing, changed, failed = list(e.missing), list(e.changed), list(e.failed)
    except Exception as e:  # noqa: BLE001 - 어느 경로에서 멈췄는지 모르는 실패도 되돌리지 못한 것으로 본다
        missing, changed, failed = [], [], [("-", type(e).__name__, "")]
    if record and (missing or changed):
        rs.build_changed = sorted(set(rs.build_changed) | set(missing) | set(changed))
        log.warning("remediation round %s: the build removed %d and changed %d file(s) of the upload; they were put "
                    "back as the remediation left them (%s)", r, len(missing), len(changed),
                    ", ".join([*changed, *missing][:5]))
        h.journal({"event": "build_side_changes", "round": r, "missing": len(missing), "changed": len(changed),
                   "paths": changed[:5], "missing_paths": missing[:5]})
        if missing:
            h.journal({"event": "manifest_files_restored", "round": r, "count": len(missing), "paths": missing[:5]})
    if failed:
        log.error("remediation round %s: %d path(s) of the candidate could not be put back (%s)", r, len(failed),
                  ", ".join(f"{f[0]}: {f[1]}" for f in failed[:5]))
        h.journal({"event": "candidate_sync_failed", "round": r, "count": len(failed),
                   "paths": [f[0] for f in failed[:5]], "errors": [f[1] for f in failed[:5]]})
        raise CandidateNotRestored(r, failed)
    return missing, changed


def _run_build(h: _Holder, r: int, cand: Path) -> tuple[Any | None, str | None]:
    """수정본을 빌드한다. 실패하면 빌드 오류 수정 루프와 확인 빌드까지 실행한다.
    라운드를 이어 가면 (빌드 결과, None)을, 여기서 끝내면 (None, 다음 노드)를 반환한다.

    `_build` re-entry rules (v2.1z-bj): `rs.fix` is never cleared here. A recorded fixed result → overlay
    (idempotent) and the confirming build (`_confirm_fix`); a recorded loop that ran and did not fix → the
    build runs and, when it fails, the loop is not run again; a recorded loop that never started (stop / time)
    is forgotten and may start now; a recorded loop that ran and ended `stop requested` is not a verdict: it
    is moved to `rs.fix_discarded` (reason kept, usage still counted; journal `remediation_fix_discarded`) and
    the loop may run again (`time budget used up` and every other not-fixed reason stay final); no record →
    first build, the loop when `_fix_wanted`, confirming build. A leftover `fixloop/` of a killed loop is
    settled first (`_settle_leftover_loop`), whatever happens next.

    Only a loop that did not start because of a stop or the time budget is forgotten (a folder setup that
    failed stays the round's verdict, as in the uninterrupted run). A round whose last discarded fix ended the
    round (`FixResult.ended_round`: a fix the loop had just returned that could not be applied) does not run
    the loop again at the node's entry (a kill right after the discard's state save re-enters here; the
    uninterrupted run ended the round as not fixed). The calls right after a stored fix of an earlier entry
    was found not usable on this one discard it with `ended_round` False, so the loop may run again."""
    rs = h.round_state(r)
    _settle_leftover_loop(h, r)
    fix = rs.fix
    if fix is not None and not fix.fixed and not fix.attempted and fix.reason in (R_FIX_STOP, R_FIX_TIME):
        rs.fix = fix = None
    if fix is not None and fix.attempted and not fix.fixed and fix.reason == R_FIX_STOP:
        # 중지 요청으로 멈춘 루프는 결과로 보지 않는다. 버린 결과로 옮기고 다시 실행할 수 있게 한다. 사용량은 계속 센다
        rs.fix_discarded = [*rs.fix_discarded, FixResult.from_dict(fix.to_dict())]
        rs.fix = fix = None
        h.save()
        h.journal({"event": "remediation_fix_discarded", "round": r, "reason": R_FIX_STOP, "changed": 0})
        log.info("remediation round %s: the build fix loop stopped on request earlier; it may run again", r)
    if fix is not None and fix.fixed:
        return _confirm_fix(h, r, cand, fresh=False)
    outcome = _package(h, r, cand, h.paths.build_log(r))
    if outcome.ok or _baseline_errors_only(h, r, outcome, cand):
        return outcome, None
    unusable = fix is None and bool(rs.fix_discarded) and rs.fix_discarded[-1].ended_round
    if fix is None and not unusable and _fix_wanted(h, r):
        _run_fix(h, r, outcome, cand)
    if rs.fix is None or not rs.fix.fixed:
        nxt = _fix_not_applied(h, r, outcome)
        if unusable and rs.fix is None and nxt == "report":
            # 앞선 진입에서 고친 내용을 쓸 수 없어 버린 라운드다. 중단이 없었을 때와 같은 문장으로 끝낸다
            suffix = f"automatic build fix: {R_FIX_UNUSABLE}"
            h.state.message = f"{h.state.message} | {suffix}" if h.state.message else suffix
        return None, nxt
    return _confirm_fix(h, r, cand, fresh=True)


FIX_LOG = "build-fixed.log"             # 수정 루프가 고친 수정본을 다시 빌드한 로그(rounds/<r>/ 아래)
FIX_DIR = "fixloop"                     # 수정 루프의 저널과 로그를 두는 폴더(rounds/<r>/ 아래)
R_FIX_CONFIRM_FAILED = "error: confirming build failed"


R_FIX_DISCARDED = "discarded: the candidate was remade"
R_FIX_UNUSABLE = "discarded: the stored fix is not usable"
LOOP_FILE = "loop.json"                 # 루프를 시작하기 전에 fixloop/에 쓰는 표시 파일
INTERRUPTED_DIR = "fixloop-interrupted-{n}"
R_FIX_STOP = "stop requested"
FIX_NO_ATTEMPT = "automatic build fix made no attempt"   # 루프는 시작했지만 소스 수정을 한 번도 시도하지 않았다
R_FIX_TIME = "time budget used up"
FIX_STOPPED = "stop requested during the round {r} build fix"   # 수정 루프 도중에 중지 요청을 받았을 때의 STOPPED 문장
FIX_RESERVE_MIN_SECONDS = 300.0         # 수정 루프 뒤 실패한 수정본을 내보낼 시간으로 남겨 두는 최소 초


def fix_made_no_attempt(fix: Any) -> bool:
    """수정 루프가 시작은 했지만 소스 수정을 한 번도 시도하지 못하고 끝난 결과인지 확인한다.

    `attempted` and not `fixed` with `attempts == 0`, except a result that had been a fix (a failed confirming build,
    a discarded fix: their reason says what happened to the fix)."""
    if fix is None or not getattr(fix, "attempted", False) or getattr(fix, "fixed", False):
        return False
    reason = str(getattr(fix, "reason", "") or "")
    if reason == R_FIX_CONFIRM_FAILED or reason.startswith("discarded:") or getattr(fix, "discarded_changed", None):
        return False
    return not int(getattr(fix, "attempts", 0) or 0)


def _fix_wanted(h: _Holder, r: int) -> bool:
    """실패한 수정본 빌드에 빌드 오류 수정 루프를 실행할지 확인한다.
    스위치가 켜져 있고, 수정 루프 구현이 있고, 조치 전 빌드가 통과했고, 적용한 제안이 있어야 한다.
    중지 요청과 남은 시간은 ``_run_fix``가 확인한다. 그때는 시작하지 않은 결과로 남긴다."""
    st = h.state
    if getattr(h.c, "fixer", None) is None or not getattr(h.rcfg, "fix_loop", False):
        return False
    bb = st.baseline_build
    if h.baseline_failed_tolerated or bb is None or not bb.ok:
        return False
    return bool(h.round_state(r).applied)


def _fix_journal_end(h: _Holder, r: int, res: FixResult) -> None:
    h.journal({"event": "remediation_fix_loop", "round": r, "phase": "end", "attempted": res.attempted,
               "fixed": res.fixed, "iterations": res.iterations, "llm_calls": res.llm_calls,
               "changed": len(res.changed), "reason": res.reason})


def fix_loop_seconds(h: _Holder, first: Any) -> float:
    """수정 루프에 줄 시간(초)을 계산한다. 실패한 수정본을 내보낼 시간은 남겨 둔다.
    ``fix_loop_max_minutes``는 넘지 않는다.

    v2.1z-bj: `min(fix_loop_max_minutes × 60, time_left − reserve)`, `reserve = max(300, 2 × the round's first
    candidate build, 2 × the baseline build)` in seconds; `time_left` None → the cap alone."""
    cap = float(getattr(h.rcfg, "fix_loop_max_minutes", 30) or 30) * 60.0
    left = h.time_left()
    if left is None:
        return cap
    bb = h.state.baseline_build
    reserve = max(FIX_RESERVE_MIN_SECONDS, 2.0 * float(getattr(first, "seconds", 0.0) or 0.0),
                  2.0 * float(getattr(bb, "seconds", 0.0) or 0.0) if bb is not None else 0.0)
    return min(cap, left - reserve)


def _settle_old_loop(h: _Holder, r: int, work: Path, *, remove_recorded: bool = True) -> None:
    """라운드에 남은 이전 ``fixloop/`` 폴더를 정리한다.
    - 정상으로 기록된 루프의 폴더는 ``remove_recorded``일 때 지운다
    - 중간에 끝난 루프는 LLM 사용량을 ``rs.fix_discarded``에 넣고 state를 저장한다. 그다음 폴더 이름을 바꿔 남긴다

    The folder's `loop.json` names the loop; a loop whose token is on no recorded `FixResult` of the round
    (`rs.fix`, `rs.fix_discarded`) was killed. A folder without the file counts as recorded when the round
    records an attempted loop without a token (written by an earlier version); otherwise it is given a fresh
    token in its `loop.json` first. Killed → `fixer.interrupted_result` recovers its usage from the loop's own
    journal, the result (with the folder's token as `loop_id`) is appended to `rs.fix_discarded` and SAVED,
    journal `remediation_fix_interrupted`, and only then is the folder renamed `fixloop-interrupted-<n>/` (a
    failed rename removes it). A folder whose token is already recorded on an interrupted result (a kill
    between that save and the rename) is only renamed, so the usage is counted once whatever the kill point. A
    folder of a loop recorded normally is removed only with `remove_recorded` (before a new loop starts).
    After the rename the work copy `tree/` is removed (`_drop_work_copy`); the journal and the logs stay."""
    from .fixer import R_INTERRUPTED, interrupted_result
    if not (work.exists() or work.is_symlink()):
        return
    if work.is_symlink() or not work.is_dir():
        if remove_recorded:
            _safe_rmtree(work)
        return
    rs = h.round_state(r)
    data = read_json(work / LOOP_FILE)
    token = str(data.get("loop") or "") if isinstance(data, dict) else ""
    recorded = [f for f in [rs.fix, *rs.fix_discarded] if f is not None]
    if not token:
        if any(f.attempted and not f.loop_id for f in recorded):
            if remove_recorded:
                _safe_rmtree(work)
            return
        token = f"{h.state.id}-fix-r{r}-interrupted-{os.urandom(4).hex()}"
        try:
            write_json_atomic(work / LOOP_FILE, {"loop": token, "round": r})
        except OSError:
            token = ""
    hit = next((f for f in recorded if token and f.loop_id == token), None)
    if hit is not None and not str(hit.reason or "").startswith(R_INTERRUPTED):
        if remove_recorded:
            _safe_rmtree(work)
        return
    rd = h.paths.round_dir(r)
    n = 1
    while (rd / INTERRUPTED_DIR.format(n=n)).exists():
        n += 1
    dst = rd / INTERRUPTED_DIR.format(n=n)
    if hit is None:
        res = interrupted_result(work, h.cfg)
        res.loop_id = token
        rs.fix_discarded = [*rs.fix_discarded, res]
        h.save()
        log.warning("remediation round %s: a fix loop that the run did not finish was found (%d LLM call(s)%s); kept as "
                    "%s", r, res.llm_calls, ", usage unknown" if res.reason.endswith(")") else "", h.rel(dst))
        h.journal({"event": "remediation_fix_interrupted", "round": r, "folder": h.rel(dst),
                   "llm_calls": res.llm_calls, "reason": res.reason})
    try:
        fsops.replace(work, dst)
    except OSError as e:
        log.warning("remediation round %s: the interrupted fix loop folder was not renamed: %s", r, e)
        try:
            _safe_rmtree(work)
        except OSError:
            log.warning("remediation round %s: the interrupted fix loop folder was not removed", r, exc_info=True)
        return
    _drop_work_copy(h, r, dst)


def _drop_work_copy(h: _Holder, r: int, folder: Path) -> None:
    """사용량을 기록하고 저장한 중단된 루프 폴더에서 작업 복사본(``tree/``)을 지운다. 저널과 로그는 남긴다.

    The work copy of an interrupted loop (`fixloop-interrupted-<n>/tree`, with its own `.git` and `target/`)
    is removed once the loop's usage is recorded and saved; best effort, WARNING on failure."""
    tree = folder / "tree"
    if not (tree.exists() or tree.is_symlink()):
        return
    try:
        _safe_rmtree(tree)
    except OSError as e:
        log.warning("remediation round %s: the work copy of the interrupted fix loop was not removed (%s): %s", r,
                    h.rel(tree), type(e).__name__)


def _settle_leftover_loop(h: _Holder, r: int) -> None:
    """라운드에 중간에 끝난 수정 루프의 폴더가 남아 있으면 그 사용량을 기록한다.
    정상으로 기록된 루프의 폴더는 그대로 둔다.

    Called at the start of the build node, of `_run_build` and of the report node, whatever happens next (the build
    passes, a stop is pending, no time, the loop switched off): `_settle_old_loop(remove_recorded=False)`."""
    work = h.paths.round_dir(r) / FIX_DIR
    if work.exists() or work.is_symlink():
        _settle_old_loop(h, r, work, remove_recorded=False)


def _run_fix(h: _Holder, r: int, outcome: Any, cand: Path) -> None:
    """실패한 수정본 빌드에 빌드 오류 수정 루프를 실행한다. 결과는 ``rs.fix``에 넣고 바로 state를 저장한다.
    고쳤으면 처음 실패한 빌드를 ``rs.fix_first_build``로 남긴다. 수정본은 건드리지 않는다.

    Not started (`attempted: false`) when less than `fixer.MIN_LOOP_SECONDS` of `fix_loop_seconds` is left
    (`time budget used up`) or a stop is pending (`stop requested`). An older `fixloop/` of the round is settled
    first (`_settle_old_loop`). The state is saved right after the loop returns, before anything touches the
    candidate, together with `rs.fix.tree_hash`. A fixer that raises (a programming error) is a not-fixed
    result `error: <Type>: <msg>`; the candidate was not touched, so it is delivered like any other failed one. Its
    usage is read from the loop's own journal (`fixer.usage_from_folder`; unreadable → `usage_unknown`)."""
    from .fixer import MIN_LOOP_SECONDS
    st, c, p = h.state, h.c, h.paths
    rs = h.round_state(r)
    rd = p.round_dir(r)
    work = rd / FIX_DIR
    seconds = fix_loop_seconds(h, outcome)
    if seconds < MIN_LOOP_SECONDS or h.stop():
        rs.fix = FixResult(attempted=False, reason=R_FIX_TIME if seconds < MIN_LOOP_SECONDS else R_FIX_STOP)
        log.info("remediation round %s: the build fix loop was not started (%s)", r, rs.fix.reason)
        h.journal({"event": "remediation_fix_loop", "round": r, "phase": "not_started", "reason": rs.fix.reason})
        h.save()
        return
    token = f"{st.id}-fix-r{r}-{os.urandom(4).hex()}"
    try:
        if work.exists() or work.is_symlink():
            _settle_old_loop(h, r, work)
        _safe_rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        write_json_atomic(work / LOOP_FILE, {"loop": token, "round": r})
    except Exception as e:  # noqa: BLE001 - 폴더를 준비하지 못하면 루프를 시작하지 않고 고치지 못한 것으로 이어 간다
        # 수정본은 건드리지 않았다. 실패한 수정본을 그대로 내보낸다
        log.warning("remediation round %s: the fix loop folder could not be prepared: %s", r, type(e).__name__,
                    exc_info=True)
        rs.fix = FixResult(attempted=False, fixed=False,
                           reason=scrub_message(f"error: {type(e).__name__}: {e}", p.root, 500))
        h.save()
        h.journal({"event": "remediation_fix_loop", "round": r, "phase": "not_started", "reason": rs.fix.reason})
        return
    deadline = time.monotonic() + seconds
    h.journal({"event": "remediation_fix_loop", "round": r, "phase": "start"})
    log.info("remediation round %s: the candidate build failed; running the build fix loop (%.0f s at most)", r,
             seconds)
    try:
        res = c.fixer.run(h.cfg, h.project(cand), work, run_id=f"{st.id}-fix-r{r}",
                          targets=list(getattr(st, "build_targets", None) or []),
                          applied=_applied_proposals(h, r), manifest_paths=h.project_paths, first_outcome=outcome,
                          stop=h.stop, deadline=deadline, journal=h.journal)
    except Exception as e:  # noqa: BLE001 - 수정 루프는 예외를 던지지 않는 계약이지만, 던져도 조치 실행은 계속한다
        # 수정본은 읽기만 했으니 완성 표시는 그대로 둔다. 고치지 못한 다른 경우처럼 실패한 수정본을 내보낸다
        log.warning("remediation round %s: the fix loop raised %s", r, type(e).__name__, exc_info=True)
        rs.fix = FixResult(attempted=True, reason=scrub_message(f"error: {type(e).__name__}: {e}", p.root, 500),
                           loop_id=token)
        # 루프가 저널에 남긴 사용량을 읽는다. 읽지 못하면 모르는 것으로 표시한다(0회로 세지 않는다)
        from .fixer import usage_from_folder
        usage_from_folder(rs.fix, work, h.cfg)
        h.save()
        _fix_journal_end(h, r, rs.fix)
        return
    if res.patch:
        res.patch = h.rel(Path(res.patch))
    if res.fixed_dir:
        res.fixed_dir = h.rel(Path(res.fixed_dir))
    if not res.fixed:
        res.changed, res.patch, res.fixed_dir, res.retry_passed = [], "", "", False
    res.loop_id = token
    res.tree_hash = ""
    if res.fixed and res.changed:
        res.tree_hash = _fix_tree_hash(h, r, res)
    rs.fix = res
    if res.fixed:
        rs.fix_first_build = outcome
    h.save()
    _fix_journal_end(h, r, res)


def _fix_tree_hash(h: _Holder, r: int, fix: FixResult) -> str:
    """고친 파일을 넣었을 때의 수정본 해시를 계산한다. 수정본은 바꾸지 않는다. 만들 수 없으면 빈 문자열을 반환한다.

    The hash of the candidate's manifest paths with the fix's bytes substituted, computed and saved together
    with `rs.fix` (the candidate is not touched for it). Same rows as `intake.tree_hash`; "" when it cannot be made.
    Computed from `rounds/<r>/expected.json` plus the fix's files (no read of the tree); "" without that table (both
    callers run only after a build of a vouched candidate, which has it)."""
    from .intake import table_hash
    try:
        table = _expected_table(h, r, with_fix=False)
        if table is None:
            return ""
        # 기대 값 표에 고친 파일의 값을 넣어 계산한다. 수정본 파일은 읽지 않는다
        return table_hash(_with_rows(table, _fix_rows(h, fix)), h.paths_list)
    except Exception:  # noqa: BLE001 - 해시를 만들 수 없으면 재개할 때 다시 계산한다
        log.debug("fix tree hash not computed", exc_info=True)
        return ""


def _fix_paths(h: _Holder, fix: FixResult, r: int) -> list[str] | None:
    """덮어쓸 경로(``rs.fix.changed``)를 확인해서 반환한다.
    경로가 하나라도 manifest의 기존 파일이 아니거나 수정 루프가 바꿀 수 없는 파일이면 None을 반환한다.
    고친 파일이 없을 때도 None이다.

    Every path is checked again with the fixer's `is_editable` rule (the poms Maven reads under another name
    included, read from the candidate) and against the manifest."""
    from .fixer import is_editable, maven_pom_paths
    p = h.paths
    allowed = set(h.project_paths)
    src_root = p.root / fix.fixed_dir if fix.fixed_dir else None
    if src_root is None or not src_root.is_dir():
        return None
    base = p.candidate(r) if p.candidate(r).is_dir() else p.accepted
    other_poms = maven_pom_paths(h.project(base), list(getattr(h.state, "build_targets", None) or []))
    out: list[str] = []
    for rel in fix.changed:
        rel = str(rel).replace("\\", "/").strip("/")
        src = src_root / rel
        if rel not in allowed or ".." in rel.split("/") or src.is_symlink() or not src.is_file():
            return None
        if not is_editable(rel, other_poms):
            return None
        out.append(rel)
    return out


def _copy_over(src: Path, dst: Path) -> None:
    """``dst`` 파일의 내용을 ``src``의 바이트로 바꾼다. 내용이 같으면 쓰지 않고, 권한은 그대로 둔다."""
    data = src.read_bytes()
    if dst.is_file() and not dst.is_symlink() and dst.read_bytes() == data:
        return
    if dst.is_symlink() or not dst.is_file():
        raise RuntimeError(f"not an existing file of the candidate: {dst.name}")
    tmp = dst.with_name(f".{dst.name}.fix-tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, os.lstat(dst).st_mode & 0o777)
    fsops.replace(tmp, dst)


def _overlay_fix(h: _Holder, r: int) -> bool:
    """``fixloop/fixed/``의 고친 파일을 수정본의 같은 경로에 덮어쓴다. 여러 번 호출해도 결과가 같다.
    ``_fix_paths``가 None이거나 수정본에 그 파일이 없으면 아무것도 바꾸지 않는다. 이때 False를 반환한다."""
    rs = h.round_state(r)
    fix = rs.fix
    paths = _fix_paths(h, fix, r) if fix is not None else None
    if paths is None:
        return False
    cand = h.project(h.paths.candidate(r))
    if any(not (cand / rel).is_file() or (cand / rel).is_symlink() for rel in paths):
        return False
    src_root = h.paths.root / fix.fixed_dir
    for rel in paths:
        _copy_over(src_root / rel, cand / rel)
    return True


def _drop_fix_files(h: _Holder, fix: FixResult) -> None:
    """버린 수정 내용의 ``fix.patch``와 ``fixed/`` 폴더를 지운다. state를 저장한 뒤에만 호출한다."""
    for rel in (fix.patch, fix.fixed_dir):
        if rel:
            target = h.paths.root / rel
            try:
                _safe_rmtree(target)
            except OSError as e:
                log.warning("remediation: %s not removed: %s", rel, e)


def _discard_fix(h: _Holder, r: int, reason: str, *, ends_round: bool = False) -> None:
    """수정 루프가 고친 내용을 버린다. 결과는 ``rs.fix_discarded``로 옮기고 ``rs.fix``를 비운다. 사용량은 계속 센다.
    state를 저장한 뒤에 고친 파일을 지운다. 덮어쓴 파일은 되돌리지 않는다.
    수정본은 호출한 쪽이 맞추거나 다시 만든다.

    `ends_round`: this discard ends the round as not fixed (the fresh path of `_confirm_fix`); recorded as
    `FixResult.ended_round` in the same save, so a kill right after it re-enters without a second loop."""
    rs = h.round_state(r)
    fix = rs.fix
    if fix is None:
        return
    gone = FixResult.from_dict(fix.to_dict())
    gone.fixed, gone.reason, gone.retry_passed = False, reason, False
    gone.discarded_changed = sorted(set(gone.discarded_changed) | set(gone.changed))
    gone.changed, gone.patch, gone.fixed_dir, gone.tree_hash = [], "", "", ""
    gone.ended_round = ends_round
    rs.fix_discarded = [*rs.fix_discarded, gone]
    rs.fix, rs.fix_first_build = None, None
    h.save()
    _drop_fix_files(h, fix)
    h.journal({"event": "remediation_fix_discarded", "round": r, "reason": reason,
               "changed": len(gone.discarded_changed)})
    log.info("remediation round %s: the build fix was discarded (%s)", r, reason)


def _confirm_fix(h: _Holder, r: int, cand: Path, *, fresh: bool) -> tuple[Any | None, str | None]:
    """기록된 수정 내용을 수정본에 덮어쓰고 확인 빌드를 실행한다.
    - 통과하면 (확인 빌드 결과, None)을 반환한다
    - 실패하면 덮어쓴 파일을 되돌리고 고치지 못한 것으로 끝낸다
    - 확인 빌드 전이나 도중에 중지 요청이 있으면 고친 내용을 그대로 두고 멈춘다

    `fresh` = the loop just ran on this candidate. Overlay first (idempotent), then the confirming build into
    `rounds/<r>/build-fixed.log`. On re-entry (resume, remade candidate) the paths are checked again
    (`_fix_paths`), the candidate is brought to the expected files (fix included) and its hash compared with
    `rs.fix.tree_hash`; a path that fails the check or a different hash means the stored fix is not usable:
    discarded (`discarded: the stored fix is not usable`), the candidate synchronised without it, and the
    round builds as if no loop had run (a fix the loop has just returned that fails the check ends the round
    as not fixed instead, so the loop never runs twice on one candidate for it). A stop right before or during
    the confirming build discards nothing: the overlay and `rs.fix` stay, the node halts STOPPED with
    `next_node = build`, and a resume confirms again. Only a confirming build that failed with no stop pending
    discards the fix: the overlay is undone first, then the state is saved (`fixed = False`, `reason: error:
    confirming build failed`, `changed` → `discarded_changed`), and only then are `fix.patch` and `fixed/`
    removed; `rs.build` = the first failed build again; then `_candidate_failed`."""
    rs = h.round_state(r)
    fix = rs.fix
    assert fix is not None
    paths = list(fix.changed)
    if paths:
        if not _overlay_fix(h, r):
            log.warning("remediation round %s: the recorded build fix cannot be applied to the candidate", r)
            first = rs.fix_first_build or rs.build
            _discard_fix(h, r, R_FIX_UNUSABLE, ends_round=fresh and first is not None)
            _sync_candidate(h, r, cand, record=False)
            if fresh and first is not None:
                # 방금 실행한 루프의 결과다. 루프를 다시 실행하지 않고 고치지 못한 것으로 끝낸다
                rs.build = first
                nxt = _candidate_failed(h, r, first)
                suffix = f"automatic build fix: {R_FIX_UNUSABLE}"
                h.state.message = f"{h.state.message} | {suffix}" if h.state.message else suffix
                return None, nxt
            return _run_build(h, r, cand)
        if not fresh:
            # 중간에 끝난 빌드가 남긴 변경을 되돌린다. 그다음 같은 수정본에서 고친 내용인지 해시로 확인한다
            _sync_candidate(h, r, cand)
            th = _fix_tree_hash(h, r, fix)
            if fix.tree_hash and th and fix.tree_hash != th:
                log.warning("remediation round %s: the candidate with the recorded build fix differs from the one it "
                            "was made for", r)
                _discard_fix(h, r, R_FIX_UNUSABLE)
                _sync_candidate(h, r, cand, record=False)
                return _run_build(h, r, cand)
            if not fix.tree_hash and th:
                fix.tree_hash = th
                h.save()
    if h.stop():
        h.halt(STOPPED, R_STOP_REQUESTED, "build",
               f"stop requested before the round {r} confirming build; the build fix is kept and confirmed on resume")
        return None, None
    fixed_log = h.paths.round_dir(r) / FIX_LOG
    confirm = _package(h, r, cand, fixed_log, confirming=True)
    if confirm.ok:
        log.info("remediation round %s: the build fix passed the confirming build (%d file(s) changed%s)", r,
                 len(paths), ", the first build failure was transient" if fix.retry_passed else "")
        return confirm, None
    if h.stop():
        h.halt(STOPPED, R_STOP_REQUESTED, "build",
               f"stop requested during the round {r} confirming build; the build fix is kept and confirmed again on "
               "resume")
        return None, None
    old = FixResult.from_dict(fix.to_dict())
    fix.fixed, fix.reason, fix.retry_passed = False, R_FIX_CONFIRM_FAILED, False
    fix.discarded_changed = sorted(set(fix.discarded_changed) | set(paths))
    fix.changed, fix.patch, fix.fixed_dir, fix.tree_hash = [], "", "", ""
    first = rs.fix_first_build or confirm
    rs.build, rs.fix_first_build = first, None
    if paths:
        _sync_candidate(h, r, cand, record=False)       # 덮어쓴 파일을 되돌린다(상태를 저장하기 전에)
    log.warning("remediation round %s: the build fix did not pass the confirming build (%s); the change was reset",
                r, confirm.log)
    h.save()
    _drop_fix_files(h, old)
    _fix_journal_end(h, r, fix)
    return None, _fix_not_applied(h, r, first)


def _fix_not_applied(h: _Holder, r: int, outcome: Any) -> str | None:
    """수정 루프가 고치지 못한 수정본 빌드 실패를 정책대로 끝낸다. 루프의 사유는 메시지 끝에 붙인다.
    - 루프가 시도했으면 ``automatic build fix: <사유>``를 붙인다
    - 시작했지만 시도가 하나도 없으면 ``automatic build fix made no attempt: <사유>``를 붙인다
    - 시작하지 않았으면 ``automatic build fix not started: <사유>``를 붙인다

    A loop that ran and ended `stop requested` while the stop is still pending halts STOPPED with
    `FIX_STOPPED` ("stop requested during the round <r> build fix", no suffix); a stop during a candidate build keeps
    "stop requested during the round <r> candidate build"."""
    fix = h.round_state(r).fix
    if fix is not None and fix.attempted and not fix.fixed and fix.reason == R_FIX_STOP and h.stop():
        # 수정 루프 도중에 중지 요청을 받았다. 수정본 빌드 중의 중지와 구별하고 루프 사유는 덧붙이지 않는다
        h.halt(STOPPED, R_STOP_REQUESTED, "build", FIX_STOPPED.format(r=r))
        return None
    nxt = _candidate_failed(h, r, outcome)
    if fix is not None and not fix.fixed and fix.reason:
        if not fix.attempted:
            label = "automatic build fix not started"
        elif fix_made_no_attempt(fix):
            label = FIX_NO_ATTEMPT
        else:
            label = "automatic build fix"
        suffix = f"{label}: {fix.reason}"
        h.state.message = f"{h.state.message} | {suffix}" if h.state.message else suffix
    return nxt


def _commit_round(h: _Holder, r: int, cand: Path) -> str:
    """수정본을 기대하는 바이트로 다시 맞춘 뒤 accepted/로 확정한다. 마지막 빌드 뒤의 Maven 호출이 바꾼 파일도 기록한다."""
    rs = h.round_state(r)
    _sync_candidate(h, r, cand)
    _commit(h, cand)
    rs.committed = True
    h.journal({"event": "round_committed", "round": r, "applied": list(rs.applied)})
    return "freeze_delivery"


def _drop_ineffective(h: _Holder, r: int, applied: list[Proposal], drop: list[Proposal],
                      problems: list[str]) -> str | None:
    """효과가 없던 override 항목을 빼고 수정본을 다시 만든다. 그 수정본을 한 번 더 빌드하고 검증한다.
    다시 만들 시간이 없거나 두 번째 검증도 실패하면 ``resolution_mismatch``로 끝낸다.

    The only problems of the round are inserted override entries whose governed modules still resolve the old
    version (`apply.split_ineffective`). They are dropped: plan decision `ineffective` (`D_INEFFECTIVE`), one
    `manual_review` residual each, journal `override_ineffective`; the candidate is rebuilt from accepted/
    with the remaining proposals only (byte for byte what applying them alone gives) and built + verified ONCE
    more. Nothing left → the round ends like a round with nothing to apply. A stop / used-up time budget
    before the rebuild, a failed second build or a second verification problem → the round's usual failure
    (FAILED `resolution_mismatch`, or the failed-build path). A build fix of the round is discarded first
    (`rs.fix_discarded`, `discarded: the candidate was remade`), and the remade candidate is built through
    `_run_build`, so its failing build may run the fix loop once more. Only a FIXED fix is discarded (a
    recorded not-fixed result stays in `rs.fix`); `rounds/<r>/applied/` is made anew for the remaining
    proposals."""
    st, c, p = h.state, h.c, h.paths
    rs = h.round_state(r)
    cand = p.candidate(r)
    ids = ", ".join(x.id for x in drop)
    if h.stop():
        h.discard_candidate(r)
        return h.finish(FAILED, R_RESOLUTION_MISMATCH,
                        "; ".join(problems[:5]) + f" | the ineffective override entr(ies) {ids} were not removed and "
                        "rebuilt: a stop was requested or the time budget is used up")
    plan = h.load_plan(r)
    gone = {x.id for x in drop}
    items = _inventory_items(h, r)
    groups: dict[tuple[str, str], list[Proposal]] = {}
    for x in drop:
        pom = next((e.pom for e in x.edits if e.op == "insert"), "")
        groups.setdefault((ga_key(x.ga), version_key(x.from_version)), []).append(x)
        h.journal({"event": "override_ineffective", "round": r, "proposal": x.id, "pom": pom, "ga": x.ga,
                   "modules": list(x.modules)})
        log.info("remediation round %s: override entry %s in %s had no effect on %s; removed, rebuilding without it",
                 r, x.id, pom, x.ga)
    # 남은 항목은 (g:a, 버전, 사유)마다 하나다. 같은 라이브러리에서 뺀 항목은 pom을 모두 모아 한 항목으로 만든다
    residuals: list[Residual] = []
    for key, xs in groups.items():
        x = xs[0]
        poms = [next((e.pom for e in y.edits if e.op == "insert"), "") for y in xs]
        it = items.get(key)
        if len(xs) == 1:
            detail = (f"the override entry in {poms[0]} did not change the resolved version of {x.ga} "
                      f"({x.from_version} stays): a dependency declared with a version, here or in a parent, wins "
                      "over <dependencyManagement>; the entry was removed again")
        else:
            detail = (f"the override entries in {', '.join(poms)} did not change the resolved version of {x.ga} "
                      f"({x.from_version} stays): a dependency declared with a version, here or in a parent, wins "
                      "over <dependencyManagement>; the entries were removed again")
        srcs = [sc for y in xs for sc in y.sources]
        residuals.append(Residual(
            ga=x.ga, version=x.from_version, origin=it.origin if it is not None else "proposal", reason=R_MANUAL_REVIEW,
            detail=detail, finding_ids=sorted({f for y in xs for f in y.finding_ids}),
            modules=sorted({m for y in xs for m in [*y.modules, *y.also_modules]}),
            sources=srcs or (list(it.sources or []) if it is not None else []),
            reported_version=(it.reported_version or "") if it is not None else ""))
    _merge_residuals(st, residuals)
    keep = [x for x in applied if x.id not in gone]
    if rs.fix is not None and rs.fix.fixed:
        # 고친 내용은 지금 수정본에 대한 것이라서 다시 만들기 전에 버린다. 고치지 못한 기록은 그대로 둔다.
        # 완성 표시를 먼저 지운다. 버린 뒤 파드가 끝나도 고친 소스가 남은 수정본을 그대로 빌드하지 않는다
        h.unmark_candidate(r)
        _discard_fix(h, r, R_FIX_DISCARDED)
    rs.applied = [x.id for x in keep]
    # 수정본을 지우기 전에 적용 목록과 next_node=apply를 저장한다. 어디서 끝나도 재개하면 accepted/에서 다시 만든다.
    # 저장과 plan.json 기록 사이에서 끝나면 apply가 뺄 제안까지 다시 적용한다. 그러면 build가 같은 판단을 한 번 더 한다
    h.unmark_candidate(r)
    st.next_node = "apply"
    h.save()
    if plan is not None:
        for x in plan.proposals:
            if x.id in gone:
                x.decision = D_INEFFECTIVE
        write_json_atomic(p.plan(r), plan.to_dict())
    _safe_rmtree(cand)
    _safe_rmtree(_applied_dir(h, r))
    _safe_rmtree(_expected_path(h, r))
    if not keep:
        # 남은 변경이 없으면 적용할 것이 없던 라운드와 같이 끝낸다
        if st.delivery is None:
            h.deliver_original()
        return "evaluate"
    c.intake.copy_tree(p.accepted, cand, h.paths_list)
    result = c.apply.apply_proposals(h.project(cand), keep)
    p.patch(r).write_text(result.patch or "", encoding="utf-8")
    write_json_atomic(p.round_dir(r) / "apply.json", result.to_dict())
    if result.skipped or list(result.applied) != rs.applied:
        h.discard_candidate(r)
        return h.finish(FAILED, R_RESOLUTION_MISMATCH,
                        "; ".join(problems[:5]) + f" | the candidate without {ids} could not be rebuilt: "
                        + "; ".join(f"{k}: {v}" for k, v in sorted(result.skipped.items())))
    violations = list(c.apply.verify_text_only(h.project(p.accepted), h.project(cand), keep, h.project_paths))
    if violations:
        rs.verify_problems = violations
        h.discard_candidate(r)
        return h.finish(FAILED, R_TEXT_ONLY_VIOLATION, "; ".join(violations[:5]))
    _write_expected(h, r, _store_applied(h, r, cand, result, keep))
    h.mark_candidate(r)
    # 다시 만든 수정본의 빌드가 실패하면 같은 조건에서 수정 루프를 한 번 더 실행할 수 있다
    outcome, nxt = _run_build(h, r, cand)
    if outcome is None:
        return nxt
    lines = _verify(h, r, cand, keep)
    rs.verify_problems = lines
    again = [ln for ln in lines if not ln.startswith("info:")]
    if again:
        h.discard_candidate(r)
        return h.finish(FAILED, R_RESOLUTION_MISMATCH,
                        "; ".join(again[:5]) + f" | after removing the ineffective override entr(ies) {ids}")
    return _commit_round(h, r, cand)


def _freeze_delivery(h: _Holder) -> str | None:
    """확정한 accepted/를 결과 ZIP으로 만든다.
    ZIP을 쓰지 못하면 이전 결과 ZIP이 그대로 남는다. ``_wrap``이 ``DeliveryNotWritten``을 받아 FAILED로 끝낸다."""
    st, p = h.state, h.paths
    # 조치 전과 같은 오류로 빌드가 실패한 변경이 들어 있으면 빌드로 확인한 결과 ZIP이 아니다
    tolerated = h.baseline_failed_tolerated and any(rs.committed and rs.build_tolerated for rs in st.rounds)
    h.install_delivery(lambda out: h.c.intake.freeze_delivery(p.accepted, out, st.round, h.paths_list), st.round,
                       build_ok=not tolerated, build_tolerated=tolerated)
    return "rescan"


def _rescan(h: _Holder) -> str | None:
    st, p = h.state, h.paths
    r = st.round
    label = f"final-r{r}"
    if st.delivery is None or not p.delivery_zip.is_file():
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "the delivery ZIP is missing")
    # 재검사한 ZIP이 확정한 바이트와 같아야 검사 결과가 결과 ZIP에 대한 것이 된다
    if sha256_file(p.delivery_zip) != st.delivery.sha256:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "the delivery ZIP changed after it was frozen")
    name = f"{h.rcfg.ivas_project_prefix}-{st.id}-{label}"
    res = h.scan("rescan", label, p.delivery_zip, st.delivery.sha256, name)
    if res is None:
        return None
    h.round_state(r).scan_label = label
    st.final_scan = h.rel(p.scan(label))
    h.mark_scanned()
    write_json_atomic(p.delivery_json, st.delivery.to_dict())
    if R_ZIP_HASH_MISMATCH in res.incomplete_reasons or (res.zip_sha256 and res.zip_sha256 != st.delivery.sha256):
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "IVAS scanned a ZIP whose sha256 differs from the delivery ZIP")
    if not res.complete:
        return h.finish(INCOMPLETE, R_SCAN_INCOMPLETE, "final scan incomplete: " + ", ".join(res.incomplete_reasons))
    return "evaluate"


def _evaluate(h: _Holder) -> str | None:
    st = h.state
    final = h.load_scan(st.final_scan)
    complete = final is not None and final.complete
    # 남은 취약점은 조치 대상만 센다. ignore_actions 코드의 항목은 성공 판단과 다음 라운드 판단에 넣지 않는다
    remaining = len(h.split(final)[0]) if complete else 0
    last = next((rs for rs in st.rounds if rs.round == st.round), None)
    committed_last = bool(last and last.committed)
    max_rounds = int(h.rcfg.max_rounds)
    if complete and remaining and committed_last and st.round < max_rounds:
        st.round += 1
        h.journal({"event": "next_round", "round": st.round, "remaining": remaining})
        return "inventory"

    reasons: set[str] = set()
    if (st.baseline_build is None or not st.baseline_build.ok) and not (h.baseline_failed_tolerated and h.nothing_unconfirmed):
        # 조치 전 빌드 실패를 받아들인 실행은 빌드로 확인하지 못한 변경이 있을 때만 이 사유를 둔다
        reasons.add(R_BASELINE_BUILD_FAILED)
    for rs in st.rounds:
        if rs.applied and not (rs.build is not None and (rs.build.ok or rs.build_tolerated) and rs.committed):
            reasons.add(R_CANDIDATE_BUILD_FAILED)
    if not complete:
        reasons.add(R_SCAN_INCOMPLETE)
    if st.delivery is None or final is None or final.zip_sha256 != st.delivery.sha256:
        reasons.add(R_ZIP_HASH_MISMATCH)
    if st.delivery is not None:
        # scanned 필드가 없는 이전 state를 재개해도 같은 규칙으로 다시 계산한다
        h.mark_scanned()
        if not st.delivery.build_ok and not st.delivery.build_tolerated:
            reasons.add(R_CANDIDATE_BUILD_FAILED)
        elif not st.delivery.scanned:
            reasons.add(R_ZIP_HASH_MISMATCH if complete else R_SCAN_INCOMPLETE)
    if files_reported(h.file_mode):
        # 설정이 생기기 전이나 count 모드에서 만든 파일 컴포넌트 항목은 report 모드에서 남은 항목이 아니다
        dropped = [x for x in st.residuals if is_file_residual(x)]
        if dropped:
            st.residuals = [x for x in st.residuals if not is_file_residual(x)]
            h.journal({"event": "file_residuals_dropped", "residuals": len(dropped)})
    residual_reasons = [x.reason or R_MANUAL_REVIEW for x in st.residuals]
    reasons.update(residual_reasons)
    if remaining:
        reasons.add(R_RESIDUAL_FINDINGS)
        if committed_last and st.round >= max_rounds:
            reasons.add(R_ROUNDS_EXHAUSTED)
    ordered = [x for x in (R_BASELINE_BUILD_FAILED,) + _REASON_ORDER if x in reasons]
    ordered += sorted(x for x in reasons if x not in ordered and x != R_ROUNDS_EXHAUSTED)
    if R_ROUNDS_EXHAUSTED in reasons:
        ordered.append(R_ROUNDS_EXHAUSTED)
    if h.baseline_failed_tolerated:
        st.message = tolerated_message(st)
    if not ordered:
        if not h.baseline_failed_tolerated:
            st.message = ""
        return h.finish(SUCCEEDED, "", reasons=[])
    return h.finish(INCOMPLETE, ordered[0], reasons=ordered)


# 조치 전 빌드 실패를 받아들인 실행이 끝날 때 state.message의 앞부분. i18n의 한국어 규칙과 보고서가 이 문장을 알아본다
TOLERATED_HEAD = "the project did not build before the remediation either"


def tolerated_message(st: RemediationState) -> str:
    """조치 전 빌드 실패를 받아들인 실행의 결과를 설명하는 영어 문장을 만든다.

    The English sentence of a run whose failed baseline build was tolerated: the baseline's error count, whether
    versions were changed and scanned again, that no build confirmed them, and what the comparison showed."""
    cmps = [rs.build_compare for rs in st.rounds if rs.committed and rs.build_tolerated and rs.build_compare]
    base_n = cmps[-1].get("baseline_errors") if cmps else None
    head = TOLERATED_HEAD + (f" ({base_n} error(s) in baseline/build.log)" if isinstance(base_n, int) else "")
    if not cmps:
        if any(rs.committed for rs in st.rounds):
            return f"{head}; the changed versions were built and scanned again"
        return f"{head}; no version was changed, so there was nothing for a build to confirm"
    last = cmps[-1]
    return (f"{head}; the changed versions were scanned again, but no build confirmed them: the candidate build "
            f"stopped at the same errors as the baseline build ({last.get('candidate_errors')} error(s), none new)")


def _report(h: _Holder) -> str | None:
    from . import report as report_mod  # report.py는 그래프에서만 쓰므로 여기서 import한다
    for rs in list(h.state.rounds):
        # 빌드 노드를 다시 거치지 않고 끝난 실행에도 중간에 끝난 수정 루프의 LLM 사용량이 빠지지 않게 한다
        try:
            _settle_leftover_loop(h, rs.round)
        except Exception:  # noqa: BLE001 - 사용량 정리가 보고서를 막지 않게 한다
            log.warning("remediation round %s: a leftover fix loop folder was not settled", rs.round, exc_info=True)
    # 위험도 경계값이다. 화면 표시에만 쓰므로 정책 해시에 넣지 않는다. 이전 설정 객체에 없으면 기본값을 쓴다
    severity = (getattr(h.rcfg, "severity_red", 9.0), getattr(h.rcfg, "severity_orange", 7.0),
                getattr(h.rcfg, "severity_yellow", 4.0))
    report_mod.write_report(h.state, h.paths, ignore_actions=sorted(h.ignored_codes),
                            inhouse_message=getattr(h.rcfg, "inhouse_message", None), severity=severity,
                            inhouse=inhouse_keys(h.cfg), file_components=h.file_mode)
    return None


_NODE_FNS: dict[str, Callable[[_Holder], str | None]] = {
    "baseline_build": _baseline_build, "baseline_scan": _baseline_scan, "inventory": _inventory, "plan": _plan,
    "apply": _apply, "build": _build, "freeze_delivery": _freeze_delivery, "rescan": _rescan, "evaluate": _evaluate,
    "report": _report,
}


def _without_report_note(message: str) -> str:
    """상태 메시지 끝에 붙은 보고서 실패 문장(`` | report not written: …``)을 뗀 메시지를 반환한다.

    The note the report node appends once when it fails is always the message's tail (later changes prepend); it is
    removed when the report is written on a later entry, so neither the state nor the new report keeps it."""
    text = message or ""
    cut = text.find(f" | {REPORT_NOT_WRITTEN}")
    if cut >= 0:
        return text[:cut]
    return "" if text.startswith(REPORT_NOT_WRITTEN) else text


# ---------------------------------------------------------------------------- graph
def _wrap(name: str, h: _Holder) -> Callable[[RemediationGraphState], dict]:
    """노드 함수에 노드 경계 처리를 덧붙인 함수를 반환한다.
    순서는 중지 확인 -> RUNNING 저장, node_start -> 노드 실행 -> node_end 저장이다.
    예외가 나면 FAILED internal_error로 바꾸고 보고서 노드로 간다. 대기 상태로 멈추면 보고서 없이 그래프를 끝낸다."""
    fn = _NODE_FNS[name]

    def node(gs: RemediationGraphState) -> dict:
        st = h.state
        if name == "report":
            # 보고서는 끝 상태에서만 실행한다. 여기서 실패해도 보고서로 다시 돌아가지 않는다
            h.journal({"event": "node_start", "node": name, "round": st.round})
            # report.json이 저장되는 state.json과 같도록 시각과 노드를 먼저 정한다. 저장할 때 시각을 다시 찍지 않는다
            before, before_msg = st.updated, st.message
            # 앞선 진입에서 붙인 보고서 실패 문장은 이번에 보고서를 쓰면 맞지 않는다. 떼고 쓴다
            st.message = _without_report_note(st.message)
            st.node, st.next_node, st.updated = name, "report", _now()
            try:
                fn(h)
            except Exception as e:  # noqa: BLE001 - 보고서 실패가 끝 상태를 덮어쓰지 않게 한다
                log.error("report node failed: %s", scrub_message(traceback.format_exc(), h.paths.root, 20000))
                if REPORT_NOT_WRITTEN in (before_msg or ""):
                    # 앞선 진입에서 이미 실패를 기록했다. 문장을 다시 붙이지 않고 시각도 그대로 둔다
                    st.message, st.updated = before_msg, before
                else:
                    st.message = (st.message + " | " if st.message else "") + \
                        scrub_message(f"{REPORT_NOT_WRITTEN}: {type(e).__name__}: {e}", h.paths.root)
                    st.updated = _now()
            h._save(st)
            h.journal({"event": "node_end", "node": name, "status": st.status})
            return {"node": name, "halt": True, "next": END, "round": st.round}
        if h.stop() or st.stop_requested:
            h.halt(STOPPED, R_STOP_REQUESTED, name, f"stop requested before node {name}")
            h.save()
            h.journal({"event": "stopped", "node": name})
            return {"node": name, "halt": True, "next": END, "round": st.round}
        st.status, st.node, st.next_node = RUNNING, name, name
        h.save()
        h.journal({"event": "node_start", "node": name, "round": st.round})
        try:
            nxt = fn(h)
        except Exception as e:  # noqa: BLE001 - 노드의 예상 못 한 오류도 보고서까지 간다
            # 예외 메시지에 URL 자격 증명 같은 비밀 값이 있을 수 있다. traceback도 마스킹한 뒤 로그에 기록한다
            log.error("remediation node %s failed: %s", name, scrub_message(traceback.format_exc(), h.paths.root, 20000))
            if isinstance(e, (DeliveryNotWritten, DeliveryRecordNotWritten)):
                # 예외 문장이 남은 결과 ZIP이나 이미 바뀐 새 결과 ZIP을 알려 준다. 그대로 메시지로 쓴다
                nxt = h.finish(FAILED, R_INTERNAL_ERROR, scrub_message(str(e), h.paths.root))
            elif isinstance(e, ScanSourceChanged):
                nxt = h.finish(FAILED, R_ZIP_HASH_MISMATCH, scrub_message(str(e), h.paths.root))
            else:
                reason = e.code if isinstance(e, NotReady) else R_INTERNAL_ERROR
                nxt = h.finish(FAILED, reason, scrub_message(f"{type(e).__name__}: {e}", h.paths.root))
        h.journal({"event": "node_end", "node": name, "status": st.status, "next": nxt, "round": st.round})
        if nxt is None:                       # 노드가 대기 상태로 멈춘 경우다
            h.save()
            return {"node": name, "halt": True, "next": END, "round": st.round}
        st.next_node = nxt
        h.save()
        return {"node": name, "halt": False, "next": nxt, "round": st.round}

    node.__name__ = name
    return node


def build_graph(holder: Any) -> Any:
    """``holder``를 쓰는 StateGraph를 만들어 컴파일한다.
    진입 노드 ``route``가 ``state.next_node``로 바로 건너뛴다. 그 뒤로 ``NODES``가 이어진다."""
    targets = {n: n for n in NODES}
    targets[END] = END

    def route(gs: RemediationGraphState) -> dict:
        st = holder.state
        nxt = st.next_node if st.next_node in NODES else NODES[0]
        if st.status in TERMINAL:
            nxt = END
        return {"id": st.id, "node": ROUTE, "round": st.round, "halt": False, "next": nxt}

    def router(gs: RemediationGraphState) -> str:
        return gs.get("next") or END

    g = StateGraph(RemediationGraphState)
    g.add_node(ROUTE, route)
    for n in NODES:
        g.add_node(n, _wrap(n, holder))
    g.add_edge(START, ROUTE)
    g.add_conditional_edges(ROUTE, router, targets)
    for n in NODES:
        g.add_conditional_edges(n, router, targets)
    return g.compile()


def run_remediation(state: RemediationState, paths: RunPaths, cfg: Any, components: Components, *,
                    save: Callable[[RemediationState], None], journal: Journal, stop: StopCheck) -> RemediationState:
    """취약점 조치 하나를 ``state.next_node``부터 실행한다.
    끝 상태나 대기 상태(AWAITING_CONFIRMATION / STOPPED)가 되면 멈춘다.
    노드 경계마다 ``save``를 호출한다.
    - 이미 끝 상태면 결과 ZIP 기록과 남은 보고서만 맞추고 반환한다
    - 이전 버전이 남긴 승인 대기 상태면 아무것도 하지 않는다"""
    # 결과 ZIP을 바꾸던 중에 끝났으면 state를 디스크의 결과 ZIP과 먼저 맞춘다
    if recover_delivery(paths, state):
        save(state)
    # 승인을 기다리던 이전 버전의 작업은 끝난 작업처럼 다루고 아무것도 실행하지 않는다
    if state.status == LEGACY_AWAITING_APPROVAL:
        return state
    if state.status in TERMINAL:
        # 보고서 노드 도중에 끝난 작업이면 보고서만 쓴다
        repair_final(state, paths, cfg, save=save, journal=journal)
        return state
    _recover_accepted(paths)
    holder = _Holder(state, paths, cfg, components, save, journal, stop)
    if state.status in RESUMABLE or state.status == QUEUED:
        state.reason, state.message = "", ""
    graph = build_graph(holder)
    max_rounds = int(getattr(cfg.remediation, "max_rounds", 3) or 3)
    limit = RECURSION_BASE + RECURSION_PER_ROUND * max(1, max_rounds)
    graph.invoke(RemediationGraphState(id=state.id, node="", round=state.round, halt=False, next=""),
                 config={"recursion_limit": limit})
    return holder.state


def report_written(paths: RunPaths, state: RemediationState) -> bool:
    """이 state로 보고서를 썼는지 확인한다. report.md가 있고 report.json에 담긴 ``updated``가 같으면 True다.

    The report node stamps `state.updated`, writes `fix.patch`, `report.json` (which embeds the state) and
    `report.md`, then saves the state without stamping again; so a report belongs to this state exactly when
    `report.md` exists and `report.json["state"]["updated"]` equals `state.updated`."""
    if not paths.report_md.is_file():
        return False
    data = read_json(paths.report_json)
    rec = data.get("state") if isinstance(data, dict) else None
    return isinstance(rec, dict) and rec.get("updated") == state.updated


def report_pending(paths: RunPaths, state: RemediationState) -> bool:
    """끝 상태로 보고서 노드에 들어갔지만 보고서를 다 쓰지 못한 작업인지 확인한다.

    A final status (`TERMINAL`) whose next node is `report` (every final status the graph saves) and whose report was
    not written for it (`report_written`): a kill during the report node. States that ended outside the graph (an
    intake rejection, a start that failed) never had a report node and are not pending."""
    return state.status in TERMINAL and state.next_node == "report" and not report_written(paths, state)


def write_pending_report(state: RemediationState, paths: RunPaths, cfg: Any, *,
                         save: Callable[[RemediationState], None], journal: Journal) -> bool:
    """보고서 노드 도중에 끝난 작업의 보고서를 쓴다. 합친 ``fix.patch``도 쓴다. 쓸 것이 없으면 False를 반환한다.

    Runs the report node once (`_report` through `_wrap`, so the same stamps and saves) for a `report_pending` state;
    nothing else of the graph runs. Called on the next entry of a run folder: `run_remediation` (in-process service,
    `chain._drive`) and `chain`'s entries for an existing state (`run_after_migration`, `run_only`, `resume`)."""
    if not report_pending(paths, state):
        return False
    log.warning("remediation %s: the report of the %s run was not written; writing it now", state.id, state.status)
    holder = _Holder(state, paths, cfg, None, save, journal, lambda: False)    # type: ignore[arg-type]
    holder.journal({"event": "report_rewritten", "status": state.status})
    _wrap("report", holder)(RemediationGraphState(id=state.id, node="", round=state.round, halt=False, next=""))
    return True


def repair_final(state: RemediationState, paths: RunPaths, cfg: Any, *, save: Callable[[RemediationState], None],
                 journal: Journal) -> bool:
    """끝 상태의 실행 폴더를 정리한다. 고친 것이 있으면 True를 반환한다.
    결과 ZIP을 바꾸던 기록이 남았으면 마저 맞춘다. 쓰지 못한 보고서가 있으면 쓴다.

    v2.1z-bo: `recover_delivery` (the state saved when it changed; journal `delivery_recovered`) then
    `write_pending_report`. The caller holds the run claim. The status, the reasons and the message are left as they
    are (the report node stamps `updated` and writes the report for this state)."""
    changed = recover_delivery(paths, state)
    if changed:
        save(state)
        try:
            journal({"remediation": state.id, "event": "delivery_recovered",
                     "round": state.delivery.round if state.delivery is not None else None})
        except Exception:  # noqa: BLE001 - 저널 한 줄 때문에 복구를 멈추지 않는다
            log.debug("journal line not written: delivery_recovered", exc_info=True)
    wrote = write_pending_report(state, paths, cfg, save=save, journal=journal)
    return changed or wrote


def final_repair_needed(paths: RunPaths, state: RemediationState) -> bool:
    """끝 상태인데 결과 ZIP 기록이나 보고서를 맞춰야 하는지 확인한다.

    A final state (`TERMINAL`) with a swap left half done (`delivery_swap_pending`) or a pending report
    (`report_pending`)."""
    return state.status in TERMINAL and (delivery_swap_pending(paths) or report_pending(paths, state))


# ---------------------------------------------------------------------------- service helpers
def verified_zip_path(paths: RunPaths, round: int) -> Path:
    """실패한 수정본이 result.zip을 차지할 때 라운드 ``round``의 검증한 ZIP을 복사해 둘 경로를 반환한다."""
    return paths.delivery_zip.with_name(f"result-r{int(round)}-verified.zip")


def find_verified_zip(paths: RunPaths) -> Path | None:
    """``delivery/result-r<k>-verified.zip``을 찾아 반환한다. 여럿이면 k가 가장 큰 것을 반환한다. 없으면 None이다."""
    folder = paths.delivery_zip.parent
    if not folder.is_dir():
        return None
    found = []
    for f in folder.glob("result-r*-verified.zip"):
        m = re.fullmatch(r"result-r([0-9]{1,4})-verified\.zip", f.name)
        if m and f.is_file() and not f.is_symlink():
            found.append((int(m.group(1)), f))
    return max(found)[1] if found else None


def new_state(remediation_id: str, name: str, cfg: Any, intake: IntakeResult, configs: list[str], *,
              policy_hash: str) -> RemediationState:
    """접수가 끝난 취약점 조치의 첫 state를 만든다. QUEUED, 라운드 1, 첫 노드부터 시작하고 정책 해시를 담는다."""
    now = _now()
    return RemediationState(id=remediation_id, name=name, status=QUEUED, created=now, updated=now, node="",
                            next_node=NODES[0], round=1, configs=list(configs or []), policy_hash=policy_hash,
                            intake=intake)


def can_resume(state: RemediationState) -> bool:
    """재개할 수 있는 state인지 확인한다. 확인 대기, 중지, 중단 상태이고 다음 노드가 알려진 노드여야 한다.

    A state an earlier version saved in `AWAITING_APPROVAL` never can (`approval_removed`)."""
    return state.status in RESUMABLE and state.next_node in NODES


def approval_removed(state: RemediationState) -> StateConflict | None:
    """이전 버전이 남긴 승인 대기 state면 재개를 거절하는 오류를 반환한다. 아니면 None을 반환한다.

    The refusal for a state an earlier version saved in `AWAITING_APPROVAL`: `StateConflict` (HTTP 409) with the
    English reason as its message and the Korean sentence of the page in `details["message_ko"]`; None otherwise."""
    if state.status != LEGACY_AWAITING_APPROVAL:
        return None
    return StateConflict(f"remediation {state.id}: {LEGACY_APPROVAL_MESSAGE}", status=state.status,
                         message_ko=LEGACY_APPROVAL_MESSAGE_KO)


def default_components(cfg: Any, journal: Journal | None = None) -> Components:
    """운영용 구성 요소 묶음을 만든다. IVAS, Maven, 저장소, 접수, 인벤토리, 계획, 적용, 수정 루프 구현을 연결한다.
    ``journal``을 주면 Maven 게이트웨이가 실행 환경 경고(``maven_project_config``)를 그 저널에 기록한다.
    버전 근거는 승인된 호스트(``ServerMaven.allowed_hosts``)의 저장소에서 온 것만 인정한다.

    Imported lazily so the graph module never depends on them at import time."""
    from . import apply as apply_mod
    from . import fixer as fixer_mod
    from . import intake as intake_mod
    from . import inventory as inventory_mod
    from . import ivas as ivas_mod
    from . import maven as maven_mod
    from . import planner as planner_mod
    settings = ivas_mod.ivas_settings(cfg)
    maven = maven_mod.ServerMaven(cfg, journal=journal)
    return Components(
        ivas=ivas_mod.IvasHttpClient(settings),
        scan=functools.partial(ivas_mod.run_scan, settings=settings),
        maven=maven,
        versions=planner_mod.RepositoryVersionSource(cfg, allowed_hosts=maven.allowed_hosts),
        intake=intake_mod,
        inventory=inventory_mod.build_inventory,
        plan=planner_mod.plan_round,
        apply=apply_mod,
        fixer=fixer_mod.RemediationFixer(),
    )
