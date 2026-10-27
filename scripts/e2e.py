#!/usr/bin/env python3
"""End-to-end run of `sbmig run` over the fixture projects, with asserted expectations.

    python scripts/e2e.py                         # all fixtures (happy path + failure scenarios)
    python scripts/e2e.py --install-libs          # first install fixtures/libs (simulated in-house libraries)
    python scripts/e2e.py --only boot2-javax-lib,missing-artifact
    python scripts/e2e.py --expect-only           # just print what every fixture is expected to produce
    python scripts/e2e.py --only boot2,boot3 --stop-after spring --llm-provider fake
    python scripts/e2e.py --only boot2-hystrix-command --llm-config config/llm-anthropic.yml   # real model, not the replay

For each fixture it runs
    python -m sbmig.cli run fixtures/projects/<name> --config config/artifacts.local-e2e.yml
        [--config fixtures/projects/<name>/sbmig.e2e.yml] --out out/e2e [...]
captures the exit code + output, finds the run directory the CLI created under --out, parses its report.json
(a serialized RunReport) and evaluates the `expect` block of the fixture's expected.json against it:

    "expect": {"success": bool, "exit_code": int, "max_llm_calls": int, "max_duration_s": number,
               "blocked": [{"reason": str, "artifact": "g:a", "owner_class": str}],   # exact set (by match)
               "flags": ["werror-removed", "regex:source-encoding-set:(cp949|ms949)"],   # must be present
               "optional_bumped": ["g:a old→new", "regex:..."],                         # must be present
               "unresolved_max": int,
               "journal_events": [{"event": "restructure", "ok": true}],                # subset match on journal.jsonl
               "phases": {"restructure": "ok"},                                        # ok | FAIL | skipped
               "post_checks": [{"file_decodes_as": {"path", "encoding", "contains", "not_contains"}},
                               {"file_contains": {...}}, {"pom_parent": {...}}, {"pom_dependency_absent": {...}},
                               {"pom_dependency_present": {"path", "artifacts": ["g:a"]}},
                               {"pom_dependency_unversioned": {...}},   # declared, no <version> (parent-managed)
                               {"pom_dependency_versioned": {...}},     # declared with a <version>
                               {"pom_packaging": {"path", "packaging": "war"}},   # <packaging> (absent = jar)
                               {"pom_module_list": {"path", "modules": [...], "any_order": false}},  # exact <modules>
                               {"file_exists": {"path"}}, {"file_absent": {"path"}},    # files or dirs, globs ok
                               {"file_same_as": {"path", "source"}}],   # every match byte-equal to the fixture's
                                                                        # as-is `source` (binaries kept intact)
               "advisories": [{"id": "springfox", "policy": "recipe", "outcome": "replaced",   # report.advisories:
                               "recipe_source": "advisory"}],                  # entry by advisory_id, fields equal
               "advisories_exact": true,                                       # no unlisted findings allowed
               "advisories_absent": ["log4j1"],                                # these ids must not be reported
               "handover": {"changes_min": 1, "carried_over_min": 1, "carried_over_max": 3,
                            "parent_after": "org.springframework.boot:spring-boot-starter-parent",  # prefix
                            "spring_boot_after": "4.1.0",                      # *_before/*_after: exact
                            "from_here_contains": ["regex:(?i).*hystrix.*", "plain substring"],
                            "removed_contains": ["g:a"], "added_contains": ["g:a[:v]"]},
               "followups_contains": ["regex:(?i).*resilience4j.*"],             # over manual_followups
               "properties": {"external_min": 3,                   # report.properties (else profile.properties):
                              "external": ["mail.host"],           # keys that must be `external` findings
                              "absent": ["catalog.title"],         # keys that must NOT be `external`
                              "external_in": {"order-service": ["order.tax-rate"]},   # MSA: external findings
                                                                   # whose `service` is that deployable
                              "written": ["mail.host"],            # ⊆ placeholders_written
                              "placeholder_file_contains": ["mail.host=__SET_ME__"]},   # substrings of
                                                                   # placeholder_files[0] in the migrated project
               "kb": {"dir": "out/e2e/kb-method-rename",           # the fixture's own kb.dir (sbmig.e2e.yml)
                      "candidates_min": 1,                         # *.yml recipes in <dir>/recipes/candidates
                      "harvest_candidates_min": 1,                 # len(report.harvest.candidates_created)
                      "recipes": [{"recipe": "org.openrewrite.java.ChangeMethodName",
                                   "options": {"newMethodName": "remoteClientIp"},          # subset of the entry
                                   "name": "LegacyRequestHelperClientIpToRemoteClientIp"}]},
               # v2.1 (2026-10-27): the run's error list, report.error_entries (one ErrorEntry per error)
               "error_entries": {"min": 2, "max": 40,              # len(report.error_entries)
                                 "status": {"resolved": 2},        # exact count per status (open / pom-verify /
                                                                   # resolved / blocked / unresolved / note)
                                 "status_min": {"resolved": 1}, "status_max": {"unresolved": 0},
                                 "open_max": 0,                    # status open + unresolved (SBMIG-RESULT open=)
                                 "seen": {"compile": 3}, "seen_min": {"build": 1},   # by where it was first seen
                                 "cases": [{"l2": "compile|package_does_not_exist|jakarta.xml.bind",  # or "case"
                                            "entries_min": 1, "status": "resolved",   # every entry of the case
                                            "sources": ["llm-new"],                  # exact sequence of its tries
                                            "sources_contain": ["kb-script"],        # ordered subsequence
                                            "results": ["improved"],                 # ordered subsequence
                                            "versions": [1],                         # script versions of the tries
                                            "applied": "edits",                      # some try applied this
                                            "llm_calls": 1,                          # sum over the case's tries
                                            "resolved": "regex:r\\d+ try \\d+", "script": "regex:cases/.*"}],
                                 "records": [{"file": "regex:.*/B\\.java", "status": "resolved", "seen": "compile",
                                              "tries_min": 1, "sources": ["llm-new"], "results": ["improved"]}],
                                 "run_files": ["errors.jsonl", "errors.md"],
                                 "kb_errors_min": 2,               # lines in <expect.kb.dir>/errors.jsonl
                                 "kb_cases_min": 1},               # <kb>/cases/*/case.json (+ cases.md)
               # legacy (2026-10-23, ErrorCaseRecord): evaluated against the v2.1 error list, see below
               "error_cases": {"min": 2,                          # len(report.error_cases): one record per error
                               "status": {"resolved": 2},          # exact count for each listed status
                               "status_min": {"blocked": 1},       # at least that many
                               "records": [{"file": "regex:.*/ClientIpFilter\\.java", "status": "resolved",
                                            "fix_method": "llm", "change_kind": "text_replace",
                                            "replace_rule_candidate": true,
                                            "replace_pairs": [{"old": "clientIp", "new": "remoteClientIp"}],
                                            "attempts_min": 2,       # len(attempts) (failed ones included)
                                            "attempt_outcomes": ["apply_failed", "improved"],   # in this order,
                                                                     # as a subsequence of the history
                                            "failure_reason": "regex:(?i).*no change.*"}],      # every listed
                                                                     # field must match one and the same record
                               "run_files": ["error_cases.jsonl", "fix_attempts.jsonl", "error_cases.csv"],
                               "kb_errors_min": 2,                 # lines in <expect.kb.dir>/errors.jsonl
                               "kb_attempts_min": 3,               # lines in <expect.kb.dir>/fix_attempts.jsonl
                               "kb_cases_min": 1},                 # <expect.kb.dir>/catalog/**.yml (+ views/cases.md)
               # v2 (flow-v2, 2026-10-26)
               "structure_review": {"verdict": "issues",           # report.structure_review: ok | issues | failed |
                                                                   # skipped (a list = any of them)
                                    "critical": 1, "critical_min": 1, "critical_max": 1,   # severity critical
                                    "warnings_min": 0, "warnings_max": 2,
                                    "issues_contain": ["regex:critical module/comp-jar/pom.xml: .*MS949.*"],
                                                                   # over "<severity> <file>: <problem> -> <fix_hint>"
                                    "sbmig_check": false,          # an issue sbmig added itself ("(sbmig check) …")
                                    "script_status": "ok",         # script_result.status ("none" = no script ran)
                                    "committed": true, "reverted": false,
                                    "changed_files": ["module/comp-jar/pom.xml"],   # ⊆ script_result.changed_files
                                    "llm_calls_max": 1},
               "scripts": {"dir": "out/e2e/kb-jaxb",               # the fixture's KB (default: expect.kb.dir)
                           "attempts": {"new": 1, "augment": 0, "reuse": 1},   # exact count of script attempts
                           "attempts_min": {"new": 1}, "attempts_max": {"augment": 1},  # by FixAttempt.script_mode
                           "reuse_without_llm": true,              # every reuse attempt made no LLM call
                           "cases": [{"l2": "compile|package_does_not_exist|jakarta.xml.bind",   # or "case": id
                                      "modes": ["reuse"],          # exact sequence of this case's attempt modes
                                      "outcomes": ["build_ok"],    # ordered subsequence of its outcomes
                                      "versions": [1],             # exact sequence of script_version
                                      "llm_calls": 0}],            # sum of the case's attempts' llm_usage.calls
                           "stored": [{"l2": "compile|cannot_find_symbol|clientIp", "version": 2,
                                       "parent": 1, "status": "active",          # <dir>/scripts/<case>/vN.meta.json
                                       "successes_min": 1, "failures_min": 0, "partials_min": 0,
                                       "success_projects_min": 2, "last_outcome": "build_ok",
                                       "code_contains": ["remoteClientIp"]}],    # substrings of vN.py
                           "versions": {"compile|cannot_find_symbol|clientIp": 2}}}   # exact stored versions per
                                                                   # case (key: case id or L2 signature)

`*_contains` entries: `regex:<re>` = full match (re.DOTALL) of some entry, otherwise a substring of some entry;
`removed_contains`/`added_contains` match by "g:a" (plus a version prefix when given).
`error_cases`: string fields of a wanted record match like `*_contains` entries (`regex:` = full match, else
equality); `replace_pairs` entries must each be in the record's pairs (old / new equal).
`kb`: reads every recipe document of `<repo>/<dir>/recipes/{candidates,active}/*.yml` (multi-document YAML); each
wanted recipe must match a document (`name` equal when given) with a `recipeList` entry `{<recipe>: {options}}` whose
options are a superset of `options`. `kb.dir` must lie under `out/`: run_fixture deletes it before the run so a re-run
is deterministic (a fixture that exercises the KB sets its own `kb.dir`, because every KB candidate is tried in every
later plan). v2 reuse scenario: `kb.after: <fixture>` = this fixture continues the KB that <fixture> filled earlier in
the SAME e2e run (the dir is not cleared; when <fixture> did not run before it, the fixture fails with a precondition
mismatch — run them together, e.g. `--only pure-java,java6-legacy`).
`scripts` / `structure_review` (v2): the script attempts are the report's `phases[].attempts` with a `script_mode`;
a case is named by its id (`case`) or by its L2 signature (`l2`, hashed like `sbmig.errors.common.case_id_for`).
v2.1 KB layout: `scripts.stored` / `scripts.versions` read `<dir>/cases/<case>/case.json` (CaseInfo.scripts) and
`fix_v<N>.py`; `status: active` = unverified or proven, `parent: N` = the version was an augment (source
llm-augment of v N+1), `last_outcome` = last_result, `success_projects_min` = projects, `partials_min` is not
recorded any more (always 0).
`error_entries` (v2.1): records / cases match like `error_cases` records; a case's tries are de-duplicated over its
entries (one try fixes the whole case). `error_cases` (legacy) is mapped onto the error list: statuses
resolved_indirectly → resolved, not_visible → unresolved; `fix_method` script / llm → a try from llm-new (or
llm-edit), script_reuse → kb-script, script_augment → llm-augment; `attempt_outcomes` → the tries' results,
`attempts_min` → tries_min; run files error_cases.* / fix_attempts.jsonl → errors.jsonl / errors.md; kb_attempts_min
counts the tries in <kb>/errors.jsonl, kb_cases_min the case.json files; any other legacy record field is reported
as having no v2.1 equivalent.

Missing report fields are treated as empty (the engine features are built in parallel) — a mismatch is reported,
the script never crashes on report shape. A summary table (with an `expect` PASS/FAIL column and the first
mismatch) is printed and written to <out>/summary.md, raw data to <out>/summary.json. Exit code: 0 when every
selected fixture meets its expectations (fixtures without an `expect` block must reach a successful final build),
1 otherwise, 2 for usage errors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO / "fixtures" / "projects"
LIBS_DIR = REPO / "fixtures" / "libs"
INSTALL_LIBS = LIBS_DIR / "install.sh"
DEFAULT_CONFIG = REPO / "config" / "artifacts.local-e2e.yml"
FIXTURE_CONFIG = "sbmig.e2e.yml"                 # per-fixture config override, appended as an extra --config
DEFAULT_OUT = REPO / "out" / "e2e"
DEFAULT_JAVA_HOME = "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"
DEFAULT_LOCAL_REPO = Path.home() / ".m2" / "repository"
DEFAULT_ORDER = ["pure-java", "servlet-tomcat", "spring5-xml", "boot2", "boot3", "msa",
                 "boot2-javax-lib", "boot2-javax-lib-optional", "euckr-legacy", "enforcer-java8-werror",
                 "missing-artifact", "msa-shared-lib", "boot2-cloud-legacy", "boot2-method-rename",
                 "boot2-hystrix-command", "multi-module-merge", "legacy-external-config", "java6-legacy",
                 "multi-module-conflict", "legacy-ibatis", "legacy-role-parents", "company-boot-single",
                 "company-spring5-web", "company-servlet-jsp"]
ARROW = "→"
NS = "{http://maven.apache.org/POM/4.0.0}"


@dataclass
class FixtureResult:
    fixture: str
    exit_code: int | None = None
    case_type: str = "?"
    phases_ok: str = "-"                 # e.g. "3/4"
    phases: list[str] = field(default_factory=list)   # "jdk21:ok", "spring:FAIL", "restructure:skipped"
    final_build_ok: bool | None = None
    success: bool | None = None
    llm_calls: int | None = None
    unresolved: int | None = None
    blocked: list[str] = field(default_factory=list)          # "reason artifact" per BlockedItem
    optional_bumped: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    run_dir: str | None = None
    report_path: str | None = None
    flags: list[str] = field(default_factory=list)
    error: str | None = None
    command: list[str] = field(default_factory=list)
    log_path: str | None = None
    advisories: list[str] = field(default_factory=list)     # "advisory_id:outcome" per AdvisoryFinding
    carried_over: int | None = None                         # len(handover.carried_over); None = no handover
    review: str | None = None                               # v2: structure_review.verdict (None = no review)
    scripts: str = "-"                                      # v2: script attempts by mode, e.g. "new 2, aug 1, reuse 1"
    errors: str = "-"                                       # v2.1: error list by status, e.g. "resolved 23, unresolved 2"
    expect_ok: bool | None = None                           # None = fixture has no expect block
    mismatches: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------- fixtures
def discover_fixtures() -> list[str]:
    # a fixture = a directory with expected.json (ear-sibling-poms has no root pom.xml: intake generates it)
    found = sorted(p.name for p in FIXTURES_DIR.iterdir()
                   if p.is_dir() and ((p / "pom.xml").exists() or (p / "expected.json").exists())) \
        if FIXTURES_DIR.exists() else []
    ordered = [n for n in DEFAULT_ORDER if n in found]
    return ordered + [n for n in found if n not in ordered]


def load_expected(fixture: str) -> dict:
    p = FIXTURES_DIR / fixture / "expected.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fixture_config(fixture: str) -> Path | None:
    p = FIXTURES_DIR / fixture / FIXTURE_CONFIG
    return p if p.exists() else None


def build_command(fixture: str, args: argparse.Namespace, python: str = sys.executable) -> list[str]:
    cmd = [python, "-m", "sbmig.cli", "run", str(FIXTURES_DIR / fixture), "--config", str(args.config)]
    extra_cfg = fixture_config(fixture)
    if extra_cfg is not None:
        cmd += ["--config", str(extra_cfg)]
    if getattr(args, "llm_config", None):            # real-LLM profile LAST: overrides a fixture's replayed fake LLM
        cmd += ["--config", str(args.llm_config)]
    cmd += ["--out", str(args.out)]
    if args.stop_after:
        cmd += ["--stop-after", args.stop_after]
    if args.llm_provider and args.llm_provider_flag:
        cmd += ["--llm-provider", args.llm_provider]
    cmd += list(args.extra or [])
    return cmd


def build_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    if args.java_home:
        env["JAVA_HOME"] = str(args.java_home)
        env["JAVA21_HOME"] = str(args.java_home)       # config/artifacts.local-e2e.yml: build.java_home
        env["PATH"] = f"{Path(args.java_home) / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    if args.llm_provider:
        env["SBMIG_LLM__PROVIDER"] = args.llm_provider  # config env override, works even without a CLI flag
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _gav_path(local_repo: Path, gav: str) -> Path:
    """The installed artifact: `<a>-<v>.jar`, or the `.war` when only a WAR is there (legacy-base-web, 2026-10-23)."""
    g, a, v = gav.split(":")[:3]
    base = local_repo / Path(*g.split(".")) / a / v
    jar = base / f"{a}-{v}.jar"
    war = base / f"{a}-{v}.war"
    return war if war.exists() and not jar.exists() else jar


def preflight(fixture: str, expected: dict, local_repo: Path = DEFAULT_LOCAL_REPO) -> str | None:
    """Problems with the environment that would make the fixture's outcome meaningless (None = fine)."""
    for gav in expected.get("requires_libs") or []:
        if not _gav_path(local_repo, gav).exists():
            return f"precondition: {gav} not in {local_repo} (run fixtures/libs/install.sh or --install-libs)"
    for gav in expected.get("requires_absent") or []:
        if _gav_path(local_repo, gav).exists():
            return f"precondition: {gav} must NOT be in {local_repo} (delete {_gav_path(local_repo, gav).parent})"
    return None


def install_libs(local_repo: str | None = None) -> int:
    cmd = ["bash", str(INSTALL_LIBS)] + ([local_repo] if local_repo else [])
    print(f"==> installing fixture libraries: {' '.join(cmd)}", flush=True)
    try:
        return subprocess.run(cmd, check=False, cwd=REPO).returncode
    except OSError as e:
        print(f"cannot run {INSTALL_LIBS}: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------------------------- report access
def _get(d: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _ga(artifact: Any) -> str:
    """'g:a' of a GAV dict ({group_id, artifact_id, ...}) or a 'g:a[:v]' string; '' when unknown."""
    if isinstance(artifact, dict):
        g, a = artifact.get("group_id") or artifact.get("groupId"), artifact.get("artifact_id") or \
            artifact.get("artifactId")
        return f"{g}:{a}" if g and a else ""
    if isinstance(artifact, str) and artifact.count(":") >= 1:
        return ":".join(artifact.split()[0].split(":")[:2])     # "g:a:v (advisory:x)" → the token before the space
    return ""


def blocked_items(report: dict) -> list[dict]:
    """Top-level RunReport.blocked plus PhaseResult.blocked (deduplicated by id / content)."""
    items: list[dict] = []
    seen: set[str] = set()
    for b in _list(report.get("blocked")) + [b for p in _list(report.get("phases")) if isinstance(p, dict)
                                              for b in _list(p.get("blocked"))]:
        if not isinstance(b, dict):
            continue
        key = str(b.get("id") or json.dumps(b, sort_keys=True, default=str))
        if key not in seen:
            seen.add(key)
            items.append(b)
    return items


def _norm_bump(s: str) -> str:
    return re.sub(r"\s*(->|=>|→)\s*", ARROW, s.strip())


def optional_bumps(report: dict, journal: list[dict]) -> list[str]:
    """Every 'g:a old→new' the engine recorded: report fields, restructure/bump journal events, phase notes."""
    out: list[str] = []

    def add(v: Any) -> None:
        for s in (v if isinstance(v, list) else [v]):
            if isinstance(s, str) and ARROW in _norm_bump(s):
                n = _norm_bump(s)
                if n not in out:
                    out.append(n)

    add(report.get("optional_bumped"))
    add(_get(report, "restructure", "optional_bumped"))
    for ev in journal:
        add(ev.get("optional_bumped"))
        if ev.get("event") in ("optional_bump", "optional_bumped") and isinstance(ev.get("bumped"), list):
            add(ev["bumped"])
    for p in _list(report.get("phases")):
        for note in _list(p.get("notes") if isinstance(p, dict) else None):
            if not isinstance(note, str):
                continue
            m = re.search(r"optional (?:artifacts? )?bumped:\s*(.*?)(?:;|$)", note)
            if m and m.group(1).strip() not in ("", "-"):
                add([s for s in m.group(1).split(", ")])
    return out


def read_journal(run_dir: Path | None) -> list[dict]:
    if run_dir is None or not (run_dir / "journal.jsonl").exists():
        return []
    events = []
    for line in (run_dir / "journal.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def apply_report(res: FixtureResult, report: dict, journal: list[dict] | None = None) -> None:
    res.case_type = str(report.get("case_type") or _get(report, "profile", "case_type", default="?"))
    phases = [p for p in _list(report.get("phases")) if isinstance(p, dict)]
    ran = [p for p in phases if not p.get("skipped")]
    res.phases = [f"{p.get('phase')}:{'skipped' if p.get('skipped') else ('ok' if p.get('ok') else 'FAIL')}"
                  for p in phases]
    res.phases_ok = f"{sum(1 for p in ran if p.get('ok'))}/{len(ran)}"
    fb = report.get("final_build")
    res.final_build_ok = bool(fb.get("ok")) if isinstance(fb, dict) else None
    res.success = report.get("success")
    res.llm_calls = _get(report, "llm_usage", "calls", default=None)
    res.unresolved = len(_list(report.get("unresolved")))
    res.flags = [f for f in _list(report.get("flags")) if isinstance(f, str)]
    res.blocked = [f"{b.get('reason')} {_ga(b.get('artifact')) or b.get('owner_class') or '-'}"
                   for b in blocked_items(report)]
    res.optional_bumped = optional_bumps(report, journal or [])
    res.advisories = [f"{_advisory_id(a)}:{a.get('outcome') or 'unknown'}" for a in advisory_findings(report)]
    ho = report.get("handover")
    res.carried_over = len(carried_over_items(ho)) if isinstance(ho, dict) else None
    rv = report.get("structure_review")
    res.review = str(rv.get("verdict") or "?") if isinstance(rv, dict) else None
    res.scripts = format_script_modes(script_attempts(report))
    res.errors = format_error_counts(error_entries(report))


# ---------------------------------------------------------------------------------------------- v2.1 error list
ENTRY_STATUS_ORDER = ("unresolved", "open", "blocked", "pom-verify", "resolved", "note")
OPEN_STATUSES = ("open", "unresolved")


def error_entries(report: dict) -> list[dict]:
    return [e for e in _list(report.get("error_entries")) if isinstance(e, dict)]


def _count_by(entries: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        k = str(e.get(key) or "")
        out[k] = out.get(k, 0) + 1
    return out


def format_error_counts(entries: list[dict]) -> str:
    c = _count_by(entries, "status")
    keys = [k for k in ENTRY_STATUS_ORDER if c.get(k)] + sorted(k for k in c if k not in ENTRY_STATUS_ORDER)
    return ", ".join(f"{k} {c[k]}" for k in keys) or "-"


def _tries(e: dict) -> list[dict]:
    return [t for t in _list(e.get("tries")) if isinstance(t, dict)]


def _try_key(t: dict) -> tuple:
    return (t.get("round"), t.get("n"), t.get("source"), t.get("script_version"), t.get("commit"))


def case_tries(entries: list[dict]) -> list[dict]:
    """The distinct tries of some entries (a try fixes the whole case, every entry carries it), by (round, n)."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in entries:
        for t in _tries(e):
            if _try_key(t) not in seen:
                seen.add(_try_key(t))
                out.append(t)
    return sorted(out, key=lambda t: (int(t.get("round") or 0), int(t.get("n") or 0)))


def _subsequence(want: list, got: list) -> bool:
    it = iter(got)
    return all(any(g == w for g in it) for w in want)


def _tries_match(want: dict, tries: list[dict]) -> list[str]:
    """Mismatches of the try-level keys (sources / sources_contain / results / versions / applied / llm_calls /
    tries_min / _sources_any) against `tries`."""
    mm: list[str] = []
    sources = [str(t.get("source")) for t in tries]
    results = [str(t.get("result")) for t in tries]
    if want.get("tries_min") is not None and len(tries) < int(want["tries_min"]):
        mm.append(f"tries={len(tries)} < min {want['tries_min']}")
    if want.get("sources") is not None and sources != list(want["sources"]):
        mm.append(f"sources {sources} expected {list(want['sources'])}")
    if want.get("sources_contain") and not _subsequence(list(want["sources_contain"]), sources):
        mm.append(f"sources {sources} lack the sequence {list(want['sources_contain'])}")
    if want.get("_sources_any") and not set(sources) & set(want["_sources_any"]):
        mm.append(f"sources {sources} have none of {list(want['_sources_any'])}")
    if want.get("results") and not _subsequence(list(want["results"]), results):
        mm.append(f"results {results} lack the sequence {list(want['results'])}")
    versions = [int(t.get("script_version") or 0) for t in tries if t.get("script_version")]
    if want.get("versions") is not None and versions != [int(v) for v in want["versions"]]:
        mm.append(f"script versions {versions} expected {list(want['versions'])}")
    if want.get("applied") and not any(t.get("applied") == want["applied"] for t in tries):
        mm.append(f"no try applied {want['applied']!r} (got: {[t.get('applied') for t in tries]})")
    if want.get("llm_calls") is not None and sum(int(t.get("llm_calls") or 0) for t in tries) != int(want["llm_calls"]):
        mm.append(f"llm_calls={sum(int(t.get('llm_calls') or 0) for t in tries)} expected {want['llm_calls']}")
    return mm


_TRY_KEYS = ("tries_min", "sources", "sources_contain", "_sources_any", "results", "versions", "applied", "llm_calls")


def _entry_matches(want: dict, e: dict) -> bool:
    for k, v in want.items():
        if k in _TRY_KEYS:
            continue
        if k == "case":
            if e.get("case_id") != case_ref({"case": v}):
                return False
        elif k == "l2" and "case" not in want:
            if e.get("case_id") != case_ref({"l2": v}) and not _field_matches(v, e.get("l2")):
                return False
        elif k == "status" and isinstance(v, list):
            if e.get("status") not in v:
                return False
        elif not _field_matches(v, e.get(k)):
            return False
    return not _tries_match({k: want[k] for k in _TRY_KEYS if k in want}, _tries(e))


def _entry_text(e: dict) -> str:
    loc = (e.get("file") or e.get("module") or "-") + (f":{e.get('line')}" if e.get("file") and e.get("line") else "")
    srcs = ",".join(f"{t.get('source')}:{t.get('result')}" for t in _tries(e))
    return f"{loc}:{e.get('status')}:{e.get('seen')}" + (f":{srcs}" if srcs else "")


def _kb_error_lines(kb_dir: Path | None) -> list[dict]:
    if kb_dir is None:
        return []
    out = []
    try:
        for line in (kb_dir / "errors.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line) if line.strip() else None
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
    except OSError:
        return []
    return out


def _kb_case_files(kb_dir: Path | None) -> list[Path]:
    if kb_dir is None or not (kb_dir / "cases").is_dir():
        return []
    return sorted((kb_dir / "cases").glob("*/case.json"))


def _counts_want(label: str, want: Any, got: dict[str, int], op: str, total: str) -> list[str]:
    mm: list[str] = []
    for k, n in (want or {}).items():
        have = got.get(k, 0)
        if (op == "==" and have != n) or (op == ">=" and have < n) or (op == "<=" and have > n):
            mm.append(f"{label} {k}={have} expected {op} {n} (got: {total})")
    return mm


def evaluate_error_entries(want: dict, report: dict, run_dir: Path | None, kb_dir: Path | None) -> list[str]:
    """`error_entries` expectation (v2.1): the run's error list (`report.error_entries`)."""
    mm: list[str] = []
    es = error_entries(report)
    total = format_error_counts(es)
    if want.get("min") is not None and len(es) < int(want["min"]):
        mm.append(f"error_entries={len(es)} < min {want['min']} (got: {total})")
    if want.get("max") is not None and len(es) > int(want["max"]):
        mm.append(f"error_entries={len(es)} > max {want['max']} (got: {total})")
    by_status = _count_by(es, "status")
    mm += _counts_want("error_entries status", want.get("status"), by_status, "==", total)
    mm += _counts_want("error_entries status", want.get("status_min"), by_status, ">=", total)
    mm += _counts_want("error_entries status", want.get("status_max"), by_status, "<=", total)
    if want.get("open_max") is not None:
        n = sum(by_status.get(s, 0) for s in OPEN_STATUSES)
        if n > int(want["open_max"]):
            mm.append(f"error_entries open={n} > max {want['open_max']} (got: {total})")
    by_seen = _count_by(es, "seen")
    seen_txt = ", ".join(f"{k} {v}" for k, v in sorted(by_seen.items())) or "none"
    mm += _counts_want("error_entries seen", want.get("seen"), by_seen, "==", seen_txt)
    mm += _counts_want("error_entries seen", want.get("seen_min"), by_seen, ">=", seen_txt)
    known = ", ".join(sorted({str(e.get("case_id")) for e in es if e.get("case_id")})) or "none"
    for c in _list(want.get("cases")):
        if not isinstance(c, dict):
            continue
        cid = case_ref(c)
        mine = [e for e in es if e.get("case_id") == cid]
        if not mine:
            mm.append(f"case {cid}: no entry (cases: {known[:200]})")
            continue
        if c.get("entries_min") is not None and len(mine) < int(c["entries_min"]):
            mm.append(f"case {cid}: entries={len(mine)} < min {c['entries_min']}")
        if c.get("status") is not None:
            wanted = c["status"] if isinstance(c["status"], list) else [c["status"]]
            bad = sorted({str(e.get("status")) for e in mine} - set(wanted))
            if bad:
                mm.append(f"case {cid}: status {bad} expected {wanted}")
        for key in ("resolved", "script"):
            if c.get(key) is not None and not any(_field_matches(c[key], e.get(key)) for e in mine):
                mm.append(f"case {cid}: no entry with {key} {c[key]!r} (got: {[e.get(key) for e in mine][:4]})")
        mm += [f"case {cid}: {m}" for m in _tries_match(c, case_tries(mine))]
    for w in _list(want.get("records")):
        if isinstance(w, dict) and not any(_entry_matches(w, e) for e in es):
            got = "; ".join(_entry_text(e) for e in es[:4]) or "none"
            mm.append(f"no error entry matching {json.dumps(w, ensure_ascii=False)[:160]} (got: {got[:200]})")
    for name in _list(want.get("run_files")):
        if run_dir is None or not (run_dir / str(name)).is_file():
            mm.append(f"run file {name} missing")
    if want.get("kb_errors_min") is not None:
        n = len(_kb_error_lines(kb_dir))
        if n < int(want["kb_errors_min"]):
            mm.append(f"kb errors.jsonl lines={n} < min {want['kb_errors_min']}")
    if want.get("kb_tries_min") is not None:
        n = sum(len(_tries(e)) for e in _kb_error_lines(kb_dir))
        if n < int(want["kb_tries_min"]):
            mm.append(f"kb errors.jsonl tries={n} < min {want['kb_tries_min']}")
    if want.get("kb_cases_min") is not None:
        n = len(_kb_case_files(kb_dir))
        if n < int(want["kb_cases_min"]):
            mm.append(f"kb cases={n} < min {want['kb_cases_min']}")
        if kb_dir is None or not (kb_dir / "cases.md").is_file():
            mm.append("kb cases.md missing")
    return mm


_LEGACY_STATUS = {"resolved_indirectly": "resolved", "not_visible": "unresolved"}
_LEGACY_SOURCES = {"script": ["llm-new", "llm-edit"], "llm": ["llm-new", "llm-edit"], "script_reuse": ["kb-script"],
                   "script_augment": ["llm-augment"], "review": ["review"]}
_LEGACY_RUN_FILES = {"error_cases.jsonl": "errors.jsonl", "fix_attempts.jsonl": "errors.jsonl",
                     "error_cases.csv": "errors.md"}
_LEGACY_RECORD_KEYS = {"file", "line", "module", "message", "status", "seen", "kind"}


def legacy_error_cases_want(want: dict) -> tuple[dict, list[str]]:
    """A legacy `error_cases` expectation as an `error_entries` one + the fields that have no v2.1 equivalent."""
    out: dict[str, Any] = {}
    gaps: list[str] = []

    def status_map(d: dict) -> dict:
        m: dict[str, int] = {}
        for k, v in (d or {}).items():
            m[_LEGACY_STATUS.get(k, k)] = m.get(_LEGACY_STATUS.get(k, k), 0) + int(v)
        return m
    for k in ("min", "kb_errors_min", "kb_cases_min"):
        if want.get(k) is not None:
            out[k] = want[k]
    if want.get("status"):
        out["status"] = status_map(want["status"])
    if want.get("status_min"):
        out["status_min"] = status_map(want["status_min"])
    if want.get("kb_attempts_min") is not None:
        out["kb_tries_min"] = want["kb_attempts_min"]
    if want.get("run_files"):
        out["run_files"] = list(dict.fromkeys(_LEGACY_RUN_FILES.get(str(f), str(f)) for f in want["run_files"]))
    recs = []
    for r in _list(want.get("records")):
        if not isinstance(r, dict):
            continue
        n: dict[str, Any] = {}
        for k, v in r.items():
            if k == "status":
                n["status"] = _LEGACY_STATUS.get(v, v) if isinstance(v, str) else v
            elif k in _LEGACY_RECORD_KEYS:
                n[k] = v
            elif k == "fix_method":
                if v in _LEGACY_SOURCES:
                    n["_sources_any"] = _LEGACY_SOURCES[v]
                elif v != "none":
                    gaps.append(f"error_cases record fix_method {v!r} has no v2.1 equivalent")
            elif k == "attempt_outcomes":
                n["results"] = list(v)
            elif k == "attempts_min":
                n["tries_min"] = v
            else:
                gaps.append(f"error_cases record field {k!r} has no v2.1 equivalent")
        recs.append(n)
    if recs:
        out["records"] = recs
    return out, gaps


# ---------------------------------------------------------------------------------------------- v2 scripts / review
SCRIPT_MODES = ("new", "augment", "reuse")
_MODE_SHORT = {"new": "new", "augment": "aug", "reuse": "reuse"}


def script_attempts(report: dict) -> list[dict]:
    """v2 fix-script attempts (`phases[].attempts` with a `script_mode`), in run order."""
    return [a for p in _list(report.get("phases")) if isinstance(p, dict)
            for a in _list(p.get("attempts")) if isinstance(a, dict) and a.get("script_mode")]


def format_script_modes(attempts: list[dict]) -> str:
    counts = {m: sum(1 for a in attempts if a.get("script_mode") == m) for m in SCRIPT_MODES}
    return ", ".join(f"{_MODE_SHORT[m]} {n}" for m, n in counts.items() if n) or "-"


def case_ref(spec: dict | str) -> str:
    """A case id: `case` as given, or `l2` hashed like sbmig.kb.catalog.case_id_for (sha1(L2)[:10])."""
    if isinstance(spec, str):
        return spec if re.fullmatch(r"[0-9a-f]{10}", spec) else hashlib.sha1(spec.encode("utf-8")).hexdigest()[:10]
    if spec.get("case"):
        return str(spec["case"])
    return hashlib.sha1(str(spec.get("l2") or "").encode("utf-8")).hexdigest()[:10] if spec.get("l2") else ""


def _calls(a: dict) -> int:
    return int(_get(a, "llm_usage", "calls", default=0) or 0)


def _counts_check(label: str, want: Any, got: dict[str, int], op: str) -> list[str]:
    mm: list[str] = []
    for mode, n in (want or {}).items():
        have = got.get(mode, 0)
        bad = have != n if op == "==" else (have < n if op == ">=" else have > n)
        if bad:
            mm.append(f"{label} {mode}={have} expected {op} {n} (got: {format_script_modes_from(got)})")
    return mm


def format_script_modes_from(counts: dict[str, int]) -> str:
    return ", ".join(f"{m} {n}" for m, n in counts.items() if n) or "none"


def _case_json(kb_dir: Path, case_id: str) -> dict | None:
    try:
        data = json.loads((kb_dir / "cases" / case_id / "case.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_meta(kb_dir: Path, case_id: str, version: int) -> dict | None:
    """v2.1: the CaseScript of `<kb>/cases/<case>/case.json` in the shape the v2 `stored` keys read (parent from an
    augment source, last_outcome = last_result, success_projects = projects, partials 0)."""
    case = _case_json(kb_dir, case_id)
    s = next((x for x in _list((case or {}).get("scripts")) if isinstance(x, dict)
              and int(x.get("version") or 0) == int(version)), None)
    if s is None:
        return None
    return {**s, "parent": (int(version) - 1 if s.get("source") == "llm-augment" and int(version) > 1 else None),
            "last_outcome": s.get("last_result") or None, "success_projects": _list(s.get("projects")),
            "partials": 0}


def _script_code(kb_dir: Path, case_id: str, version: int) -> str:
    try:
        return (kb_dir / "cases" / case_id / f"fix_v{version}.py").read_text(encoding="utf-8")
    except OSError:
        return ""


def _stored_versions(kb_dir: Path, case_id: str) -> list[int]:
    case = _case_json(kb_dir, case_id)
    if case is not None:
        return sorted(int(x.get("version") or 0) for x in _list(case.get("scripts")) if isinstance(x, dict))
    d = kb_dir / "cases" / case_id
    if not d.is_dir():
        return []
    return sorted(int(m.group(1)) for f in d.iterdir() if (m := re.fullmatch(r"fix_v(\d+)\.py", f.name)))


def evaluate_scripts(want: dict, report: dict, kb_dir: Path | None) -> list[str]:
    """`scripts` expectation (v2): the run's script attempts by mode / case, and the versions in the fixture's KB."""
    mm: list[str] = []
    atts = script_attempts(report)
    counts = {m: sum(1 for a in atts if a.get("script_mode") == m) for m in SCRIPT_MODES}
    mm += _counts_check("script attempts", want.get("attempts"), counts, "==")
    mm += _counts_check("script attempts", want.get("attempts_min"), counts, ">=")
    mm += _counts_check("script attempts", want.get("attempts_max"), counts, "<=")
    if want.get("reuse_without_llm"):
        for a in atts:
            if a.get("script_mode") == "reuse" and _calls(a):
                mm.append(f"reuse attempt {a.get('id')} made {_calls(a)} LLM call(s)")
        if not counts["reuse"]:
            mm.append("reuse_without_llm: no reuse attempt in the run")
    for c in _list(want.get("cases")):
        if not isinstance(c, dict):
            continue
        cid = case_ref(c)
        mine = [a for a in atts if a.get("script_case_id") == cid]
        seen = ", ".join(sorted({str(a.get("script_case_id")) for a in atts})) or "none"
        if not mine:
            mm.append(f"case {cid}: no script attempt (cases with attempts: {seen})")
            continue
        modes = [str(a.get("script_mode")) for a in mine]
        if c.get("modes") is not None and modes != list(c["modes"]):
            mm.append(f"case {cid}: modes {modes} expected {list(c['modes'])}")
        versions = [int(a.get("script_version") or 0) for a in mine]
        if c.get("versions") is not None and versions != [int(v) for v in c["versions"]]:
            mm.append(f"case {cid}: versions {versions} expected {list(c['versions'])}")
        outcomes = [str(a.get("outcome")) for a in mine]
        it = iter(outcomes)
        if c.get("outcomes") and not all(any(o == w for o in it) for w in c["outcomes"]):
            mm.append(f"case {cid}: outcomes {outcomes} lack the sequence {list(c['outcomes'])}")
        if c.get("llm_calls") is not None and sum(_calls(a) for a in mine) != int(c["llm_calls"]):
            mm.append(f"case {cid}: llm_calls={sum(_calls(a) for a in mine)} expected {c['llm_calls']}")
    stored = [s for s in _list(want.get("stored")) if isinstance(s, dict)]
    versions_want = want.get("versions") if isinstance(want.get("versions"), dict) else {}
    if (stored or versions_want) and (kb_dir is None or not kb_dir.is_dir()):
        mm.append(f"scripts: kb dir {kb_dir} missing")
        return mm
    for s in stored:
        cid, v = case_ref(s), int(s.get("version") or 0)
        meta = _read_meta(kb_dir, cid, v)
        if meta is None:
            mm.append(f"stored script {cid} v{v} missing (versions: {_stored_versions(kb_dir, cid) or 'none'})")
            continue
        for key in ("parent", "status", "last_outcome"):
            if key not in s:
                continue
            if key == "status" and s[key] == "active":           # v2 "active" = v2.1 unverified or proven
                if meta.get("status") not in ("unverified", "proven"):
                    mm.append(f"stored script {cid} v{v}: status={meta.get('status')!r} expected active "
                              "(unverified / proven)")
            elif meta.get(key) != s[key]:
                mm.append(f"stored script {cid} v{v}: {key}={meta.get(key)!r} expected {s[key]!r}")
        for key, field_name in (("successes_min", "successes"), ("failures_min", "failures"),
                                ("partials_min", "partials")):
            if s.get(key) is not None and int(meta.get(field_name) or 0) < int(s[key]):
                mm.append(f"stored script {cid} v{v}: {field_name}={meta.get(field_name)} < min {s[key]}")
        for key, field_name in (("successes_max", "successes"), ("failures_max", "failures")):
            if s.get(key) is not None and int(meta.get(field_name) or 0) > int(s[key]):
                mm.append(f"stored script {cid} v{v}: {field_name}={meta.get(field_name)} > max {s[key]}")
        projects = _list(meta.get("success_projects"))
        if s.get("success_projects_min") is not None and len(projects) < int(s["success_projects_min"]):
            mm.append(f"stored script {cid} v{v}: success_projects={projects} < min {s['success_projects_min']}")
        if s.get("code_contains"):
            code = _script_code(kb_dir, cid, v)
            for needle in _list(s["code_contains"]):
                if needle not in code:
                    mm.append(f"stored script {cid} v{v} lacks {needle!r}")
    for key, n in versions_want.items():
        cid = case_ref(key)
        have = _stored_versions(kb_dir, cid)
        if len(have) != int(n):
            mm.append(f"stored versions of case {cid}: {have or 'none'} expected {n}")
    return mm


def _issue_text(i: dict) -> str:
    hint = f" -> {i.get('fix_hint')}" if i.get("fix_hint") else ""
    return f"{i.get('severity') or 'warning'} {i.get('file') or ''}: {i.get('problem') or ''}{hint}"


def evaluate_structure_review(want: dict, report: dict) -> list[str]:
    """`structure_review` expectation (v2): `report.structure_review` (verdict, issues, the script and its commit)."""
    rv = report.get("structure_review")
    if not isinstance(rv, dict):
        return ["no structure_review in report.json"]
    mm: list[str] = []
    issues = [i for i in _list(rv.get("issues")) if isinstance(i, dict)]
    critical = [i for i in issues if i.get("severity") == "critical"]
    warnings = [i for i in issues if i.get("severity") != "critical"]
    verdict = str(rv.get("verdict") or "")
    allowed = want.get("verdict")
    if allowed is not None and verdict not in (allowed if isinstance(allowed, list) else [allowed]):
        mm.append(f"structure_review verdict={verdict!r} expected {allowed!r} (note: {rv.get('note') or '-'})"[:300])
    for key, items, op in (("critical", critical, "=="), ("critical_min", critical, ">="),
                           ("critical_max", critical, "<="), ("warnings_min", warnings, ">="),
                           ("warnings_max", warnings, "<=")):
        n = want.get(key)
        if n is None:
            continue
        have = len(items)
        if (op == "==" and have != n) or (op == ">=" and have < n) or (op == "<=" and have > n):
            mm.append(f"structure_review {key.split('_')[0]}={have} expected {op} {n} "
                      f"(got: {_short([_issue_text(i) for i in items])})")
    texts = [_issue_text(i) for i in issues]
    for pat in _list(want.get("issues_contain")):
        if not _contains_match(pat, texts):
            mm.append(f"structure_review issues lack {pat!r} (got: {_short(texts)})")
    if want.get("sbmig_check") is not None:
        has = any(str(i.get("problem") or "").startswith("(sbmig check)") for i in issues)
        if has != bool(want["sbmig_check"]):
            mm.append(f"structure_review sbmig_check={has} expected {want['sbmig_check']}")
    res = rv.get("script_result") if isinstance(rv.get("script_result"), dict) else None
    status = str(res.get("status")) if res else "none"
    if want.get("script_status") is not None and status != want["script_status"]:
        err = (res or {}).get("error") or rv.get("note") or "-"
        mm.append(f"structure_review script_status={status} expected {want['script_status']} ({err})"[:300])
    if want.get("committed") is not None and bool(rv.get("commit")) != bool(want["committed"]):
        mm.append(f"structure_review commit={rv.get('commit')!r} expected committed={want['committed']}")
    if want.get("reverted") is not None and bool(rv.get("reverted")) != bool(want["reverted"]):
        mm.append(f"structure_review reverted={rv.get('reverted')} expected {want['reverted']}")
    changed = _strings((res or {}).get("changed_files"))
    for f in _list(want.get("changed_files")):
        if f not in changed:
            mm.append(f"structure_review script did not change {f} (changed: {_short(changed, 5)})")
    calls = int(_get(rv, "llm_usage", "calls", default=0) or 0)
    if want.get("llm_calls_max") is not None and calls > int(want["llm_calls_max"]):
        mm.append(f"structure_review llm_calls={calls} > max {want['llm_calls_max']}")
    return mm


def advisory_findings(report: dict) -> list[dict]:
    return [a for a in _list(report.get("advisories")) if isinstance(a, dict)]


def _advisory_id(a: dict) -> str:
    return str(a.get("advisory_id") or a.get("id") or "?")


def carried_over_items(handover: Any) -> list:
    """handover.carried_over, or the changes flagged carried_over when the list itself is missing."""
    if not isinstance(handover, dict):
        return []
    items = _list(handover.get("carried_over"))
    if items:
        return items
    return [c for c in _list(handover.get("changes")) if isinstance(c, dict) and c.get("carried_over")]


# ---------------------------------------------------------------------------------------------- expectations
def _pattern_match(pattern: str, values: list[str]) -> bool:
    if pattern.startswith("regex:"):
        rx = re.compile(pattern[len("regex:"):])
        return any(rx.fullmatch(v) for v in values)
    return pattern in values


def _contains_match(pattern: str, values: list[str]) -> bool:
    """`regex:<re>` = full match (DOTALL) of some value, otherwise a substring of some value."""
    if pattern.startswith("regex:"):
        rx = re.compile(pattern[len("regex:"):], re.DOTALL)
        return any(rx.fullmatch(v) for v in values)
    return any(pattern in v for v in values)


def _gav_listed(want: str, values: list[str]) -> bool:
    """'g:a[:v]' in a list of 'g:a[:v]' strings: same g:a, and the version starts with the wanted one if given."""
    parts = want.split(":")
    for v in values:
        if _ga(v) != _ga(want):
            continue
        if len(parts) < 3 or ":".join(v.split()[0].split(":")[2:]).startswith(":".join(parts[2:])):
            return True
    return False


def _strings(v: Any) -> list[str]:
    return [x for x in _list(v) if isinstance(x, str)]


def _short(values: list[str], limit: int = 3) -> str:
    if not values:
        return "none"
    more = f" (+{len(values) - limit})" if len(values) > limit else ""
    return "; ".join(x[:80] for x in values[:limit]) + more


HANDOVER_SCALARS = ("parent_before", "parent_after", "java_before", "java_after", "spring_boot_before",
                    "spring_boot_after")


def evaluate_advisories(expect: dict, report: dict) -> list[str]:
    mm: list[str] = []
    findings = advisory_findings(report)
    by_id = {_advisory_id(a): a for a in findings}
    got = ", ".join(f"{_advisory_id(a)}:{a.get('outcome') or 'unknown'}" for a in findings) or "none"
    want = [w for w in _list(expect.get("advisories")) if isinstance(w, dict)]
    for w in want:
        aid = str(w.get("id") or w.get("advisory_id") or "")
        a = by_id.get(aid)
        if a is None:
            mm.append(f"advisory {aid} missing (got: {got})")
            continue
        for k, v in w.items():
            if k in ("id", "advisory_id"):
                continue
            if a.get(k) != v:
                mm.append(f"advisory {aid}: {k}={a.get(k)!r} expected {v!r}")
    if expect.get("advisories_exact"):
        listed = {str(w.get("id") or w.get("advisory_id") or "") for w in want}
        for aid in by_id:
            if aid not in listed:
                mm.append(f"unexpected advisory {aid}")
    for aid in _list(expect.get("advisories_absent")):
        if aid in by_id:
            mm.append(f"advisory {aid} present but expected absent")
    return mm


def evaluate_handover(want: dict, report: dict) -> list[str]:
    mm: list[str] = []
    ho = report.get("handover")
    ho = ho if isinstance(ho, dict) else {}
    if not ho:
        mm.append("no handover in report.json")
    changes = [c for c in _list(ho.get("changes")) if isinstance(c, dict)]
    carried = carried_over_items(ho)
    if want.get("changes_min") is not None and len(changes) < want["changes_min"]:
        mm.append(f"handover changes={len(changes)} < min {want['changes_min']}")
    if want.get("carried_over_min") is not None and len(carried) < want["carried_over_min"]:
        mm.append(f"handover carried_over={len(carried)} < min {want['carried_over_min']}")
    if want.get("carried_over_max") is not None and len(carried) > want["carried_over_max"]:
        mm.append(f"handover carried_over={len(carried)} > max {want['carried_over_max']}")
    for key in HANDOVER_SCALARS:
        if want.get(key) is None:
            continue
        got = ho.get(key)
        ok = isinstance(got, str) and (got.startswith(want[key]) if key.startswith("parent_") else got == want[key])
        if not ok:
            mm.append(f"handover {key}={got!r} expected {want[key]!r}")
    from_here = _strings(ho.get("from_here"))
    for pat in _list(want.get("from_here_contains")):
        if not _contains_match(pat, from_here):
            mm.append(f"handover from_here lacks {pat!r} (got: {_short(from_here)})")
    for key in ("removed", "added"):
        values = _strings(ho.get(key))
        for ga in _list(want.get(f"{key}_contains")):
            if not _gav_listed(ga, values):
                mm.append(f"handover {key} lacks {ga} (got: {_short(values, 5)})")
    return mm


KB_RECIPE_SUBDIRS = ("candidates", "active")


def kb_recipe_docs(kb_dir: Path) -> list[tuple[str, dict]]:
    """(status dir, recipe document) for every YAML document in <kb_dir>/recipes/{candidates,active}/*.yml."""
    import yaml

    docs: list[tuple[str, dict]] = []
    for sub in KB_RECIPE_SUBDIRS:
        d = kb_dir / "recipes" / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yml")):
            try:
                loaded = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            docs += [(sub, doc) for doc in loaded if isinstance(doc, dict)]
    return docs


def _recipe_entries(doc: dict) -> list[tuple[str, dict]]:
    """recipeList as (recipe id, options): `- id` or `- {id: {options}}`."""
    out: list[tuple[str, dict]] = []
    for item in _list(doc.get("recipeList")):
        if isinstance(item, str):
            out.append((item, {}))
        elif isinstance(item, dict):
            for rid, opts in item.items():
                out.append((str(rid), opts if isinstance(opts, dict) else {}))
    return out


def _kb_recipe_matches(want: dict, doc: dict) -> bool:
    if want.get("name") and doc.get("name") != want["name"]:
        return False
    opts = want.get("options") if isinstance(want.get("options"), dict) else {}
    return any(rid == want.get("recipe") and all(k in got and got[k] == v for k, v in opts.items())
               for rid, got in _recipe_entries(doc))


def evaluate_kb(want: dict, report: dict, repo: Path | None = None) -> list[str]:
    """`kb` expectation: recipes the run left in its KB directory (+ the report's harvest block)."""
    mm: list[str] = []
    kb_dir = (repo or REPO) / str(want.get("dir") or "")
    if not want.get("dir") or not kb_dir.is_dir():
        mm.append(f"kb dir {want.get('dir')!r} missing")
        docs: list[tuple[str, dict]] = []
    else:
        docs = kb_recipe_docs(kb_dir)
    candidates = [doc for sub, doc in docs if sub == "candidates"]
    if want.get("candidates_min") is not None and len(candidates) < want["candidates_min"]:
        mm.append(f"kb candidates={len(candidates)} < min {want['candidates_min']}")
    if want.get("harvest_candidates_min") is not None:
        n = len(_list(_get(report, "harvest", "candidates_created")))
        if n < want["harvest_candidates_min"]:
            mm.append(f"harvest candidates_created={n} < min {want['harvest_candidates_min']}")
    got = "; ".join(f"{doc.get('name')}[{sub}]: " + ", ".join(rid for rid, _ in _recipe_entries(doc))
                    for sub, doc in docs) or "none"
    for w in _list(want.get("recipes")):
        if not isinstance(w, dict):
            continue
        if not any(_kb_recipe_matches(w, doc) for _, doc in docs):
            what = w.get("recipe") or "?"
            if w.get("name"):
                what += f" in {w['name']}"
            mm.append(f"kb recipe {what} {json.dumps(w.get('options') or {}, ensure_ascii=False)} missing "
                      f"(got: {got[:200]})")
    return mm


def _field_matches(want: Any, got: Any) -> bool:
    if isinstance(want, str) and want.startswith("regex:"):
        return isinstance(got, str) and re.fullmatch(want[len("regex:"):], got, re.DOTALL) is not None
    return got == want


def evaluate_error_cases(want: dict, report: dict, run_dir: Path | None, kb_dir: Path | None) -> list[str]:
    """Legacy `error_cases` expectation (2026-10-23, ErrorCaseRecord): v2.1 no longer writes those records, so the
    expectation is mapped onto the error list (`legacy_error_cases_want`) and evaluated as `error_entries`."""
    mapped, gaps = legacy_error_cases_want(want)
    return [m.replace("error_entries", "error_cases") for m in evaluate_error_entries(mapped, report, run_dir, kb_dir)] \
        + gaps


def kb_dir_to_clear(expect: dict | None, repo: Path | None = None) -> Path | None:
    """The fixture's KB directory (expect.kb.dir) when it is safely inside <repo>/out/, else None."""
    if not isinstance(expect, dict) or not isinstance(expect.get("kb"), dict) or not expect["kb"].get("dir"):
        return None
    if expect["kb"].get("after"):          # v2 reuse scenario: continues the KB another fixture filled
        return None
    repo = repo or REPO
    target = (repo / str(expect["kb"]["dir"])).resolve()
    out_root = (repo / "out").resolve()
    if target == out_root or out_root not in target.parents:
        return None
    return target


def _blocked_matches(exp: dict, item: dict) -> bool:
    if exp.get("reason") and str(item.get("reason")) != exp["reason"]:
        return False
    if exp.get("artifact") and _ga(item.get("artifact")) != _ga(exp["artifact"]):
        return False
    if exp.get("owner_class") and item.get("owner_class") != exp["owner_class"]:
        return False
    return not (exp.get("phase") and str(item.get("phase")) != exp["phase"])


def _resolve(project: Path, pattern: str) -> list[Path]:
    if any(c in pattern for c in "*?["):
        return sorted(p for p in project.glob(pattern) if p.is_file() and ".git" not in p.parts)
    p = project / pattern
    return [p] if p.is_file() else []


def _pom_root(path: Path):
    import xml.etree.ElementTree as ET
    return ET.parse(path).getroot()


def _pom_text(el, tag: str) -> str | None:
    if el is None:
        return None
    child = el.find(NS + tag)
    if child is None:
        child = el.find(tag)
    return child.text.strip() if child is not None and child.text else None


def _pom_dependencies(root) -> set[str]:
    deps = set()
    for dep in list(root.iter(NS + "dependency")) + list(root.iter("dependency")):
        g, a = _pom_text(dep, "groupId"), _pom_text(dep, "artifactId")
        if g and a:
            deps.add(f"{g}:{a}")
    return deps


def _pom_modules(root) -> list[str]:
    """The top-level `<modules>` entries in declaration order (profile modules are not counted)."""
    mods = root.find(NS + "modules")
    if mods is None:
        mods = root.find("modules")
    if mods is None:
        return []
    return [m.text.strip() for m in list(mods) if isinstance(m.tag, str) and m.tag.endswith("module") and m.text]


def _pom_dependency_versions(root) -> dict[str, list[str | None]]:
    """"g:a" → the <version> text of every declaration of it (None = declared without a <version> element)."""
    out: dict[str, list[str | None]] = {}
    for dep in list(root.iter(NS + "dependency")) + list(root.iter("dependency")):
        g, a = _pom_text(dep, "groupId"), _pom_text(dep, "artifactId")
        if g and a:
            has_version = dep.find(NS + "version") is not None or dep.find("version") is not None
            out.setdefault(f"{g}:{a}", []).append((_pom_text(dep, "version") or "") if has_version else None)
    return out


def run_post_check(check: dict, project: Path | None, fixture_dir: Path | None = None) -> str | None:
    """None when the check passes, else a one-line mismatch. `fixture_dir`: the as-is fixture (`file_same_as`)."""
    if not isinstance(check, dict) or len(check) != 1:
        return f"post_check malformed: {check!r}"
    kind, spec = next(iter(check.items()))
    spec = spec if isinstance(spec, dict) else {}
    if project is None or not project.is_dir():
        return f"{kind}: no migrated project directory"
    if kind in ("file_exists", "file_absent"):        # files or directories; `**` globs allowed
        pattern = str(spec.get("path", ""))
        hits = [p for p in project.glob(pattern) if ".git" not in p.relative_to(project).parts] \
            if any(c in pattern for c in "*?[") else ([project / pattern] if (project / pattern).exists() else [])
        if kind == "file_exists" and not hits:
            return f"file_exists: {pattern} not found in {project.name}/"
        if kind == "file_absent" and hits:
            return f"file_absent: {hits[0].relative_to(project).as_posix()} exists in {project.name}/"
        return None
    files = _resolve(project, str(spec.get("path", "")))
    if not files:
        return f"{kind}: {spec.get('path')} not found in {project.name}/"
    if kind == "file_same_as":                       # 2026-10-23: binaries must survive the migration unchanged
        source = (fixture_dir / str(spec.get("source", ""))) if fixture_dir is not None else None
        if source is None or not source.is_file():
            return f"file_same_as: as-is source {spec.get('source')!r} not found"
        want = source.read_bytes()
        for f in files:
            got = f.read_bytes()
            if got != want:
                return (f"file_same_as: {f.relative_to(project).as_posix()} ({len(got)} bytes) differs from "
                        f"{spec.get('source')} ({len(want)} bytes)")
        return None
    if kind in ("file_decodes_as", "file_contains"):
        enc = spec.get("encoding") or "utf-8"
        for f in files:
            rel = f.relative_to(project).as_posix()
            try:
                text = f.read_bytes().decode(enc)        # strict: undecodable bytes = corrupted / re-encoded
            except UnicodeDecodeError as e:
                return f"{kind}: {rel} does not decode as {enc} ({e.reason} at byte {e.start})"
            except LookupError:
                return f"{kind}: unknown encoding {enc!r}"
            if spec.get("contains") and spec["contains"] not in text:
                return f"{kind}: {rel} ({enc}) lacks {spec['contains']!r}"
            if spec.get("not_contains") and spec["not_contains"] in text:
                return f"{kind}: {rel} still contains {spec['not_contains']!r}"
        return None
    if kind in ("pom_parent", "pom_dependency_absent", "pom_dependency_present", "pom_dependency_unversioned",
                "pom_dependency_versioned", "pom_packaging", "pom_module_list"):
        for f in files:
            rel = f.relative_to(project).as_posix()
            try:
                root = _pom_root(f)
            except Exception as e:  # noqa: BLE001 — any parse problem is a mismatch
                return f"{kind}: {rel} unparsable: {e}"
            if kind == "pom_parent":
                parent = root.find(NS + "parent")
                if parent is None:
                    parent = root.find("parent")
                got = (_pom_text(parent, "groupId"), _pom_text(parent, "artifactId"), _pom_text(parent, "version"))
                want = (spec.get("group_id"), spec.get("artifact_id"), spec.get("version"))
                if parent is None or any(w is not None and w != g for w, g in zip(want, got)):
                    return f"pom_parent: {rel} parent is {':'.join(x or '?' for x in got) if parent is not None else 'none'}" \
                           f", expected {':'.join(x or '*' for x in want)}"
            elif kind == "pom_packaging":
                got = _pom_text(root, "packaging") or "jar"            # Maven default
                if got != spec.get("packaging"):
                    return f"pom_packaging: {rel} packaging is {got}, expected {spec.get('packaging')}"
            elif kind == "pom_module_list":
                got = _pom_modules(root)
                want = [str(m) for m in spec.get("modules") or []]
                same = sorted(got) == sorted(want) if spec.get("any_order") else got == want
                if not same:
                    return f"pom_module_list: {rel} modules are {got}, expected {want}" \
                           f"{' (any order)' if spec.get('any_order') else ''}"
            elif kind in ("pom_dependency_unversioned", "pom_dependency_versioned"):
                versions = _pom_dependency_versions(root)     # every declaration counts (dependencyManagement too)
                for ga in spec.get("artifacts") or []:
                    if ga not in versions:
                        return f"{kind}: {rel} lacks {ga}"
                    pinned = [v for v in versions[ga] if v is not None]
                    if kind == "pom_dependency_unversioned" and pinned:
                        return f"pom_dependency_unversioned: {rel} declares {ga} with <version>{pinned[0]}</version>"
                    if kind == "pom_dependency_versioned" and len(pinned) < len(versions[ga]):
                        return f"pom_dependency_versioned: {rel} declares {ga} without a <version>"
            else:
                deps = _pom_dependencies(root)
                for ga in spec.get("artifacts") or []:
                    if kind == "pom_dependency_absent" and ga in deps:
                        return f"pom_dependency_absent: {rel} declares {ga}"
                    if kind == "pom_dependency_present" and ga not in deps:
                        return f"pom_dependency_present: {rel} lacks {ga}"
        return None
    return f"unknown post_check kind {kind!r}"


def report_properties(report: dict) -> dict:
    """report.properties (profile inventory + placeholders written), else the profile's inventory, else {}."""
    inv = report.get("properties")
    if not isinstance(inv, dict) or not inv:
        inv = _get(report, "profile", "properties", default={})
    return inv if isinstance(inv, dict) else {}


def evaluate_properties(want: dict, report: dict, project: Path | None) -> list[str]:
    """`expect.properties` (round 8 externalized-config inventory + placeholders) → mismatches."""
    mm: list[str] = []
    inv = report_properties(report)
    if not inv:
        mm.append("no properties inventory in report.json")
    findings = [f for f in _list(inv.get("findings")) if isinstance(f, dict)]
    external = [str(f.get("key")) for f in findings if f.get("status") == "external"]
    if want.get("external_min") is not None and len(external) < want["external_min"]:
        mm.append(f"properties external={len(external)} < min {want['external_min']} (got: {_short(external, 8)})")
    for key in _list(want.get("external")):
        if key not in external:
            mm.append(f"properties: {key!r} is not an external finding (external: {_short(external, 8)})")
    for key in _list(want.get("absent")):
        if key in external:
            mm.append(f"properties: {key!r} must not be external")
    by_service = want.get("external_in") if isinstance(want.get("external_in"), dict) else {}
    for svc, keys in by_service.items():         # MSA (round 9): finding.service = the deployable that reads the key
        got = [str(f.get("key")) for f in findings if f.get("status") == "external" and f.get("service") == svc]
        for key in _list(keys):
            if key not in got:
                mm.append(f"properties: {key!r} is not an external finding of service {svc!r} (got: {_short(got, 8)})")
    written = _strings(inv.get("placeholders_written"))
    for key in _list(want.get("written")):
        if key not in written:
            mm.append(f"properties: {key!r} not in placeholders_written (got: {_short(written, 8)})")
    contains = _list(want.get("placeholder_file_contains"))
    if contains:
        files = _strings(inv.get("placeholder_files"))
        if not files:
            mm.append("properties: no placeholder file written")
        elif project is None or not (project / files[0]).is_file():
            mm.append(f"properties: placeholder file {files[0]} not found in the migrated project")
        else:
            text = (project / files[0]).read_text(encoding="utf-8", errors="replace")
            for needle in contains:
                if needle not in text:
                    mm.append(f"properties: {files[0]} lacks {needle!r}")
    return mm


def evaluate(res: FixtureResult, expect: dict, report: dict, journal: list[dict],
             project: Path | None, repo: Path | None = None) -> list[str]:
    """All mismatches between the expectation block and what the run produced (empty = PASS)."""
    mm: list[str] = []
    if not report:
        mm.append(f"no report.json ({res.error or 'run produced no report'})")
    if "success" in expect and report.get("success") is not expect["success"]:
        mm.append(f"success={report.get('success')} expected {expect['success']}")
    if "exit_code" in expect and res.exit_code != expect["exit_code"]:
        mm.append(f"exit_code={res.exit_code} expected {expect['exit_code']}")
    if expect.get("max_llm_calls") is not None:
        calls = _get(report, "llm_usage", "calls", default=0) or 0
        if calls > expect["max_llm_calls"]:
            mm.append(f"llm_calls={calls} > max {expect['max_llm_calls']}")
    if expect.get("max_duration_s") is not None and res.duration_s > expect["max_duration_s"]:
        mm.append(f"duration {res.duration_s:.0f}s > max {expect['max_duration_s']}s")
    if "blocked" in expect:
        actual = blocked_items(report)
        want = [b for b in _list(expect["blocked"]) if isinstance(b, dict)]
        for b in want:
            if not any(_blocked_matches(b, a) for a in actual):
                got = ", ".join(res.blocked) or "none"
                mm.append(f"blocked {b.get('reason')} {b.get('artifact') or ''}".rstrip() + f" missing (got: {got})")
        for a in actual:
            if not any(_blocked_matches(b, a) for b in want):
                mm.append(f"unexpected blocked {a.get('reason')} {_ga(a.get('artifact')) or a.get('owner_class') or ''}"
                          .rstrip())
    flags = [f for f in _list(report.get("flags")) if isinstance(f, str)]
    for pat in _list(expect.get("flags")):
        if not _pattern_match(pat, flags):
            mm.append(f"flag {pat!r} missing (got: {', '.join(flags) or 'none'})")
    for pat in _list(expect.get("forbidden_flags")):
        if _pattern_match(pat, flags):
            mm.append(f"forbidden flag {pat!r} present")
    if expect.get("optional_bumped"):
        bumps = optional_bumps(report, journal)
        for pat in expect["optional_bumped"]:
            if not _pattern_match(_norm_bump(pat) if not pat.startswith("regex:") else pat, bumps):
                mm.append(f"optional_bumped {pat!r} missing (got: {', '.join(bumps) or 'none'})")
    if expect.get("unresolved_max") is not None:
        n = len(_list(report.get("unresolved")))
        if n > expect["unresolved_max"]:
            mm.append(f"unresolved={n} > max {expect['unresolved_max']}")
    for want_ev in _list(expect.get("journal_events")):
        if not any(all(ev.get(k) == v for k, v in want_ev.items()) for ev in journal):
            mm.append(f"no journal event matching {json.dumps(want_ev, ensure_ascii=False)}")
    for phase, status in (expect.get("phases") or {}).items():
        got = [s.split(":", 1)[1] for s in res.phases if s.split(":", 1)[0] == phase]
        if not got or got[-1] != status:
            mm.append(f"phase {phase}={got[-1] if got else 'absent'} expected {status}")
    if any(k in expect for k in ("advisories", "advisories_exact", "advisories_absent")):
        mm += evaluate_advisories(expect, report)
    if isinstance(expect.get("handover"), dict):
        mm += evaluate_handover(expect["handover"], report)
    followups = _strings(report.get("manual_followups"))
    for pat in _list(expect.get("followups_contains")):
        if not _contains_match(pat, followups):
            mm.append(f"manual_followups lack {pat!r} (got: {_short(followups)})")
    if isinstance(expect.get("kb"), dict):
        mm += evaluate_kb(expect["kb"], report, repo)
    kb_dir = (repo or REPO) / str(expect["kb"]["dir"]) \
        if isinstance(expect.get("kb"), dict) and expect["kb"].get("dir") else None
    if isinstance(expect.get("error_entries"), dict):
        mm += evaluate_error_entries(expect["error_entries"], report, Path(res.run_dir) if res.run_dir else None,
                                     kb_dir)
    if isinstance(expect.get("error_cases"), dict):
        mm += evaluate_error_cases(expect["error_cases"], report, Path(res.run_dir) if res.run_dir else None,
                                   kb_dir)
    if isinstance(expect.get("properties"), dict):
        mm += evaluate_properties(expect["properties"], report, project)
    if isinstance(expect.get("structure_review"), dict):
        mm += evaluate_structure_review(expect["structure_review"], report)
    if isinstance(expect.get("scripts"), dict):
        sdir = expect["scripts"].get("dir") or (expect.get("kb") or {}).get("dir")
        mm += evaluate_scripts(expect["scripts"], report, (repo or REPO) / str(sdir) if sdir else None)
    for check in _list(expect.get("post_checks")):
        problem = run_post_check(check, project, FIXTURES_DIR / res.fixture)
        if problem:
            mm.append(problem)
    return mm


def describe_expect(fixture: str, expected: dict) -> str:
    exp = expected.get("expect")
    if not isinstance(exp, dict):
        return f"{fixture}: (no expect block → final build must pass)"
    parts = [f"success={exp.get('success')}", f"exit={exp.get('exit_code')}"]
    if exp.get("max_llm_calls") is not None:
        parts.append(f"llm<={exp['max_llm_calls']}")
    if exp.get("max_duration_s") is not None:
        parts.append(f"duration<={exp['max_duration_s']}s")
    if "blocked" in exp:
        parts.append("blocked=[" + ", ".join(f"{b.get('reason')} {b.get('artifact', '')}".strip()
                                            for b in exp["blocked"]) + "]")
    for key in ("flags", "optional_bumped"):
        if exp.get(key):
            parts.append(f"{key}=[{', '.join(exp[key])}]")
    if exp.get("unresolved_max") is not None:
        parts.append(f"unresolved<={exp['unresolved_max']}")
    if exp.get("journal_events"):
        parts.append(f"journal={json.dumps(exp['journal_events'], ensure_ascii=False)}")
    if exp.get("advisories"):
        parts.append("advisories" + ("(exact)" if exp.get("advisories_exact") else "") + "=[" + ", ".join(
            ":".join([str(a.get("id") or a.get("advisory_id"))] + [f"{k}={v}" for k, v in a.items()
                                                                     if k not in ("id", "advisory_id")])
            for a in exp["advisories"] if isinstance(a, dict)) + "]")
    if exp.get("advisories_absent"):
        parts.append(f"advisories_absent=[{', '.join(exp['advisories_absent'])}]")
    if isinstance(exp.get("handover"), dict):
        ho = exp["handover"]
        hp = [f"{k}={ho[k]}" for k in ("changes_min", "carried_over_min", "carried_over_max", *HANDOVER_SCALARS)
              if ho.get(k) is not None]
        hp += [f"{k}={len(ho[k])}" for k in ("from_here_contains", "removed_contains", "added_contains") if ho.get(k)]
        parts.append(f"handover({', '.join(hp)})")
    if exp.get("followups_contains"):
        parts.append(f"followups=[{', '.join(exp['followups_contains'])}]")
    if isinstance(exp.get("kb"), dict):
        kb = exp["kb"]
        kp = [f"dir={kb.get('dir')}"] + [f"{k}={kb[k]}" for k in ("after", "candidates_min", "harvest_candidates_min")
                                         if kb.get(k) is not None]
        kp += [f"{r.get('recipe')}" + (f"@{r['name']}" if r.get("name") else "")
               for r in _list(kb.get("recipes")) if isinstance(r, dict)]
        parts.append(f"kb({', '.join(kp)})")
    if isinstance(exp.get("error_entries"), dict):
        ee = exp["error_entries"]
        ep = [f"{k}={ee[k]}" for k in ("min", "max", "open_max") if ee.get(k) is not None]
        ep += [f"{k}={v}" for key in ("status", "seen") for k, v in (ee.get(key) or {}).items()]
        ep += [f"{k}>={v}" for key in ("status_min", "seen_min") for k, v in (ee.get(key) or {}).items()]
        ep += [f"{k}={len(ee[k])}" for k in ("cases", "records") if ee.get(k)]
        parts.append(f"error_entries({', '.join(ep)})")
    if isinstance(exp.get("error_cases"), dict):
        ec = exp["error_cases"]
        ep = [f"min={ec['min']}"] if ec.get("min") is not None else []
        ep += [f"{k}={v}" for k, v in (ec.get("status") or {}).items()]
        ep += [f"{k}>={v}" for k, v in (ec.get("status_min") or {}).items()]
        if ec.get("records"):
            ep.append(f"records={len(ec['records'])}")
        parts.append(f"error_cases({', '.join(ep)})")
    if isinstance(exp.get("properties"), dict):
        pp = exp["properties"]
        pr = [f"external_min={pp['external_min']}"] if pp.get("external_min") is not None else []
        pr += [f"{k}={len(pp[k])}" for k in ("external", "absent", "written", "placeholder_file_contains")
               if pp.get(k)]
        parts.append(f"properties({', '.join(pr)})")
    if isinstance(exp.get("structure_review"), dict):
        sr = exp["structure_review"]
        parts.append("review(" + ", ".join(f"{k}={sr[k]}" for k in ("verdict", "critical", "critical_min",
                                                                     "script_status", "committed")
                                           if sr.get(k) is not None) + ")")
    if isinstance(exp.get("scripts"), dict):
        sc = exp["scripts"]
        sp = [f"{k}={json.dumps(sc[k], sort_keys=True)}" for k in ("attempts", "attempts_min", "attempts_max")
              if sc.get(k)]
        sp += [f"cases={len(sc['cases'])}"] if sc.get("cases") else []
        sp += [f"stored={len(sc['stored'])}"] if sc.get("stored") else []
        sp += ["reuse_without_llm"] if sc.get("reuse_without_llm") else []
        parts.append(f"scripts({', '.join(sp)})")
    if exp.get("post_checks"):
        parts.append(f"post_checks={len(exp['post_checks'])}")
    cfg = fixture_config(fixture)
    cat = ",".join(expected.get("catalog") or []) or "-"
    return f"{fixture} [{cat}]{' +' + FIXTURE_CONFIG if cfg else ''}: " + "; ".join(parts)


# ---------------------------------------------------------------------------------------------- running
def _run_dirs(out: Path) -> dict[Path, float]:
    if not out.exists():
        return {}
    return {p: p.stat().st_mtime for p in out.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}


def find_run_dir(out: Path, before: dict[Path, float], started: float, output: str = "") -> Path | None:
    """The run directory created (or touched) by this invocation."""
    after = _run_dirs(out)
    new = [p for p in after if p not in before]
    if not new:
        new = [p for p, m in after.items() if m >= started - 1]
    # prefer a dir that the CLI mentioned in its output
    for p in sorted(new, key=lambda p: after[p], reverse=True):
        if p.name in output:
            return p
    with_report = [p for p in new if (p / "report.json").exists()]
    pool = with_report or new
    return max(pool, key=lambda p: after[p]) if pool else None


def kb_after_problem(expect: dict | None, ran_before: list[str] | None) -> str | None:
    """`kb.after: <fixture>` (v2 reuse scenario): <fixture> must have run earlier in this e2e run (None = fine)."""
    after = (expect or {}).get("kb", {}).get("after") if isinstance((expect or {}).get("kb"), dict) else None
    if not after or ran_before is None or after in ran_before:
        return None
    return (f"precondition: {after} must run before this fixture in the same e2e run (it continues the KB "
            f"{expect['kb'].get('dir')}; e.g. --only {after},<this fixture>)")


def run_fixture(fixture: str, args: argparse.Namespace, cmd: list[str] | None = None,
                expected: dict | None = None, ran_before: list[str] | None = None) -> FixtureResult:
    cmd = cmd or build_command(fixture, args)
    res = FixtureResult(fixture=fixture, command=cmd)
    expected = load_expected(fixture) if expected is None else expected
    expect = expected.get("expect") if isinstance(expected.get("expect"), dict) else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "_e2e-logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"{fixture}.log"
    res.log_path = str(log_path)

    problem = preflight(fixture, expected, Path(getattr(args, "local_repo", None) or DEFAULT_LOCAL_REPO)) \
        or kb_after_problem(expect, ran_before)
    if problem:
        res.error = problem
        if expect is not None:
            res.expect_ok, res.mismatches = False, [problem]
        log_path.write_text(problem + "\n", encoding="utf-8")
        return res

    kb_dir = kb_dir_to_clear(expect)
    if kb_dir is not None and kb_dir.exists():
        shutil.rmtree(kb_dir)                    # the fixture's own KB: start empty so a re-run is deterministic
    before = _run_dirs(out)
    started = time.time()
    t0 = time.monotonic()
    output = ""
    try:
        cp = subprocess.run(cmd, check=False, cwd=REPO, env=build_env(args), capture_output=True, text=True,
                            timeout=args.timeout_minutes * 60)
        res.exit_code = cp.returncode
        output = (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired as e:
        res.exit_code = None
        res.error = f"timeout after {args.timeout_minutes} min"
        output = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    except OSError as e:
        res.error = f"cannot start CLI: {e}"
    res.duration_s = round(time.monotonic() - t0, 1)
    log_path.write_text(" ".join(cmd) + "\n\n" + output, encoding="utf-8")

    if res.exit_code not in (None, 0) and ("No module named sbmig.cli" in output or
                                           "No module named 'sbmig.cli'" in output):
        res.error = "sbmig.cli not available yet"
    report: dict = {}
    journal: list[dict] = []
    run_dir = find_run_dir(out, before, started, output)
    if run_dir is not None:
        res.run_dir = str(run_dir)
        journal = read_journal(run_dir)
        rp = run_dir / "report.json"
        if rp.exists():
            res.report_path = str(rp)
            try:
                loaded = json.loads(rp.read_text(encoding="utf-8"))
                report = loaded if isinstance(loaded, dict) else {}
                apply_report(res, report, journal)
            except (OSError, ValueError, AttributeError, TypeError) as e:
                res.error = (res.error + "; " if res.error else "") + f"unreadable report.json: {e}"
        elif not res.error:
            res.error = "no report.json in run dir"
    elif not res.error:
        tail = [ln for ln in output.strip().splitlines() if ln.strip()][-1:] or ["no output"]
        res.error = f"no run dir created (exit {res.exit_code}): {tail[0][:200]}"

    if expect is not None:
        project = None
        pd = _get(report, "outputs", "project_dir")
        if pd and Path(pd).is_dir():
            project = Path(pd)
        elif run_dir is not None and (run_dir / "project").is_dir():
            project = run_dir / "project"
        try:
            res.mismatches = evaluate(res, expect, report, journal, project)
        except Exception as e:  # noqa: BLE001 — never crash the whole e2e on one odd report
            res.mismatches = [f"expectation evaluation crashed: {type(e).__name__}: {e}"]
        res.expect_ok = not res.mismatches
    return res


def fixture_passed(r: FixtureResult) -> bool:
    return bool(r.expect_ok) if r.expect_ok is not None else bool(r.final_build_ok)


# ---------------------------------------------------------------------------------------------- output
def _fmt_bool(v: bool | None) -> str:
    return "-" if v is None else ("yes" if v else "NO")


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


HEADERS = ["fixture", "case", "exit", "phases ok", "final build ok", "llm calls", "unresolved", "blocked",
           "advisories", "carried", "review", "scripts", "errors", "duration s", "expect", "note"]


def _fmt_advisories(values: list[str], width: int = 60) -> str:
    """'springfox:replaced,hystrix:still-present' truncated to `width` with a '…(+n)' tail."""
    if not values:
        return "-"
    out = ""
    for i, v in enumerate(values):
        cand = (out + "," if out else "") + v
        rest = len(values) - i - 1
        if len(cand) + (len(f"…(+{rest})") if rest else 0) > width and out:
            return out + f"…(+{len(values) - i})"
        out = cand
    return out


def _note(r: FixtureResult) -> str:
    if r.mismatches:
        more = f" (+{len(r.mismatches) - 1})" if len(r.mismatches) > 1 else ""
        return r.mismatches[0] + more
    return r.error or ", ".join(r.flags)


def summary_rows(results: list[FixtureResult]) -> list[list[str]]:
    return [[r.fixture, r.case_type, _fmt(r.exit_code), r.phases_ok, _fmt_bool(r.final_build_ok),
             _fmt(r.llm_calls), _fmt(r.unresolved), str(len(r.blocked)), _fmt_advisories(r.advisories),
             _fmt(r.carried_over), _fmt(r.review), r.scripts or "-", r.errors or "-", f"{r.duration_s:.1f}",
             "-" if r.expect_ok is None else ("PASS" if r.expect_ok else "FAIL"), _note(r)[:110]]
            for r in results]


def format_table(results: list[FixtureResult]) -> str:
    rows = summary_rows(results)
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(HEADERS)]
    line = "  ".join(h.ljust(w) for h, w in zip(HEADERS, widths))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join([line, sep, *body])


def format_markdown(results: list[FixtureResult], args: argparse.Namespace) -> str:
    rows = summary_rows(results)
    md = ["# sbmig e2e summary", "",
          f"* date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
          f"* config: `{args.config}` (+ per-fixture `{FIXTURE_CONFIG}` when present)", f"* out: `{args.out}`",
          f"* java_home: `{args.java_home}`", f"* stop_after: `{args.stop_after or '-'}`",
          f"* llm_provider: `{args.llm_provider or '(config)'}`", "",
          "| " + " | ".join(HEADERS) + " |", "|" + "---|" * len(HEADERS)]
    md += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows]
    md += ["", "## Details", ""]
    for r in results:
        md.append(f"* **{r.fixture}** phases: {', '.join(r.phases) or '-'}; run dir: `{r.run_dir or '-'}`; "
                  f"log: `{r.log_path}`")
        if r.blocked:
            md.append(f"  * blocked: {'; '.join(r.blocked)}")
        if r.optional_bumped:
            md.append(f"  * optional bumped: {', '.join(r.optional_bumped)}")
        if r.flags:
            md.append(f"  * flags: {', '.join(r.flags)}")
        if r.advisories:
            md.append(f"  * advisories: {', '.join(r.advisories)}")
        for m in r.mismatches:
            md.append(f"  * MISMATCH: {m}")
    ok = sum(1 for r in results if r.final_build_ok)
    passed = sum(1 for r in results if fixture_passed(r))
    md += ["", f"**{ok}/{len(results)} fixtures reached a successful final build.**",
           f"**{passed}/{len(results)} fixtures met their expectations.**", ""]
    return "\n".join(md)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated fixture names (default: all)")
    ap.add_argument("--stop-after", default=None, help="passed through to `sbmig run --stop-after`")
    ap.add_argument("--java-home", default=os.environ.get("JAVA21_HOME") or DEFAULT_JAVA_HOME)
    ap.add_argument("--llm-provider", default=None,
                    help="fake | openai_compatible | anthropic (sets SBMIG_LLM__PROVIDER; see --llm-provider-flag)")
    ap.add_argument("--llm-provider-flag", action="store_true",
                    help="also pass --llm-provider to the CLI (only if the CLI supports the flag)")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--llm-config", type=Path, default=None,
                    help="extra config appended LAST, e.g. config/llm-anthropic.yml to run a replayed fixture against a "
                         "real model (LLM_API_KEY from the environment); expectations on llm calls may then differ")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--timeout-minutes", type=int, default=90)
    ap.add_argument("--install-libs", action="store_true",
                    help="run fixtures/libs/install.sh (simulated in-house libraries) before the fixtures")
    ap.add_argument("--skip-libs", action="store_true", help="never install fixtures/libs (overrides --install-libs)")
    ap.add_argument("--local-repo", default=None,
                    help="Maven local repository used for preflight checks and --install-libs "
                         "(default ~/.m2/repository; sbmig's own Maven must use the same one)")
    ap.add_argument("--expect-only", action="store_true", help="print each fixture's expectations and exit")
    ap.add_argument("--extra", nargs=argparse.REMAINDER, default=[],
                    help="everything after --extra is appended to the CLI command")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.out = Path(args.out).resolve()
    available = discover_fixtures()
    selected = [s.strip() for s in args.only.split(",") if s.strip()] or available
    unknown = [s for s in selected if s not in available]
    if unknown:
        print(f"unknown fixture(s): {', '.join(unknown)} (available: {', '.join(available)})", file=sys.stderr)
        return 2
    if args.expect_only:
        for name in selected:
            print(describe_expect(name, load_expected(name)))
        return 0
    if args.install_libs and not args.skip_libs:
        rc = install_libs(args.local_repo)
        if rc != 0:
            print(f"fixtures/libs/install.sh failed (exit {rc})", file=sys.stderr)
            return 1
    if args.java_home and not Path(args.java_home, "bin", "java").exists():
        print(f"warning: no java at {args.java_home}/bin/java", file=sys.stderr)

    results: list[FixtureResult] = []
    for name in selected:
        print(f"==> {name}", flush=True)
        r = run_fixture(name, args, ran_before=[x.fixture for x in results])
        verdict = "-" if r.expect_ok is None else ("PASS" if r.expect_ok else "FAIL")
        status = _note(r) if (r.mismatches or r.error) else "ok"
        print(f"    exit={_fmt(r.exit_code)} final_build_ok={_fmt_bool(r.final_build_ok)} "
              f"{r.duration_s:.1f}s  expect={verdict}  {status}", flush=True)
        results.append(r)

    table = format_table(results)
    print()
    print(table)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.md").write_text(format_markdown(results, args), encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    print(f"\nsummary: {args.out / 'summary.md'}")
    failed = [r.fixture for r in results if not fixture_passed(r)]
    if failed:
        print(f"FAILED: {', '.join(failed)}", file=sys.stderr)
    return 0 if results and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
