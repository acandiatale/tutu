"""Cross-project learning memory: ``kb/ledger.jsonl`` (append-only ResolutionRecords) and
``kb/symbols.json`` (error-case symbol → how it was fixed, in how many projects, by which case / recipe).

* ``Ledger(path)``: ``append(records)``, ``iter(project=…, symbol=…, label=…, resolution=…,
  signature=…)`` (regex filters), ``summary()`` (what did we learn / what still needs the LLM / what blocks
  most projects) and :func:`render_learning_md` for the markdown version (``sbmig kb learning --md``,
  the fleet's ``learning.md``).
* ``SymbolIndex(path)``: ``add(case)`` (incremental, on harvest), ``rebuild(cases)`` (``kb reindex``),
  ``hints(symbol)`` (first hints in ``KBMatch.hints``).
* ``attempt_record`` / ``blocked_record`` / ``plan_recipe_records`` / ``unresolved_records`` /
  ``advisory_record``: build the ResolutionRecords of a PhaseResult / a finished RunReport.
* ``summary()`` also carries ``error_cases`` (``sbmig.kb.error_cases.ErrorCaseStore.summary`` of the
  ``errors.jsonl`` beside the ledger) and the markdown gets an "Error cases" section from it (2026-10-23), plus
  ``top_cases`` (the case catalog beside the ledger, most projects first) for a short "Top error cases" table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Optional

from sbmig.core.models import (
    AdvisoryFinding,
    BlockedItem,
    BuildError,
    CaseType,
    DecisionKind,
    ErrorKind,
    FixAttempt,
    KBCase,
    Phase,
    PhaseResult,
    ProjectProfile,
    ResolutionRecord,
)

from .jsonl import append_lines, parse_record
from .labels import label_changes, symbol_for_blocked, symbol_for_errors
from .lock import kb_lock

if TYPE_CHECKING:  # pragma: no cover
    from sbmig.core.workspace import Workspace

log = logging.getLogger("sbmig.kb")

SUCCESS_OUTCOMES = ("improved", "build_ok")
FIXED_RESOLUTIONS = ("handler", "plan_recipe", "kb_recipe", "kb_patch", "llm_patch", "optional_bump")
KB_RESOLUTIONS = ("plan_recipe", "kb_recipe", "kb_patch")
_DECISION_RESOLUTION = {
    DecisionKind.HANDLER: "handler",
    DecisionKind.KB_RECIPE: "kb_recipe",
    DecisionKind.KB_PATCH: "kb_patch",
    DecisionKind.LLM_FIX: "llm_patch",
}


def _basename(p: str) -> str:
    return PurePosixPath((p or "").replace("\\", "/")).name


def _files(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        b = _basename(p)
        if b and b not in out:
            out.append(b)
    return out


def project_name(profile: Optional[ProjectProfile], ws: Optional["Workspace"] = None) -> str:
    """Project name for KB/ledger records: the profile name (a workspace copy is always called 'project')."""
    if profile is not None and profile.name:
        return _basename(profile.name)
    if ws is not None:
        return ws.root.name
    return ""


def _record_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _redact(text: Optional[str]) -> str:
    from .redact import redact

    return redact(text or "") or ""


# --------------------------------------------------------------------------- ledger
class Ledger:
    """Append-only jsonl of ResolutionRecords. One json object per line; unreadable lines are skipped. Appends run
    under the KB lock and are torn-line safe (``sbmig.kb.jsonl``: a half-written last line goes to
    ``ledger.jsonl.corrupt`` before the next batch; one fsync per batch)."""

    def __init__(self, path: Path, lock_timeout: Optional[float] = None):
        self.path = Path(path)
        self.lock_timeout = lock_timeout

    def append(self, records: Iterable[ResolutionRecord]) -> int:
        recs = list(records)
        if not recs:
            return 0
        with kb_lock(self.path.parent, self.lock_timeout):
            return append_lines(self.path, [r.model_dump_json() for r in recs])

    def __iter__(self) -> Iterator[ResolutionRecord]:
        return self.iter()

    def iter(
        self,
        *,
        project: Optional[str] = None,
        symbol: Optional[str] = None,
        label: Optional[str] = None,
        resolution: Optional[str] = None,
        signature: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Iterator[ResolutionRecord]:
        """Records matching every given regex (``re.search``; ``label`` matches any of fix_labels)."""
        rx = {
            k: re.compile(v)
            for k, v in (
                ("project", project),
                ("symbol", symbol),
                ("label", label),
                ("resolution", resolution),
                ("signature", signature),
            )
            if v
        }
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                r = parse_record(line, ResolutionRecord)        # a torn line with a whole one glued on: that one
                if r is None:
                    log.warning("skipping unreadable ledger line %s:%d", self.path, n)
                    continue
                if run_id is not None and r.run_id != run_id:
                    continue
                if "project" in rx and not rx["project"].search(r.project):
                    continue
                if "symbol" in rx and not rx["symbol"].search(r.symbol):
                    continue
                if "resolution" in rx and not rx["resolution"].search(r.resolution):
                    continue
                if "signature" in rx and not (
                    rx["signature"].search(r.signature) or rx["signature"].search(r.signature_generic)
                ):
                    continue
                if "label" in rx and not any(rx["label"].search(lb) for lb in r.fix_labels):
                    continue
                yield r

    def count(self) -> int:
        """Complete lines (a torn last line is not a record)."""
        if not self.path.exists():
            return 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for ln in fh if ln.strip() and ln.endswith("\n"))

    def ids(self) -> set[str]:
        return {r.id for r in self.iter()}

    def summary(self, *, run_ids: Optional[Iterable[str]] = None, top: int = 15) -> dict[str, Any]:
        """Aggregate the ledger (optionally restricted to some run ids, e.g. the runs of one fleet)."""
        wanted = set(run_ids) if run_ids is not None else None
        recs = [r for r in self.iter() if wanted is None or r.run_id in wanted]

        def pkey(r: ResolutionRecord) -> str:
            return r.project_fingerprint or r.project

        by_res = Counter(r.resolution for r in recs)
        ok = [
            r
            for r in recs
            if r.resolution in FIXED_RESOLUTIONS
            and (r.outcome in SUCCESS_OUTCOMES or r.resolution == "plan_recipe")
        ]
        # symbols that already have a KB recipe or were fixed by the KB at least once
        recipe_symbols = {
            r.symbol for r in recs if r.symbol and (r.recipe_id or r.resolution in KB_RESOLUTIONS)
        }

        llm: dict[str, dict] = {}
        for r in ok:
            if r.resolution != "llm_patch" or not r.symbol:
                continue
            d = llm.setdefault(
                r.symbol,
                {
                    "symbol": r.symbol,
                    "count": 0,
                    "projects": set(),
                    "labels": Counter(),
                    "replacements": Counter(),
                    "error_kind": r.error_kind.value,
                    "signature_generic": r.signature_generic,
                },
            )
            d["count"] += 1
            d["projects"].add(pkey(r))
            d["labels"].update(r.fix_labels[:1] or ["?"])
            if r.replacement:
                d["replacements"][r.replacement] += 1
        llm_rows = []
        for d in llm.values():
            llm_rows.append(
                {
                    "symbol": d["symbol"],
                    "count": d["count"],
                    "projects": len(d["projects"]),
                    "label": d["labels"].most_common(1)[0][0] if d["labels"] else "",
                    "replacement": d["replacements"].most_common(1)[0][0] if d["replacements"] else "",
                    "error_kind": d["error_kind"],
                    "signature_generic": d["signature_generic"],
                    "has_recipe": d["symbol"] in recipe_symbols,
                }
            )
        llm_rows.sort(key=lambda x: (-x["projects"], -x["count"], x["symbol"]))

        blocked: dict[tuple[str, str], dict] = {}
        for r in recs:
            if r.resolution != "blocked":
                continue
            key = (r.symbol or r.signature_generic or "?", r.resolver_id)
            d = blocked.setdefault(
                key,
                {
                    "symbol": key[0],
                    "reason": key[1],
                    "count": 0,
                    "projects": set(),
                    "project_names": set(),
                    "suggestion": "",
                },
            )
            d["count"] += 1
            d["projects"].add(pkey(r))
            d["project_names"].add(r.project)
            if r.notes and not d["suggestion"]:
                d["suggestion"] = r.notes[0]
        blocked_rows = [
            {
                **{k: v for k, v in d.items() if k not in ("projects", "project_names")},
                "projects": len(d["projects"]),
                "project_names": sorted(d["project_names"]),
            }
            for d in blocked.values()
        ]
        blocked_rows.sort(key=lambda x: (-x["projects"], -x["count"], x["symbol"]))

        fired: dict[str, dict] = {}
        for r in ok:
            if r.resolution not in KB_RESOLUTIONS:
                continue
            rid = r.recipe_id or r.case_id or r.resolver_id
            if not rid:
                continue
            d = fired.setdefault(
                rid, {"id": rid, "kind": r.resolution, "count": 0, "projects": set(), "symbol": r.symbol}
            )
            d["count"] += 1
            d["projects"].add(pkey(r))
            d["symbol"] = d["symbol"] or r.symbol
        fired_rows = [{**d, "projects": len(d["projects"])} for d in fired.values()]
        fired_rows.sort(key=lambda x: (-x["projects"], -x["count"], x["id"]))

        adv: dict[str, dict] = {}
        for r in recs:
            if r.resolution != "advisory":
                continue
            aid = r.notes[1] if len(r.notes) > 1 and r.notes[1].startswith("advisory:") else ""
            aid = aid[len("advisory:"):] if aid else (r.symbol or "?")
            d = adv.setdefault(
                aid,
                {
                    "id": aid,
                    "library": r.symbol,
                    "status": "",
                    "policy": r.resolver_id.split(":", 1)[0],
                    "count": 0,
                    "projects": set(),
                    "project_names": set(),
                    "replaced": 0,
                    "still_present": 0,
                    "replacement": r.replacement,
                },
            )
            d["count"] += 1
            d["projects"].add(pkey(r))
            d["project_names"].add(r.project)
            if r.outcome == "replaced":
                d["replaced"] += 1
            elif r.outcome == "still-present":
                d["still_present"] += 1
            st = next((n[len("status:"):] for n in r.notes if n.startswith("status:")), "")
            d["status"] = d["status"] or st
        adv_rows = [
            {
                **{k: v for k, v in d.items() if k not in ("projects", "project_names")},
                "projects": len(d["projects"]),
                "project_names": sorted(d["project_names"]),
            }
            for d in adv.values()
        ]
        adv_rows.sort(key=lambda x: (-x["projects"], -x["count"], x["id"]))

        unresolved = Counter(r.symbol or r.signature_generic for r in recs if r.resolution == "unresolved")
        labels = Counter(lb for r in ok for lb in r.fix_labels[:1])
        out = {
            "records": len(recs),
            "projects": len({pkey(r) for r in recs}),
            "runs": len({r.run_id for r in recs if r.run_id}),
            "by_resolution": dict(sorted(by_res.items(), key=lambda kv: -kv[1])),
            "fixed_by_label": dict(labels.most_common()),
            "llm_symbols": llm_rows[:top],
            "llm_symbols_without_recipe": [x for x in llm_rows if not x["has_recipe"] and x["count"] >= 2][
                :top
            ],
            "blocked": blocked_rows[:top],
            "kb_fired": fired_rows[:top],
            "unresolved": [{"symbol": s, "count": n} for s, n in unresolved.most_common(top)],
            "advisories": adv_rows[:top],
        }
        # error cases (2026-10-23) live next to the ledger (kb/errors.jsonl): the learning report — the fleet's
        # learning.md included — gets their section without its callers having to know about a second file
        ec = self.error_cases_summary(run_ids=wanted, top=top)
        if ec:
            out["error_cases"] = ec
            cases = self.top_cases(top=min(top, 10))
            if cases:
                out["top_cases"] = cases
        return out

    def top_cases(self, top: int = 10) -> list[dict[str, Any]]:
        """The most widespread cases of the case catalog beside this ledger (``kb/catalog/``), [] without one."""
        try:
            from .catalog import CaseCatalog, action_text

            cat = CaseCatalog(self.path.parent)
            if not cat.index_path.exists():
                return []
            return [{"id": c.id, "name": c.effective_name(), "status": c.effective_status(),
                     "action": action_text(c.effective_action()), "projects": c.evidence.projects,
                     "seen": c.evidence.seen} for c in cat.cases()[:top]]
        except Exception as e:  # pragma: no cover - the learning report must not fail on it
            log.warning("case catalog summary failed: %s: %s", type(e).__name__, e)
            return []

    def error_cases_summary(self, *, run_ids: Optional[Iterable[str]] = None, top: int = 15) -> dict[str, Any]:
        """``ErrorCaseStore.summary`` of the ``errors.jsonl`` beside this ledger, {} when there are none."""
        errors_path = self.path.parent / "errors.jsonl"
        if not errors_path.exists():
            return {}
        try:
            from .error_cases import ErrorCaseStore

            ec = ErrorCaseStore(self.path.parent).summary(run_ids=run_ids, top=top)
            return ec if ec.get("errors") else {}
        except Exception as e:  # pragma: no cover - the learning report must not fail on it
            log.warning("error-case summary failed: %s: %s", type(e).__name__, e)
            return {}


def _md_cell(v: Any) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


_EC_METHODS = ("handler", "recipe", "kb_patch", "llm", "plan_step", "none")
_EC_STATUSES = ("resolved", "resolved_indirectly", "blocked", "unresolved", "not_visible")


def _error_cases_md(ec: dict[str, Any]) -> list[str]:
    """The "Error cases" section (ErrorCaseStore.summary): status × method, top failure reasons, text-replace
    candidates."""
    lines = ["## Error cases", ""]
    lines.append(
        f"{ec.get('errors', 0)} build error(s) recorded one by one from {ec.get('projects', 0)} project(s) / "
        f"{ec.get('runs', 0)} run(s), {ec.get('attempts', 0)} fix attempt(s); "
        f"{ec.get('llm_text_replace', 0)} LLM fix(es) were pure text replaces."
    )
    if ec.get("by_status"):
        lines.append("Status: " + ", ".join(f"{k} {v}" for k, v in ec["by_status"].items()) + ".")
    if ec.get("by_method"):
        lines.append("Resolved by: " + ", ".join(f"{k} {v}" for k, v in ec["by_method"].items()) + ".")
    matrix = ec.get("status_method") or {}
    if matrix:
        methods = [m for m in _EC_METHODS if any(m in row for row in matrix.values())]
        methods += sorted({m for row in matrix.values() for m in row} - set(methods))
        statuses = [st for st in _EC_STATUSES if st in matrix] + sorted(set(matrix) - set(_EC_STATUSES))
        lines += [
            "",
            "Status × method (resolved: the method that fixed it; otherwise the method of the last attempt, "
            "none = never attempted):",
            "",
            "| status | " + " | ".join(methods) + " |",
            "|---|" + "---|" * len(methods),
        ]
        lines += [
            f"| {st} | " + " | ".join(str(matrix[st].get(m, 0)) for m in methods) + " |" for st in statuses
        ]
    lines += ["", "### Top failure reasons", ""]
    if ec.get("top_failure_reasons"):
        lines += ["| L2 signature | errors | projects | why not resolved |", "|---|---|---|---|"]
        lines += [
            f"| {_md_cell(r['signature_generic'])} | {r['count']} | {r['projects']} | {_md_cell(r['reason'])} |"
            for r in ec["top_failure_reasons"]
        ]
    else:
        lines.append("_none_")
    lines += [
        "",
        "### LLM fixes that were only text replaces — candidates for a deterministic rule",
        "",
    ]
    if ec.get("replace_candidates"):
        lines += ["| L2 signature | replace | errors | projects | example |", "|---|---|---|---|---|"]
        for r in ec["replace_candidates"]:
            repl = f"`{r['old']}` → `{r['new']}`"
            lines.append(
                f"| {_md_cell(r['signature_generic'])} | {_md_cell(repl)} | {r['count']} | {r['projects']} | "
                f"{_md_cell(r['example_file'])} |"
            )
    else:
        lines.append("_none_")
    return lines + [""]


def render_learning_md(
    summary: dict[str, Any],
    stats: Optional[dict[str, Any]] = None,
    title: str = "KB learning report",
    extra: Optional[dict[str, Any]] = None,
    error_cases: Optional[dict[str, Any]] = None,
) -> str:
    """Markdown 'what did we learn / what still needs the LLM / what blocks most projects'. ``error_cases``
    (``ErrorCaseStore.summary``; default: ``summary["error_cases"]`` from :meth:`Ledger.summary`) adds the
    "Error cases" section when it has records."""
    s = summary
    ec = error_cases if error_cases is not None else s.get("error_cases")
    lines = [f"# {title}", ""]
    lines.append(f"{s['records']} ledger record(s) from {s['projects']} project(s) / {s['runs']} run(s).")
    if s["by_resolution"]:
        lines.append("Resolutions: " + ", ".join(f"{k} {v}" for k, v in s["by_resolution"].items()) + ".")
    lines.append("")
    if extra:
        lines += ["## This fleet", ""]
        for k, v in extra.items():
            lines.append(f"* {k}: {', '.join(map(str, v)) if isinstance(v, (list, tuple)) else v}")
        lines.append("")
    if stats:
        lines += [
            "## Knowledge base",
            "",
            f"* cases: {stats.get('cases', 0)} (reused {stats.get('cases_reused', 0)})",
            f"* recipes: active {stats.get('recipes_active', 0)}, "
            f"candidate {stats.get('recipes_candidate', 0)}"
            f", rejected {stats.get('recipes_rejected', 0)}",
            f"* symbols indexed: {stats.get('symbols', 0)}, ledger entries: {stats.get('ledger_entries', 0)}",
            "",
        ]
    lines += [
        "## Write a recipe / handler for these",
        "",
        "Symbols the LLM fixed at least twice and that still have no KB recipe — each one costs an LLM call "
        "in every further project.",
        "",
    ]
    rows = s["llm_symbols_without_recipe"]
    if rows:
        lines += [
            "| symbol | fixed | projects | label | replacement | L2 signature |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {_md_cell(r['symbol'])} | {r['count']} | {r['projects']} | {_md_cell(r['label'])} | "
            f"{_md_cell(r['replacement'])} | {_md_cell(r['signature_generic'])} |"
            for r in rows
        ]
    else:
        lines.append("_none_")
    lines += ["", "## Still resolved by the LLM", ""]
    if s["llm_symbols"]:
        lines += [
            "| symbol | fixed | projects | label | replacement | KB recipe? |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {_md_cell(r['symbol'])} | {r['count']} | {r['projects']} | {_md_cell(r['label'])} | "
            f"{_md_cell(r['replacement'])} | {'yes' if r['has_recipe'] else 'no'} |"
            for r in s["llm_symbols"]
        ]
    else:
        lines.append("_none_")
    lines += ["", "## Blocking most projects (library owners / infra)", ""]
    if s["blocked"]:
        lines += [
            "| symbol / artifact | reason | projects | count | blocked projects | suggestion |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {_md_cell(r['symbol'])} | {_md_cell(r['reason'])} | {r['projects']} | {r['count']} | "
            f"{_md_cell(', '.join(r['project_names'][:8]))} | {_md_cell(r['suggestion'][:160])} |"
            for r in s["blocked"]
        ]
    else:
        lines.append("_none_")
    lines += ["", "## Libraries needing owner action (advisories)", ""]
    if s.get("advisories"):
        lines += [
            "| advisory | library | status | policy | projects | replaced | still present | replacement | "
            "projects (first 8) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {_md_cell(r['id'])} | {_md_cell(r['library'])} | {_md_cell(r['status'])} | "
            f"{_md_cell(r['policy'])} | {r['projects']} | {r['replaced']} | {r['still_present']} | "
            f"{_md_cell(r['replacement'])} | {_md_cell(', '.join(r['project_names'][:8]))} |"
            for r in s["advisories"]
        ]
    else:
        lines.append("_none_")
    lines += ["", "## KB recipes / patches that paid off", ""]
    if s["kb_fired"]:
        lines += ["| recipe / case | kind | projects | count | symbol |", "|---|---|---|---|---|"]
        lines += [
            f"| {_md_cell(r['id'])} | {r['kind']} | {r['projects']} | {r['count']} | "
            f"{_md_cell(r['symbol'])} |"
            for r in s["kb_fired"]
        ]
    else:
        lines.append("_none_")
    if ec and ec.get("errors"):
        lines += [""] + _error_cases_md(ec)[:-1]
        if s.get("top_cases"):
            lines += ["", "### Top error cases (case catalog, whole KB — `sbmig kb cases show <id>`)", "",
                      "| case | name | status | action | projects | seen |", "|---|---|---|---|---|---|"]
            lines += [f"| `{r['id']}` | {_md_cell(r['name'])} | {r['status']} | {_md_cell(r['action'])} | "
                      f"{r['projects']} | {r['seen']} |" for r in s["top_cases"]]
    if s["unresolved"]:
        lines += ["", "## Unresolved at the end of a run", "", "| symbol / signature | count |", "|---|---|"]
        lines += [f"| {_md_cell(r['symbol'])} | {r['count']} |" for r in s["unresolved"]]
    if s["fixed_by_label"]:
        lines += ["", "## Fixes by label", "", ", ".join(f"{k} {v}" for k, v in s["fixed_by_label"].items())]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- symbol index
class SymbolIndex:
    """``symbols.json``: {symbol: {error_kind, generic_signatures, fixes: [{labels, replacement, projects,
    project_keys, cases, recipe_id}]}}. Fixes are keyed by (labels, replacement)."""

    def __init__(self, path: Path, lock_timeout: Optional[float] = None):
        self.path = Path(path)
        self.lock_timeout = lock_timeout
        self._data: Optional[dict[str, dict]] = None

    def load(self) -> dict[str, dict]:
        if self._data is None:
            data: dict = {}
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    log.warning("symbols index unreadable (%s); starting empty", e)
                    data = {}
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def save(self) -> None:
        """Write the index (atomic, under the KB lock). Load → add → save of one update belong in ONE locked
        section of the caller (another process may have written the file since it was loaded)."""
        from .store import atomic_write

        with kb_lock(self.path.parent, self.lock_timeout):
            data = dict(sorted(self.load().items()))
            atomic_write(self.path, json.dumps(data, indent=1, ensure_ascii=False) + "\n")

    def get(self, symbol: str) -> Optional[dict]:
        return self.load().get(symbol) if symbol else None

    def __len__(self) -> int:
        return len(self.load())

    def add(self, case: KBCase, project_key: str = "") -> bool:
        """Merge a case into the index (idempotent). Returns False when the case has no symbol."""
        if not case.symbol:
            return False
        data = self.load()
        ent = data.setdefault(
            case.symbol, {"error_kind": case.error_kind.value, "generic_signatures": [], "fixes": []}
        )
        for s in case.signatures_generic:
            if s not in ent["generic_signatures"]:
                ent["generic_signatures"].append(s)
        labels = list(case.fix_labels)
        fix = next(
            (
                f
                for f in ent["fixes"]
                if f.get("labels") == labels and f.get("replacement", "") == case.replacement
            ),
            None,
        )
        if fix is None:
            fix = {
                "labels": labels,
                "replacement": case.replacement,
                "projects": 0,
                "project_keys": [],
                "cases": [],
                "recipe_id": None,
            }
            ent["fixes"].append(fix)
        keys = fix.setdefault("project_keys", [])
        for k in [project_key, case.project_fingerprint or case.project, *case.reuse_projects]:
            if k and k not in keys:
                keys.append(k)
        fix["projects"] = len(keys)
        if case.id and case.id not in fix["cases"]:
            fix["cases"].append(case.id)
        if case.recipe_candidate_id:
            fix["recipe_id"] = case.recipe_candidate_id
        ent["fixes"].sort(key=lambda f: (-f.get("projects", 0), -len(f.get("cases", []))))
        return True

    def rebuild(self, cases: Iterable[KBCase]) -> int:
        self._data = {}
        for c in cases:
            self.add(c)
        self.save()
        return len(self._data)

    def hints(self, symbol: str, limit: int = 2) -> list[str]:
        ent = self.get(symbol)
        if not ent:
            return []
        out = []
        for f in ent.get("fixes", [])[:limit]:
            labels = ", ".join(f.get("labels") or []) or "a fix"
            repl = f": {f['replacement']}" if f.get("replacement") else ""
            rec = f" (recipe {f['recipe_id']})" if f.get("recipe_id") else ""
            cases = f" [cases {', '.join(f.get('cases', [])[:3])}]" if f.get("cases") and not rec else ""
            out.append(
                f"[symbol {symbol}] fixed in {f.get('projects', 0)} project(s) by {labels}{repl}{rec}{cases}"
            )
        return out


# --------------------------------------------------------------------------- record builders
def _base(profile: Optional[ProjectProfile], ws: Optional["Workspace"], phase: Phase) -> dict[str, Any]:
    return {
        "project": project_name(profile, ws),
        "project_fingerprint": profile.fingerprint if profile else "",
        "run_id": getattr(ws, "run_id", "") or "",
        "case_type": profile.case_type if profile else CaseType.UNKNOWN,
        "phase": phase,
    }


def _err_fields(errors: list[BuildError], sigs: list[str]) -> dict[str, Any]:
    l1 = next((s for s in sigs if s), "") or next((e.signature for e in errors if e.signature), "")
    l2 = next((e.signature_generic for e in errors if e.signature_generic), "")
    kind = errors[0].kind if errors else ErrorKind.UNKNOWN
    if not errors and l1:
        try:
            kind = ErrorKind(l1.split("|", 1)[0])
        except ValueError:
            pass
    return {"signature": l1, "signature_generic": l2, "error_kind": kind}


def attempt_record(
    a: FixAttempt,
    profile: Optional[ProjectProfile],
    ws: Optional["Workspace"],
    phase: Phase,
    case_id: Optional[str] = None,
    recipe_id: Optional[str] = None,
) -> Optional[ResolutionRecord]:
    res = _DECISION_RESOLUTION.get(a.decision.kind)
    if res is None:
        return None
    lab = label_changes(a.changes, a.cluster_errors) if a.changes else None
    base = _base(profile, ws, phase)
    if res == "handler":
        resolver = a.decision.handler_id or ""
    elif res == "kb_recipe":
        resolver = ",".join(a.decision.recipe_ids)
        recipe_id = recipe_id or (a.decision.recipe_ids[0] if a.decision.recipe_ids else None)
    elif res == "kb_patch":
        resolver = ",".join(a.decision.kb_case_ids)
        case_id = case_id or (a.decision.kb_case_ids[0] if a.decision.kb_case_ids else None)
    else:
        resolver = a.model or ""
    return ResolutionRecord(
        id=_record_id(base["run_id"], phase.value, a.id, a.cluster_id, res),
        **base,
        **_err_fields(a.cluster_errors, a.cluster_signatures),
        symbol=(lab.symbol if lab else "") or symbol_for_errors(a.cluster_errors),
        resolution=res,
        resolver_id=resolver,
        fix_labels=lab.labels if lab else [],
        replacement=_redact(lab.replacement) if lab else "",
        outcome=a.outcome,
        files=_files([c.path for c in a.changes]),
        case_id=case_id,
        recipe_id=recipe_id,
        model=a.model if res == "llm_patch" else "",
        # round 6: the library advisories whose hints were in the prompt ("advisory:<id>", the same token the
        # resolution="advisory" lines carry in notes[1]) — free-form notes, so the ledger model stays unchanged
        notes=[f"advisory:{x}" for x in a.advisory_ids],
    )


def blocked_record(
    b: BlockedItem,
    profile: Optional[ProjectProfile],
    ws: Optional["Workspace"],
    phase: Optional[Phase] = None,
) -> ResolutionRecord:
    from .harvest import _strip_roots, redact

    base = _base(profile, ws, phase or b.phase)
    notes = [(redact(_strip_roots(b.suggestion, ws)) or "")[:300]] if b.suggestion else []
    if b.required_type or b.found_type:
        notes.append(f"required {b.required_type or '?'}, found {b.found_type or '?'}")
    return ResolutionRecord(
        id=_record_id(base["run_id"], "blocked", b.id),
        **base,
        **_err_fields(b.errors, b.signatures),
        symbol=symbol_for_blocked(b),
        resolution="blocked",
        resolver_id=b.reason.value,
        outcome="blocked",
        files=_files(b.files),
        notes=notes,
    )


def plan_recipe_records(
    pr: PhaseResult, kb: Any, profile: Optional[ProjectProfile], ws: Optional["Workspace"]
) -> list[ResolutionRecord]:
    """KB recipes (active/candidate) that changed files in this phase's OpenRewrite runs."""
    out: list[ResolutionRecord] = []
    applied: dict[str, list[str]] = {}
    for rr in pr.rewrite_results:
        for rid, files in (getattr(rr, "recipes_applied", None) or {}).items():
            if files:
                applied.setdefault(rid, []).extend(files)
    if not applied:
        return out
    base = _base(profile, ws, pr.phase)
    for rid, files in applied.items():
        r = kb.get_recipe(rid)
        if r is None:
            continue
        symbol, labels, repl, sig2 = "", [], "", ""
        for cid in r.source_case_ids:
            c = kb.get_case(cid)
            if c is not None and c.symbol:
                symbol, labels, repl = c.symbol, list(c.fix_labels), c.replacement
                sig2 = next(iter(c.signatures_generic), "")
                break
        out.append(
            ResolutionRecord(
                id=_record_id(base["run_id"], pr.phase.value, "plan", rid),
                **base,
                signature=next(iter(r.signatures), ""),
                signature_generic=sig2 or next((s for s in r.signatures if s.count("|") == 2), ""),
                symbol=symbol,
                resolution="plan_recipe",
                resolver_id=rid,
                fix_labels=labels,
                replacement=repl,
                outcome="build_ok" if pr.build_ok else "no_change",
                files=_files(files)[:20],
                recipe_id=rid,
                notes=[f"{r.status} KB recipe ran in the plan's OpenRewrite step"],
            )
        )
    return out


def unresolved_records(
    errors: list[BuildError],
    profile: Optional[ProjectProfile],
    ws: Optional["Workspace"],
    phase: Phase = Phase.VERIFY,
) -> list[ResolutionRecord]:
    base = _base(profile, ws, phase)
    groups: dict[str, list[BuildError]] = defaultdict(list)
    for e in errors:
        groups[e.signature or e.signature_generic or e.message[:80]].append(e)
    out = []
    for sig, errs in groups.items():
        out.append(
            ResolutionRecord(
                id=_record_id(base["run_id"], "unresolved", sig),
                **base,
                **_err_fields(errs, [sig]),
                symbol=symbol_for_errors(errs),
                resolution="unresolved",
                outcome="unresolved",
                files=_files([e.file for e in errs if e.file])[:10],
                notes=[f"{len(errs)} error(s)"],
            )
        )
    return out


def advisory_record(
    finding: AdvisoryFinding,
    profile: Optional[ProjectProfile],
    ws: Optional["Workspace"],
) -> ResolutionRecord:
    """One ledger line per library advisory finding of a run (resolution="advisory"). notes[0] = follow-up,
    notes[1] = "advisory:<id>", notes[2] = "status:<status>" (read back by Ledger.summary)."""
    f = finding
    base = _base(profile, ws, Phase.ANALYZE)
    return ResolutionRecord(
        id=_record_id(base["run_id"], "advisory", f.advisory_id),
        **base,
        error_kind=ErrorKind.UNKNOWN,
        symbol=f.library,
        resolution="advisory",
        resolver_id=f"{f.policy}:{','.join(f.recipes) or '-'}",
        fix_labels=["dependency-replace"] if f.outcome == "replaced" else [],
        replacement=f"{f.library} → {f.replacement or '?'}",
        outcome=f.outcome,
        recipe_id=f.recipes[0] if f.recipes else None,
        notes=[(f.followup or "")[:300], f"advisory:{f.advisory_id}", f"status:{f.status}"],
    )
