"""Third-party libraries with a known jakarta.* release (knowledge/libraries/jakarta_libraries.yml).

Used by `compose_suggestion` for X1 blocked items (the library behind a javax API jar), by the report's javax API
inventory and, since v2.1d (2026-10-27, docs/INTERFACES.md "v2.1d"), by the dependency version policy: the fix prompt
lists the table entry of a case's library ("Dependency versions", `library_hint_line`) and `sbmig.build.depchanges.
apply_table` marks a major upgrade a person's entry allows.

Two files, one schema: the shipped table (knowledge, redeployed with sbmig; `source="shipped"`) and the people's
table `<kb dir>/libraries.yml` (`libraries.company_file`; `source="company"`, see
knowledge/libraries/libraries.company.example.yml) that operators edit between runs without a redeploy. A company
entry whose key (or one of its `old_keys`) names a shipped entry replaces it; `allow_major: true` lets the LLM cross
the major line to `version`. The company file is read again whenever its mtime changes (a fleet of 500 projects runs
in one process); a broken company file is logged once and ignored — the shipped table still applies.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("sbmig.core.jakarta_libraries")

JAKARTA_LIBRARIES_FILE = Path(__file__).resolve().parents[2] / "knowledge" / "libraries" / "jakarta_libraries.yml"
DEFAULT_COMPANY_FILE = "libraries.yml"


class JakartaLibrary(BaseModel):
    key: str                                  # groupId:artifactId (current coordinates)
    old_keys: list[str] = Field(default_factory=list)   # earlier coordinates (e.g. ch.simas.qlrm:qlrm) → relocated to key
    jakarta_since: str                        # first jakarta.* release
    version: str                              # release the handler sets (>= jakarta_since)
    classifier: Optional[str] = None          # jakarta flavour published as a classifier jar (Ehcache 3: `jakarta`);
                                              # the handler sets it on the bumped / relocated declaration
    requires_java: Optional[int] = None
    note: str = ""
    docs: list[str] = Field(default_factory=list)
    # v2.1d (2026-10-27): a person's entry may allow what the policy forbids by default
    allow_major: bool = False                 # the LLM may move to `version` even when it crosses the major line
    source: Literal["shipped", "company"] = "shipped"   # shipped = knowledge/libraries/jakarta_libraries.yml;
                                              # company = <kb>/libraries.yml (people edit it; no redeploy)

    @property
    def group_id(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def artifact_id(self) -> str:
        return self.key.split(":", 1)[1]

    @property
    def all_keys(self) -> list[str]:
        """Current + earlier coordinates (`groupId:artifactId`)."""
        return [self.key, *self.old_keys]


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[JakartaLibrary, ...]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError:
        return ()
    return tuple(JakartaLibrary.model_validate(e) for e in (doc.get("libraries") or []))


@lru_cache(maxsize=8)
def _load_company(path: str, mtime_ns: int, size: int) -> tuple[JakartaLibrary, ...]:
    """The people's table at `path`; (mtime_ns, size) are only the cache key, so an edited file is read again.
    Never raises: a file that does not parse → warning (once per file version) and (); an entry that does not
    validate → warning and the entry is skipped (the others still count)."""
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        log.warning("company library table %s is not readable YAML (%s) — using the shipped table only", path,
                    str(e).splitlines()[0] if str(e) else type(e).__name__)
        return ()
    if doc is None:
        return ()
    entries = doc.get("libraries") if isinstance(doc, dict) else doc
    if entries is None:
        return ()
    if not isinstance(entries, list):
        log.warning("company library table %s: `libraries` is not a list — using the shipped table only", path)
        return ()
    out: list[JakartaLibrary] = []
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            log.warning("company library table %s: entry %d is not a mapping — skipped", path, i + 1)
            continue
        data = {**raw, "source": "company"}
        if data.get("version") is not None and not data.get("jakarta_since"):
            data["jakarta_since"] = data["version"]            # an approval entry may omit it: `version` is jakarta
        for k in ("version", "jakarta_since"):
            if data.get(k) is not None and not isinstance(data[k], str):
                data[k] = str(data[k])                         # `version: 2.6` in YAML is a float
        try:
            lib = JakartaLibrary.model_validate(data)
        except ValidationError as e:
            log.warning("company library table %s: entry %d (%s) is invalid — skipped: %s", path, i + 1,
                        raw.get("key", "?"), "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                                         for x in e.errors()[:3]))
            continue
        if lib.key.count(":") != 1 or not all(lib.key.split(":")):
            log.warning("company library table %s: entry %d key %r is not groupId:artifactId — skipped", path, i + 1,
                        lib.key)
            continue
        out.append(lib)
    return tuple(out)


def _company_entries(company_path: Optional[str | Path]) -> tuple[JakartaLibrary, ...]:
    if not company_path:
        return ()
    p = Path(company_path)
    try:
        st = p.stat()
    except OSError:
        return ()
    if not p.is_file():
        return ()
    return _load_company(str(p), st.st_mtime_ns, st.st_size)


def merge_tables(shipped: tuple[JakartaLibrary, ...] | list[JakartaLibrary],
                 company: tuple[JakartaLibrary, ...] | list[JakartaLibrary]) -> tuple[JakartaLibrary, ...]:
    """Shipped entries in their order, a company entry in the place of the shipped one it names (its key or one of
    its old_keys among the shipped entry's all_keys); company entries naming nothing shipped are appended."""
    out: list[Optional[JakartaLibrary]] = list(shipped)
    for c in company:
        names = set(c.all_keys)
        hits = [i for i, s in enumerate(out) if s is not None and names & set(s.all_keys)]
        if hits:
            out[hits[0]] = c
            for i in hits[1:]:
                out[i] = None
        else:
            out.append(c)
    return tuple(x for x in out if x is not None)


def jakarta_libraries(path: Optional[str | Path] = None,
                      company_path: Optional[str | Path] = None) -> tuple[JakartaLibrary, ...]:
    """The shipped table (`path`, default knowledge/libraries/jakarta_libraries.yml) merged with the people's table
    `company_path` when it is given and exists (see `merge_tables`)."""
    shipped = _load(str(path or JAKARTA_LIBRARIES_FILE))
    company = _company_entries(company_path)
    return merge_tables(shipped, company) if company else shipped


def jakarta_library_for(key: Optional[str], path: Optional[str | Path] = None,
                        company_path: Optional[str | Path] = None) -> Optional[JakartaLibrary]:
    """The table entry whose current or earlier coordinates equal `key` (None when the library is unknown)."""
    if not key:
        return None
    return next((lib for lib in jakarta_libraries(path, company_path) if key in lib.all_keys), None)


def company_libraries_file(cfg: Any) -> Path:
    """`<kb dir>/<libraries.company_file>` (default `kb-v2/libraries.yml`); an absolute `company_file` wins."""
    libs = getattr(cfg, "libraries", None)
    name = getattr(libs, "company_file", None) or DEFAULT_COMPANY_FILE
    p = Path(str(name)).expanduser()
    if p.is_absolute():
        return p
    kb = getattr(cfg, "kb_dir", None)
    kb = kb() if callable(kb) else kb
    return Path(kb or "kb-v2") / p


_SENTENCE_RE = re.compile(r"^(.+?[.!?])(?=\s|$)", re.DOTALL)


def first_sentence(text: str, limit: int = 300) -> str:
    """The note's first sentence on one line (version dots like `2.6.0` never end it), capped at `limit` chars."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    m = _SENTENCE_RE.match(t)
    s = m.group(1) if m else t
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def library_hint_line(entry: JakartaLibrary, current: Optional[str] = None) -> str:
    """ONE English line for the prompt section "Dependency versions" — what the table says about the library, e.g.
    `opensymphony:sitemesh: approved — raise to 2.6.0 (jakarta since 2.6.0, requires Java 17; SiteMesh 2 got …;
    source: shipped table)`. Never raises."""
    try:
        from sbmig.build.depchanges import major_of
        from sbmig.build.repo_versions import parse_version

        reached = bool(current and "${" not in current and parse_version(current) >= parse_version(entry.version))
        cm, vm = major_of(current), major_of(entry.version)
        crosses = cm is not None and vm is not None and cm < vm and not reached
        if reached:
            head = (f"{entry.key}: current {current} is already at or above the table's {entry.version} — keep it, "
                    f"no version change needed")
        elif entry.allow_major:
            head = f"{entry.key}: approved — a person allowed the major upgrade to {entry.version}"
        elif crosses:
            # "approved" is reserved for a person's allow_major or a same-major pin: a cross-major table version is a
            # fact (the known jakarta release), not an approval — rule 14 decides whether the move is allowed
            head = (f"{entry.key}: the table knows the jakarta release {entry.version} (crosses {cm}.x → {vm}.x — "
                    f"use it only if the repository has no same-major jakarta build, then port what that major "
                    f"changes and write <verify>)")
        else:
            head = f"{entry.key}: approved — raise to {entry.version}"
        tail: list[str] = []
        facts = [f"jakarta since {entry.jakarta_since}"]
        if entry.classifier:
            facts.append(f"classifier {entry.classifier}")
        if entry.requires_java:
            facts.append(f"requires Java {entry.requires_java}")
        tail.append(", ".join(facts))
        note = first_sentence(entry.note)
        if note:
            tail.append(note.rstrip("."))
        tail.append(f"source: {entry.source} table")
        return f"{head} ({'; '.join(tail)})"
    except Exception as e:  # noqa: BLE001 - a prompt line never breaks a fix
        log.debug("library_hint_line(%s): %s", getattr(entry, "key", "?"), e)
        return f"{getattr(entry, 'key', '?')}: table version {getattr(entry, 'version', '?')}"
