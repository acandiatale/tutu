"""취약점 조치(remediation)의 독립 LangGraph — 이관 그래프(``sbmig/graph/engine.py``)와 별개의 흐름이다.
노드: baseline_build → baseline_scan → inventory → plan → apply → build → freeze_delivery → rescan → evaluate → report
(evaluate 에서 취약점이 남고 라운드 한도 안이면 inventory 로 돌아간다). 흐름상 위치: ``service.py`` 의 작업 스레드가
``run_remediation`` 을 부르고, 승인 대기·업로드 확인 대기·중지에서 돌아온 뒤 ``state.next_node`` 부터 다시 부른다(재개).
그래프 상태에는 작은 JSON 값(id, 노드, 라운드, 멈춤 여부)만 두고 ``RemediationState`` · 경로 · 부품은 노드 함수가 닫아 쥔
holder 에 있다. 부품은 ``Components`` 로만 받는다(테스트는 가짜, 운영은 ``default_components``).
지켜야 할 것: 노드 경계마다 state 저장; 기준 빌드가 실패하면 검사·계획으로 가지 않는다; 불확실한 업로드는 다시 올리지 않는다;
원격 IVAS 분석을 "취소됨" 이라고 쓰지 않는다; 후보는 격리 사본에서 빌드·검증된 뒤에만 accepted/ 로 확정한다; 전달 ZIP 과
같은 바이트(sha256)를 다시 검사한다; SUCCEEDED 는 evaluate 의 모든 조건(전달 ZIP 의 build_ok · scanned 포함)을 만족할 때만.
후보 빌드가 실패하면(``remediation.failed_candidate``): include(기본, 사용자 결정 2026-11-11) = 실패한 후보 트리를 결과 ZIP 으로
고정한다(build_ok=False, 재검사 없음, 상태 FAILED; 앞 라운드의 검증된 ZIP 은 ``delivery/result-r<k>-verified.zip`` 으로 남긴다);
discard = 실패한 후보는 ZIP 에 넣지 않는다. 글자 밖 변경·해석 불일치 후보는 어느 설정이든 내지 않는다.

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
from .models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
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

log = logging.getLogger("sbmig.remediations.graph")

ROUTE = "route"
BASELINE_LABEL = "baseline"
# 노드 한 번 = 그래프 한 걸음. 라운드마다 inventory … evaluate 7 걸음 — 넉넉히 둔다(LangGraph 기본 25 로는 모자라다)
RECURSION_BASE = 100
RECURSION_PER_ROUND = 20
# 성공이 아닌 이유의 순서(state.reason = 첫 번째): 결과를 믿을 수 없는 이유 → 사람이 결정할 것 → 남은 취약점
_REASON_ORDER = (R_SCAN_INCOMPLETE, R_ZIP_HASH_MISMATCH, R_CANDIDATE_BUILD_FAILED, R_PENDING_APPROVAL,
                 R_REJECTED_PROPOSALS, R_RESIDUAL_FINDINGS, R_MANUAL_REVIEW, R_NO_CANDIDATE_VERSION,
                 R_INHOUSE_ARTIFACT)
# 예외 문장에서 지울 비밀: 이름에 이런 낱말이 든 환경 변수의 값 + IVAS 아이디(브리프: 사용자·비밀번호·토큰 모두 비밀)
_SECRET_WORDS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "CREDENTIAL", "PRIVATE_KEY")
_SECRET_NAMES = ("IVAS_USER_ID", "IVAS_PASSWORD")
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^\s/@'\"<>]+@")
_MIN_SECRET_LEN = 4


class RemediationGraphState(TypedDict, total=False):
    """그래프 상태(작은 JSON 값만): 작업 id, 방금 돈 노드, 라운드, 멈춤 여부, 다음 노드.

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
    """사람이 읽을 문장(예외 문장 등)에서 비밀 값·URL 자격증명·실행 폴더의 절대 경로를 지운다. 예외 없음.

    Remove secret env values, URL userinfo and the run directory's absolute path from a message."""
    try:
        out = str(text)
        values = set()
        for k, v in os.environ.items():
            up = k.upper()
            if (up in _SECRET_NAMES or any(w in up for w in _SECRET_WORDS)) and v and len(v) >= _MIN_SECRET_LEN:
                values.add(v)
        for v in sorted(values, key=len, reverse=True):   # 긴 값부터 — 짧은 값이 긴 값의 일부만 지우지 않게
            out = out.replace(v, "***")
        out = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}***@", out)
        if root is not None:
            for r in {str(Path(root).resolve()), str(root)}:
                out = out.replace(r + os.sep, "").replace(r, ".")
        return _scrub.scrub(out)[:limit]
    except Exception:  # noqa: BLE001 — 지우다 실패하면 원문 대신 고정 문구
        return "[message withheld]"


def _safe_rmtree(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)


class _Holder:
    """노드 함수가 닫아 쥐는 무거운 객체 묶음(그래프 상태 밖)."""

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
        """manifest 경로를 프로젝트 루트 기준으로(``verify_text_only`` 용)."""
        pr = self.project_root
        if pr in ("", "."):
            return list(self.paths_list)
        prefix = pr.rstrip("/") + "/"
        return [p[len(prefix):] for p in self.paths_list if p.startswith(prefix)]

    @property
    def outside_paths(self) -> list[str]:
        """프로젝트 폴더 밖의 manifest 경로(ZIP 이 최상위 폴더로 싸였을 때 그 옆의 파일들)."""
        pr = self.project_root
        if pr in ("", "."):
            return []
        prefix = pr.rstrip("/") + "/"
        return [p for p in self.paths_list if not p.startswith(prefix)]

    @property
    def target_kw(self) -> dict[str, Any]:
        """Maven 게이트웨이에 넘길 빌드 대상(이관의 최종 빌드 대상 폴더들). 비어 있으면 넘기지 않는다(루트에서 한 번 — 예전 그대로)."""
        targets = list(getattr(self.state, "build_targets", None) or [])
        return {"targets": targets} if targets else {}

    def build_outcome(self, outcome: Any, log_path: Path) -> Any:
        """빌드 결과를 state 에 넣기 전에: 로그 경로는 실행 폴더 기준 상대 경로로(계약), 요약은 비밀·절대 경로를 지운다.
        게이트웨이가 다른 로그 파일을 알려 주면(빌드 대상이 여럿일 때 실패한 대상의 로그) 그것을 쓴다."""
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
        """대기 상태(승인·확인·중지)로 멈춘다 — 보고서 노드는 돌지 않는다."""
        st = self.state
        st.status, st.reason, st.next_node, st.message = status, reason, next_node, message
        st.reasons = [reason]

    def finish(self, status: str, reason: str, message: str = "", reasons: list[str] | None = None) -> str:
        """끝 상태(FAILED / INCOMPLETE / SUCCEEDED)를 정하고 보고서 노드로 간다."""
        st = self.state
        st.status, st.reason = status, reason
        st.reasons = list(reasons) if reasons is not None else ([reason] if reason else [])
        if message:
            st.message = message
        self.fill_counts()
        return "report"

    def discard_delivery(self) -> None:
        """실패한 후보 정책(discard): 전달 ZIP 을 남기지 않는다."""
        self.state.delivery = None
        for p in (self.paths.delivery_zip, self.paths.delivery_json):
            if p.exists():
                p.unlink()

    def verified_earlier(self, r: int) -> bool:
        """라운드 ``r`` 앞에서 빌드·확정·재검사(완료, 같은 sha256)까지 마친 전달 ZIP 이 지금 delivery/result.zip 인가."""
        st = self.state
        d = st.delivery
        committed = [rs.round for rs in st.rounds if rs.committed and rs.round < r]
        final = self.load_scan(st.final_scan)
        return bool(d is not None and d.build_ok and committed and d.round == max(committed) and final is not None
                    and final.complete and final.zip_sha256 == d.sha256 and self.paths.delivery_zip.is_file()
                    and sha256_file(self.paths.delivery_zip) == d.sha256)

    def discard_candidate(self, r: int) -> None:
        """라운드 ``r`` 의 후보를 버린다(정책 discard, 또는 글자 밖 변경·해석 불일치). 앞 라운드에서 이미 빌드·확정·재검사
        (완료, 같은 sha256)까지 마친 전달 ZIP 이 있으면 그것은 남긴다 — 그 바이트는 검증을 마친 상태이고 실패한 변경은 들어 있지
        않다. 없으면(라운드 1) 전달 ZIP 없음.

        Discard round `r`'s candidate; an earlier round's built, committed and rescanned delivery stays."""
        if self.verified_earlier(r):
            self.journal({"event": "earlier_delivery_kept", "round": r, "delivery_round": self.state.delivery.round})
            return
        self.discard_delivery()

    def deliver_failed_candidate(self, r: int) -> str:
        """정책 include: 빌드가 실패한 라운드 ``r`` 의 후보 트리를 결과 ZIP 으로 고정한다(build_ok=False, scanned=False).
        accepted/ 는 바꾸지 않는다(마지막 확정 상태 그대로). 앞 라운드의 검증된 전달 ZIP 이 있으면 지우지 않고
        ``delivery/result-r<k>-verified.zip`` (+ ``.json``) 으로 옮겨 둔다 — 사람이 검증된 쪽을 고를 수 있게. 돌려주는 값 = 옮긴
        파일의 실행 폴더 기준 경로(없으면 빈 글자).

        Policy include: freeze the failed candidate tree as the delivery; keep an earlier verified ZIP beside it."""
        st, p = self.state, self.paths
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
        """최종 검사가 완료됐고 그 ZIP sha256 이 지금 전달 ZIP 과 같으면 ``delivery.scanned`` 를 켠다(아니면 끈다)."""
        d = self.state.delivery
        if d is None:
            return
        final = self.load_scan(self.state.final_scan)
        d.scanned = bool(d.build_ok and final is not None and final.complete and final.zip_sha256 == d.sha256)

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
        """라운드 n 의 "이전" 빌드 트리: 그 전에 확정된 마지막 후보(빌드됨), 없으면 기준 빌드 사본."""
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
            # 사내 아티팩트 담당자 몫의 잔여(보고서가 '예외 처리 가능, 담당자 연락' 으로 묶는다); 없으면 0
            "residual_inhouse": sum(1 for x in st.residuals if x.reason == R_INHOUSE_ARTIFACT),
        }
        # *_findings = 조치 대상만(ignore_actions 코드는 *_ignored 로 따로 센다)
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
                    return f"{f.kind}|{f.id}|{f.identifier}"
                left = {k(f) for f in final_act}
                counts["fixed"] = len({k(f) for f in base_act} - left)
        st.counts = counts

    # -- action codes (remediation.ignore_actions)
    @property
    def ignored_codes(self) -> frozenset[str]:
        from .ivas import ignore_set
        return ignore_set(getattr(self.rcfg, "ignore_actions", None))

    def split(self, scan: ScanResult) -> tuple[list[Any], list[Any]]:
        """검사의 (조치 대상, 조치 대상에서 뺀 항목). 뺀 항목(``ignore_actions`` 코드, 기본은 없음)은 검사 파일·보고서에는 남지만
        인벤토리·계획에 가지 않고 남은 취약점으로 세지 않는다. 표에 없는 코드·빈 코드는 조치 대상 — 한 번만 저널에 남긴다.

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
        """바꾼 것이 없을 때: 전달 ZIP = 원본 ZIP 의 바이트 사본, 최종 검사 = 기준 검사."""
        st, p = self.state, self.paths
        p.delivery_zip.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p.original_zip, p.delivery_zip)
        sha = sha256_file(p.delivery_zip)
        st.delivery = DeliveryInfo(path=self.rel(p.delivery_zip), sha256=sha, size=p.delivery_zip.stat().st_size,
                                   files=st.intake.files if st.intake else 0,
                                   tree_hash=st.intake.tree_hash if st.intake else "", round=0)
        st.final_scan = st.baseline_scan
        # 전달 ZIP = 원본 바이트, 최종 검사 = 그 바이트의 기준 검사 → 같은 sha256 을 완전히 검사했다면 scanned
        self.mark_scanned()
        write_json_atomic(p.delivery_json, st.delivery.to_dict())

    # -- one IVAS scan with the upload-safety rules
    def scan(self, node: str, label: str, zip_path: Path, sha: str, project_name: str) -> ScanResult | None:
        """IVAS 검사 한 번. 업로드 전 ``pending_upload`` 기록·저장, 영수증을 받으면 ``pending_receipt`` 로 옮겨 저장, 끝나면 둘 다
        지운다. 영수증이 있으면 올리지 않고 이어서 조회하고, 결과를 모르는 업로드가 있으면 올리지 않고 확인을 기다린다.
        None = 멈춤(대기 상태가 정해졌다).

        One scan with the upload rules (pending_upload → pending_receipt → cleared). None = halted."""
        st = self.state
        receipt: UploadReceipt | None = None
        if label in st.pending_receipt:
            receipt = UploadReceipt.from_dict(st.pending_receipt[label])
            self.journal({"event": "scan_resume", "label": label, "analysis": receipt.analysis_id})
        elif label in st.pending_upload:
            # 지난 업로드의 결과를 모른다 — 자동으로 다시 올리면 IVAS 에 같은 ZIP 이 두 번 들어갈 수 있다(회사 규칙: 다시 올리지 않음)
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
            # 원격 분석은 IVAS 에서 계속 돈다 — 영수증을 남겨 재개 때 다시 올리지 않고 이어서 조회한다
            if res.receipt is not None:
                st.pending_receipt[label] = res.receipt.to_dict()
                st.pending_upload.pop(label, None)
            elif label not in st.pending_receipt:
                st.pending_upload.pop(label, None)      # 영수증도 불확실 예외도 없음 = 올리기 전에 멈췄다
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
        return h.finish(FAILED, R_BOUNDARY_REFUSED,
                        "; ".join(f"{b.code}: {b.path}" for b in refused))
    # 격리된 사본에서 빌드한다 — accepted/ 에는 target/ 이 생기지 않는다(전달 ZIP 의 파일 집합 = 원본)
    tree = h.baseline_tree
    _safe_rmtree(tree)
    c.intake.copy_tree(h.paths.accepted, tree, h.paths_list)
    outcome = h.build_outcome(c.maven.package(h.project(tree), h.paths.baseline_log, h.stop, **h.target_kw), h.paths.baseline_log)
    st.baseline_build = outcome
    if not outcome.ok:
        if h.stop():
            h.halt(STOPPED, R_STOP_REQUESTED, "baseline_build", "stop requested during the baseline build")
            return None
        return h.finish(FAILED, R_BASELINE_BUILD_FAILED, outcome.summary)
    return "baseline_scan"


def _baseline_scan(h: _Holder) -> str | None:
    st, p = h.state, h.paths
    sha = st.intake.zip_sha256 if st.intake else ""
    # 올리기 전에 원본이 접수 때 그 바이트인지 본다(영수증으로 이어서 조회할 때는 이미 올린 뒤라 다시 볼 필요가 없다)
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
        # 미완료 검사는 0건이 아니다 — 계획을 만들지 않는다
        return h.finish(INCOMPLETE, R_SCAN_INCOMPLETE, "baseline scan incomplete: " + ", ".join(res.incomplete_reasons))
    if res.zip_sha256 != sha:
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "IVAS scanned a ZIP whose sha256 differs from the original")
    actionable, _ignored = h.split(res)
    if not actionable:
        # 조치 대상이 없다(0건, 또는 모두 ignore_actions 코드): 바꿀 것이 없으니 원본 그대로를 전달한다
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
        # 읽을 수 없거나 미완료인 검사는 0건이 아니다 — 빈 발견 목록으로 계획하지 않는다
        return h.finish(INCOMPLETE, R_SCAN_INCOMPLETE, "the scan this round plans from is missing or incomplete")
    # ignore_actions 코드(기본은 없음; 예 PC 패치 완료 · IG 조치 예외)의 항목은 인벤토리·계획에 넘기지 않는다
    findings, _ignored = h.split(scan)
    inv = c.inventory(h.project(p.accepted), findings, before, inhouse=inhouse_keys(h.cfg))
    write_json_atomic(rd / "inventory.json", inv.to_dict())
    if not inv.complete:
        return h.finish(INCOMPLETE, R_MANUAL_REVIEW, "inventory incomplete: " + "; ".join(inv.notes))
    h.round_state(r)
    return "plan"


def _current_policy_hash(h: _Holder) -> str:
    """지금 서버 정책의 해시: 계획 함수가 ``policy_hash(cfg)`` 를 알려 주면(진짜 ``planner.plan_round``) 그것, 아니면 작업에
    묶인 값. 계획을 다시 만들지 않고 낡은 계획인지 판단하려는 것이다."""
    fn = getattr(h.c.plan, "policy_hash", None)
    if callable(fn):
        try:
            return str(fn(h.cfg) or "")
        except Exception:  # noqa: BLE001 — 해시를 못 구하면 작업에 묶인 값을 쓴다
            log.debug("policy hash of the plan function unavailable", exc_info=True)
    return h.state.policy_hash


def _merge_residuals(st: RemediationState, residuals: list[Residual]) -> None:
    """라운드의 잔여를 작업 상태에 합친다: (ga, 버전, 이유)마다 하나. 이미 있으면 보고서용 사실(모듈·사내 아티팩트)은
    합집합으로 더하고, 사슬은 비어 있을 때만 채운다(먼저 적힌 것이 이긴다).

    Merge a round's residuals into the state, one per (ga, version, reason); facts of a repeat are unioned."""
    by_key = {(x.ga, x.version, x.reason): x for x in st.residuals}
    for x in residuals:
        key = (x.ga, x.version, x.reason)
        old = by_key.get(key)
        if old is None:
            by_key[key] = x
            st.residuals.append(x)
            continue
        old.modules = sorted(set(old.modules or []) | set(x.modules or []))
        old.inhouse = sorted(set(old.inhouse or []) | set(x.inhouse or []))
        if not old.via and x.via:
            old.via = list(x.via)


def _plan(h: _Holder) -> str | None:
    st, c, p = h.state, h.c, h.paths
    r = st.round
    rs = h.round_state(r)
    tree_hash = c.intake.tree_hash(p.accepted, h.paths_list)
    current = _current_policy_hash(h)
    if current and current != st.policy_hash:
        # 서버 정책이 바뀌었다(재개 전 설정 변경 등): 이 작업은 이제 지금 정책으로 계획한다 — 예전 정책의 승인 후보는 다시 쓰지 않는다
        h.journal({"event": "policy_changed", "round": r})
        st.policy_hash = current
    existing = h.load_plan(r)
    reuse = (existing is not None and existing.source_tree_hash == tree_hash and existing.policy_hash == st.policy_hash
             and all(x.source_tree_hash == tree_hash and x.policy_hash == st.policy_hash for x in existing.proposals))
    if reuse:
        plan = existing
        h.journal({"event": "plan_reused", "round": r, "proposals": len(plan.proposals)})
    else:
        rejected = {(x.ga, x.to_version) for k in range(1, r) if (pl := h.load_plan(k)) is not None
                    for x in pl.proposals if x.decision == D_REJECTED}
        if existing is not None:
            # 낡은 계획(트리·정책 해시가 다름)은 승인할 수 없다 — 기록으로 남기고 새로 만든다
            rejected |= {(x.ga, x.to_version) for x in existing.proposals if x.decision == D_REJECTED}
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
    if plan.pending():
        h.halt(AWAITING_APPROVAL, R_PENDING_APPROVAL, "plan",
               f"{len(plan.pending())} proposal(s) of round {r} wait for approval")
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
    _safe_rmtree(cand)
    c.intake.copy_tree(p.accepted, cand, h.paths_list)
    to_apply = plan.to_apply()
    result = c.apply.apply_proposals(h.project(cand), to_apply)
    p.patch(r).parent.mkdir(parents=True, exist_ok=True)
    p.patch(r).write_text(result.patch or "", encoding="utf-8")
    write_json_atomic(p.round_dir(r) / "apply.json", result.to_dict())
    rs.applied = list(result.applied)
    by_id = {x.id: x for x in plan.proposals}
    # 건너뛴 제안 = 적용되지 않음 → 성공일 수 없다(수동 검토로 남긴다)
    _merge_residuals(st, [Residual(ga=by_id[pid].ga if pid in by_id else pid,
                                   version=by_id[pid].from_version if pid in by_id else "",
                                   origin="proposal", reason=R_MANUAL_REVIEW,
                                   detail=f"proposal {pid} not applied: {why}",
                                   finding_ids=list(by_id[pid].finding_ids) if pid in by_id else [],
                                   modules=sorted(set(by_id[pid].modules) | set(by_id[pid].also_modules))
                                   if pid in by_id else [])
                          for pid, why in sorted(result.skipped.items())])
    applied = [by_id[pid] for pid in result.applied if pid in by_id]
    if not applied:
        if st.delivery is None:
            h.deliver_original()
        return "evaluate"
    violations = list(c.apply.verify_text_only(h.project(p.accepted), h.project(cand), applied, h.project_paths))
    # verify_text_only 는 프로젝트 폴더 기준이다 — ZIP 이 최상위 폴더로 싸였으면 그 밖의 파일(예: __MACOSX/)은 여기서 본다
    outside = h.outside_paths
    if outside and c.intake.tree_hash(p.accepted, outside) != c.intake.tree_hash(cand, outside):
        violations.append("a file outside the project folder differs from the accepted tree")
    if violations:
        rs.verify_problems = violations
        h.discard_candidate(r)
        return h.finish(FAILED, R_TEXT_ONLY_VIOLATION, "; ".join(violations[:5]))
    return "build"


def _applied_proposals(h: _Holder, r: int) -> list[Proposal]:
    plan = h.load_plan(r)
    rs = h.round_state(r)
    by_id = {x.id: x for x in (plan.proposals if plan else [])}
    return [by_id[pid] for pid in rs.applied if pid in by_id]


def _commit(h: _Holder, cand: Path) -> None:
    """후보의 manifest 파일들이 새 accepted/ 가 된다: 사본(accepted.new)을 만든 뒤 이름 바꾸기로 교체한다."""
    acc = h.paths.accepted
    new, old = acc.with_name(acc.name + ".new"), acc.with_name(acc.name + ".old")
    _safe_rmtree(new)
    _safe_rmtree(old)
    h.c.intake.copy_tree(cand, new, h.paths_list)
    os.replace(acc, old)
    os.replace(new, acc)
    _safe_rmtree(old)


def _recover_accepted(paths: RunPaths) -> None:
    """확정 도중 서버가 내려갔을 때 accepted/ 를 되살린다(이름 바꾸기 두 번 사이)."""
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


def _build(h: _Holder) -> str | None:
    st, c, p = h.state, h.c, h.paths
    r = st.round
    rs = h.round_state(r)
    cand = p.candidate(r)
    outcome = h.build_outcome(c.maven.package(h.project(cand), p.build_log(r), h.stop, **h.target_kw), p.build_log(r))
    rs.build = outcome
    if not outcome.ok:
        if h.stop():
            h.halt(STOPPED, R_STOP_REQUESTED, "build", f"stop requested during the round {r} candidate build")
            return None
        if getattr(h.rcfg, "failed_candidate", "include") == "include":
            # 정책 include(사용자 결정 2026-11-11): 실패한 수정본도 결과 ZIP 으로 낸다 — 성공일 수 없으니 재검사(IVAS 한 번)는
            # 하지 않는다. accepted/ 와 원본 ZIP 은 그대로, candidate.patch · build.log 도 그대로 남는다
            kept = h.deliver_failed_candidate(r)
            ids = ", ".join(rs.applied) or "-"
            msg = (f"delivery/result.zip contains the round {r} change set whose candidate build FAILED "
                   f"(proposals: {ids}; build log {h.rel(p.build_log(r))}); it was not rescanned; "
                   "the original is preserved as original/source.zip")
            if kept:
                msg += f"; the verified delivery of an earlier round is kept as {kept}"
            return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED, f"{msg} | {outcome.summary}")
        # 정책 discard: 실패한 후보는 결과 ZIP 에 넣지 않는다 — accepted/ 는 그대로, candidate.patch 는 남긴다
        # (앞 라운드에서 검증을 마친 전달 ZIP 이 있으면 그것은 남는다)
        h.discard_candidate(r)
        msg = outcome.summary
        if st.delivery is not None:
            msg = (f"round {r} candidate discarded; delivery/result.zip is the verified state of round "
                   f"{st.delivery.round}, without the failed change | {msg}")
        return h.finish(FAILED, R_CANDIDATE_BUILD_FAILED, msg)
    after = c.maven.resolve(h.project(cand), p.round_dir(r) / "work-after", **h.target_kw)
    write_json_atomic(p.resolved(r), after.to_dict())
    packaged = list(c.maven.packaged(h.project(cand), **h.target_kw))
    before_data = read_json(p.round_dir(r) / "before.json")
    before = ResolvedModel.from_dict(before_data) if isinstance(before_data, dict) else ResolvedModel(complete=False)
    lines = list(c.apply.verify_resolution(_applied_proposals(h, r), before, after, packaged))
    rs.verify_problems = lines
    problems = [ln for ln in lines if not ln.startswith("info:")]
    if problems:
        h.discard_candidate(r)
        return h.finish(FAILED, R_RESOLUTION_MISMATCH, "; ".join(problems[:5]))
    _commit(h, cand)
    rs.committed = True
    h.journal({"event": "round_committed", "round": r, "applied": list(rs.applied)})
    return "freeze_delivery"


def _freeze_delivery(h: _Holder) -> str | None:
    st, p = h.state, h.paths
    if p.delivery_zip.exists():
        p.delivery_zip.unlink()
    st.delivery = h.c.intake.freeze_delivery(p.accepted, p.delivery_zip, st.round, h.paths_list)
    write_json_atomic(p.delivery_json, st.delivery.to_dict())
    return "rescan"


def _rescan(h: _Holder) -> str | None:
    st, p = h.state, h.paths
    r = st.round
    label = f"final-r{r}"
    if st.delivery is None or not p.delivery_zip.is_file():
        return h.finish(FAILED, R_ZIP_HASH_MISMATCH, "the delivery ZIP is missing")
    # 다시 검사할 ZIP 이 고정한 그 바이트인가 — 아니면 검사 결과가 전달 ZIP 을 말하지 않는다
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
    # 남은 취약점 = 조치 대상만(ignore_actions 코드는 성공 판정·다음 라운드 판단에 넣지 않는다)
    remaining = len(h.split(final)[0]) if complete else 0
    last = next((rs for rs in st.rounds if rs.round == st.round), None)
    committed_last = bool(last and last.committed)
    max_rounds = int(h.rcfg.max_rounds)
    if complete and remaining and committed_last and st.round < max_rounds:
        st.round += 1
        h.journal({"event": "next_round", "round": st.round, "remaining": remaining})
        return "inventory"

    reasons: set[str] = set()
    if st.baseline_build is None or not st.baseline_build.ok:
        reasons.add(R_BASELINE_BUILD_FAILED)
    for rs in st.rounds:
        if rs.applied and not (rs.build is not None and rs.build.ok and rs.committed):
            reasons.add(R_CANDIDATE_BUILD_FAILED)
    if not complete:
        reasons.add(R_SCAN_INCOMPLETE)
    if st.delivery is None or final is None or final.zip_sha256 != st.delivery.sha256:
        reasons.add(R_ZIP_HASH_MISMATCH)
    if st.delivery is not None:
        # 재개된 예전 state(scanned 필드 없음)도 같은 규칙으로 다시 판정한다
        h.mark_scanned()
        if not st.delivery.build_ok:
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
    if not ordered:
        st.message = ""
        return h.finish(SUCCEEDED, "", reasons=[])
    return h.finish(INCOMPLETE, ordered[0], reasons=ordered)


def _report(h: _Holder) -> str | None:
    from . import report as report_mod  # report.py 는 그래프만 쓴다 — 늦게 불러도 된다
    # 위험도 경계(표시만, 정책 해시 밖): 예전 설정 객체에 없으면 기본값
    severity = (getattr(h.rcfg, "severity_red", 9.0), getattr(h.rcfg, "severity_orange", 7.0),
                getattr(h.rcfg, "severity_yellow", 4.0))
    report_mod.write_report(h.state, h.paths, ignore_actions=sorted(h.ignored_codes),
                            inhouse_message=getattr(h.rcfg, "inhouse_message", None), severity=severity)
    return None


_NODE_FNS: dict[str, Callable[[_Holder], str | None]] = {
    "baseline_build": _baseline_build, "baseline_scan": _baseline_scan, "inventory": _inventory, "plan": _plan,
    "apply": _apply, "build": _build, "freeze_delivery": _freeze_delivery, "rescan": _rescan, "evaluate": _evaluate,
    "report": _report,
}


# ---------------------------------------------------------------------------- graph
def _wrap(name: str, h: _Holder) -> Callable[[RemediationGraphState], dict]:
    """노드 경계 규칙: 중지 확인 → RUNNING·저장·node_start → 노드 → node_end·저장. 예외는 FAILED internal_error 로 바꾸고
    보고서 노드로 간다. 대기 상태로 멈추면 그래프를 끝낸다(보고서 없음)."""
    fn = _NODE_FNS[name]

    def node(gs: RemediationGraphState) -> dict:
        st = h.state
        if name == "report":
            # 보고서는 끝 상태에서만 돈다; 여기서 실패해도 다시 보고서로 돌지 않는다
            h.journal({"event": "node_start", "node": name, "round": st.round})
            # report.json 이 저장되는 state.json 과 같은 내용이 되도록 시각·노드를 먼저 정하고, 저장 때 시각을 다시 찍지 않는다
            st.node, st.next_node, st.updated = name, "report", _now()
            try:
                fn(h)
            except Exception as e:  # noqa: BLE001 — 보고서 실패가 끝 상태를 덮지 않게
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
        except Exception as e:  # noqa: BLE001 — 노드의 예상 못 한 오류도 보고서까지는 간다
            # 예외 문장에 비밀(URL 자격증명 등)이 들어 있을 수 있다 — traceback 도 지운 뒤에만 로그에 쓴다
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
    """``holder`` 에 묶인 컴파일된 StateGraph: 진입 노드 ``route`` (``state.next_node`` 로 건너뛴다 = 재개) + ``NODES``.

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
    """작업 하나를 ``state.next_node`` 부터 끝 상태(SUCCEEDED / INCOMPLETE / FAILED) 또는 대기 상태(AWAITING_APPROVAL /
    AWAITING_CONFIRMATION / STOPPED)까지 몬다. 노드 경계마다 ``save`` 를 부른다. 끝 상태면 아무것도 하지 않는다.

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
    """정책 include 로 실패한 수정본이 result.zip 을 차지할 때, 앞 라운드 ``round`` 의 검증된 ZIP 을 옮겨 두는 자리.

    Where an earlier round's verified delivery is kept when a failed candidate takes result.zip."""
    return paths.delivery_zip.with_name(f"result-r{int(round)}-verified.zip")


def find_verified_zip(paths: RunPaths) -> Path | None:
    """``delivery/result-r<k>-verified.zip`` 이 있으면 그 경로(여럿이면 가장 큰 k), 없으면 None.

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
    """접수가 끝난 작업의 첫 상태: QUEUED, 라운드 1, 첫 노드부터, 정책 해시·승인 설정을 묶는다.

    The first state of an accepted remediation (QUEUED, round 1, next node = the first node)."""
    now = _now()
    return RemediationState(id=remediation_id, name=name, status=QUEUED, created=now, updated=now, node="",
                            next_node=NODES[0], round=1, configs=list(configs or []), policy_hash=policy_hash,
                            require_approval=bool(cfg.remediation.require_approval), intake=intake)


def can_resume(state: RemediationState) -> bool:
    """재개할 수 있는 상태인가: 대기·중지·중단 상태이고 다음 노드가 알려진 노드.

    Whether `state` can be resumed (a resumable status and a known next node)."""
    return state.status in RESUMABLE and state.next_node in NODES


def default_components(cfg: Any, journal: Journal | None = None) -> Components:
    """운영용 부품 묶음: 진짜 IVAS · Maven · 저장소 · 접수 · 인벤토리 · 계획 · 적용 구현을 늦게 불러 잇는다. ``journal`` 을
    주면 Maven 게이트웨이가 경계 경고(``maven_project_config``)를 그 저널에 쓴다. 저장소 근거는 승인된 호스트
    (``ServerMaven.allowed_hosts``: 설정 목록, 없으면 서버 settings.xml)에서 온 버전만 인정한다.

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
