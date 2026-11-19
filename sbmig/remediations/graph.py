"""취약점 조치의 전체 흐름을 LangGraph로 실행하는 모듈. 이관 그래프(``sbmig/graph/engine.py``)와는 별개다.
노드: baseline_build -> baseline_scan -> inventory -> plan -> apply -> build -> freeze_delivery -> rescan ->
evaluate -> report. evaluate에서 취약점이 남았고 라운드 한도 안이면 inventory로 돌아간다.
진입점은 ``run_remediation``이며, 대기나 중지 뒤 재개하면 ``state.next_node``부터 다시 실행한다. 그래프 상태에는 작은
JSON 값만 두고 ``RemediationState``, 경로, ``Components``는 노드 함수들이 공유하는 holder 객체에 둔다.
- 노드 경계마다 state를 저장한다
- 조치 전 빌드가 실패하면 검사와 계획으로 넘어가지 않는다. ``state.baseline_tolerated``인 실행만 이어 가고,
  수정본 빌드가 조치 전 빌드에 없던 오류를 내지 않았을 때만 라운드를 확정하며 SUCCEEDED로 끝내지 않는다
- 결과를 모르는 업로드는 다시 올리지 않고, 원격 IVAS 분석을 취소된 것으로 기록하지 않는다
- 수정본은 따로 복사한 트리에서 빌드하고 검증한 뒤에만 accepted/로 확정한다
- 결과 ZIP과 같은 바이트(sha256)를 재검사하며, SUCCEEDED는 evaluate의 모든 조건을 만족할 때만 정한다
- 수정본 빌드가 실패했을 때 ``remediation.failed_candidate``가 include(기본값)면 실패한 트리를 결과 ZIP으로
  확정하고(build_ok=False, 재검사 없음, FAILED), 이전 라운드에서 검증된 ZIP은
  ``delivery/result-r<k>-verified.zip``으로 남긴다. discard면 결과 ZIP에 넣지 않는다
- 버전 문자열 밖의 변경이 있거나 의존성 결과가 계획과 다른 수정본은 설정과 상관없이 내보내지 않는다

Independent LangGraph of the remediation feature (not the migration graph). Only small JSON values live in the graph
state; the `RemediationState`, the run paths and the `Components` live in a holder closed over by the node functions.
Entry point: `run_remediation`; helpers for the service: `new_state`, `can_resume`; `build_graph` (node list for tests);
`default_components(cfg)` wires the real implementations lazily.
"""
from __future__ import annotations

import functools
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

from .inhouse import inhouse_keys
from .interfaces import Components, Journal, StopCheck
from .matching import finding_key, ga_key, version_key
from .models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    D_APPROVED,
    D_PENDING,
    D_REJECTED,
    FAILED,
    INCOMPLETE,
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
    R_PENDING_APPROVAL,
    R_REJECTED_PROPOSALS,
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
# 서버 정책이 바뀌어 승인한 계획을 버리고 다시 계획했을 때 state.message 앞에 붙이는 문장
POLICY_CHANGED_NOTE = ("the server policy changed since these proposals were approved: the earlier approvals were not "
                       "applied and the plan was made again")
# 상태가 보증하지 않는 수정본을 결과 ZIP으로 내보내지 않았을 때의 문장
CANDIDATE_NOT_VOUCHED = ("the round {r} candidate was not delivered: the files on disk are not the complete change set "
                         "this state records")
# 노드 하나가 그래프 한 단계이고 라운드마다 7단계가 필요하므로, LangGraph 기본값 25로는 모자라 넉넉히 둔다
RECURSION_BASE = 100
RECURSION_PER_ROUND = 20
# 성공이 아닌 사유의 우선순위.
# 결과를 믿을 수 없는 사유, 사람이 결정할 사유, 남은 취약점 순이며 state.reason에는 첫 번째 사유가 들어간다
_REASON_ORDER = (R_SCAN_INCOMPLETE, R_ZIP_HASH_MISMATCH, R_CANDIDATE_BUILD_FAILED, R_PENDING_APPROVAL,
                 R_REJECTED_PROPOSALS, R_RESIDUAL_FINDINGS, R_MANUAL_REVIEW, R_NO_CANDIDATE_VERSION,
                 R_INHOUSE_ARTIFACT)
# 예외 메시지에서 마스킹할 값을 고르는 단어. 이름에 이 단어가 든 환경 변수의 값과 IVAS 아이디를 가린다
_SECRET_WORDS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "CREDENTIAL", "PRIVATE_KEY")
_SECRET_NAMES = ("IVAS_USER_ID", "IVAS_PASSWORD")
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^\s/@'\"<>]+@")
_MIN_SECRET_LEN = 4


class RemediationGraphState(TypedDict, total=False):
    """그래프 상태를 담는 TypedDict. 작업 id, 방금 실행한 노드, 라운드, 멈춤 여부, 다음 노드 같은 작은 JSON 값만 둔다.

    Graph state: small JSON values only."""
    id: str
    node: str
    round: int
    halt: bool
    next: str


# ---------------------------------------------------------------------------- helpers
def _now() -> float:
    return time.time()


def scrub_message(text: str, root: Path | None = None, limit: int = 2000) -> str:
    """사람이 읽을 메시지에서 비밀 값, URL 자격 증명, 실행 폴더의 절대 경로를 지운다. 예외를 던지지 않는다.

    Remove secret env values, URL userinfo and the run directory's absolute path from a message."""
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
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)


class _Holder:
    """노드 함수들이 공유하는 무거운 객체를 그래프 상태 밖에 모아 두는 클래스."""

    def __init__(self, state: RemediationState, paths: RunPaths, cfg: Any, components: Components,
                 save: Callable[[RemediationState], None], journal: Journal, stop: StopCheck):
        self.state, self.paths, self.cfg, self.c = state, paths, cfg, components
        self._save, self._journal, self._stop = save, journal, stop
        self._paths_list: list[str] | None = None
        self._unknown_noted: set[str] = set()

    # -- plumbing
    def save(self) -> None:
        self.state.updated = _now()
        self._save(self.state)

    def journal(self, event: dict[str, Any]) -> None:
        try:
            self._journal({"remediation": self.state.id, **event})
        except Exception:  # a journal line never fails a node
            log.debug("journal line not written: %s", event.get("event"), exc_info=True)

    def stop(self) -> bool:
        try:
            return bool(self._stop())
        except Exception:  # noqa: BLE001 — an unreadable stop flag is not a stop request
            return False

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
        """빌드 결과를 state에 넣기 전에 로그 경로를 실행 폴더 기준 상대 경로로
        바꾸고, 요약에 있는 비밀 값과 절대 경로를 지운다.
        빌드 대상이 여럿이어서 게이트웨이가 실패한 대상의 로그를 알려 주면 그 로그를 쓴다."""
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
        """승인, 확인, 중지 같은 대기 상태로 작업을 멈춘다. 보고서 노드는 실행하지 않는다."""
        st = self.state
        st.status, st.reason, st.next_node, st.message = status, reason, next_node, message
        st.reasons = [reason]

    @property
    def baseline_failed_tolerated(self) -> bool:
        """조치 전 빌드가 실패했지만 그 실패를 받아들이고 이어 가는 실행인지 확인한다."""
        st = self.state
        return bool(st.baseline_tolerated and st.baseline_build is not None and not st.baseline_build.ok)

    @property
    def nothing_changed(self) -> bool:
        """확정한 라운드에 적용한 제안이 하나도 없는지 확인한다. 이때 결과 ZIP은 원본이다."""
        return not any(rs.committed and rs.applied for rs in self.state.rounds)

    @property
    def nothing_unconfirmed(self) -> bool:
        """확정한 라운드 가운데 빌드로 확인하지 못한 변경이 하나도 없는지 확인한다.
        바꾼 버전이 없거나, 버전을 바꾼 수정본의 빌드가 모두 통과했을 때 True다."""
        return not any(rs.committed and rs.applied and rs.build_tolerated for rs in self.state.rounds)

    def finish(self, status: str, reason: str, message: str = "", reasons: list[str] | None = None) -> str:
        """최종 상태(FAILED / INCOMPLETE / SUCCEEDED)를 정하고, 다음 노드로 보고서 노드를 반환한다.
        조치 전 빌드 실패를 받아들인 실행이 버전을 바꿨으면 SUCCEEDED가 되지 않고 사유에 ``baseline_build_failed``가 들어간다.
        바꾼 버전이 없으면 빌드로 확인할 것이 없으므로 그 사유를 빼고 원래 상태로 끝낸다.

        Tolerated baseline failure: FAILED keeps its reasons and gets `baseline_build_failed` appended. Otherwise, when
        a committed round applied a proposal whose candidate build failed with the baseline's errors (changes no build
        confirmed), the status is INCOMPLETE with `baseline_build_failed` first; when none did (the delivery is the
        original, or every candidate build passed), `baseline_build_failed` is dropped and the run ends as it would
        without the tolerance: SUCCEEDED when nothing else is left (the reason list empty), INCOMPLETE with its own
        reasons otherwise."""
        st = self.state
        reasons = list(reasons) if reasons is not None else ([reason] if reason else [])
        if self.baseline_failed_tolerated and status != FAILED and self.nothing_unconfirmed:
            # 바꾼 버전이 없거나 수정본 빌드가 통과했으면 확인하지 못한 변경이 없으므로 조치 전 빌드 실패를 사유로 두지 않는다
            reasons = [x for x in reasons if x != R_BASELINE_BUILD_FAILED]
            status = status if reasons else SUCCEEDED
            reason = reasons[0] if reasons else ""
        elif self.baseline_failed_tolerated:
            # 빌드로 확인한 변경이 아니므로 성공으로 끝내지 않는다. 실패가 아니면 조치 전 빌드 실패를 첫 사유로 둔다
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
        """지금 delivery/result.zip이 라운드 ``r`` 이전에 빌드, 확정,
        재검사(완료, 같은 sha256)까지 마친 결과 ZIP인지 확인한다."""
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
        이전 라운드에서 빌드, 확정, 재검사까지 마친 결과 ZIP이 있으면 남기고, 없으면 결과 ZIP이 없는 상태가 된다.

        Discard round `r`'s candidate; an earlier round's built, committed and rescanned delivery stays."""
        if self.verified_earlier(r):
            self.journal({"event": "earlier_delivery_kept", "round": r, "delivery_round": self.state.delivery.round})
            return
        self.discard_delivery()

    # -- candidate completeness marker
    def _ready_path(self, r: int) -> Path:
        return self.paths.round_dir(r) / CANDIDATE_READY

    def mark_candidate(self, r: int) -> None:
        """라운드 ``r``의 수정본이 복사와 적용을 모두 마쳤다고 기록한다. 내용은 적용한 제안 id 목록이다.

        Written after copy + apply (and the text-only check) of round `r`'s candidate: {round, applied}."""
        write_json_atomic(self._ready_path(r), {"round": r, "applied": list(self.round_state(r).applied)})

    def unmark_candidate(self, r: int) -> None:
        """수정본을 지우거나 다시 만들기 전에 완료 표시를 지운다."""
        try:
            self._ready_path(r).unlink()
        except FileNotFoundError:
            pass

    def candidate_vouched(self, r: int) -> bool:
        """라운드 ``r``의 수정본이 상태에 기록된 적용 목록 그대로 완성된 트리인지 확인한다.

        The candidate exists and its completeness marker names exactly the state's `applied` of round `r`."""
        data = read_json(self._ready_path(r))
        return (isinstance(data, dict) and data.get("applied") == list(self.round_state(r).applied)
                and self.paths.candidate(r).is_dir())

    def deliver_failed_candidate(self, r: int) -> str | None:
        """정책이 include일 때 빌드에 실패한 라운드 ``r``의 수정본 트리를 결과 ZIP으로
        확정한다(build_ok=False, scanned=False). accepted/는 바꾸지 않는다.
        이전 라운드의 검증된 결과 ZIP이 있으면 사람이 고를 수 있도록 ``delivery/result-r<k>-verified.zip``(+
        ``.json``)으로 옮기고 그 경로(실행 폴더 기준)를 반환한다. 없으면 빈 문자열을 반환한다.
        수정본이 완성됐다는 표시가 없으면 아무것도 확정하지 않고 None을 반환한다.

        Policy include: freeze the failed candidate tree as the delivery; keep an earlier verified ZIP beside it.
        None (nothing frozen, nothing moved) when the state does not vouch for the candidate (`candidate_vouched`)."""
        st, p = self.state, self.paths
        if not self.candidate_vouched(r):
            self.journal({"event": "candidate_not_vouched", "round": r})
            return None
        kept = ""
        if self.verified_earlier(r):
            d = st.delivery
            vz = verified_zip_path(p, d.round)
            os.replace(p.delivery_zip, vz)
            info = DeliveryInfo.from_dict(d.to_dict())
            info.path, info.scanned = self.rel(vz), True
            write_json_atomic(vz.with_suffix(".json"), info.to_dict())
            kept = self.rel(vz)
            self.journal({"event": "verified_delivery_kept", "round": r, "delivery_round": d.round, "file": kept,
                          "sha256": d.sha256})
        else:
            self.discard_delivery()
        info = self.c.intake.freeze_delivery(p.candidate(r), p.delivery_zip, r, self.paths_list)
        info.build_ok, info.scanned = False, False
        st.delivery = info
        write_json_atomic(p.delivery_json, info.to_dict())
        self.journal({"event": "failed_candidate_delivered", "round": r, "sha256": info.sha256})
        return kept

    def mark_scanned(self) -> None:
        """재검사가 완료됐고 검사한 ZIP의 sha256이 지금 결과 ZIP과 같으면 ``delivery.scanned``를 켜고, 아니면 끈다."""
        d = self.state.delivery
        if d is None:
            return
        final = self.load_scan(self.state.final_scan)
        d.scanned = bool((d.build_ok or d.build_tolerated) and final is not None and final.complete
                         and final.zip_sha256 == d.sha256)

    # -- data helpers
    def load_scan(self, rel: str) -> ScanResult | None:
        if not rel:
            return None
        data = read_json(self.paths.root / rel)
        return ScanResult.from_dict(data) if isinstance(data, dict) else None

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
        """라운드 n에서 비교 기준이 되는 이전 빌드 트리를 반환한다.
        그 전에 확정된 마지막 수정본이고, 없으면 조치 전 빌드에 쓴 트리다."""
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
        """``state.counts``를 채운다. 취약점 수는 검사 결과의 행에서, 남은 항목 수는 직접 확인 / 사내 항목의 행에서 센다.

        What each count counts:
        - `baseline_findings` / `final_findings`: actionable rows of the baseline / final scan (IVAS findings, not
          inventory items), so a finding whose library was split into an in-house item and a project item is one row;
          `*_ignored`: the rows of an `ignore_actions` code.
        - `fixed`: baseline actionable findings (kind, CVE id, identifier compared case-insensitively) absent from the
          complete final scan: a finding still reported for any module is not fixed, whichever part was changed.
        - `residual`: residual ROWS in the state (one per g:a, version, reason after `_merge_residuals`); a library split
          into an in-house part and a project part left for review is two rows, one per section.
        - `residual_inhouse`: the `inhouse_artifact` rows among them (one per in-house item).
        - `proposals*`: proposals of every round, applied / rejected / pending; a variant override is its own proposal."""
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
            "proposals_rejected": sum(1 for p in props if p.decision == D_REJECTED),
            "proposals_pending": sum(1 for p in props if p.decision == D_PENDING),
            # 사내 아티팩트 담당자가 처리해야 할 항목 수.
            # 보고서가 담당자 연락 안내와 함께 묶어 보여 주며, 없으면 0이다
            "residual_inhouse": sum(1 for x in st.residuals if x.reason == R_INHOUSE_ARTIFACT),
        }
        # *_findings는 조치 대상만 세고, ignore_actions 코드의 항목은 *_ignored로 따로 센다
        base_act: list[Any] = []
        if base is not None and base.complete:
            base_act, base_ign = self.split(base)
            counts["baseline_findings"] = len(base_act)
            counts["baseline_ignored"] = len(base_ign)
        if final is not None and final.complete:
            final_act, final_ign = self.split(final)
            counts["final_findings"] = len(final_act)
            counts["final_ignored"] = len(final_ign)
            if base is not None and base.complete:
                def k(f: Any) -> str:
                    # IVAS가 같은 라이브러리를 다른 대소문자로 보고해도 같은 취약점으로 센다. 보고서와 같은 키를 쓴다
                    return finding_key(f.kind, f.id, f.identifier)
                left = {k(f) for f in final_act}
                counts["fixed"] = len({k(f) for f in base_act} - left)
        st.counts = counts

    # -- action codes (remediation.ignore_actions)
    @property
    def ignored_codes(self) -> frozenset[str]:
        from .ivas import ignore_set
        return ignore_set(getattr(self.rcfg, "ignore_actions", None))

    def split(self, scan: ScanResult) -> tuple[list[Any], list[Any]]:
        """검사 결과를 (조치 대상, 제외 항목)으로 나눈다.
        제외 항목(``ignore_actions`` 코드, 기본값은 없음)은 검사 파일과 보고서에는 남지만 인벤토리, 계획,
        남은 취약점 수에는 들어가지 않는다. 표에 없거나 빈 코드는 조치 대상으로 보고 저널에 한 번만 남긴다.

        (actionable, ignored) findings of a scan; unknown / empty codes stay actionable and are journaled once."""
        from .ivas import split_findings, unknown_actions
        act, ign = split_findings(list(scan.findings), self.ignored_codes)
        new = [c for c in unknown_actions(list(scan.findings)) if c not in self._unknown_noted]
        if new:
            self._unknown_noted.update(new)
            log.info("IVAS action codes outside the known table treated as actionable: %s", ", ".join(new))
            self.journal({"event": "ivas_action_unknown", "codes": new, "label": scan.label})
        return act, ign

    # -- the original ZIP as the delivery (nothing changed)
    def deliver_original(self) -> None:
        """바꾼 것이 없을 때 원본 ZIP의 바이트를 그대로 결과 ZIP으로 두고, 재검사 결과는 조치 전 검사 결과로 둔다."""
        st, p = self.state, self.paths
        p.delivery_zip.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p.original_zip, p.delivery_zip)
        sha = sha256_file(p.delivery_zip)
        st.delivery = DeliveryInfo(path=self.rel(p.delivery_zip), sha256=sha, size=p.delivery_zip.stat().st_size,
                                   files=st.intake.files if st.intake else 0,
                                   tree_hash=st.intake.tree_hash if st.intake else "", round=0)
        if self.baseline_failed_tolerated:
            # 원본도 빌드되지 않았으므로 빌드로 확인한 결과 ZIP이 아니다
            st.delivery.build_ok, st.delivery.build_tolerated = False, True
        st.final_scan = st.baseline_scan
        # 결과 ZIP이 원본과 같은 바이트이므로 같은 sha256을 빠짐없이 검사했다면 scanned로 본다
        self.mark_scanned()
        write_json_atomic(p.delivery_json, st.delivery.to_dict())

    # -- one IVAS scan with the upload-safety rules
    def scan(self, node: str, label: str, zip_path: Path, sha: str, project_name: str) -> ScanResult | None:
        """IVAS 검사를 한 번 실행한다. 검사 결과를 반환하고, 대기 상태로 멈췄으면 None을 반환한다.
        업로드 전에 ``pending_upload``를, 접수 정보를 받으면 ``pending_receipt``를 저장하고 끝나면 둘 다 지운다. 접수
        정보가 있으면 다시 올리지 않고 이어서 조회하며, 결과를 모르는 업로드가 있으면 확인을 기다린다.

        One scan with the upload rules (pending_upload → pending_receipt → cleared). None = halted."""
        st = self.state
        receipt: UploadReceipt | None = None
        if label in st.pending_receipt:
            receipt = UploadReceipt.from_dict(st.pending_receipt[label])
            self.journal({"event": "scan_resume", "label": label, "analysis": receipt.analysis_id})
        elif label in st.pending_upload:
            # 이전 업로드 결과를 모르므로, IVAS에 같은 ZIP이 두 번 들어가지 않도록 자동으로 다시 올리지 않는다
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
            # 원격 분석은 IVAS에서 계속 진행되므로 접수 정보를 남겨, 재개할 때 다시 올리지 않고 이어서 조회한다
            if res.receipt is not None:
                st.pending_receipt[label] = res.receipt.to_dict()
                st.pending_upload.pop(label, None)
            elif label not in st.pending_receipt:
                st.pending_upload.pop(label, None)      # 접수 정보도 불확실 예외도 없으면 올리기 전에 멈춘 것이다
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
        return res


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
    # 따로 복사한 트리에서 빌드한다. accepted/에 target/이 생기지 않아야 결과 ZIP의 파일 목록이 원본과 같다
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
            # 취약점 검사는 오픈소스 라이브러리를 보므로 빌드 오류와 상관없이 이어 간다. 시간 초과나 실행 실패는 비교할 로그가 없다
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
    sha = st.intake.zip_sha256 if st.intake else ""
    # 올리기 전에 원본이 접수 때와 같은 바이트인지 확인한다.
    # 접수 정보로 이어서 조회할 때는 이미 올린 뒤이므로 확인하지 않는다
    if BASELINE_LABEL not in st.pending_receipt and sha256_file(p.original_zip) != sha:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "the original ZIP no longer has the sha256 recorded at intake")
    name = f"{h.rcfg.ivas_project_prefix}-{st.id}-{BASELINE_LABEL}"
    res = h.scan("baseline_scan", BASELINE_LABEL, p.original_zip, sha, name)
    if res is None:
        return None
    st.baseline_scan = h.rel(p.scan(BASELINE_LABEL))
    if R_ZIP_HASH_MISMATCH in res.incomplete_reasons:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "IVAS analysed bytes whose sha256 differs from the original ZIP")
    if not res.complete:
        # 끝나지 않은 검사를 0건으로 보지 않으며 계획도 만들지 않는다
        return h.finish(INCOMPLETE, R_SCAN_INCOMPLETE, "baseline scan incomplete: " + ", ".join(res.incomplete_reasons))
    if res.zip_sha256 != sha:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "IVAS scanned a ZIP whose sha256 differs from the original")
    actionable, _ignored = h.split(res)
    if not actionable:
        # 조치 대상이 없으면(0건이거나 모두 ignore_actions 코드) 바꿀 것이 없으므로 원본을 그대로 전달한다
        h.deliver_original()
        return "evaluate"
    st.round = max(st.round, 1)
    return "inventory"


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
        # 읽을 수 없거나 끝나지 않은 검사를 0건으로 보지 않으므로, 빈 목록으로 계획하지 않는다
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
    """현재 서버 정책의 해시를 반환한다.
    계획 함수가 ``policy_hash(cfg)``를 제공하면 그 값을, 아니면 작업에 저장된 값을 쓴다.
    계획을 다시 만들지 않고도 기존 계획이 낡았는지 판단하는 데 쓴다."""
    fn = getattr(h.c.plan, "policy_hash", None)
    if callable(fn):
        try:
            return str(fn(h.cfg) or "")
        except Exception:  # noqa: BLE001 - 해시를 구하지 못하면 작업에 저장된 값을 쓴다
            log.debug("policy hash of the plan function unavailable", exc_info=True)
    return h.state.policy_hash


def _residual_key(x: Residual) -> tuple[str, str, str]:
    return (ga_key(x.ga), version_key(x.version), x.reason)


def _merge_residuals(st: RemediationState, residuals: list[Residual]) -> None:
    """이번 라운드에서 조치하지 못한 항목을 작업 상태에 합친다. (ga, 버전, 사유)마다 한 항목으로 둔다.
    g:a와 버전은 대소문자를 구분하지 않는다. 이미 있는 항목이면 모듈, 사내 아티팩트, 모듈별 출처는 합치고,
    경로와 IVAS 표기는 비어 있을 때만 채운다.

    Merge a round's residuals into the state, one per (ga, version, reason) compared case-insensitively; facts of a
    repeat are unioned (sources by module + variant)."""
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


def _rejected_key(p: Proposal) -> tuple[str, ...]:
    """거절된 제안을 planner의 ``rejected`` 키로 바꾼다. 형식 / classifier가 있는 override는 그 변종까지 담는다.
    효과가 없어 뺀 항목은 그 pom까지 담아, 같은 실행에서 그 pom에만 다시 제안하지 않는다.

    (ga, to[, variant]) for a rejected proposal; (ga, to, variant, pom) for an override entry dropped as ineffective
    (`D_INEFFECTIVE`), so only that pom's entry is never proposed again in this run."""
    from .apply import override_variant
    variant = override_variant(p)
    if p.decision == D_INEFFECTIVE:
        pom = next((e.pom for e in p.edits if e.op == "insert"), "")
        return (p.ga, p.to_version, variant, pom)
    return (p.ga, p.to_version, variant) if variant else (p.ga, p.to_version)


def _inventory_items(h: _Holder, r: int) -> dict[tuple[str, str], Any]:
    """라운드의 인벤토리 항목을 (g:a, 버전) 비교 키로 찾을 수 있게 반환한다. 파일이 없으면 빈 dict다."""
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
        # 서버 정책이 바뀌었으면 지금 정책으로 다시 계획하고, 이전 정책으로 승인한 수정본은 다시 쓰지 않는다
        h.journal({"event": "policy_changed", "round": r})
        st.policy_hash = current
    existing = h.load_plan(r)
    # 사람이 결정한 계획을 정책 변경 때문에 버리면 그 이유를 state.message로 알린다
    decided = existing is not None and any(x.decision in (D_APPROVED, D_REJECTED) for x in existing.proposals)
    reuse = (existing is not None and existing.source_tree_hash == tree_hash and existing.policy_hash == st.policy_hash
             and all(x.source_tree_hash == tree_hash and x.policy_hash == st.policy_hash for x in existing.proposals))
    if reuse:
        plan = existing
        h.journal({"event": "plan_reused", "round": r, "proposals": len(plan.proposals)})
    else:
        rejected = {_rejected_key(x) for k in range(1, r) if (pl := h.load_plan(k)) is not None
                    for x in pl.proposals if x.decision in (D_REJECTED, D_INEFFECTIVE)}
        if existing is not None:
            # 트리나 정책 해시가 달라진 낡은 계획은 승인할 수 없으므로 기록으로만 남기고 새로 만든다
            rejected |= {_rejected_key(x) for x in existing.proposals if x.decision in (D_REJECTED, D_INEFFECTIVE)}
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
    replanned = policy_changed and decided and not reuse
    if replanned:
        h.journal({"event": "approvals_dropped", "round": r, "reason": "policy_changed"})
        log.warning("remediation %s round %s: the server policy changed since the proposals were approved; planned again",
                    st.id, r)
        st.message = POLICY_CHANGED_NOTE
    if plan.pending():
        waiting = f"{len(plan.pending())} proposal(s) of round {r} wait for approval"
        h.halt(AWAITING_APPROVAL, R_PENDING_APPROVAL, "plan",
               f"{POLICY_CHANGED_NOTE} | {waiting}" if replanned else waiting)
        h.fill_counts()
        return None
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
    c.intake.copy_tree(p.accepted, cand, h.paths_list)
    to_apply = plan.to_apply()
    result = c.apply.apply_proposals(h.project(cand), to_apply)
    p.patch(r).parent.mkdir(parents=True, exist_ok=True)
    p.patch(r).write_text(result.patch or "", encoding="utf-8")
    write_json_atomic(p.round_dir(r) / "apply.json", result.to_dict())
    rs.applied = list(result.applied)
    by_id = {x.id: x for x in plan.proposals}
    # 건너뛴 제안은 적용되지 않았으므로 성공으로 볼 수 없어 직접 확인 항목으로 남긴다
    # 모듈별 출처와 IVAS 표기는 이 라운드의 인벤토리 항목에서 가져온다
    items = _inventory_items(h, r) if result.skipped else {}
    skipped: list[Residual] = []
    for pid, why in sorted(result.skipped.items()):
        prop = by_id.get(pid)
        it = items.get((ga_key(prop.ga), version_key(prop.from_version))) if prop is not None else None
        skipped.append(Residual(ga=prop.ga if prop else pid, version=prop.from_version if prop else "",
                                origin="proposal", reason=R_MANUAL_REVIEW, detail=f"proposal {pid} not applied: {why}",
                                finding_ids=list(prop.finding_ids) if prop else [],
                                modules=sorted(set(prop.modules) | set(prop.also_modules)) if prop else [],
                                # 제안이 다루는 모듈의 출처가 있으면 그것을, 없으면(예전 계획) 인벤토리 항목의 것을 쓴다
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
    # verify_text_only는 프로젝트 폴더만 보므로, ZIP이 최상위 폴더로 감싸였을 때 그 밖의 파일(예:
    # __MACOSX/)은 여기서 확인한다
    outside = h.outside_paths
    if outside and c.intake.tree_hash(p.accepted, outside) != c.intake.tree_hash(cand, outside):
        violations.append("a file outside the project folder differs from the accepted tree")
    if violations:
        rs.verify_problems = violations
        h.discard_candidate(r)
        return h.finish(FAILED, R_TEXT_ONLY_VIOLATION, "; ".join(violations[:5]))
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
    os.replace(acc, old)
    os.replace(new, acc)
    _safe_rmtree(old)


def _recover_accepted(paths: RunPaths) -> None:
    """두 번의 이름 바꾸기 사이에 서버가 내려갔을 때 accepted/ 폴더를 복구한다."""
    acc = paths.accepted
    new, old = acc.with_name(acc.name + ".new"), acc.with_name(acc.name + ".old")
    if not acc.exists():
        if new.exists() and old.exists():
            os.replace(new, acc)            # the new tree was complete (copied before the first rename)
        elif old.exists():
            os.replace(old, acc)
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
        # 정책이 include면 실패한 수정본도 결과 ZIP으로 내보내지만, 성공일 수 없으므로 재검사는 하지 않는다
        # accepted/, 원본 ZIP, candidate.patch, build.log는 그대로 남는다
        kept = h.deliver_failed_candidate(r)
        if kept is None:
            # 완성됐다는 표시가 없는 수정본은 잘린 트리일 수 있으므로 결과 ZIP으로 내보내지 않는다
            h.discard_candidate(r)
            return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED,
                            f"{CANDIDATE_NOT_VOUCHED.format(r=r)} | {outcome.summary}")
        ids = ", ".join(rs.applied) or "-"
        msg = (f"delivery/result.zip contains the round {r} change set whose candidate build FAILED "
               f"(proposals: {ids}; build log {h.rel(p.build_log(r))}); it was not rescanned; "
               "the original is preserved as original/source.zip")
        if kept:
            msg += f"; the verified delivery of an earlier round is kept as {kept}"
        return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED, f"{msg} | {outcome.summary}")
    # 정책이 discard면 실패한 수정본은 결과 ZIP에 넣지 않는다.
    # accepted/는 그대로 두고 candidate.patch는 남긴다
    # 이전 라운드에서 검증을 마친 결과 ZIP이 있으면 그것은 남는다
    h.discard_candidate(r)
    msg = outcome.summary
    if st.delivery is not None:
        msg = (f"round {r} candidate discarded; delivery/result.zip is the verified state of round "
               f"{st.delivery.round}, without the failed change | {msg}")
    return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED, msg)


def _baseline_errors_only(h: _Holder, r: int, outcome: Any, cand: Path) -> bool:
    """조치 전 빌드가 실패한 실행에서 실패한 수정본 빌드를 조치 전 빌드와 비교하고, 새 오류가 없으면 True를 반환한다.

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
    """수정본 빌드 실패를 정책대로 끝내고, 조치 전 빌드와 비교한 결과가 있으면 그 문장을 메시지 앞에 붙인다."""
    nxt = _build_failed(h, r, outcome)
    extra = _new_errors_text(h.round_state(r).build_compare)
    if extra and nxt == "report":
        h.state.message = f"{extra} | {h.state.message}" if h.state.message else extra
    return nxt


def _verify(h: _Holder, r: int, cand: Path, proposals: list[Proposal]) -> list[str]:
    """수정본의 의존성을 다시 해석하고 제안과 비교한 검증 줄을 반환한다."""
    c, p = h.c, h.paths
    after = c.maven.resolve(h.project(cand), p.round_dir(r) / "work-after", **h.target_kw)
    write_json_atomic(p.resolved(r), after.to_dict())
    packaged = list(c.maven.packaged(h.project(cand), **h.target_kw))
    before_data = read_json(p.round_dir(r) / "before.json")
    before = ResolvedModel.from_dict(before_data) if isinstance(before_data, dict) else ResolvedModel(complete=False)
    return list(c.apply.verify_resolution(proposals, before, after, packaged))


def _build(h: _Holder) -> str | None:
    st, c, p = h.state, h.c, h.paths
    r = st.round
    rs = h.round_state(r)
    cand = p.candidate(r)
    if not h.candidate_vouched(r):
        # 복사나 적용 도중에 파드가 끝나 수정본이 덜 만들어졌을 수 있으므로 accepted/에서 다시 만든다
        h.journal({"event": "candidate_rebuilt", "round": r})
        log.warning("remediation round %s: the candidate has no completeness marker; it is made again from accepted/", r)
        return "apply"
    outcome = h.build_outcome(c.maven.package(h.project(cand), p.build_log(r), h.stop, **h.target_kw), p.build_log(r))
    rs.build, rs.build_compare, rs.build_tolerated = outcome, {}, False
    if not outcome.ok and not _baseline_errors_only(h, r, outcome, cand):
        return _candidate_failed(h, r, outcome)
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
            # 이번 라운드에서 이미 한 번 빼고 다시 만든 수정본이다(중단 뒤 재개).
            # 중단이 없었을 때처럼 두 번째 검증 문제는 실패로 끝낸다
            h.discard_candidate(r)
            return h.finish(FAILED, R_RESOLUTION_MISMATCH,
                            "; ".join(problems[:5]) + f" | after removing the ineffective override entr(ies) "
                            f"{', '.join(dropped)}")
        if drop and not other:
            return _drop_ineffective(h, r, applied, drop, problems)
        h.discard_candidate(r)
        return h.finish(FAILED, R_RESOLUTION_MISMATCH, "; ".join(problems[:5]))
    return _commit_round(h, r, cand)


def _commit_round(h: _Holder, r: int, cand: Path) -> str:
    rs = h.round_state(r)
    _commit(h, cand)
    rs.committed = True
    h.journal({"event": "round_committed", "round": r, "applied": list(rs.applied)})
    return "freeze_delivery"


def _drop_ineffective(h: _Holder, r: int, applied: list[Proposal], drop: list[Proposal],
                      problems: list[str]) -> str | None:
    """효과가 없던 override 항목을 빼고 수정본을 다시 만들어 한 번 더 빌드하고 검증한다.
    다시 만들 시간이 없거나 두 번째 검증도 실패하면 예전처럼 ``resolution_mismatch``로 끝낸다.

    The only problems of the round are inserted override entries whose governed modules still resolve the old
    version (`apply.split_ineffective`). They are dropped: plan decision `ineffective` (`D_INEFFECTIVE`), one
    `manual_review` residual each, journal `override_ineffective`; the candidate is rebuilt from accepted/ with the
    remaining proposals only (byte for byte what applying them alone gives) and built + verified ONCE more. Nothing
    left → the round ends like a round with nothing to apply. A stop / used-up time budget before the rebuild, a
    failed second build or a second verification problem → today's outcome."""
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
    # 상태는 (g:a, 버전, 사유)마다 항목 하나이므로 같은 라이브러리의 뺀 항목은 pom을 모두 적은 항목 하나로 만든다
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
    rs.applied = [x.id for x in keep]
    # 수정본을 지우기 전에 결정한 내용(남길 항목, 적용 목록)을 상태에 저장한 뒤 plan.json에 ineffective를 쓴다.
    # 다음 노드를 apply로 두므로 어디서 파드가 끝나도 재개하면 accepted/에서 수정본을 다시 만든다.
    # 상태 저장과 plan.json 사이에서 끝나면 apply가 뺄 제안까지 다시 적용하고 build가 같은 판단을 다시 한다
    h.unmark_candidate(r)
    st.next_node = "apply"
    h.save()
    if plan is not None:
        for x in plan.proposals:
            if x.id in gone:
                x.decision = D_INEFFECTIVE
        write_json_atomic(p.plan(r), plan.to_dict())
    _safe_rmtree(cand)
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
    h.mark_candidate(r)
    outcome = h.build_outcome(c.maven.package(h.project(cand), p.build_log(r), h.stop, **h.target_kw), p.build_log(r))
    rs.build, rs.build_compare, rs.build_tolerated = outcome, {}, False
    if not outcome.ok and not _baseline_errors_only(h, r, outcome, cand):
        return _candidate_failed(h, r, outcome)
    lines = _verify(h, r, cand, keep)
    rs.verify_problems = lines
    again = [ln for ln in lines if not ln.startswith("info:")]
    if again:
        h.discard_candidate(r)
        return h.finish(FAILED, R_RESOLUTION_MISMATCH,
                        "; ".join(again[:5]) + f" | after removing the ineffective override entr(ies) {ids}")
    return _commit_round(h, r, cand)


def _freeze_delivery(h: _Holder) -> str | None:
    st, p = h.state, h.paths
    if p.delivery_zip.exists():
        p.delivery_zip.unlink()
    st.delivery = h.c.intake.freeze_delivery(p.accepted, p.delivery_zip, st.round, h.paths_list)
    if h.baseline_failed_tolerated and any(rs.committed and rs.build_tolerated for rs in st.rounds):
        # 조치 전과 같은 오류로 빌드가 실패한 변경이 들어 있으므로 빌드로 확인한 결과 ZIP이 아니다
        st.delivery.build_ok, st.delivery.build_tolerated = False, True
    write_json_atomic(p.delivery_json, st.delivery.to_dict())
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
    # 남은 취약점은 조치 대상만 센다.
    # ignore_actions 코드의 항목은 성공 여부와 다음 라운드 판단에 넣지 않는다
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
        # 조치 전 빌드 실패를 받아들인 실행은 바꾼 버전이 있을 때만 이 사유를 둔다
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
    props = [x for pl in h.all_plans() for x in pl.proposals]
    if any(x.decision == D_PENDING for x in props):
        reasons.add(R_PENDING_APPROVAL)
    if any(x.decision == D_REJECTED for x in props):
        reasons.add(R_REJECTED_PROPOSALS)
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
    # 위험도 경계값. 화면 표시에만 쓰므로 정책 해시에 넣지 않고, 이전 설정 객체에 없으면 기본값을 쓴다
    severity = (getattr(h.rcfg, "severity_red", 9.0), getattr(h.rcfg, "severity_orange", 7.0),
                getattr(h.rcfg, "severity_yellow", 4.0))
    report_mod.write_report(h.state, h.paths, ignore_actions=sorted(h.ignored_codes),
                            inhouse_message=getattr(h.rcfg, "inhouse_message", None), severity=severity,
                            inhouse=inhouse_keys(h.cfg))
    return None


_NODE_FNS: dict[str, Callable[[_Holder], str | None]] = {
    "baseline_build": _baseline_build, "baseline_scan": _baseline_scan, "inventory": _inventory, "plan": _plan,
    "apply": _apply, "build": _build, "freeze_delivery": _freeze_delivery, "rescan": _rescan, "evaluate": _evaluate,
    "report": _report,
}


# ---------------------------------------------------------------------------- graph
def _wrap(name: str, h: _Holder) -> Callable[[RemediationGraphState], dict]:
    """노드 함수에 경계 처리를 덧붙인 함수를 반환한다.
    순서는 중지 확인 -> RUNNING 저장, node_start -> 노드 실행 -> node_end 저장이다. 예외가 나면 FAILED
    internal_error로 바꾸고 보고서 노드로 가며, 대기 상태로 멈추면 보고서 없이 그래프를 끝낸다."""
    fn = _NODE_FNS[name]

    def node(gs: RemediationGraphState) -> dict:
        st = h.state
        if name == "report":
            # 보고서는 끝 상태에서만 실행하고, 여기서 실패해도 보고서로 다시 돌아가지 않는다
            h.journal({"event": "node_start", "node": name, "round": st.round})
            # report.json이 저장되는 state.json과 같은 내용이 되도록 시각과
            # 노드를 먼저 정하고, 저장할 때 시각을 다시 찍지 않는다
            st.node, st.next_node, st.updated = name, "report", _now()
            try:
                fn(h)
            except Exception as e:  # noqa: BLE001 - 보고서 실패가 끝 상태를 덮어쓰지 않게 한다
                log.error("report node failed: %s", scrub_message(traceback.format_exc(), h.paths.root, 20000))
                st.message = (st.message + " | " if st.message else "") + \
                    scrub_message(f"report not written: {type(e).__name__}: {e}", h.paths.root)
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
            # 예외 메시지에 URL 자격 증명 같은 비밀 값이 있을 수 있으므로 traceback도 마스킹한 뒤에 로그에 기록한다
            log.error("remediation node %s failed: %s", name, scrub_message(traceback.format_exc(), h.paths.root, 20000))
            reason = e.code if isinstance(e, NotReady) else R_INTERNAL_ERROR
            nxt = h.finish(FAILED, reason, scrub_message(f"{type(e).__name__}: {e}", h.paths.root))
        h.journal({"event": "node_end", "node": name, "status": st.status, "next": nxt, "round": st.round})
        if nxt is None:                       # waiting status set by the node
            h.save()
            return {"node": name, "halt": True, "next": END, "round": st.round}
        st.next_node = nxt
        h.save()
        return {"node": name, "halt": False, "next": nxt, "round": st.round}

    node.__name__ = name
    return node


def build_graph(holder: Any) -> Any:
    """``holder``를 쓰는 StateGraph를 만들어 컴파일한다.
    진입 노드 ``route``가 ``state.next_node``로 바로 건너뛰어 재개하고, 그 뒤로 ``NODES``가 이어진다.

    Compiled `StateGraph(RemediationGraphState)`: a `route` entry that jumps to `state.next_node`, then `NODES`."""
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
    """작업 하나를 ``state.next_node``부터 끝 상태(SUCCEEDED / INCOMPLETE / FAILED)나 대기
    상태(AWAITING_APPROVAL / AWAITING_CONFIRMATION / STOPPED)에 이를 때까지 실행한다.
    노드 경계마다 ``save``를 호출하며, 이미 끝 상태면 아무것도 하지 않는다.

    Drive one remediation from `state.next_node` to a terminal or waiting status, saving at every node boundary."""
    if state.status in TERMINAL:
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


# ---------------------------------------------------------------------------- service helpers
def verified_zip_path(paths: RunPaths, round: int) -> Path:
    """정책 include로 실패한 수정본이 result.zip을 차지할 때, 이전 라운드
    ``round``의 검증된 ZIP을 옮겨 둘 경로를 반환한다.

    Where an earlier round's verified delivery is kept when a failed candidate takes result.zip."""
    return paths.delivery_zip.with_name(f"result-r{int(round)}-verified.zip")


def find_verified_zip(paths: RunPaths) -> Path | None:
    """``delivery/result-r<k>-verified.zip`` 경로를 찾아 반환한다.
    여럿이면 k가 가장 큰 것을, 없으면 None을 반환한다.

    The kept verified delivery ZIP of this run, if any (highest round)."""
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
    """접수가 끝난 작업의 첫 상태를 만든다.
    QUEUED 상태로 첫 라운드의 첫 노드부터 시작하며 정책 해시와 승인 설정을 저장한다.

    The first state of an accepted remediation (QUEUED, round 1, next node = the first node)."""
    now = _now()
    return RemediationState(id=remediation_id, name=name, status=QUEUED, created=now, updated=now, node="",
                            next_node=NODES[0], round=1, configs=list(configs or []), policy_hash=policy_hash,
                            require_approval=bool(cfg.remediation.require_approval), intake=intake)


def can_resume(state: RemediationState) -> bool:
    """작업을 재개할 수 있는 상태인지 확인한다. 대기, 중지, 중단 상태이고 다음 노드가 알려진 노드여야 한다.

    Whether `state` can be resumed (a resumable status and a known next node)."""
    return state.status in RESUMABLE and state.next_node in NODES


def default_components(cfg: Any, journal: Journal | None = None) -> Components:
    """운영용 구성 요소 묶음을 만든다.
    실제 IVAS, Maven, 저장소, 접수, 인벤토리, 계획, 적용 구현을 필요할 때 import해서 연결한다.
    ``journal``을 주면 Maven 게이트웨이가 실행 환경 경고(``maven_project_config``)를 그 저널에 기록한다.
    버전 근거는 승인된 호스트(``ServerMaven.allowed_hosts``)의 저장소에서 온 것만 인정한다.

    The real components (imported lazily so the graph module never depends on them at import time)."""
    from . import apply as apply_mod
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
    )
