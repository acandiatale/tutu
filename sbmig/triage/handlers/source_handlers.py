"""Deterministic Java-source handlers (catalog E13 / E14).

* ``jakarta-namespace-sweep`` (E13) — renames the ``javax.*`` Jakarta EE references the OpenRewrite run left behind
  (files the plugin could not parse / type-attribute, ``openrewrite.exclusions``, fully-qualified names) with the
  ChangePackage table of ``JavaxMigrationToJakarta`` (``jakarta_packages.yml``).
* ``spring-data-crud-repository`` (E14) — Spring Data 3 removed ``CrudRepository`` from
  ``PagingAndSortingRepository``'s hierarchy: repository interfaces that extend only the paging interface get
  ``ListCrudRepository<T, ID>`` (what ``MigratePagingAndSortingRepository`` does when it can attribute types).
  E14b (reactive only): a repository named by the error whose CRUD came from a *library* base interface (not a
  project file, compiled against Spring Data 2) gets ``ListCrudRepository`` with that base's two type arguments.

Both run pre-emptively after the spring recipes (``preemptive_phase = SPRING``, ``cluster is None`` → whole project)
and reactively on the matching javac errors. Both are idempotent and edit sources through ``Workspace.read`` /
``Workspace.write`` (detected encoding and ``\\r\\n`` preserved); flags / notes as every ``EdgeHandler``.
"""
from __future__ import annotations

import logging
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional

import yaml

from sbmig.core.models import ErrorCluster, ErrorKind, Phase, ProjectProfile
from sbmig.core.workspace import Workspace

from .base import cluster_text
from .edge_handlers import EdgeHandler

log = logging.getLogger("sbmig.triage.handlers.source")

_DATA_DIR = Path(__file__).parent
_KINDS = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE})

# comments (group 1, treated as code) | string / text-block / char literals (group 2, never edited)
_LIT_RE = re.compile(r'(//[^\n]*|/\*[\s\S]*?\*/)|("""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\')')
_JAVAX_TOKEN_RE = re.compile(r"(?<![\w$.])javax(?:\.[A-Za-z_$][\w$]*)+(?:\.\*)?")
_JAKARTA_IMPORT_RE = re.compile(r"^[ \t]*import\s+(?:static\s+)?jakarta\.", re.M)
_RESOURCE_JAVAX_RE = re.compile(r"javax\.(?:persistence|servlet|validation|transaction|annotation)\.")
_RESOURCE_SUFFIXES = (".xml", ".properties", ".yml", ".yaml")


def _under_target(rel: str) -> bool:
    return "target" in rel.replace("\\", "/").split("/")


def _mask(text: str) -> str:
    """`text` with comments and literals blanked (same length, newlines kept) — for structural regexes."""
    return _LIT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


# ---------------------------------------------------------------------------------------------------------------------
# E13 table + pure rename function
# ---------------------------------------------------------------------------------------------------------------------
class JakartaRule(NamedTuple):
    from_: str
    to: str
    recursive: bool


@lru_cache(maxsize=1)
def jakarta_packages() -> tuple[JakartaRule, ...]:
    """The javax → jakarta ChangePackage table (jakarta_packages.yml), longest `from` first."""
    doc = yaml.safe_load((_DATA_DIR / "jakarta_packages.yml").read_text(encoding="utf-8")) or {}
    rules = [JakartaRule(str(p["from"]), str(p["to"]), bool(p.get("recursive", True)))
             for p in doc.get("packages") or []]
    return tuple(sorted(rules, key=lambda r: -len(r.from_)))


def _suffixes(table: tuple[JakartaRule, ...]) -> str:
    """Regex alternation of the table's packages after `javax.` (longest first)."""
    return "|".join(re.escape(r.from_[len("javax."):]) for r in table if r.from_.startswith("javax."))


def javax_key_in_table(key: str, table: Optional[tuple[JakartaRule, ...]] = None) -> bool:
    """`profile.source.javax_imports` key (e.g. 'javax.servlet', 'javax.xml.bind') equal to / under a table package."""
    table = table if table is not None else jakarta_packages()
    return any(key == r.from_ or key.startswith(r.from_ + ".") for r in table)


@dataclass
class RenameResult:
    text: str
    replacements: Counter = field(default_factory=Counter)        # from-package → count
    literal_hits: list[str] = field(default_factory=list)          # javax EE names inside string / char literals


def _rename_token(token: str, by_from: dict[str, JakartaRule]) -> Optional[tuple[str, str]]:
    """(new token, from-package) or None. The longest table package that prefixes the token decides."""
    parts = token.split(".")
    for i in range(len(parts) - 1, 1, -1):
        rule = by_from.get(".".join(parts[:i]))
        if rule is None:
            continue
        rest = parts[i:]
        if not rest:
            return None
        if not rule.recursive and not (rest[0] == "*" or rest[0][:1].isupper()):
            return None
        return ".".join([rule.to, *rest]), rule.from_
    return None


def rename_javax(text: str, table: Optional[tuple[JakartaRule, ...]] = None) -> RenameResult:
    """Rename table javax.* references in code: string / char / text-block literals are left alone (counted in
    `literal_hits`) and so are comments (prose — a Javadoc that mentions `javax.persistence.Query` is not a leftover;
    OpenRewrite's ChangePackage does not touch them either, and renaming them would raise the flag for nothing)."""
    table = table if table is not None else jakarta_packages()
    by_from = {r.from_: r for r in table}
    # spans to skip: (start, end, is_literal) — comments are skipped silently, literals are reported
    literals = [(m.start(), m.end(), m.group(2) is not None) for m in _LIT_RE.finditer(text)]
    res = RenameResult(text)
    out: list[str] = []
    last = 0
    li = 0
    for m in _JAVAX_TOKEN_RE.finditer(text):
        hit = _rename_token(m.group(0), by_from)
        if hit is None:
            continue
        while li < len(literals) and literals[li][1] <= m.start():
            li += 1
        if li < len(literals) and literals[li][0] <= m.start() < literals[li][1]:
            if literals[li][2]:
                res.literal_hits.append(m.group(0))
            continue
        out.append(text[last:m.start()])
        out.append(hit[0])
        last = m.end()
        res.replacements[hit[1]] += 1
    if res.replacements:
        out.append(text[last:])
        res.text = "".join(out)
    return res


# ---------------------------------------------------------------------------------------------------------------------
# E13 handler
# ---------------------------------------------------------------------------------------------------------------------
def _jakarta_triggers() -> tuple[str, ...]:
    alt = _suffixes(jakarta_packages())
    return (rf"package javax\.(?:{alt})(?:\.\w+)* does not exist",
            rf"location: package javax\.(?:{alt})",
            rf"\b(?:javax|jakarta)\.(?:{alt})\.[\w$.]+ cannot be converted to (?:jakarta|javax)\.",
            rf"required: (?:javax|jakarta)\.(?:{alt})\.[\s\S]{{0,300}}?found: (?:jakarta|javax)\.",
            rf"class file for javax\.(?:{alt})\.\S+ not found")


_CONVERT_PAIR_RE = re.compile(r"\b(?:javax|jakarta)\.[\w$.]+\s+cannot be converted to\s+(?:jakarta|javax)\.|"
                              r"required:\s*(?:javax|jakarta)\.[\s\S]{0,300}?found:\s*(?:jakarta|javax)\.")


class JakartaNamespaceSweep(EdgeHandler):
    id = "jakarta-namespace-sweep"
    description = ("rename javax.* Jakarta EE references the recipes left behind (unparsed / excluded files, "
                   "fully-qualified names) to jakarta.* — table sbmig/triage/handlers/jakarta_packages.yml")
    kinds = _KINDS
    triggers = _jakarta_triggers()
    preemptive_phase = Phase.SPRING

    def applicable(self, cluster: Optional[ErrorCluster], profile: Optional[ProjectProfile], cfg) -> bool:
        if profile is None:
            return True
        if any(javax_key_in_table(k) for k, n in (profile.source.javax_imports or {}).items() if n):
            return True
        # a javax/jakarta conversion mismatch whose owner is a project class (blocked.py X1 "project owner" case)
        if cluster is not None and _CONVERT_PAIR_RE.search(cluster_text(cluster)):
            from sbmig.triage.blocked import extract_owner, is_project_class
            return any(is_project_class(None, profile, extract_owner(e)) for e in cluster.errors)
        return False

    def edit(self, ws: Workspace, cluster, profile, cfg) -> list[str]:
        table = jakarta_packages()
        files = [f for f in ws.java_files() if not _under_target(f)]
        texts: dict[str, str] = {}
        for f in files:
            try:
                texts[f] = ws.read(f)
            except OSError as e:
                log.debug("jakarta-namespace-sweep: cannot read %s: %s", f, e)
        if cluster is None and not any(_JAKARTA_IMPORT_RE.search(t) for t in texts.values()):
            return []          # nothing migrated yet (e.g. before the jakarta recipes): leave javax alone
        changed: list[str] = []
        packages: Counter = Counter()
        literal_hits: list[tuple[str, str]] = []
        for f, text in texts.items():
            res = rename_javax(text, table)
            literal_hits += [(f, lit) for lit in res.literal_hits]
            if res.replacements and res.text != text:
                ws.write(f, res.text)
                changed.append(f)
                packages.update(res.replacements)
        if changed:
            self.flag("jakarta-leftovers-renamed")
            self.note(f"renamed javax.* → jakarta.* in {len(changed)} file(s) the recipes had not converted "
                      f"(e.g. {', '.join(changed[:3])}); packages: {', '.join(sorted(packages))}")
            log.info("jakarta-namespace-sweep: %d file(s), packages %s", len(changed), sorted(packages))
        if literal_hits:
            path, lit = literal_hits[0]
            self.note(f"follow-up: {len(literal_hits)} string literal(s) still name javax.* (reflection / "
                      f"configuration keys), e.g. {path}: {lit[:60]}; renamed nothing inside literals — review by hand")
        self._resource_note(ws)
        return changed

    def _resource_note(self, ws: Workspace) -> None:
        hits: list[str] = []
        for suffix in _RESOURCE_SUFFIXES:
            for rel in ws.glob(f"*{suffix}"):
                p = "/" + rel.replace("\\", "/")
                if "/src/main/resources/" not in p and "/src/main/webapp/WEB-INF/" not in p:
                    continue
                try:
                    if _RESOURCE_JAVAX_RE.search(ws.read(rel)) and rel not in hits:
                        hits.append(rel)
                except OSError:
                    continue
        if hits:
            self.note(f"follow-up: javax.* names remain in {len(hits)} resource file(s) ({', '.join(sorted(hits)[:3])}): "
                      "JPA property keys are jakarta.persistence.* since Hibernate 6, persistence.xml / web.xml need "
                      "the Jakarta schema — the recipes rewrite persistence.xml and web.xml, the rest is yours")


# ---------------------------------------------------------------------------------------------------------------------
# E14 Spring Data CRUD repository
# ---------------------------------------------------------------------------------------------------------------------
SD_REPO_PKG = "org.springframework.data.repository"
PAGING_NAMES = frozenset({"PagingAndSortingRepository", "ListPagingAndSortingRepository"})
CRUD_NAMES = frozenset({
    "CrudRepository", "ListCrudRepository", "JpaRepository", "JpaRepositoryImplementation", "MongoRepository",
    "ElasticsearchRepository", "CassandraRepository", "Neo4jRepository", "CouchbaseRepository", "KeyValueRepository",
    "LdapRepository", "ReactiveCrudRepository", "ReactiveMongoRepository", "ReactiveCassandraRepository",
    "ReactiveNeo4jRepository", "R2dbcRepository"})
_CRUD_METHODS = ("save", "saveAll", "findById", "existsById", "findAll", "findAllById", "count", "deleteById",
                 "delete", "deleteAllById", "deleteAll")
_MAX_DEPTH = 3

_IFACE_RE = re.compile(r"(?<!@)\binterface\s+([A-Za-z_$][\w$]*)")
_PACKAGE_RE = re.compile(r"^[ \t]*package\s+([\w.]+)\s*;", re.M)
_IMPORT_RE = re.compile(r"^[ \t]*import\s+(?!static\b)([\w.]+?)(\.\*)?\s*;", re.M)
_LOC_VAR_RE = re.compile(r"variable\s+[\w$]+\s+of\s+type\s+([\w$.]+)")
_LOC_TYPE_RE = re.compile(r"location\s*:\s*(?:class|interface)\s+([\w$.]+)")


@dataclass
class _SuperRef:
    name: str                 # as written, generics stripped, whitespace removed ('PagingAndSortingRepository')
    args: Optional[str]       # original text between the outer < > (None = raw)
    end: int                  # offset right after the element in the file text

    @property
    def simple(self) -> str:
        return self.name.rsplit(".", 1)[-1]

    @property
    def is_paging(self) -> bool:
        return self.simple in PAGING_NAMES and ("." not in self.name or self.name.startswith(SD_REPO_PKG + "."))

    @property
    def is_crud(self) -> bool:
        return self.simple in CRUD_NAMES


@dataclass
class _Iface:
    name: str
    supers: list[_SuperRef]

    @property
    def paging(self) -> Optional[_SuperRef]:
        return next((s for s in self.supers if s.is_paging), None)


def _balanced_end(s: str, i: int) -> int:
    """Index after the `>` matching the `<` at s[i]; -1 when unbalanced."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "<":
            depth += 1
        elif s[j] == ">":
            depth -= 1
            if depth == 0:
                return j + 1
        elif s[j] in "{;":
            return -1
    return -1


def parse_interfaces(text: str) -> dict[str, _Iface]:
    """Interface declarations with an `extends` list (comment / literal safe)."""
    masked = _mask(text)
    out: dict[str, _Iface] = {}
    for m in _IFACE_RE.finditer(masked):
        i = m.end()
        n = len(masked)
        while i < n and masked[i].isspace():
            i += 1
        if i < n and masked[i] == "<":
            i = _balanced_end(masked, i)
            if i < 0:
                continue
        em = re.compile(r"\s*extends\b").match(masked, i)
        if not em:
            continue
        start = em.end()
        depth, j = 0, start
        parts: list[tuple[int, int]] = []
        seg = start
        while j < n:
            c = masked[j]
            if c == "<":
                depth += 1
            elif c == ">":
                depth -= 1
            elif c == "," and depth == 0:
                parts.append((seg, j))
                seg = j + 1
            elif c in "{;" and depth <= 0:
                break
            j += 1
        if j >= n or masked[j] != "{":
            continue
        parts.append((seg, j))
        supers: list[_SuperRef] = []
        for a, b in parts:
            raw = masked[a:b]
            lead = len(raw) - len(raw.lstrip())
            a2, b2 = a + lead, a + len(raw.rstrip())
            if a2 >= b2:
                continue
            elem = masked[a2:b2]
            lt = elem.find("<")
            name = re.sub(r"\s+", "", elem if lt < 0 else elem[:lt])
            name = re.sub(r"^(?:@[\w.]+)+", "", name)
            args = None
            if lt >= 0:
                close = _balanced_end(masked, a2 + lt)
                if close > 0:
                    args = text[a2 + lt + 1:close - 1]
            supers.append(_SuperRef(name, args, b2))
        out.setdefault(m.group(1), _Iface(m.group(1), supers))
    return out


def _strip_generics(t: str) -> str:
    return t.split("<", 1)[0].strip().rstrip(".,;")


def _split_args(args: str) -> list[str]:
    """Top-level type arguments of `A, Map<K, V>, B` → ['A', 'Map<K, V>', 'B']."""
    out: list[str] = []
    depth, start = 0, 0
    for i, c in enumerate(args):
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(args[start:i].strip())
            start = i + 1
    out.append(args[start:].strip())
    return [a for a in out if a]


# supertypes that are never "a library base repository" (E14b): Spring Data's own non-CRUD interfaces and JDK types
_SPRING_DATA_NON_CRUD = frozenset({"Repository", "QueryByExampleExecutor", "JpaSpecificationExecutor",
                                   "QuerydslPredicateExecutor", "ReactiveQuerydslPredicateExecutor",
                                   "RevisionRepository", "ReactiveSortingRepository", "ListQuerydslPredicateExecutor",
                                   "ListQueryByExampleExecutor", "CrudMethodMetadata"})
_JDK_SIMPLE = frozenset({"Serializable", "Cloneable", "AutoCloseable", "Closeable", "Comparable", "Iterable"})
_PLATFORM_PREFIXES = ("java.", "javax.", "jakarta.", "org.springframework.")


class _RepoIndex:
    """Project Java sources by '<pkg path>/<Name>' (the part after '/java/'), parsed lazily."""

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.by_path: dict[str, str] = {}
        self.by_simple: dict[str, list[str]] = {}
        for f in ws.java_files():
            if _under_target(f):
                continue
            p = f.replace("\\", "/")
            idx = p.rfind("/java/")
            rel = p[idx + 6:] if idx >= 0 else p
            rel = rel[:-5] if rel.endswith(".java") else rel
            self.by_path.setdefault(rel, f)
            self.by_simple.setdefault(posixpath.basename(rel), []).append(f)
        self._texts: dict[str, str] = {}
        self._ifaces: dict[str, dict[str, _Iface]] = {}

    def text(self, f: str) -> str:
        if f not in self._texts:
            self._texts[f] = self.ws.read(f)
        return self._texts[f]

    def ifaces(self, f: str) -> dict[str, _Iface]:
        if f not in self._ifaces:
            self._ifaces[f] = parse_interfaces(self.text(f))
        return self._ifaces[f]

    def file_for_fqcn(self, fqcn: str) -> Optional[str]:
        return self.by_path.get(fqcn.replace(".", "/"))

    def resolve(self, from_file: Optional[str], name: str) -> Optional[str]:
        """Project file declaring type `name` as seen from `from_file` (imports, same package, wildcard imports)."""
        name = _strip_generics(name)
        if not name:
            return None
        if "." in name:
            return self.file_for_fqcn(name)
        if from_file and self.ws.exists(from_file):
            masked = _mask(self.text(from_file))
            wild: list[str] = []
            for m in _IMPORT_RE.finditer(masked):
                imp, star = m.group(1), m.group(2)
                if star:
                    wild.append(imp)
                elif imp.rsplit(".", 1)[-1] == name:
                    return self.file_for_fqcn(imp)
            pm = _PACKAGE_RE.search(masked)
            if pm:
                hit = self.file_for_fqcn(f"{pm.group(1)}.{name}")
                if hit:
                    return hit
            same = posixpath.join(posixpath.dirname(from_file.replace("\\", "/")), name + ".java")
            if same in self._all():
                return same
            for w in wild:
                hit = self.file_for_fqcn(f"{w}.{name}")
                if hit:
                    return hit
            return None
        cands = self.by_simple.get(name) or []
        return cands[0] if len(cands) == 1 else None

    def _all(self) -> set[str]:
        return {f for fs in self.by_simple.values() for f in fs}


def _platform_type(idx: "_RepoIndex", f: str, s: _SuperRef) -> bool:
    """A Spring / JDK / Jakarta supertype (by its written FQN or its import), or a well-known simple name."""
    if s.simple in _SPRING_DATA_NON_CRUD or ("." not in s.name and s.simple in _JDK_SIMPLE):
        return True
    fqn = s.name if "." in s.name else None
    if fqn is None:
        try:
            masked = _mask(idx.text(f))
        except OSError:
            return False
        for m in _IMPORT_RE.finditer(masked):
            if not m.group(2) and m.group(1).rsplit(".", 1)[-1] == s.simple:
                fqn = m.group(1)
                break
    return bool(fqn and fqn.startswith(_PLATFORM_PREFIXES))


class SpringDataCrudRepository(EdgeHandler):
    id = "spring-data-crud-repository"
    description = ("Spring Data 3+: PagingAndSortingRepository no longer extends CrudRepository — add "
                   "ListCrudRepository<T, ID> to repository interfaces that extend only the paging interface (the "
                   "recipe MigratePagingAndSortingRepository does it when it can attribute types)")
    kinds = _KINDS
    triggers = (r"no suitable method found for findAll\(no arguments\)",
                r"symbol:\s+method (?:" + "|".join(_CRUD_METHODS) + r")\(")
    preemptive_phase = Phase.SPRING

    def applicable(self, cluster, profile, cfg) -> bool:
        if profile is None:
            return True
        if any(lib.name.startswith("spring-data") or lib.name == "jpa" for lib in profile.libraries):
            return True
        return bool(cluster is not None and
                    re.search(r"PagingAndSortingRepository|org\.springframework\.data", cluster_text(cluster)))

    # -- analysis ---------------------------------------------------------------------------------------------------
    def _provides_crud(self, idx: _RepoIndex, f: str, name: str, depth: int, seen: set) -> bool:
        """The interface has CRUD methods once fixed: a CRUD-bearing super, a paging super (it gets fixed itself) or a
        project ancestor that provides them."""
        if depth > _MAX_DEPTH or (f, name) in seen:
            return False
        seen.add((f, name))
        decl = idx.ifaces(f).get(name)
        if decl is None:
            return False
        if any(s.is_crud or s.is_paging for s in decl.supers):
            return True
        return any(self._super_provides(idx, f, s, depth, seen) for s in decl.supers)

    def _super_provides(self, idx: _RepoIndex, f: str, s: _SuperRef, depth: int, seen: set) -> bool:
        sf = idx.resolve(f, s.name)
        return bool(sf) and self._provides_crud(idx, sf, s.simple, depth + 1, seen)

    def _needs_fix(self, idx: _RepoIndex, f: str, decl: _Iface) -> bool:
        if decl.paging is None or any(s.is_crud for s in decl.supers):
            return False
        return not any(self._super_provides(idx, f, s, 0, {(f, decl.name)})
                       for s in decl.supers if not s.is_paging)

    def _collect_tree(self, idx: _RepoIndex, f: str, name: str, depth: int, out: dict[str, set[str]],
                      seen: set, lib_out: Optional[list[tuple[str, _Iface, list[_SuperRef]]]] = None) -> None:
        if depth > _MAX_DEPTH or (f, name) in seen:
            return
        seen.add((f, name))
        decl = idx.ifaces(f).get(name)
        if decl is None:
            return
        if self._needs_fix(idx, f, decl):
            out.setdefault(f, set()).add(name)
        elif lib_out is not None:
            libs = self._library_bases(idx, f, decl)
            if libs:
                lib_out.append((f, decl, libs))
        for s in decl.supers:
            if s.is_paging or s.is_crud:
                continue
            sf = idx.resolve(f, s.name)
            if sf:
                self._collect_tree(idx, sf, s.simple, depth + 1, out, seen, lib_out)

    # -- E14b: CRUD lost through a library base interface -----------------------------------------------------------
    def _library_bases(self, idx: _RepoIndex, f: str, decl: _Iface) -> list[_SuperRef]:
        """Supertypes of `decl` that are not project files and not Spring / JDK types, when `decl` has no paging
        reference of its own, no CRUD-bearing supertype and no project ancestor that provides CRUD ([] otherwise).
        Reactive only: such a library base (compiled against Spring Data 2, where PagingAndSortingRepository still
        extended CrudRepository) may as well provide CRUD itself — only the compile error proves it does not."""
        if decl.paging is not None or any(s.is_crud for s in decl.supers):
            return []
        if any(self._super_provides(idx, f, s, 0, {(f, decl.name)}) for s in decl.supers):
            return []
        return [s for s in decl.supers if idx.resolve(f, s.name) is None and not _platform_type(idx, f, s)]

    def _targets(self, ws: Workspace, idx: _RepoIndex, cluster: ErrorCluster) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for e in cluster.errors:
            ef = e.file
            if ef:
                ef = ef.replace("\\", "/")
                if Path(ef).is_absolute():
                    ef = ws.rel(ef).replace("\\", "/")
                if not ws.exists(ef):
                    ef = None
            texts = [t for t in (e.location and f"location: {e.location}", e.raw, e.message, e.symbol) if t]
            names: list[str] = []
            for t in texts:
                names += [_strip_generics(m.group(1)) for m in _LOC_VAR_RE.finditer(t)]
                names += [_strip_generics(m.group(1)) for m in _LOC_TYPE_RE.finditer(t)]
            for n in names:
                rf = idx.resolve(ef, n)
                if rf is None and ef is not None and "." not in n:
                    rf = idx.resolve(None, n)
                if rf and (rf, n.rsplit(".", 1)[-1]) not in out:
                    out.append((rf, n.rsplit(".", 1)[-1]))
        return out

    # -- editing ----------------------------------------------------------------------------------------------------
    def edit(self, ws: Workspace, cluster, profile, cfg) -> list[str]:
        idx = _RepoIndex(ws)
        fixes: dict[str, set[str]] = {}
        if cluster is None:
            files = sorted(idx._all())
            if not self._sd3_evidence(idx, files):
                return []
            for f in files:
                try:
                    decls = idx.ifaces(f)
                except OSError:
                    continue
                for name, decl in decls.items():
                    if self._needs_fix(idx, f, decl):
                        fixes.setdefault(f, set()).add(name)
        lib_fixes: dict[str, dict[str, _SuperRef]] = {}     # E14b: file → interface → library base to anchor on
        if cluster is not None:
            seen: set = set()
            libs: list[tuple[str, _Iface, list[_SuperRef]]] = []
            for f, name in self._targets(ws, idx, cluster):
                self._collect_tree(idx, f, name, 0, fixes, seen, libs)
            for f, decl, bases in libs:
                if decl.name in fixes.get(f, set()):
                    continue
                two = next((b for b in bases if b.args is not None and len(_split_args(b.args)) == 2), None)
                if two is None:
                    b = bases[0]
                    shown = b.name + (f"<{b.args}>" if b.args is not None else "")
                    self.note(f"follow-up: {decl.name} lost its CRUD methods through a library base interface "
                              f"({shown}) whose type arguments could not be mapped — add ListCrudRepository<T, ID> "
                              "by hand or upgrade the library")
                    continue
                lib_fixes.setdefault(f, {})[decl.name] = two
        changed: list[str] = []
        names: list[str] = []
        lib_names: list[str] = []
        for f in sorted(set(fixes) | set(lib_fixes)):
            anchors = lib_fixes.get(f, {})
            decls = [idx.ifaces(f)[n] for n in sorted(fixes.get(f, set()) | set(anchors))]
            new = self._fix_file(idx.text(f), decls, anchors)
            if new != idx.text(f):
                ws.write(f, new)
                changed.append(f)
                names += sorted(fixes.get(f, set()))
                lib_names += sorted(anchors)
        if changed:
            self.flag("spring-data-crud-restored")
        if names:
            self.note(f"added ListCrudRepository<T, ID> to {len(names)} repository interface(s) that extended only "
                      f"PagingAndSortingRepository (Spring Data 3 removed its CRUD methods): {', '.join(names)}")
        if lib_names:
            self.note(f"added ListCrudRepository<T, ID> to {len(lib_names)} repository interface(s) whose base "
                      "interface comes from a library built on Spring Data 2 (PagingAndSortingRepository lost its CRUD "
                      f"methods in Spring Data 3): {', '.join(lib_names)}")
        if changed:
            log.info("spring-data-crud-repository: %s", ", ".join(names + lib_names))
        return changed

    @staticmethod
    def _sd3_evidence(idx: _RepoIndex, files: list[str]) -> bool:
        """Pre-emptive guard: ListCrudRepository only exists from Spring Data 3 on. Proceed when the sources are
        already jakarta (the spring recipes ran) or name no javax EE package at all (nothing tells us otherwise)."""
        javax_ee = False
        for f in files:
            t = idx.text(f)
            if _JAKARTA_IMPORT_RE.search(t):
                return True
            if not javax_ee and rename_javax(t).replacements:
                javax_ee = True
        return not javax_ee

    @staticmethod
    def _fix_file(text: str, decls: list[_Iface], anchors: Optional[dict[str, _SuperRef]] = None) -> str:
        """Insert `, ListCrudRepository<args>` after the paging super (E14) or after the library base `anchors`
        names for the interface (E14b, its type arguments), plus the import."""
        edits: list[tuple[int, str]] = []
        need_import = False
        for d in decls:
            p = (anchors or {}).get(d.name) or d.paging
            if p is None:
                continue
            fqn = "." in p.name
            ref = (f"{SD_REPO_PKG}.ListCrudRepository" if fqn else "ListCrudRepository") + \
                (f"<{p.args}>" if p.args is not None else "")
            edits.append((p.end, f", {ref}"))
            need_import |= not fqn
        masked = _mask(text)
        pm = _PACKAGE_RE.search(masked)
        if need_import and not (pm and pm.group(1) == SD_REPO_PKG) and not re.search(
                r"^[ \t]*import\s+org\.springframework\.data\.repository\.(?:ListCrudRepository|\*)\s*;", masked, re.M):
            nl = "\r\n" if "\r\n" in text else "\n"
            line = f"import {SD_REPO_PKG}.ListCrudRepository;"
            anchor = re.search(r"^[ \t]*import\s+org\.springframework\.data\.repository\.(?:List)?"
                               r"PagingAndSortingRepository\s*;[^\r\n]*", masked, re.M)
            if anchor is None:
                imports = list(re.finditer(r"^[ \t]*import\s+[\w.*\s]+;[^\r\n]*", masked, re.M))
                anchor = imports[-1] if imports else pm
            if anchor is not None:
                pos = anchor.end()
                # keep any trailing comment on the anchor line in place: insert at the end of the line
                eol = re.compile(r"[^\r\n]*").match(text, pos)
                edits.append((eol.end() if eol else pos, nl + line))
            else:
                edits.append((0, line + nl))
        for pos, s in sorted(edits, key=lambda x: -x[0]):
            text = text[:pos] + s + text[pos:]
        return text


def source_handlers(cfg=None) -> list:
    """In precedence order (registered right after the edge handlers)."""
    return [JakartaNamespaceSweep(), SpringDataCrudRepository()]
