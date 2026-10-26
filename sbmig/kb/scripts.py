"""v2 fix scripts per case (flow-v2, docs/v2/PLAN-v2.md §3.4): ``<kb>/scripts/<case_id>/v<N>.py`` + ``v<N>.meta.json``.

Every build-error fix of the v2 loop is a Python script ``def fix(p)`` the LLM wrote for ONE case (the L2 signature,
``case_id = sha1(L2)[:10]``, the key of the case catalog). The store keeps every version a case ever got — new ones
and augments (``parent`` = the version it extends) — each with its record (:class:`ScriptMeta`): who made it (run /
project / model), the LLM's analysis, the successes / failures / partials of every run that tried it and the
projects it fixed. The loop asks :meth:`ScriptStore.best` for reuse candidates (no LLM call), :meth:`ScriptStore.latest`
for the base of an augment, stores every LLM script before it runs (:meth:`ScriptStore.add`) and credits the outcome
after the scoring build (:meth:`ScriptStore.credit`). See docs/INTERFACES.md "v2 — flow-v2" → "Script KB (K)".

Rules:

* ``v<N>.py`` holds the exact code that runs (UTF-8 bytes, newlines as given — no text-mode translation on Windows),
  so ``ScriptMeta.sha1`` is the sha1 of that file; an identical script (same sha1) is stored once: ``add`` returns the
  existing version and writes nothing.
* The analysis is masked with the KB redaction (:mod:`sbmig.kb.redact`, ``kb.redact_file``); a script whose CODE
  matches a secret pattern is stored masked as ``quarantined`` — never returned by ``best`` / ``latest`` (the masked
  code is not the code that ran; the note says why), and a secret never lands in the KB.
* Selection: ``best`` = active versions with at least one success, the most success projects first, then
  successes − failures, then the newest; a version pinned in the case's ``human`` block (``action: {kind: script,
  script_version: N}``, ``sbmig kb cases pin <id> --action script:N``) comes first when it exists and is
  active (not retired / quarantined), even without a success; a case a person rejected gives nothing to reuse. ``latest`` = the newest active
  version (the augment base).
* ``retire`` / ``activate`` are people's decisions (``sbmig kb scripts retire|activate``); a quarantined version stays
  quarantined.
* Every write runs under the KB lock (``<kb>/.lock``, :mod:`sbmig.kb.lock`) and is atomic (temp file + ``os.replace``,
  retried briefly when Windows still holds the target open); created / quarantined / credited / retired / activated
  versions are lines of ``<kb>/audit.jsonl`` (``event: script``, the envelope of the catalog's audit lines).
* A meta file that is missing, does not parse or names another case / version is skipped with a warning — it never
  breaks a run; version numbers are never reused (the next one is above every ``v<N>.*`` file on disk).

File-based on purpose (invariant 3).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union

from sbmig import __version__ as AGENT_VERSION
from sbmig.core.models import KB_RECORD_SCHEMA, ScriptMeta, utcnow

from .jsonl import append_lines
from .lock import kb_lock
from .redact import get_redactor

log = logging.getLogger("sbmig.kb")

SCRIPTS_DIR = "scripts"
AUDIT_FILE = "audit.jsonl"
SCRIPT_METHODS = ("script", "script_reuse", "script_augment")    # FixMethod values of a script attempt
SUCCESS_OUTCOMES = ("improved", "build_ok")
PARTIAL_OUTCOMES = ("partial",)
FAILURE_OUTCOMES = ("no_change", "regressed", "reverted", "apply_failed", "blocked")
# inconclusive / pending (and anything unknown) only move the last_* fields
ANALYSIS_LIMIT = 4000
NOTE_LIMIT = 500
QUARANTINE_NOTE = "quarantined:"
RESOLVER_PREFIX = "script:"

_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_FILE_RE = re.compile(r"^v([1-9][0-9]{0,8})\.(py|meta\.json)$")
_RESOLVER_RE = re.compile(r"^script:([A-Za-z0-9][A-Za-z0-9_.-]{0,79}):v([0-9]+)$")
_REPLACE_TRIES = 6


# --------------------------------------------------------------------------- pure helpers
def code_sha1(code: str) -> str:
    """sha1 of the script's UTF-8 bytes (what ``v<N>.py`` holds)."""
    return hashlib.sha1((code or "").encode("utf-8")).hexdigest()


def script_resolver(case_id: str, version: int) -> str:
    """The ``resolver_id`` of a script attempt: ``script:<case_id>:v<N>``."""
    return f"{RESOLVER_PREFIX}{case_id}:v{int(version)}"


def parse_script_resolver(resolver_id: Optional[str]) -> Optional[tuple[str, int]]:
    """(case id, version) of a ``script:<case_id>:v<N>`` resolver id; None for anything else."""
    m = _RESOLVER_RE.match((resolver_id or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def valid_case_id(case_id: Optional[str]) -> bool:
    """A case id usable as one directory name (hex ids in practice; never a path, ``..`` or a drive)."""
    return bool(case_id) and bool(_CASE_ID_RE.match(case_id)) and ".." not in case_id


def rank_proven(metas: Iterable[ScriptMeta]) -> list[ScriptMeta]:
    """Reuse candidates, best first: active versions with ≥ 1 success, ordered by distinct success projects, then
    successes − failures, then the newest version."""
    out = [m for m in metas if m.status == "active" and m.successes >= 1]
    out.sort(key=lambda m: (-len(set(m.success_projects)), -(m.successes - m.failures), -m.version))
    return out


def rank_with_pin(metas: Iterable[ScriptMeta], pinned: int = 0) -> list[ScriptMeta]:
    """:func:`rank_proven` with a person's pinned version first (when it exists and is active — it needs no
    success; a retired version is never selected, even when pinned: retiring is the later decision)."""
    metas = list(metas)
    ranked = rank_proven(metas)
    pin = next((m for m in metas if pinned and m.version == pinned and m.status == "active"), None)
    if pin is None:
        return ranked
    return [pin] + [m for m in ranked if m.version != pin.version]


def lineage_text(chain: Iterable[ScriptMeta]) -> str:
    """``v1 → v3 → v4`` (root first)."""
    return " → ".join(f"v{m.version}" for m in chain) or "-"


def _who() -> str:
    from .catalog import _who as who  # the catalog's OS-user lookup (the same name in every audit line)

    return who()


def _write_bytes(path: Path, data: bytes) -> None:
    """Atomic write of exact bytes (temp file in the directory + ``os.replace``). Windows: a reader / virus scanner
    holding the target open makes ``os.replace`` fail with PermissionError for a moment — retried with backoff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover - a file system without fsync
                pass
        delay = 0.05
        for i in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if i == _REPLACE_TRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- the store
class ScriptStore:
    """Implements :class:`sbmig.core.interfaces.ScriptStoreLike` on ``<kb>/scripts/`` (see the module doc). ``cfg``
    (optional) gives the redaction rules (``kb.redact_file``) and the lock timeout."""

    def __init__(self, kb_dir: Union[str, Path], cfg: Any = None, *, lock_timeout: Optional[float] = None):
        self.root = Path(kb_dir)
        self.cfg = cfg
        kbc = getattr(cfg, "kb", None)
        self.lock_timeout = lock_timeout if lock_timeout is not None else getattr(kbc, "lock_timeout_seconds", None)
        self._warned: dict[str, Any] = {}               # path → stat of a file already warned about

    def lock(self):
        """The cross-process KB lock (``<kb>/.lock``, re-entrant) around a write."""
        return kb_lock(self.root, self.lock_timeout)

    # ------------------------------------------------------------------ layout
    @property
    def scripts_dir(self) -> Path:
        return self.root / SCRIPTS_DIR

    @property
    def audit_path(self) -> Path:
        return self.root / AUDIT_FILE

    def case_dir(self, case_id: str) -> Path:
        """``<kb>/scripts/<case_id>``; ValueError for an id that is not one plain directory name."""
        if not valid_case_id(case_id):
            raise ValueError(f"invalid case id for a fix script: {case_id!r}")
        return self.scripts_dir / case_id

    def code_path(self, case_id: str, version: int) -> Path:
        return self.case_dir(case_id) / f"v{int(version)}.py"

    def meta_path(self, case_id: str, version: int) -> Path:
        return self.case_dir(case_id) / f"v{int(version)}.meta.json"

    # ------------------------------------------------------------------ reads
    def _warn_once(self, path: Path, msg: str, *args: Any) -> None:
        try:
            st = path.stat()
            stamp: Any = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        key = str(path)
        if self._warned.get(key, ()) != stamp:
            self._warned[key] = stamp
            log.warning(msg, *args)

    def version_numbers(self, case_id: str) -> set[int]:
        """Every N with a ``v<N>.py`` or ``v<N>.meta.json`` on disk (readable or not)."""
        if not valid_case_id(case_id):
            return set()
        d = self.scripts_dir / case_id
        out: set[int] = set()
        try:
            names = os.listdir(d)
        except OSError:
            return out
        for name in names:
            m = _FILE_RE.match(name)
            if m:
                out.add(int(m.group(1)))
        return out

    def _read_meta(self, case_id: str, version: int) -> Optional[ScriptMeta]:
        if not valid_case_id(case_id) or int(version) < 1:
            return None
        p = self.meta_path(case_id, version)
        if not p.exists():
            if self.code_path(case_id, version).exists():
                self._warn_once(p, "fix script %s v%d has no meta file (%s) — skipped", case_id, version, p.name)
            return None
        try:
            meta = ScriptMeta.model_validate_json(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            first = str(e).splitlines()[0] if str(e) else ""
            self._warn_once(p, "fix script meta %s is unreadable (%s: %s) — skipped", p, type(e).__name__, first)
            return None
        if meta.case_id != case_id or meta.version != int(version):
            self._warn_once(p, "fix script meta %s names case %s v%s — skipped", p, meta.case_id, meta.version)
            return None
        return meta

    def _read_code(self, case_id: str, version: int) -> Optional[str]:
        p = self.code_path(case_id, version)
        try:
            return p.read_bytes().decode("utf-8")
        except FileNotFoundError:
            self._warn_once(p, "fix script %s is missing — version skipped", p)
        except (OSError, UnicodeDecodeError) as e:
            self._warn_once(p, "fix script %s is unreadable (%s) — version skipped", p, type(e).__name__)
        return None

    def versions(self, case_id: str) -> list[ScriptMeta]:
        """The readable versions of a case, oldest first ([] for an unknown / invalid case id)."""
        out = []
        for n in sorted(self.version_numbers(case_id)):
            meta = self._read_meta(case_id, n)
            if meta is not None:
                out.append(meta)
        return out

    def get(self, case_id: str, version: int) -> Optional[tuple[ScriptMeta, str]]:
        """(meta, code) of one version, whatever its status; None when unknown or unreadable."""
        try:
            meta = self._read_meta(case_id, int(version))
        except (TypeError, ValueError):
            return None
        if meta is None:
            return None
        code = self._read_code(case_id, meta.version)
        return (meta, code) if code is not None else None

    def get_meta(self, case_id: str, version: int) -> Optional[ScriptMeta]:
        try:
            return self._read_meta(case_id, int(version))
        except (TypeError, ValueError):
            return None

    def _human(self, case_id: str) -> Any:
        """The case's ``human`` block in the catalog (None without one / on any failure)."""
        try:
            from .catalog import CaseCatalog  # the catalog imports this module

            return CaseCatalog(self.root, self.cfg).human_of(case_id)
        except Exception as e:  # noqa: BLE001 - the pin is a bonus; selection works without it
            log.debug("fix scripts: human block of case %s unreadable: %s", case_id, e)
            return None

    def pinned_version(self, case_id: str) -> int:
        """The version a person pinned in the case's human block (``{kind: script, script_version: N}``), 0 = none."""
        h = self._human(case_id)
        act = getattr(h, "action", None) if h is not None else None
        if act is not None and act.kind == "script" and act.script_version > 0:
            return int(act.script_version)
        return 0

    def proven(self, case_id: str) -> list[ScriptMeta]:
        """:func:`rank_proven` over the case's versions — no pin, no code (the catalog's auto action)."""
        return rank_proven(self.versions(case_id))

    def ranked(self, case_id: str) -> list[ScriptMeta]:
        """What :meth:`best` returns, as metas (no code read): the pin first, then the proven versions; [] for a
        case a person rejected."""
        h = self._human(case_id)
        if h is not None and getattr(h, "status", None) == "rejected":
            return []
        act = getattr(h, "action", None) if h is not None else None
        pin = int(act.script_version) if act is not None and act.kind == "script" and act.script_version > 0 else 0
        return rank_with_pin(self.versions(case_id), pin)

    def best(self, case_id: str, *, limit: int = 1) -> list[tuple[ScriptMeta, str]]:
        """Reuse candidates, best first (see :meth:`ranked`); a version whose code cannot be read is left out."""
        if limit <= 0:
            return []
        out: list[tuple[ScriptMeta, str]] = []
        for m in self.ranked(case_id):
            code = self._read_code(case_id, m.version)
            if code is not None:
                out.append((m, code))
                if len(out) >= limit:
                    break
        return out

    def latest(self, case_id: str) -> Optional[tuple[ScriptMeta, str]]:
        """The newest active version with readable code (the base of an augment); None when there is none."""
        for m in reversed(self.versions(case_id)):
            if m.status != "active":
                continue
            code = self._read_code(case_id, m.version)
            if code is not None:
                return m, code
        return None

    def lineage(self, case_id: str, version: int) -> list[ScriptMeta]:
        """The augment chain of a version, root first (``v1 → v3 → v4``); stops at a missing parent or a loop."""
        by_v = {m.version: m for m in self.versions(case_id)}
        chain: list[ScriptMeta] = []
        cur = by_v.get(int(version))
        while cur is not None and all(c.version != cur.version for c in chain):
            chain.append(cur)
            cur = by_v.get(cur.parent) if cur.parent else None
        return list(reversed(chain))

    def case_ids(self) -> list[str]:
        """Case ids with at least one version file, sorted."""
        try:
            names = sorted(os.listdir(self.scripts_dir))
        except OSError:
            return []
        return [n for n in names if valid_case_id(n) and (self.scripts_dir / n).is_dir() and self.version_numbers(n)]

    def counts(self) -> dict[str, int]:
        """``script_cases`` / ``script_versions`` + versions by status and the proven ones (``sbmig kb stats``)."""
        out = {"script_cases": 0, "script_versions": 0, "scripts_active": 0, "scripts_retired": 0,
               "scripts_quarantined": 0, "scripts_proven": 0}
        for cid in self.case_ids():
            metas = self.versions(cid)
            out["script_cases"] += 1
            out["script_versions"] += len(metas)
            for m in metas:
                out[f"scripts_{m.status}"] += 1
                out["scripts_proven"] += m.status == "active" and m.successes > 0
        return out

    def check(self) -> list[str]:
        """Integrity problems, one line each (``sbmig kb scripts validate``): unreadable / misnamed meta files, a
        version without its code or meta, code whose sha1 differs from the meta (edited by hand), a parent that
        does not exist, a directory that is no case id."""
        problems: list[str] = []
        try:
            names = sorted(os.listdir(self.scripts_dir))
        except OSError:
            return problems
        for name in names:
            d = self.scripts_dir / name
            if not d.is_dir():
                continue
            if not valid_case_id(name):
                problems.append(f"{SCRIPTS_DIR}/{name}: not a case id directory")
                continue
            known = set()
            for n in sorted(self.version_numbers(name)):
                rel = f"{SCRIPTS_DIR}/{name}/v{n}"
                mp, cp = self.meta_path(name, n), self.code_path(name, n)
                if not mp.exists():
                    problems.append(f"{rel}.py: no meta file")
                    continue
                try:
                    meta = ScriptMeta.model_validate_json(mp.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as e:
                    problems.append(f"{rel}.meta.json: does not parse ({type(e).__name__})")
                    continue
                if meta.case_id != name or meta.version != n:
                    problems.append(f"{rel}.meta.json: names case {meta.case_id} v{meta.version}")
                    continue
                known.add(n)
                try:
                    data = cp.read_bytes()
                except OSError:
                    problems.append(f"{rel}.py: missing")
                    continue
                if meta.sha1 and hashlib.sha1(data).hexdigest() != meta.sha1:
                    problems.append(f"{rel}.py: sha1 differs from the meta (edited by hand?)")
                if meta.parent and meta.parent not in self.version_numbers(name):
                    problems.append(f"{rel}.meta.json: parent v{meta.parent} does not exist")
        return problems

    # ------------------------------------------------------------------ writes
    def _clean(self, text: Optional[str], limit: int = 0) -> str:
        from .error_cases import _roots, _strip  # the home directory → "~", as in the error records

        red = get_redactor(self.cfg)
        out = red.redact(_strip(text or "", _roots(None))) or ""
        if limit > 0 and len(out) > limit:
            out = out[:limit] + "…[truncated]"
        return out

    def _write_meta(self, meta: ScriptMeta) -> None:
        data = (meta.model_dump_json(indent=2) + "\n").encode("utf-8")
        _write_bytes(self.meta_path(meta.case_id, meta.version), data)

    def _audit(self, action: str, meta: ScriptMeta, *, who: str = "sbmig", **extra: Any) -> None:
        """One ``event: script`` line in ``<kb>/audit.jsonl`` (same envelope as the catalog's lines). Never raises."""
        entry = {"at": utcnow().isoformat(timespec="seconds"), "schema_version": KB_RECORD_SCHEMA,
                 "agent_version": AGENT_VERSION, "event": "script", "action": action, "who": who,
                 "case_id": meta.case_id, "signature_generic": meta.signature_generic, "version": meta.version,
                 "parent": meta.parent, **{k: v for k, v in extra.items() if v not in (None, "")},
                 "after": {"status": meta.status, "successes": meta.successes, "failures": meta.failures,
                           "partials": meta.partials, "success_projects": len(meta.success_projects)}}
        try:
            with self.lock():
                append_lines(self.audit_path, [json.dumps(entry, ensure_ascii=False, default=str)])
        except OSError as e:
            log.warning("fix script audit line not written: %s", e)

    def add(self, case_id: str, signature_generic: str, code: str, *, parent: Optional[int] = None,
            analysis: str = "", model: str = "", run_id: str = "", project: str = "",
            source: Literal["llm", "human", "review"] = "llm") -> ScriptMeta:
        """Store the next version of the case's script and return its meta; an identical script (same sha1) returns
        the existing meta and stores nothing. ``parent`` ≤ 0 = none (an unstored script of this run). A script whose
        code matches a secret pattern is stored masked as ``quarantined``. ValueError for an invalid case id or an
        empty script (caller errors); a failed write is logged and gives an unstored meta (``version`` 0)."""
        if not valid_case_id(case_id):
            raise ValueError(f"invalid case id for a fix script: {case_id!r}")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"empty fix script for case {case_id}")
        masked, values = get_redactor(self.cfg).redact_ex(code)
        quarantined = bool(values)
        stored = (masked or "") if quarantined else code
        sha = code_sha1(stored)
        parent = int(parent) if parent is not None and int(parent) > 0 else None
        analysis = self._clean(analysis, ANALYSIS_LIMIT)
        try:
            with self.lock():
                existing = self.versions(case_id)
                for m in existing:
                    if (m.sha1 or self._file_sha1(case_id, m.version)) == sha:
                        log.info("fix script %s: identical to v%d — not stored again", case_id, m.version)
                        return m
                n = max(self.version_numbers(case_id), default=0) + 1
                notes: list[str] = []
                if parent is not None and all(m.version != parent for m in existing):
                    notes.append(f"parent v{parent} is not in the store")
                if quarantined:
                    notes.append(f"{QUARANTINE_NOTE} the code matched {len(values)} secret pattern(s) (kb.redact_file "
                                 "rules) — stored masked, never selected; the stored code is not the code that ran")
                meta = ScriptMeta(case_id=case_id, signature_generic=signature_generic or "", version=n,
                                  parent=parent, sha1=sha, run_id=run_id or "", project=project or "",
                                  model=model or "", source=source, analysis=analysis,
                                  status="quarantined" if quarantined else "active", notes=notes)
                _write_bytes(self.code_path(case_id, n), stored.encode("utf-8"))   # code first: an orphan .py
                self._write_meta(meta)                                         # is skipped, its number kept
                self._audit("quarantined" if quarantined else "created", meta, run_id=run_id, project=project,
                            model=model, source=source)
            if quarantined:
                log.warning("fix script %s v%d: the code matched a secret pattern — stored masked and quarantined",
                            case_id, n)
            else:
                log.info("fix script %s v%d stored%s", case_id, n, f" (augments v{parent})" if parent else "")
            return meta
        except OSError as e:
            log.warning("fix script %s not stored: %s: %s", case_id, type(e).__name__, e)
            return ScriptMeta(case_id=case_id, signature_generic=signature_generic or "", version=0, parent=parent,
                              sha1=sha, run_id=run_id or "", project=project or "", model=model or "",
                              source=source, analysis=analysis,
                              status="quarantined" if quarantined else "active",
                              notes=[f"not stored: {type(e).__name__}: {e}"[:NOTE_LIMIT]])

    def _file_sha1(self, case_id: str, version: int) -> str:
        try:
            return hashlib.sha1(self.code_path(case_id, version).read_bytes()).hexdigest()
        except OSError:
            return ""

    def credit(self, case_id: str, version: int, outcome: str, *, project: str = "", run_id: str = "") -> None:
        """Count one scored run of a version: improved / build_ok → a success (``project`` into success_projects),
        partial → a partial, no_change / regressed / reverted / apply_failed / blocked → a failure, inconclusive /
        pending → nothing; ``last_outcome`` / ``last_at`` / ``last_run_id`` always. Never raises."""
        try:
            version = int(version)
            if version < 1 or not valid_case_id(case_id):
                log.debug("fix script credit for %r v%s ignored", case_id, version)
                return
            with self.lock():
                meta = self._read_meta(case_id, version)
                if meta is None:
                    log.debug("fix script credit for unknown %s v%d ignored", case_id, version)
                    return
                if outcome in SUCCESS_OUTCOMES:
                    meta.successes += 1
                    if project and project not in meta.success_projects:
                        meta.success_projects.append(project)
                elif outcome in PARTIAL_OUTCOMES:
                    meta.partials += 1
                elif outcome in FAILURE_OUTCOMES:
                    meta.failures += 1
                meta.last_outcome = outcome or ""
                meta.last_at = utcnow()
                meta.last_run_id = run_id or ""
                self._write_meta(meta)
                self._audit("credited", meta, outcome=outcome, run_id=run_id, project=project)
        except Exception as e:  # noqa: BLE001 - a credit must never fail the loop
            log.warning("fix script credit %s v%s failed: %s: %s", case_id, version, type(e).__name__, e)

    def set_status(self, case_id: str, version: int, status: Literal["active", "retired"], *,
                   who: Optional[str] = None, reason: str = "", command: str = "") -> ScriptMeta:
        """A person's decision: retire a version (never selected again) or activate it again. KeyError for an
        unknown version, ValueError for a quarantined one (its stored code is masked; it stays quarantined)."""
        if status not in ("active", "retired"):
            raise ValueError(f"unknown script status {status!r} (expected active | retired)")
        with self.lock():
            meta = self._read_meta(case_id, int(version)) if valid_case_id(case_id) else None
            if meta is None:
                raise KeyError(f"no fix script {case_id} v{version}")
            if meta.status == "quarantined":
                raise ValueError(f"{case_id} v{meta.version} is quarantined (its code held a secret, stored masked) "
                                 "— it stays quarantined; store a clean version instead")
            if meta.status == status:
                return meta
            before = meta.status
            who = who or _who()
            meta.status = status
            what = "retired" if status == "retired" else "activated"
            note = f"{what} by {who} at {utcnow().isoformat(timespec='seconds')}" + (f": {reason}" if reason else "")
            meta.notes.append(note[:NOTE_LIMIT])
            self._write_meta(meta)
            self._audit(what, meta, who=who, reason=reason, command=command, before={"status": before})
        return meta

    def retire(self, case_id: str, version: int, *, who: Optional[str] = None, reason: str = "",
               command: str = "") -> ScriptMeta:
        return self.set_status(case_id, version, "retired", who=who, reason=reason, command=command)

    def activate(self, case_id: str, version: int, *, who: Optional[str] = None, reason: str = "",
                 command: str = "") -> ScriptMeta:
        return self.set_status(case_id, version, "active", who=who, reason=reason, command=command)

    # ------------------------------------------------------------------ transfer
    def merge_from(self, other: "ScriptStore") -> dict[str, int]:
        """Merge another store (``sbmig kb import``): a version whose sha1 is already here merges its counts (the
        larger of each counter, success projects united, the newer last_*; the local status wins); any other one is
        added as the next local version with its record (parents mapped to the local numbers, a note naming the
        imported version). Returns ``{"script_versions_added", "script_versions_merged"}``."""
        added = merged = 0
        with self.lock():
            for cid in other.case_ids():
                local = {m.sha1 or self._file_sha1(cid, m.version): m for m in self.versions(cid)}
                vmap: dict[int, int] = {}
                for m in other.versions(cid):
                    got = other.get(cid, m.version)
                    if got is None:
                        continue
                    theirs, code = got
                    sha = theirs.sha1 or code_sha1(code)
                    mine = local.get(sha)
                    if mine is not None:
                        vmap[theirs.version] = mine.version
                        upd = mine.model_copy(deep=True)
                        upd.successes = max(mine.successes, theirs.successes)
                        upd.failures = max(mine.failures, theirs.failures)
                        upd.partials = max(mine.partials, theirs.partials)
                        upd.success_projects += [p for p in theirs.success_projects if p not in upd.success_projects]
                        if theirs.last_at and (mine.last_at is None or theirs.last_at > mine.last_at):
                            upd.last_outcome, upd.last_at, upd.last_run_id = (theirs.last_outcome, theirs.last_at,
                                                                              theirs.last_run_id)
                        if upd != mine:
                            self._write_meta(upd)
                            merged += 1
                        continue
                    n = max(self.version_numbers(cid), default=0) + 1
                    vmap[theirs.version] = n
                    parent = vmap.get(theirs.parent) if theirs.parent else None
                    new = theirs.model_copy(update={"version": n, "parent": parent, "sha1": sha,
                                                    "notes": [*theirs.notes, f"imported (v{theirs.version} there)"]})
                    _write_bytes(self.code_path(cid, n), code.encode("utf-8"))
                    self._write_meta(new)
                    local[sha] = new
                    added += 1
        return {"script_versions_added": added, "script_versions_merged": merged}
