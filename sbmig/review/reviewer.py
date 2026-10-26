"""StructureReviewer (implements `StructureReviewerLike`, v2 docs/v2/PLAN-v2.md §3.3).

After the restructure and before the first build, the LLM checks the poms, dependencies and module layout against
the company rules (`cfg.artifacts` / `cfg.layout`). Critical issues the LLM can fix in the build files come with ONE
fix script (the v2 script contract, `def fix(p)`), run through the `ScriptRunnerLike`: on success the changes stay
uncommitted (the orchestrator commits them as `review: …` and fills `StructureReview.commit`); on failure the
working tree is restored. Warnings are only recorded (report section "Structure review", Handover, `<kb>/reviews.jsonl`).

Never raises: no LLM / `review.enabled` false → verdict `skipped`; transport error, unreadable answer (after one
"answer in the format" retry: at most 2 LLM calls) or anything unexpected → verdict `failed` with a note. The build
goes ahead in every case.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.core.models import CaseType, LLMUsage, ProjectProfile, ReviewIssue, ScriptRunResult, StructureReview

from .inputs import ReviewInput, collect_review_input, java_charset
from .parse import ParsedReview, parse_review_answer

log = logging.getLogger("sbmig.review")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ROLE = "structure_review"
STEP_ID = "restructure.review"
RETRY_MESSAGE = ("Your answer could not be read. Answer again with exactly one <review>{…}</review> block holding the "
                 "JSON object described in the system prompt (verdict, issues, analysis) and — only for critical "
                 "issues you can fix in the build files — one <script> block. No other text.")
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
    return (_system_template(version)
            .replace("{{ extra_imports }}", "".join(f", {m}" for m in extra))
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


class StructureReviewer:
    """`StructureReviewer(cfg, llm, runner, kb)` — `llm`: the run's (budgeted) LLMClientLike or None; `runner`: a
    ScriptRunnerLike or None (no script run); `kb`: a ScriptKnowledgeLike (`record_review`) or None."""

    def __init__(self, cfg: Config, llm: Any = None, runner: Any = None, kb: Any = None, *,
                 prompt_version: str = "v1"):
        self.cfg = cfg
        self.llm = llm
        self.runner = runner
        self.kb = kb
        self.prompt_version = prompt_version

    # ------------------------------------------------------------------ StructureReviewerLike
    def review(self, ws: Any, profile: Optional[ProjectProfile], cfg: Optional[Config] = None, *, run_id: str = "",
               restructure_notes: Iterable[str] = (), restructure_results: Iterable[Any] = ()) -> StructureReview:
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
                self._restore(ws, rv, "the review failed after its script ran")
        self._record(rv, cfg)
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
        log.info("structure review: asking the LLM (%d pom(s), %d pom characters%s, %d module encoding line(s), "
                 "prompt %d characters)", len(inp.poms), inp.pom_chars,
                 f", {sum(1 for p in inp.poms if p.cut)} cut" if any(p.cut for p in inp.poms) else "",
                 len(inp.encodings), len(user))
        parsed, error = self._ask(rv, cfg, messages)
        if parsed is None:
            rv.verdict, rv.note = "failed", error
            log.warning("structure review: no usable answer — %s (the build goes ahead)", error)
            return
        rv.issues = list(parsed.issues)
        rv.analysis = parsed.analysis
        rv.script = parsed.script
        self._encoding_check(rv, inp)
        rv.verdict = "issues" if rv.issues else "ok"
        if parsed.warnings:
            log.debug("structure review answer warnings: %s", "; ".join(parsed.warnings))
        self._maybe_run_script(rv, ws, cfg)

    def _ask(self, rv: StructureReview, cfg: Config, messages: list[ChatMessage]
             ) -> tuple[Optional[ParsedReview], str]:
        """At most two calls: the review, and once more with RETRY_MESSAGE when the first answer was unreadable."""
        resp = self._call(rv, cfg, messages)
        if isinstance(resp, str):
            return None, resp
        parsed = parse_review_answer(resp.content)
        if parsed.ok:
            return parsed, ""
        first_error = parsed.error
        log.info("structure review: answer unreadable (%s%s) — asking once more for the format", first_error,
                 ", cut at the token limit" if (resp.finish_reason or "") == "length" else "")
        retry = messages + [ChatMessage(role="assistant", content=resp.content[:8000] or "(empty)"),
                            ChatMessage(role="user", content=RETRY_MESSAGE)]
        resp2 = self._call(rv, cfg, retry)
        if isinstance(resp2, str):
            return None, f"unreadable answer ({first_error}), then {resp2}"
        parsed = parse_review_answer(resp2.content)
        if parsed.ok:
            return parsed, ""
        return None, _short(f"unreadable answer twice ({parsed.error}): {resp2.content}", 400)

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
        them). When the LLM did not name an encoding issue for such a module, sbmig adds it (no script for it)."""
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

    def _maybe_run_script(self, rv: StructureReview, ws: Any, cfg: Config) -> None:
        critical = rv.critical
        if not rv.script:
            if critical:
                rv.note = "no script for the critical issue(s): they are handed over"
            return
        if not critical:
            rv.note = "script not run: the review reported no critical issue"
            return
        if not cfg.review.fix_critical:
            rv.note = "script not run: review.fix_critical is off"
            return
        if self.runner is None:
            rv.note = "script not run: no script runner in this run"
            return
        if self._dirty(ws):
            rv.note = ("script not run: the working tree has uncommitted changes (the restructure must be committed "
                       "before the review — restoring after a failed script would lose them)")
            log.warning("structure review: %s", rv.note)
            return
        log.info("structure review: running the review script for %d critical issue(s)", len(critical))
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
        except Exception as e:  # noqa: BLE001 - no git (tests) → the script's own account
            log.debug("git status for the review script unavailable: %s", e)
        return sorted(out)

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

    def _record(self, rv: StructureReview, cfg: Config) -> None:
        if self.kb is None or not cfg.review.record or rv.verdict == "skipped":
            return
        try:
            self.kb.record_review(rv)
        except Exception as e:  # noqa: BLE001 - the KB never stops a run
            log.warning("structure review not recorded in the KB: %s: %s", type(e).__name__, e)

    @staticmethod
    def _log_summary(rv: StructureReview) -> None:
        crit, warn = len(rv.critical), len(rv.issues) - len(rv.critical)
        if rv.script_result is not None:
            r = rv.script_result
            script = f"script {r.status}" + (", restored" if rv.reverted else
                                             f", {len(r.changed_files)} file(s) changed")
        elif rv.script:
            script = "script not run"
        else:
            script = "no script"
        log.info("structure review: verdict=%s, %d critical, %d warning(s), %s, %d LLM call(s)%s", rv.verdict, crit,
                 warn, script, rv.llm_usage.calls, f" — {rv.note}" if rv.note else "")
        for i in rv.critical:
            log.info("structure review critical: %s: %s", i.file or "(project)", _short(i.problem, 300))
        for i in rv.issues:
            if i.severity != "critical":
                log.debug("structure review warning: %s: %s", i.file or "(project)", _short(i.problem, 300))
