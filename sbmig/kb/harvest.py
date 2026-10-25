"""KB harvest: turn successful LLM fixes of a phase into labeled KB cases, credit used recipes/patches,
compile a recipe candidate from the fix labels (no LLM) or — failing that — ask the LLM for one, and append
the phase's ResolutionRecords to ``kb/ledger.jsonl`` / update ``kb/symbols.json``."""
from __future__ import annotations

import contextlib
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

from .labels import compile_recipe, label_changes
from .ledger import attempt_record, blocked_record, plan_recipe_records, project_name
from .recipes import is_none_answer, short_recipe_name, strip_fences, validate_recipe_yaml, with_recipe_name
from .redact import get_redactor

if TYPE_CHECKING:  # pragma: no cover
    from .store import FileKnowledgeBase

log = logging.getLogger("sbmig.kb")

RAW_LIMIT = 2000
MECHANICAL_MAX_LINES = 40
PROMPT_PATH = Path(__file__).with_name("prompts") / "recipe_candidate.md"
SUCCESS_OUTCOMES = ("improved", "build_ok")
NO_REPLAY_TAG = "no-replay:redacted"    # its search / replace text had a secret masked: hint only, never replayed


def _lock(kb):
    """The KB's cross-process lock, or a no-op for a KB-like object without one."""
    lock = getattr(kb, "lock", None)
    return lock() if callable(lock) else contextlib.nullcontext()


# --------------------------------------------------------------------------- sanitization
def redact(text: Optional[str], cfg=None) -> Optional[str]:
    """Mask secret VALUES by context (``sbmig.kb.redact``: built-ins + ``kb.redact_file``); code is left alone."""
    if text is None:
        return None
    return get_redactor(cfg).redact(text)


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


def _sanitize_error(e: BuildError, ws: Optional[Workspace], cfg=None) -> BuildError:
    raw = redact(_strip_roots(e.raw or "", ws), cfg) or ""
    if len(raw) > RAW_LIMIT:
        raw = raw[:RAW_LIMIT] + "…[truncated]"
    return e.model_copy(update={
        "file": _rel_posix(e.file, ws),
        "message": redact(_strip_roots(e.message, ws), cfg) or "",
        "location": redact(_strip_roots(e.location, ws), cfg) if e.location else e.location,
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


# --------------------------------------------------------------------------- symbol owner
_PACKAGE_DECL_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_IMPORT_DECL_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;", re.MULTILINE)
_TYPE_SYMBOL_PREFIXES = ("class ", "interface ", "enum ", "record ", "package ", "@interface ")


def _owner_hint(changes: list[FileChange], errors: list[BuildError]) -> str:
    """The (often simple) owner name: first ``method_renames`` owner, else javac's owner of the first error whose
    symbol is a member (for ``symbol: class X`` the location is the *referencing* class, not an owner)."""
    lab = label_changes(changes, errors)
    for r in lab.details.get("method_renames") or []:
        if r.get("owner"):
            return r["owner"]
    from sbmig.build.classpath import owner_fqcn
    for e in errors:
        if (e.symbol or "").strip().startswith(_TYPE_SYMBOL_PREFIXES):
            continue
        try:
            o = owner_fqcn(e)
        except Exception:  # pragma: no cover - defensive
            o = None
        if o:
            return o
    return ""


def _java_texts(changes: list[FileChange], ws: Workspace | None) -> list[str]:
    """Current contents of the changed .java files (falling back to the change text itself)."""
    texts: list[str] = []
    for ch in changes:
        for path in (ch.new_path, ch.path):
            if not path or not path.endswith(".java"):
                continue
            if ws is not None:
                try:
                    texts.append(ws.read(path))
                except Exception as exc:  # deleted / renamed file: the change text below still counts
                    log.debug("resolve_symbol_owner: cannot read %s: %s", path, exc)
            for t in (ch.replace, ch.content, ch.search):
                if t:
                    texts.append(t)
    return texts


def resolve_symbol_owner(changes: list[FileChange], errors: list[BuildError], ws: Workspace | None,
                         cfg=None) -> str:
    """FQCN of the type owning the error-case symbol, or "". javac prints imported types by their simple name
    (``location: class Client``), so a simple owner is resolved through the changed files: explicit import
    (also ``import static pkg.Simple.*`` / ``.member``) → wildcard import candidates via ``locate_class`` (only
    when ``cfg`` is given; runs Maven) → same package. Never raises."""
    try:
        hint = _owner_hint(list(changes or []), list(errors or []))
        if not hint:
            return ""
        if "." in hint and hint[0].islower():
            return hint
        head, _, rest = hint.partition(".")             # nested type `Outer.Inner`
        suffix = f".{rest}" if rest else ""
        texts = _java_texts(list(changes or []), ws)
        wildcards: list[str] = []
        for text in texts:
            for m in _IMPORT_DECL_RE.finditer(text):
                static, name, wild = bool(m.group(1)), m.group(2), bool(m.group(3))
                if not static and wild:
                    if name not in wildcards:
                        wildcards.append(name)
                    continue
                if name.rsplit(".", 1)[-1] == head:
                    return name + suffix
                if static and not wild and "." in name:
                    owner = name.rsplit(".", 1)[0]
                    if owner.rsplit(".", 1)[-1] == head:
                        return owner + suffix
        if wildcards and cfg is not None and ws is not None:
            from sbmig.build.classpath import locate_class
            module = next((e.module for e in errors or [] if e.module), None)
            for pkg in wildcards:
                try:
                    loc = locate_class(ws, cfg, f"{pkg}.{head}", module)
                except Exception as exc:  # pragma: no cover - locate_class never raises
                    log.warning("resolve_symbol_owner: %s: %s", type(exc).__name__, exc)
                    loc = None
                if loc is not None:
                    return f"{pkg}.{head}{suffix}"
        for text in texts:
            m = _PACKAGE_DECL_RE.search(text)
            if m:
                return f"{m.group(1)}.{head}{suffix}"
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("resolve_symbol_owner failed: %s: %s", type(exc).__name__, exc)
    return ""


def sanitize_case(attempt: FixAttempt, profile: Optional[ProjectProfile] = None, ws: Optional[Workspace] = None,
                  phase: Phase | None = None, *, cfg=None) -> KBCase:
    """Build a KBCase from a successful attempt: relative posix paths, secret values masked, raw truncated.
    ``symbol_owner`` is resolved from the changed files' imports (``cfg`` enables wildcard-import lookups). A change
    whose search / replace / content had a secret masked is stored as it is masked, and the case is tagged
    ``no-replay:redacted`` (its masked text is not the code to put back: hint only, never replayed)."""
    errors_src = attempt.cluster_errors or _errors_from_signatures(attempt.cluster_signatures)
    errors = [_sanitize_error(e, ws, cfg) for e in errors_src]
    l1: list[str] = []
    for s in [*attempt.cluster_signatures, *(e.signature for e in errors)]:
        if s and s not in l1:
            l1.append(s)
    l2 = sorted({e.signature_generic for e in errors if e.signature_generic})

    changes = []
    masked = False
    for ch in attempt.changes:
        upd = {"path": _rel_posix(ch.path, ws) or ch.path, "new_path": _rel_posix(ch.new_path, ws)}
        for k in ("search", "replace", "content"):
            v = getattr(ch, k)
            upd[k] = redact(v, cfg)
            masked = masked or upd[k] != v
        changes.append(ch.model_copy(update=upd))

    first_line = next((ln.strip() for ln in (attempt.analysis or "").splitlines() if ln.strip()), "")
    if first_line:
        summary = first_line[:240]
    else:
        sig = l2[0] if l2 else (l1[0] if l1 else "build error")
        summary = f"{sig} fixed by editing {', '.join(_changed_files(changes)) or 'nothing'}"
    summary = redact(_strip_roots(summary, ws), cfg) or summary

    kind = errors[0].kind if errors else ErrorKind.UNKNOWN
    lab = label_changes(changes, errors)
    tags = [f"attempt:{attempt.id}"] if attempt.id else []
    if masked:
        tags.append(NO_REPLAY_TAG)
    return KBCase(
        id="",
        project_fingerprint=profile.fingerprint if profile else "",
        project=project_name(profile, ws),
        symbol=lab.symbol,
        symbol_owner=resolve_symbol_owner(changes, errors, ws, cfg),
        fix_labels=lab.labels,
        replacement=redact(lab.replacement, cfg) or "",
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
        tags=tags,
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
    labels = (f"- symbol: {case.symbol or '-'}\n- labels: {', '.join(case.fix_labels) or '-'}\n"
              f"- replacement: {case.replacement or '-'}")
    return (template.replace("{{ERRORS}}", errs).replace("{{SIGNATURES}}", sigs)
            .replace("{{LABELS}}", labels).replace("{{CHANGES}}", changes_as_diff(case.changes)))


def propose_recipe_candidate(kb: "FileKnowledgeBase", case: KBCase, llm: LLMClientLike,
                             notes: list[str]) -> Optional[KBRecipe]:
    messages = [
        ChatMessage(role="system", content="You convert mechanical Java/Maven migration fixes into declarative "
                                           "OpenRewrite recipes, or answer NONE."),
        ChatMessage(role="user", content=build_recipe_prompt(case)),
    ]
    cfg = getattr(kb, "cfg", None)
    recipe_model = getattr(getattr(cfg, "llm", None), "recipe_model", None) or None
    resp = llm.complete(messages, model=recipe_model, max_tokens=1500, temperature=0.0,
                        role="recipe_candidate", metadata={"case_id": case.id})
    answer = strip_fences(resp.content or "")
    if is_none_answer(answer):
        notes.append(f"case {case.id}: LLM says no mechanical recipe")
        return None
    ok, errors, parsed = validate_recipe_yaml(answer)
    if not ok or parsed is None:
        notes.append(f"case {case.id}: recipe candidate rejected: {'; '.join(errors)}")
        return None
    return _store_candidate(kb, case, answer, parsed, notes)


def _store_candidate(kb: "FileKnowledgeBase", case: KBCase, text: str, parsed: dict, notes: list[str],
                     origin: str = "") -> KBRecipe:
    rid = short_recipe_name(parsed["name"])            # the LLM may still write the old com.company.kb. prefix
    if rid != parsed["name"]:
        text = with_recipe_name(text, rid)
    with _lock(kb):                                    # read-modify-write of the recipe meta + index
        existing = kb.get_recipe(rid)                  # also finds a legacy com.company.kb.<rid> (store)
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
        if origin:
            notes.append(f"case {case.id}: recipe candidate {rid} {origin}")
        recipe = KBRecipe(
            id=rid,
            status="candidate",
            path="",
            description=str(parsed.get("description") or parsed.get("displayName") or ""),
            signatures=[*case.signatures, *(s for s in case.signatures_generic if s not in case.signatures)],
            source_case_ids=[case.id],
        )
        return kb.add_recipe(text, recipe)


def compile_candidate(kb: "FileKnowledgeBase", case: KBCase, notes: list[str]) -> Optional[KBRecipe]:
    """Deterministic recipe candidate from the case's fix labels (no LLM), or None when not 1:1."""
    text = compile_recipe(case)
    if not text:
        return None
    ok, errors, parsed = validate_recipe_yaml(text)
    if not ok or parsed is None:  # pragma: no cover - compile_recipe validates already
        notes.append(f"case {case.id}: compiled recipe invalid: {'; '.join(errors)}")
        return None
    return _store_candidate(kb, case, text, parsed, notes, origin="compiled from labels, no LLM")


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
        kbc = kb.cfg.kb
        fp = profile.fingerprint if profile else ""
        new_cases: list[KBCase] = []
        indexed: list[KBCase] = []                       # new + deduped cases → symbols.json
        case_of_attempt: dict[str, str] = {}

        # 1. cases from successful LLM fixes
        for a in phase_result.attempts:
            if not _is_harvestable(a):
                continue
            try:
                case = sanitize_case(a, profile, ws, phase_result.phase, cfg=kb.cfg)
                cid, created = kb.record_case_ex(case)
                case_of_attempt[a.id] = cid
                stored = kb.get_case(cid)
                if created:
                    res.cases_created.append(cid)
                    if stored is not None:
                        new_cases.append(stored)
                else:
                    res.notes.append(f"attempt {a.id}: duplicate of case {cid} (reuse bumped)")
                    if stored is not None:
                        indexed.append(stored)
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
                        c = kb.bump_case_reuse(cid, fp)
                        if c is None:
                            res.notes.append(f"attempt {a.id}: replayed case {cid} not found in kb")
                        else:
                            indexed.append(c)
            except Exception as e:
                log.warning("kb credit for attempt %s failed: %s", a.id, e)
                res.notes.append(f"attempt {a.id}: credit failed: {e}")

        # 3. recipe candidates: compiled from the labels first (no LLM), the LLM only for the rest
        if kbc.generate_recipe_candidates:
            skipped_no_llm = False
            for case in new_cases:
                try:
                    recipe = compile_candidate(kb, case, res.notes) if kbc.deterministic_candidates else None
                    if recipe is None and looks_mechanical(case):
                        if llm is None:
                            skipped_no_llm = True
                        else:
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
            if skipped_no_llm:
                res.notes.append("recipe candidate generation skipped: no LLM client")
        indexed = new_cases + indexed

        # 4. symbol index (loaded, merged and saved under the lock: another process may have written it)
        if kbc.symbol_index and indexed:
            try:
                with _lock(kb):
                    idx = kb.symbol_index()
                    for c in indexed:
                        idx.add(c, project_key=fp)
                    idx.save()
            except Exception as e:
                log.warning("kb symbol index update failed: %s", e)
                res.notes.append(f"symbol index update failed: {e}")

        # 5. ledger: one ResolutionRecord per attempt / plan KB recipe / blocked item of the phase
        if kbc.ledger:
            try:
                recs = []
                cases = {c.id: c for c in indexed}
                for a in phase_result.attempts:
                    cid = case_of_attempt.get(a.id)
                    rid = cases[cid].recipe_candidate_id if cid in cases else None
                    r = attempt_record(a, profile, ws, phase_result.phase, case_id=cid, recipe_id=rid)
                    if r is not None:
                        recs.append(r)
                recs += plan_recipe_records(phase_result, kb, profile, ws)
                for b in phase_result.blocked:
                    recs.append(blocked_record(b, profile, ws, phase_result.phase))
                    mark = getattr(kb, "mark_blocked_recorded", None)
                    if callable(mark):
                        mark(ws.run_id, b.id)
                res.ledger_entries = kb.ledger().append(recs)
            except Exception as e:
                log.warning("kb ledger append failed: %s", e)
                res.notes.append(f"ledger append failed: {e}")
    except Exception as e:
        log.exception("kb harvest failed")
        res.notes.append(f"harvest failed: {e}")
    try:
        drain = getattr(kb, "drain_promoted", None)
        if callable(drain):
            res.recipes_promoted = list(drain())
    except Exception:  # pragma: no cover
        pass
    return res
