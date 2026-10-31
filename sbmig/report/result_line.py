"""``SBMIG-RESULT …`` 한 줄 결과와 주의 코드(attn) — 운영자가 회사 PC(복사·붙여넣기 불가, cp949 콘솔)에서
화면을 보고 손으로 다시 쳐서 보고하는 줄이다. 흐름상 위치: ``sbmig run`` 이 끝날 때 콘솔·로그에 찍고, 플릿은
프로젝트마다 한 줄 + ``summary.csv`` 의 attention 칸. 핵심 진입점: ``report_result_line``(실행 보고서 → 한
줄), ``result_line``(조립), ``attention_codes``(중요한 것 먼저), ``report_exit_code``(0 성공 · 2 빌드 실패 · 3
거부 / 중단). 지킬 것: 반드시 한 줄, ASCII 만(비ASCII 는 ``?``), 필드 순서·이름을 바꾸지 말 것 — 운영자 보고
양식과 플릿 집계가 이 형식을 그대로 읽는다. 새 필드는 끝에,
값이 있을 때만 붙인다(``review=`` v2, ``major=`` v2.1d, ``fix=`` v2.1i — 기본 script 모드에서는 붙이지 않는다).

One-line results and attention codes (round 10, operators retype them from a cp949 console).

    SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> open=<n> resolved=<n> llm=<calls>
                 attn=<codes>[ review=<v>][ major=<n>][ fix=<mode>]

v2: ` review=<ok|issues|skipped|failed>` is appended when the run has a structure review (`report.structure_review`;
fleet lines and runs without a review keep the round-10 format).
v2.1: `open=` / `resolved=` count the run's error list (`report.error_entries`): open = status open or unresolved
(finalize turns open into unresolved), resolved = status resolved (pom-verify / note / blocked in neither; blocked
items are `blocked=`). Present whenever the line is made from a report or a fleet result, absent on a crash line.
v2.1d: ` major=<n>` is appended when the run made n > 0 major upgrades / library replacements that are still in the
final tree (`report.operator_checks`, Handover "운영자 확인 필요"); the build passed, the runtime needs a look.
v2.1i: ` fix=<mode>` is appended when the run's `fix.mode` is not the default `script` (`report.fix_mode`, e.g.
` fix=edits` for the token comparison); a default run's line is byte-identical to v2.1d.

`sbmig run` prints (and logs) it at the end, the fleet prints one per project. `attn` is a comma list of short codes
that say why a human has to look (`-` = nothing): the same codes fill the fleet's `summary.csv` `attention` column
and `sbmig fleet status --attention` groups the projects by the first (most important) one. Everything is ASCII.

Codes, most important first:

    infra:<kind>          the run ended for an infrastructure reason (llm | maven | jdk | repository | disk |
                          file-lock | …): flags `infra:<kind>` (+ the older `llm-unavailable`, `llm-config-error`,
                          `mvn-not-found`, `jdk-not-21`; v2.1r: a `file_lock` BLOCKED item → `infra:file-lock`, a
                          Windows file lock on target/); a green run (exit 0) never gets it
    skipped:<why>         fleet only: not migrated (`skipped:gradle`, `skipped:input`)
    crash                 the run raised (fleet `error: crash: …`, CLI traceback)
    aborted[:<reason>]    exit 3 (`aborted:parent-not-found`, stopped by the user, …)
    owner-decision        v2.1y: a `needs_owner` BLOCKED item — the LLM named a blocker outside the project (a library
                          without a jakarta build, a framework to replace, a missing artifact); the owning team
                          decides (소유팀 결정 필요). Followed by `blocked:needs_owner`
    blocked:<reason>      one per distinct BlockReason (`blocked:external_javax_api`)
    unresolved:<n>        errors left after the final build
    restructure-failed, llm-budget-exhausted, time-budget-exhausted, javax-api-on-classpath, tests-skipped,
    test-compile-skipped, tests-not-compiled, major-upgrade (v2.1d: 운영자 확인 필요),
    jar-coordinates-unknown (v2.1j: Ant 합성 pom 의 좌표 미상 jar),
    parent-chain-no-boot-bom (v2.1s: 사내 부모 체인이 Boot BOM 을 import 하지 않음)      the flags of the same name
    incomplete            exit 2 with none of the above


"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

LEGACY_INFRA_FLAGS = {"llm-unavailable": "llm", "llm-config-error": "llm", "mvn-not-found": "maven",
                      "jdk-not-21": "jdk"}
ATTENTION_FLAGS = ("restructure-failed", "llm-budget-exhausted", "time-budget-exhausted", "javax-api-on-classpath",
                   "tests-skipped", "test-compile-skipped", "tests-not-compiled", "major-upgrade",
                   "jar-coordinates-unknown", "parent-chain-no-boot-bom")
INFRA_PREFIX = "infra:"
FILE_LOCK_REASON = "file_lock"          # BlockReason.FILE_LOCK (v2.1r)
FILE_LOCK_KIND = "file-lock"            # its infra kind: `infra:file-lock`
NEEDS_OWNER_REASON = "needs_owner"      # BlockReason.NEEDS_OWNER (v2.1y)
OWNER_DECISION = "owner-decision"       # its code: the owning team decides (소유팀 결정 필요)
_NON_ASCII_RE = re.compile(r"[^\x21-\x7e]+")


def _ascii_token(v: Any) -> str:
    # cp949 콘솔에서 깨지거나 손으로 칠 수 없는 글자(한글·공백
    # 포함)는 "?" 로 — 토큰 하나가 공백으로 쪼개지지 않게
    return _NON_ASCII_RE.sub("?", str(v)) or "-"


def infra_kind(flags: Iterable[str]) -> Optional[str]:
    """인프라 원인 종류(``llm`` / ``maven`` / ``jdk`` / ``repository`` …): ``infra:<kind>`` 플래그(첫 것)
    우선, 없으면 예전 플래그(``llm-unavailable`` 등)에서.

    `llm` / `maven` / `jdk` / `repository` / … from `infra:<kind>` flags (first wins), else from the older flags."""
    flags = list(flags or [])
    for f in flags:
        if f.startswith(INFRA_PREFIX) and len(f) > len(INFRA_PREFIX):
            return f[len(INFRA_PREFIX):]
    for f in flags:
        if f in LEGACY_INFRA_FLAGS:
            return LEGACY_INFRA_FLAGS[f]
    return None


def attention_codes(*, flags: Iterable[str] = (), exit_code: Optional[int] = None, status: Optional[str] = None,
                    error: Optional[str] = None, blocked_reasons: Iterable[str] = (), unresolved: int = 0) -> list[str]:
    """실행 하나의 주의 코드들(모듈 docstring 의 표), 중요한 것 먼저, 중복 없이.
    ``fleet status --attention`` 이 첫 코드로 프로젝트를 묶으므로 순서가 곧 의미다.

    The attention codes of one run (see the module docstring), most important first, no duplicates."""
    flags = list(flags or [])
    err = (error or "").strip()
    codes: list[str] = []
    # 초록 실행(exit 0)에는 infra 코드를 붙이지 않는다 — 중간에 LLM 이 한 번 끊겼어도 결과는 정상이다
    kind = err[len(INFRA_PREFIX):].split()[0] if err.startswith(INFRA_PREFIX) and len(err) > len(INFRA_PREFIX) \
        else (infra_kind(flags) if exit_code not in (0, None) or status == "pending" else None)
    # v2.1r: target/ 파일 잠금으로 차단된 실행은 빌드 PC 환경 탓 — infra:repository 처럼 infra:file-lock
    if not kind and exit_code not in (0, None) and any(str(r).split(":", 1)[0] == FILE_LOCK_REASON
                                                       for r in blocked_reasons or ()):
        kind = FILE_LOCK_KIND
    if kind:
        codes.append(INFRA_PREFIX + kind)
    if status == "skipped":
        if err.startswith("unsupported build tool:"):
            codes.append("skipped:" + (err.split(":", 1)[1].split()[0] if err.split(":", 1)[1].split() else "tool"))
        elif err.startswith(("input not found", "no pom.xml", "input must be", "config not found")):
            codes.append("skipped:input")
        else:
            codes.append("skipped")
    if err.startswith("crash:"):
        codes.append("crash")
    if status != "skipped" and (exit_code == 3 or any(f == "aborted" or f.startswith("aborted:") for f in flags)):
        reason = next((f[len("aborted:"):] for f in flags if f.startswith("aborted:")), "")
        if not kind or reason:
            codes.append("aborted:" + reason if reason else "aborted")
    reasons = [str(r).split(":", 1)[0] for r in blocked_reasons or ()]
    # v2.1y: LLM 이 프로젝트 밖의 차단 사유를 말해 소유팀 결정을 기다리는 실행 — 차단 코드들 앞에 한 번(fleet 이 첫 코드로 묶는다)
    if NEEDS_OWNER_REASON in reasons:
        codes.append(OWNER_DECISION)
    for r in reasons:
        codes.append("blocked:" + r)
    if unresolved:
        codes.append(f"unresolved:{unresolved}")
    codes += [f for f in ATTENTION_FLAGS if f in flags]
    if not codes and (status == "incomplete" or exit_code == 2):
        codes.append("incomplete")
    return [_ascii_token(c) for c in dict.fromkeys(codes)]


def report_attention(report: Any, exit_code: Optional[int]) -> list[str]:
    """실행 보고서(RunReport)의 주의 코드.

    `attention_codes` of a `RunReport`."""
    return attention_codes(flags=getattr(report, "flags", []) or [], exit_code=exit_code,
                           blocked_reasons=[b.reason.value for b in getattr(report, "blocked", []) or []],
                           unresolved=len(getattr(report, "unresolved", []) or []))


def result_attention(r: Any) -> list[str]:
    """플릿 프로젝트 결과(FleetProjectResult)의 주의 코드. 아직 안 돈 프로젝트(인프라 오류 없는 pending,
    running)는 아무도 볼 필요가 없으므로 [].

    `attention_codes` of a `FleetProjectResult` (`summary.csv` attention, `fleet status --attention`); a project
    that has not run yet (pending without an infra error, running) needs nobody."""
    if r.status == "running" or (r.status == "pending" and not (r.error or "").startswith(INFRA_PREFIX)):
        return []
    if r.status == "success":
        return attention_codes(flags=r.flags, exit_code=0, status=r.status, blocked_reasons=r.blocked_reasons,
                               unresolved=r.unresolved)
    return attention_codes(flags=r.flags, exit_code=r.exit_code, status=r.status, error=r.error,
                           blocked_reasons=r.blocked_reasons, unresolved=r.unresolved)


def result_line(run_id: Any, exit_code: Any, build_ok: bool, *, unresolved: int = 0, blocked: int = 0,
                llm_calls: int = 0, attention: Iterable[str] = (), review: Optional[str] = None,
                open_errors: Optional[int] = None, resolved_errors: Optional[int] = None, major: int = 0,
                fix_mode: str | None = None) -> str:
    """``SBMIG-RESULT`` 한 줄을 조립한다. ``open=`` / ``resolved=`` 는 오류 목록이 있을 때만(충돌 줄에는
    없음), ``review=`` 는 구조 검토가 있었을 때만, ``major=`` 는 1 이상일 때만, ``fix=`` 는 수정 방식이 기본
    script 가 아닐 때만(v2.1i) 붙인다 — 예전 형식을 읽는 쪽이 깨지지 않게."""
    # 운영자가 손으로 옮겨 적는 줄이라 기본(script) 실행의 줄은 한 글자도 바뀌면 안 된다 → 기본값이면 생략
    fix = f" fix={_ascii_token(fix_mode)}" if fix_mode and fix_mode != "script" else ""
    codes = ",".join(attention) or "-"
    errs = ""
    if open_errors is not None or resolved_errors is not None:
        errs = f" open={open_errors or 0} resolved={resolved_errors or 0}"
    return (f"SBMIG-RESULT run={_ascii_token(run_id or '-')} exit={'-' if exit_code is None else exit_code} "
            f"build={'ok' if build_ok else 'FAIL'} unresolved={unresolved} blocked={blocked}{errs} llm={llm_calls} "
            f"attn={_ascii_token(codes)}" + (f" review={_ascii_token(review)}" if review else "")
            + (f" major={int(major)}" if major and int(major) > 0 else "") + fix)


OPEN_STATUSES = ("open", "unresolved")


def entry_counts(entries: Iterable[Any]) -> tuple[int, int]:
    """오류 목록의 (열린 수, 해결 수). ErrorEntry 객체와 그 dict(플릿이 JSON 에서 읽은 것) 둘 다 받는다.

    (open, resolved) of an error list (`ErrorEntry` objects or their dicts)."""
    o = r = 0
    for e in entries or ():
        st = e.get("status") if isinstance(e, dict) else getattr(e, "status", None)
        o += st in OPEN_STATUSES
        r += st == "resolved"
    return o, r


def counts_open_resolved(counts: Optional[dict]) -> tuple[int, int]:
    """상태별 개수 dict(``FleetProjectResult.error_counts``)에서 (열린 수, 해결 수).

    (open, resolved) from a status → count dict (`FleetProjectResult.error_counts`)."""
    c = counts or {}
    return sum(int(c.get(s, 0) or 0) for s in OPEN_STATUSES), int(c.get("resolved", 0) or 0)


def report_exit_code(report: Any) -> int:
    """보고서의 ``sbmig run`` 종료 코드: 3 = 중단·거부(``aborted[:…]`` / ``mvn-not-found``), 0 = 빌드 성공이고
    차단 없음, 그 밖은 2.

    The `sbmig run` exit code of a report: 3 aborted (flag `aborted[:…]` / `mvn-not-found`), 0 green with nothing
    blocked, else 2 (`sbmig.cli.run_exit_code` is this function)."""
    flags = list(getattr(report, "flags", []) or [])
    if any(f == "aborted" or f.startswith("aborted:") or f == "mvn-not-found" for f in flags):
        return 3
    if getattr(report, "success", False) and not getattr(report, "blocked", None):
        return 0
    return 2


def report_result_line(report: Any, exit_code: Optional[int] = None, run_id: Optional[str] = None) -> str:
    """끝난 ``sbmig run`` 의 ``SBMIG-RESULT …`` 줄(``run_id`` 를 주면 보고서 것 대신, ``exit_code`` 기본은
    ``report_exit_code``). ``major=`` 는 최종 트리에 남은 메이저 업그레이드 수(인수인계 "운영자 확인 필요").

    The `SBMIG-RESULT …` line of a finished `sbmig run` (`run_id` overrides the report's; `exit_code` defaults
    to `report_exit_code(report)`)."""
    code = report_exit_code(report) if exit_code is None else exit_code
    o, r = entry_counts(getattr(report, "error_entries", None) or [])
    return result_line(run_id or getattr(report, "run_id", "-"), code, bool(getattr(report, "success", False)),
                       unresolved=len(getattr(report, "unresolved", []) or []),
                       blocked=len(getattr(report, "blocked", []) or []),
                       llm_calls=getattr(getattr(report, "llm_usage", None), "calls", 0) or 0,
                       attention=report_attention(report, code),
                       review=getattr(getattr(report, "structure_review", None), "verdict", None),
                       open_errors=o, resolved_errors=r, major=len(getattr(report, "operator_checks", None) or []),
                       fix_mode=getattr(report, "fix_mode", None))
