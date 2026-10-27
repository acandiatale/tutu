"""LLMFixer: the default fixer seam (`propose_script`, ScriptFixerLike): context packing → LLM → answer parsing →
static pre-check / one feedback round → FixAnswer (= ScriptProposal).

`propose_script` asks for ONE case (L2) across files: model choice, call tags, usage accounting, prompt hash, record
dir and <need_files>/<need_grep> context rounds. v2.1 (docs/v2/PLAN-v2.1.md §3.6): the answer has four parts —
`<analysis>` 원인, `<solution>` 해결 방법, `<edits>` 수정 (JSON list of `Edit`, applied by the loop through
`sbmig.script.apply_edits`), `<script>` 스크립트 (stored for the same case in other projects) or `<no_script>` with the
reason. Mode `new` asks for both edits and a script; `augment` for a better script, or no_script + edits. The answer
is parsed by `sbmig.fix.script_protocol`, the script statically pre-checked (`sbmig.script.check_script`); an
unusable script or unreadable edits get ONE feedback round. The fixer never edits the tree."""
from __future__ import annotations

import inspect
import json
import logging
import re
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

import jinja2

from sbmig.core.config import Config
from sbmig.core.interfaces import ChatMessage, FixContext, LLMClientLike, LLMResponse
from sbmig.core.models import ErrorCluster, LLMUsage, ProjectProfile, ScriptBase, ScriptProposal
from sbmig.core.workspace import Workspace

from .client import LLMError, RecordingClient, llm_call_in_flight, messages_hash, usage_from
from .context import ContextPacker, PackedContext, fence_for, lang_for
from .script_protocol import NO_SCRIPT, PROTOCOL_WARNING, ParsedScript, normalize_path, parse_script_response

log = logging.getLogger("sbmig.fix.fixer")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MAX_SERVED_FILES = 6
MAX_SERVED_LINES = 300
SERVED_HEAD_LINES = 200
SERVED_TAIL_LINES = 80
MAX_GREP_LINES = 40
MAX_GREP_FILES = 3000             # a <need_grep> scans at most this many project files …
MAX_GREP_FILE_BYTES = 512_000     # … skipping files bigger than this (generated sources, bundles)
GREP_BUDGET_S = 45.0              # … and stops after this long (Windows + on-access antivirus made it crawl)
# the contract's import whitelist (docs/INTERFACES.md "Script contract (S)"); sbmig.script.ALLOWED_IMPORTS wins when present
DEFAULT_SCRIPT_IMPORTS = ("re", "json", "textwrap", "fnmatch", "posixpath", "itertools", "collections", "functools",
                          "string", "math")


def _script_module_attr(name: str) -> Any:
    """An attribute of agent S's `sbmig.script` (HELPER_API_DOC, ALLOWED_IMPORTS, check_script), or None when the
    module is missing or broken — the fixer then falls back to its own copy of the contract."""
    try:
        import sbmig.script as script_mod
    except Exception:            # ImportError, or a half-written module during development
        return None
    return getattr(script_mod, name, None)


def script_allowed_imports(extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    base = _script_module_attr("ALLOWED_IMPORTS") or DEFAULT_SCRIPT_IMPORTS
    out = [str(m) for m in base]
    out += [str(m) for m in extra if str(m) not in out]
    return out


def script_helper_api_doc(extra_imports: tuple[str, ...] = (), timeout_s: Optional[int] = None) -> str:
    """The helper API block of the script prompt from agent S's `sbmig.script` (`helper_api_doc(...)`, else
    `HELPER_API_DOC`); "" when unavailable — the prompt file then uses its own copy of the table."""
    fn = _script_module_attr("helper_api_doc")
    if callable(fn):
        try:
            return str(fn(extra_imports=list(extra_imports), timeout_s=timeout_s) or "").strip()
        except Exception:
            log.debug("sbmig.script.helper_api_doc failed; trying HELPER_API_DOC", exc_info=True)
    doc = _script_module_attr("HELPER_API_DOC")
    return str(doc).strip() if doc else ""


@lru_cache(maxsize=8)
def load_script_prompts(version: str = "v1", extra_imports: tuple[str, ...] = (),
                        timeout_s: Optional[int] = None) -> tuple[str, jinja2.Template]:
    """(system prompt, user template) of a fix-script request. The helper API block comes from `sbmig.script`
    (`helper_api_doc`) when that module provides it, else from the table kept in `script_system_<version>.md` (both
    mirror the INTERFACES.md "Script contract (S)" table)."""
    sys_path = PROMPTS_DIR / f"script_system_{version}.md"
    user_path = PROMPTS_DIR / f"script_user_{version}.md.j2"
    if not sys_path.exists() or not user_path.exists():
        raise FileNotFoundError(f"script prompt version {version!r} not found in {PROMPTS_DIR}")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, autoescape=False)
    system = env.from_string(sys_path.read_text(encoding="utf-8")).render(
        helper_api=script_helper_api_doc(extra_imports, timeout_s),
        allowed_imports=", ".join(f"`{m}`" for m in script_allowed_imports(extra_imports)))
    user_t = env.from_string(user_path.read_text(encoding="utf-8"))
    return system.strip() + "\n", user_t


def default_script_checker(cfg: Config) -> Optional[Callable[[str], list[str]]]:
    """`sbmig.script.check_script` bound to the config (extra imports, max code size), or None when unavailable.
    Its findings reject a script before the loop stores / runs it (one feedback round first)."""
    check = _script_module_attr("check_script")
    if not callable(check):
        return None
    try:
        params = inspect.signature(check).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, Any] = {}
    extra = list(cfg.script.extra_allowed_imports)
    for name, value in (("extra_imports", extra), ("extra_allowed_imports", extra),
                        ("max_chars", cfg.script.max_code_chars), ("max_code_chars", cfg.script.max_code_chars),
                        ("cfg", cfg), ("config", cfg)):
        if name in params:
            kwargs[name] = value

    def _check(code: str) -> list[str]:
        res = check(code, **kwargs)
        if res is None or res is True:
            return []
        if res is False:
            return ["refused by the static check"]
        if isinstance(res, str):
            return [res] if res else []
        problems = getattr(res, "problems", res)        # a result object or a plain list
        return [str(x) for x in problems or []]

    return _check


def profile_summary(profile: ProjectProfile) -> str:
    """One-line project description for prompts (and for FixContext.profile_summary)."""
    parts = [f"case={profile.case_type.value}"]
    parts.append(f"java={profile.java_version or '?'}")
    if profile.spring_boot_version:
        parts.append(f"boot={profile.spring_boot_version}")
    if profile.spring_version:
        parts.append(f"spring={profile.spring_version}")
    if profile.packaging:
        parts.append(f"packaging={profile.packaging}")
    parts.append(f"modules={len(profile.poms)}")
    if profile.services:
        parts.append(f"services={len(profile.services)}")
    if profile.libraries:
        libs = [f"{l.name}:{l.version}" if l.version else l.name for l in profile.libraries[:20]]
        parts.append("libs=" + ",".join(libs))
    src = profile.source
    if src.has_web_xml:
        parts.append("web.xml")
    if src.jsp_count:
        parts.append(f"jsp={src.jsp_count}")
    if any(k.startswith("javax.") for k in src.javax_imports):
        parts.append("javax-imports=" + ",".join(sorted(src.javax_imports)[:8]))
    return " ".join(parts) + " | target: JDK 21, Spring Boot 4.1"


def _unset(v: Optional[str]) -> bool:
    return not v or "${" in v or v == "default"


def _same_code(a: str, b: str) -> bool:
    """Two scripts are the same code when they differ in whitespace only."""
    def norm(t: str) -> list[str]:
        return [ln.rstrip() for ln in t.replace("\r\n", "\n").strip().split("\n") if ln.strip()]
    return norm(a) == norm(b)


class LLMFixer:
    """Default fixer (ScriptFixerLike): one Python fix script per case over a chat completion.

    A fix.plugin (in-house coding agent, docs/ADAPTERS.md) implements `propose_script` itself and can reuse the same
    packed context we give our LLM: `ContextPacker(cfg).pack_script(ws, cluster, ctx, ctx.profile_summary)` →
    PackedContext (sections with the case's errors, source windows, poms, hints, previous attempts; `.render()`
    gives one markdown string)."""

    def __init__(self, cfg: Config, client: LLMClientLike, notes_dir: Optional[str | Path] = None,
                 *, packer: Optional[ContextPacker] = None,
                 script_checker: Optional[Callable[[str], list[str]]] = None):
        self.cfg = cfg
        self.client = client
        self.packer = packer or ContextPacker(cfg, notes_dir)
        # the static pre-check of a proposed script (default: sbmig.script.check_script, looked up lazily; a
        # ScriptRunnerLike's `check` fits as well)
        self.script_checker = script_checker
        self._script_prompts: Optional[tuple[str, jinja2.Template]] = None

    # ------------------------------------------------------------------ ScriptFixerLike
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal:
        """The answer for ONE case (`ctx.script_mode` new | augment, `ctx.script_base` = the version to extend) as a
        FixAnswer: `analysis` 원인, `solution` 해결 방법, `edits` (validated `Edit`s; the loop applies them with
        `sbmig.script.apply_edits`), `code` (the script; "" = none) and `no_script_reason`. Usable = `code` or
        `edits`; `error` is set only when neither is (then `code=""`, `edits=[]`). A script dropped after the
        feedback round next to valid edits → `no_script_reason` "[sbmig] the script could not be used: …"; edits
        dropped next to a valid script → an "[sbmig] edits dropped: …" line in `analysis`. Never raises; transport and
        configuration failures carry the analysis texts the loop classifies with `is_llm_transport_error` /
        `llm_400_body` (`llm-error: …`, `llm-error: response did not follow the protocol: …`)."""
        model = self._model(ctx)
        usage = LLMUsage()
        requested: list[str] = []
        transcript: list[dict] = []
        rounds = 0
        messages: list[ChatMessage] = []
        last: Optional[LLMResponse] = None
        base = ctx.script_base
        mode = "augment" if base is not None and ctx.script_mode in ("augment", None) else "new"
        if ctx.script_mode == "augment" and base is None:
            log.warning("script request for %s: augment without a script base — asking for a new script", cluster.id)
        role, meta = self.call_tags(ctx, cluster.id, ws)
        case_id = ctx.case_id or cluster.case_id or ""
        meta["script_mode"] = mode
        if case_id:
            meta["case_id"] = case_id
        if mode == "augment" and base is not None:
            meta["script_base_version"] = base.version
        adv_in_prompt = 0
        packed: Optional[PackedContext] = None
        try:
            system, user_t = self.script_prompts()
            log.info("script prompt for %s (%s%s): packing %d error(s) (%d advisory hint(s), %d KB hint(s), %d reference "
                     "file(s))", cluster.id, mode, f" of v{base.version}" if mode == "augment" and base else "",
                     len(cluster.errors), len(ctx.advisory_hints), len(ctx.kb_hints), len(ctx.reference_files))
            packed = self.packer.pack_script(ws, cluster, ctx, ctx.profile_summary)
            log.info("script prompt for %s packed: %d chars, %d file(s) shown, %d listed by name, sections [%s]%s → "
                     "calling the LLM", cluster.id, packed.chars, len(packed.files_included), len(packed.files_listed),
                     ", ".join(t[:28] for t, _ in packed.sections),
                     f", truncated {', '.join(packed.truncated)}" if packed.truncated else "")
            guidance = next((b for t, b in packed.sections if t == ContextPacker.ADVISORY_TITLE), "")
            if guidance:
                adv_in_prompt = sum(1 for ln in guidance.splitlines() if ln.startswith("- "))
            messages = self.build_script_messages(packed, mode, base, system=system, user_template=user_t)
            last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta,
                                      parse=parse_script_response)

            # 1) context rounds
            while parsed.wants_context and not parsed.usable and rounds < self.cfg.llm.max_context_rounds:
                rounds += 1
                requested.extend(p for p in parsed.need_files if p not in requested)
                remaining = self.cfg.llm.max_context_rounds - rounds
                log.info("script prompt for %s: the LLM asked for more context (round %d/%d): %d file(s) %s, %d grep(s) "
                         "%s", cluster.id, rounds, self.cfg.llm.max_context_rounds, len(parsed.need_files),
                         parsed.need_files[:4], len(parsed.need_grep), parsed.need_grep[:3])
                served = self.serve_context(ws, parsed.need_files, parsed.need_grep, remaining)
                messages = messages + [ChatMessage(role="assistant", content=last.content),
                                       ChatMessage(role="user", content=served)]
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta,
                                          parse=parse_script_response)
            if parsed.wants_context and not parsed.usable:
                requested.extend(p for p in parsed.need_files if p not in requested)

            # 2) static pre-check + one feedback round for a script that cannot be used as written / edits that
            #    cannot be read
            notes: list[str] = []
            sbase = base if mode == "augment" else None
            problems = self._script_problems(parsed, sbase)
            if self._wants_feedback(parsed, problems):
                rounds += 1
                fb = self._feedback_problems(parsed, problems)
                log.info("answer for %s cannot be used (%s) → one feedback round", cluster.id, fb[0][:160])
                messages = messages + [ChatMessage(role="assistant", content=last.content),
                                       ChatMessage(role="user", content=self.script_feedback_message(parsed, fb, mode))]
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta,
                                          parse=parse_script_response)
                problems = self._script_problems(parsed, sbase)
                if not (parsed.code and not problems) and not (parsed.edits and not parsed.edits_problem):
                    notes.append(f"feedback round did not give a usable script or edits (first answer: {fb[0][:200]})")

            code = parsed.code if not problems else ""
            edits = list(parsed.edits) if not parsed.edits_problem else []
            analysis = parsed.analysis
            no_script = parsed.no_script_reason
            error = ""
            if not code and not edits:
                error = self._answer_error(parsed, problems)
                if not parsed.candidate and not parsed.wants_context and PROTOCOL_WARNING in parsed.warnings:
                    analysis = "llm-error: response did not follow the protocol: " + parsed.raw.strip()[:300]
                elif parsed.wants_context and not parsed.candidate:
                    analysis = (analysis + "\n" if analysis else "") + \
                        f"(still requesting context after {self.cfg.llm.max_context_rounds} round(s): " + \
                        ", ".join(parsed.need_files + parsed.need_grep) + ")"
            else:
                if parsed.candidate and not code and problems:       # edits kept, the broken script dropped
                    notes.append(f"script dropped: {problems[0][:200]}")
                    no_script = no_script or f"[sbmig] the script could not be used: {problems[0][:200]}"
                if parsed.edits_problem:                             # the script kept, the broken edits dropped
                    notes.append(f"edits dropped: {parsed.edits_problem[:200]}")
            if last and last.finish_reason in ("length", "max_tokens"):
                notes.append(f"output truncated (finish_reason={last.finish_reason})")
            if notes:
                analysis = (analysis + "\n" if analysis else "") + "\n".join(f"[sbmig] {n}" for n in notes)
            proposal = ScriptProposal(
                code=code,
                analysis=analysis,
                solution=parsed.solution,
                edits=edits,
                no_script_reason=no_script if not code else "",
                model=last.model if last else (model or ""),
                prompt_hash=messages_hash(messages),
                llm_usage=usage,
                requested_files=requested,
                raw_response=last.content if last else "",
                error=error,
            )
            log.debug("answer for %s: %d edit(s), script %s, solution %d chars%s", cluster.id, len(proposal.edits),
                      "yes" if proposal.code else ("no: " + (proposal.no_script_reason or proposal.error or "-"))[:120],
                      len(proposal.solution), f", error {proposal.error[:120]}" if proposal.error else "")
        except LLMError as exc:
            proposal = self._error_script(f"llm-error: {exc}", f"LLM call failed (status={exc.status})", model,
                                          messages, usage, requested)
        except Exception as exc:  # never raise into the orchestrator
            log.exception("LLM script request failed for cluster %s", cluster.id)
            proposal = self._error_script(f"llm-error: {type(exc).__name__}: {exc}",
                                          f"LLM call failed ({type(exc).__name__})", model, messages, usage, requested)

        self._journal_script(ws, cluster, ctx, mode, proposal, rounds, adv_in_prompt, packed)
        self._record(cluster, transcript, proposal)
        return proposal

    def script_prompts(self) -> tuple[str, jinja2.Template]:
        """(system, user template) of the script requests — loaded on first use (`llm.prompt_version`, falling back
        to v1 when the version has no script prompts)."""
        if self._script_prompts is None:
            extra = tuple(self.cfg.script.extra_allowed_imports)
            timeout = self.cfg.script.timeout_seconds
            try:
                self._script_prompts = load_script_prompts(self.cfg.llm.prompt_version, extra, timeout)
            except FileNotFoundError:
                log.warning("no script prompts for llm.prompt_version=%s; using v1", self.cfg.llm.prompt_version)
                self._script_prompts = load_script_prompts("v1", extra, timeout)
        return self._script_prompts

    def build_script_messages(self, packed: PackedContext, mode: str = "new", base: Optional[ScriptBase] = None,
                              *, system: Optional[str] = None,
                              user_template: Optional[jinja2.Template] = None) -> list[ChatMessage]:
        if system is None or user_template is None:
            system, user_template = self.script_prompts()
        label = (f"version {base.version}" if base.version else "this run's script") if base is not None else ""
        # the loop rolls a base back before the next try unless it was improved / build_ok (then it stays in the tree
        # and the case's errors are ones javac showed after it)
        kept = base is not None and base.outcome in ("improved", "build_ok")
        user = user_template.render(sections=packed.sections, truncated=packed.truncated, mode=mode, base_label=label,
                                    base_kept=kept)
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user.strip() + "\n")]

    def _static_problems(self, code: str) -> list[str]:
        checker = self.script_checker if self.script_checker is not None else default_script_checker(self.cfg)
        if checker is None:
            return []
        try:
            return [str(p) for p in (checker(code) or [])]
        except Exception as exc:          # a broken checker must not block fixing; the runner checks again
            log.warning("static script check failed (%s: %s); leaving the check to the runner", type(exc).__name__, exc)
            return []

    def _script_problems(self, parsed: ParsedScript, base: Optional[ScriptBase]) -> list[str]:
        """Why the parsed answer's script is not usable ([] = usable, or no script at all next to valid edits /
        a <no_script> reason)."""
        if parsed.code:
            problems = self._static_problems(parsed.code)
            if not problems and base is not None and _same_code(parsed.code, base.code):
                label = f"version {base.version}" if base.version else "this run's script"
                problems = [(f"the script is identical to the version it should extend ({label}), which did not "
                             "fix the case — change what its result shows")]
            return problems
        if parsed.candidate:
            return [parsed.problem]
        if parsed.used_patches:
            return ["the answer used SEARCH/REPLACE <patches>; this loop takes <edits> (a JSON list) and ONE Python "
                    "fix script in <script>"]
        if parsed.edits or parsed.no_script_reason:
            return []                                     # no script, on purpose or with edits: not a script problem
        return [parsed.problem or NO_SCRIPT]

    @staticmethod
    def _feedback_problems(parsed: ParsedScript, problems: list[str]) -> list[str]:
        """What the feedback round names: the unreadable edits and / or the script's problems."""
        out = [parsed.edits_problem] if parsed.edits_problem else []
        if parsed.candidate or parsed.used_patches:
            out += problems
        return out or problems or [NO_SCRIPT]

    @staticmethod
    def _wants_feedback(parsed: ParsedScript, problems: list[str]) -> bool:
        """One feedback round when a script block is there but unusable, the answer used v1 patches, or the edits
        block cannot be read. An answer without any fix (only an analysis / a <no_script> without edits) is the
        model's decision: no feedback round."""
        return bool(parsed.edits_problem) or bool(problems and (parsed.candidate or parsed.used_patches))

    def _answer_error(self, parsed: ParsedScript, problems: list[str]) -> str:
        """The short reason of an answer with neither a usable script nor valid edits (ScriptProposal.error)."""
        err = self._script_error(parsed, problems)
        if parsed.edits_problem:
            return parsed.edits_problem if err == NO_SCRIPT and not parsed.candidate else \
                f"{err}; {parsed.edits_problem}"
        if parsed.no_script_reason and not parsed.candidate and err == NO_SCRIPT:
            return f"no script and no edits: {parsed.no_script_reason[:200]}"
        return err

    def _script_error(self, parsed: ParsedScript, problems: list[str]) -> str:
        """The short reason of a proposal without a script."""
        if parsed.code and problems:
            if problems[0].startswith("the script is identical"):
                return problems[0]
            return "script rejected by the static check: " + "; ".join(problems[:5])
        if parsed.candidate:
            return parsed.problem
        if parsed.wants_context:
            return f"still requesting context after {self.cfg.llm.max_context_rounds} round(s)"
        if parsed.used_patches:
            return NO_SCRIPT + " (the answer used the v1 <patches> format)"
        return NO_SCRIPT

    def script_feedback_message(self, parsed: ParsedScript, problems: list[str], mode: str = "new") -> str:
        parts = ["Your answer cannot be used. Problems:"]
        parts += [f"- {p}" for p in problems[:10]]
        if len(problems) > 10:
            parts.append(f"- … and {len(problems) - 10} more problem(s)")
        if parsed.used_patches and not parsed.candidate:
            parts.append("This loop does not take SEARCH/REPLACE patches: give the changes as <edits> (a JSON list of "
                         "{\"op\": \"replace\", \"path\": …, \"old\": …, \"new\": …} objects) and ONE Python fix script "
                         "that makes the same edits through the helper API `p` (p.replace / p.sub / p.pom …) for "
                         "every error in p.errors.")
        if parsed.edits_problem:
            parts.append("<edits> must be ONE valid JSON list: double-quoted keys and strings, `\\n` and `\\\"` "
                         "escaped inside strings, no comments, no trailing commas; `op` one of replace / write / "
                         "create / delete / move / pom; paths relative to the project root.")
        what = ("<analysis>, <solution>, <edits> and <script> with a module-level `def fix(p)` (or <no_script> with the "
                "reason)") if mode != "augment" else \
            ("<analysis>, <solution> and the COMPLETE new <script> with a module-level `def fix(p)` — or <no_script> "
             "with the reason and <edits>")
        parts.append(f"Re-send the COMPLETE corrected answer: {what}. Use only the helper API `p` and the allowed "
                     "imports in the script (no open / os / sys / subprocess / eval / exec / __dunder__ access).")
        return "\n\n".join(parts) + "\n"

    def _error_script(self, analysis: str, error: str, model: Optional[str], messages: list[ChatMessage],
                      usage: LLMUsage, requested: list[str]) -> ScriptProposal:
        log.warning("%s", analysis[:300])
        return ScriptProposal(code="", analysis=analysis[:1000], model=model or "",
                              prompt_hash=messages_hash(messages) if messages else "", llm_usage=usage,
                              requested_files=requested, error=error[:300])

    def _journal_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, mode: str, p: ScriptProposal,
                        rounds: int, advisory_hints: int, packed: Optional[PackedContext]) -> None:
        try:
            ws.journal({"event": "llm_script", "cluster_id": cluster.id,
                        "case_id": ctx.case_id or cluster.case_id or None, "mode": mode,
                        "base_version": ctx.script_base.version if mode == "augment" and ctx.script_base else None,
                        "model": p.model, "rounds": rounds, "calls": p.llm_usage.calls,
                        "tokens": {"input": p.llm_usage.input_tokens, "output": p.llm_usage.output_tokens},
                        "prompt_hash": p.prompt_hash, "code_chars": len(p.code), "edits": len(p.edits),
                        "solution_chars": len(p.solution), "no_script": bool(p.no_script_reason),
                        "files_shown": len(packed.files_included) if packed else 0,
                        "files_listed": len(packed.files_listed) if packed else 0,
                        "error": p.error or None,
                        "llm_error": p.analysis if p.analysis.startswith("llm-error") else None,
                        "advisory_hints": advisory_hints})
        except Exception:  # pragma: no cover
            log.debug("journal failed", exc_info=True)

    # ------------------------------------------------------------------ building blocks
    def _model(self, ctx: FixContext) -> Optional[str]:
        llm = self.cfg.llm
        if ctx.use_escalation_model and not _unset(llm.escalation_model):
            return llm.escalation_model
        return None if _unset(llm.model) else llm.model

    @staticmethod
    def call_tags(ctx: Optional[FixContext], cluster_id: Optional[str] = None,
                  ws: Optional[Workspace] = None) -> tuple[str, dict]:
        """(role, metadata) sent with every LLM call of one propose_script() — lets a gateway route/budget by purpose.
        Round 10: `run_id` and `project` (the workspace's run id / project directory name) as the LLMRequest contract
        says (`sbmig.core.interfaces.LLMRequest.metadata`); never secrets."""
        meta: dict = {}
        if ws is not None:
            run_id = getattr(ws, "run_id", None)
            root = getattr(ws, "root", None)
            if run_id:
                meta["run_id"] = str(run_id)
            if root is not None and getattr(root, "name", ""):
                meta["project"] = root.name
        if ctx is None:
            if cluster_id:
                meta["cluster_id"] = cluster_id
            return "fix", meta
        meta.update({"phase": ctx.phase.value, "iteration": ctx.iteration})
        if cluster_id:
            meta["cluster_id"] = cluster_id
        return ("fix_escalation" if ctx.use_escalation_model else "fix"), meta

    def _call(self, messages: list[ChatMessage], model: Optional[str], usage: LLMUsage,
              transcript: list[dict], *, role: str = "fix", metadata: Optional[dict] = None,
              parse: Callable[[str], Any] = parse_script_response) -> tuple[LLMResponse, Any]:
        with llm_call_in_flight():          # the loop's watchdog stays silent while the call heartbeats (round 10)
            resp = self.client.complete(messages, model=model, max_tokens=self.cfg.llm.max_output_tokens,
                                        temperature=self.cfg.llm.temperature, role=role,
                                        metadata=dict(metadata) if metadata else None)
        u = usage_from(resp)
        usage.model = usage.model or u.model
        usage.calls += 1
        usage.input_tokens += u.input_tokens
        usage.output_tokens += u.output_tokens
        parsed = parse(resp.content)
        transcript.append({"prompt_hash": messages_hash(messages), "response": resp.content,
                           "usage": u.model_dump(), "warnings": parsed.warnings})
        if parsed.warnings:
            log.debug("protocol warnings: %s", parsed.warnings)
        return resp, parsed

    # ------------------------------------------------------------------ context serving
    def resolve_path(self, ws: Workspace, want: str) -> tuple[Optional[str], list[str]]:
        """Map a requested path (maybe partial, or a FQCN) to a workspace file. Returns (path, candidates)."""
        if ws.exists(want) and ws.abs(want).is_file():
            return want.replace("\\", "/"), []
        want = want.replace("\\", "/")
        cands: list[str] = []
        suffixes = [want]
        if re.fullmatch(r"[\w$]+(\.[\w$]+)+", want) and not re.search(r"\.(java|xml|properties|ya?ml|kt|jsp)$", want):
            suffixes.append(want.replace(".", "/") + ".java")
        # posix forms: on Windows the workspace globs may come back with backslashes (round 10)
        files = [f.replace("\\", "/") for f in ws.java_files() + ws.pom_files()] + self._resource_files(ws)
        for s in suffixes:
            cands = [f for f in files if f == s or f.endswith("/" + s)]
            if cands:
                break
        if not cands:
            base = PurePosixPath(want).name
            cands = [f for f in files if PurePosixPath(f).name == base]
        if len(cands) == 1:
            return cands[0], []
        return None, cands[:8]

    @staticmethod
    def _resource_files(ws: Workspace) -> list[str]:
        out: list[str] = []
        for ext in ("properties", "yml", "yaml", "xml", "factories", "imports"):
            for f in ws.glob(f"*.{ext}"):
                f = f.replace("\\", "/")
                if f.startswith("src/") or "/src/" in f:
                    if PurePosixPath(f).name != "pom.xml":
                        out.append(f)
        return sorted(set(out))

    def serve_context(self, ws: Workspace, need_files: list[str], need_grep: list[str], remaining_rounds: int) -> str:
        parts: list[str] = ["Here is the requested context."]
        for want in need_files[:MAX_SERVED_FILES]:
            path, cands = self.resolve_path(ws, want)
            if not path:
                if cands:
                    parts.append(f"// {want}: not found exactly; candidates: {', '.join(cands)}")
                else:
                    parts.append(f"// {want}: file not found in the project")
                continue
            text = ws.read(path).replace("\r\n", "\n")
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            lang = lang_for(path)
            if len(lines) <= MAX_SERVED_LINES:
                body = "\n".join(lines)
                f = fence_for(body)
                parts.append(f"// {path} lines 1-{len(lines)} (whole file)\n{f}{lang}\n{body}\n{f}")
            else:
                head = "\n".join(lines[:SERVED_HEAD_LINES])
                tail_start = len(lines) - SERVED_TAIL_LINES + 1
                tail = "\n".join(lines[tail_start - 1:])
                f = fence_for(head + tail)
                parts.append(
                    f"// {path} lines 1-{SERVED_HEAD_LINES} (of {len(lines)})\n{f}{lang}\n{head}\n{f}\n"
                    f"// … lines {SERVED_HEAD_LINES + 1}-{tail_start - 1} omitted (use <need_grep> to locate code there)\n"
                    f"// {path} lines {tail_start}-{len(lines)}\n{f}{lang}\n{tail}\n{f}")
        if len(need_files) > MAX_SERVED_FILES:
            parts.append(f"(only the first {MAX_SERVED_FILES} files are served per round; skipped: "
                         + ", ".join(need_files[MAX_SERVED_FILES:]) + ")")
        if need_grep:
            parts.append(self.grep(ws, need_grep))
        if remaining_rounds <= 0:
            parts.append("This was the LAST context round. Answer now with <analysis>, <solution>, <edits> and "
                         "<script> (or <no_script> with the reason and <edits>; or explain in <analysis> why this "
                         "case cannot be fixed safely).")
        else:
            parts.append(f"You may ask for more context {remaining_rounds} more time(s), but prefer answering now "
                         "(<analysis>, <solution>, <edits>, <script>).")
        return "\n\n".join(parts) + "\n"

    def grep(self, ws: Workspace, patterns: list[str]) -> str:
        """`<need_grep>` from the LLM: one pass over the project's java / pom / resource files (each read once, big
        files skipped, bounded by MAX_GREP_FILES and GREP_BUDGET_S) for up to 5 patterns."""
        t0 = time.monotonic()
        files = [f.replace("\\", "/") for f in ws.java_files() + ws.pom_files()] + self._resource_files(ws)
        skipped_count = max(0, len(files) - MAX_GREP_FILES)
        files = files[:MAX_GREP_FILES]
        rxs: list[tuple[str, re.Pattern[str]]] = []
        for pat in patterns[:5]:
            try:
                rxs.append((pat, re.compile(pat)))
            except re.error:
                rxs.append((pat, re.compile(re.escape(pat))))
        log.info("grep for the LLM: %d pattern(s) over %d file(s)%s", len(rxs), len(files),
                 f" ({skipped_count} more not scanned)" if skipped_count else "")
        hits: dict[str, list[str]] = {pat: [] for pat, _ in rxs}
        counts: dict[str, int] = {pat: 0 for pat, _ in rxs}
        total = 0
        scanned = 0
        budget_hit = False
        for f in files:
            if time.monotonic() - t0 > GREP_BUDGET_S:
                budget_hit = True
                break
            try:
                if ws.abs(f).stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                text = ws.read(f)
            except OSError:
                continue
            scanned += 1
            lines: Optional[list[str]] = None
            for pat, rx in rxs:
                if not rx.search(text):
                    continue
                if lines is None:
                    lines = text.splitlines()
                for i, line in enumerate(lines, 1):
                    if rx.search(line):
                        counts[pat] += 1
                        if total < MAX_GREP_LINES:
                            hits[pat].append(f"{f}:{i}: {line.strip()[:200]}")
                            total += 1
        out: list[str] = []
        for pat, _ in rxs:
            head = f"grep /{pat}/ → {counts[pat]} matching line(s)" + \
                (f" (showing {len(hits[pat])})" if counts[pat] > len(hits[pat]) else "")
            out.append(head + ("\n" + "\n".join(hits[pat]) if hits[pat] else ""))
        if budget_hit or skipped_count:
            out.append(f"(grep stopped after {scanned} of {len(files) + skipped_count} file(s)"
                       f"{' — time budget' if budget_hit else ''}; ask for specific files instead)")
        log.info("grep for the LLM done: %d file(s) scanned in %.1fs, %d line(s) returned%s", scanned,
                 time.monotonic() - t0, total, " (budget hit)" if budget_hit else "")
        return "\n\n".join(out)

    # ------------------------------------------------------------------ bookkeeping
    def _record(self, cluster: ErrorCluster, transcript: list[dict], p: ScriptProposal,
                prefix: str = "script") -> None:
        rd = self.cfg.llm.record_dir
        if not rd or isinstance(self.client, RecordingClient):
            return
        try:
            d = self.cfg.resolve_path(rd)
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", cluster.id)[:80]
            path = d / f"{prefix}-{safe}-{(p.prompt_hash or 'none')[:12]}.json"
            path.write_text(json.dumps({"cluster_id": cluster.id, "calls": transcript,
                                        "proposal": p.model_dump(mode="json")}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError:  # pragma: no cover
            log.debug("record failed", exc_info=True)


__all__ = ["LLMFixer", "profile_summary", "load_script_prompts", "default_script_checker",
           "script_allowed_imports", "script_helper_api_doc", "normalize_path"]
