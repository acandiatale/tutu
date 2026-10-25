"""LLMFixer: context packing → LLM → protocol parsing → validation/feedback → FixProposal (implements FixerLike)."""
from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Optional

import jinja2

from sbmig.core.config import Config
from sbmig.core.interfaces import ChatMessage, FixContext, LLMClientLike, LLMResponse
from sbmig.core.models import ErrorCluster, FileChange, FixProposal, LLMUsage, ProjectProfile
from sbmig.core.workspace import Workspace

from .apply import BlockFailure, PatchApplier, best_window
from .client import LLMError, RecordingClient, llm_call_in_flight, messages_hash, usage_from
from .context import ContextPacker, PackedContext, fence_for, lang_for
from .protocol import ParsedResponse, normalize_path, parse_fix_response, render_protocol_instructions

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
FEEDBACK_PAD = 4


@lru_cache(maxsize=8)
def load_prompts(version: str = "v1") -> tuple[str, jinja2.Template]:
    sys_path = PROMPTS_DIR / f"system_{version}.md"
    user_path = PROMPTS_DIR / f"user_{version}.md.j2"
    if not sys_path.exists() or not user_path.exists():
        raise FileNotFoundError(f"prompt version {version!r} not found in {PROMPTS_DIR}")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, autoescape=False)
    system = env.from_string(sys_path.read_text(encoding="utf-8")).render(protocol=render_protocol_instructions())
    user_t = env.from_string(user_path.read_text(encoding="utf-8"))
    return system.strip() + "\n", user_t


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


class LLMFixer:
    """Default FixerLike: our SEARCH/REPLACE protocol over a chat completion.

    A fix.plugin (in-house coding agent, docs/ADAPTERS.md) can reuse the same packed context we give our LLM:
    `ContextPacker(cfg).pack(ws, cluster, ctx, ctx.profile_summary)` → PackedContext (sections with the errors,
    source files, pom excerpt, KB hints, previous attempts; `.render()` gives one markdown string)."""

    def __init__(self, cfg: Config, client: LLMClientLike, notes_dir: Optional[str | Path] = None,
                 *, packer: Optional[ContextPacker] = None):
        self.cfg = cfg
        self.client = client
        self.packer = packer or ContextPacker(cfg, notes_dir)
        self.system_prompt, self.user_template = load_prompts(cfg.llm.prompt_version)

    # ------------------------------------------------------------------ FixerLike
    def apply(self, ws: Workspace, changes: list[FileChange]) -> tuple[list[FileChange], list[str]]:
        return PatchApplier(ws).apply(changes)

    def propose(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> FixProposal:
        model = self._model(ctx)
        usage = LLMUsage()
        requested: list[str] = []
        transcript: list[dict] = []
        rounds = 0
        messages: list[ChatMessage] = []
        last: Optional[LLMResponse] = None
        role, meta = self.call_tags(ctx, cluster.id, ws)
        adv_in_prompt = 0
        try:
            log.info("fix prompt for %s: packing (%d advisory hint(s), %d KB hint(s), %d reference file(s))",
                     cluster.id, len(ctx.advisory_hints), len(ctx.kb_hints), len(ctx.reference_files))
            packed = self.packer.pack(ws, cluster, ctx, ctx.profile_summary)
            log.info("fix prompt for %s packed: %d chars, sections [%s]%s → calling the LLM", cluster.id, packed.chars,
                     ", ".join(t[:28] for t, _ in packed.sections),
                     f", truncated {', '.join(packed.truncated)}" if packed.truncated else "")
            guidance = next((b for t, b in packed.sections if t == ContextPacker.ADVISORY_TITLE), "")
            if guidance:                      # round 6: the library advisories' "what to migrate TO" in this prompt
                adv_in_prompt = sum(1 for ln in guidance.splitlines() if ln.startswith("- "))
                log.debug("advisory guidance in the prompt for %s:\n%s", cluster.id, guidance)
            messages = self.build_messages(packed)
            last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta)

            # 1) context rounds
            while parsed.wants_context and not parsed.changes and rounds < self.cfg.llm.max_context_rounds:
                rounds += 1
                requested.extend(p for p in parsed.need_files if p not in requested)
                remaining = self.cfg.llm.max_context_rounds - rounds
                log.info("fix prompt for %s: the LLM asked for more context (round %d/%d): %d file(s) %s, %d grep(s) %s",
                         cluster.id, rounds, self.cfg.llm.max_context_rounds, len(parsed.need_files),
                         parsed.need_files[:4], len(parsed.need_grep), parsed.need_grep[:3])
                served = self.serve_context(ws, parsed.need_files, parsed.need_grep, remaining)
                messages = messages + [ChatMessage(role="assistant", content=last.content),
                                       ChatMessage(role="user", content=served)]
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta)
            if parsed.wants_context and not parsed.changes:
                requested.extend(p for p in parsed.need_files if p not in requested)

            # 2) validation + one feedback round
            notes: list[str] = []
            if parsed.changes:
                failures = PatchApplier(ws).check(parsed.changes)
                if failures:
                    rounds += 1
                    log.info("fix prompt for %s: %d patch block(s) did not apply → one feedback round", cluster.id,
                             len(failures))
                    feedback = self.feedback_message(ws, failures)
                    messages = messages + [ChatMessage(role="assistant", content=last.content),
                                           ChatMessage(role="user", content=feedback)]
                    prev = parsed
                    last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta)
                    if not parsed.changes:
                        notes.append("feedback round returned no patches; keeping first proposal")
                        parsed = ParsedResponse(changes=prev.changes, analysis=prev.analysis,
                                                confidence=prev.confidence, raw=prev.raw,
                                                warnings=prev.warnings + parsed.warnings)
                    still = PatchApplier(ws).check(parsed.changes)
                    if still:
                        notes.append(f"validation: {len(still)} block(s) still do not apply: "
                                     + "; ".join(f.message for f in still[:3]))

            analysis = parsed.analysis
            if not parsed.changes and not parsed.wants_context and "response did not follow the protocol" in parsed.warnings:
                analysis = "llm-error: response did not follow the protocol: " + (parsed.raw.strip()[:300])
            elif not parsed.changes and parsed.wants_context:
                analysis = (analysis + "\n" if analysis else "") + \
                    f"(still requesting context after {self.cfg.llm.max_context_rounds} round(s): " + \
                    ", ".join(parsed.need_files + parsed.need_grep) + ")"
            if last and last.finish_reason in ("length", "max_tokens"):
                notes.append(f"output truncated (finish_reason={last.finish_reason})")
            if notes:
                analysis = (analysis + "\n" if analysis else "") + "\n".join(f"[sbmig] {n}" for n in notes)

            proposal = FixProposal(
                changes=parsed.changes,
                analysis=analysis,
                confidence=parsed.confidence if parsed.confidence is not None else 0.5,
                model=last.model if last else (model or ""),
                prompt_hash=messages_hash(messages),
                llm_usage=usage,
                requested_files=requested,
                raw_response=last.content if last else "",
            )
        except LLMError as exc:
            proposal = self._error_proposal(f"llm-error: {exc}", model, messages, usage, requested)
        except Exception as exc:  # never raise into the orchestrator
            log.exception("LLM fixer failed for cluster %s", cluster.id)
            proposal = self._error_proposal(f"llm-error: {type(exc).__name__}: {exc}", model, messages, usage, requested)

        self._journal(ws, cluster, proposal, rounds, adv_in_prompt)
        self._record(cluster, transcript, proposal)
        return proposal

    # ------------------------------------------------------------------ building blocks
    def build_messages(self, packed: PackedContext) -> list[ChatMessage]:
        user = self.user_template.render(sections=packed.sections, truncated=packed.truncated)
        return [ChatMessage(role="system", content=self.system_prompt),
                ChatMessage(role="user", content=user.strip() + "\n")]

    def _model(self, ctx: FixContext) -> Optional[str]:
        llm = self.cfg.llm
        if ctx.use_escalation_model and not _unset(llm.escalation_model):
            return llm.escalation_model
        return None if _unset(llm.model) else llm.model

    @staticmethod
    def call_tags(ctx: Optional[FixContext], cluster_id: Optional[str] = None,
                  ws: Optional[Workspace] = None) -> tuple[str, dict]:
        """(role, metadata) sent with every LLM call of one propose() — lets a gateway route/budget by purpose.
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
              transcript: list[dict], *, role: str = "fix",
              metadata: Optional[dict] = None) -> tuple[LLMResponse, ParsedResponse]:
        with llm_call_in_flight():          # the loop's watchdog stays silent while the call heartbeats (round 10)
            resp = self.client.complete(messages, model=model, max_tokens=self.cfg.llm.max_output_tokens,
                                        temperature=self.cfg.llm.temperature, role=role,
                                        metadata=dict(metadata) if metadata else None)
        u = usage_from(resp)
        usage.model = usage.model or u.model
        usage.calls += 1
        usage.input_tokens += u.input_tokens
        usage.output_tokens += u.output_tokens
        parsed = parse_fix_response(resp.content)
        transcript.append({"prompt_hash": messages_hash(messages), "response": resp.content,
                           "usage": u.model_dump(), "warnings": parsed.warnings})
        if parsed.warnings:
            log.debug("protocol warnings: %s", parsed.warnings)
        return resp, parsed

    def _error_proposal(self, analysis: str, model: Optional[str], messages: list[ChatMessage], usage: LLMUsage,
                        requested: list[str]) -> FixProposal:
        log.warning("%s", analysis[:300])
        return FixProposal(changes=[], analysis=analysis[:1000], confidence=0.0, model=model or "",
                           prompt_hash=messages_hash(messages) if messages else "", llm_usage=usage,
                           requested_files=requested)

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
            parts.append("This was the LAST context round. Answer now with <patches> (or explain in <analysis> why no "
                         "safe fix exists and return empty <patches></patches>).")
        else:
            parts.append(f"You may ask for more context {remaining_rounds} more time(s), but prefer answering with <patches> now.")
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

    # ------------------------------------------------------------------ validation feedback
    def feedback_message(self, ws: Workspace, failures: list[BlockFailure]) -> str:
        parts = ["Your patch could not be applied. Problems:"]
        shown: set[tuple[str, int, int]] = set()
        for f in failures[:8]:
            if f.reason in ("not_found", "ambiguous") and ws.exists(f.path):
                text = ws.read(f.path).replace("\r\n", "\n")
                if f.reason == "not_found":
                    a, b = best_window(text, f.search or "", pad=FEEDBACK_PAD)
                    lines = text.split("\n")
                    body = "\n".join(lines[a - 1:b])
                    fence = fence_for(body)
                    head = f"- block {f.block} of {f.path}: search text not found. Here is the current exact text of lines {a}-{b}:"
                    if (f.path, a, b) not in shown:
                        shown.add((f.path, a, b))
                        parts.append(f"{head}\n{fence}{lang_for(f.path)}\n{body}\n{fence}")
                    else:
                        parts.append(head + " (see above)")
                else:
                    parts.append(f"- block {f.block} of {f.path}: search text matches {f.matches} times (ambiguous) — "
                                 f"add surrounding lines so it matches exactly once.")
            elif f.reason == "missing_file":
                _, cands = self.resolve_path(ws, PurePosixPath(f.path).name)
                hint = f" Existing files with that name: {', '.join(cands)}" if cands else ""
                parts.append(f"- {f.message}.{hint}")
            elif f.reason == "exists":
                parts.append(f"- {f.message}. Use MODIFY with SEARCH/REPLACE blocks for existing files.")
            elif f.reason == "truncated":
                parts.append(f"- {f.message}. Never output partial files; use small SEARCH/REPLACE blocks.")
            else:
                parts.append(f"- {f.message}")
        if len(failures) > 8:
            parts.append(f"- … and {len(failures) - 8} more problem(s)")
        parts.append("Re-send the COMPLETE corrected answer (<analysis>, <confidence>, <patches> with ALL changes, "
                     "including those that were fine). Copy SEARCH text exactly from the text shown.")
        return "\n\n".join(parts) + "\n"

    # ------------------------------------------------------------------ bookkeeping
    def _journal(self, ws: Workspace, cluster: ErrorCluster, p: FixProposal, rounds: int,
                 advisory_hints: int = 0) -> None:
        try:
            ws.journal({"event": "llm_fix", "cluster_id": cluster.id, "model": p.model, "rounds": rounds,
                        "calls": p.llm_usage.calls,
                        "tokens": {"input": p.llm_usage.input_tokens, "output": p.llm_usage.output_tokens},
                        "changes": [{"op": c.op, "path": c.path} for c in p.changes],
                        "confidence": p.confidence, "prompt_hash": p.prompt_hash,
                        "error": p.analysis if p.analysis.startswith("llm-error") else None,
                        "advisory_hints": advisory_hints})   # library-guidance lines that reached the prompt
        except Exception:  # pragma: no cover
            log.debug("journal failed", exc_info=True)

    def _record(self, cluster: ErrorCluster, transcript: list[dict], p: FixProposal) -> None:
        rd = self.cfg.llm.record_dir
        if not rd or isinstance(self.client, RecordingClient):
            return
        try:
            d = self.cfg.resolve_path(rd)
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", cluster.id)[:80]
            path = d / f"fix-{safe}-{(p.prompt_hash or 'none')[:12]}.json"
            path.write_text(json.dumps({"cluster_id": cluster.id, "calls": transcript,
                                        "proposal": p.model_dump(mode="json")}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError:  # pragma: no cover
            log.debug("record failed", exc_info=True)


__all__ = ["LLMFixer", "profile_summary", "load_prompts", "normalize_path"]
