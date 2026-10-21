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
                               {"file_exists": {"path"}}, {"file_absent": {"path"}}],   # files or dirs, globs ok
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
                                   "name": "com.company.kb.LegacyRequestHelperClientIpToRemoteClientIp"}]}}

`*_contains` entries: `regex:<re>` = full match (re.DOTALL) of some entry, otherwise a substring of some entry;
`removed_contains`/`added_contains` match by "g:a" (plus a version prefix when given).
`kb`: reads every recipe document of `<repo>/<dir>/recipes/{candidates,active}/*.yml` (multi-document YAML); each
wanted recipe must match a document (`name` equal when given) with a `recipeList` entry `{<recipe>: {options}}` whose
options are a superset of `options`. `kb.dir` must lie under `out/`: run_fixture deletes it before the run so a re-run
is deterministic (a fixture that exercises the KB sets its own `kb.dir`, because every KB candidate is tried in every
later plan).

Missing report fields are treated as empty (the engine features are built in parallel) — a mismatch is reported,
the script never crashes on report shape. A summary table (with an `expect` PASS/FAIL column and the first
mismatch) is printed and written to <out>/summary.md, raw data to <out>/summary.json. Exit code: 0 when every
selected fixture meets its expectations (fixtures without an `expect` block must reach a successful final build),
1 otherwise, 2 for usage errors.
"""
from __future__ import annotations

import argparse
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
                 "multi-module-conflict", "legacy-ibatis", "legacy-role-parents"]
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
    expect_ok: bool | None = None                           # None = fixture has no expect block
    mismatches: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------- fixtures
def discover_fixtures() -> list[str]:
    found = sorted(p.name for p in FIXTURES_DIR.iterdir() if p.is_dir() and (p / "pom.xml").exists()) \
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
    g, a, v = gav.split(":")[:3]
    return local_repo / Path(*g.split(".")) / a / v / f"{a}-{v}.jar"


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


def kb_dir_to_clear(expect: dict | None, repo: Path | None = None) -> Path | None:
    """The fixture's KB directory (expect.kb.dir) when it is safely inside <repo>/out/, else None."""
    if not isinstance(expect, dict) or not isinstance(expect.get("kb"), dict) or not expect["kb"].get("dir"):
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


def _pom_dependency_versions(root) -> dict[str, list[str | None]]:
    """"g:a" → the <version> text of every declaration of it (None = declared without a <version> element)."""
    out: dict[str, list[str | None]] = {}
    for dep in list(root.iter(NS + "dependency")) + list(root.iter("dependency")):
        g, a = _pom_text(dep, "groupId"), _pom_text(dep, "artifactId")
        if g and a:
            has_version = dep.find(NS + "version") is not None or dep.find("version") is not None
            out.setdefault(f"{g}:{a}", []).append((_pom_text(dep, "version") or "") if has_version else None)
    return out


def run_post_check(check: dict, project: Path | None) -> str | None:
    """None when the check passes, else a one-line mismatch."""
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
                "pom_dependency_versioned"):
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
    if isinstance(expect.get("properties"), dict):
        mm += evaluate_properties(expect["properties"], report, project)
    for check in _list(expect.get("post_checks")):
        problem = run_post_check(check, project)
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
        kp = [f"dir={kb.get('dir')}"] + [f"{k}={kb[k]}" for k in ("candidates_min", "harvest_candidates_min")
                                         if kb.get(k) is not None]
        kp += [f"{r.get('recipe')}" + (f"@{r['name']}" if r.get("name") else "")
               for r in _list(kb.get("recipes")) if isinstance(r, dict)]
        parts.append(f"kb({', '.join(kp)})")
    if isinstance(exp.get("properties"), dict):
        pp = exp["properties"]
        pr = [f"external_min={pp['external_min']}"] if pp.get("external_min") is not None else []
        pr += [f"{k}={len(pp[k])}" for k in ("external", "absent", "written", "placeholder_file_contains")
               if pp.get(k)]
        parts.append(f"properties({', '.join(pr)})")
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


def run_fixture(fixture: str, args: argparse.Namespace, cmd: list[str] | None = None,
                expected: dict | None = None) -> FixtureResult:
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

    problem = preflight(fixture, expected, Path(getattr(args, "local_repo", None) or DEFAULT_LOCAL_REPO))
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
           "advisories", "carried", "duration s", "expect", "note"]


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
             _fmt(r.carried_over), f"{r.duration_s:.1f}",
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
        r = run_fixture(name, args)
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
