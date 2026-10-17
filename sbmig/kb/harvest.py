"""KB harvest: turn successful LLM fixes of a phase into KB cases, credit used recipes/patches, and (optionally)
ask the LLM for a declarative OpenRewrite recipe candidate for mechanical fixes."""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional

from sbmig.core.interfaces import ChatMessage, LLMClientLike
from sbmig.core.models import (
    BuildError,
    DecisionKind,
    ErrorKind,
    FileChange,
    FixAttempt,
    HarvestResult,
    KBCase,
    KBRecipe,
    Phase,
    PhaseResult,
    ProjectProfile,
)
from sbmig.core.workspace import Workspace

from .recipes import is_none_answer, strip_fences, validate_recipe_yaml

if TYPE_CHECKING:  # pragma: no cover
    from .store import FileKnowledgeBase

log = logging.getLogger("sbmig.kb")

SECRET_RE = re.compile(r"(?i)(password|secret|token|api[_-]?key)(\s*[=:]\s*)\S+")
RAW_LIMIT = 2000
MECHANICAL_MAX_LINES = 40
PROMPT_PATH = Path(__file__).with_name("prompts") / "recipe_candidate.md"
SUCCESS_OUTCOMES = ("improved", "build_ok")


# --------------------------------------------------------------------------- sanitization
def redact(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def _rel_posix(path: Optional[str], ws: Optional[Workspace]) -> Optional[str]:
    if not path:
        return path
    p = path.replace("\\", "/")
    if ws is not None and (p.startswith("/") or re.match(r"^[A-Za-z]:/", p)):
        p = ws.rel(path).replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        p = PurePosixPath(p).name          # outside the project: never leak absolute paths
    while p.startswith("./"):
        p = p[2:]
    return p


def _strip_roots(text: str, ws: Optional[Workspace]) -> str:
    if ws is not None:
        for root in {str(ws.root), str(ws.root).replace("\\", "/")}:
            text = text.replace(root + "/", "").replace(root, "")
    return text


def _sanitize_error(e: BuildError, ws: Optional[Workspace]) -> BuildError:
    raw = redact(_strip_roots(e.raw or "", ws)) or ""
    if len(raw) > RAW_LIMIT:
        raw = raw[:RAW_LIMIT] + "…[truncated]"
    return e.model_copy(update={
        "file": _rel_posix(e.file, ws),
        "message": redact(_strip_roots(e.message, ws)) or "",
        "location": redact(_strip_roots(e.location, ws)) if e.location else e.location,
        "raw": raw,
    })


def _errors_from_signatures(sigs: list[str]) -> list[BuildError]:
    out = []
    for s in sigs:
        head = s.split("|", 1)[0]
        try:
            kind = ErrorKind(head)
        except ValueError:
            kind = ErrorKind.UNKNOWN
        out.append(BuildError(kind=kind, message=s, signature=s))
    return out


def _changed_files(changes: list[FileChange]) -> list[str]:
    files: list[str] = []
    for ch in changes:
        b = PurePosixPath(ch.path).name
        if b not in files:
            files.append(b)
    return files


def sanitize_case(attempt: FixAttempt, profile: Optional[ProjectProfile] = None, ws: Optional[Workspace] = None,
                  phase: Optional[Phase] = None) -> KBCase:
    """Build a KBCase from a successful attempt: relative posix paths, secrets redacted, raw truncated."""
    errors_src = attempt.cluster_errors or _errors_from_signatures(attempt.cluster_signatures)
    errors = [_sanitize_error(e, ws) for e in errors_src]
    l1: list[str] = []
    for s in [*attempt.cluster_signatures, *(e.signature for e in errors)]:
        if s and s not in l1:
            l1.append(s)
    l2 = sorted({e.signature_generic for e in errors if e.signature_generic})

    changes = []
    for ch in attempt.changes:
        changes.append(ch.model_copy(update={
            "path": _rel_posix(ch.path, ws) or ch.path,
            "new_path": _rel_posix(ch.new_path, ws),
            "search": redact(ch.search),
            "replace": redact(ch.replace),
            "content": redact(ch.content),
        }))

    first_line = next((ln.strip() for ln in (attempt.analysis or "").splitlines() if ln.strip()), "")
    if first_line:
        summary = first_line[:240]
    else:
        sig = l2[0] if l2 else (l1[0] if l1 else "build error")
        summary = f"{sig} fixed by editing {', '.join(_changed_files(changes)) or 'nothing'}"
    summary = redact(_strip_roots(summary, ws)) or summary

    kind = errors[0].kind if errors else ErrorKind.UNKNOWN
    return KBCase(
        id="",
        project_fingerprint=profile.fingerprint if profile else "",
        case_type=profile.case_type if profile else KBCase.model_fields["case_type"].default,
        phase=phase or attempt.phase,
        signatures=l1,
        signatures_generic=l2,
        error_kind=kind,
        errors=errors,
        fix_kind="llm_patch",
        changes=changes,
        summary=summary,
        outcome=attempt.outcome,
        model=attempt.model,
        tags=[f"attempt:{attempt.id}"] if attempt.id else [],
    )


# --------------------------------------------------------------------------- recipe candidates
def changes_as_diff(changes: list[FileChange]) -> str:
    parts: list[str] = []
    for ch in changes:
        if ch.op == "modify":
            a = (ch.search or "").splitlines(keepends=False)
            b = (ch.replace or "").splitlines(keepends=False)
            diff = difflib.unified_diff(a, b, f"a/{ch.path}", f"b/{ch.path}", lineterm="", n=2)
            parts.append("\n".join(diff) or f"=== MODIFY {ch.path} (no textual change)")
        elif ch.op == "create":
            body = "\n".join("+" + ln for ln in (ch.content or "").splitlines())
            parts.append(f"=== CREATE {ch.path}\n{body}")
        elif ch.op == "delete":
            parts.append(f"=== DELETE {ch.path}")
        else:
            parts.append(f"=== RENAME {ch.path} -> {ch.new_path}")
    return "\n\n".join(parts)


def _changed_line_count(changes: list[FileChange]) -> int:
    n = 0
    for ch in changes:
        a = (ch.search or "").splitlines()
        b = (ch.replace or "").splitlines()
        for ln in difflib.unified_diff(a, b, lineterm="", n=0):
            if (ln.startswith("+") or ln.startswith("-")) and not ln.startswith(("+++", "---")):
                n += 1
    return n


def looks_mechanical(case: KBCase) -> bool:
    if any(PurePosixPath(ch.path).name == "pom.xml" for ch in case.changes):
        return True
    if not case.changes or any(ch.op != "modify" for ch in case.changes):
        return False
    return _changed_line_count(case.changes) <= MECHANICAL_MAX_LINES


def build_recipe_prompt(case: KBCase) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    errs = "\n".join(f"- [{e.kind.value}] {e.file or '-'}:{e.line or ''} {e.message}"
                     + (f" (symbol: {e.symbol})" if e.symbol else "")
                     for e in case.errors[:20]) or "- (none recorded)"
    sigs = "\n".join(f"- {s}" for s in [*case.signatures, *case.signatures_generic]) or "- (none)"
    return (template.replace("{{ERRORS}}", errs).replace("{{SIGNATURES}}", sigs)
            .replace("{{CHANGES}}", changes_as_diff(case.changes)))


def propose_recipe_candidate(kb: "FileKnowledgeBase", case: KBCase, llm: LLMClientLike,
                             notes: list[str]) -> Optional[KBRecipe]:
    messages = [
        ChatMessage(role="system", content="You convert mechanical Java/Maven migration fixes into declarative "
                                           "OpenRewrite recipes, or answer NONE."),
        ChatMessage(role="user", content=build_recipe_prompt(case)),
    ]
    cfg = getattr(kb, "cfg", None)
    recipe_model = getattr(getattr(cfg, "llm", None), "recipe_model", None) or None
    resp = llm.complete(messages, model=recipe_model, max_tokens=1500, temperature=0.0)
    answer = strip_fences(resp.content or "")
    if is_none_answer(answer):
        notes.append(f"case {case.id}: LLM says no mechanical recipe")
        return None
    ok, errors, parsed = validate_recipe_yaml(answer)
    if not ok or parsed is None:
        notes.append(f"case {case.id}: recipe candidate rejected: {'; '.join(errors)}")
        return None
    rid = parsed["name"]
    existing = kb.get_recipe(rid)
    if existing is not None:
        for s in [*case.signatures, *case.signatures_generic]:
            if s not in existing.signatures:
                existing.signatures.append(s)
        if case.id not in existing.source_case_ids:
            existing.source_case_ids.append(case.id)
        kb._write_recipe_meta(existing, kb._recipe_meta_extra(existing))
        kb._index_recipe(existing)
        kb._save_index()
        notes.append(f"case {case.id}: recipe {rid} already exists ({existing.status}); linked")
        return existing
    recipe = KBRecipe(
        id=rid,
        status="candidate",
        path="",
        description=str(parsed.get("description") or parsed.get("displayName") or ""),
        signatures=[*case.signatures, *(s for s in case.signatures_generic if s not in case.signatures)],
        source_case_ids=[case.id],
    )
    return kb.add_recipe(answer, recipe)


# --------------------------------------------------------------------------- main entry
def _is_harvestable(a: FixAttempt) -> bool:
    return (a.decision.kind == DecisionKind.LLM_FIX and a.applied and a.outcome in SUCCESS_OUTCOMES
            and a.cluster_resolved is not False and bool(a.changes))


def harvest_phase(kb: "FileKnowledgeBase", phase_result: PhaseResult, profile: ProjectProfile, ws: Workspace,
                  llm: Optional[LLMClientLike] = None) -> HarvestResult:
    """Never raises; problems are collected in ``HarvestResult.notes``."""
    res = HarvestResult()
    try:
        if not kb.cfg.kb.harvest:
            res.notes.append("kb harvest disabled (kb.harvest=false)")
            return res
        fp = profile.fingerprint if profile else ""
        new_cases: list[KBCase] = []

        # 1. cases from successful LLM fixes
        for a in phase_result.attempts:
            if not _is_harvestable(a):
                continue
            try:
                case = sanitize_case(a, profile, ws, phase_result.phase)
                cid, created = kb.record_case_ex(case)
                if created:
                    res.cases_created.append(cid)
                    stored = kb.get_case(cid)
                    if stored is not None:
                        new_cases.append(stored)
                else:
                    res.notes.append(f"attempt {a.id}: duplicate of case {cid} (reuse bumped)")
            except Exception as e:
                log.warning("kb harvest of attempt %s failed: %s", a.id, e)
                res.notes.append(f"attempt {a.id}: harvest failed: {e}")

        # 2. credit recipes / replayed patches
        for a in phase_result.attempts:
            try:
                success = a.applied and a.outcome in SUCCESS_OUTCOMES
                if a.decision.kind == DecisionKind.KB_RECIPE:
                    for rid in a.decision.recipe_ids:
                        if kb.get_recipe(rid) is None:      # builtin / pattern recipe ids are not KB recipes
                            continue
                        kb.credit_recipe(rid, success, fp)
                        if rid not in res.recipes_credited:
                            res.recipes_credited.append(rid)
                elif a.decision.kind == DecisionKind.KB_PATCH and success:
                    for cid in a.decision.kb_case_ids:
                        if kb.bump_case_reuse(cid, fp) is None:
                            res.notes.append(f"attempt {a.id}: replayed case {cid} not found in kb")
            except Exception as e:
                log.warning("kb credit for attempt %s failed: %s", a.id, e)
                res.notes.append(f"attempt {a.id}: credit failed: {e}")

        # 3. recipe candidates
        if kb.cfg.kb.generate_recipe_candidates and llm is not None:
            for case in new_cases:
                if not looks_mechanical(case):
                    continue
                try:
                    recipe = propose_recipe_candidate(kb, case, llm, res.notes)
                    if recipe is not None:
                        case.recipe_candidate_id = recipe.id
                        kb.update_case(case)
                        if recipe.status == "candidate" and recipe.id not in res.candidates_created \
                                and recipe.source_case_ids == [case.id]:
                            res.candidates_created.append(recipe.id)
                except Exception as e:
                    log.warning("kb recipe candidate for case %s failed: %s", case.id, e)
                    res.notes.append(f"case {case.id}: recipe candidate generation failed: {e}")
        elif kb.cfg.kb.generate_recipe_candidates and new_cases:
            res.notes.append("recipe candidate generation skipped: no LLM client")
    except Exception as e:
        log.exception("kb harvest failed")
        res.notes.append(f"harvest failed: {e}")
    return res
