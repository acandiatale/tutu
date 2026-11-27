"""실행 결과 한 줄(``SBMIG-RESULT …``)과 주의 코드(attn)를 만드는 모듈.
운영자가 회사 PC의 cp949 콘솔 화면을 보고 손으로 옮겨서 보고하는 줄이다.

`sbmig run` prints it to the console and the log when it ends; the fleet writes one line per project and the
`summary.csv` attention column. Entry points: `report_result_line`, `result_line`, `attention_codes`,
`report_exit_code` (0 success, 2 build failed, 3 refused / aborted). Always one line, ASCII only: any other
character becomes `?`. The operators' report form and the fleet totals read this format as it is, so field
order and names never change; a new field goes at the end, only when it has a value.

Format:

    SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> open=<n> resolved=<n> llm=<calls>
                 attn=<codes>[ review=<v>][ major=<n>][ scope=jdk-jakarta][ flow=remediation]
                 [ remediation=<STATUS|skipped>]

Optional fields:
- `review=<ok|issues|skipped|failed>`: only when the run has a structure review (`report.structure_review`);
  fleet lines and runs without a review have none.
- `open=` / `resolved=` count `report.error_entries`: open = status open or unresolved (finalize turns open
  into unresolved), resolved = status resolved (pom-verify / note / blocked in neither; blocked items are
  `blocked=`). Present whenever the line is made from a report or a fleet result, absent on a crash line.
- `major=<n>`: n > 0 major upgrades / library replacements still in the final tree (`report.operator_checks`,
  Handover "운영자 확인 필요"); the build passed, the runtime needs a look.
- `scope=jdk-jakarta` (after `major=`, before `flow=` / `remediation=`): a framework project's reduced run
  (`report.plan.scope`, else `report.profile.reduced_scope` when the run ended before the plan).
- `flow=remediation` (just before `remediation=`): a run of the `remediation` flow (no migration; `exit=` 0 =
  SUCCEEDED, 2 = any other saved status, 3 = not started). Its `build=` says what was built, not whether the
  remediation succeeded (`remediation_build_ok`). So an INCOMPLETE / AWAITING_CONFIRMATION /
  STOPPED-after-baseline run or a FAILED `resolution_mismatch` after a passed build prints `build=ok` with
  `exit=2`. A `full` run never prints `flow=`.
- `remediation=<STATUS|skipped>`: a remediation was chained after the migration (`report.remediation`:
  SUCCEEDED / INCOMPLETE / FAILED / AWAITING_CONFIRMATION / …) or `remediation.after_migration` was on and the
  chain was skipped (not ready, stop request, inline engine). It never changes `exit=` / `build=` / `attn=`. A
  conversion-only framework run whose build failed is chained too, so it can stand beside `exit=2 build=FAIL`.
- `llm=` = the migration's calls + the remediation's build-fix-loop calls of every round (`report_llm_calls`).

`attn` is a comma list of short codes that say why a human has to look (`-` = nothing): the same codes fill
the fleet's `summary.csv` `attention` column and `sbmig fleet status --attention` groups the projects by the
first (most important) one. Codes, most important first:

    infra:<kind>          the run ended for an infrastructure reason (llm | maven | jdk | repository | disk |
                          file-lock | http-blocked | …): flags `infra:<kind>` plus the older `llm-unavailable`,
                          `llm-config-error`, `mvn-not-found`, `jdk-not-21`; a `file_lock` BLOCKED item (Windows
                          lock on target/) gives `infra:file-lock`; a green run (exit 0) never gets it
    skipped:<why>         fleet only: not migrated (`skipped:gradle`, `skipped:input`)
    crash                 the run raised (fleet `error: crash: …`, CLI traceback)
    aborted[:<reason>]    exit 3 (`aborted:parent-not-found`, `aborted:http-blocked`, stopped by the user, …)
    owner-decision        a `needs_owner` BLOCKED item: the LLM named a blocker outside the project (a library
                          without a jakarta build, a framework to replace, a missing artifact); the owning team
                          decides. Followed by `blocked:needs_owner`
    blocked:<reason>      one per distinct BlockReason (`blocked:external_javax_api`)
    unresolved:<n>        errors left after the final build
    <flag>                the flags of `ATTENTION_FLAGS`, under their own name
    incomplete            exit 2 with none of the above
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

LEGACY_INFRA_FLAGS = {"llm-unavailable": "llm", "llm-config-error": "llm", "mvn-not-found": "maven",
                      "jdk-not-21": "jdk"}
# 주의 코드로 그대로 쓰는 플래그
ATTENTION_FLAGS = ("rewrite-failed",                   # 자동 변환(OpenRewrite)이 실패했다
                   "restructure-failed", "llm-budget-exhausted", "time-budget-exhausted", "javax-api-on-classpath",
                   "tests-skipped", "test-compile-skipped", "tests-not-compiled", "major-upgrade",
                   "jar-coordinates-unknown", "parent-chain-no-boot-bom",
                   "hollow-reactor",                 # reactor가 컴파일하는 소스가 없어 소스가 이관되지 않았다
                   "adf-project",                    # 변환 대상이 아닌 Oracle ADF 프로젝트다
                   "remediation:not-ready")          # 조치만 하는 실행이 IVAS나 Maven 문제로 시작하지 못했다
INFRA_PREFIX = "infra:"
FILE_LOCK_REASON = "file_lock"          # BlockReason.FILE_LOCK
FILE_LOCK_KIND = "file-lock"            # 이 사유의 주의 코드는 `infra:file-lock`이다
NEEDS_OWNER_REASON = "needs_owner"      # BlockReason.NEEDS_OWNER
OWNER_DECISION = "owner-decision"       # 담당 팀의 결정이 필요한 실행의 주의 코드
_NON_ASCII_RE = re.compile(r"[^\x21-\x7e]+")


def _ascii_token(v: Any) -> str:
    # cp949 콘솔에서 깨지거나 손으로 칠 수 없는 문자(한글, 공백 포함)는 "?"로 바꾼다.
    # 공백도 바꾸므로 토큰 하나가 둘로 쪼개지지 않는다
    return _NON_ASCII_RE.sub("?", str(v)) or "-"


def infra_kind(flags: Iterable[str]) -> Optional[str]:
    """인프라 원인 종류(``llm``, ``maven``, ``jdk``, ``repository`` 등)를 반환한다.
    ``infra:<kind>`` 플래그가 있으면 첫 번째를 쓴다. 없으면 이전 형식의 플래그(``llm-unavailable`` 등)에서 찾는다."""
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
    """실행 하나의 주의 코드를 중요한 것부터 중복 없이 반환한다.
    ``fleet status --attention``이 첫 코드로 프로젝트를 묶는다. 그래서 순서가 중요하고, 순서는 모듈 docstring에 있다."""
    flags = list(flags or [])
    err = (error or "").strip()
    codes: list[str] = []
    # 성공한 실행(exit 0)에는 infra 코드를 붙이지 않는다. 중간에 LLM 연결이 한 번 끊겼어도 결과는 정상이다
    kind = err[len(INFRA_PREFIX):].split()[0] if err.startswith(INFRA_PREFIX) and len(err) > len(INFRA_PREFIX) \
        else (infra_kind(flags) if exit_code not in (0, None) or status == "pending" else None)
    # target/ 파일 잠금으로 차단된 실행에는 infra:file-lock 코드를 붙인다. 빌드 PC 환경 문제다
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
    # LLM이 프로젝트 밖의 차단 사유를 들어 담당 팀의 결정을 기다리는 실행.
    # 플릿이 첫 코드로 묶는다. 그래서 차단 코드들 앞에 한 번 넣는다
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
    """실행 보고서(RunReport)의 주의 코드를 반환한다."""
    return attention_codes(flags=getattr(report, "flags", []) or [], exit_code=exit_code,
                           blocked_reasons=[b.reason.value for b in getattr(report, "blocked", []) or []],
                           unresolved=len(getattr(report, "unresolved", []) or []))


def result_attention(r: Any) -> list[str]:
    """플릿 프로젝트 결과(FleetProjectResult)의 주의 코드를 반환한다.
    아직 실행되지 않은 프로젝트는 볼 필요가 없다. 빈 목록을 반환한다.

    "Not run yet" = pending without an infra error, or running. Feeds `summary.csv` attention and
    `fleet status --attention`."""
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
                remediation: Optional[str] = None, flow: Optional[str] = None, scope: Optional[str] = None) -> str:
    """``SBMIG-RESULT`` 한 줄을 조립한다.
    - ``open=``, ``resolved=``, ``review=``, ``major=``, ``remediation=``은 값이 있을 때만 붙인다.
      이전 형식을 읽는 쪽이 깨지지 않게 하기 위해서다
    - ``scope=``는 범위가 ``full``이 아닐 때만 ``major=`` 뒤에 붙인다
    - ``flow=``는 실행 방식이 ``full``이 아닐 때만 ``remediation=`` 바로 앞에 붙인다"""
    codes = ",".join(attention) or "-"
    errs = ""
    if open_errors is not None or resolved_errors is not None:
        errs = f" open={open_errors or 0} resolved={resolved_errors or 0}"
    return (f"SBMIG-RESULT run={_ascii_token(run_id or '-')} exit={'-' if exit_code is None else exit_code} "
            f"build={'ok' if build_ok else 'FAIL'} unresolved={unresolved} blocked={blocked}{errs} llm={llm_calls} "
            f"attn={_ascii_token(codes)}" + (f" review={_ascii_token(review)}" if review else "")
            + (f" major={int(major)}" if major and int(major) > 0 else "")
            + (f" scope={_ascii_token(scope)}" if scope and scope != "full" else "")
            + (f" flow={_ascii_token(flow)}" if flow and flow != "full" else "")
            + (f" remediation={_ascii_token(remediation)}" if remediation else ""))


OPEN_STATUSES = ("open", "unresolved")


def entry_counts(entries: Iterable[Any]) -> tuple[int, int]:
    """오류 목록의 (열린 수, 해결 수)를 반환한다. ErrorEntry 객체와 플릿이 JSON에서 읽은 dict를 모두 받는다."""
    o = r = 0
    for e in entries or ():
        st = e.get("status") if isinstance(e, dict) else getattr(e, "status", None)
        o += st in OPEN_STATUSES
        r += st == "resolved"
    return o, r


def counts_open_resolved(counts: Optional[dict]) -> tuple[int, int]:
    """상태별 개수 dict(``FleetProjectResult.error_counts``)에서 (열린 수, 해결 수)를 반환한다."""
    c = counts or {}
    return sum(int(c.get(s, 0) or 0) for s in OPEN_STATUSES), int(c.get("resolved", 0) or 0)


def report_exit_code(report: Any) -> int:
    """보고서로 ``sbmig run``의 종료 코드를 정한다.
    중단이나 거부(``aborted[:…]``, ``mvn-not-found``)는 3, 빌드가 성공하고 차단이 없으면 0, 그 밖은 2다.

    `sbmig.cli.run_exit_code` is this function."""
    flags = list(getattr(report, "flags", []) or [])
    if any(f == "aborted" or f.startswith("aborted:") or f == "mvn-not-found" for f in flags):
        return 3
    if getattr(report, "success", False) and not getattr(report, "blocked", None):
        return 0
    return 2


def report_result_line(report: Any, exit_code: Optional[int] = None, run_id: Optional[str] = None) -> str:
    """끝난 ``sbmig run``의 ``SBMIG-RESULT …`` 줄을 만든다.
    ``run_id``를 주면 보고서의 값 대신 쓴다. ``exit_code``를 주지 않으면 ``report_exit_code``의 결과를 쓴다.
    ``major=``는 최종 결과에 남은 메이저 업그레이드 수다."""
    code = report_exit_code(report) if exit_code is None else exit_code
    o, r = entry_counts(getattr(report, "error_entries", None) or [])
    flow = getattr(report, "flow", None)
    build_ok = remediation_build_ok(report) if flow == "remediation" else bool(getattr(report, "success", False))
    # 변환이 실패해 성공이 아닌 프레임워크 실행도 빌드 자체는 통과했으면 build=ok로 쓴다
    if not build_ok and flow != "remediation" and conversion_only_failed(report):
        build_ok = bool(getattr(getattr(report, "final_build", None), "ok", False))
    return result_line(run_id or getattr(report, "run_id", "-"), code, build_ok,
                       unresolved=len(getattr(report, "unresolved", []) or []),
                       blocked=len(getattr(report, "blocked", []) or []),
                       llm_calls=report_llm_calls(report),
                       attention=report_attention(report, code),
                       review=getattr(getattr(report, "structure_review", None), "verdict", None),
                       open_errors=o, resolved_errors=r, major=len(getattr(report, "operator_checks", None) or []),
                       remediation=remediation_token(report), flow=flow, scope=run_scope(report))


def report_llm_calls(report: Any) -> int:
    """``llm=`` 값을 반환한다. 이관의 LLM 호출 수에 취약점 조치의 빌드 오류 수정 루프가 호출한 수를 더한다.

    Sources: `report.llm_usage.calls` + `report.remediation["fix_llm_calls"]` (every round, filled by
    `chain.summary`), for the chained run and the remediation-only flow alike. Each call is counted once: the
    migration's `llm_usage` never holds the remediation's calls."""
    own = _count(getattr(getattr(report, "llm_usage", None), "calls", 0))
    rem = getattr(report, "remediation", None)
    return own + (_count(rem.get("fix_llm_calls")) if isinstance(rem, dict) else 0)


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


REDUCED_SCOPE = "jdk-jakarta"
REWRITE_FAILED = "rewrite-failed"


def conversion_only_failed(report: Any) -> bool:
    """변환만 하는 실행(프레임워크 프로젝트)에서 자동 변환이 실패했는지 확인한다. 중단된 실행은 False다.
    True인 실행은 빌드가 통과해도 성공이 아니다.

    Read by `Orchestrator.build_succeeded`, the renderers and the `build=` token."""
    flags = list(getattr(report, "flags", []) or [])
    if REWRITE_FAILED not in flags or any(f == "aborted" or f.startswith("aborted:") for f in flags):
        return False
    return run_scope(report) is not None


HOLLOW_REACTOR = "hollow-reactor"


def _field(obj: Any, name: str) -> Any:
    # 보고서 객체(RunReport)와 report.json에서 읽은 dict에서 같은 이름의 값을 꺼낸다
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def conversion_complete(report: Any) -> bool:
    """사내 프레임워크 실행이 JDK 21, Jakarta 변환을 끝냈는지 확인한다. True면 화면과 보고서가 완료로 표시한다.
    최종 빌드가 실패해도 True일 수 있다. 종료 코드와 ``build=`` 값은 이 함수와 상관없다. 아래를 모두 만족해야 True다.
    - 줄인 범위(``jdk-jakarta``)로 계획한 이관 실행이다
    - 중단되지 않았다
    - 계획한 자동 변환 단계마다 실행 결과가 있고 실패한 변환이 없다
    - 소스가 없는 reactor가 아니다
    - 최종 빌드를 시도한 결과가 있다
    RunReport와 report.json에서 읽은 dict를 모두 받는다. 읽을 수 없는 값이 있으면 False다. 예외를 던지지 않는다.

    One rule for every place that words the outcome of such a run: the page (`status()["conversion_complete"]`),
    the admin page (result word `converted`), both report renderers and the console summary. A run with
    `success` true satisfies it too; the readers use it only when the build failed. Derived from what a report
    already stores (`flow`, `plan.scope`, `plan.steps`, `flags`, `phases[].rewrite_results[]`, `final_build`), so
    a report written before this rule existed is judged the same way.
    - "Every planned step": the number of rewrite results that are not dry runs must reach the number of planned
      `openrewrite` steps that carry recipes (one result per executed step; a step skipped for the time budget
      records none, which matters for an MSA plan with one step per service).
    - Files OpenRewrite could not parse and JSP files the rename step could not change are follow-ups named in
      the report, as in a successful full run; they do not make the rule false.
    Contract: docs/INTERFACES.md "v2.1z-ck"."""
    if _field(report, "flow") == "remediation":
        return False
    plan = _field(report, "plan")
    if plan is None or _field(plan, "scope") != REDUCED_SCOPE:
        return False
    raw_flags = _field(report, "flags")
    # 목록이 아닌 플래그 값은 읽을 수 없는 보고서다. 문자열을 글자 단위로 읽어 플래그가 없다고 판단하지 않는다
    if not isinstance(raw_flags, (list, tuple)):
        return False
    flags = [f for f in raw_flags if isinstance(f, str)]
    if any(f == "aborted" or f.startswith("aborted:") or f == "mvn-not-found" for f in flags):
        return False
    if REWRITE_FAILED in flags or HOLLOW_REACTOR in flags:
        return False
    # 최종 빌드 결과에 ok 값(True / False)이 있어야 한다. 통과했는지는 보지 않는다
    if not isinstance(_field(_field(report, "final_build"), "ok"), bool):
        return False
    steps = _field(plan, "steps")
    # 종류를 읽을 수 없는 단계가 있으면 계획한 변환 단계를 셀 수 없다
    if not isinstance(steps, list) or any(not _kind(st) for st in steps):
        return False
    planned = sum(1 for st in steps if _kind(st) == "openrewrite" and _field(st, "recipes"))
    phases = _field(report, "phases")
    converted = 0
    for pr in phases if isinstance(phases, list) else []:
        results = _field(pr, "rewrite_results")
        for res in results if isinstance(results, list) else []:
            if _field(res, "dry_run"):
                continue
            if _field(res, "ok") is not True:
                return False
            converted += 1
    # 서비스마다 변환 단계가 있는 계획에서 한 단계라도 결과가 없으면 변환을 끝낸 것이 아니다
    return converted >= max(planned, 1)


def _kind(step: Any) -> str:
    # 계획 단계의 종류를 문자열로 반환한다. 객체에서는 StepKind, dict에서는 문자열이다
    kind = _field(step, "kind")
    return str(getattr(kind, "value", kind) or "")


def run_scope(report: Any) -> Optional[str]:
    """실행 범위를 반환한다. JDK와 jakarta만 적용한 실행이면 ``jdk-jakarta``, 그 밖은 None이다.
    계획이 있으면 계획의 범위를 쓰고, 계획 전에 끝난 실행은 프로필의 검사 결과를 쓴다."""
    plan = getattr(report, "plan", None)
    if plan is not None:
        return REDUCED_SCOPE if getattr(plan, "scope", "full") == REDUCED_SCOPE else None
    profile = getattr(report, "profile", None)
    return REDUCED_SCOPE if profile is not None and bool(getattr(profile, "reduced_scope", False)) else None


def remediation_token(report: Any) -> Optional[str]:
    """``remediation=`` 값을 반환한다.
    이어서 실행한 조치의 상태(SUCCEEDED 등)나 "skipped"를 반환한다. 조치 요약이 없으면 None이다."""
    rem = getattr(report, "remediation", None)
    if not isinstance(rem, dict):
        return None
    status = str(rem.get("status") or "").strip()
    return status or None


BUILD_FAILED_REASONS = ("baseline_build_failed", "candidate_build_failed")


def remediation_build_ok(report: Any) -> bool:
    """취약점 조치만 하는 실행의 ``build=`` 값을 정한다. 아래를 모두 만족하면 True다.
    - 업로드한 그대로의 조치 전 빌드가 통과했다
    - 사유에 빌드 실패(``BUILD_FAILED_REASONS``)가 없다
    - 결과 ZIP이 ``build_ok: false``가 아니다
    시작하지 않았거나 조치 전 빌드 전에 멈춘 실행은 False다. 조치가 성공했는지(종료 코드)와는 별개다."""
    rem = getattr(report, "remediation", None)
    if not isinstance(rem, dict) or str(rem.get("status") or "") in ("", "skipped"):
        return False
    if rem.get("baseline_build_ok") is not True:
        return False
    reasons = {str(rem.get("reason") or "")} | {str(x) for x in (rem.get("reasons") or [])}
    if reasons & set(BUILD_FAILED_REASONS):
        return False
    delivery = rem.get("delivery")
    return not (isinstance(delivery, dict) and delivery.get("build_ok") is False)
