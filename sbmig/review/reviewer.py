"""StructureReviewer (implements `StructureReviewerLike`, v2 docs/v2/PLAN-v2.md §3.3, v2.1 docs/v2/PLAN-v2.1.md §2).

After the restructure and before the first build, the LLM checks the poms, dependencies and module layout against
the company rules (`cfg.artifacts` / `cfg.layout`) — and fixes what is wrong itself ("검증하고, 문제가 있으면 직접 고쳐라").
The answer is the v2.1 four-part answer next to the `<review>` JSON: `<analysis>` 원인, `<solution>` 해결 방법,
`<edits>` (JSON list of `Edit`) and an optional `<script>` (stored by the loop for reuse, `StructureReview.answer`).

For critical issues the reviewer APPLIES the edits through `sbmig.script.apply_edits` (same helper API / guard as a
fix script); a v2 answer with a script and no edits runs the script through the `ScriptRunnerLike` as before. On
success the changes stay uncommitted (the orchestrator commits them as `review: …` and fills
`StructureReview.commit`); on any failure the working tree is restored. Guards: edits / changes that touch sources are
refused (only build files may change), every changed pom must still parse. Warnings are only recorded (report section
"Structure review", Handover; the loop records the review in the error list — the reviewer no longer writes the KB).

LLM calls: the review; once more when the answer is unreadable (no review JSON, or edits JSON that cannot be read
next to a critical issue); once more when the edits could not be applied (the error + "restored" go back to the LLM,
the corrected edits are applied once more) — at most 3. Never raises: no LLM / `review.enabled` false → verdict
`skipped`; transport error, unreadable answer or anything unexpected → verdict `failed` with a note. The build goes
ahead in every case. Every log line is tagged `[review]` (`sbmig.core.stage`).
"""
from __future__ import annotations

import logging
import re
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.core.models import (CaseType, LLMUsage, ProjectProfile, ReviewIssue, ScriptProposal, ScriptRunResult,
                               StructureReview)
from sbmig.core.stage import stage

from .inputs import ReviewInput, collect_review_input, java_charset
from .parse import ParsedReview, changes_note, parse_review_answer

log = logging.getLogger("sbmig.review")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ROLE = "structure_review"
STEP_ID = "restructure.review"
RETRY_MESSAGE = ("Your answer could not be read. Answer again with exactly one <review>{…}</review> block holding the "
                 "JSON object described in the system prompt (verdict, issues, analysis) and — only for critical "
                 "issues you can fix in the build files — <analysis>, <solution>, <edits> (a JSON list, every edit "
                 "with its \"desc\") and optionally <script> with <changes>. No other text.")
EDITS_RETRY_MESSAGE = ("Your <edits> could not be read: {problem}. Answer again with the COMPLETE answer: the <review> "
                       "block, <analysis>, <solution> and <edits> as ONE valid JSON list (double quotes, `\\n` and "
                       "`\\\"` escaped inside strings, no comments, no trailing commas), optionally <script>.")
APPLY_RETRY_MESSAGE = ("Your edits could not be applied: {error}. The project was restored to the state before your "
                       "edits. Answer again with the COMPLETE answer (<review>, <analysis>, <solution>, corrected "
                       "<edits> each with its \"desc\", optionally <script> with <changes>): copy every `old` text "
                       "exactly from the poms shown (indentation and line breaks included), prefer `op: pom` editor "
                       "calls, change build files only.")
SOURCE_SUFFIXES = (".java", ".kt", ".groovy", ".scala", ".jsp", ".jspx", ".tag")
_ENCODING_WORDS = ("encoding", "인코딩", "ms949", "cp949", "euc-kr", "euc_kr", "charset")
SBMIG_CHECK = "(sbmig check) "

# The helper API as the script contract states it (docs/INTERFACES.md "Script contract"); used when sbmig.script
# (HELPER_API_DOC) cannot be imported.
FALLBACK_HELPER_API = """| API | |
|---|---|
| `p.errors` | list of dicts (empty for the structure review) |
| `p.files(glob="**/*")` | sorted relative posix paths (no `.git/`, `target/`, `node_modules/`) |
| `p.read(path)` / `p.exists(path)` | text decoded with the file's detected encoding |
| `p.write(path, text)` | same encoding and newline style as the original (a new file: UTF-8, `\\n`) |
| `p.replace(path, old, new, count=-1)` / `p.sub(path, pattern, repl, count=0, flags=0)` | number of replacements |
| `p.create(path, text)` / `p.delete(path)` / `p.move(src, dst)` | create refuses an existing file |
| `p.pom(path="pom.xml")` | pom editor: `add_dependency(g, a, version=None, scope=None, type=None, classifier=None)`, `remove_dependency(g, a)`, `set_dependency_version(g, a, version)`, `add_exclusion(g, a, ex_g, ex_a)`, `add_managed_dependency(g, a, version, scope=None, type=None)`, `set_property(name, value)`, `remove_property(name)`, `set_plugin_version(g, a, version)`, `text()`, `save()` — mutators return True when the pom changed; call `save()` once at the end |
| `p.log(msg)` | a log line (recorded); `print` goes to the log too |
"""


@lru_cache(maxsize=4)
def _system_template(version: str = "v1") -> str:
    path = PROMPTS_DIR / f"review_{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"review prompt {version!r} not found in {PROMPTS_DIR}")
    return path.read_text(encoding="utf-8")


def helper_api_doc() -> str:
    """The helper table of `sbmig.script.HELPER_API_DOC` when importable (from its first table line on: the fix-loop
    answer format in front of it does not apply to the review), else the contract's table."""
    try:
        from sbmig.script import HELPER_API_DOC          # optional: the runner package is written in parallel
        if isinstance(HELPER_API_DOC, str) and HELPER_API_DOC.strip():
            lines = HELPER_API_DOC.strip().splitlines()
            start = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("|")), 0)
            return "\n".join(lines[start:]).strip() + "\n"
    except Exception:  # noqa: BLE001 - the runner package may be missing or half-built
        log.debug("sbmig.script.HELPER_API_DOC unavailable: using the built-in helper table")
    return FALLBACK_HELPER_API


def system_prompt(cfg: Config, version: str = "v1") -> str:
    extra = [m for m in (getattr(cfg.script, "extra_allowed_imports", None) or []) if m]
    from sbmig.script.notes import describe_language    # report.language → the language of the change notes

    return (_system_template(version)
            .replace("{{ extra_imports }}", "".join(f", {m}" for m in extra))
            .replace("{{ desc_language }}", describe_language(cfg))
            .replace("{{ helper_api }}", helper_api_doc().strip())).strip() + "\n"


def _unset(v: Optional[str]) -> bool:
    return not v or "${" in v or v == "default"


def _in_flight() -> Any:
    try:
        from sbmig.fix.client import llm_call_in_flight  # the loop watchdog stays silent (round 10)
        return llm_call_in_flight()
    except Exception:  # noqa: BLE001
        return nullcontext()


def _short(text: object, limit: int = 300) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _diff_stats(diff: str) -> dict[str, tuple[int, int]]:
    """{file: (+lines, -lines)} of a unified `git diff`."""
    out: dict[str, list[int]] = {}
    cur: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            cur = m.group(2) if m else None
            if cur is not None:
                out.setdefault(cur, [0, 0])
        elif cur is None or line.startswith(("+++", "---")):
            continue
        elif line.startswith("+"):
            out[cur][0] += 1
        elif line.startswith("-"):
            out[cur][1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def _file_desc(path: str, notes: list[Any], changed: list[str]) -> str:
    """The LLM's description(s) of the change to `path` ("where: change", several joined by "; "); a note with a
    short path counts when its basename names only this one of the changed files."""
    from sbmig.script.notes import same_path

    base = PurePosixPath(path).name.lower()
    out: list[str] = []
    for n in notes:
        npath = str(getattr(n, "path", "") or "")
        if not npath:
            continue
        hit = same_path(npath, path)
        if not hit and PurePosixPath(npath.replace("\\", "/")).name.lower() == base:
            hit = sum(1 for c in changed if PurePosixPath(c).name.lower() == base) == 1
        if not hit:
            continue
        where, change = str(getattr(n, "where", "") or ""), str(getattr(n, "change", "") or "")
        text = f"{where}: {change}" if where and change and where not in change else (change or where)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


class StructureReviewer:
    """`StructureReviewer(cfg, llm, runner, kb)` — `llm`: the run's (budgeted) LLMClientLike or None; `runner`: a
    ScriptRunnerLike for a v2 answer's script (None = such a script is not run); `kb`: ignored since v2.1 (the loop
    records the review in the error list). `edits_runner`: the runner `apply_edits` uses (None = its own
    `ScriptRunner`)."""

    def __init__(self, cfg: Config, llm: Any = None, runner: Any = None, kb: Any = None, *,
                 prompt_version: str = "v1", edits_runner: Any = None):
        self.cfg = cfg
        self.llm = llm
        self.runner = runner
        self.kb = kb                                   # unused (v2.1): kept for the factory signature
        self.prompt_version = prompt_version
        self.edits_runner = edits_runner

    # ------------------------------------------------------------------ StructureReviewerLike
    def review(self, ws: Any, profile: Optional[ProjectProfile], cfg: Optional[Config] = None, *, run_id: str = "",
               restructure_notes: Iterable[str] = (), restructure_results: Iterable[Any] = ()) -> StructureReview:
        with stage("review"):
            cfg = cfg or self.cfg
            run_id = run_id or str(getattr(ws, "run_id", "") or "")
            project = (profile.name if profile is not None and profile.name else
                       str(getattr(getattr(ws, "root", None), "name", "") or ""))       # like RunReport.project_name
            rv = StructureReview(id=f"{run_id}:review", run_id=run_id, project=project,
                                 case_type=profile.case_type if profile is not None else CaseType.UNKNOWN)
            try:
                self._review(rv, ws, profile, cfg, list(restructure_notes or ()), list(restructure_results or ()))
            except Exception as e:  # noqa: BLE001 - the review never stops a run
                log.warning("structure review failed: %s: %s", type(e).__name__, e, exc_info=True)
                rv.verdict = "failed"
                rv.note = _short(f"review error: {type(e).__name__}: {e}", 500)
                if rv.script_result is not None and not rv.reverted:
                    self._restore(ws, rv, "the review failed after its fix ran")
            if rv.answer is not None:
                rv.answer = rv.answer.model_copy(update={"llm_usage": rv.llm_usage, "model": rv.model})
            self._log_summary(rv)
            return rv

    # ------------------------------------------------------------------ steps
    def _review(self, rv: StructureReview, ws: Any, profile: Optional[ProjectProfile], cfg: Config,
                notes: list[str], results: list[Any]) -> None:
        if not cfg.review.enabled:
            rv.verdict, rv.note = "skipped", "review.enabled is off"
            return
        if self.llm is None:
            rv.verdict, rv.note = "skipped", "no LLM client for this run (dry run or LLM off)"
            return
        inp = collect_review_input(ws, profile, cfg, notes=notes, results=results)
        if not inp.poms:
            rv.verdict, rv.note = "skipped", "no pom.xml in the workspace"
            return
        user = inp.render()
        messages = [ChatMessage(role="system", content=system_prompt(cfg, self.prompt_version)),
                    ChatMessage(role="user", content=user)]
        modules = len({str(PurePosixPath(p.path).parent) for p in inp.poms})
        log.info("structure review: asking the LLM (%d pom(s), %d module(s), %d pom characters%s, %d module encoding "
                 "line(s), prompt %d characters)", len(inp.poms), modules, inp.pom_chars,
                 f", {sum(1 for p in inp.poms if p.cut)} cut" if any(p.cut for p in inp.poms) else "",
                 len(inp.encodings), len(user))
        parsed, error, messages = self._ask(rv, cfg, messages)
        if parsed is None:
            rv.verdict, rv.note = "failed", error
            log.warning("structure review: no usable answer — %s (the build goes ahead)", error)
            return
        self._take(rv, parsed, inp)
        log.info("structure review: %d pom, %d module; LLM 판정: %s (critical %d, warning %d)%s", len(inp.poms),
                 modules, rv.verdict, len(rv.critical), len(rv.issues) - len(rv.critical),
                 f"; edits {len(parsed.edits)}" if parsed.edits else "")
        if parsed.warnings:
            log.debug("structure review answer warnings: %s", "; ".join(parsed.warnings))
        if self._fix(rv, ws, cfg, parsed) == "retry":
            self._apply_retry(rv, ws, cfg, inp, messages)

    def _take(self, rv: StructureReview, parsed: ParsedReview, inp: ReviewInput) -> None:
        """The parsed answer into the review: issues, analysis, script, and the four-part answer."""
        rv.issues = list(parsed.issues)
        rv.analysis = parsed.analysis
        rv.script = parsed.script
        analysis = parsed.fix_analysis
        missing = changes_note(parsed)
        if missing:
            analysis = (analysis + "\n" if analysis else "") + f"[sbmig] {missing}"
        rv.answer = ScriptProposal(code=parsed.script, analysis=analysis, solution=parsed.solution,
                                   edits=list(parsed.edits), changes=list(parsed.changes),
                                   no_script_reason=parsed.no_script_reason, model=rv.model, raw_response=parsed.raw,
                                   error=parsed.edits_problem)
        self._encoding_check(rv, inp)
        rv.verdict = "issues" if rv.issues else "ok"

    def _ask(self, rv: StructureReview, cfg: Config, messages: list[ChatMessage]
             ) -> tuple[Optional[ParsedReview], str, list[ChatMessage]]:
        """(parsed, error, conversation). At most two calls: the review, and once more when the first answer was
        unreadable (no review JSON — RETRY_MESSAGE; edits that cannot be read next to a critical issue —
        EDITS_RETRY_MESSAGE)."""
        resp = self._call(rv, cfg, messages)
        if isinstance(resp, str):
            return None, resp, messages
        parsed = parse_review_answer(resp.content)
        convo = messages + [ChatMessage(role="assistant", content=resp.content[:8000] or "(empty)")]
        if parsed.ok and not (parsed.edits_problem and parsed.critical):
            return parsed, "", convo
        if parsed.ok:
            log.info("structure review: edits unreadable (%s) — asking once more", _short(parsed.edits_problem, 200))
            ask = EDITS_RETRY_MESSAGE.format(problem=_short(parsed.edits_problem, 400))
        else:
            log.info("structure review: answer unreadable (%s%s) — asking once more for the format", parsed.error,
                     ", cut at the token limit" if (resp.finish_reason or "") == "length" else "")
            ask = RETRY_MESSAGE
        first, first_error = parsed, parsed.error
        retry = convo + [ChatMessage(role="user", content=ask)]
        resp2 = self._call(rv, cfg, retry)
        if isinstance(resp2, str):
            if first.ok:                                  # the review itself was readable: keep it, without edits
                return first, "", convo
            return None, f"unreadable answer ({first_error}), then {resp2}", retry
        parsed = parse_review_answer(resp2.content)
        convo2 = retry + [ChatMessage(role="assistant", content=resp2.content[:8000] or "(empty)")]
        if parsed.ok:
            return parsed, "", convo2
        if first.ok:
            return first, "", convo
        return None, _short(f"unreadable answer twice ({parsed.error}): {resp2.content}", 400), convo2

    def _call(self, rv: StructureReview, cfg: Config, messages: list[ChatMessage]) -> LLMResponse | str:
        model = None if _unset(cfg.llm.model) else cfg.llm.model
        meta = {"run_id": rv.run_id, "project": rv.project, "phase": "restructure", "step": STEP_ID}
        try:
            with _in_flight():
                resp = self.llm.complete(messages, model=model, max_tokens=cfg.llm.max_output_tokens,
                                         temperature=cfg.llm.temperature, role=ROLE, metadata=meta)
        except Exception as e:  # noqa: BLE001 - LLMError, budget, transport: the review is skipped, not the run
            return _short(f"llm-error: {type(e).__name__}: {e}", 400)
        u = LLMUsage(model=str(getattr(resp, "model", "") or model or ""), calls=1,
                     input_tokens=int(getattr(resp, "input_tokens", 0) or 0),
                     output_tokens=int(getattr(resp, "output_tokens", 0) or 0))
        rv.llm_usage = rv.llm_usage.add(u)
        rv.model = rv.model or u.model
        if not isinstance(getattr(resp, "content", None), str):
            return "llm-error: the client returned no text"
        return resp

    def _encoding_check(self, rv: StructureReview, inp: ReviewInput) -> None:
        """Non-UTF-8 sources without a matching source encoding are critical by contract (no build error ever shows
        them). When the LLM did not name an encoding issue for such a module, sbmig adds it (no fix for it)."""
        for me in inp.encoding_mismatches:
            pom = me.pom
            named = any(
                any(w in f"{i.problem} {i.fix_hint}".lower() for w in _ENCODING_WORDS)
                and (not i.file or i.file == pom or i.file.rstrip("/") == me.module or i.file == "pom.xml")
                for i in rv.issues)
            if named:
                continue
            names = ", ".join(java_charset(e) for e in me.mismatch)
            declared = f"`{me.declared}`" if me.declared else "UTF-8 (not declared in the project poms)"
            rv.issues.append(ReviewIssue(
                severity="critical", file=pom,
                problem=f"{SBMIG_CHECK}{names} Java sources in `{me.module or '(root)'}` build with {declared}: javac "
                        "only warns and garbles the Korean string literals",
                fix_hint=f"set `project.build.sourceEncoding` to {names.split(', ')[0]} in `{pom}` (or convert the "
                         "sources to UTF-8)" + (" — the module mixes encodings, convert one side" if me.mixed else "")))

    def _fix(self, rv: StructureReview, ws: Any, cfg: Config, parsed: ParsedReview) -> str:
        """Apply the answer's fix for the critical issues: its edits, else (a v2 answer) its script. "" = done (or
        nothing to do), "retry" = the edits failed and the tree was restored (the LLM may correct them once)."""
        critical = rv.critical
        edits = list(parsed.edits)
        if not edits and not rv.script:
            if critical:
                rv.note = ("edits unreadable, no script for the critical issue(s): they are handed over"
                           f" ({_short(parsed.edits_problem, 200)})" if parsed.edits_problem else
                           "no edits or script for the critical issue(s): they are handed over")
            return ""
        what = "edits" if edits else "script"
        if not critical:
            rv.note = f"{what} not applied: the review reported no critical issue"
            return ""
        if not cfg.review.fix_critical:
            rv.note = f"{what} not applied: review.fix_critical is off"
            return ""
        if not edits and self.runner is None:
            rv.note = "script not run: no script runner in this run"
            return ""
        if self._dirty(ws):
            rv.note = (f"{what} not applied: the working tree has uncommitted changes (the restructure must be "
                       "committed before the review — restoring after a failed fix would lose them)")
            log.warning("structure review: %s", rv.note)
            return ""
        if edits:
            return self._apply(rv, ws, cfg, parsed)
        self._run_script(rv, ws)
        return ""

    def _apply(self, rv: StructureReview, ws: Any, cfg: Config, parsed: ParsedReview) -> str:
        """The review's edits through `apply_edits` (uncommitted); restored on failure → "retry"."""
        from sbmig.script.edits import apply_edits, summarize_edits

        edits = list(parsed.edits)
        sources = sorted({p for e in edits for p in (e.path, e.to) if p and p.lower().endswith(SOURCE_SUFFIXES)})
        if sources:
            rv.note = ("edits not applied: they change sources (" + ", ".join(sources[:5]) + ") — only build files "
                       "may change")
            log.warning("structure review: %s", rv.note)
            return ""
        log.info("structure review: applying %d edit(s) for %d critical issue(s): %s", len(edits), len(rv.critical),
                 _short(summarize_edits(edits), 300))
        res = apply_edits(ws, edits, cfg=cfg, runner=self.edits_runner)
        rv.script_result = res
        if res.status != "ok":
            what = res.error or "; ".join(res.problems[:3]) or "-"
            self._restore(ws, rv, f"review edits failed ({res.status}): {_short(what, 300)}")
            return "retry"
        changed = self._changed_files(ws, res.changed_files)
        if changed != sorted(res.changed_files):
            rv.script_result = res = res.model_copy(update={"changed_files": changed})
        if not changed:
            rv.note = f"review edits ({len(edits)}) changed nothing (already as asked)"
            return ""
        touched = [f for f in changed if f.lower().endswith(SOURCE_SUFFIXES)]
        if touched:
            self._restore(ws, rv, "review edits touched sources (" + ", ".join(touched[:5])
                          + ") — only build files may change: restored")
            return ""
        broken = self._unparsable_poms(ws, changed)
        if broken:
            self._restore(ws, rv, "review edits left unparsable pom(s): " + "; ".join(broken[:3]) + " — restored")
            return "retry"
        self._log_diff(ws, changed, rv.answer.solution if rv.answer is not None else "",
                       rv.answer.changes if rv.answer is not None else ())
        rv.note = (f"review edits changed {len(changed)} file(s): {', '.join(changed[:8])}"
                   + (f" (+{len(changed) - 8})" if len(changed) > 8 else "")
                   + f" ({len(edits)} edit(s)) — not committed yet")
        return ""

    def _apply_retry(self, rv: StructureReview, ws: Any, cfg: Config, inp: ReviewInput,
                     convo: list[ChatMessage]) -> None:
        """The edits failed and were restored: tell the LLM why, apply its corrected edits once (no further retry)."""
        error = rv.note
        log.info("structure review: asking the LLM once more with the failure (%s)", _short(error, 200))
        ask = convo + [ChatMessage(role="user", content=APPLY_RETRY_MESSAGE.format(error=_short(error, 600)))]
        resp = self._call(rv, cfg, ask)
        if isinstance(resp, str):
            rv.note = f"{error}; asking again failed: {resp}"
            return
        parsed = parse_review_answer(resp.content)
        if not parsed.ok or not parsed.edits:
            why = parsed.error or parsed.edits_problem or "no edits in the answer"
            rv.note = f"{error}; the corrected answer had no usable edits ({_short(why, 200)})"
            return
        first_result = rv.script_result
        self._take(rv, parsed, inp)
        rv.script_result, rv.reverted = None, False
        self._fix(rv, ws, cfg, parsed)                    # no third try
        if rv.script_result is None:                      # the corrected answer was not applied: the tree stays restored
            rv.script_result, rv.reverted = first_result, True
        rv.note = f"{rv.note} (second try; first: {_short(error, 200)})"

    def _run_script(self, rv: StructureReview, ws: Any) -> None:
        """A v2 answer: the review's script (no edits) through the ScriptRunnerLike, as before v2.1."""
        log.info("structure review: running the review script for %d critical issue(s)", len(rv.critical))
        try:
            res = self.runner.run(ws, rv.script, errors=[])
        except Exception as e:  # noqa: BLE001 - ScriptRunnerLike.run never raises; defensive
            res = ScriptRunResult(status="error", error=f"{type(e).__name__}: {e}")
        rv.script_result = res
        if res.status != "ok":
            what = res.error or "; ".join(res.problems[:3]) or "-"
            self._restore(ws, rv, f"review script failed ({res.status}): {_short(what, 300)}")
            return
        changed = self._changed_files(ws, res.changed_files)
        if changed != sorted(res.changed_files):          # git saw more than the script's own account
            rv.script_result = res = res.model_copy(update={"changed_files": changed})
        if not changed:
            rv.note = "review script ran but changed nothing"
            return
        sources = [f for f in changed if f.lower().endswith(SOURCE_SUFFIXES)]
        if sources:
            self._restore(ws, rv, "review script touched sources (" + ", ".join(sources[:5])
                          + ") — only build files may change: restored")
            return
        broken = self._unparsable_poms(ws, changed)
        if broken:
            self._restore(ws, rv, "review script left unparsable pom(s): " + "; ".join(broken[:3]) + " — restored")
            return
        self._log_diff(ws, changed, "", rv.answer.changes if rv.answer is not None else ())
        rv.note = (f"review script changed {len(changed)} file(s): {', '.join(changed[:8])}"
                   + (f" (+{len(changed) - 8})" if len(changed) > 8 else "") + " — not committed yet")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _dirty(ws: Any) -> bool:
        try:
            return bool(ws.is_dirty())
        except Exception:  # noqa: BLE001 - no git repository (tests, plain directories)
            return False

    @staticmethod
    def _changed_files(ws: Any, reported: Iterable[str]) -> list[str]:
        out = {str(f).replace("\\", "/") for f in (reported or []) if f}
        try:
            out |= set(ws.changed_files_since(ws.head()))
        except Exception as e:  # noqa: BLE001 - no git (tests) → the fix's own account
            log.debug("git status for the review fix unavailable: %s", e)
        return sorted(out)

    @staticmethod
    def _log_diff(ws: Any, changed: list[str], solution: str, notes: Iterable[Any] = ()) -> None:
        """`수정: <file> (+a −b) — <the LLM's description of that file's change, else the solution> … diff N lines`
        per changed file, the diff itself at DEBUG."""
        try:
            diff = ws.diff(ws.head())
        except Exception as e:  # noqa: BLE001 - no git: the file list is all there is
            log.debug("diff of the review fix unavailable: %s", e)
            diff = ""
        stats = _diff_stats(diff)
        lines = len([ln for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))])
        notes = list(notes or ())
        for f in changed:
            add, rem = stats.get(f, (0, 0))
            desc = _file_desc(f, notes, changed)
            what = _short(desc, 240) if desc else (_short(solution, 160) if solution else "")
            log.info("structure review 수정: %s%s%s", f, f" (+{add} −{rem})" if f in stats else " (new / deleted)",
                     (f" — {what}" if what else "") + (f" … diff {lines} lines" if lines else ""))
        if diff:
            log.debug("structure review diff:\n%s", diff[:6000])

    @staticmethod
    def _unparsable_poms(ws: Any, changed: list[str]) -> list[str]:
        from sbmig.analyze.pom_parser import parse_pom_text

        out: list[str] = []
        for f in changed:
            if PurePosixPath(f).name != "pom.xml":
                continue
            try:
                if not ws.exists(f):
                    continue
                parse_pom_text(ws.read(f), f)
            except Exception as e:  # noqa: BLE001
                out.append(f"{f}: {_short(e, 160)}")
        return out

    @staticmethod
    def _restore(ws: Any, rv: StructureReview, note: str) -> None:
        try:
            ws.discard_uncommitted()
            rv.reverted = True
        except Exception as e:  # noqa: BLE001
            note += f" (restoring the working tree failed: {type(e).__name__}: {e})"
            log.warning("structure review: could not restore the working tree: %s", e)
        rv.note = note
        log.warning("structure review: %s", note)

    @staticmethod
    def _log_summary(rv: StructureReview) -> None:
        crit, warn = len(rv.critical), len(rv.issues) - len(rv.critical)
        edits = len(rv.answer.edits) if rv.answer is not None else 0
        kind = "edits" if edits else "script"
        if rv.script_result is not None:
            r = rv.script_result
            fix = f"{kind} {r.status}" + (", restored" if rv.reverted else f", {len(r.changed_files)} file(s) changed")
        elif edits or rv.script:
            fix = f"{kind} not applied"
        else:
            fix = "no fix"
        log.info("structure review: verdict=%s, %d critical, %d warning(s), %s, %d LLM call(s)%s", rv.verdict, crit,
                 warn, fix, rv.llm_usage.calls, f" — {rv.note}" if rv.note else "")
        for i in rv.critical:
            log.info("structure review critical: %s: %s", i.file or "(project)", _short(i.problem, 300))
        for i in rv.issues:
            if i.severity != "critical":
                log.debug("structure review warning: %s: %s", i.file or "(project)", _short(i.problem, 300))
