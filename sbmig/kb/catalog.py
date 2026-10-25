"""Case catalog (2026-10-23): ONE reusable, human-readable case per error signature.

The error-case events (``kb/errors.jsonl``, ``kb/fix_attempts.jsonl``, see :mod:`sbmig.kb.error_cases`) record every
build error of every run. The catalog folds them into one case per L2 signature (the case key; variants by the owner
class / L1) so people can browse "what is this error, how was it fixed, what failed before", edit it, and so that a
hit applies the case's logic automatically. See docs/INTERFACES.md "Case catalog".

Files (under the KB root)::

    catalog/<error_kind>/<error_class|other>/<slug(symbol)|unknown>--<id>.yml   one CatalogCase (YAML)
    catalog/index.json      {version: 1, updated_at, cases: {id: path relative to catalog/}, by_l2: {sig: id},
                             by_l1: {sig: id}, by_symbol: {symbol: [id]}}   (rebuildable)
    views/cases.md          generated table (one row per case) + how to read / edit (Korean + English)
    views/cases.csv         the same rows for Excel (UTF-8 with BOM)

Rules:
* Everything but the ``human:`` block is recomputed from the events on every update (``update_from``) and on
  ``rebuild`` (``sbmig kb reindex``); an incremental update equals a rebuild because both compute a case from ALL
  its records in file order. The ``human:`` block is never written by the KB: its text (comments included) is kept
  verbatim as long as it parses to the same values; a ``set_human`` / ``sbmig kb cases …`` edit re-renders it
  (comments inside the block are then lost). An invalid ``human:`` block is kept as it is and ignored (warning).
* Reads are cached per process (index by its stat stamp, each case file by its own stamp), so the triage handler can
  create a ``CaseCatalog`` per cluster and a fleet of 500 projects never re-parses an unchanged file.
* File-based on purpose (invariant 3); every write is atomic and runs under the KB lock (``<kb>/.lock``,
  :mod:`sbmig.kb.lock`); ``update_from`` never raises.

Round 10 (2026-10-26):
* evidence: a failure is counted per error record from ITS attempts — an attempt that resolved that error (in its
  ``resolution_attempts`` / ``resolved_by``) or ended ``partial`` / ``inconclusive`` is never a failure for it;
* the learned replace action is the pair set fixed in the most distinct PROJECTS (then records); the handler
  ``case-replace`` reports ``resolver_id = "case-replace:<case id>:<pair-set hash>"`` (:func:`pairset_hash`), so a
  pair set's successes / failures stay with that pair set (a plain ``case-replace`` of older records counts for the
  top-ranked set); a set falls back once ``failures ≥ 2 and failures > successes`` (the next set is tried, then
  patch / hint) and is promoted to ``active`` after ``kb.auto_promote_after_projects`` projects without a failure;
* ``kb/audit.jsonl``: every human edit (``sbmig kb cases …``: who, when, case, before → after) and every automatic
  status / action transition (observed → candidate → active, fallen back) with the evidence counts;
* a hit whose action comes from the human block is ``CaseHit.pinned`` (triage obeys it); a rejected case also stops
  the KB patch replay / KB recipes / symbol hints of its L2 (:meth:`CaseCatalog.rejected_signatures`);
* ``errors.jsonl`` lines that cannot be validated are counted, warned about and noted in ``kb/quarantine.jsonl``;
  ``rebuild`` never deletes a case file whose L2 had such lines.
"""
from __future__ import annotations

import copy
import getpass
import hashlib
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

import yaml

from sbmig import __version__ as AGENT_VERSION
from sbmig.core.models import (
    KB_RECORD_SCHEMA,
    BuildError,
    CaseAction,
    CaseEvent,
    CaseEvidence,
    CaseFailure,
    CaseFix,
    CaseHit,
    CaseHuman,
    CaseVariant,
    CatalogCase,
    ErrorCaseRecord,
    ErrorKind,
    ReplacePair,
    utcnow,
)

from .error_cases import (
    _WHO_PREFIX_RE,
    RESOLVED,
    _cell,
    _norm_failure,
    _project_key,
    _short,
    _write_csv,
    failed_refs,
)
from .harvest import NO_REPLAY_TAG
from .jsonl import append_lines, lenient_fields, line_hash, parse_record, quarantine
from .labels import symbol_for_error
from .lock import kb_lock
from .redact import MASK

log = logging.getLogger("sbmig.kb")

INDEX_VERSION = 1
CATALOG_DIR = "catalog"
VIEWS_DIR = "views"
AUDIT_FILE = "audit.jsonl"
CASE_REPLACE = "case-replace"           # the triage handler applying a case's replace action
FALLBACK_MIN_FAILURES = 2               # a replace pair set falls back at ≥ 2 failures and more failures than successes
STATUS_RANK = {"rejected": -1, "observed": 0, "candidate": 1, "active": 2}
MAX_VARIANTS = 50
MAX_FIXES = 5
MAX_FAILURES = 5
MAX_TIMELINE = 20
MAX_HIT_HINTS = 3
MAX_PATCH_CASES = 5
DESCRIPTION_LIMIT = 300
DEFAULT_PROMOTE_AFTER = 2
_CACHE_LIMIT = 20000                    # parsed case files kept per process (a fleet KB has a few thousand)

GENERATED_MARK = "# ---- generated by sbmig: everything below is recomputed — do not edit below this line ----"
FILE_HEADER = (
    "# sbmig error case — one reusable case per error signature (L2). Edit ONLY the `human:` block (name, status:\n"
    "# observed|candidate|active|rejected, action, hints, notes; it wins over the generated part) or use\n"
    "# `sbmig kb cases reject|pin|unpin|hint|note <id>`. The rest is recomputed from kb/errors.jsonl on every run\n"
    "# and on `sbmig kb reindex`.\n"
    "# 사람이 고치는 곳은 `human:` 블록뿐입니다(이름 / 상태 / 조치 / 힌트 / 메모 — 자동 계산보다 우선). 나머지는\n"
    "# 실행할 때마다, 그리고 `sbmig kb reindex` 때 kb/errors.jsonl 에서 다시 계산됩니다.\n"
)
HUMAN_COMMENT = "  # ← edit here / 여기를 수정"
AUTO_KEY_ORDER = ("name", "status", "action", "hints", "error_kind", "error_class", "symbol", "variants", "evidence",
                  "updated_at")

CASE_CSV_COLUMNS = [
    "id", "name", "status", "action", "seen", "projects", "resolved_by", "latest_fix", "top_failure", "runs",
    "first_at", "last_at", "error_kind", "error_class", "symbol", "signature_generic", "human", "file",
]

_CLASS_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]*")
_SLUG_BAD_RE = re.compile(r"[^A-Za-z0-9._-]+")
_HEX_RE = re.compile(r"[0-9a-f]{3,}")
_LOC_TYPE_RE = re.compile(r"^(?:class|interface|enum|record|@interface)\s+([\w.$]+)")
_LOC_VAR_RE = re.compile(r"^variable \S+ of type ([\w.$]+)")
_LOC_PKG_RE = re.compile(r"^package ([\w.]+)")
_TOP_KEY_RE = re.compile(r"^[^\s#][^:]*:")
_HUMAN_KEY_RE = re.compile(r"^human\s*:")
_UPDATED_LINE_RE = re.compile(r"^updated_at:.*$", re.MULTILINE)
_L2_FIELD = '"signature_generic":"'
_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _Dumper(getattr(yaml, "CSafeDumper", yaml.SafeDumper)):   # type: ignore[misc]
    """Multi-line strings (notes) as `|` blocks — easier to edit by hand than quoted scalars."""


def _str_representer(dumper: Any, value: str) -> Any:
    style = "|" if "\n" in value and not any(ln != ln.rstrip() for ln in value.splitlines()) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str_representer)

# process-wide caches: str(path) → (stamp, value); the stamp includes the inode, so an atomic replace always reloads
_INDEX_CACHE: dict[str, tuple[Any, dict[str, Any]]] = {}
_FILE_CACHE: dict[str, tuple[Any, _Loaded]] = {}
_WARNED_LINES: set[str] = set()         # hashes of unreadable errors.jsonl lines already warned about


# --------------------------------------------------------------------------- pure helpers
def case_id_for(signature_generic: Optional[str]) -> str:
    """CatalogCase id = ``sha1(signature_generic)[:10]``; "" when there is no L2 signature."""
    if not signature_generic:
        return ""
    return hashlib.sha1(signature_generic.encode("utf-8")).hexdigest()[:10]


def split_l2(signature_generic: str) -> tuple[str, str, str]:
    """(kind, error class, symbol) of an L2 signature. The class is the 2nd segment when it is a class token
    (``cannot_find_symbol``, ``package_does_not_exist``, ``build`` …); otherwise ("dependency_resolution|g:a",
    "enforcer|<first words>") the class is "" and the rest is the symbol."""
    parts = (signature_generic or "").split("|")
    kind = parts[0] if parts else ""
    if len(parts) < 2:
        return kind, "", ""
    if _CLASS_TOKEN_RE.fullmatch(parts[1]):
        return kind, parts[1], "|".join(parts[2:])
    return kind, "", "|".join(parts[1:])


def owner_of(location: Optional[str]) -> str:
    """The FQCN (or package) owning the symbol, from a javac ``location:`` line; "" when there is none."""
    loc = " ".join((location or "").split())
    for rx in (_LOC_TYPE_RE, _LOC_VAR_RE, _LOC_PKG_RE):
        m = rx.match(loc)
        if m:
            return re.sub(r"<.*$", "", m.group(1)).rstrip(".")
    return ""


def slug(text: Optional[str], limit: int = 60) -> str:
    s = _SLUG_BAD_RE.sub("-", text or "")
    s = re.sub(r"-{2,}", "-", s).strip("-.")          # "--" separates the slug from the id
    return s[:limit].strip("-.") or "unknown"


def _ts(at: Optional[datetime]) -> datetime:
    if at is None:
        return _EPOCH
    return at if at.tzinfo is not None else at.replace(tzinfo=_UTC)


def _owner_label(owner: str) -> str:
    """Simple class name of an owner FQCN; a package owner stays whole (``org.foo.Bar`` reads like an FQCN)."""
    last = owner.rsplit(".", 1)[-1]
    return last if last[:1].isupper() else owner


def _valid_pair(old: str, new: str) -> bool:
    return (bool(old) and old != new and MASK not in old and MASK not in (new or "") and "\n" not in old
            and "\r" not in old)


def pairset_hash(pairs: Iterable[Any]) -> str:
    """8 hex chars identifying a replace pair set, independent of order and duplicates: sha1 of the sorted unique
    ``old<TAB>new`` lines. ``pairs``: ReplacePair objects or (old, new) tuples; invalid pairs (empty / equal / with a
    newline) are left out — exactly the pairs handler ``case-replace`` can apply."""
    lines: set[str] = set()
    for p in pairs or []:
        old, new = (p.old, p.new) if hasattr(p, "old") else (p[0], p[1])
        new = new or ""
        if old and old != new and "\n" not in old and "\r" not in old:
            lines.add(f"{old}\t{new}")
    return hashlib.sha1("\n".join(sorted(lines)).encode("utf-8")).hexdigest()[:8]


def case_replace_resolver(case_id: str, pairs: Iterable[Any]) -> str:
    """The ``resolver_id`` handler ``case-replace`` reports for one applied case: ``case-replace:<id>:<hash>``."""
    return f"{CASE_REPLACE}:{case_id}:{pairset_hash(pairs)}"


RESOLVER_NOTE = "resolver: "            # a handler note "resolver: case-replace:<id>:<hash>" sets the attempt's resolver
_RESOLVER_NOTE_RE = re.compile(r"^resolver: (case-replace(?::[0-9a-f]*(?::[0-9a-f]*)?)?)\s*$")


def case_replace_note(case_id: str, pairs: Iterable[Any]) -> str:
    """The note handler ``case-replace`` adds (``self.note(...)``) for every case it applied; the error tracker
    turns these notes into the attempt's ``resolver_id`` (comma-joined when several cases were applied)."""
    return RESOLVER_NOTE + case_replace_resolver(case_id, pairs)


def resolver_from_notes(notes: Iterable[str]) -> str:
    """``case-replace:<id>:<hash>[,…]`` from an attempt's notes, "" when there is none."""
    found: list[str] = []
    for n in notes or []:
        m = _RESOLVER_NOTE_RE.match(str(n))
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return ",".join(found)


def parse_case_replace_resolver(resolver_id: Optional[str]) -> list[tuple[str, str]]:
    """(case id, pair-set hash) of every case-replace part of a resolver id (comma-separated when one attempt
    applied several cases); a plain ``case-replace`` (records before round 10) gives ("", "")."""
    out: list[tuple[str, str]] = []
    for part in (resolver_id or "").split(","):
        part = part.strip()
        if part != CASE_REPLACE and not part.startswith(CASE_REPLACE + ":"):
            continue
        _, _, rest = part.partition(":")
        cid, _, h = rest.partition(":")
        out.append((cid, h))
    return out


def _cr_hash(resolver_id: Optional[str], case_id: str) -> Optional[str]:
    """The pair-set hash a case-replace resolver id carries for THIS case ('' = unknown / legacy), None when it is
    not a case-replace of this case."""
    found: Optional[str] = None
    for cid, h in parse_case_replace_resolver(resolver_id):
        if cid and cid != case_id:
            continue
        if h:
            return h
        found = ""
    return found


def _who() -> str:
    try:
        return getpass.getuser() or "unknown"
    except Exception:  # noqa: BLE001 - no user database (containers)
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def _description(text: str) -> str:
    return _short(_WHO_PREFIX_RE.sub("", text or ""), DESCRIPTION_LIMIT)


def pairs_text(pairs: Iterable[ReplacePair], sep: str = ", ") -> str:
    return sep.join(f"`{p.old}` → `{p.new}`" for p in pairs)


def action_text(action: CaseAction) -> str:
    """One-line description of an action (views, CLI)."""
    k = action.kind
    if k == "replace":
        return "replace " + pairs_text(action.replace_pairs) + (" (whole file)" if action.replace_scope == "file"
                                                                  else "")
    if k == "recipe":
        return "recipe " + (", ".join(action.recipe_ids) or "?")
    if k == "patch":
        return "patch " + (", ".join(action.patch_case_ids) or "?")
    if k == "handler":
        return "handler " + (action.handler_id or "?")
    if k == "block":
        out = "block" + (f" ({action.block_reason})" if action.block_reason else "")
        return out + (f": {action.suggestion}" if action.suggestion else "")
    return "hint"


def fix_hint(f: CaseFix) -> str:
    """Same wording as the error hints (``sbmig.kb.error_cases._entry_lines``)."""
    who = " ".join(x for x in (f.method, f.resolver_id) if x)
    body = f"past runs: fixed by {who} in {f.projects} project(s)"
    if f.description:
        body += f": {f.description}"
    if f.replace_pairs:
        body += "; replace " + pairs_text(f.replace_pairs[:3])
    return body


def failure_hint(x: CaseFailure) -> str:
    body = f"past failure ×{x.count}: {x.method} {x.outcome}"
    if x.failure:
        body += f" — {x.failure}"
    return body + "; do not repeat it"


def _stamp(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def _atomic_write(path: Path, text: str) -> None:
    from .store import atomic_write  # store imports this module

    atomic_write(path, text)


def _may_have_l2(line: str, wanted: set[str]) -> bool:
    """Cheap pre-filter of an errors.jsonl line: its ``signature_generic`` value is one of ``wanted`` — or it cannot
    be read without parsing (an escape inside the value, an unusual layout)."""
    i = line.find(_L2_FIELD)
    if i < 0:
        return True
    i += len(_L2_FIELD)
    j = line.find('"', i)
    if j < 0:
        return True
    value = line[i:j]
    return "\\" in value or value in wanted


def _human_segment(text: str) -> Optional[str]:
    """The raw text of the top-level ``human:`` block (comments inside it included), None when there is none."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if _HUMAN_KEY_RE.match(ln)), None)
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.startswith(GENERATED_MARK[:20]) or _TOP_KEY_RE.match(ln):
            end = j
            break
    return "".join(lines[start:end]).rstrip() + "\n"


def _parse_human(segment: Optional[str]) -> tuple[CaseHuman, bool]:
    """(human, valid). No block = an empty, valid human."""
    if segment is None:
        return CaseHuman(), True
    try:
        data = yaml.load(segment, Loader=_Loader)
        raw = (data or {}).get("human") if isinstance(data, dict) else None
        return CaseHuman.model_validate(raw or {}), True
    except Exception as e:  # yaml / validation errors alike
        log.debug("invalid human block: %s", e)
        return CaseHuman(), False


def _prune(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _prune(x) for k, x in v.items() if x is not None and x != "" and x != [] and x != {}}
    if isinstance(v, list):
        return [_prune(x) for x in v]
    return v


def _dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110, default_flow_style=False)


def _human_dict(h: CaseHuman) -> dict[str, Any]:
    action = None
    if h.action is not None:
        action = {"kind": h.action.kind, **h.action.model_dump(mode="json", exclude_defaults=True)}
    return {"name": h.name, "status": h.status, "action": action, "hints": list(h.hints), "notes": h.notes}


def render_human(h: CaseHuman) -> str:
    text = _dump({"human": _human_dict(h)})
    first, _, rest = text.partition("\n")
    return first + HUMAN_COMMENT + "\n" + rest


def render_case(case: CatalogCase, human_text: Optional[str] = None) -> str:
    """The case file: header, id + key, the human block (``human_text`` verbatim when given), the generated part."""
    head = _dump({"id": case.id, "signature_generic": case.signature_generic})
    human = human_text if human_text is not None else render_human(case.human)
    dumped = case.model_dump(mode="json", exclude={"id", "signature_generic", "human"})
    a = case.action                                                # the kind always shows, the defaults do not
    dumped["action"] = {"kind": a.kind, **a.model_dump(mode="json", exclude_defaults=True)}
    auto = {k: dumped[k] for k in AUTO_KEY_ORDER if k in dumped}
    auto.update({k: v for k, v in dumped.items() if k not in auto})
    return FILE_HEADER + head + human + GENERATED_MARK + "\n" + _dump(_prune(auto))


def _without_updated(text: str) -> str:
    return _UPDATED_LINE_RE.sub("", text)


@dataclass
class _Loaded:
    case: Optional[CatalogCase]          # None: the generated part is unreadable
    human: CaseHuman
    human_valid: bool
    human_raw: Optional[str]
    text: str


def _parse_case_text(text: str, path: Path) -> _Loaded:
    seg = _human_segment(text)
    human, valid = _parse_human(seg)
    if not valid:
        log.warning("case %s: the `human:` block is invalid — kept as it is and ignored until fixed", path)
    case: Optional[CatalogCase] = None
    try:
        data = yaml.load(text, Loader=_Loader)
        if isinstance(data, dict) and data.get("id"):
            data = dict(data)
            data["human"] = human.model_dump(mode="json")
            case = CatalogCase.model_validate(data)
    except Exception as e:
        log.warning("case %s is unreadable (%s: %s); `sbmig kb reindex` regenerates it", path, type(e).__name__,
                    str(e).splitlines()[0] if str(e) else "")
    return _Loaded(case=case, human=human, human_valid=valid, human_raw=seg, text=text)


def _empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "updated_at": "", "cases": {}, "by_l2": {}, "by_l1": {}, "by_symbol": {}}


def _index_drop(idx: dict[str, Any], cid: str) -> None:
    idx["cases"].pop(cid, None)
    for table in ("by_l2", "by_l1"):
        for k in [k for k, v in idx[table].items() if v == cid]:
            del idx[table][k]
    for k in list(idx["by_symbol"]):
        ids = [x for x in idx["by_symbol"][k] if x != cid]
        if ids:
            idx["by_symbol"][k] = ids
        else:
            del idx["by_symbol"][k]


def _index_put(idx: dict[str, Any], case: CatalogCase, rel: str) -> None:
    _index_drop(idx, case.id)
    idx["cases"][case.id] = rel
    if case.signature_generic:
        idx["by_l2"][case.signature_generic] = case.id
    for v in case.variants:
        if v.signature:
            idx["by_l1"][v.signature] = case.id
    if case.symbol:
        ids = idx["by_symbol"].setdefault(case.symbol, [])
        if case.id not in ids:
            ids.append(case.id)
            ids.sort()


# --------------------------------------------------------------------------- context (recipes, patch cases)
class _Context:
    """KB recipes and patch cases, read once per update / rebuild through the store's API."""

    def __init__(self, cat: CaseCatalog):
        self.recipes: list[Any] = []
        self.l2_cases: dict[str, list[str]] = {}
        self._kb = cat.kb()
        self._cases: dict[str, Any] = {}
        try:
            self.recipes = [r for r in self._kb.list_recipes() if r.status in ("active", "candidate")]
        except Exception as e:
            log.warning("case catalog: KB recipes unreadable: %s: %s", type(e).__name__, e)
        try:
            self.l2_cases = self._load_l2_cases(cat.root)
        except Exception as e:
            log.warning("case catalog: KB case index unreadable: %s: %s", type(e).__name__, e)

    def _load_l2_cases(self, root: Path) -> dict[str, list[str]]:
        # read index.json directly: the store's _load_index may call reindex, which rebuilds this catalog
        p = root / "index.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("l2"), dict):
                    return {k: list(v) for k, v in data["l2"].items() if isinstance(v, list)}
            except (OSError, ValueError):
                pass
        out: dict[str, list[str]] = {}
        for c in self._kb.iter_cases() or []:
            self._cases[c.id] = c
            for s in c.signatures_generic:
                out.setdefault(s, []).append(c.id)
        return out

    def recipes_for(self, l2: str, l1s: set[str]) -> list[Any]:
        sigs = {l2} | l1s
        hits = [r for r in self.recipes if sigs.intersection(r.signatures)]
        return [r for r in hits if r.status == "active"] + [r for r in hits if r.status != "active"]

    def patch_ids(self, l2: str) -> list[str]:
        out: list[str] = []
        for cid in self.l2_cases.get(l2, []):
            if cid not in self._cases:
                try:
                    self._cases[cid] = self._kb.get_case(cid)
                except Exception:
                    self._cases[cid] = None
            c = self._cases[cid]
            if c is not None and c.fix_kind == "llm_patch" and c.changes and c.outcome in ("improved", "build_ok") \
                    and NO_REPLAY_TAG not in c.tags:
                out.append(c.id)
            if len(out) >= MAX_PATCH_CASES:
                break
        return out


# --------------------------------------------------------------------------- the catalog
class CaseCatalog:
    """``kb/catalog/`` + ``kb/views/`` (see the module doc). ``kb`` (optional): the FileKnowledgeBase whose recipes /
    patch cases feed the actions; by default one is opened on ``kb_dir``."""

    def __init__(self, kb_dir: Union[str, Path], cfg: Any = None, *, kb: Any = None):
        self.root = Path(kb_dir)
        self.cfg = cfg
        self._kb = kb
        kbc = getattr(cfg, "kb", None)
        self.promote_after = int(getattr(kbc, "auto_promote_after_projects", DEFAULT_PROMOTE_AFTER))
        self.replace_scope = getattr(kbc, "case_replace_scope", "error-lines") or "error-lines"
        self.lock_timeout = getattr(kbc, "lock_timeout_seconds", None)
        self.bad_lines: list[dict[str, Any]] = []        # unreadable errors.jsonl lines of the last iter_records
        self._fallen: dict[str, str] = {}               # case id → why its replace action fell back (compute)
        self._audit: list[dict[str, Any]] = []          # transitions not yet written to kb/audit.jsonl

    def lock(self):
        """The cross-process KB lock (``<kb>/.lock``) around a write."""
        return kb_lock(self.root, self.lock_timeout)

    @property
    def audit_path(self) -> Path:
        return self.root / AUDIT_FILE

    # ------------------------------------------------------------------ layout
    @property
    def catalog_dir(self) -> Path:
        return self.root / CATALOG_DIR

    @property
    def index_path(self) -> Path:
        return self.catalog_dir / "index.json"

    @property
    def views_dir(self) -> Path:
        return self.root / VIEWS_DIR

    @property
    def errors_path(self) -> Path:
        return self.root / "errors.jsonl"

    def kb(self) -> Any:
        if self._kb is None:
            from .store import FileKnowledgeBase  # store imports this module

            self._kb = FileKnowledgeBase(self.cfg, kb_dir=self.root)
        return self._kb

    def rel_path(self, case: CatalogCase) -> str:
        kind = case.error_kind.value if isinstance(case.error_kind, ErrorKind) else str(case.error_kind)
        return f"{kind or 'unknown'}/{case.error_class or 'other'}/{slug(case.symbol)}--{case.id}.yml"

    def path_of(self, case_id: str) -> Optional[Path]:
        rel = self._index()["cases"].get(case_id)
        return self.catalog_dir / rel if rel else None

    # ------------------------------------------------------------------ cached reads
    def _key(self, path: Path) -> str:
        return os.path.abspath(path)

    def _index(self) -> dict[str, Any]:
        """The index (cached by its stat stamp); without one, an index scanned from the case files (not saved)."""
        stamp = _stamp(self.index_path)
        if stamp is None:
            return self._scan_index()
        key = self._key(self.index_path)
        cached = _INDEX_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        data: Optional[dict[str, Any]] = None
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == INDEX_VERSION:
                data = _empty_index()
                data.update({k: raw.get(k) or v for k, v in data.items()})
        except (OSError, ValueError) as e:
            log.warning("case catalog index %s unreadable (%s); scanning the case files", self.index_path, e)
        if data is None:
            data = self._scan_index()
        _INDEX_CACHE[key] = (stamp, data)
        return data

    def _scan_index(self) -> dict[str, Any]:
        idx = _empty_index()
        if not self.catalog_dir.exists():
            return idx
        for p in sorted(self.catalog_dir.rglob("*.yml")):
            ld = self._load(p)
            if ld is not None and ld.case is not None:
                _index_put(idx, ld.case, p.relative_to(self.catalog_dir).as_posix())
        return idx

    def _load(self, path: Path) -> Optional[_Loaded]:
        stamp = _stamp(path)
        if stamp is None:
            return None
        key = self._key(path)
        cached = _FILE_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as e:
            log.warning("cannot read case %s: %s", path, e)
            return None
        ld = _parse_case_text(text, path)
        self._remember(path, ld)
        return ld

    def _remember(self, path: Path, ld: _Loaded) -> None:
        if len(_FILE_CACHE) >= _CACHE_LIMIT:
            _FILE_CACHE.clear()
        _FILE_CACHE[self._key(path)] = (_stamp(path), ld)

    def _case(self, case_id: str) -> Optional[CatalogCase]:
        p = self.path_of(case_id)
        ld = self._load(p) if p is not None else None
        return ld.case if ld is not None else None

    # ------------------------------------------------------------------ public reads
    def resolve(self, key: str) -> list[str]:
        """Case ids for a key: id, L2 signature, L1 signature, id prefix (all matches), symbol (all cases)."""
        idx = self._index()
        key = (key or "").strip()
        if not key:
            return []
        if key in idx["cases"]:
            return [key]
        for table in ("by_l2", "by_l1"):
            if key in idx[table]:
                return [idx[table][key]]
        if _HEX_RE.fullmatch(key):
            pref = sorted(i for i in idx["cases"] if i.startswith(key))
            if pref:
                return pref
        return list(idx["by_symbol"].get(key, []))

    def get(self, key: str) -> Optional[CatalogCase]:
        """A case by id, unique id prefix, L2 (or L1) signature or symbol; None when unknown or ambiguous."""
        ids = self.resolve(key)
        return self._case(ids[0]) if len(ids) == 1 else None

    def cases(self) -> list[CatalogCase]:
        """Every readable case, most projects first, then most seen, then id."""
        out = [c for c in (self._case(cid) for cid in self._index()["cases"]) if c is not None]
        out.sort(key=lambda c: (-c.evidence.projects, -c.evidence.seen, c.id))
        return out

    def hint_lines(self, case: CatalogCase, *, human: bool = True, auto: bool = True) -> list[str]:
        prefix = f"[case {case.id}] "
        out: list[str] = []
        for h in [*(case.human.hints if human else []), *(case.hints if auto else [])]:
            line = prefix + " ".join(str(h).split())
            if h and line not in out:
                out.append(line)
        return out

    def match(self, errors: Iterable[BuildError]) -> list[CaseHit]:
        """Cases for these errors (L1 → L2 → error symbol), effective status / action applied; rejected cases
        never hit. One hit per case, errors in their order."""
        errors = [e for e in (errors or []) if e is not None]
        if not errors:
            return []
        try:
            idx = self._index()
            if not idx["cases"]:
                return []
            order: list[str] = []
            groups: dict[str, list[BuildError]] = {}
            owners: dict[str, str] = {}
            cases: dict[str, CatalogCase] = {}
            for e in errors:
                cid, owner = self._match_one(idx, e, cases)
                if not cid:
                    continue
                if cid not in groups:
                    order.append(cid)
                    groups[cid] = []
                groups[cid].append(e)
                if owner and not owners.get(cid):
                    owners[cid] = owner
            hits = []
            for cid in order:
                c = cases[cid]
                hits.append(CaseHit(case_id=cid, name=c.effective_name(), status=c.effective_status(),
                                    action=c.effective_action().model_copy(deep=True), errors=groups[cid],
                                    variant_owner=owners.get(cid, ""), hints=self.hint_lines(c)[:MAX_HIT_HINTS],
                                    pinned=c.human.action is not None))
            return hits
        except Exception as ex:   # matching is best effort: the loop goes on without the catalog
            log.warning("case catalog match failed: %s: %s", type(ex).__name__, ex)
            return []

    def _usable(self, cid: Optional[str], cases: dict[str, CatalogCase]) -> Optional[CatalogCase]:
        if not cid:
            return None
        if cid not in cases:
            c = self._case(cid)
            if c is None or c.effective_status() == "rejected":
                return None
            cases[cid] = c
        return cases[cid]

    def _match_one(self, idx: dict[str, Any], e: BuildError, cases: dict[str, CatalogCase]) -> tuple[str, str]:
        if e.signature and e.signature in idx["by_l1"]:
            c = self._usable(idx["by_l1"][e.signature], cases)
            if c is not None:
                owner = next((v.owner for v in c.variants if v.signature == e.signature), "")
                return c.id, owner
        if e.signature_generic and e.signature_generic in idx["by_l2"]:
            c = self._usable(idx["by_l2"][e.signature_generic], cases)
            if c is not None:
                return c.id, self._variant_owner(c, e)
        sym = symbol_for_error(e)
        ids = idx["by_symbol"].get(sym, []) if sym else []
        cands = [c for c in (self._usable(i, cases) for i in ids) if c is not None]
        if not cands:
            return "", ""
        kind = e.kind.value if isinstance(e.kind, ErrorKind) else str(e.kind)
        cands.sort(key=lambda c: (c.error_kind.value != kind, -c.evidence.projects, -c.evidence.seen, c.id))
        return cands[0].id, self._variant_owner(cands[0], e)

    @staticmethod
    def _variant_owner(c: CatalogCase, e: BuildError) -> str:
        owner = owner_of(e.location)
        return owner if owner and any(v.owner == owner for v in c.variants) else ""

    def rejected_signatures(self, signatures: Iterable[str]) -> set[str]:
        """The given L1 / L2 signatures whose case a person rejected (``sbmig kb cases reject``): the KB match
        then skips their patch replay, KB recipes and symbol hints too. Never raises."""
        out: set[str] = set()
        try:
            idx = self._index()
            if not idx["cases"]:
                return out
            for s in signatures or []:
                cid = idx["by_l2"].get(s) or idx["by_l1"].get(s) if s else None
                if not cid:
                    continue
                c = self._case(cid)
                if c is not None and c.effective_status() == "rejected":
                    out.add(s)
        except Exception as ex:  # noqa: BLE001 - best effort, like match
            log.warning("case catalog rejected lookup failed: %s: %s", type(ex).__name__, ex)
        return out

    # ------------------------------------------------------------------ events
    def _events_exist(self) -> bool:
        st = _stamp(self.errors_path)
        return bool(st and st[1] > 0)

    def iter_records(self, l2s: Optional[set[str]] = None) -> Iterator[ErrorCaseRecord]:
        """Error records of ``errors.jsonl`` (file order, first line of an id wins), only the given L2 signatures
        when ``l2s`` is set (a pre-filter on the raw line keeps a large file cheap). Bulky fields are dropped.
        Lines that cannot be validated are not silently dropped: they land in ``self.bad_lines`` (id / L2 as far
        as readable), one warning per iteration."""
        self.bad_lines = []
        if not self.errors_path.exists():
            return
        wanted = set(l2s) if l2s is not None else None
        seen: set[str] = set()
        try:
            fh = open(self.errors_path, "r", encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("cannot read %s: %s", self.errors_path, e)
            return
        with fh:
            for n, line in enumerate(fh, 1):
                if wanted is not None and not _may_have_l2(line, wanted):
                    continue
                line = line.strip()
                if not line:
                    continue
                r = parse_record(line, ErrorCaseRecord)
                if r is None:
                    info = lenient_fields(line)
                    if wanted is None or not info["signature_generic"] or info["signature_generic"] in wanted:
                        self.bad_lines.append({"line": n, "id": info["id"],
                                               "signature_generic": info["signature_generic"],
                                               "hash": line_hash(line), "reason": "does not validate"})
                    continue
                if r.id in seen:
                    continue
                seen.add(r.id)
                if not r.signature_generic or (l2s is not None and r.signature_generic not in l2s):
                    continue
                if '"schema_version"' not in line:
                    r.schema_version = 1
                r.raw, r.diff_excerpt, r.message, r.problem_errors, r.notes = "", "", "", [], []
                yield r
        new = [b for b in self.bad_lines if b["hash"] not in _WARNED_LINES]
        if new:                                          # once per bad line and process (a fleet refreshes often)
            _WARNED_LINES.update(b["hash"] for b in new)
            log.warning("case catalog: %d line(s) of %s could not be read (ids %s) — left out of the cases, noted in "
                        "%s", len(new), self.errors_path.name, ", ".join(b["id"] or "?" for b in new[:5]),
                        "quarantine.jsonl")

    def _quarantine_bad(self) -> None:
        if self.bad_lines:
            quarantine(self.root, self.errors_path.name, list(self.bad_lines))

    # ------------------------------------------------------------------ compute (pure over the records)
    def compute(self, l2: str, records: list[ErrorCaseRecord], human: Optional[CaseHuman] = None,
                ctx: Optional[_Context] = None) -> CatalogCase:
        """The case of ``l2`` from all its records (file order) — the auto rules of the contract."""
        ctx = ctx if ctx is not None else _Context(self)
        kind_s, cls, symbol = split_l2(l2)
        if records:
            kind = records[0].error_kind
        else:
            try:
                kind = ErrorKind(kind_s)
            except ValueError:
                kind = ErrorKind.UNKNOWN
        if not symbol:
            syms = Counter(r.error_symbol for r in records if r.error_symbol)
            symbol = syms.most_common(1)[0][0] if syms else ""

        variants = self._variants(records)
        cid = case_id_for(l2)
        ev, cr_stats = self._evidence(records, cid)
        l1s = {r.signature for r in records if r.signature} | {v.signature for v in variants if v.signature}
        action, status = self._action(l2, l1s, records, ev, ctx, cr_stats, cid)
        hints = ([fix_hint(ev.fixes[0])] if ev.fixes else []) + ([failure_hint(ev.failures[0])] if ev.failures
                                                                   else [])
        cls_words = (cls or (kind.value if isinstance(kind, ErrorKind) else str(kind))).replace("_", " ")
        top_owner = variants[0].owner if variants else ""
        label = symbol
        if top_owner and symbol and "." not in symbol and " " not in symbol:   # a qualified symbol / package stays
            o = _owner_label(top_owner)
            if symbol != o:
                label = f"{o}.{symbol}"
        name = f"{label} — {cls_words}" if label else (cls_words or l2)
        return CatalogCase(id=cid, name=name, error_kind=kind, error_class=cls, symbol=symbol,
                           signature_generic=l2, variants=variants, status=status, action=action, evidence=ev,
                           hints=hints, human=human or CaseHuman())

    @staticmethod
    def _variants(records: list[ErrorCaseRecord]) -> list[CaseVariant]:
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for r in records:
            g = groups.setdefault((owner_of(r.location), r.signature), {"seen": 0, "pk": set()})
            g["seen"] += 1
            pk = _project_key(r)
            if pk:
                g["pk"].add(pk)
        out = [CaseVariant(owner=o, signature=s, seen=g["seen"], projects=len(g["pk"]))
               for (o, s), g in groups.items()]
        out.sort(key=lambda v: (-v.seen, -v.projects))             # stable: ties keep the first-seen order
        return out[:MAX_VARIANTS]

    def _evidence(self, records: list[ErrorCaseRecord], case_id: str = "") -> tuple[CaseEvidence, dict[str, Any]]:
        """The evidence + per pair-set stats of the case's own replace action (handler ``case-replace``):
        {pair-set hash ('' = a plain legacy resolver id): {"succ", "fail", "pk": [project keys of the successes]}}."""
        pks: list[str] = []
        runs: set[str] = set()
        for r in records:
            pk = _project_key(r)
            if pk and pk not in pks:
                pks.append(pk)
            if r.run_id:
                runs.add(r.run_id)
        ats = [_ts(r.at) for r in records if r.at is not None]

        fixes: dict[tuple, dict[str, Any]] = {}
        failures: dict[tuple, dict[str, Any]] = {}
        cr: dict[str, dict[str, Any]] = {}
        for r in records:
            pk = _project_key(r)
            if r.status == "resolved" and r.fix_method != "none" \
                    and not any(MASK in p.old or MASK in p.new for p in r.replace_pairs):
                key = (r.fix_method, r.resolver_id, tuple((p.old, p.new) for p in r.replace_pairs))
                g = fixes.setdefault(key, {"count": 0, "pk": set(), "latest": None})
                g["count"] += 1
                if pk:
                    g["pk"].add(pk)
                if g["latest"] is None or _ts(r.at) >= _ts(g["latest"].at):
                    g["latest"] = r
            # failed FOR THIS error: an attempt that resolved it (resolution chain) or ended partial / inconclusive
            # is never one, even when its cluster as a whole stayed `no_change` (round 10)
            failed = failed_refs(r)
            for ref in failed:
                key = (ref.fix_method, ref.outcome, _norm_failure(ref.failure))
                g = failures.setdefault(key, {"count": 0, "pk": set(), "latest": None})
                g["count"] += 1
                if pk:
                    g["pk"].add(pk)
                g["latest"] = (ref, r)
            # the case's own replace action (handler case-replace), per applied pair set: a success resolved it, a
            # failure tried it (a failed outcome) and did not
            h = _cr_hash(r.resolver_id, case_id) if r.status == "resolved" and r.fix_method == "handler" else None
            if h is not None:
                s = cr.setdefault(h, {"succ": 0, "fail": 0, "pk": []})
                s["succ"] += 1
                if pk and pk not in s["pk"]:
                    s["pk"].append(pk)
            for fh in {x for x in (_cr_hash(ref.resolver_id, case_id) for ref in failed if ref.fix_method == "handler")
                       if x is not None}:
                cr.setdefault(fh, {"succ": 0, "fail": 0, "pk": []})["fail"] += 1
        succ = sum(s["succ"] for s in cr.values())
        fail = sum(s["fail"] for s in cr.values())
        succ_pk = list(dict.fromkeys(pk for s in cr.values() for pk in s["pk"]))

        fix_rows = []
        for (method, resolver, _), g in fixes.items():
            r = g["latest"]
            fix_rows.append(CaseFix(
                method=method, resolver_id=resolver, change_kind=r.change_kind,
                replace_pairs=[ReplacePair(old=p.old, new=p.new, count=p.count) for p in r.replace_pairs],
                description=_description(r.description), attempt_id=r.resolved_by or "",
                attempt_ids=list(r.resolution_attempts) if len(r.resolution_attempts) > 1 else [], error_id=r.id,
                project=r.project or r.project_fingerprint, at=r.at, count=g["count"], projects=len(g["pk"])))
        fix_rows.sort(key=lambda f: (-f.projects, -f.count))
        fail_rows = []
        for (method, outcome, _), g in failures.items():
            ref, r = g["latest"]
            fail_rows.append(CaseFailure(method=method, outcome=outcome, failure=_short(ref.failure),
                                         attempt_id=ref.attempt_id, project=r.project or r.project_fingerprint,
                                         count=g["count"], projects=len(g["pk"])))
        fail_rows.sort(key=lambda x: (-x.count, -x.projects))

        ordered = sorted(records, key=lambda r: _ts(r.at))          # stable: same-time records keep file order
        timeline = [CaseEvent(at=r.at, project=r.project or r.project_fingerprint, run_id=r.run_id, error_id=r.id,
                              file=r.file or "", status=r.status, fix_method=r.fix_method,
                              attempts=[f"{ref.fix_method}:{ref.outcome}" for ref in r.attempts],
                              resolved_by=r.resolved_by)
                    for r in ordered[-MAX_TIMELINE:]]
        return CaseEvidence(
            seen=len(records), projects=len(pks), runs=len(runs), first_at=min(ats) if ats else None,
            last_at=max(ats) if ats else None, by_status=dict(Counter(r.status for r in records).most_common()),
            resolved_by=dict(Counter(r.fix_method for r in records if r.status in RESOLVED).most_common()),
            fixes=fix_rows[:MAX_FIXES], failures=fail_rows[:MAX_FAILURES], timeline=timeline,
            action_successes=succ, action_failures=fail, action_projects=succ_pk), cr

    def _action(self, l2: str, l1s: set[str], records: list[ErrorCaseRecord], ev: CaseEvidence,
                ctx: _Context, cr_stats: Optional[dict[str, Any]] = None, case_id: str = ""
                ) -> tuple[CaseAction, str]:
        cr_stats = cr_stats or {}
        self._fallen.pop(case_id, None)
        # (1) KB recipes whose signatures contain the L2 or one of the case's L1s
        recipes = ctx.recipes_for(l2, l1s)
        if recipes:
            status = "active" if any(r.status == "active" for r in recipes) else "candidate"
            return CaseAction(kind="recipe", recipe_ids=[r.id for r in recipes]), status
        # (2) learned text replaces (LLM fixes that were pure token substitutions), the set fixed in the most
        # distinct projects first (then records, then first seen) — not the most records of one big project
        sets: dict[tuple, dict[str, Any]] = {}
        for r in records:
            if r.status == "resolved" and r.replace_rule_candidate and r.replace_pairs:
                pairs = tuple(dict.fromkeys((p.old, p.new) for p in r.replace_pairs if _valid_pair(p.old, p.new)))
                if not pairs:
                    continue
                g = sets.setdefault(tuple(sorted(pairs)), {"pairs": pairs, "count": 0, "pk": set(),
                                                           "order": len(sets)})
                g["count"] += 1
                pk = _project_key(r)
                if pk:
                    g["pk"].add(pk)
        ranked = sorted(sets.values(), key=lambda g: (-len(g["pk"]), -g["count"], g["order"]))
        legacy = cr_stats.get("", {"succ": 0, "fail": 0, "pk": []})
        fallen: list[str] = []
        for i, g in enumerate(ranked):
            st = cr_stats.get(pairset_hash(g["pairs"]), {"succ": 0, "fail": 0, "pk": []})
            succ, fail, pks = st["succ"], st["fail"], list(st["pk"])
            if i == 0:                                   # plain `case-replace` (older records): the top-ranked set
                succ, fail = succ + legacy["succ"], fail + legacy["fail"]
                pks += [p for p in legacy["pk"] if p not in pks]
            if fail >= FALLBACK_MIN_FAILURES and fail > succ:
                fallen.append(f"replace {pairs_text(ReplacePair(old=o, new=n) for o, n in g['pairs'])}: "
                              f"{fail} failure(s) > {succ} success(es)")
                continue
            action = CaseAction(kind="replace", replace_scope=self.replace_scope,
                                replace_pairs=[ReplacePair(old=o, new=nw, count=g["count"]) for o, nw in g["pairs"]])
            ev.action_successes, ev.action_failures, ev.action_projects = succ, fail, pks
            if fallen:
                self._fallen[case_id] = "fallen back: " + "; ".join(fallen)
            promoted = self.promote_after > 0 and len(pks) >= self.promote_after and fail == 0
            return action, "active" if promoted else "candidate"
        if fallen:
            self._fallen[case_id] = "fallen back: " + "; ".join(fallen)
        # (3) replayable LLM patches (the existing KB_PATCH step replays them)
        patch_ids = ctx.patch_ids(l2)
        if patch_ids:
            return CaseAction(kind="patch", patch_case_ids=patch_ids), "candidate"
        return CaseAction(kind="hint"), "observed"

    # ------------------------------------------------------------------ writes
    def _note_transition(self, case: CatalogCase, before: Optional[CatalogCase], *, new_ok: bool = True) -> None:
        """Queue an audit line when the generated status / action changed (or a new case starts above observed)."""
        a_after = action_text(case.action)
        if before is None:
            if not new_ok or case.status == "observed":
                return
            b_status, b_action = None, None
        else:
            b_status, b_action = before.status, action_text(before.action)
            if b_status == case.status and b_action == a_after:
                return
        ev = case.evidence
        reason = self._fallen.get(case.id, "")
        if not reason:
            if before is not None and STATUS_RANK.get(case.status, 0) > STATUS_RANK.get(b_status or "", 0):
                reason = "promoted"
            elif before is not None and STATUS_RANK.get(case.status, 0) < STATUS_RANK.get(b_status or "", 0):
                reason = "demoted"
            else:
                reason = "action changed" if before is not None else "created"
        self._audit.append({
            "event": "status" if b_status != case.status else "action", "who": "sbmig", "case_id": case.id,
            "signature_generic": case.signature_generic, "reason": reason,
            "before": {"status": b_status, "action": b_action}, "after": {"status": case.status, "action": a_after},
            "evidence": {"seen": ev.seen, "projects": ev.projects, "action_successes": ev.action_successes,
                         "action_failures": ev.action_failures, "action_projects": len(ev.action_projects)},
        })

    def audit(self, entries: Iterable[dict[str, Any]]) -> int:
        """Append audit lines to ``kb/audit.jsonl`` (``at`` / ``schema_version`` / ``agent_version`` added)."""
        lines = []
        for e in entries or []:
            lines.append(json.dumps({"at": utcnow().isoformat(timespec="seconds"), "schema_version": KB_RECORD_SCHEMA,
                                     "agent_version": AGENT_VERSION, **e}, ensure_ascii=False, default=str))
        if not lines:
            return 0
        try:
            with self.lock():
                return append_lines(self.audit_path, lines)
        except OSError as ex:
            log.warning("case audit not written: %s", ex)
            return 0

    def _flush_audit(self) -> None:
        entries, self._audit = self._audit, []
        self.audit(entries)

    def iter_audit(self, case_id: Optional[str] = None) -> Iterator[dict[str, Any]]:
        """The audit lines (oldest first), only those of ``case_id`` when given."""
        if not self.audit_path.exists():
            return
        with open(self.audit_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and (case_id is None or obj.get("case_id") == case_id):
                    yield obj

    def _write_case(self, idx: dict[str, Any], case: CatalogCase, loaded: Optional[_Loaded],
                    old_rel: Optional[str], *, keep_human_text: bool = True, audit_new: bool = True) -> bool:
        """Write ``case`` (keeping the human block text when it still says the same, or when it is invalid) and
        update ``idx``. ``keep_human_text=False`` (a human edit) always renders the block. Returns True when the
        file content changed. A change of the generated status / action is queued for the audit log."""
        self._note_transition(case, loaded.case if loaded is not None else None, new_ok=audit_new)
        human_text = None
        if keep_human_text and loaded is not None and loaded.human_raw is not None and (
                not loaded.human_valid or loaded.human == case.human):
            human_text = loaded.human_raw
        rel = self.rel_path(case)
        if loaded is not None and loaded.case is not None:
            case = case.model_copy(update={"updated_at": loaded.case.updated_at})
            text = render_case(case, human_text)
            if rel == old_rel and _without_updated(text) == _without_updated(loaded.text):
                _index_put(idx, case, rel)
                return False
        case = case.model_copy(update={"updated_at": utcnow()})
        text = render_case(case, human_text)
        path = self.catalog_dir / rel
        _atomic_write(path, text)
        valid = human_text is None or loaded is None or loaded.human_valid
        self._remember(path, _Loaded(case=case, human=case.human, human_valid=valid,
                                     human_raw=human_text if human_text is not None else _human_segment(text),
                                     text=text))
        if old_rel and old_rel != rel:
            self._unlink(self.catalog_dir / old_rel)
        _index_put(idx, case, rel)
        return True

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            return
        _FILE_CACHE.pop(self._key(path), None)
        d = path.parent
        while d != self.catalog_dir and self.catalog_dir in d.parents:
            try:
                d.rmdir()                       # only when empty
            except OSError:
                break
            d = d.parent

    def _save_index(self, idx: dict[str, Any]) -> None:
        idx["updated_at"] = utcnow().isoformat(timespec="seconds")
        for k in ("cases", "by_l2", "by_l1", "by_symbol"):
            idx[k] = dict(sorted(idx[k].items()))
        _atomic_write(self.index_path, json.dumps(idx, indent=1, ensure_ascii=False) + "\n")
        _INDEX_CACHE[self._key(self.index_path)] = (_stamp(self.index_path), copy.deepcopy(idx))

    def _existing(self, idx: dict[str, Any], cid: str) -> tuple[Optional[_Loaded], Optional[str]]:
        rel = idx["cases"].get(cid)
        if not rel:
            return None, None
        return self._load(self.catalog_dir / rel), rel

    def _human_of(self, ld: Optional[_Loaded]) -> CaseHuman:
        return ld.human if ld is not None and ld.human_valid else CaseHuman()

    def update_from(self, errors: Iterable[ErrorCaseRecord], attempts: Iterable[Any] = ()) -> list[str]:
        """Recompute the cases of these records from ALL their records in ``errors.jsonl`` (+ given records not
        stored yet) and write them; ``attempts`` are not needed (the error records carry their attempts). Without
        an index over existing events the whole catalog is rebuilt first. Returns the ids whose file changed.
        Never raises."""
        try:
            errs = [e for e in (errors or []) if e is not None and e.signature_generic]
            if not errs:
                return []
            l2s: list[str] = []
            for e in errs:
                if e.signature_generic not in l2s:
                    l2s.append(e.signature_generic)
            with self.lock():
                if not self.index_path.exists() and self._events_exist():
                    before = {cid: self._file_text(cid) for cid in map(case_id_for, l2s)}
                    self.rebuild()
                    return [cid for cid in map(case_id_for, l2s) if self._file_text(cid) != before[cid]]
                return self.refresh(l2s, extra=errs)
        except Exception as ex:
            log.warning("case catalog update failed: %s: %s", type(ex).__name__, ex)
            return []

    def _file_text(self, cid: str) -> Optional[str]:
        p = self.path_of(cid)
        ld = self._load(p) if p is not None else None
        return _without_updated(ld.text) if ld is not None else None

    def refresh(self, l2s: Iterable[str], extra: Iterable[ErrorCaseRecord] = ()) -> list[str]:
        """Recompute the cases of these L2 signatures from the events (e.g. after a recipe was promoted by hand).
        Returns the ids whose file changed."""
        l2s = [s for s in dict.fromkeys(l2s or []) if s]
        if not l2s:
            return []
        with self.lock():
            groups: dict[str, list[ErrorCaseRecord]] = {s: [] for s in l2s}
            seen: set[str] = set()
            for r in self.iter_records(set(l2s)):
                groups[r.signature_generic].append(r)
                seen.add(r.id)
            self._quarantine_bad()
            for e in extra or []:
                if e.signature_generic in groups and e.id not in seen:
                    seen.add(e.id)
                    groups[e.signature_generic].append(e)
            ctx = _Context(self)
            idx = copy.deepcopy(self._index())
            changed: list[str] = []
            for l2 in l2s:
                cid = case_id_for(l2)
                loaded, old_rel = self._existing(idx, cid)
                if not groups[l2] and (loaded is None or self._human_of(loaded) == CaseHuman()):
                    continue                       # no events and no human edits: nothing to keep
                case = self.compute(l2, groups[l2], self._human_of(loaded), ctx)
                if self._write_case(idx, case, loaded, old_rel):
                    changed.append(cid)
            if changed or not self.index_path.exists():
                self._save_index(idx)
            self._flush_audit()
        return changed

    def refresh_signatures(self, signatures: Iterable[str]) -> list[str]:
        """Refresh the cases whose L2 or a variant L1 is one of these signatures (recipe promoted / rejected)."""
        idx = self._index()
        l2_of = {cid: sig for sig, cid in idx["by_l2"].items()}
        l2s: list[str] = []
        for s in signatures or []:
            cid = idx["by_l2"].get(s) or idx["by_l1"].get(s)
            if cid and l2_of.get(cid) and l2_of[cid] not in l2s:
                l2s.append(l2_of[cid])
        return self.refresh(l2s) if l2s else []

    def rebuild(self) -> int:
        """Rebuild every case from ``errors.jsonl`` + KB recipes + patch cases (human blocks kept; cases without
        events are kept only when a person edited them — or when ``errors.jsonl`` had lines of their L2, or of an
        unknown L2, that could not be read: those are never deleted). Returns the number of cases."""
        with self.lock():
            existing: dict[str, tuple[_Loaded, str]] = {}
            stale: list[Path] = []
            if self.catalog_dir.exists():
                for p in sorted(self.catalog_dir.rglob("*.yml")):
                    ld = self._load(p)
                    cid = ld.case.id if ld is not None and ld.case is not None else self._id_from_name(p)
                    if ld is None or not cid:
                        continue
                    if cid in existing:
                        stale.append(p)                 # a second file of one case: the first one wins
                        continue
                    existing[cid] = (ld, p.relative_to(self.catalog_dir).as_posix())
            groups: dict[str, list[ErrorCaseRecord]] = {}
            for r in self.iter_records():
                groups.setdefault(r.signature_generic, []).append(r)
            bad = list(self.bad_lines)
            self._quarantine_bad()
            bad_l2s = {b["signature_generic"] for b in bad if b.get("signature_generic")}
            unknown_bad = any(not b.get("signature_generic") for b in bad)
            for cid, (ld, _) in existing.items():
                l2 = ld.case.signature_generic if ld.case is not None else ""
                if l2 and l2 not in groups and self._human_of(ld) != CaseHuman():
                    groups[l2] = []                     # a human-edited case without events stays
            ctx = _Context(self)
            idx = _empty_index()
            keep: set[str] = set()
            for l2, records in groups.items():
                cid = case_id_for(l2)
                ld, rel = existing.get(cid, (None, None))
                case = self.compute(l2, records, self._human_of(ld), ctx)
                self._write_case(idx, case, ld, rel, audit_new=False)
                keep.add(cid)
            for cid, (ld, rel) in existing.items():
                if cid in keep:
                    continue
                l2 = ld.case.signature_generic if ld.case is not None else ""
                if ld.case is not None and (unknown_bad or l2 in bad_l2s):
                    # its events may be among the unreadable lines: keep the file as it is (and in the index)
                    _index_put(idx, ld.case, rel)
                    log.warning("case catalog: case %s kept although none of its events could be read (%s)", cid,
                                "unreadable lines of its L2" if l2 in bad_l2s else "unreadable lines of unknown L2")
                    continue
                self._unlink(self.catalog_dir / rel)
            for p in stale:
                self._unlink(p)
            self.root.mkdir(parents=True, exist_ok=True)
            self._save_index(idx)
            self._flush_audit()
            return len(idx["cases"])

    @staticmethod
    def _id_from_name(p: Path) -> str:
        m = re.search(r"--([0-9a-f]{10})$", p.stem)
        return m.group(1) if m else ""

    def ids_on_disk(self) -> dict[str, Path]:
        """Case id → file for every case file under ``catalog/`` (import merges)."""
        out: dict[str, Path] = {}
        if self.catalog_dir.exists():
            for p in sorted(self.catalog_dir.rglob("*.yml")):
                ld = self._load(p)
                cid = ld.case.id if ld is not None and ld.case is not None else self._id_from_name(p)
                if cid and cid not in out:
                    out[cid] = p
        return out

    def human_of_file(self, path: Path) -> CaseHuman:
        ld = self._load(path)
        return self._human_of(ld)

    # ------------------------------------------------------------------ human edits
    def set_human(self, case_id: str, *, audit_who: Optional[str] = None, audit_note: str = "",
                  **fields: Any) -> CatalogCase:
        """Set fields of the case's ``human`` block (name / status / action / hints / notes; None clears one) and
        rewrite the case file, the index stamp and the views. ``case_id`` may be any key :meth:`get` accepts.
        Every edit is a line of ``kb/audit.jsonl`` (who = ``audit_who`` or the OS user, when, before → after,
        ``audit_note`` = the command). Raises KeyError (unknown / ambiguous case) or ValueError (invalid fields)."""
        allowed = {"name", "status", "action", "hints", "notes"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown human field(s): {', '.join(sorted(bad))} (allowed: {', '.join(sorted(allowed))})")
        ids = self.resolve(case_id)
        if len(ids) != 1:
            raise KeyError(f"ambiguous case key {case_id}: {', '.join(ids[:10])}" if ids else
                           f"unknown case {case_id}")
        cid = ids[0]
        with self.lock():
            idx = copy.deepcopy(self._index())
            loaded, rel = self._existing(idx, cid)
            if loaded is None or loaded.case is None:
                raise ValueError(f"case {cid} is unreadable; run `sbmig kb reindex` first")
            before = self._human_of(loaded)
            data = before.model_dump()
            for k, v in fields.items():
                if k == "action" and isinstance(v, CaseAction):
                    v = v.model_dump()
                if k == "hints" and v is None:
                    v = []
                if k == "notes" and v is None:
                    v = ""
                data[k] = v
            try:
                human = CaseHuman.model_validate(data)
            except ValueError as e:
                raise ValueError(f"invalid human fields for case {cid}: {e}") from e
            case = loaded.case.model_copy(update={"human": human})
            self._write_case(idx, case, loaded, rel, keep_human_text=False)
            self._save_index(idx)
            if before != human:
                self._audit.append({"event": "human", "who": audit_who or _who(), "case_id": cid,
                                    "signature_generic": case.signature_generic, "command": audit_note,
                                    "fields": sorted(fields), "before": _prune(_human_dict(before)),
                                    "after": _prune(_human_dict(human))})
            self._flush_audit()
            try:
                self.write_views()
            except Exception as e:  # the edit itself is stored
                log.warning("case views not written: %s: %s", type(e).__name__, e)
        out = self._case(cid)
        return out if out is not None else case

    # ------------------------------------------------------------------ views
    def view_rows(self) -> list[dict[str, Any]]:
        rows = []
        idx = self._index()
        for c in self.cases():
            st, act = c.effective_status(), c.effective_action()
            latest = max(c.evidence.fixes, key=lambda f: _ts(f.at), default=None)
            fail = c.evidence.failures[0] if c.evidence.failures else None
            latest_txt = ""
            if latest is not None:
                who = " ".join(x for x in (latest.method, latest.resolver_id) if x)
                latest_txt = who + (f": {latest.description}" if latest.description else "")
                if latest.replace_pairs:
                    latest_txt += "; " + pairs_text(latest.replace_pairs[:3])
                when = latest.at.strftime("%Y-%m-%d") if latest.at else ""
                latest_txt += " (" + ", ".join(x for x in (latest.project, when) if x) + ")"
            fail_txt = ""
            if fail is not None:
                fail_txt = f"{fail.method} {fail.outcome} ×{fail.count}" + (f": {fail.failure}" if fail.failure
                                                                            else "")
            rows.append({
                "id": c.id, "name": c.effective_name(),
                "status": st + ("*" if c.human.status else ""),
                "action": action_text(act) + ("*" if c.human.action else ""),
                "seen": c.evidence.seen, "projects": c.evidence.projects,
                "resolved_by": ", ".join(f"{k} {v}" for k, v in c.evidence.resolved_by.items()),
                "latest_fix": latest_txt, "top_failure": fail_txt, "runs": c.evidence.runs,
                "first_at": c.evidence.first_at.isoformat(timespec="seconds") if c.evidence.first_at else "",
                "last_at": c.evidence.last_at.isoformat(timespec="seconds") if c.evidence.last_at else "",
                "error_kind": c.error_kind.value, "error_class": c.error_class, "symbol": c.symbol,
                "signature_generic": c.signature_generic, "human": "yes" if c.human != CaseHuman() else "",
                "file": f"{CATALOG_DIR}/{idx['cases'].get(c.id, '')}",
            })
        return rows

    def write_views(self) -> list[Path]:
        """``views/cases.md`` + ``views/cases.csv`` (UTF-8 with BOM) from the current case files."""
        with self.lock():
            rows = self.view_rows()
            md = self.views_dir / "cases.md"
            _atomic_write(md, render_cases_md(rows, self.promote_after))
            csv_path = _write_csv(self.views_dir / "cases.csv", CASE_CSV_COLUMNS,
                                  ([_cell(r[c]) for c in CASE_CSV_COLUMNS] for r in rows))
        return [md, csv_path]

    # ------------------------------------------------------------------ counts
    def status_counts(self) -> dict[str, int]:
        """Cases by effective status, streamed over the case files (nothing kept in memory or the parse cache)."""
        cnt: Counter = Counter()
        for rel in list(self._index()["cases"].values()):
            p = self.catalog_dir / rel
            key = self._key(p)
            cached = _FILE_CACHE.get(key)
            ld = cached[1] if cached is not None and cached[0] == _stamp(p) else None
            if ld is None:
                try:
                    ld = _parse_case_text(p.read_text(encoding="utf-8-sig"), p)
                except OSError:
                    continue
            if ld.case is not None:
                cnt[ld.case.effective_status()] += 1
        return {s: cnt.get(s, 0) for s in ("observed", "candidate", "active", "rejected")}


def _md(v: Any) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def render_cases_md(rows: list[dict[str, Any]], promote_after: int = DEFAULT_PROMOTE_AFTER) -> str:
    n = promote_after if promote_after > 0 else "N (auto promotion off)"
    lines = [
        "# Error case catalog / 오류 케이스 카탈로그",
        "",
        f"_Generated by sbmig at {utcnow().isoformat(timespec='seconds')} from `kb/errors.jsonl` — do not edit this "
        "file; edit the case YAML instead. / 자동 생성 파일입니다 — 수정은 케이스 YAML 에서 하세요._",
        "",
        "**How to read.** One row per error signature (L2). *status*: `observed` (seen, no reusable fix yet) → "
        "`candidate` (a fix that can be re-applied: learned text replace, KB recipe or replayable patch) → `active` "
        f"(the case's own replace worked in ≥ {n} projects without a failure, or an active KB recipe); `rejected` = "
        "switched off by a person, never applied. *action*: what a hit does at triage — `replace` (token replace on "
        "the reported lines + imports, handler `case-replace`), `recipe` (KB OpenRewrite recipe), `patch` (replayed "
        "LLM patch), `hint` (hints for the fix LLM only), `block` / `handler` (set by a person). `*` = set by a "
        "person. *resolved by*: fix method → resolved errors; *top failure*: the most frequent failed attempt.",
        "",
        "**How to edit.** Change the `human:` block of the case file (the `file` column of `cases.csv`, under `kb/`) "
        "— name, "
        "status, action, hints, notes; it wins over the generated part and is never overwritten — or use "
        "`sbmig kb cases reject|pin|unpin|hint|note <id>`. `sbmig kb cases show <id>` prints the whole story "
        "(timeline, fixes, failures, the latest fix's diff).",
        "",
        "**읽는 법.** 오류 시그니처(L2)마다 한 줄입니다. *status*: `observed`(관찰만, 재사용할 수정 없음) → "
        "`candidate`(다시 적용할 수 있는 수정 있음: 학습된 텍스트 치환, KB 레시피, 재적용 가능한 패치) → `active`"
        f"(케이스의 치환이 {n}개 이상 프로젝트에서 실패 없이 성공, 또는 active KB 레시피); `rejected` = 사람이 끔(적용 "
        "안 함). *action*: 매칭될 때 하는 일 — `replace`(오류 줄 + import 줄의 토큰 치환, 핸들러 `case-replace`), "
        "`recipe`(KB OpenRewrite 레시피), `patch`(LLM 패치 재적용), `hint`(수정 LLM 에 힌트만), `block` / `handler`"
        "(사람이 지정). `*` = 사람이 지정한 값.",
        "",
        "**고치는 법.** 케이스 파일(`cases.csv` 의 `file` 열, `kb/` 아래)의 `human:` 블록(이름 / 상태 / 조치 / 힌트 / "
        "메모)을 수정하거나 `sbmig kb cases reject|pin|unpin|hint|note <id>` 를 사용하세요. 자동 계산 부분보다 "
        "우선하며 덮어쓰지 않습니다. 전체 이력(타임라인, 수정, 실패, 최근 수정의 diff)은 `sbmig kb cases show <id>`.",
        "",
        f"{len(rows)} case(s).",
        "",
        "| id | name | status | action | seen | projects | resolved by | latest fix | top failure |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| `{r['id']}` | {_md(r['name'])} | {_md(r['status'])} | {_md(r['action'])} | {r['seen']} | "
                     f"{r['projects']} | {_md(r['resolved_by'])} | {_md(r['latest_fix'])} | "
                     f"{_md(r['top_failure'])} |")
    if not rows:
        lines.append("| - | _no cases yet_ | | | | | | | |")
    return "\n".join(lines) + "\n"


def clear_caches() -> None:
    """Drop the process-wide parse caches (tests)."""
    _INDEX_CACHE.clear()
    _FILE_CACHE.clear()
    _WARNED_LINES.clear()
