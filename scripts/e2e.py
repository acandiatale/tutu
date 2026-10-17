#!/usr/bin/env python3
"""End-to-end run of `sbmig run` over the fixture projects.

    python scripts/e2e.py                         # all fixtures
    python scripts/e2e.py --only boot2,boot3 --stop-after spring
    python scripts/e2e.py --llm-provider fake --java-home /path/to/jdk21

For each fixture it runs
    python -m sbmig.cli run fixtures/projects/<name> --config config/artifacts.local-e2e.yml --out out/e2e [...]
captures the exit code + output, finds the run directory the CLI created under --out, parses its report.json
(a serialized RunReport) and prints a summary table; the same table is written to <out>/summary.md and the raw
data to <out>/summary.json. A missing/broken CLI is reported per fixture and the script carries on.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO / "fixtures" / "projects"
DEFAULT_CONFIG = REPO / "config" / "artifacts.local-e2e.yml"
DEFAULT_OUT = REPO / "out" / "e2e"
DEFAULT_JAVA_HOME = "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"
DEFAULT_ORDER = ["pure-java", "servlet-tomcat", "spring5-xml", "boot2", "boot3", "msa"]


@dataclass
class FixtureResult:
    fixture: str
    exit_code: Optional[int] = None
    case_type: str = "?"
    phases_ok: str = "-"                 # e.g. "3/4"
    phases: list[str] = field(default_factory=list)   # "jdk21:ok", "spring:FAIL", "restructure:skipped"
    final_build_ok: Optional[bool] = None
    success: Optional[bool] = None
    llm_calls: Optional[int] = None
    unresolved: Optional[int] = None
    duration_s: float = 0.0
    run_dir: Optional[str] = None
    report_path: Optional[str] = None
    flags: list[str] = field(default_factory=list)
    error: Optional[str] = None
    command: list[str] = field(default_factory=list)
    log_path: Optional[str] = None


def discover_fixtures() -> list[str]:
    found = sorted(p.name for p in FIXTURES_DIR.iterdir() if p.is_dir() and (p / "pom.xml").exists()) \
        if FIXTURES_DIR.exists() else []
    ordered = [n for n in DEFAULT_ORDER if n in found]
    return ordered + [n for n in found if n not in ordered]


def build_command(fixture: str, args: argparse.Namespace, python: str = sys.executable) -> list[str]:
    cmd = [python, "-m", "sbmig.cli", "run", str(FIXTURES_DIR / fixture),
           "--config", str(args.config), "--out", str(args.out)]
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


def _run_dirs(out: Path) -> dict[Path, float]:
    if not out.exists():
        return {}
    return {p: p.stat().st_mtime for p in out.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))}


def find_run_dir(out: Path, before: dict[Path, float], started: float, output: str = "") -> Optional[Path]:
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


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def apply_report(res: FixtureResult, report: dict) -> None:
    res.case_type = str(report.get("case_type") or _get(report, "profile", "case_type", default="?"))
    phases = report.get("phases") or []
    ran = [p for p in phases if not p.get("skipped")]
    res.phases = [f"{p.get('phase')}:{'skipped' if p.get('skipped') else ('ok' if p.get('ok') else 'FAIL')}"
                  for p in phases]
    res.phases_ok = f"{sum(1 for p in ran if p.get('ok'))}/{len(ran)}"
    fb = report.get("final_build")
    res.final_build_ok = bool(fb.get("ok")) if isinstance(fb, dict) else None
    res.success = report.get("success")
    res.llm_calls = _get(report, "llm_usage", "calls", default=None)
    res.unresolved = len(report.get("unresolved") or [])
    res.flags = list(report.get("flags") or [])


def run_fixture(fixture: str, args: argparse.Namespace, cmd: Optional[list[str]] = None) -> FixtureResult:
    cmd = cmd or build_command(fixture, args)
    res = FixtureResult(fixture=fixture, command=cmd)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "_e2e-logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"{fixture}.log"
    res.log_path = str(log_path)
    before = _run_dirs(out)
    started = time.time()
    t0 = time.monotonic()
    output = ""
    try:
        cp = subprocess.run(cmd, cwd=REPO, env=build_env(args), capture_output=True, text=True,
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
    run_dir = find_run_dir(out, before, started, output)
    if run_dir is not None:
        res.run_dir = str(run_dir)
        rp = run_dir / "report.json"
        if rp.exists():
            res.report_path = str(rp)
            try:
                apply_report(res, json.loads(rp.read_text(encoding="utf-8")))
            except (OSError, ValueError, AttributeError, TypeError) as e:
                res.error = (res.error + "; " if res.error else "") + f"unreadable report.json: {e}"
        elif not res.error:
            res.error = "no report.json in run dir"
    elif not res.error:
        tail = [ln for ln in output.strip().splitlines() if ln.strip()][-1:] or ["no output"]
        res.error = f"no run dir created (exit {res.exit_code}): {tail[0][:200]}"
    return res


def _fmt_bool(v: Optional[bool]) -> str:
    return "-" if v is None else ("yes" if v else "NO")


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


HEADERS = ["fixture", "case", "exit", "phases ok", "final build ok", "llm calls", "unresolved", "duration s", "note"]


def summary_rows(results: list[FixtureResult]) -> list[list[str]]:
    return [[r.fixture, r.case_type, _fmt(r.exit_code), r.phases_ok, _fmt_bool(r.final_build_ok),
             _fmt(r.llm_calls), _fmt(r.unresolved), f"{r.duration_s:.1f}", (r.error or ", ".join(r.flags))[:80]]
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
          f"* config: `{args.config}`", f"* out: `{args.out}`",
          f"* java_home: `{args.java_home}`", f"* stop_after: `{args.stop_after or '-'}`",
          f"* llm_provider: `{args.llm_provider or '(config)'}`", "",
          "| " + " | ".join(HEADERS) + " |", "|" + "---|" * len(HEADERS)]
    md += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows]
    md += ["", "## Details", ""]
    for r in results:
        md.append(f"* **{r.fixture}** phases: {', '.join(r.phases) or '-'}; run dir: `{r.run_dir or '-'}`; "
                  f"log: `{r.log_path}`")
    ok = sum(1 for r in results if r.final_build_ok)
    md += ["", f"**{ok}/{len(results)} fixtures reached a successful final build.**", ""]
    return "\n".join(md)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated fixture names (default: all)")
    ap.add_argument("--stop-after", default=None, help="passed through to `sbmig run --stop-after`")
    ap.add_argument("--java-home", default=os.environ.get("JAVA21_HOME") or DEFAULT_JAVA_HOME)
    ap.add_argument("--llm-provider", default=None,
                    help="fake | openai_compatible | anthropic (sets SBMIG_LLM__PROVIDER; see --llm-provider-flag)")
    ap.add_argument("--llm-provider-flag", action="store_true",
                    help="also pass --llm-provider to the CLI (only if the CLI supports the flag)")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--timeout-minutes", type=int, default=90)
    ap.add_argument("--extra", nargs=argparse.REMAINDER, default=[],
                    help="everything after --extra is appended to the CLI command")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    args.out = Path(args.out).resolve()
    available = discover_fixtures()
    selected = [s.strip() for s in args.only.split(",") if s.strip()] or available
    unknown = [s for s in selected if s not in available]
    if unknown:
        print(f"unknown fixture(s): {', '.join(unknown)} (available: {', '.join(available)})", file=sys.stderr)
        return 2
    if args.java_home and not Path(args.java_home, "bin", "java").exists():
        print(f"warning: no java at {args.java_home}/bin/java", file=sys.stderr)

    results: list[FixtureResult] = []
    for name in selected:
        print(f"==> {name}", flush=True)
        r = run_fixture(name, args)
        status = "ok" if r.final_build_ok else ("error: " + r.error if r.error else "not ok")
        print(f"    exit={_fmt(r.exit_code)} final_build_ok={_fmt_bool(r.final_build_ok)} "
              f"{r.duration_s:.1f}s  {status}", flush=True)
        results.append(r)

    table = format_table(results)
    print()
    print(table)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.md").write_text(format_markdown(results, args), encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"\nsummary: {args.out / 'summary.md'}")
    return 0 if results and all(r.final_build_ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
