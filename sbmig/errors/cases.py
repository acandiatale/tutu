"""The v2.1 knowledge base (docs/v2/PLAN-v2.1.md §3.4): every run's error list + one folder per case.

Layout under the KB directory (``kb.dir``, default ``kb-v2/``)::

    errors.jsonl                 every run's entries (ErrorEntry lines, the run's errors.jsonl appended at its end)
    cases.md                     the case table for people (rewritten by every record_run / reindex)
    cases/<case_id>/case.json    CaseInfo: case_id, l2, name, kind, projects, seen, resolved, scripts[…],
                                 last_analysis, last_solution, updated_at — plus the keys people / sbmig add:
                                 "pin": "block[:<reason>]" (never tried: `sbmig kb cases pin`), "last_result"
                                 (v2.1e: every script carries parent / reason — its augment lineage and why it was
                                 made — last_failure and runs[]: one ScriptRun per scored run, newest last, capped
                                 by errors.script_runs)
    cases/<case_id>/fix_v<N>.py  the stored fix scripts (exact bytes; an identical script is stored once)
    .lock                        the cross-process write lock (web server, fleet and `sbmig kb` share one KB)

Scripts: ``unverified`` when stored → ``proven`` once a reuse resolved the case in a project other than the one
that created it → ``retired`` by a person (``sbmig kb cases retire``; never selected). :meth:`CaseStore.best` =
the newest proven version, else the newest unverified one. A script whose code holds a secret value is stored
masked and retired at once (the masked code is not the code that ran; a secret never lands in the KB).

Every write holds the KB lock and is atomic; texts are masked (:mod:`sbmig.kb.redact`). Readers never lock and
never raise on a damaged file (it is skipped with a warning; ``sbmig kb validate`` lists it). File-based on purpose
(invariant 3).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

from sbmig.core.models import CaseInfo, CaseScript, EditNote, ErrorEntry, ErrorKind, FixTry, ScriptRun, utcnow
from sbmig.core.stage import stage

from .common import Cleaner, atomic_write, atomic_write_bytes, case_id_for, first_line, note_text, posix
from .entries import change_line, guide_lines, tries_text

log = logging.getLogger("sbmig.errors")

CASES_DIR = "cases"
ENTRIES_DIR = "errors"            # <kb>/cases/<case_id>/errors/<run_id>-<id>.json (one readable file per error)
OTHER_DIR = "_other"              # entries without a case (pom-verify, notes)
CASE_FILE = "case.json"
KB_ERRORS = "errors.jsonl"
CASES_MD = "cases.md"
SUCCESS_RESULTS = frozenset({"improved", "build_ok"})
FAILURE_RESULTS = frozenset({"no_change", "regressed", "reverted", "apply_failed", "blocked"})
TEXT_CHARS = 4000
REASON_CHARS = 600                # ScriptRun.reason / CaseScript.reason / last_failure (one line)
RUN_FILES = 50                    # ScriptRun.files kept
SCRIPT_RUNS = 30                  # errors.script_runs default (0 = no cap)
RUN_SOURCES = frozenset({"kb-script", "llm-new", "llm-augment"})   # tries whose stored script ran (reindex)
# parts of a try's note that describe the try rather than why it failed (left out of a run's reason)
NOISE_NOTES = ("reused stored v", "escalation model requested", "merge-conflict hint:")
_NOT_RUN = ("stored (unverified), not run", "not run again")        # the try's script never ran
_RAN_THEN_EDITS = "did not apply → applying the answer's edits"      # it ran, failed; the edits went in instead
SECRET_HEADER = "# sbmig: retired at once — the code held a secret value (stored masked; not the code that ran)\n"
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SCRIPT_RE = re.compile(r"^fix_v([1-9][0-9]{0,8})\.py$")


def valid_case_id(case_id: Optional[str]) -> bool:
    """A case id usable as one directory name (hex in practice; never a path or ``..``)."""
    return bool(case_id) and bool(_CASE_ID_RE.match(case_id or "")) and ".." not in (case_id or "")


def case_name(message: str, l2: str) -> str:
    """``package jakarta.xml.bind does not exist``; ``cannot find symbol: WebSecurityConfigurerAdapter`` when the
    message does not name the L2's key."""
    msg = first_line(message, 120)
    key = (l2 or "").split("|")[-1] if (l2 or "").count("|") >= 2 else ""
    if key and key not in msg:
        return f"{msg}: {key}"[:160] if msg else key[:160]
    return msg or (l2 or "")[:160]


def _short(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def scripts_text(case: CaseInfo, *, failures: bool = False) -> str:
    """``v1 proven 3/0, v2 unverified 0/1`` (successes / failures); ``failures`` (cases.md, v2.1e) adds each
    version's last failure: ``v1 proven 3/0; v2 unverified 0/1 — no_change: 3 of 3 targeted error(s) remained``."""
    def one(s: CaseScript) -> str:
        t = f"v{s.version} {s.status} {s.successes}/{s.failures}"
        return t + (f" — {_short(s.last_failure, 80)}" if failures and s.last_failure else "")
    return ("; " if failures else ", ").join(one(s) for s in case.scripts) or "-"


def run_reason(result: str, note: str = "", head: str = "") -> str:
    """ScriptRun.reason (v2.1e): why a run of a stored script did not resolve its case, one line — "" for improved /
    build_ok. ``head`` = the caller's own first part (the fix loop: ``no_change: 3 of 3 targeted error(s) remained —
    …``), then the try's note without the parts that describe the try rather than its failure (the reuse banner, an
    escalation request, merge-conflict hints); the bare result when nothing is left."""
    if result in SUCCESS_RESULTS:
        return ""
    parts = [" ".join(p.split()) for p in (note or "").split("; ")]
    parts = [p for p in parts if p and not p.startswith(NOISE_NOTES)]
    if head and " ".join(head.split()):
        parts.insert(0, " ".join(head.split()))
    return "; ".join(dict.fromkeys(parts)) or (result or "")


def first_reason(reason: str, parent: int | None = None, limit: int = 160) -> str:
    """The first part of a reason for a log line (``v1: `` of an augment's reason dropped: the line names it)."""
    t = " ".join((reason or "").split())
    if parent and t.startswith(f"v{parent}: "):
        t = t[len(f"v{parent}: "):]
    for sep in ("; ", ". "):
        if sep in t:
            t = t.split(sep, 1)[0]
    return _short(t, limit)


def run_line(r: ScriptRun) -> str:
    """``run r-1 demo r2 try 1: no_change — no_change: 3 of 3 targeted error(s) remained [errors 5 → 5, new 0;
    files: a/B.java]`` (``sbmig kb cases show``)."""
    s = f"run {r.run_id or '-'} {r.project or '-'} r{r.round} try {r.n}: {r.result or '-'}"
    if r.reason:
        s += f" — {r.reason}"
    facts = []
    if r.errors_before is not None or r.errors_left is not None:
        facts.append(f"errors {'?' if r.errors_before is None else r.errors_before} → "
                     f"{'?' if r.errors_left is None else r.errors_left}")
    if r.errors_new is not None:
        facts.append(f"new {r.errors_new}")
    tail = ", ".join(facts)
    if r.files:
        tail += ("; " if tail else "") + "files: " + ", ".join(r.files[:5]) + \
            (f", … (+{len(r.files) - 5})" if len(r.files) > 5 else "")
    return s + (f" [{tail}]" if tail else "")


def _sha(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _ts(at: Any) -> float:
    try:
        return float(at.timestamp())
    except Exception:  # noqa: BLE001 - a hand-edited record: oldest
        return 0.0


def _run_key(r: ScriptRun) -> tuple[str, int, int] | None:
    """(run_id, round, n) — None for a run credited without a try number (never merged with another)."""
    return (r.run_id, int(r.round), int(r.n)) if r.n else None


class CaseStore:
    """``<kb>/cases/<case_id>/`` + ``<kb>/errors.jsonl`` + ``<kb>/cases.md``."""

    def __init__(self, kb_dir: Union[str, Path], cfg: Any = None, *, lock_timeout: Optional[float] = None,
                 script_runs: int | None = None):
        self.root = Path(kb_dir)
        self.cfg = cfg
        if lock_timeout is None:
            lock_timeout = float(getattr(getattr(cfg, "kb", None), "lock_timeout_seconds", 300) or 0)
        self.lock_timeout = lock_timeout
        if script_runs is None:
            script_runs = getattr(getattr(cfg, "errors", None), "script_runs", SCRIPT_RUNS)
        try:
            self.script_runs = max(0, int(script_runs if script_runs is not None else SCRIPT_RUNS))
        except (TypeError, ValueError):
            self.script_runs = SCRIPT_RUNS
        self.clean = Cleaner(cfg)
        self.last_reindex: dict[str, int] = {}         # v2.1e: what the last recount rebuilt (runs, hunks)

    # ------------------------------------------------------------------ layout
    @property
    def cases_dir(self) -> Path:
        return self.root / CASES_DIR

    @property
    def errors_path(self) -> Path:
        return self.root / KB_ERRORS

    @property
    def md_path(self) -> Path:
        return self.root / CASES_MD

    def lock(self):
        from sbmig.kb.lock import kb_lock

        return kb_lock(self.root, self.lock_timeout)

    def case_dir(self, case_id: str) -> Path:
        if not valid_case_id(case_id):
            raise ValueError(f"not a case id: {case_id!r}")
        return self.cases_dir / case_id

    def case_ids(self) -> list[str]:
        if not self.cases_dir.is_dir():
            return []
        return sorted(p.name for p in self.cases_dir.iterdir() if p.is_dir() and (p / CASE_FILE).is_file())

    # ------------------------------------------------------------------ case.json
    def _read(self, case_id: str) -> Optional[tuple[CaseInfo, dict[str, Any]]]:
        """(case, extra keys of case.json) — None when there is no readable case file."""
        if not valid_case_id(case_id):
            return None
        path = self.cases_dir / case_id / CASE_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            case = CaseInfo.model_validate(data)
        except (OSError, ValueError) as e:
            log.warning("kb: %s does not read (%s) — skipped (`sbmig kb validate`)", path, type(e).__name__)
            return None
        if case.case_id != case_id:
            log.warning("kb: %s names case %s — skipped", path, case.case_id)
            return None
        extra = {k: v for k, v in data.items() if k not in CaseInfo.model_fields}
        return case, extra

    def _save(self, case: CaseInfo, extra: Optional[dict[str, Any]] = None) -> None:
        data = case.model_dump(mode="json")
        data.update({k: v for k, v in (extra or {}).items() if k not in data})
        atomic_write(self.case_dir(case.case_id) / CASE_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def find(self, case_id: str) -> Optional[CaseInfo]:
        hit = self._read(case_id)
        return hit[0] if hit else None

    def extra(self, case_id: str) -> dict[str, Any]:
        """The keys of case.json beyond CaseInfo (``pin``, ``last_result``, anything a person added)."""
        hit = self._read(case_id)
        return dict(hit[1]) if hit else {}

    def pin_of(self, case_id: str) -> str:
        """``block`` / ``block:<reason>`` when a person pinned the case, else "" (anything else in ``pin`` is
        ignored here; ``sbmig kb validate`` names it)."""
        p = self.extra(case_id).get("pin")
        p = p.strip() if isinstance(p, str) else ""
        return p if p == "block" or p.startswith("block:") else ""

    def _case_or_new(self, case_id: str, l2: str = "", name: str = "",
                     kind: Optional[ErrorKind] = None) -> tuple[CaseInfo, dict[str, Any]]:
        hit = self._read(case_id)
        if hit is not None:
            case, extra = hit
            if l2 and not case.l2:
                case.l2 = l2
            if name and not case.name:
                case.name = name
            if kind is not None and case.kind == ErrorKind.UNKNOWN:
                case.kind = kind
            return case, extra
        return CaseInfo(case_id=case_id, l2=l2, name=name or case_name("", l2),
                        kind=kind or ErrorKind.UNKNOWN), {}

    def set_pin(self, case_id: str, pin: str) -> CaseInfo:
        """Pin (``block`` / ``block:<reason>``) or unpin (``""``) a case; creates the case file when needed."""
        pin = (pin or "").strip()
        if pin and not (pin == "block" or pin.startswith("block:")):
            raise ValueError(f"a pin is `block` or `block:<reason>`, not {pin!r}")
        with stage("kb"), self.lock():
            case, extra = self._case_or_new(case_id)
            if pin:
                extra["pin"] = self.clean(pin, 500)
            else:
                extra.pop("pin", None)
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("case %s %s", case_id, f"pinned: {extra['pin']}" if pin else "unpinned")
            self._write_md_locked()
        return case

    # ------------------------------------------------------------------ scripts
    def scripts(self, case_id: str) -> list[CaseScript]:
        """The case's scripts, oldest first."""
        case = self.find(case_id)
        return sorted(case.scripts, key=lambda s: s.version) if case else []

    def code(self, case_id: str, version: int) -> Optional[str]:
        if not valid_case_id(case_id):
            return None
        path = self.cases_dir / case_id / f"fix_v{int(version)}.py"
        try:
            return path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def best(self, case_id: str) -> Optional[tuple[CaseScript, str]]:
        """The script to run first: the newest proven version, else the newest unverified one; retired never (nor
        a version whose file is missing). None = nothing stored."""
        ranked = sorted((s for s in self.scripts(case_id) if s.status != "retired"),
                        key=lambda s: (s.status == "proven", s.version), reverse=True)
        for s in ranked:
            code = self.code(case_id, s.version)
            if code:
                return s, code
        return None

    def latest(self, case_id: str) -> Optional[tuple[CaseScript, str]]:
        """The newest non-retired version (the base of an augment request)."""
        for s in sorted(self.scripts(case_id), key=lambda s: s.version, reverse=True):
            code = self.code(case_id, s.version) if s.status != "retired" else None
            if code:
                return s, code
        return None

    def add_script(self, case_id: str, l2: str, code: str, *, source: str = "llm-new", run_id: str = "",
                   project: str = "", analysis: str = "", solution: str = "", name: str = "",
                   kind: Optional[ErrorKind] = None, parent: int | None = None, reason: str = "") -> CaseScript:
        """Store ``code`` as the case's next version (``unverified``); identical code returns the stored version
        (unchanged). v2.1e: ``parent`` = the version an augment extends, ``reason`` = why this version was made (the
        parent's failure in words, ``v1: no_change: …``)."""
        if not (code or "").strip():
            raise ValueError("empty script")
        try:
            parent = int(parent) if parent else None
        except (TypeError, ValueError):
            parent = None
        why = self._reason(reason)
        with stage("kb"), self.lock():
            case, extra = self._case_or_new(case_id, l2, name, kind)
            masked = self.clean.red.redact(code) or ""
            secret = masked != code
            stored = SECRET_HEADER + masked if secret else code
            for s in case.scripts:
                if _sha(self.code(case_id, s.version) or "") == _sha(stored):
                    log.info("cases/%s/fix_v%d.py: the same script is already stored", case_id, s.version)
                    return s
            on_disk = [int(m.group(1)) for p in (self.case_dir(case_id).glob("fix_v*.py")
                                                if self.case_dir(case_id).is_dir() else [])
                       if (m := _SCRIPT_RE.match(p.name))]
            v = 1 + max([s.version for s in case.scripts] + on_disk, default=0)
            atomic_write_bytes(self.case_dir(case_id) / f"fix_v{v}.py", stored.encode("utf-8"))
            s = CaseScript(version=v, file=f"fix_v{v}.py", status="retired" if secret else "unverified",
                           source=source, created_by=run_id, project=project, parent=parent, reason=why)
            case.scripts.append(s)
            if analysis:
                case.last_analysis = self.clean(analysis, TEXT_CHARS)
            if solution:
                case.last_solution = self.clean(solution, TEXT_CHARS)
            case.updated_at = utcnow()
            self._save(case, extra)
            if secret:
                log.warning("cases/%s/fix_v%d.py: the script held a secret value — stored masked and retired",
                            case_id, v)
            else:
                lineage = ""
                if parent:
                    first = first_reason(why, parent)
                    lineage = f" (v{parent} 보강: {first})" if first else f" (v{parent} 보강)"
                log.info("스크립트 v%d 저장 (unverified, %s) → cases/%s/fix_v%d.py%s", v, source, case_id, v, lineage)
        return s                                          # (cases.md: rewritten by record_run at the end of the run)

    def _reason(self, text: str | None) -> str:
        """A reason as stored: masked, one line, at most REASON_CHARS."""
        t = " ".join(self.clean(text or "").split())
        return t if len(t) <= REASON_CHARS else t[: REASON_CHARS - 1] + "…"

    def _run_files(self, files: Iterable[Any]) -> list[str]:
        out = [posix(self.clean(str(f))) for f in (files or ()) if f]
        return list(dict.fromkeys(f for f in out if f))[:RUN_FILES]

    def _cap_runs(self, runs: list[ScriptRun]) -> list[ScriptRun]:
        return runs[-self.script_runs:] if self.script_runs and len(runs) > self.script_runs else runs

    def _add_run(self, s: CaseScript, run: ScriptRun) -> None:
        """Append ``run`` (newest last); a run with the same (run_id, round, n) is replaced — an inconclusive try
        is credited again after the revert rebuild scored it."""
        key = _run_key(run)
        runs = [r for r in s.runs if key is None or _run_key(r) != key]
        s.runs = self._cap_runs(runs + [run])

    def credit(self, case_id: str, version: int, result: str, project: str = "", run_id: str = "", *,
               reason: str = "", round: int = 0, n: int = 0, errors_before: int | None = None,
               errors_left: int | None = None, errors_new: int | None = None, files: Iterable[Any] = ()
               ) -> Optional[CaseScript]:
        """The scored result of a run of version N: improved / build_ok → successes +1 (project added; ``proven``
        once that is a project other than the one that created it); no_change / regressed / reverted /
        apply_failed / blocked → failures +1; anything else (partial, inconclusive) only sets ``last_result``;
        pending is ignored. v2.1e: every credit appends a ``ScriptRun`` (round / try ``n``, the reason in words,
        error counts, files; the same run_id + round + n replaces the earlier record; ``errors.script_runs`` kept)
        and a failure sets ``last_failure``."""
        if not result or result == "pending":
            return None
        why = self._reason(reason)
        with stage("kb"), self.lock():
            hit = self._read(case_id)
            if hit is None:
                return None
            case, extra = hit
            s = next((x for x in case.scripts if x.version == int(version)), None)
            if s is None:
                log.warning("cases/%s: no script v%s to credit", case_id, version)
                return None
            s.last_result = result
            if result in SUCCESS_RESULTS:
                s.successes += 1
                if project and project not in s.projects:
                    s.projects.append(project)
                other = (project and s.project and project != s.project) or \
                    (not s.project and run_id and s.created_by and run_id != s.created_by)
                if s.status == "unverified" and other:
                    s.status = "proven"
                    log.info("cases/%s/fix_v%d.py: proven (resolved the case in %s)", case_id, s.version, project)
            elif result in FAILURE_RESULTS:
                s.failures += 1
                s.last_failure = why or result
            self._add_run(s, ScriptRun(run_id=run_id or "", project=project or "", round=int(round or 0),
                                       n=int(n or 0), result=result, reason=why, errors_before=errors_before,
                                       errors_left=errors_left, errors_new=errors_new, files=self._run_files(files)))
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("cases/%s/fix_v%d.py: %s (성공 %d / 실패 %d)%s", case_id, s.version, result, s.successes,
                     s.failures, f" — {_short(why, 300)}" if why else "")
        return s

    def _set_status(self, case_id: str, version: int, retire: bool) -> CaseScript:
        with stage("kb"), self.lock():
            hit = self._read(case_id)
            if hit is None:
                raise KeyError(f"no case {case_id}")
            case, extra = hit
            s = next((x for x in case.scripts if x.version == int(version)), None)
            if s is None:
                raise KeyError(f"case {case_id} has no script v{version}")
            if retire:
                s.status = "retired"
            else:
                s.status = "proven" if any(p != s.project for p in s.projects) else "unverified"
            case.updated_at = utcnow()
            self._save(case, extra)
            log.info("cases/%s/fix_v%d.py: %s", case_id, s.version, s.status)
            self._write_md_locked()
        return s

    def retire(self, case_id: str, version: int) -> CaseScript:
        return self._set_status(case_id, version, True)

    def activate(self, case_id: str, version: int) -> CaseScript:
        """Undo a retire: proven when it resolved the case in another project, else unverified."""
        return self._set_status(case_id, version, False)

    def describe(self, case_id: str) -> str:
        """The KB lookup result as one log phrase: ``저장된 스크립트 없음 (처음 보는 케이스)``, ``저장된 스크립트 v2 (proven,
        성공 3 / 실패 0, 프로젝트 2)``, ``pin block: <reason>``, ``케이스는 있으나 쓸 스크립트 없음 (v1 retired)``."""
        case = self.find(case_id)
        if case is None:
            return "저장된 스크립트 없음 (처음 보는 케이스)"
        pin = self.pin_of(case_id)
        if pin:
            return f"pin {pin.replace(':', ': ', 1)}"
        best = self.best(case_id)
        if best is None:
            return ("케이스는 있으나 쓸 스크립트 없음 (" + scripts_text(case) + ")") if case.scripts else \
                f"저장된 스크립트 없음 (케이스는 {len(case.projects)}개 프로젝트에서 봄)"
        s = best[0]
        return (f"저장된 스크립트 v{s.version} ({s.status}, 성공 {s.successes} / 실패 {s.failures}, "
                f"프로젝트 {len(s.projects)})")

    # ------------------------------------------------------------------ errors.jsonl + cases
    def _known_ids(self) -> set[str]:
        from sbmig.kb.jsonl import line_ids

        out: set[str] = set()
        if not self.errors_path.is_file():
            return out
        with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    out.update(line_ids(line))
        return out

    def _clean_note(self, n: EditNote) -> EditNote:
        c = self.clean
        hunks = [h.model_copy(update={"before": c(h.before), "after": c(h.after)}) for h in n.hunks]
        return n.model_copy(update={"where": c(n.where), "change": c(n.change), "hunks": hunks})

    def _clean_entry(self, e: ErrorEntry) -> ErrorEntry:
        c = self.clean
        tries = [t.model_copy(update={"analysis": c(t.analysis), "solution": c(t.solution),
                                      "no_script_reason": c(t.no_script_reason), "diff": c(t.diff),
                                      "note": c(t.note), "edits": [self._clean_note(n) for n in t.edits]})
                 for t in e.tries]
        return e.model_copy(update={"message": c(e.message), "raw": c(e.raw), "note": c(e.note), "tries": tries})

    def record_run(self, entries: Iterable[ErrorEntry]) -> int:
        """Append a run's finished entries to ``<kb>/errors.jsonl`` (ids already stored are skipped), update each
        case they belong to (seen / resolved / projects / last analysis + solution / last result — a case folder is
        made for entries with an L2), rewrite ``cases.md``. Returns the number of lines appended."""
        from sbmig.kb.jsonl import append_lines

        entries = [self._clean_entry(e) for e in entries]
        if not entries:
            return 0
        with stage("kb"), self.lock():
            known = self._known_ids()
            fresh = [e for e in entries if e.id not in known]
            n = append_lines(self.errors_path, [e.model_dump_json() for e in fresh])
            by_case: dict[str, list[ErrorEntry]] = {}
            for e in fresh:
                if e.case_id and e.status not in ("note", "pom-verify") and valid_case_id(e.case_id) \
                        and (e.l2 or (self.cases_dir / e.case_id / CASE_FILE).is_file()):
                    by_case.setdefault(e.case_id, []).append(e)
            for cid, es in by_case.items():
                case, extra = self._case_or_new(cid, es[0].l2, case_name(es[0].message, es[0].l2), es[0].kind)
                self._apply(case, extra, es)
                self._save(case, extra)
            self._write_entry_files(fresh)
            self._write_md_locked()
            log.info("errors.jsonl +%d (%d already stored), %d case(s) updated, cases.md rewritten", n,
                     len(entries) - len(fresh), len(by_case))
        return n

    @property
    def json_files(self) -> bool:
        return bool(getattr(getattr(self.cfg, "errors", None), "json_files", True))

    def entry_file(self, e: ErrorEntry) -> Path:
        """``<kb>/cases/<case_id>/errors/<run_id>-<id>.json`` — entries without a case (pom-verify, notes) go under
        ``cases/_other/errors/``."""
        folder = e.case_id if e.case_id and valid_case_id(e.case_id) else OTHER_DIR
        return self.cases_dir / folder / ENTRIES_DIR / f"{e.run_id or 'run'}-{e.id}.json"

    def _write_entry_files(self, entries: list[ErrorEntry]) -> int:
        """v2.1c (2026-10-27, the business team wants to browse the stored errors): one indented JSON file per
        error beside its case — the same record as the errors.jsonl line, readable in any editor."""
        if not self.json_files:
            return 0
        n = 0
        for e in entries:
            try:
                atomic_write(self.entry_file(e), json.dumps(e.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
                n += 1
            except OSError as ex:
                log.warning("could not write %s: %s", self.entry_file(e), ex)
        return n

    def entry_files(self, case_id: str) -> list[Path]:
        return sorted((self.cases_dir / case_id / ENTRIES_DIR).glob("*.json"))

    @staticmethod
    def _apply(case: CaseInfo, extra: dict[str, Any], es: list[ErrorEntry]) -> None:
        """Count one run's entries of the case into it."""
        case.seen += len(es)
        case.resolved += sum(1 for e in es if e.status == "resolved")
        for e in es:
            if e.project and e.project not in case.projects:
                case.projects.append(e.project)
        tries = [t for e in es for t in e.tries]
        with_text = [t for t in tries if t.analysis or t.solution]
        if with_text:
            t = max(with_text, key=lambda t: (t.round, t.n))
            case.last_analysis = t.analysis or case.last_analysis
            case.last_solution = t.solution or case.last_solution
        status: dict[str, int] = {}
        for e in es:
            status[e.status] = status.get(e.status, 0) + 1
        last = es[-1]
        how = next((e.resolved for e in es if e.status == "resolved" and e.resolved), "")
        extra["last_result"] = (", ".join(f"{k} {v}" for k, v in sorted(status.items())) + (f" ({how})" if how else "")
                                + f" — {last.project or '-'} {last.run_id or ''}".rstrip())
        case.updated_at = utcnow()

    def iter_errors(self, *, run: str = "", case: str = "", status: str = "", project: str = "",
                    entry: str = "") -> Iterator[ErrorEntry]:
        """``<kb>/errors.jsonl`` entries (file order; unreadable lines skipped), filtered (``entry`` = id prefix,
        ``case`` = case id prefix, ``run`` / ``project`` exact)."""
        from sbmig.kb.jsonl import parse_record

        if not self.errors_path.is_file():
            return
        with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                e = parse_record(line, ErrorEntry)
                if e is None:
                    continue
                if (run and e.run_id != run) or (case and not e.case_id.startswith(case)) or \
                        (status and e.status != status) or (project and e.project != project) or \
                        (entry and not e.id.startswith(entry)):
                    continue
                yield e

    def count_errors(self) -> int:
        if not self.errors_path.is_file():
            return 0
        with open(self.errors_path, "rb") as fh:
            return sum(1 for line in fh if line.strip())

    def recount(self) -> int:
        """Rebuild every case's seen / resolved / projects / last texts from ``<kb>/errors.jsonl`` (script counts and
        people's keys untouched; cases for L2s that only errors.jsonl knows are made). Returns the cases.

        v2.1e: every stored version's ``runs`` are rebuilt from the entries' tries (merged with the runs the store
        already has: those keep their error counts; deduplicated by run_id + round + try n), ``parent`` / ``reason``
        filled where an ``llm-augment`` try shows them, ``last_failure`` = the newest failed run's reason; tries with
        a diff whose 수정 notes lack ``hunks`` get them (``sbmig.script.notes.attach_hunks``) — errors.jsonl and those
        entries' JSON files are rewritten once. One INFO line says how many."""
        with stage("kb"), self.lock():
            try:
                hunked = self._backfill_hunks()
            except Exception as e:  # noqa: BLE001 - the backfill is extra; the recount goes on
                log.warning("kb reindex: hunk backfill failed: %s: %s", type(e).__name__, e)
                hunked = 0
            groups: dict[str, list[ErrorEntry]] = {}
            for e in self.iter_errors():
                if e.case_id and e.status not in ("note", "pom-verify") and valid_case_id(e.case_id):
                    groups.setdefault(e.case_id, []).append(e)
            ids = set(self.case_ids()) | {cid for cid, es in groups.items() if any(x.l2 for x in es)}
            rebuilt = 0
            for cid in sorted(ids):
                es = groups.get(cid, [])
                first = es[0] if es else None
                case, extra = self._case_or_new(cid, first.l2 if first else "",
                                                case_name(first.message, first.l2) if first else "",
                                                first.kind if first else None)
                case.seen, case.resolved, case.projects = 0, 0, []
                runs: dict[str, list[ErrorEntry]] = {}
                for e in es:
                    runs.setdefault(e.run_id, []).append(e)
                for run_es in runs.values():
                    self._apply(case, extra, run_es)
                try:
                    rebuilt += self._rebuild_runs(case, es)
                except Exception as e:  # noqa: BLE001 - the run history is extra; the counts are saved anyway
                    log.warning("kb reindex: runs of case %s not rebuilt: %s: %s", cid, type(e).__name__, e)
                self._save(case, extra)
            # the readable per-error files for entries stored before they existed (or deleted by hand)
            missing = [e for e in self.iter_errors() if not self.entry_file(e).is_file()]
            if missing:
                log.info("writing %d per-error JSON file(s) missing under cases/*/errors/", len(missing))
                self._write_entry_files(missing)
            self._write_md_locked()
            self.last_reindex = {"cases": len(ids), "runs": rebuilt, "hunks": hunked}
            log.info("reindex: %d script run record(s) rebuilt, %d entr(ies) given as-is/to-be hunks", rebuilt, hunked)
            return len(ids)

    # ------------------------------------------------------------------ v2.1e: runs + hunks from errors.jsonl
    @staticmethod
    def _ran(t: FixTry) -> str | None:
        """The result the try's stored script was credited with when it ran — None when it did not run (stored
        next to applied edits, the same code again, pending)."""
        if not t.script_version or t.source not in RUN_SOURCES or t.result == "pending":
            return None
        note = t.note or ""
        if t.applied == "edits":
            return "apply_failed" if _RAN_THEN_EDITS in note else None
        if any(x in note for x in _NOT_RUN):
            return None
        return t.result

    def _rebuild_runs(self, case: CaseInfo, es: list[ErrorEntry]) -> int:
        """The case's tries → each stored version's runs (merged), parent / reason, last_failure. Returns the run
        records derived from errors.jsonl for versions the case has."""
        derived: dict[int, dict[tuple[str, int, int], ScriptRun]] = {}
        augments: dict[int, list[tuple[str, int, int]]] = {}
        for e in es:
            for t in e.tries:
                result = self._ran(t)
                if result is None:
                    continue
                v, key = int(t.script_version), (e.run_id, int(t.round), int(t.n))
                per = derived.setdefault(v, {})
                if key in per:                            # several entries of the case share the try: one run
                    continue
                # a no_change note never says why (the scoring adds none): the result leads
                head = result if result == "no_change" and result not in (t.note or "") else ""
                per[key] = ScriptRun(run_id=e.run_id, project=e.project, round=int(t.round), n=int(t.n),
                                     result=result, reason=self._reason(run_reason(result, t.note, head)),
                                     files=self._run_files(t.files), at=e.at)
                if t.source == "llm-augment":
                    augments.setdefault(v, []).append(key)
        n = 0
        merged: dict[int, dict[tuple[str, int, int], ScriptRun]] = {}
        for s in case.scripts:
            mine = derived.get(s.version, {})
            n += len(mine)
            keyed = {k: r for r in s.runs if (k := _run_key(r)) is not None}
            loose = [r for r in s.runs if _run_key(r) is None]
            for key, r in mine.items():
                have = keyed.get(key)
                if have is None:                          # the store's own record wins (it has the counts)
                    keyed[key] = r
                    continue
                fill = {f: getattr(r, f) for f in ("project", "reason", "files") if not getattr(have, f)
                        and getattr(r, f) and not (f == "reason" and have.result in SUCCESS_RESULTS)}
                if fill:
                    keyed[key] = have.model_copy(update=fill)
            merged[s.version] = keyed
            runs = sorted(loose + list(keyed.values()), key=lambda r: (_ts(r.at), r.round, r.n))
            failed = [r for r in runs if r.result in FAILURE_RESULTS]
            if failed:
                s.last_failure = failed[-1].reason or failed[-1].result
            s.runs = self._cap_runs(runs)
        for s in case.scripts:
            if s.parent is None and s.version in augments:
                self._lineage(s, augments[s.version], merged)
        return n

    def _lineage(self, s: CaseScript, keys: list[tuple[str, int, int]],
                 runs: dict[int, dict[tuple[str, int, int], ScriptRun]]) -> None:
        """An augment's parent (best effort): the highest lower version that ran in the same run before the try
        that made it (the run that created the version first); reason = that run's reason."""
        keys = sorted(keys, key=lambda k: (k[0] != s.created_by, k[1], k[2]))
        for run_id, rnd, n in keys:
            before = [(v, k, r) for v, per in runs.items() if v < s.version for k, r in per.items()
                      if k[0] == run_id and (k[1], k[2]) < (rnd, n)]
            if not before:
                continue
            v, _k, r = max(before, key=lambda x: (x[0], x[1][1], x[1][2]))
            s.parent = v
            if not s.reason:
                s.reason = self._reason(f"v{v}: {r.reason or r.result}")
            return

    @property
    def snippet_lines(self) -> int:
        """``errors.snippet_lines``: lines kept per side of a hunk (0 = the whole hunk)."""
        try:
            return max(0, int(getattr(getattr(self.cfg, "errors", None), "snippet_lines", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _hunked(self, e: ErrorEntry, attach: Any, max_lines: int = 0) -> ErrorEntry | None:
        """``e`` with hunks on the tries whose diff notes lack them; None when nothing changed."""
        changed, tries = False, []
        for t in e.tries:
            if t.diff and t.edits and any(not n.hunks for n in t.edits):
                try:
                    got = attach(list(t.edits), t.diff, max_lines=max_lines)
                    notes = [self._clean_note(x if isinstance(x, EditNote) else EditNote.model_validate(x))
                             for x in (got or [])]
                except Exception as ex:  # noqa: BLE001 - a broken helper leaves the try as it is
                    log.debug("attach_hunks failed for %s try %d: %s: %s", e.id, t.n, type(ex).__name__, ex)
                    notes = []
                if notes and [x.model_dump() for x in notes] != [x.model_dump() for x in t.edits]:
                    t = t.model_copy(update={"edits": notes})
                    changed = True
            tries.append(t)
        return e.model_copy(update={"tries": tries}) if changed else None

    def _backfill_hunks(self) -> int:
        """errors.jsonl entries stored before v2.1e: as-is / to-be hunks from their diffs (``attach_hunks``,
        ``errors.snippet_lines`` per side; skipped when it is not there). Rewrites errors.jsonl atomically (other lines byte for byte, a line that does not read
        kept as it is) and the changed entries' JSON files. Caller holds the lock. Returns the entries changed."""
        try:
            from sbmig.script.notes import attach_hunks
        except Exception:  # noqa: BLE001 - ImportError / not there yet: no backfill
            return 0
        if not callable(attach_hunks) or not self.errors_path.is_file():
            return 0
        try:
            raw = self.errors_path.read_bytes()
        except OSError as ex:
            log.warning("kb reindex: %s does not read (%s) — no hunk backfill", self.errors_path, ex)
            return 0
        out: list[bytes] = []
        changed: list[ErrorEntry] = []
        bad, cut = 0, self.snippet_lines
        for b in raw.split(b"\n"):
            if not b.strip():
                out.append(b)
                continue
            try:
                e = ErrorEntry.model_validate_json(b.decode("utf-8").strip())
            except (UnicodeDecodeError, ValueError):
                bad += 1
                out.append(b)
                continue
            new = self._hunked(e, attach_hunks, cut)
            if new is None:
                out.append(b)
                continue
            out.append(new.model_dump_json().encode("utf-8"))
            changed.append(new)
        if bad:
            log.warning("kb reindex: %d line(s) of %s are no ErrorEntry — skipped (`sbmig kb validate`)", bad,
                        KB_ERRORS)
        if changed:
            atomic_write_bytes(self.errors_path, b"\n".join(out))
            self._write_entry_files(changed)
        return len(changed)

    # ------------------------------------------------------------------ cases.md / stats / validate
    def cases(self) -> list[tuple[CaseInfo, dict[str, Any]]]:
        out = []
        for cid in self.case_ids():
            hit = self._read(cid)
            if hit is not None:
                out.append(hit)
        out.sort(key=lambda h: (-len(h[0].projects), -h[0].seen, h[0].case_id))
        return out

    def _write_md_locked(self) -> None:
        try:
            atomic_write(self.md_path, render_cases_md(self.cases()))
        except Exception as e:  # noqa: BLE001 - the view is derived (`sbmig kb reindex` rewrites it)
            log.warning("kb: could not write %s: %s: %s", self.md_path, type(e).__name__, e)

    def write_cases_md(self) -> Path:
        with stage("kb"), self.lock():
            self._write_md_locked()
        return self.md_path

    def stats(self) -> dict[str, Any]:
        cases = self.cases()
        scripts = [s for c, _ in cases for s in c.scripts]
        return {
            "kb_dir": str(self.root),
            "errors": self.count_errors(),
            "cases": len(cases),
            "cases_with_scripts": sum(1 for c, _ in cases if c.scripts),
            "script_versions": len(scripts),
            "scripts_proven": sum(1 for s in scripts if s.status == "proven"),
            "scripts_unverified": sum(1 for s in scripts if s.status == "unverified"),
            "scripts_retired": sum(1 for s in scripts if s.status == "retired"),
            "pinned": sum(1 for _, x in cases if x.get("pin")),
        }

    def validate(self, limit: int = 20) -> list[str]:
        """Problems of the KB files, one line each: errors.jsonl lines that are no ErrorEntry, case.json files that
        do not read / name another case, scripts listed without a file or files no script lists, bad pins."""
        from sbmig.kb.jsonl import parse_record

        problems: list[str] = []
        if self.errors_path.is_file():
            bad = []
            with open(self.errors_path, "r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if line.strip() and parse_record(line, ErrorEntry) is None:
                        bad.append(n)
            problems += [f"{KB_ERRORS}: line {n} is no ErrorEntry" for n in bad[:limit]]
            if len(bad) > limit:
                problems.append(f"{KB_ERRORS}: … {len(bad) - limit} more unreadable line(s)")
        if self.cases_dir.is_dir():
            found = []
            for d in sorted(p for p in self.cases_dir.iterdir() if p.is_dir()):
                rel = f"{CASES_DIR}/{d.name}"
                path = d / CASE_FILE
                if not path.is_file():
                    found.append(f"{rel}: no {CASE_FILE}")
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    case = CaseInfo.model_validate(data)
                except (OSError, ValueError) as e:
                    found.append(f"{rel}/{CASE_FILE}: does not read ({type(e).__name__})")
                    continue
                if case.case_id != d.name:
                    found.append(f"{rel}/{CASE_FILE}: names case {case.case_id}")
                if case.l2 and case_id_for(case.l2) != d.name:
                    found.append(f"{rel}/{CASE_FILE}: l2 hashes to {case_id_for(case.l2)}, not {d.name}")
                listed = {s.file for s in case.scripts}
                for s in case.scripts:
                    if not (d / s.file).is_file():
                        found.append(f"{rel}: script v{s.version} has no file {s.file}")
                for p in d.glob("fix_v*.py"):
                    if p.name not in listed:
                        found.append(f"{rel}: {p.name} is not listed in {CASE_FILE}")
                pin = data.get("pin")
                if pin is not None and not (isinstance(pin, str) and (pin.strip() in ("", "block")
                                                                      or pin.strip().startswith("block:"))):
                    found.append(f"{rel}/{CASE_FILE}: pin {pin!r} is not `block` / `block:<reason>`")
            problems += found[:limit]
            if len(found) > limit:
                problems.append(f"{CASES_DIR}: … {len(found) - limit} more problem(s)")
        return problems

    # ------------------------------------------------------------------ import (merge)
    def merge_from(self, other: "CaseStore") -> dict[str, int]:
        """Merge another KB (an unpacked export): errors.jsonl lines with new ids, case folders the local KB does
        not have (copied whole), scripts of known cases by their code (same code → counters max / projects union;
        other code → the next local version), then :meth:`recount`. Idempotent."""
        from sbmig.kb.jsonl import append_lines

        counters = {"errors_added": 0, "cases_added": 0, "scripts_added": 0, "scripts_merged": 0}
        with stage("kb"), self.lock():
            if other.errors_path.is_file():
                known = self._known_ids()
                lines = [e.model_dump_json() for e in other.iter_errors() if e.id not in known]
                counters["errors_added"] = append_lines(self.errors_path, lines)
            for cid in other.case_ids():
                theirs = other._read(cid)
                if theirs is None:
                    continue
                t_case, t_extra = theirs
                mine = self._read(cid)
                if mine is None:
                    for s in t_case.scripts:
                        code = other.code(cid, s.version)
                        if code is not None:
                            atomic_write_bytes(self.case_dir(cid) / s.file, code.encode("utf-8"))
                    self._save(t_case, t_extra)
                    counters["cases_added"] += 1
                    continue
                case, extra = mine
                for k, v in t_extra.items():
                    extra.setdefault(k, v)                # a local pin wins
                by_sha = {_sha(self.code(cid, s.version) or ""): s for s in case.scripts}
                local: dict[int, int] = {}                # their version → ours (v2.1e: an augment's parent)
                for s in sorted(t_case.scripts, key=lambda x: x.version):
                    code = other.code(cid, s.version)
                    if code is None:
                        continue
                    have = by_sha.get(_sha(code))
                    if have is not None:
                        local[s.version] = have.version
                        have.successes, have.failures = max(have.successes, s.successes), max(have.failures, s.failures)
                        have.projects += [p for p in s.projects if p not in have.projects]
                        if s.status == "proven" and have.status == "unverified":
                            have.status = "proven"
                        self._merge_runs(have, s, local)
                        counters["scripts_merged"] += 1
                        continue
                    v = 1 + max((x.version for x in case.scripts), default=0)
                    atomic_write_bytes(self.case_dir(cid) / f"fix_v{v}.py", code.encode("utf-8"))
                    local[s.version] = v
                    new = s.model_copy(update={"version": v, "file": f"fix_v{v}.py",
                                               "parent": local.get(s.parent) if s.parent else None})
                    case.scripts.append(new)
                    by_sha[_sha(code)] = new
                    counters["scripts_added"] += 1
                if t_case.last_solution and not case.last_solution:
                    case.last_analysis, case.last_solution = t_case.last_analysis, t_case.last_solution
                self._save(case, extra)
            self.recount()
        return counters

    def _merge_runs(self, have: CaseScript, theirs: CaseScript, local: dict[int, int]) -> None:
        """The same script in both KBs: the union of their runs (same record once), lineage / reasons when ours has
        none."""
        def ident(r: ScriptRun) -> tuple:
            return _run_key(r) or (r.run_id, r.round, r.n, r.result, _ts(r.at))
        seen = {ident(r) for r in have.runs}
        runs = have.runs + [r for r in theirs.runs if ident(r) not in seen]
        have.runs = self._cap_runs(sorted(runs, key=lambda r: (_ts(r.at), r.round, r.n)))
        if have.parent is None and theirs.parent and local.get(theirs.parent):
            have.parent = local[theirs.parent]
        have.reason = have.reason or theirs.reason
        have.last_failure = have.last_failure or theirs.last_failure


# --------------------------------------------------------------------------- cases.md
def _cell(text: Any, limit: int = 120) -> str:
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_cases_md(cases: list[tuple[CaseInfo, dict[str, Any]]]) -> str:
    """``cases.md``: one row per case — case · name · projects · seen / resolved · scripts (version status
    successes/failures, v2.1e: ``— <last failure>``) · pin · last result — most widespread first."""
    scripts = [s for c, _ in cases for s in c.scripts]
    by = {k: sum(1 for s in scripts if s.status == k) for k in ("proven", "unverified", "retired")}
    L = ["# KB cases", "",
         f"{len(cases)} case(s), {len(scripts)} script version(s) (proven {by['proven']}, unverified "
         f"{by['unverified']}, retired {by['retired']}); written {utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC. "
         "`sbmig kb cases show <case>` for one case; scripts: successes/failures — last failure.", "",
         "| case | name | projects | seen / resolved | scripts | pin | last result |",
         "|---|---|---|---|---|---|---|"]
    for c, extra in cases:
        L.append(f"| {c.case_id} | {_cell(c.name or c.l2)} | {len(c.projects)} | {c.seen} / {c.resolved} | "
                 f"{_cell(scripts_text(c, failures=True), 320)} | {_cell(extra.get('pin') or '', 60) or '-'} | "
                 f"{_cell(extra.get('last_result') or '', 120) or '-'} |")
    return "\n".join(L) + "\n"


def note_lines(n: Any) -> list[str]:
    """One 수정 note with its as-is / to-be code (``sbmig.errors.common.note_block``, v2.1e); the one-line
    ``note_text`` when that is not there or fails."""
    try:
        from .common import note_block
    except ImportError:
        return [note_text(n)]
    try:
        got = [str(x) for x in (note_block(n) or [])]
    except Exception:  # noqa: BLE001 - a broken renderer: the line alone
        got = []
    return got or [note_text(n)]


def entry_lines(e: ErrorEntry, *, diff: bool = True) -> list[str]:
    """A readable block of one entry and its tries (``sbmig kb errors show``, ``cases show``); each 수정 note with
    its as-is / to-be code (v2.1e)."""
    loc = (e.file or e.module or "-") + (f":{e.line}" if e.file and e.line else "")
    L = [f"entry {e.id}  [{e.status}]  case {e.case_id or '-'}  seen {e.seen or '-'}"
         + (f"  resolved {e.resolved}" if e.resolved else ""),
         f"  run {e.run_id or '-'}  project {e.project or '-'}  kind {e.kind.value}",
         f"  {loc}  {e.message}"]
    if e.l2:
        L.append(f"  l2: {e.l2}")
    if e.note:
        L.append(f"  note: {e.note}")
    if e.script:
        L.append(f"  script: {e.script}")
    if e.raw and e.raw != e.message:
        L += ["  raw:"] + ["    " + ln for ln in e.raw.splitlines()[:15]]
    if e.tries:
        L.append(f"  tries: {tries_text(e)}")
    for t in e.tries:
        L.append(f"  try {t.n} (round {t.round}, {t.source}" + (f" v{t.script_version}" if t.script_version else "")
                 + f", applied {t.applied}, result {t.result}" + (f", commit {t.commit[:10]}" if t.commit else "")
                 + (f", {t.llm_calls} LLM call(s)" if t.llm_calls else "") + (f", {t.seconds:.0f}s" if t.seconds else "")
                 + ")")
        for label, text in (("원인", t.analysis), ("해결", t.solution), ("스크립트 없음", t.no_script_reason),
                            ("note", t.note)):
            if text:
                L.append(f"    {label}: {text}")
        if t.edits:
            L += ["    수정:"] + [f"      {ln}" for x in t.edits for ln in note_lines(x)]
        if t.major_upgrades:                  # v2.1d: major upgrades / library replacements → 운영자 확인
            L += ["    메이저 변경:"] + [f"      {change_line(x)}" for x in t.major_upgrades]
        if t.verify:
            L += ["    운영자 확인:"] + [f"      {x}" for x in guide_lines(t.verify)]
        if t.files:
            L.append(f"    files: {', '.join(t.files)}")
        if diff and t.diff:
            L += ["    diff:"] + ["      " + ln for ln in t.diff.splitlines()]
    return L
