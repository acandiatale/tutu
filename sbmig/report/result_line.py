"""One-line results and attention codes (round 10, operators retype them from a cp949 console).

    SBMIG-RESULT run=<id> exit=<n> build=<ok|FAIL> unresolved=<n> blocked=<n> open=<n> resolved=<n> llm=<calls>
                 attn=<codes>[ review=<v>][ major=<n>]

v2: ` review=<ok|issues|skipped|failed>` is appended when the run has a structure review (`report.structure_review`;
fleet lines and runs without a review keep the round-10 format).
v2.1: `open=` / `resolved=` count the run's error list (`report.error_entries`): open = status open or unresolved
(finalize turns open into unresolved), resolved = status resolved (pom-verify / note / blocked in neither; blocked
items are `blocked=`). Present whenever the line is made from a report or a fleet result, absent on a crash line.
v2.1d: ` major=<n>` is appended when the run made n > 0 major upgrades / library replacements that are still in the
final tree (`report.operator_checks`, Handover "운영자 확인 필요"); the build passed, the runtime needs a look.

`sbmig run` prints (and logs) it at the end, the fleet prints one per project. `attn` is a comma list of short codes
that say why a human has to look (`-` = nothing): the same codes fill the fleet's `summary.csv` `attention` column
and `sbmig fleet status --attention` groups the projects by the first (most important) one. Everything is ASCII.

Codes, most important first:

    infra:<kind>          the run ended for an infrastructure reason (llm | maven | jdk | repository | disk | …):
                          flags `infra:<kind>` (+ the older `llm-unavailable`, `llm-config-error`, `mvn-not-found`,
                          `jdk-not-21`); a green run (exit 0) never gets it
    skipped:<why>         fleet only: not migrated (`skipped:gradle`, `skipped:input`)
    crash                 the run raised (fleet `error: crash: …`, CLI traceback)
    aborted[:<reason>]    exit 3 (`aborted:parent-not-found`, stopped by the user, …)
    blocked:<reason>      one per distinct BlockReason (`blocked:external_javax_api`)
    unresolved:<n>        errors left after the final build
    restructure-failed, llm-budget-exhausted, time-budget-exhausted, javax-api-on-classpath, tests-skipped,
    test-compile-skipped, tests-not-compiled, major-upgrade (v2.1d: 운영자 확인 필요)      the flags of the same name
    incomplete            exit 2 with none of the above
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

LEGACY_INFRA_FLAGS = {"llm-unavailable": "llm", "llm-config-error": "llm", "mvn-not-found": "maven",
                      "jdk-not-21": "jdk"}
ATTENTION_FLAGS = ("restructure-failed", "llm-budget-exhausted", "time-budget-exhausted", "javax-api-on-classpath",
                   "tests-skipped", "test-compile-skipped", "tests-not-compiled", "major-upgrade")
INFRA_PREFIX = "infra:"
_NON_ASCII_RE = re.compile(r"[^\x21-\x7e]+")


def _ascii_token(v: Any) -> str:
    return _NON_ASCII_RE.sub("?", str(v)) or "-"


def infra_kind(flags: Iterable[str]) -> Optional[str]:
    """`llm` / `maven` / `jdk` / `repository` / … from `infra:<kind>` flags (first wins), else from the older flags."""
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
    """The attention codes of one run (see the module docstring), most important first, no duplicates."""
    flags = list(flags or [])
    err = (error or "").strip()
    codes: list[str] = []
    kind = err[len(INFRA_PREFIX):].split()[0] if err.startswith(INFRA_PREFIX) and len(err) > len(INFRA_PREFIX) \
        else (infra_kind(flags) if exit_code not in (0, None) or status == "pending" else None)
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
    for r in blocked_reasons or ():
        codes.append("blocked:" + str(r).split(":", 1)[0])
    if unresolved:
        codes.append(f"unresolved:{unresolved}")
    codes += [f for f in ATTENTION_FLAGS if f in flags]
    if not codes and (status == "incomplete" or exit_code == 2):
        codes.append("incomplete")
    return [_ascii_token(c) for c in dict.fromkeys(codes)]


def report_attention(report: Any, exit_code: Optional[int]) -> list[str]:
    """`attention_codes` of a `RunReport`."""
    return attention_codes(flags=getattr(report, "flags", []) or [], exit_code=exit_code,
                           blocked_reasons=[b.reason.value for b in getattr(report, "blocked", []) or []],
                           unresolved=len(getattr(report, "unresolved", []) or []))


def result_attention(r: Any) -> list[str]:
    """`attention_codes` of a `FleetProjectResult` (`summary.csv` attention, `fleet status --attention`); a project
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
                open_errors: Optional[int] = None, resolved_errors: Optional[int] = None, major: int = 0) -> str:
    codes = ",".join(attention) or "-"
    errs = ""
    if open_errors is not None or resolved_errors is not None:
        errs = f" open={open_errors or 0} resolved={resolved_errors or 0}"
    return (f"SBMIG-RESULT run={_ascii_token(run_id or '-')} exit={'-' if exit_code is None else exit_code} "
            f"build={'ok' if build_ok else 'FAIL'} unresolved={unresolved} blocked={blocked}{errs} llm={llm_calls} "
            f"attn={_ascii_token(codes)}" + (f" review={_ascii_token(review)}" if review else "")
            + (f" major={int(major)}" if major and int(major) > 0 else ""))


OPEN_STATUSES = ("open", "unresolved")


def entry_counts(entries: Iterable[Any]) -> tuple[int, int]:
    """(open, resolved) of an error list (`ErrorEntry` objects or their dicts)."""
    o = r = 0
    for e in entries or ():
        st = e.get("status") if isinstance(e, dict) else getattr(e, "status", None)
        o += st in OPEN_STATUSES
        r += st == "resolved"
    return o, r


def counts_open_resolved(counts: Optional[dict]) -> tuple[int, int]:
    """(open, resolved) from a status → count dict (`FleetProjectResult.error_counts`)."""
    c = counts or {}
    return sum(int(c.get(s, 0) or 0) for s in OPEN_STATUSES), int(c.get("resolved", 0) or 0)


def report_exit_code(report: Any) -> int:
    """The `sbmig run` exit code of a report: 3 aborted (flag `aborted[:…]` / `mvn-not-found`), 0 green with nothing
    blocked, else 2 (`sbmig.cli.run_exit_code` is this function)."""
    flags = list(getattr(report, "flags", []) or [])
    if any(f == "aborted" or f.startswith("aborted:") or f == "mvn-not-found" for f in flags):
        return 3
    if getattr(report, "success", False) and not getattr(report, "blocked", None):
        return 0
    return 2


def report_result_line(report: Any, exit_code: Optional[int] = None, run_id: Optional[str] = None) -> str:
    """The `SBMIG-RESULT …` line of a finished `sbmig run` (`run_id` overrides the report's; `exit_code` defaults
    to `report_exit_code(report)`)."""
    code = report_exit_code(report) if exit_code is None else exit_code
    o, r = entry_counts(getattr(report, "error_entries", None) or [])
    return result_line(run_id or getattr(report, "run_id", "-"), code, bool(getattr(report, "success", False)),
                       unresolved=len(getattr(report, "unresolved", []) or []),
                       blocked=len(getattr(report, "blocked", []) or []),
                       llm_calls=getattr(getattr(report, "llm_usage", None), "calls", 0) or 0,
                       attention=report_attention(report, code),
                       review=getattr(getattr(report, "structure_review", None), "verdict", None),
                       open_errors=o, resolved_errors=r, major=len(getattr(report, "operator_checks", None) or []))
