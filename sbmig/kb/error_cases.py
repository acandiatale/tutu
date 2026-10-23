"""Error cases (2026-10-23): every build error of a run on its own record (``ErrorCaseRecord``: what it was, what
was tried, how it was resolved — with the code applied — or why not) and every fix attempt on its own record
(``FixAttemptRecord``, failures included, with its diff). See docs/INTERFACES.md "Error cases".

* :func:`prepare_error_cases` turns the tracker's raw records (``sbmig.loop.error_tracker``) into KB records:
  project fields, sanitizing (relative posix paths, workspace roots stripped, secrets redacted, capped sizes),
  :func:`sbmig.kb.changes.classify_diff` per attempt, fix labels from the diff, the cross-project error symbol and
  the resolving attempt denormalized into each resolved error.
* :func:`write_error_cases_jsonl` / :func:`write_error_cases_csv` / :func:`write_fix_attempts_csv`: run-dir files
  (the CSVs are UTF-8 with BOM so Excel opens Korean text).
* :class:`ErrorCaseStore` (``kb/errors.jsonl``, ``kb/fix_attempts.jsonl``, ``kb/error_hints.json``): append-only
  store, regex queries, summaries, text-replace rule candidates and the hints index the fix prompt reads (past
  fixes AND past failures of the same L2 signature / error symbol). The case catalog (:mod:`sbmig.kb.catalog`)
  folds these events into one reusable case per L2 (``ErrorCaseRecord.case_id``).

File-based on purpose (invariant 3): the jsonl files are the source of truth, ``error_hints.json`` is derived and
``sbmig kb reindex`` rebuilds it.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from pydantic import BaseModel

from sbmig.core.models import (
    GAV,
    BuildError,
    CaseType,
    ErrorCaseRecord,
    FixAttemptRecord,
    ProjectProfile,
    utcnow,
)

from .changes import classify_diff, diff_for_file, file_changes_from_diff, path_matches
from .harvest import RAW_LIMIT, SECRET_RE, _rel_posix, redact
from .labels import label_changes, symbol_for_errors
from .ledger import project_name

log = logging.getLogger("sbmig.kb")

HINTS_VERSION = 1
RESOLVED = ("resolved", "resolved_indirectly")
NOT_RESOLVED = ("unresolved", "blocked", "not_visible")
FAILED_OUTCOMES = ("no_change", "regressed", "reverted", "apply_failed")
TRUNCATED = "…[truncated]"
DESCRIPTION_LIMIT = 4000
FAILURE_LIMIT = 2000
ITEM_LIMIT = 500                 # one "file:line message" of problem / introduced / remaining errors, one note
LIST_LIMIT = 20
CSV_CELL_LIMIT = 2000
CSV_DIFF_LIMIT = 30000           # Excel's cell limit is 32,767 characters
HINT_TEXT_LIMIT = 200
HINT_PROJECTS = 50
HINT_VARIANTS = 25               # fix / failure variants kept per key (first come; the same in incremental + rebuild)
HINT_TOP = 3

ERROR_CSV_COLUMNS = [
    "id", "project", "run_id", "status", "error_kind", "file", "line", "message", "symbol", "signature_generic",
    "case_id", "first_seen", "resolved_at", "fix_method", "resolver_id", "change_kind", "replace_pairs",
    "replace_rule_candidate", "description", "failure_reason", "problem_errors", "block_reason", "suggestion",
    "attempt_count", "llm_attempts", "attempts",
]
ATTEMPT_CSV_COLUMNS = [
    "id", "project", "run_id", "phase", "iteration", "attempt_id", "cluster_id", "fix_method", "resolver_id", "model",
    "outcome", "applied", "change_kind", "replace_pairs", "fix_labels", "replacement", "description", "failure",
    "introduced_errors", "remaining_errors", "files", "lines_added", "lines_removed", "error_ids", "llm_calls",
    "diff_truncated", "diff",
]

Record = Union[ErrorCaseRecord, FixAttemptRecord]


# --------------------------------------------------------------------------- sanitizing
def _roots(ws: Any) -> list[tuple[str, str]]:
    """(needle, replacement) pairs: the workspace roots → "" (relative paths), the user's home → "~"."""
    found: set[str] = set()
    for attr in ("root", "run_dir"):
        r = getattr(ws, attr, None) if ws is not None else None
        if r:
            s = str(r)
            found |= {s, s.replace("\\", "/")}
            if s.startswith("/private/"):                    # macOS: /var/… and /tmp/… resolve to /private/…
                found.add(s[len("/private"):])
    # longest first: <run dir>/project is stripped as the project root before <run dir> itself
    out = [(r, "") for r in sorted((r for r in found if len(r) > 1), key=len, reverse=True)]
    try:
        home = str(Path.home())
    except Exception:  # pragma: no cover - no HOME
        home = ""
    if len(home) > 3:                                        # C:\Users\<name>\.m2\… names the operator
        out += [(home, "~"), (home.replace("\\", "/"), "~")]
    return out


def _strip(text: Optional[str], roots: list[tuple[str, str]]) -> str:
    if not text:
        return ""
    for needle, repl in roots:
        if repl:
            text = text.replace(needle, repl)
        else:
            text = text.replace(needle + "/", "").replace(needle + "\\", "").replace(needle, "")
    return text


def _clean(text: Optional[str], roots: list[tuple[str, str]], limit: int = 0) -> str:
    out = redact(_strip(text, roots)) or ""
    if limit > 0 and len(out) > limit:
        out = out[:limit] + TRUNCATED
    return out


def _secret_values(text: str) -> set[str]:
    """The values ``redact`` would mask in ``text`` (so replace pairs taken from the raw diff can be masked too)."""
    return {text[m.end(2):m.end()] for m in SECRET_RE.finditer(text or "")}


def _mask(side: str, secrets: set[str]) -> str:
    for s in secrets:
        if side == s or (len(side) >= 3 and side in s):
            return "***"
    return side


def _as_build_error(e: ErrorCaseRecord) -> BuildError:
    art = None
    if e.artifact:
        try:
            art = GAV.parse(e.artifact)
        except ValueError:
            art = None
    return BuildError(kind=e.error_kind, message=e.message or "", file=e.file, line=e.line, symbol=e.symbol,
                      location=e.location, module=e.module, artifact=art, raw=e.raw or "", signature=e.signature,
                      signature_generic=e.signature_generic)


def _capped(text: str, limit: int) -> tuple[str, bool]:
    if limit > 0 and len(text) > limit:
        return text[:limit] + "\n" + TRUNCATED, True
    return text, False


# --------------------------------------------------------------------------- prepare
def _base_update(rec: Record, base: dict[str, Any]) -> dict[str, Any]:
    """Project fields the tracker left empty (it does not know the profile)."""
    upd = {k: v for k, v in base.items() if k != "case_type" and v and not getattr(rec, k)}
    if base.get("case_type") is not None and rec.case_type == CaseType.UNKNOWN:
        upd["case_type"] = base["case_type"]
    return upd


def _prepare_attempt(a: FixAttemptRecord, base: dict[str, Any], roots: list[tuple[str, str]], ws: Any,
                     errors_by_id: dict[str, ErrorCaseRecord], diff_cap: int) -> tuple[FixAttemptRecord, str]:
    """(sanitized record, full redacted diff for the error excerpts)."""
    upd = _base_update(a, base)
    stripped = _strip(a.diff or "", roots)                  # unredacted: redaction would hide real token changes
    if stripped.strip():
        change = classify_diff(stripped)
        secrets = _secret_values(stripped)
        pairs = []
        for rp in change.replace_pairs:
            pairs.append(rp.model_copy(update={
                "old": _mask(rp.old, secrets), "new": _mask(rp.new, secrets),
                "files": [_rel_posix(f, ws) or f for f in rp.files]}))
        upd["change"] = change.model_copy(update={"replace_pairs": pairs,
                                                  "files": [_rel_posix(f, ws) or f for f in change.files]})
        if not a.fix_labels:
            try:
                errs = [_as_build_error(errors_by_id[i]) for i in a.error_ids if i in errors_by_id]
                lab = label_changes(file_changes_from_diff(stripped), errs)
                upd["fix_labels"] = list(lab.labels)
                if not a.replacement and lab.replacement:
                    upd["replacement"] = redact(lab.replacement) or ""
            except Exception as e:  # pragma: no cover - label_changes never raises
                log.debug("error cases: labels of %s failed: %s", a.id, e)
    full = redact(stripped) or ""
    diff, cut = _capped(full, diff_cap)
    upd.update({
        "diff": diff,
        "diff_truncated": bool(a.diff_truncated or cut),
        "description": _clean(a.description, roots, DESCRIPTION_LIMIT),
        "failure": _clean(a.failure, roots, FAILURE_LIMIT),
        "replacement": redact(upd.get("replacement", a.replacement)) or "",
        "introduced_errors": [_clean(x, roots, ITEM_LIMIT) for x in a.introduced_errors[:LIST_LIMIT]],
        "remaining_errors": [_clean(x, roots, ITEM_LIMIT) for x in a.remaining_errors[:LIST_LIMIT]],
        "notes": [_clean(x, roots, ITEM_LIMIT) for x in a.notes],
    })
    return a.model_copy(update=upd), full


def _case_id(signature_generic: str) -> str:
    from .catalog import case_id_for          # the catalog imports this module

    return case_id_for(signature_generic)


def _prepare_error(e: ErrorCaseRecord, base: dict[str, Any], roots: list[tuple[str, str]], ws: Any,
                   attempts: dict[str, FixAttemptRecord], full_diffs: dict[str, str],
                   excerpt_cap: int) -> ErrorCaseRecord:
    upd = _base_update(e, base)
    upd["case_id"] = e.case_id or _case_id(e.signature_generic)     # the case catalog key (one case per L2)
    raw = _clean(e.raw, roots)
    if len(raw) > RAW_LIMIT:
        raw = raw[:RAW_LIMIT] + TRUNCATED
    file = _rel_posix(e.file, ws)
    upd.update({
        "file": file,
        "message": _clean(e.message, roots, FAILURE_LIMIT),
        "location": _clean(e.location, roots) if e.location else e.location,
        "raw": raw,
        "description": _clean(e.description, roots, DESCRIPTION_LIMIT),
        "failure_reason": _clean(e.failure_reason, roots, FAILURE_LIMIT),
        "problem_errors": [_clean(x, roots, ITEM_LIMIT) for x in e.problem_errors[:LIST_LIMIT]],
        "suggestion": _clean(e.suggestion, roots, FAILURE_LIMIT),
        "notes": [_clean(x, roots, ITEM_LIMIT) for x in e.notes],
    })
    if not e.error_symbol:
        upd["error_symbol"] = symbol_for_errors([_as_build_error(e)])
    refs = []
    for ref in e.attempts:
        att = attempts.get(ref.attempt_id)
        r_upd: dict[str, Any] = {"failure": _clean(ref.failure or (att.failure if att else ""), roots, FAILURE_LIMIT)}
        if att is not None and ref.change_kind == "none":
            r_upd["change_kind"] = att.change.change_kind
        if att is not None and not ref.resolver_id:
            r_upd["resolver_id"] = att.resolver_id
        refs.append(ref.model_copy(update=r_upd))
    upd["attempts"] = refs
    if refs:
        upd["attempt_count"] = len(refs)
        upd["llm_attempts"] = sum(1 for r in refs if r.fix_method == "llm")

    att = attempts.get(e.resolved_by or "") if e.status in RESOLVED else None
    if att is not None:
        ch = att.change
        own = [p for p in ch.replace_pairs if file and any(path_matches(f, file) for f in p.files)]
        full = full_diffs.get(att.id, att.diff or "")
        # an error without a file (pom / dependency errors): the head of the resolving diff
        excerpt = diff_for_file(full, file) if file else full
        excerpt, _ = _capped(excerpt, excerpt_cap)
        method = e.fix_method if e.fix_method != "none" else att.fix_method
        upd.update({
            "fix_method": method,
            "resolver_id": e.resolver_id or att.resolver_id,
            "description": upd["description"] or att.description,
            "change_kind": ch.change_kind,
            "replace_pairs": own or list(ch.replace_pairs),
            "fix_labels": list(att.fix_labels),
            "diff_excerpt": excerpt,
            # only a targeted / own-file resolution makes "this L2 is fixed by replacing a → b" a rule candidate
            "replace_rule_candidate": e.status == "resolved" and method == "llm" and ch.change_kind == "text_replace",
        })
    else:
        upd["replace_rule_candidate"] = False
    return e.model_copy(update=upd)


def prepare_error_cases(errors: Iterable[ErrorCaseRecord], attempts: Iterable[FixAttemptRecord],
                        profile: Optional[ProjectProfile] = None, ws: Any = None,
                        cfg: Any = None) -> tuple[list[ErrorCaseRecord], list[FixAttemptRecord]]:
    """Raw tracker records → KB records (see the module doc). ``profile`` / ``ws`` / ``cfg`` may be None (the
    fields that need them are left as they are). Never raises: a record that fails is kept as given."""
    errors = [e for e in (errors or []) if e is not None]
    attempts = [a for a in (attempts or []) if a is not None]
    try:
        kbc = getattr(cfg, "kb", None)
        diff_cap = int(getattr(kbc, "error_diff_chars", 12000))
        excerpt_cap = int(getattr(kbc, "error_excerpt_chars", 1500))
        roots = _roots(ws)
        base: dict[str, Any] = {
            "project": project_name(profile, ws) if (profile is not None or ws is not None) else "",
            "project_fingerprint": profile.fingerprint if profile is not None else "",
            "run_id": getattr(ws, "run_id", "") or "",
        }
        if profile is not None:
            base["case_type"] = profile.case_type
        errors_by_id = {e.id: e for e in errors}

        out_attempts: list[FixAttemptRecord] = []
        full_diffs: dict[str, str] = {}
        for a in attempts:
            try:
                pa, full = _prepare_attempt(a, base, roots, ws, errors_by_id, diff_cap)
                full_diffs[pa.id] = full
                out_attempts.append(pa)
            except Exception as ex:
                log.warning("error cases: preparing attempt %s failed: %s: %s", a.id, type(ex).__name__, ex)
                out_attempts.append(a)
        by_id: dict[str, FixAttemptRecord] = {}
        for a in out_attempts:
            by_id.setdefault(a.id, a)
            if a.attempt_id:
                by_id.setdefault(a.attempt_id, a)

        out_errors: list[ErrorCaseRecord] = []
        for e in errors:
            try:
                out_errors.append(_prepare_error(e, base, roots, ws, by_id, full_diffs, excerpt_cap))
            except Exception as ex:
                log.warning("error cases: preparing error %s failed: %s: %s", e.id, type(ex).__name__, ex)
                out_errors.append(e if e.case_id else e.model_copy(update={"case_id": _case_id(e.signature_generic)}))
        return out_errors, out_attempts
    except Exception as ex:  # pragma: no cover - defensive
        log.warning("error cases: prepare failed: %s: %s", type(ex).__name__, ex)
        return errors, attempts


# --------------------------------------------------------------------------- run-dir files
def _dump(r: Any) -> str:
    if isinstance(r, BaseModel):
        return r.model_dump_json()
    return json.dumps(r, ensure_ascii=False, default=str)


def write_error_cases_jsonl(records: Iterable[Any], path: Union[str, Path]) -> Path:
    """One JSON object per line (ErrorCaseRecord or FixAttemptRecord alike); replaces the file atomically."""
    from .store import atomic_write

    path = Path(path)
    atomic_write(path, "".join(_dump(r) + "\n" for r in records))
    return path


def _cell(v: Any, limit: int = CSV_CELL_LIMIT) -> str:
    if v is None:
        s = ""
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif hasattr(v, "value"):
        s = str(v.value)
    else:
        s = str(v)
    if limit > 0 and len(s) > limit:
        s = s[:limit] + TRUNCATED
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s                                          # Excel would evaluate it as a formula
    return s


def pairs_text(pairs: Iterable[Any]) -> str:
    return "; ".join(f"{p.old}→{p.new}" + (f" ×{p.count}" if getattr(p, "count", 1) > 1 else "") for p in pairs)


def _short_attempt(attempt_id: str, run_id: str) -> str:
    return attempt_id[len(run_id) + 1:] if run_id and attempt_id.startswith(run_id + ":") else attempt_id


def _write_csv(path: Union[str, Path], columns: list[str], rows: Iterable[list[str]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:   # BOM: Excel reads it as UTF-8 (Korean text)
        w = csv.writer(fh)
        w.writerow(columns)
        for r in rows:
            w.writerow(r)
    tmp.replace(path)
    return path


def error_csv_row(e: ErrorCaseRecord) -> list[str]:
    attempts = "; ".join(f"{_short_attempt(r.attempt_id, e.run_id)}:{r.fix_method}:{r.outcome}" for r in e.attempts)
    values = {
        "id": e.id, "project": e.project, "run_id": e.run_id, "status": e.status, "error_kind": e.error_kind,
        "file": e.file, "line": e.line, "message": e.message, "symbol": e.error_symbol or e.symbol,
        "signature_generic": e.signature_generic, "case_id": e.case_id or _case_id(e.signature_generic),
        "first_seen": e.first_seen, "resolved_at": e.resolved_at,
        "fix_method": e.fix_method, "resolver_id": e.resolver_id, "change_kind": e.change_kind,
        "replace_pairs": pairs_text(e.replace_pairs), "replace_rule_candidate": e.replace_rule_candidate,
        "description": e.description, "failure_reason": e.failure_reason,
        "problem_errors": "\n".join(e.problem_errors), "block_reason": e.block_reason, "suggestion": e.suggestion,
        "attempt_count": e.attempt_count, "llm_attempts": e.llm_attempts, "attempts": attempts,
    }
    return [_cell(values[c]) for c in ERROR_CSV_COLUMNS]


def write_error_cases_csv(errors: Iterable[ErrorCaseRecord], path: Union[str, Path]) -> Path:
    """Excel copy of the error records: one row per error, UTF-8 with BOM, multi-line cells kept but capped."""
    return _write_csv(path, ERROR_CSV_COLUMNS, (error_csv_row(e) for e in errors))


def write_fix_attempts_csv(attempts: Iterable[FixAttemptRecord], path: Union[str, Path]) -> Path:
    """Excel copy of the attempt records (one row per attempt, the diff in the last column)."""
    def row(a: FixAttemptRecord) -> list[str]:
        values = {
            "id": a.id, "project": a.project, "run_id": a.run_id, "phase": a.phase, "iteration": a.iteration,
            "attempt_id": a.attempt_id, "cluster_id": a.cluster_id, "fix_method": a.fix_method,
            "resolver_id": a.resolver_id, "model": a.model, "outcome": a.outcome, "applied": a.applied,
            "change_kind": a.change.change_kind, "replace_pairs": pairs_text(a.change.replace_pairs),
            "fix_labels": ", ".join(a.fix_labels), "replacement": a.replacement, "description": a.description,
            "failure": a.failure, "introduced_errors": "\n".join(a.introduced_errors),
            "remaining_errors": "\n".join(a.remaining_errors), "files": "\n".join(a.change.files),
            "lines_added": a.change.lines_added, "lines_removed": a.change.lines_removed,
            "error_ids": ", ".join(a.error_ids), "llm_calls": a.llm_calls, "diff_truncated": a.diff_truncated,
            "diff": a.diff,
        }
        return [_cell(values[c], CSV_DIFF_LIMIT if c == "diff" else CSV_CELL_LIMIT) for c in ATTEMPT_CSV_COLUMNS]
    return _write_csv(path, ATTEMPT_CSV_COLUMNS, (row(a) for a in attempts))


# --------------------------------------------------------------------------- hints index entries
_DIGITS_RE = re.compile(r"\d+")
_PATHISH_RE = re.compile(r"(?:[\w.\-]+[/\\])+([\w.\-]+)")


def _norm_failure(text: str) -> str:
    """Grouping key of a failure text: paths → basenames, numbers → #, whitespace collapsed, lowercase."""
    t = _PATHISH_RE.sub(r"\1", text or "")
    t = _DIGITS_RE.sub("#", t)
    return " ".join(t.lower().split())[:160]


# the tracker's description prefixes repeat what the hint line already says ("fixed by llm <model>")
_WHO_PREFIX_RE = re.compile(r"^(?:LLM [^:\n]{0,80}|handler `[^`\n]{0,80}`): ")


def _short(text: str, limit: int = HINT_TEXT_LIMIT) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _new_entry() -> dict[str, Any]:
    return {"seen": 0, "projects": [], "resolved": {}, "unresolved": 0, "blocked": 0, "fixes": [], "failures": [],
            "variants": {"fixes": [], "failures": []}}


def _project_key(e: ErrorCaseRecord) -> str:
    return e.project_fingerprint or e.project


def _add_to_entry(ent: dict[str, Any], e: ErrorCaseRecord) -> None:
    pk = _project_key(e)
    ent["seen"] += 1
    if pk and pk not in ent["projects"] and len(ent["projects"]) < HINT_PROJECTS:
        ent["projects"].append(pk)
    if e.status in RESOLVED:
        ent["resolved"][e.fix_method] = ent["resolved"].get(e.fix_method, 0) + 1
    elif e.status == "unresolved":
        ent["unresolved"] += 1
    elif e.status == "blocked":
        ent["blocked"] += 1
    variants = ent.setdefault("variants", {"fixes": [], "failures": []})
    if e.status == "resolved" and e.fix_method != "none":
        pairs = [{"old": p.old, "new": p.new} for p in e.replace_pairs[:HINT_TOP]]
        key = [e.fix_method, e.resolver_id, e.change_kind, [[p["old"], p["new"]] for p in pairs]]
        v = next((x for x in variants["fixes"] if x["key"] == key), None)
        if v is None and len(variants["fixes"]) < HINT_VARIANTS:
            v = {"key": key, "method": e.fix_method, "resolver": e.resolver_id, "change_kind": e.change_kind,
                 "description": _short(_WHO_PREFIX_RE.sub("", e.description or "")), "replace_pairs": pairs,
                 "count": 0, "projects": 0,
                 "project_keys": []}
            variants["fixes"].append(v)
        if v is not None:
            v["count"] += 1
            if pk and pk not in v["project_keys"] and len(v["project_keys"]) < HINT_PROJECTS:
                v["project_keys"].append(pk)
            v["projects"] = len(v["project_keys"])
    for ref in e.attempts:
        if ref.outcome not in FAILED_OUTCOMES:
            continue
        key = [ref.fix_method, ref.outcome, _norm_failure(ref.failure)]
        v = next((x for x in variants["failures"] if x["key"] == key), None)
        if v is None and len(variants["failures"]) < HINT_VARIANTS:
            v = {"key": key, "method": ref.fix_method, "outcome": ref.outcome, "failure": _short(ref.failure),
                 "count": 0}
            variants["failures"].append(v)
        if v is not None:
            v["count"] += 1
    # stable sorts: ties keep the first-seen order, identical in incremental updates and rebuilds
    fixes = sorted(variants["fixes"], key=lambda x: (-x["count"], -x["projects"]))
    ent["fixes"] = [{k: x[k] for k in ("method", "resolver", "change_kind", "description", "replace_pairs", "count",
                                       "projects")} for x in fixes[:HINT_TOP]]
    failures = sorted(variants["failures"], key=lambda x: -x["count"])
    ent["failures"] = [{k: x[k] for k in ("method", "outcome", "failure", "count")} for x in failures[:HINT_TOP]]


def _empty_hints() -> dict[str, Any]:
    return {"version": HINTS_VERSION, "updated_at": "", "by_generic": {}, "by_symbol": {}}


def _index_error(data: dict[str, Any], e: ErrorCaseRecord) -> None:
    if e.signature_generic:
        _add_to_entry(data["by_generic"].setdefault(e.signature_generic, _new_entry()), e)
    if e.error_symbol:
        _add_to_entry(data["by_symbol"].setdefault(e.error_symbol, _new_entry()), e)


def _entry_lines(key: str, ent: dict[str, Any]) -> list[tuple[str, str]]:
    """(prefix, body) of at most two hint lines: the most frequent past fix and the most frequent past failure."""
    out: list[tuple[str, str]] = []
    prefix = f"[error {key}] "
    fixes = ent.get("fixes") or []
    if fixes:
        f = fixes[0]
        who = " ".join(x for x in (f.get("method"), f.get("resolver")) if x)
        body = f"past runs: fixed by {who} in {f.get('projects', 0)} project(s)"
        if f.get("description"):
            body += f": {f['description']}"
        if f.get("replace_pairs"):
            body += "; replace " + ", ".join(f"`{p['old']}` → `{p['new']}`" for p in f["replace_pairs"])
        out.append((prefix, body))
    failures = ent.get("failures") or []
    if failures:
        x = failures[0]
        body = f"past failure ×{x.get('count', 0)}: {x.get('method', '')} {x.get('outcome', '')}"
        if x.get("failure"):
            body += f" — {x['failure']}"
        out.append((prefix, body + "; do not repeat it"))
    return out


# --------------------------------------------------------------------------- store
def _rx(v: Optional[str]) -> Optional[re.Pattern[str]]:
    return re.compile(v) if v else None


def _stamp(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


_ID_RE = re.compile(r'^\{"id":"((?:[^"\\]|\\.)*)"')


class ErrorCaseStore:
    """``kb/errors.jsonl`` + ``kb/fix_attempts.jsonl`` (append-only) and ``kb/error_hints.json`` (derived)."""

    def __init__(self, kb_dir: Union[str, Path]):
        self.root = Path(kb_dir)
        self._ids: dict[str, tuple[Optional[tuple[int, int]], set[str]]] = {}
        self._hints: Optional[dict[str, Any]] = None
        self._hints_stamp: Optional[tuple[int, int]] = None

    @property
    def errors_path(self) -> Path:
        return self.root / "errors.jsonl"

    @property
    def attempts_path(self) -> Path:
        return self.root / "fix_attempts.jsonl"

    @property
    def hints_path(self) -> Path:
        return self.root / "error_hints.json"

    # ------------------------------------------------------------------ reading
    @staticmethod
    def _iter_file(path: Path, model: type) -> Iterator[Any]:
        if not path.exists():
            return
        seen: set[str] = set()
        try:
            fh = open(path, "r", encoding="utf-8")
        except OSError as e:
            log.warning("cannot read %s: %s", path, e)
            return
        with fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = model.model_validate_json(line)
                except ValueError as e:
                    log.warning("skipping unreadable line %s:%d: %s", path, n, str(e).splitlines()[0])
                    continue
                if r.id in seen:
                    continue
                seen.add(r.id)
                yield r

    def iter_errors(self, *, status: Optional[str] = None, method: Optional[str] = None,
                    change_kind: Optional[str] = None, project: Optional[str] = None, symbol: Optional[str] = None,
                    signature: Optional[str] = None, run_id: Optional[str] = None,
                    run_ids: Optional[Iterable[str]] = None) -> Iterator[ErrorCaseRecord]:
        """Error records matching every given regex (``re.search``); ``run_id`` is exact, ``run_ids`` a set."""
        r_status, r_method, r_change = _rx(status), _rx(method), _rx(change_kind)
        r_project, r_symbol, r_sig = _rx(project), _rx(symbol), _rx(signature)
        wanted = set(run_ids) if run_ids is not None else None
        for e in self._iter_file(self.errors_path, ErrorCaseRecord):
            if run_id is not None and e.run_id != run_id:
                continue
            if wanted is not None and e.run_id not in wanted:
                continue
            if r_status and not r_status.search(e.status):
                continue
            if r_method and not r_method.search(e.fix_method):
                continue
            if r_change and not r_change.search(e.change_kind):
                continue
            if r_project and not (r_project.search(e.project) or r_project.search(e.project_fingerprint)):
                continue
            if r_symbol and not (r_symbol.search(e.error_symbol) or r_symbol.search(e.symbol or "")):
                continue
            if r_sig and not (r_sig.search(e.signature) or r_sig.search(e.signature_generic)):
                continue
            yield e

    def iter_attempts(self, *, method: Optional[str] = None, outcome: Optional[str] = None,
                      change_kind: Optional[str] = None, project: Optional[str] = None,
                      run_id: Optional[str] = None, ids: Optional[Iterable[str]] = None) -> Iterator[FixAttemptRecord]:
        """Attempt records matching every given regex; ``run_id`` exact, ``ids`` a set of FixAttemptRecord ids."""
        r_method, r_outcome, r_change, r_project = _rx(method), _rx(outcome), _rx(change_kind), _rx(project)
        wanted = set(ids) if ids is not None else None
        for a in self._iter_file(self.attempts_path, FixAttemptRecord):
            if wanted is not None and a.id not in wanted:
                continue
            if run_id is not None and a.run_id != run_id:
                continue
            if r_method and not r_method.search(a.fix_method):
                continue
            if r_outcome and not r_outcome.search(a.outcome):
                continue
            if r_change and not r_change.search(a.change.change_kind):
                continue
            if r_project and not (r_project.search(a.project) or r_project.search(a.project_fingerprint)):
                continue
            yield a

    def get_error(self, error_id: str) -> Optional[ErrorCaseRecord]:
        return next((e for e in self.iter_errors() if e.id == error_id), None)

    def get_attempt(self, attempt_id: str) -> Optional[FixAttemptRecord]:
        return next((a for a in self.iter_attempts() if a.id == attempt_id), None)

    def attempts_of(self, error: ErrorCaseRecord) -> list[FixAttemptRecord]:
        """The attempts of an error in its history order (+ the resolving one, + any that list it in error_ids)."""
        order = [r.attempt_id for r in error.attempts]
        if error.resolved_by and error.resolved_by not in order and not error.resolved_by.startswith("step:"):
            order.append(error.resolved_by)
        found: dict[str, FixAttemptRecord] = {}
        extra: list[FixAttemptRecord] = []
        for a in self.iter_attempts(run_id=error.run_id or None):
            if a.id in order:
                found[a.id] = a
            elif error.id in a.error_ids:
                extra.append(a)
        return [found[i] for i in order if i in found] + extra

    def _count(self, path: Path) -> int:
        """Cheap line count (no parsing)."""
        try:
            n, last = 0, b"\n"
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    n += chunk.count(b"\n")
                    last = chunk[-1:]
            return n + (0 if last == b"\n" else 1)
        except OSError:
            return 0

    def count_errors(self) -> int:
        return self._count(self.errors_path)

    def count_attempts(self) -> int:
        return self._count(self.attempts_path)

    # ------------------------------------------------------------------ writing
    def _known_ids(self, path: Path) -> set[str]:
        stamp = _stamp(path)
        cached = self._ids.get(str(path))
        if cached is not None and cached[0] == stamp:
            return cached[1]
        ids: set[str] = set()
        if stamp is not None:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    m = _ID_RE.match(line)
                    if m:
                        ids.add(json.loads(f'"{m.group(1)}"'))
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and obj.get("id"):
                            ids.add(str(obj["id"]))
                    except ValueError:
                        continue
        self._ids[str(path)] = (stamp, ids)
        return ids

    def _append_lines(self, path: Path, records: list[Any]) -> None:
        if not records:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        known = self._known_ids(path)
        with open(path, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(r.model_dump_json() + "\n")
            fh.flush()
        known.update(r.id for r in records)
        self._ids[str(path)] = (_stamp(path), known)

    def append_counts(self, errors: Iterable[ErrorCaseRecord],
                      attempts: Iterable[FixAttemptRecord]) -> tuple[int, int]:
        """Append records whose id is not stored yet; update the hints index. Returns (errors, attempts) added."""
        had_errors = bool(self._stamp_size(self.errors_path))
        known_e, known_a = self._known_ids(self.errors_path), self._known_ids(self.attempts_path)
        def fresh(records: Iterable[Any], known: set[str]) -> list[Any]:
            out, ids = [], set(known)
            for r in records or []:
                if r is not None and r.id not in ids:
                    ids.add(r.id)
                    out.append(r)
            return out

        new_e: list[ErrorCaseRecord] = fresh(errors, known_e)
        new_a: list[FixAttemptRecord] = fresh(attempts, known_a)
        self._append_lines(self.errors_path, new_e)
        self._append_lines(self.attempts_path, new_a)
        if new_e:
            try:
                data = self._load_hints_file()
                if data is None or (had_errors and not self.hints_path.exists()):
                    self.rebuild_hints()             # missing / unreadable index over existing records
                else:
                    for e in new_e:
                        _index_error(data, e)
                    self._save_hints(data)
            except Exception as ex:
                log.warning("error hints update failed: %s: %s", type(ex).__name__, ex)
        return len(new_e), len(new_a)

    def append(self, errors: Iterable[ErrorCaseRecord], attempts: Iterable[FixAttemptRecord]) -> int:
        """Append-only (ids already stored are skipped); returns the number of new records (errors + attempts)."""
        ne, na = self.append_counts(errors, attempts)
        return ne + na

    @staticmethod
    def _stamp_size(path: Path) -> int:
        st = _stamp(path)
        return st[1] if st else 0

    # ------------------------------------------------------------------ hints index
    def _load_hints_file(self) -> Optional[dict[str, Any]]:
        """The stored index, an empty one when there is none, None when it is unreadable / of another version."""
        if not self.hints_path.exists():
            return _empty_hints()
        try:
            data = json.loads(self.hints_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("error hints index unreadable (%s); rebuilding", e)
            return None
        if not isinstance(data, dict) or data.get("version") != HINTS_VERSION:
            log.warning("error hints index %s has an unknown version; rebuilding", self.hints_path)
            return None
        data.setdefault("by_generic", {})
        data.setdefault("by_symbol", {})
        return data

    def _save_hints(self, data: dict[str, Any]) -> None:
        from .store import atomic_write

        data["updated_at"] = utcnow().isoformat(timespec="seconds")
        data["by_generic"] = dict(sorted(data["by_generic"].items()))
        data["by_symbol"] = dict(sorted(data["by_symbol"].items()))
        atomic_write(self.hints_path, json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        self._hints, self._hints_stamp = data, _stamp(self.hints_path)

    def rebuild_hints(self) -> int:
        """Rebuild ``error_hints.json`` from ``errors.jsonl``; returns the number of keys (L2 + symbol)."""
        data = _empty_hints()
        for e in self.iter_errors():
            _index_error(data, e)
        self._save_hints(data)
        return len(data["by_generic"]) + len(data["by_symbol"])

    def hints_index(self) -> dict[str, Any]:
        """The hints index, loaded lazily and reloaded only when the file changes (a fleet runs 500 projects in
        one process). An unreadable index reads as empty here (``rebuild_hints`` / the next append repairs it)."""
        stamp = _stamp(self.hints_path)
        if stamp is None:
            return _empty_hints()
        if self._hints is not None and self._hints_stamp == stamp:
            return self._hints
        data = self._load_hints_file() or _empty_hints()
        self._hints, self._hints_stamp = data, stamp
        return data

    def hints_for(self, signature_generic: Optional[str] = None, symbol: Optional[str] = None) -> list[str]:
        """At most two lines per key (past fix, past failure) for the L2 signature and the error symbol."""
        return self.hints_for_keys([signature_generic] if signature_generic else [], [symbol] if symbol else [])

    def hints_for_keys(self, signatures: Iterable[str], symbols: Iterable[str]) -> list[str]:
        """:meth:`hints_for` over several L2 signatures / symbols (a cluster); a line whose text another key
        already gave (the L2 and the symbol of one error usually share their history) is left out."""
        idx = self.hints_index()
        out: list[str] = []
        bodies: set[str] = set()
        keys = [("by_generic", k) for k in signatures if k] + [("by_symbol", k) for k in symbols if k]
        for table, key in keys:
            ent = idx.get(table, {}).get(key)
            if not ent:
                continue
            for prefix, body in _entry_lines(key, ent):
                if body not in bodies:
                    bodies.add(body)
                    out.append(prefix + body)
        return out

    # ------------------------------------------------------------------ aggregates
    @staticmethod
    def _tried_method(e: ErrorCaseRecord) -> str:
        """Resolving method, or for an error not resolved the method of its last attempt (none = never tried)."""
        if e.status in RESOLVED:
            return e.fix_method
        return e.attempts[-1].fix_method if e.attempts else "none"

    def summary(self, run_ids: Optional[Iterable[str]] = None, top: int = 15) -> dict[str, Any]:
        """Counts by status / fix method / change kind, status × method, top failure reasons by L2, top
        unresolved L2s, attempt outcomes per method, LLM text-replace count and the top rule candidates."""
        wanted = list(run_ids) if run_ids is not None else None
        errs = list(self.iter_errors(run_ids=wanted))
        resolved = [e for e in errs if e.status in RESOLVED]
        matrix: dict[str, dict[str, int]] = {}
        for e in errs:
            row = matrix.setdefault(e.status, {})
            m = self._tried_method(e)
            row[m] = row.get(m, 0) + 1

        reasons: dict[tuple[str, str], dict[str, Any]] = {}
        unresolved: dict[str, dict[str, Any]] = {}
        outcomes: dict[str, Counter] = {}
        seen_attempts: set[str] = set()
        for e in errs:
            for r in e.attempts:
                if r.attempt_id in seen_attempts:
                    continue
                seen_attempts.add(r.attempt_id)
                outcomes.setdefault(r.fix_method, Counter())[r.outcome] += 1
            if e.status not in NOT_RESOLVED:
                continue
            if e.failure_reason:
                d = reasons.setdefault((e.signature_generic, _norm_failure(e.failure_reason)), {
                    "signature_generic": e.signature_generic, "reason": _short(e.failure_reason), "count": 0,
                    "projects": set()})
                d["count"] += 1
                d["projects"].add(_project_key(e))
            if e.status == "not_visible":
                continue
            key = e.signature_generic or e.signature or e.message[:80]
            u = unresolved.setdefault(key, {"signature_generic": key, "error_kind": e.error_kind.value,
                                            "symbol": e.error_symbol, "count": 0, "unresolved": 0, "blocked": 0,
                                            "projects": set()})
            u["count"] += 1
            u[e.status] += 1
            u["projects"].add(_project_key(e))
        for e in resolved:
            for r in e.attempts:
                seen_attempts.add(r.attempt_id)
            if e.resolved_by and not e.resolved_by.startswith("step:"):
                seen_attempts.add(e.resolved_by)

        def rows(d: dict, k: str = "count") -> list[dict[str, Any]]:
            out = [{**v, "projects": len(v["projects"])} for v in d.values()]
            out.sort(key=lambda x: (-x["projects"], -x[k], x["signature_generic"]))
            return out[:top]

        return {
            "errors": len(errs),
            "attempts": len(seen_attempts),
            "projects": len({_project_key(e) for e in errs if _project_key(e)}),
            "runs": len({e.run_id for e in errs if e.run_id}),
            "by_status": dict(Counter(e.status for e in errs).most_common()),
            "by_method": dict(Counter(e.fix_method for e in resolved).most_common()),
            "by_change_kind": dict(Counter(e.change_kind for e in resolved).most_common()),
            "status_method": matrix,
            "attempt_outcomes": {m: dict(c.most_common()) for m, c in sorted(outcomes.items())},
            "top_failure_reasons": rows(reasons),
            "top_unresolved": rows(unresolved),
            "llm_text_replace": sum(1 for e in errs if e.replace_rule_candidate),
            "replace_candidates": self.replace_candidates(run_ids=wanted, _errors=errs)[:top],
        }

    def replace_candidates(self, min_projects: int = 1, run_ids: Optional[Iterable[str]] = None,
                           _errors: Optional[list[ErrorCaseRecord]] = None) -> list[dict[str, Any]]:
        """LLM fixes that were only text replaces, grouped by (L2 signature, old, new): rule / command candidates,
        most projects first."""
        errs = _errors if _errors is not None else self.iter_errors(run_ids=run_ids)
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for e in errs:
            if not e.replace_rule_candidate:
                continue
            for p in e.replace_pairs:
                g = groups.setdefault((e.signature_generic, p.old, p.new), {
                    "signature_generic": e.signature_generic, "old": p.old, "new": p.new,
                    "error_kind": e.error_kind.value, "error_symbol": e.error_symbol, "count": 0, "projects": set(),
                    "project_names": [], "example_file": e.file or "", "example_attempt": e.resolved_by or "",
                    "example_error": e.id, "fix_labels": list(e.fix_labels)})
                g["count"] += 1
                pk = _project_key(e)
                if pk:
                    g["projects"].add(pk)
                if e.project and e.project not in g["project_names"] and len(g["project_names"]) < 10:
                    g["project_names"].append(e.project)
        out = [{**g, "projects": len(g["projects"])} for g in groups.values()]
        if min_projects > 1:
            out = [g for g in out if g["projects"] >= min_projects]
        out.sort(key=lambda g: (-g["projects"], -g["count"], g["signature_generic"], g["old"], g["new"]))
        return out
