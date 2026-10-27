"""Class → jar → GAV attribution (catalog X1/X2: "which dependency still exposes javax.* in its API?").

* ``module_classpath(ws, cfg, module)``  → resolved test-scope classpath of a module (``dependency:build-classpath``),
  cached in ``<run_dir>/logs/classpath-<module or root>.txt`` and invalidated when the module's (or root) pom
  changes.
* ``locate_class(ws, cfg, fqcn, module)`` → ``ClassLocation`` (project source, repository jar with GAV, or a
  non-repository jar such as a ``systemPath`` one).
* ``locate_type_owner(ws, cfg, error, module)`` → the library class a javac error is about (``method … in class X``,
  ``location: class X``, ``incompatible types: … cannot be converted to X`` …), located.

Nothing here raises for Maven / IO failures: an unresolvable classpath gives ``[]`` and ``None`` locations (logged).
"""
from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from sbmig.core.config import Config
from sbmig.core.models import GAV, BuildError
from sbmig.core.workspace import Workspace, logs_dir

from .mvn import base_command, local_repo_path, mvn_env, run_logged

log = logging.getLogger("sbmig.build.classpath")

CLASSPATH_TIMEOUT_S = 15 * 60


class ClassLocation(BaseModel):
    artifact: Optional[GAV] = None         # repository jar → its coordinates; project source / foreign jar → None
    jar: str = ""                          # absolute jar (or classes dir) path; "" for project sources
    in_project: bool = False               # the class is a source of this project
    fqcn: str = ""                         # the class that was located (as given, generics stripped)
    source_file: Optional[str] = None      # project-relative .java when in_project


# ---------------------------------------------------------------------------
# module classpath
# ---------------------------------------------------------------------------
def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "root"


def _module_dir(ws: Workspace, module: Optional[str]) -> Optional[str]:
    """Project-relative module directory ("" = root) for a module dir path or artifactId; None if unknown."""
    if not module or module in (".", "/"):
        return ""
    m = module.strip("/")
    if ws.abs(m).is_dir() and ws.abs(f"{m}/pom.xml").is_file():
        return m
    for rel in ws.pom_files():
        if "/src/" in f"/{rel}":
            continue
        d = rel[:-len("pom.xml")].rstrip("/")
        if d and (d == m or d.rsplit("/", 1)[-1] == m):
            return d
    for rel in ws.pom_files():
        if "/src/" in f"/{rel}":
            continue
        try:
            text = ws.read(rel)
        except OSError:
            continue
        body = re.sub(r"<parent>.*?</parent>", "", text, count=1, flags=re.S)
        mm = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", body)
        if mm and mm.group(1) == m:
            return rel[:-len("pom.xml")].rstrip("/")
    return None


def _pom_stamp(ws: Workspace, mdir: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for rel in {"pom.xml", f"{mdir}/pom.xml" if mdir else "pom.xml"}:
        try:
            out[rel] = ws.abs(rel).stat().st_mtime_ns
        except OSError:
            out[rel] = 0
    return out


def _parse_classpath(text: str) -> list[Path]:
    entries: list[Path] = []
    for line in text.splitlines():
        for part in line.strip().split(os.pathsep):
            part = part.strip()
            if part and Path(part) not in entries:
                entries.append(Path(part))
    return entries


_PACKAGE_SKIPS = ("-Dmaven.main.skip=true", "-Dmaven.test.skip=true", "-Dspring-boot.repackage.skip=true",
                  "-Denforcer.skip=true", "-Dcheckstyle.skip=true", "-Dpmd.skip=true", "-Dspotbugs.skip=true",
                  "-Djacoco.skip=true", "-Dmaven.javadoc.skip=true", "-Dmaven.source.skip=true", "-Dgpg.skip=true",
                  "-DfailOnMissingWebXml=false")


def _has_modules(ws: Workspace) -> bool:
    try:
        return "<module>" in ws.read("pom.xml")
    except OSError:
        return False


def _run_build_classpath(ws: Workspace, cfg: Config, mdir: str, name: str, out_file: Path, *, package: bool):
    cmd = base_command(cfg)
    if mdir:
        cmd += ["-pl", mdir, "-am"]              # upstream reactor modules first; the target module writes last
    if package:
        cmd += ["package", *_PACKAGE_SKIPS]
    cmd += ["org.apache.maven.plugins:maven-dependency-plugin:3.8.1:build-classpath",
            f"-Dmdep.outputFile={out_file}", "-Dmdep.includeScope=test", "-q"]
    log_path = ws.new_log_path(f"classpath-{name}{'-pkg' if package else ''}")
    pr = run_logged(cmd, cwd=ws.root, env=mvn_env(cfg), log_path=log_path, timeout_s=CLASSPATH_TIMEOUT_S)
    return pr, log_path


def module_classpath(ws: Workspace, cfg: Config, module: Optional[str] = None) -> list[Path]:
    """Test-scope classpath of `module` (dir path or artifactId; None = root project). Never raises; [] on failure."""
    try:
        mdir = _module_dir(ws, module)
        if mdir is None:
            log.info("classpath: unknown module %r", module)
            return []
        out_dir = logs_dir(ws)                       # v2.1: logs/classpath-<module>.txt (+ .meta.json)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = _safe(mdir.replace("/", "__")) if mdir else "root"
        out_file = out_dir / f"classpath-{name}.txt"
        meta_file = out_dir / f"classpath-{name}.meta.json"
        stamp = _pom_stamp(ws, mdir)
        if out_file.is_file() and meta_file.is_file():
            try:
                if json.loads(meta_file.read_text(encoding="utf-8")).get("poms") == stamp:
                    return _parse_classpath(out_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        out_file.unlink(missing_ok=True)
        pr, log_path = _run_build_classpath(ws, cfg, mdir, name, out_file, package=False)
        if (pr.exit_code != 0 or not out_file.is_file()) and _has_modules(ws):
            # reactor siblings are only resolvable once packaged in the same session → package (compilation,
            # tests, repackage and checks skipped: empty jars are enough) and build the classpath in one run
            out_file.unlink(missing_ok=True)
            pr, log_path = _run_build_classpath(ws, cfg, mdir, name, out_file, package=True)
        if pr.exit_code != 0 or not out_file.is_file():
            log.warning("classpath: dependency:build-classpath failed for %s (exit %s, see %s)",
                        mdir or "root", pr.exit_code, log_path)
            ws.journal({"event": "classpath", "module": mdir or None, "ok": False, "log": str(log_path)})
            return []
        meta_file.write_text(json.dumps({"poms": stamp}), encoding="utf-8")
        entries = _parse_classpath(out_file.read_text(encoding="utf-8"))
        ws.journal({"event": "classpath", "module": mdir or None, "ok": True, "entries": len(entries)})
        return entries
    except Exception as exc:                     # never break the caller (report attribution is best effort)
        log.warning("classpath: %s: %s", type(exc).__name__, exc)
        return []


# ---------------------------------------------------------------------------
# jar → GAV
# ---------------------------------------------------------------------------
def gav_for_jar(jar: Path, repo: Optional[Path] = None) -> Optional[GAV]:
    """Map `<repo>/g/r/o/u/p/artifact/version/artifact-version[-classifier].jar` back to a GAV. When `repo` does not
    contain the path, a `.../repository/...` layout is recognised heuristically. None for foreign jars."""
    jar = Path(jar)
    parts: Optional[tuple[str, ...]] = None
    if repo is not None:
        try:
            parts = jar.resolve().relative_to(Path(repo).expanduser().resolve()).parts
        except (ValueError, OSError):
            try:
                parts = jar.relative_to(Path(repo).expanduser()).parts
            except ValueError:
                parts = None
    if parts is None:
        all_parts = jar.parts
        if "repository" not in all_parts:
            return None
        idx = len(all_parts) - 1 - list(reversed(all_parts)).index("repository")
        parts = all_parts[idx + 1:]
    if len(parts) < 4:
        return None
    *group, artifact, version, filename = parts
    stem = f"{artifact}-{version}"
    if not filename.startswith(stem):
        return None
    rest = filename[len(stem):]
    ext = rest.rsplit(".", 1)[-1] if "." in rest else ""
    rest = rest[: -(len(ext) + 1)] if ext else rest
    classifier = rest[1:] if rest.startswith("-") and len(rest) > 1 else None
    return GAV(group_id=".".join(group), artifact_id=artifact, version=version, classifier=classifier,
               type=None if ext in ("jar", "") else ext)


# ---------------------------------------------------------------------------
# class lookup
# ---------------------------------------------------------------------------
_JAR_CACHE: dict[str, tuple[int, frozenset[str]]] = {}     # jar path → (mtime_ns, class entries)


def _jar_entries(jar: Path) -> frozenset[str]:
    key = str(jar)
    try:
        mtime = jar.stat().st_mtime_ns
    except OSError:
        return frozenset()
    hit = _JAR_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with zipfile.ZipFile(jar) as zf:
            names = frozenset(n for n in zf.namelist() if n.endswith(".class"))
    except (zipfile.BadZipFile, OSError) as exc:
        log.debug("classpath: cannot list %s: %s", jar, exc)
        names = frozenset()
    _JAR_CACHE[key] = (mtime, names)
    return names


def clean_fqcn(name: str) -> str:
    n = re.sub(r"<.*>", "", name or "").strip()
    n = n.replace("[]", "").replace("...", "").strip().rstrip(".,;:)")
    return n


def _class_entries(fqcn: str) -> list[tuple[str, str]]:
    """(class entry, top-level source path) candidates: `a.b.C.D` → a/b/C/D.class, a/b/C$D.class, a/b$C$D.class …"""
    fq = clean_fqcn(fqcn)
    if not fq:
        return []
    if "$" in fq:
        outer = fq.split("$", 1)[0]
        return [(fq.replace(".", "/") + ".class", outer.replace(".", "/") + ".java")]
    names = fq.split(".")
    out: list[tuple[str, str]] = []
    for k in range(len(names), 0, -1):
        outer = "/".join(names[:k])
        inner = names[k:]
        entry = outer + ("$" + "$".join(inner) if inner else "") + ".class"
        out.append((entry, outer + ".java"))
    return out


def _find_project_source(ws: Workspace, fqcn: str) -> Optional[str]:
    cands = [src for _, src in _class_entries(fqcn) if "/" in src]
    if not cands:
        return None
    files = ws.java_files()
    for src in cands:
        for f in files:
            if f == src or f.endswith("/" + src):
                return f
    return None


def locate_class(ws: Workspace, cfg: Config, fqcn: str, module: Optional[str] = None) -> Optional[ClassLocation]:
    """Where does `fqcn` come from? Project sources first, then the jars (and class dirs) of the module classpath."""
    try:
        fq = clean_fqcn(fqcn)
        if not fq or "." not in fq:
            return None
        src = _find_project_source(ws, fq)
        if src:
            return ClassLocation(in_project=True, jar="", fqcn=fq, source_file=src)
        entries = _class_entries(fq)
        repo = local_repo_path(cfg)
        root = ws.root.resolve()
        for cp in module_classpath(ws, cfg, module):
            cp = Path(cp)
            if cp.is_dir():
                if any((cp / e).is_file() for e, _ in entries):
                    try:
                        in_proj = cp.resolve().is_relative_to(root)
                    except OSError:
                        in_proj = False
                    return ClassLocation(in_project=in_proj, jar=str(cp), fqcn=fq)
                continue
            if cp.suffix.lower() not in (".jar", ".zip", ".war"):
                continue
            names = _jar_entries(cp)
            if not names or not any(e in names for e, _ in entries):
                continue
            try:
                in_proj = cp.resolve().is_relative_to(root)      # reactor sibling jar (target/*.jar)
            except OSError:
                in_proj = False
            return ClassLocation(artifact=None if in_proj else gav_for_jar(cp, repo), jar=str(cp),
                                 in_project=in_proj, fqcn=fq)
        return None
    except Exception as exc:
        log.warning("locate_class(%s): %s: %s", fqcn, type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# javac error → owner class
# ---------------------------------------------------------------------------
_FQ = r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+"
_IN_CLASS_RE = re.compile(r"\b(?:method|constructor|variable|field)\s+[\w$<>]+\s+in\s+(?:class|interface|enum|record)\s+"
                          r"(" + _FQ + r")")
_LOCATION_RE = re.compile(r"location:\s+(?:class|interface|enum|record)\s+(" + _FQ + r")")
_LOCATION_VAR_RE = re.compile(r"location:\s+variable\s+\w+\s+of\s+type\s+(" + _FQ + r")")
_OVERRIDE_RE = re.compile(r"does not override(?: abstract)? method .*? in (" + _FQ + r")")
_INCOMPATIBLE_RE = re.compile(r"incompatible types:\s*(?P<found>\S+?) cannot be converted to (?P<req>" + _FQ + r")")
_REQUIRED_RE = re.compile(r"required:\s*(?P<req>" + _FQ + r")")
_FOUND_RE = re.compile(r"found:\s*(?P<found>" + _FQ + r")")
_CANNOT_ACCESS_RE = re.compile(r"cannot access (" + _FQ + r")")
_SUITABLE_RE = re.compile(r"(?:constructor|method)\s+(" + _FQ + r")\.[\w$<>]+\(")


def _error_text(error: BuildError) -> str:
    parts = [error.message or "", f"location: {error.location}" if error.location else "", error.raw or ""]
    return "\n".join(p for p in parts if p)


def type_mismatch(error: BuildError) -> tuple[Optional[str], Optional[str]]:
    """(required type, found type) of an `incompatible types` / `cannot be applied` error, when stated."""
    text = _error_text(error)
    m = _INCOMPATIBLE_RE.search(text)
    if m:
        return clean_fqcn(m.group("req")), clean_fqcn(m.group("found"))
    req = _REQUIRED_RE.search(text)
    found = _FOUND_RE.search(text)
    return (clean_fqcn(req.group("req")) if req else None, clean_fqcn(found.group("found")) if found else None)


def owner_fqcn(error: BuildError) -> Optional[str]:
    """The class an error is *about*: `… in class X`, `location: class X`, `variable v of type X`, `does not override
    … in X`, `cannot access X`, `no suitable … X.m(`; for a bare `incompatible types` the required type."""
    text = _error_text(error)
    for rx in (_IN_CLASS_RE, _OVERRIDE_RE, _SUITABLE_RE, _LOCATION_VAR_RE, _LOCATION_RE, _CANNOT_ACCESS_RE):
        m = rx.search(text)
        if m:
            return clean_fqcn(m.group(1))
    req, _found = type_mismatch(error)
    return req


_API_PREFIXES = ("javax.", "jakarta.", "java.", "org.springframework.", "org.slf4j.", "lombok.")
_NEW_RE = re.compile(r"\bnew\s+([A-Z][\w$]*)\s*(?:<[^>]*>)?\s*\(")
_STATIC_CALL_RE = re.compile(r"\b([A-Z][\w$]*)\s*\.\s*[a-z_]\w*\s*\(")
_INSTANCE_CALL_RE = re.compile(r"\b([a-z_]\w*)\s*\.\s*[a-z_]\w*\s*\(")
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", re.M)


def callee_candidates(ws: Workspace, rel_file: str, line: Optional[int]) -> list[str]:
    """Fully-qualified classes whose constructor/method is CALLED on the error line (resolved through the file's
    imports). This is how we find the library behind a bare `incompatible types` / `cannot access` javac error,
    which never names the callee: e.g. `new LegacyRequestHelper(request)` → com.company.legacy.web.LegacyRequestHelper."""
    if not rel_file or not line:
        return []
    try:
        text = ws.read(rel_file)
    except Exception:
        return []
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return []
    src = lines[line - 1]
    simple: list[str] = []
    simple += _NEW_RE.findall(src)
    simple += _STATIC_CALL_RE.findall(src)
    # instance calls: find the declared type of the variable in this file (`LegacyRequestHelper helper = …`)
    for var in _INSTANCE_CALL_RE.findall(src):
        if var in ("this", "super"):
            continue
        m = re.search(r"\b([A-Z][\w$]*)(?:<[^>]*>)?\s+" + re.escape(var) + r"\b", text)
        if m:
            simple.append(m.group(1))
    if not simple:
        return []
    imports = _IMPORT_RE.findall(text)
    explicit = {imp.rsplit(".", 1)[-1]: imp for imp in imports if not imp.endswith(".*")}
    wildcards = [imp[:-2] for imp in re.findall(r"^\s*import\s+([\w.]+\.\*)\s*;", text, re.M)]
    pkg_m = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    own_pkg = pkg_m.group(1) if pkg_m else ""
    out: list[str] = []
    for name in dict.fromkeys(simple):
        fq = explicit.get(name)
        cands = [fq] if fq else [f"{w}.{name}" for w in wildcards] + ([f"{own_pkg}.{name}"] if own_pkg else [])
        for c in cands:
            if c and c not in out and not c.startswith(("java.", "javax.", "jakarta."))\
                    and not c.startswith("org.springframework."):
                out.append(c)
    return out


def locate_type_owner(ws: Workspace, cfg: Config, error: BuildError,
                      module: Optional[str] = None) -> Optional[ClassLocation]:
    """`locate_class` for the library class *behind* a javac error. Order: the owner named in the message
    (`in class X`, `location: class X` …) unless it is an API type (javax/jakarta/java/spring); then the classes
    called on the error line (`callee_candidates`); finally the required type itself. Never raises."""
    mod = module if module is not None else error.module
    try:
        owner = owner_fqcn(error)
        req, _found = type_mismatch(error)
        if owner and owner != req and not owner.startswith(_API_PREFIXES):
            loc = locate_class(ws, cfg, owner, mod)
            if loc is not None:
                return loc
        for fq in callee_candidates(ws, error.file or "", error.line):
            loc = locate_class(ws, cfg, fq, mod)
            if loc is not None and not loc.in_project:
                return loc
        if owner:
            return locate_class(ws, cfg, owner, mod)
        return None
    except Exception as exc:
        log.warning("locate_type_owner: %s: %s", type(exc).__name__, exc)
        return None
