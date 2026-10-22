"""Context packing for the LLM fixer: errors, file windows, pom excerpts, library migration guidance (advisories),
migration notes, history — within a budget.

Round 9: module-merge notes (`FixContext.reference_notes`) and the dropped first-wins copies as read-only reference text
(`FixContext.reference_files`): never part of the build, never a SEARCH/REPLACE target (the loop drops such patches)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

import yaml

from sbmig.core.config import Config
from sbmig.core.interfaces import FixContext
from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, FixAttempt
from sbmig.core.workspace import Workspace

log = logging.getLogger("sbmig.fix.context")

WHOLE_FILE_MAX_LINES = 400
WINDOW_RADIUS = 60
POM_WHOLE_MAX_LINES = 250
DEFAULT_MAX_NOTES = 4
POM_KINDS = {ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION,
             ErrorKind.POM_INVALID, ErrorKind.ENFORCER}
POM_MESSAGE_RE = re.compile(r"package \S+ does not exist|cannot access|class file for \S+ not found|"
                            r"Could not (resolve|find artifact)|non-resolvable", re.IGNORECASE)
_LANG = {".java": "java", ".xml": "xml", ".kt": "kotlin", ".yml": "yaml", ".yaml": "yaml",
         ".properties": "properties", ".gradle": "groovy", ".jsp": "jsp", ".json": "json", ".sql": "sql"}


# ---------------------------------------------------------------------------
# notes index
# ---------------------------------------------------------------------------
@dataclass
class NoteEntry:
    file: str
    title: str
    keywords: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    always: bool = False
    path: Optional[Path] = None
    _compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def compiled(self) -> list[re.Pattern]:
        if not self._compiled and self.keywords:
            for k in self.keywords:
                try:
                    self._compiled.append(re.compile(k))
                except re.error:
                    self._compiled.append(re.compile(re.escape(k)))
        return self._compiled

    def score(self, text: str) -> int:
        return sum(1 for rx in self.compiled() if rx.search(text))

    def applies(self, case: Optional[str], phase: Optional[str]) -> bool:
        if self.cases and case and case not in self.cases:
            return False
        if self.phases and phase and phase not in self.phases:
            # the final verify loop fixes the restructured tree: notes written for `restructure` apply there too
            if not (phase == "verify" and "restructure" in self.phases):
                return False
        return True

    def body(self) -> str:
        if not self.path or not self.path.exists():
            return ""
        return strip_front_matter(self.path.read_text(encoding="utf-8")).strip()


def split_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def strip_front_matter(text: str) -> str:
    return split_front_matter(text)[1]


def load_notes_index(notes_dir: str | Path) -> list[NoteEntry]:
    """Read `index.yml` (falls back to front-matter of every *.md when the index is missing)."""
    d = Path(notes_dir)
    idx = d / "index.yml"
    entries: list[NoteEntry] = []
    if idx.exists():
        data = yaml.safe_load(idx.read_text(encoding="utf-8")) or {}
        raw = data.get("notes", data) if isinstance(data, dict) else data
        for item in raw or []:
            if not isinstance(item, dict) or "file" not in item:
                continue
            p = d / item["file"]
            meta = split_front_matter(p.read_text(encoding="utf-8"))[0] if p.exists() else {}
            entries.append(NoteEntry(
                file=item["file"],
                title=item.get("title") or meta.get("title") or item["file"],
                keywords=list(item.get("keywords") if item.get("keywords") is not None else meta.get("keywords") or []),
                cases=list(item.get("cases") or meta.get("cases") or []),
                phases=list(item.get("phases") or meta.get("phases") or []),
                always=bool(item.get("always", False)),
                path=p,
            ))
    elif d.is_dir():
        for p in sorted(d.glob("*.md")):
            meta = split_front_matter(p.read_text(encoding="utf-8"))[0]
            entries.append(NoteEntry(file=p.name, title=meta.get("title", p.stem),
                                     keywords=list(meta.get("keywords") or []), cases=list(meta.get("cases") or []),
                                     phases=list(meta.get("phases") or []), always=bool(meta.get("always", False)),
                                     path=p))
    return entries


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def fence_for(text: str) -> str:
    return "````" if "```" in text else "```"


def lang_for(path: str) -> str:
    return _LANG.get(PurePosixPath(path).suffix.lower(), "")


def merge_windows(lines: list[int], total: int, radius: int = WINDOW_RADIUS) -> list[tuple[int, int]]:
    """1-based inclusive windows of ±radius around each line, merged when overlapping or adjacent."""
    spans: list[tuple[int, int]] = []
    for ln in sorted(set(l for l in lines if l and l > 0)):
        a, b = max(1, ln - radius), min(total, ln + radius)
        if spans and a <= spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    if not spans:
        spans = [(1, min(total, 2 * radius))]
    return spans


def format_error(e: BuildError) -> str:
    loc = e.file or "-"
    if e.line:
        loc += f":{e.line}"
        if e.column:
            loc += f":{e.column}"
    parts = [f"- [{e.kind.value}] {loc}: {e.message}"]
    if e.symbol:
        parts.append(f"    symbol:   {e.symbol}")
    if e.location:
        parts.append(f"    location: {e.location}")
    if e.artifact:
        parts.append(f"    artifact: {e.artifact.coordinates}")
    if e.module:
        parts.append(f"    module:   {e.module}")
    if e.goal:
        parts.append(f"    goal:     {e.goal}")
    return "\n".join(parts)


def summarize_attempt(a: FixAttempt) -> str:
    lines = [f"- attempt {a.id} (iteration {a.iteration}, model {a.model or '-'}) → outcome: {a.outcome}"
             + (f", errors {a.errors_before}→{a.errors_after}" if a.errors_after is not None else "")]
    if a.analysis:
        lines.append(f"    analysis: {a.analysis.strip()[:400]}")
    for c in a.changes[:12]:
        if c.op == "modify":
            s = (c.search or "").strip().splitlines()
            r = (c.replace or "").strip().splitlines()
            lines.append(f"    MODIFY {c.path}: `{(s[0] if s else '')[:100]}` → `{(r[0] if r else '')[:100]}`")
        elif c.op == "rename":
            lines.append(f"    RENAME {c.path} -> {c.new_path}")
        else:
            lines.append(f"    {c.op.upper()} {c.path}")
    if len(a.changes) > 12:
        lines.append(f"    … {len(a.changes) - 12} more change(s)")
    for err in a.apply_errors[:5]:
        lines.append(f"    apply error: {err}")
    return "\n".join(lines)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"\n…[truncated {len(text) - max_chars} chars]"
    return text[: max(0, max_chars - len(marker))] + marker


def nearest_pom(ws: Workspace, rel: Optional[str]) -> Optional[str]:
    """The module pom of a file: walk up from the file's directory to the project root. Root-relative posix paths
    only; a path with backslashes is normalized, an absolute one is made root-relative when it lies under the
    workspace and dropped otherwise (round 9c: `PurePosixPath("/").parent` is `/`, so an absolute path made the old
    `while True` loop spin forever — seen as a run "stuck in fixer.propose" on Windows)."""
    if not rel:
        return "pom.xml" if ws.exists("pom.xml") else None
    norm = str(rel).replace("\\", "/")
    p = PurePosixPath(norm)
    if p.is_absolute() or (len(norm) > 1 and norm[1] == ":"):          # posix-absolute or a Windows drive path
        try:
            norm = Path(norm).resolve().relative_to(Path(ws.root).resolve()).as_posix()
        except (ValueError, OSError):
            return "pom.xml" if ws.exists("pom.xml") else None
        p = PurePosixPath(norm)
    if p.name == "pom.xml" and ws.exists(norm):
        return norm
    cur = p.parent
    for _ in range(128):                                               # never spin: depth-bounded walk to the root
        cand = (cur / "pom.xml").as_posix()
        cand = cand[2:] if cand.startswith("./") else cand
        if ws.exists(cand):
            return cand
        if str(cur) in ("", ".", "/") or cur.parent == cur:
            return None
        cur = cur.parent
    return None


_POM_SECTIONS = ("parent", "properties", "dependencyManagement", "dependencies", "build")


def pom_excerpt(text: str, path: str) -> str:
    lines = text.split("\n")
    if len(lines) <= POM_WHOLE_MAX_LINES:
        return f"// {path} (whole file)\n```xml\n{text.rstrip()}\n```"
    out = []
    taken: list[tuple[int, int]] = []
    for tag in _POM_SECTIONS:
        for m in re.finditer(rf"<{tag}>.*?</{tag}>", text, re.DOTALL):
            a, b = m.start(), m.end()
            if any(x <= a and b <= y for x, y in taken):
                continue                                 # nested (e.g. <dependencies> inside depMgmt)
            # expand to whole lines
            ls = text.rfind("\n", 0, a) + 1
            le = text.find("\n", b)
            le = len(text) if le < 0 else le
            taken.append((ls, le))
            l1 = text.count("\n", 0, ls) + 1
            l2 = text.count("\n", 0, le) + 1
            out.append((l1, f"// {path} lines {l1}-{l2} <{tag}>\n```xml\n{text[ls:le]}\n```"))
            break
    out.sort()
    return "\n".join(s for _, s in out) or f"// {path} (no standard sections found)"


# ---------------------------------------------------------------------------
# packer
# ---------------------------------------------------------------------------
@dataclass
class PackedContext:
    sections: list[tuple[str, str]] = field(default_factory=list)
    files_included: list[str] = field(default_factory=list)
    notes_included: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    chars: int = 0

    def render(self) -> str:
        return "\n\n".join(f"## {t}\n{body}" for t, body in self.sections)


@dataclass
class _Section:
    key: str
    title: str
    text: str
    order: float
    truncatable: bool = True
    always: bool = False


class ContextPacker:
    # truncation priority (first = dropped/shrunk first); "errors" and "header" are never truncated
    TRUNCATE_ORDER = ("notes", "plan_notes", "other_errors", "previous_attempts", "kb_hints", "reference_files",
                      "advisory_hints", "reference_notes", "pom", "files")
    ADVISORY_TITLE = "Library migration guidance (what to migrate TO)"
    REFERENCE_NOTES_TITLE = "Module merge notes (first-wins; the dropped copy is below as reference)"
    REFERENCE_FILES_TITLE = ("Reference files (read-only, NOT in the build — never write SEARCH/REPLACE blocks for "
                             "these paths)")

    def __init__(self, cfg: Config, notes_dir: Optional[str | Path] = None, max_notes: int = DEFAULT_MAX_NOTES):
        self.cfg = cfg
        self.notes_dir = Path(notes_dir) if notes_dir else cfg.resolve_path("knowledge/notes")
        self.max_notes = max_notes
        self._notes: Optional[list[NoteEntry]] = None

    @property
    def notes(self) -> list[NoteEntry]:
        if self._notes is None:
            try:
                self._notes = load_notes_index(self.notes_dir)
            except Exception as exc:  # broken index must never break fixing
                log.warning("cannot load notes index from %s: %s", self.notes_dir, exc)
                self._notes = []
        return self._notes

    # ------------------------------------------------------------------ public
    def pack(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, profile_summary: str = "",
             *, case_type: Optional[str] = None, budget: Optional[int] = None) -> PackedContext:
        budget = budget or self.cfg.llm.max_context_chars
        summary = profile_summary or ctx.profile_summary
        if case_type is None:
            m = re.search(r"case=(\w+)", summary or "")
            case_type = m.group(1) if m else None
        packed = PackedContext()
        secs: list[_Section] = []

        header = [f"phase: {ctx.phase.value}, iteration {ctx.iteration}" + (f", stall level {ctx.stall_level}" if ctx.stall_level else "")]
        if summary:
            header.append(f"project: {summary}")
        header.append(f"cluster: {cluster.id} kind={cluster.kind.value}" + (f" module={cluster.module}" if cluster.module else "")
                      + (f" file={cluster.file}" if cluster.file else ""))
        secs.append(_Section("header", "Situation", "\n".join(header), 0, truncatable=False))

        secs.append(_Section("errors", f"Errors to fix ({len(cluster.errors)})",
                             "\n".join(format_error(e) for e in cluster.errors), 1, truncatable=False))

        # files
        files = self._cluster_files(cluster)
        pom_path = nearest_pom(ws, cluster.file or next((e.file for e in cluster.errors if e.file), None))
        file_texts: list[str] = []
        import_text = ""
        for f in files:
            if PurePosixPath(f).name == "pom.xml":
                continue                                     # handled as pom section
            block, imports = self._file_block(ws, f, [e.line for e in cluster.errors if e.file == f and e.line])
            file_texts.append(block)
            import_text += imports
            if ws.exists(f):
                packed.files_included.append(f)
        if file_texts:
            secs.append(_Section("files", "Source (exact text; copy SEARCH blocks from here)", "\n\n".join(file_texts), 2))
        # dropped module-merge copies (round 9): context only, right after the source they differ from
        if ctx.reference_files:
            secs.append(_Section("reference_files", self.REFERENCE_FILES_TITLE,
                                 "\n\n".join(self._reference_block(label, text)
                                             for label, text in ctx.reference_files.items()), 2.5))

        # pom
        if pom_path and self._wants_pom(cluster, files):
            try:
                secs.append(_Section("pom", f"POM ({pom_path})", pom_excerpt(ws.read(pom_path), pom_path), 3))
                packed.files_included.append(pom_path)
            except OSError:
                pass

        # library advisories that match this cluster (round 6): the library was replaced on purpose — say what TO
        if ctx.advisory_hints:
            secs.append(_Section("advisory_hints", self.ADVISORY_TITLE,
                                 "The libraries below were removed or replaced by the migration on purpose. Rewrite the "
                                 "code that still uses them to the named replacement (keep public signatures so callers "
                                 "still compile); do not re-add the removed library.\n"
                                 + "\n".join(f"- {h}" for h in ctx.advisory_hints), 4))
        if ctx.reference_notes:
            secs.append(_Section("reference_notes", self.REFERENCE_NOTES_TITLE,
                                 "\n".join(f"- {n}" for n in ctx.reference_notes), 4.5))
        if ctx.kb_hints:
            secs.append(_Section("kb_hints", "Knowledge-base hints (similar resolved cases)",
                                 "\n".join(f"- {h}" for h in ctx.kb_hints), 5))
        if ctx.previous_attempts:
            secs.append(_Section("previous_attempts",
                                 "Previous attempts on this cluster (they did NOT resolve it — do not repeat them)",
                                 "\n".join(summarize_attempt(a) for a in ctx.previous_attempts), 6))
        if ctx.other_errors_summary:
            secs.append(_Section("other_errors", "Other error clusters in this build (for awareness; fix only this cluster)",
                                 ctx.other_errors_summary.strip(), 7))
        if ctx.plan_notes:
            secs.append(_Section("plan_notes", "Plan notes", "\n".join(f"- {n}" for n in ctx.plan_notes), 8))

        # notes: one section, built from selected notes in rank order
        match_text = "\n".join(
            [e.message for e in cluster.errors] + [e.symbol or "" for e in cluster.errors]
            + [e.location or "" for e in cluster.errors] + [e.raw[:2000] for e in cluster.errors]
            + [e.artifact.coordinates for e in cluster.errors if e.artifact] + [import_text])
        selected = self.select_notes(match_text, case_type, ctx.phase.value)
        note_secs = []
        for i, n in enumerate(selected):
            body = n.body()
            if body:
                note_secs.append(_Section("notes", f"Migration note: {n.title}", body, 10 + i, always=n.always))
        secs.extend(note_secs)

        self._fit(secs, budget, packed)
        secs.sort(key=lambda s: s.order)
        packed.sections = [(s.title, s.text) for s in secs]
        packed.notes_included = [s.title.removeprefix("Migration note: ") for s in secs if s.key == "notes"]
        packed.chars = sum(len(t) + len(b) + 4 for t, b in packed.sections)
        return packed

    def select_notes(self, text: str, case: Optional[str], phase: Optional[str]) -> list[NoteEntry]:
        scored = []
        always = []
        for idx, n in enumerate(self.notes):
            if n.always:
                always.append(n)
                continue
            if not n.applies(case, phase):
                continue
            s = n.score(text)
            if s > 0:
                scored.append((-s, idx, n))
        scored.sort(key=lambda t: (t[0], t[1]))
        top = [n for _, _, n in scored[: self.max_notes]]
        return always + top            # always-notes first (rendered first, dropped last)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _cluster_files(cluster: ErrorCluster) -> list[str]:
        out: list[str] = []
        for f in [cluster.file] + [e.file for e in cluster.errors]:
            if f and f not in out:
                out.append(f)
        return out

    @staticmethod
    def _reference_block(label: str, text: str) -> str:
        body = text.replace("\r\n", "\n").rstrip("\n")
        fence = fence_for(body)
        return f"// reference copy (read-only, not in the build): {label}\n{fence}{lang_for(label)}\n{body}\n{fence}"

    @staticmethod
    def _wants_pom(cluster: ErrorCluster, files: list[str]) -> bool:
        if cluster.kind in POM_KINDS:
            return True
        if any(PurePosixPath(f).name == "pom.xml" for f in files):
            return True
        return any(POM_MESSAGE_RE.search(e.message or "") for e in cluster.errors)

    def _file_block(self, ws: Workspace, path: str, err_lines: list[int]) -> tuple[str, str]:
        if not ws.exists(path):
            return f"// {path}: (file not found in workspace)", ""
        text = ws.read(path).replace("\r\n", "\n")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        imports = "\n".join(l for l in lines if l.startswith("import ") or l.startswith("package ")) + "\n"
        lang = lang_for(path)
        if len(lines) <= WHOLE_FILE_MAX_LINES:
            body = "\n".join(lines)
            fence = fence_for(body)
            return f"// {path} lines 1-{len(lines)} (whole file)\n{fence}{lang}\n{body}\n{fence}", imports
        blocks = []
        # always show the import block when the window does not include it
        spans = merge_windows(err_lines, len(lines))
        last_import = max((i + 1 for i, l in enumerate(lines) if l.startswith("import ")), default=0)
        if last_import and spans[0][0] > 1:
            spans.insert(0, (1, min(last_import, spans[0][0] - 1)))
        for a, b in spans:
            body = "\n".join(lines[a - 1:b])
            fence = fence_for(body)
            blocks.append(f"// {path} lines {a}-{b} (of {len(lines)})\n{fence}{lang}\n{body}\n{fence}")
        return "\n".join(blocks), imports

    def _fit(self, secs: list[_Section], budget: int, packed: PackedContext) -> None:
        def total() -> int:
            return sum(len(s.title) + len(s.text) + 6 for s in secs)

        over = total() - budget
        if over <= 0:
            return
        for key in self.TRUNCATE_ORDER:
            if over <= 0:
                break
            group = [s for s in secs if s.key == key and s.truncatable]
            # notes: drop lowest-ranked first (they are in rank order; always-notes come last)
            if key == "notes":
                for s in sorted(group, key=lambda x: -x.order):
                    if over <= 0:
                        break
                    if s.always and over < len(s.text) - 400:
                        # keep a shortened always-note (fix policy) rather than dropping it
                        keep = len(s.text) - over
                        s.text = _truncate(s.text, keep)
                        packed.truncated.append(f"notes:{s.title} (shortened)")
                    else:
                        secs.remove(s)
                        packed.truncated.append(f"notes:{s.title} (dropped)")
                    over = total() - budget
                continue
            for s in group:
                if over <= 0:
                    break
                keep = len(s.text) - over
                if keep < 200:
                    secs.remove(s)
                    packed.truncated.append(f"{key} (dropped)")
                else:
                    s.text = _truncate(s.text, keep)
                    packed.truncated.append(f"{key} (truncated)")
                over = total() - budget
        if packed.truncated:
            log.info("context over budget (%d chars): %s", budget, ", ".join(packed.truncated))
