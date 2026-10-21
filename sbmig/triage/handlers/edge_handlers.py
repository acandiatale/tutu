"""Edge-case handlers (catalog docs/ERROR-HANDLING.md §2: E1 encoding, E2 BOM, E4 enforcer, E5 -Werror,
E6 carry-over managed versions, E7 systemPath jars).

Registered by `default_handlers()` in pom_handlers.py BEFORE the generic handlers (first matching handler wins), in
this order: source-encoding, strip-utf8-bom, carry-over-managed-version, enforcer-relax, remove-werror,
systempath-relocate.

Every handler is reactive (`matches` + `apply(ws, cluster, profile, cfg)`) and idempotent. After `apply` each
instance exposes `flags` (report flags such as ``source-encoding-set:cp949``, ``enforcer-skipped``,
``werror-removed``) and `notes` (human follow-ups such as "install … into Nexus"); both are reset at every apply.
`source-encoding` and `strip-utf8-bom` also work pre-emptively (`cluster=None`: scan the whole project) — needed
because on JDK 21 javac only *warns* about unmappable characters: the build succeeds and Korean literals silently
become U+FFFD.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar, Optional

from sbmig.core.config import Config
from sbmig.core.models import ErrorCluster, ErrorKind, FileChange, ProjectProfile
from sbmig.core.workspace import Workspace, detect_encoding

from .base import Handler, all_poms, cluster_text, poms_for_cluster, root_pom, version_lt
from .pom_handlers import _MISSING_RE, PomHandler, _declared, _resolve, known_versions, version_for

log = logging.getLogger("sbmig.triage.handlers.edge")

_UTF8 = {"utf-8", "utf_8", "utf8", "utf-8-sig", "utf_8_sig", "ascii", "us-ascii"}
# python codec name → Java charset name accepted by javac / maven (-encoding, project.build.sourceEncoding)
JAVA_CHARSETS = {"cp949": "MS949", "euc_kr": "EUC-KR", "euc-kr": "EUC-KR", "shift_jis": "Shift_JIS",
                 "cp932": "MS932", "latin_1": "ISO-8859-1", "latin-1": "ISO-8859-1", "iso8859-1": "ISO-8859-1",
                 "iso8859_1": "ISO-8859-1", "cp1252": "windows-1252", "utf-8": "UTF-8", "utf_8": "UTF-8",
                 "gb2312": "GB2312", "gbk": "GBK", "big5": "Big5"}
_SOURCE_KINDS = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE, ErrorKind.PLUGIN_EXECUTION, ErrorKind.UNKNOWN})


def _norm_codec(name: str) -> str:
    import codecs
    try:
        return codecs.lookup(name).name
    except LookupError:
        return name.lower()


def java_charset(codec: str) -> str:
    n = _norm_codec(codec)
    return JAVA_CHARSETS.get(n) or JAVA_CHARSETS.get(codec.lower()) or codec.upper()


def _is_utf8(enc: str) -> bool:
    return enc.lower() in _UTF8 or _norm_codec(enc) in _UTF8


def _module_dir(pom_rel: str) -> str:
    return posixpath.dirname(pom_rel)


def _java_files_under(ws: Workspace, mdir: str) -> list[str]:
    prefix = (mdir.rstrip("/") + "/") if mdir else ""
    return [f for f in ws.java_files() if f.startswith(prefix)]


def _cluster_files(cluster: Optional[ErrorCluster]) -> list[str]:
    out: list[str] = []
    for e in (cluster.errors if cluster else []):
        if e.file and e.file.endswith(".java") and e.file not in out:
            out.append(e.file)
    return out


def _is_ascii(ws: Workspace, rel: str) -> bool:
    try:
        return ws.abs(rel).read_bytes().isascii()
    except OSError:
        return True


class EdgeHandler(PomHandler):
    """PomHandler with per-apply `flags` / `notes` for the orchestrator (`getattr(handler, "flags", [])`)."""

    def __init__(self) -> None:
        super().__init__()
        self.flags: list[str] = []
        self.notes: list[str] = []

    def apply(self, ws: Workspace, cluster: Optional[ErrorCluster] = None,
              profile: Optional[ProjectProfile] = None, cfg: Optional[Config] = None) -> list[FileChange]:
        self.flags, self.notes = [], []
        changes = super().apply(ws, cluster, profile, cfg)
        if not changes:
            self.flags = []
        return changes

    def flag(self, f: str) -> None:
        if f not in self.flags:
            self.flags.append(f)

    def note(self, n: str) -> None:
        if n not in self.notes:
            self.notes.append(n)


def _set_props(handler: PomHandler, ws: Workspace, rel: str, values: dict[str, str]) -> bool:
    ed = handler.editor(ws, rel)
    ch = False
    for k, v in values.items():
        if ed.get_property(k) != v:
            ch |= bool(ed.set_property(k, v))
    if ch:
        ed.save()
    return ch


def _plugin_blocks(text: str, artifact: str) -> list[tuple[int, int]]:
    """(start, end) spans of every <plugin> element whose artifactId is `artifact` (build + pluginManagement)."""
    out: list[tuple[int, int]] = []
    for m in re.finditer(r"<plugin>(?:(?!</plugin>).)*?</plugin>", text, re.S):
        if re.search(r"<artifactId>\s*" + re.escape(artifact) + r"\s*</artifactId>", m.group(0)):
            out.append((m.start(), m.end()))
    return out


# ---------------------------------------------------------------------------
# E1 source encoding
# ---------------------------------------------------------------------------
class SourceEncoding(EdgeHandler):
    id = "source-encoding"
    description = ("legacy MS949/EUC-KR sources: set project.build.sourceEncoding to the detected encoding "
                   "(policy set-property) or convert the sources to UTF-8 (policy convert-utf8)")
    kinds = _SOURCE_KINDS
    triggers = (r"unmappable character .*for encoding (?i:UTF-?8|US-ASCII|ASCII)", r"error: unmappable character",
                r"illegal character: '\\ufffd'", r"illegal character: \\65533",   # U+FFFD = legacy bytes read as UTF-8
                r"unmappable character \(0x[0-9A-Fa-f]+\)")
    PROPS = ("project.build.sourceEncoding", "project.reporting.outputEncoding")

    def _scope(self, ws: Workspace, cluster: Optional[ErrorCluster], profile) -> tuple[list[str], list[str]]:
        """(pom targets, java files to inspect)."""
        if cluster is None:
            poms = [p for p in [root_pom(ws, profile)] if p]
            return poms, ws.java_files()
        poms = poms_for_cluster(ws, cluster, profile)
        files: list[str] = []
        for p in poms:
            files += [f for f in _java_files_under(ws, _module_dir(p)) if f not in files]
        for f in _cluster_files(cluster):
            if f not in files:
                files.append(f)
        r = root_pom(ws, profile)
        if r and r not in poms:
            poms.append(r)
        return poms, files

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        if cfg is not None:
            ws.set_encoding_candidates(cfg.build.encoding_candidates)
        policy = cfg.build.source_encoding_policy if cfg is not None else "set-property"
        poms, files = self._scope(ws, cluster, profile)
        legacy: dict[str, str] = {}
        utf8_non_ascii = 0
        for f in files:
            enc = ws.encoding_of(f)
            if _is_utf8(enc):
                if not _is_ascii(ws, f):
                    utf8_non_ascii += 1
                continue
            legacy[f] = _norm_codec(enc)
        if not legacy:
            # failing files are UTF-8 → maybe a pom forces a legacy encoding: realign to UTF-8 only when we can see it
            return self._realign_utf8(ws, poms) if cluster is not None else []
        majority = Counter(legacy.values()).most_common(1)[0][0]
        changed: list[str] = []
        if policy == "convert-utf8" or utf8_non_ascii:
            if utf8_non_ascii and policy != "convert-utf8":
                self.note(f"mixed source encodings ({len(legacy)} {majority} file(s), {utf8_non_ascii} non-ASCII "
                          "UTF-8 file(s)): legacy files converted to UTF-8 instead of setting a module-wide encoding")
            for f, enc in legacy.items():
                text = ws.read(f, encoding=enc)
                ws.write(f, text, encoding="utf-8")
                changed.append(f)
            target = "UTF-8"
            self.flag(f"source-encoding-converted:{majority}")
        else:
            target = java_charset(majority)
            self.flag(f"source-encoding-set:{majority}")
        for rel in self._pom_targets(ws, poms):
            if _set_props(self, ws, rel, {k: target for k in self.PROPS}):
                changed.append(rel)
            if self._compiler_encoding(ws, rel, target):
                changed.append(rel)
        if changed:
            log.info("source-encoding: %s (%d legacy file(s), majority %s)", target, len(legacy), majority)
        return changed

    def _pom_targets(self, ws: Workspace, poms: list[str]) -> list[str]:
        out = list(poms)
        for rel in all_poms(ws):         # a module overriding the property would win over the root's value
            if rel not in out and re.search(r"<project\.build\.sourceEncoding>", ws.read(rel)):
                out.append(rel)
        return out

    def _realign_utf8(self, ws: Workspace, poms: list[str]) -> list[str]:
        changed: list[str] = []
        for rel in self._pom_targets(ws, poms):
            ed = self.editor(ws, rel)
            cur = ed.get_property("project.build.sourceEncoding")
            if cur is not None and "${" not in cur and cur.strip().upper().replace("_", "-") not in ("UTF-8", "UTF8"):
                if _set_props(self, ws, rel, {"project.build.sourceEncoding": "UTF-8"}):
                    changed.append(rel)
                    self.flag("source-encoding-set:utf-8")
        return changed

    @staticmethod
    def _compiler_encoding(ws: Workspace, rel: str, target: str) -> bool:
        text = ws.read(rel)
        new = text
        for s, e in reversed(_plugin_blocks(text, "maven-compiler-plugin")):
            block = new[s:e]
            nb = re.sub(r"(<encoding>\s*)([^<]+?)(\s*</encoding>)",
                        lambda m: m.group(0) if m.group(2).strip() in (target, "${project.build.sourceEncoding}")
                        else m.group(1) + "${project.build.sourceEncoding}" + m.group(3), block)
            new = new[:s] + nb + new[e:]
        if new != text:
            ws.write(rel, new)
            return True
        return False


# ---------------------------------------------------------------------------
# E2 UTF-8 BOM
# ---------------------------------------------------------------------------
class StripUtf8Bom(EdgeHandler):
    id = "strip-utf8-bom"
    description = "remove the UTF-8 byte order mark javac rejects (illegal character: '\\ufeff')"
    kinds = _SOURCE_KINDS
    triggers = (r"illegal character: '\\ufeff'", "illegal character: '﻿'", r"illegal character: '\\65279'",
                r"\\65279")

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        if cluster is None:
            files = ws.java_files()
        else:
            files = _cluster_files(cluster)
            for p in poms_for_cluster(ws, cluster, profile):   # every BOM file of the module in one go
                files += [f for f in _java_files_under(ws, _module_dir(p)) if f not in files]
        changed: list[str] = []
        for f in files:
            try:
                head = ws.abs(f).read_bytes()[:3]
            except OSError:
                continue
            if head != b"\xef\xbb\xbf":
                continue
            ws.write(f, ws.read(f, encoding="utf-8-sig"), encoding="utf-8")
            changed.append(f)
        if changed:
            self.flag(f"utf8-bom-stripped:{len(changed)}")
        return changed


# ---------------------------------------------------------------------------
# E6 carry over versions managed by the as-is parent chain
# ---------------------------------------------------------------------------
def _effective_data(ws: Optional[Workspace], profile: Optional[ProjectProfile]) -> dict[str, Any]:
    from sbmig.build.effective import DEPMGMT_FILE, load_effective
    if ws is not None:
        return load_effective(ws)
    # matches() has no workspace: Workspace.from_input lays out <run_dir>/project → <run_dir>/effective
    if profile is not None and profile.root:
        p = Path(profile.root).parent / "effective" / DEPMGMT_FILE
        if p.is_file():
            import json
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
    return {}


def _effective_lookup(data: dict[str, Any], key: str, modules: list[str]) -> Optional[str]:
    """g:a version from the declaring module's own as-is chain (any candidate name), else the merged view."""
    if not data:
        return None
    from sbmig.build.effective import effective_version
    for m in modules:
        for mkey, own in (data.get("by_module") or {}).items():
            if mkey == m or mkey.split(":")[-1] == m:
                v = (own.get("managed") or {}).get(key) or (own.get("direct") or {}).get(key)
                if v:
                    return v
    return effective_version(None, key, data=data)  # type: ignore[arg-type]


class CarryOverManagedVersion(EdgeHandler):
    id = "carry-over-managed-version"
    description = ("pin versions the as-is parent chain managed (captured from the as-is effective pom at intake) "
                   "on dependencies whose version went missing after the parent swap; else versions.yml")
    kinds = frozenset({ErrorKind.POM_INVALID, ErrorKind.UNKNOWN})

    def _module_candidates(self, cluster: Optional[ErrorCluster], cfg: Optional[Config]) -> list[str]:
        out: list[str] = []
        for e in (cluster.errors if cluster else []):
            if e.module:
                out.append(e.module)
                if cfg is not None:
                    for suf in (cfg.layout.comp_artifact_suffix, cfg.layout.boot_artifact_suffix):
                        if suf and e.module.endswith(suf):
                            out.append(e.module[: -len(suf)])
        return out

    def _version(self, key: str, data: dict[str, Any], modules: list[str],
                 cfg: Optional[Config]) -> tuple[Optional[str], str]:
        if key.startswith("org.springframework.boot:"):
            return version_for(key, cfg), "target-boot"          # never pin the old Boot line
        v = _effective_lookup(data, key, modules)
        known = known_versions().get(key)
        if v and known and version_lt(v, known) is True:
            return known, "versions.yml (as-is version below the known-good floor)"
        if v:
            return v, "as-is effective pom"
        if known:
            return known, "versions.yml"
        return None, ""

    def missing(self, ws: Optional[Workspace], cluster: Optional[ErrorCluster], profile, cfg
                ) -> list[tuple[str, str, str, str]]:
        out: list[tuple[str, str, str, str]] = []
        if cluster is None:
            return out
        data = _effective_data(ws, profile)
        modules = self._module_candidates(cluster, cfg)
        for e in cluster.errors:
            for m in _MISSING_RE.finditer(f"{e.message}\n{e.raw}"):
                g, a = m.group(1), m.group(2)
                v, src = self._version(f"{g}:{a}", data, modules, cfg)
                if v and not any(x[:2] == (g, a) for x in out):
                    out.append((g, a, v, src))
        return out

    def matches(self, cluster, profile, cfg) -> bool:
        if cluster.kind not in self.kinds or not _MISSING_RE.search(cluster_text(cluster)):
            return False
        # only claim the cluster when the as-is effective pom knows a version; otherwise the generic
        # missing-dependency-version handler (versions.yml / target Boot) or the LLM/block path takes over
        data = _effective_data(None, profile)
        if not data:
            return False
        modules = self._module_candidates(cluster, cfg)
        for m in _MISSING_RE.finditer(cluster_text(cluster)):
            key = f"{m.group(1)}:{m.group(2)}"
            if not key.startswith("org.springframework.boot:") and _effective_lookup(data, key, modules):
                return True
        return False

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        items = self.missing(ws, cluster, profile, cfg)
        if not items:
            return []
        targets = poms_for_cluster(ws, cluster, profile)
        changed: list[str] = []
        for rel in [*targets, *[p for p in all_poms(ws) if p not in targets]]:
            ed = self.editor(ws, rel)
            ch = False
            for g, a, v, src in items:
                d = _declared(ed, f"{g}:{a}")
                if d is None:
                    continue
                if d.version and "${" not in d.version:
                    continue
                if d.version and _resolve(ed, d.version) != d.version:
                    continue                      # ${prop} resolves locally → not the culprit
                if ed.set_dependency_version(g, a, v):
                    ch = True
                    self.note(f"{g}:{a} pinned to {v} ({src}) — it was managed by the as-is parent")
            if ch:
                ed.save()
                changed.append(rel)
        if changed:
            self.flag("managed-versions-carried-over")
        return changed


# ---------------------------------------------------------------------------
# E4 enforcer rules
# ---------------------------------------------------------------------------
_RULE_NAMES = {"requirejavaversion": "requireJavaVersion", "dependencyconvergence": "dependencyConvergence",
               "banneddependencies": "bannedDependencies",
               "requireupperbounddeps": "requireUpperBoundDeps", "requiremavenversion": "requireMavenVersion",
               "requirepluginversions": "requirePluginVersions", "requirereleasedeps": "requireReleaseDeps",
               "enforcebytecodeversion": "enforceBytecodeVersion", "banduplicatepomdependencyversions":
               "banDuplicatePomDependencyVersions"}


def _failed_rules(cluster: Optional[ErrorCluster]) -> list[str]:
    out: list[str] = []
    for e in (cluster.errors if cluster else []):
        text = f"{e.message}\n{e.raw}".lower()
        for low, name in _RULE_NAMES.items():
            if re.search(r"\b" + low + r"\b", text) and name not in out:
                out.append(name)
    return out


class EnforcerRelax(EdgeHandler):
    id = "enforcer-relax"
    description = ("enforcer rules failing on JDK 21: relax requireJavaVersion to [21,), skip the execution of "
                   "dependencyConvergence/bannedDependencies; fallback / policy skip: enforcer.skip=true (flagged)")
    kinds = frozenset({ErrorKind.ENFORCER, ErrorKind.PLUGIN_EXECUTION})
    triggers = (r"(?i)enforcer", r"(?i)RequireJavaVersion", r"(?i)DependencyConvergence", r"(?i)BannedDependencies")

    def applicable(self, cluster, profile, cfg) -> bool:
        policy = cfg.build.enforcer_policy if cfg is not None else "relax-rule"
        if policy == "keep":
            return False
        if cluster is not None and cluster.kind == ErrorKind.PLUGIN_EXECUTION:
            return bool(re.search(r"maven-enforcer-plugin|enforcer:enforce", cluster_text(cluster)))
        return True

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        policy = cfg.build.enforcer_policy if cfg is not None else "relax-rule"
        if policy == "keep":
            return []
        changed: list[str] = []
        if policy == "relax-rule":
            rules = _failed_rules(cluster) if cluster is not None else ["requireJavaVersion"]
            jv = cfg.target.java_version if cfg is not None else "21"
            handled: set[str] = set()
            for rel in all_poms(ws):
                text = ws.read(rel)
                new = text
                for rule in rules:
                    if f"<{rule}" not in new:
                        continue
                    if rule == "requireJavaVersion":
                        nxt = self._relax_java(new, jv)
                    else:
                        nxt = self._skip_execution(new, rule)
                    if nxt is not None:
                        handled.add(rule)
                        new = nxt
                if new != text:
                    ws.write(rel, new)
                    changed.append(rel)
            if changed:
                self.flag("enforcer-relaxed")
            if rules and set(rules) <= handled:
                return changed
        # policy skip, or the failing rule is not defined in this project (in-house parent) → skip enforcer
        r = root_pom(ws, profile)
        if r and _set_props(self, ws, r, {"enforcer.skip": "true"}):
            changed.append(r)
            self.flag("enforcer-skipped")
            self.note("maven-enforcer-plugin skipped (enforcer.skip=true): review the rules for JDK 21")
        return changed

    @staticmethod
    def _relax_java(text: str, jv: str) -> Optional[str]:
        """Rewrite <requireJavaVersion><version>…</version> to "[jv,)" unless it already admits jv.
        Returns the new text, or the same text when the rule already admits jv (= handled), None if absent."""
        found = False

        def repl(m: re.Match[str]) -> str:
            nonlocal found
            found = True
            block = m.group(0)
            vm = re.search(r"(<version>\s*)([^<]*?)(\s*</version>)", block)
            if vm is None:
                return block
            cur = vm.group(2).strip()
            if _range_admits(cur, jv):
                return block
            return block[:vm.start()] + vm.group(1) + f"[{jv},)" + vm.group(3) + block[vm.end():]

        new = re.sub(r"<requireJavaVersion\b[^>]*>.*?</requireJavaVersion>", repl, text, flags=re.S)
        return new if found else None

    @staticmethod
    def _skip_execution(text: str, rule: str) -> Optional[str]:
        """Add <skip>true</skip> to the configuration of the enforcer execution (or plugin) declaring `rule`."""
        found = False
        new = text
        for s, e in reversed(_plugin_blocks(text, "maven-enforcer-plugin")):
            block = new[s:e]
            if f"<{rule}" not in block:
                continue
            nb = block
            execs = list(re.finditer(r"<execution>.*?</execution>", block, re.S))
            hit = [m for m in execs if f"<{rule}" in m.group(0)]
            if hit:
                for m in reversed(hit):
                    found = True
                    ex = m.group(0)
                    nb = nb[:m.start()] + _add_skip(ex, "execution") + nb[m.end():]
            else:
                found = True
                # rule declared in the plugin-level configuration
                nb = _add_skip(block, "plugin")
            new = new[:s] + nb + new[e:]
        return new if found else None


def _range_admits(spec: str, jv: str) -> bool:
    """Does a Maven version spec ("[1.8,9)", "1.8", "[17,)", "${x}") admit Java `jv`? Unknown → False."""
    if "${" in spec or not spec:
        return False
    j = int(jv.split(".")[0]) if jv.split(".")[0].isdigit() else 21
    spec = spec.strip()
    if not spec.startswith(("[", "(")):
        v = spec.split(".")
        major = int(v[1]) if v[0] == "1" and len(v) > 1 and v[1].isdigit() else (int(v[0]) if v[0].isdigit() else 0)
        return major <= j                                  # plain version = minimum
    m = re.fullmatch(r"([\[(])\s*([^,]*)\s*,\s*([^\])]*)\s*([\])])", spec)
    if not m:
        return False

    def major(v: str) -> Optional[int]:
        v = v.strip()
        if not v:
            return None
        p = v.split(".")
        if p[0] == "1" and len(p) > 1 and p[1].isdigit():
            return int(p[1])
        return int(p[0]) if p[0].isdigit() else None
    lo, hi = major(m.group(2)), major(m.group(3))
    if lo is not None and (j < lo or (m.group(1) == "(" and j == lo)):
        return False
    if hi is not None and (j > hi or (m.group(4) == ")" and j >= hi)):
        return False
    return True


def _add_skip(xml: str, scope: str) -> str:
    """Insert <skip>true</skip> into the first own <configuration> of an <execution>/<plugin> fragment."""
    if scope == "plugin":
        # plugin-level configuration: outside <executions>
        stripped = re.sub(r"<executions>.*?</executions>", lambda m: " " * len(m.group(0)), xml, flags=re.S)
    else:
        stripped = xml
    m = re.search(r"<configuration\s*>", stripped)
    if m is None:
        close = "</execution>" if scope == "execution" else "</plugin>"
        idx = xml.rfind(close)
        ind = _line_indent(xml, idx)
        return xml[:idx] + "  <configuration><skip>true</skip></configuration>\n" + ind + xml[idx:]
    end = re.search(r"</configuration>", stripped[m.end():])
    body = xml[m.end(): m.end() + end.start()] if end else ""
    if re.search(r"<skip>\s*true\s*</skip>", body):
        return xml
    if re.search(r"<skip>[^<]*</skip>", body):
        return xml[:m.end()] + re.sub(r"<skip>[^<]*</skip>", "<skip>true</skip>", body, count=1) + \
            xml[m.end() + len(body):]
    ind = _line_indent(xml, m.start())
    return xml[:m.end()] + f"\n{ind}  <skip>true</skip>" + xml[m.end():]


def _line_indent(text: str, idx: int) -> str:
    ls = text.rfind("\n", 0, idx) + 1
    line = text[ls:idx]
    return line[: len(line) - len(line.lstrip())]


# ---------------------------------------------------------------------------
# E5 -Werror
# ---------------------------------------------------------------------------
_WERROR_RE = re.compile(r"warnings found and -Werror specified")
_BENIGN_RE = re.compile(r"^(?:Compilation failure|COMPILATION (?:ERROR|WARNING)|warning:|\[WARNING\]|"
                        r"Some messages have been simplified|\d+ (?:error|warning)s?$|"
                        r".*has been deprecated|.*deprecat)", re.I)


class RemoveWerror(EdgeHandler):
    id = "remove-werror"
    description = "-Werror makes JDK 21 deprecation warnings fatal: drop it from the compiler configuration (flagged)"
    kinds = _SOURCE_KINDS
    triggers = (_WERROR_RE.pattern,)

    def matches(self, cluster, profile, cfg) -> bool:
        if cfg is not None and cfg.build.werror_policy == "keep":
            return False
        if not self.triggered(cluster):
            return False
        # only clusters made of the -Werror failure (and the warnings it promoted), not real compile errors
        for e in cluster.errors:
            text = f"{e.message}\n{e.raw}"
            if _WERROR_RE.search(text) or _BENIGN_RE.match((e.message or "").strip()):
                continue
            return False
        return True

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        if cfg is not None and cfg.build.werror_policy == "keep":
            return []
        changed: list[str] = []
        for rel in all_poms(ws):
            text = ws.read(rel)
            new = text
            # <arg>-Werror</arg> lines (compilerArgs)
            new = re.sub(r"(?m)^[ \t]*<arg>\s*-Werror\s*</arg>[ \t]*\r?\n", "", new)
            new = re.sub(r"<arg>\s*-Werror\s*</arg>", "", new)
            # <compilerArgument>… -Werror …</compilerArgument>
            def ca(m: re.Match[str]) -> str:
                rest = " ".join(t for t in m.group(2).split() if t != "-Werror")
                return f"{m.group(1)}{rest}{m.group(3)}" if rest else ""
            new = re.sub(r"(<compilerArgument>\s*)([^<]*-Werror[^<]*?)(\s*</compilerArgument>)", ca, new)
            new = re.sub(r"(?m)^[ \t]*<compilerArgument>\s*</compilerArgument>[ \t]*\r?\n", "", new)
            new = re.sub(r"<compilerArgument>\s*</compilerArgument>", "", new)
            # <failOnWarning>true</failOnWarning> (compiler plugin ≥ 3.6) and the user property
            for s, e in reversed(_plugin_blocks(new, "maven-compiler-plugin")):
                block = new[s:e]
                nb = re.sub(r"<failOnWarning>\s*true\s*</failOnWarning>", "<failOnWarning>false</failOnWarning>",
                            block)
                new = new[:s] + nb + new[e:]
            if new != text:
                ws.write(rel, new)
                changed.append(rel)
            ed = self.editor(ws, rel)
            if (ed.get_property("maven.compiler.failOnWarning") or "").strip().lower() == "true":
                if ed.set_property("maven.compiler.failOnWarning", "false"):
                    ed.save()
                    changed.append(rel)
        if changed:
            self.flag("werror-removed")
            self.note("-Werror removed from the compiler configuration: JDK 21 deprecation warnings are no longer "
                      "fatal; review them")
        return changed


# ---------------------------------------------------------------------------
# E7 system-scoped jars whose systemPath broke (restructure moved the module)
# ---------------------------------------------------------------------------
_SYS_DEP_RE = re.compile(r"<dependency>(?:(?!</dependency>).)*?<scope>\s*system\s*</scope>(?:(?!</dependency>).)*?"
                         r"</dependency>", re.S)


def _system_deps(root: Path, pom_rel: str, text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pom_dir = (root / pom_rel).parent
    pm = re.search(r"<properties>(.*?)</properties>", text, re.S)
    props = dict(re.findall(r"<([\w.\-]+)>\s*([^<]*?)\s*</\1>", pm.group(1))) if pm else {}
    for m in _SYS_DEP_RE.finditer(text):
        block = m.group(0)
        sp = re.search(r"<systemPath>\s*([^<]+?)\s*</systemPath>", block)
        g = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", block)
        a = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", block)
        v = re.search(r"<version>\s*([^<]+?)\s*</version>", block)
        if not (sp and g and a):
            continue
        raw = sp.group(1)
        val = raw
        for k in ("project.basedir", "basedir"):
            val = val.replace("${" + k + "}", str(pom_dir))
        val = re.sub(r"\$\{([^}]+)\}", lambda mm: props.get(mm.group(1), mm.group(0)), val)
        resolved = Path(val) if os.path.isabs(val) else pom_dir / val
        out.append({"pom": pom_rel, "group": g.group(1), "artifact": a.group(1),
                    "version": v.group(1) if v else None, "raw": raw, "resolved": resolved,
                    "exists": "${" not in val and resolved.is_file(), "span": (m.start(), m.end())})
    return out


def broken_system_deps(root: Path, poms: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rel in poms:
        try:
            text = (root / rel).read_text(encoding=detect_encoding(root / rel), errors="replace")
        except OSError:
            continue
        out += [d for d in _system_deps(root, rel, text) if not d["exists"]]
    return out


def _project_poms(root: Path) -> list[str]:
    out: list[str] = []
    for p in root.rglob("pom.xml"):
        parts = p.relative_to(root).parts
        if ".git" in parts or "target" in parts or "src" in parts:
            continue
        out.append(p.relative_to(root).as_posix())
    return sorted(out)


def _jar_candidates(root: Path, dep: dict[str, Any], cfg: Optional[Config]) -> list[Path]:
    """Where a moved system jar may live now: <root>/<old relative path> (as-is single module),
    <root>/module/comp-jar/<old relative path>, or the only file with that name in the project."""
    raw = dep["raw"]
    rel_old = re.sub(r"^\$\{(?:project\.)?basedir\}[/\\]?", "", raw).replace("\\", "/")
    name = Path(rel_old).name
    out: list[Path] = []
    if rel_old and not os.path.isabs(rel_old) and "${" not in rel_old and not re.match(r"^[A-Za-z]:/", rel_old):
        out.append(root / rel_old)
        # the comp module of the configured layout: company `<root>/{name}-back` (name unknown here → every dir
        # matching the template) or the legacy `module/comp-jar`
        import fnmatch
        lay = cfg.layout if cfg else None
        tpl = (lay.comp_module if lay else "comp-jar") or "comp-jar"
        mdir = (lay.module_dir if lay else "module").strip("/")
        base = root / mdir if mdir else root
        pattern = tpl.replace("{name}", "*")
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and fnmatch.fnmatch(d.name, pattern):
                    out.append(d / rel_old)
        if "{name}" not in tpl:
            out.append(base / tpl / rel_old)
    if name and "${" not in name:
        hits = [p for p in root.rglob(name) if p.is_file() and ".git" not in p.relative_to(root).parts
                and "target" not in p.relative_to(root).parts]
        if len(hits) == 1:
            out.append(hits[0])
    return out


class SystemPathRelocate(EdgeHandler):
    id = "systempath-relocate"
    description = ("system-scoped jars whose systemPath no longer exists after restructure: point systemPath at "
                   "the jar's new location (${project.basedir}/…); follow-up: install it into Nexus")
    kinds = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.POM_INVALID, ErrorKind.COMPILE,
                       ErrorKind.TEST_COMPILE, ErrorKind.UNKNOWN})
    triggers = (r"at specified path", r"refers to a non-existing file", r"systemPath",
                r"package \S+ does not exist", r"Could not find artifact")

    def matches(self, cluster, profile, cfg) -> bool:
        if cluster.kind not in self.kinds or not self.triggered(cluster):
            return False
        if profile is None or not profile.root or not Path(profile.root).is_dir():
            text = cluster_text(cluster)
            return bool(re.search(r"at specified path|refers to a non-existing file", text))
        root = Path(profile.root)
        # only claim clusters we can actually fix (the jar exists somewhere in the project); otherwise the
        # artifact-not-found block / add-known-dependency path keeps the cluster
        broken = [d for d in broken_system_deps(root, _project_poms(root))
                  if any(c.is_file() for c in _jar_candidates(root, d, cfg))]
        if not broken:
            return False
        if cluster.kind in (ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.POM_INVALID):
            keys = {f"{d['group']}:{d['artifact']}" for d in broken}
            arts = {e.artifact.key for e in cluster.errors if e.artifact is not None}
            text = cluster_text(cluster)
            return bool(arts & keys) or any(k in text for k in keys)
        return True                                       # compile error while a system jar is unreachable

    def edit(self, ws, cluster, profile, cfg) -> list[str]:
        changed: list[str] = []
        for rel in all_poms(ws):
            text = ws.read(rel)
            deps = [d for d in _system_deps(ws.root, rel, text) if not d["exists"]]
            if not deps:
                continue
            new = text
            pom_dir = ws.abs(rel).parent
            for d in sorted(deps, key=lambda x: x["span"][0], reverse=True):
                target = next((c for c in _jar_candidates(ws.root, d, cfg) if c.is_file()), None)
                if target is None:
                    self.note(f"{d['group']}:{d['artifact']}: system jar {d['raw']} not found anywhere in the "
                              "project — install it into Nexus and declare it as a normal dependency")
                    continue
                relp = os.path.relpath(target, pom_dir).replace(os.sep, "/")
                value = "${project.basedir}/" + relp
                s, e = d["span"]
                block = new[s:e]
                nb = re.sub(r"(<systemPath>\s*)[^<]+?(\s*</systemPath>)", lambda m: m.group(1) + value + m.group(2),
                            block, count=1)
                if nb != block:
                    new = new[:s] + nb + new[e:]
                    coords = ":".join(x for x in (d["group"], d["artifact"], d["version"]) if x)
                    self.note(f"{coords}: systemPath now {value} — install it into Nexus and drop the system scope")
            if new != text:
                ws.write(rel, new)
                changed.append(rel)
        if changed:
            self.flag("systempath-relocated")
        return changed


def edge_handlers(cfg: Optional[Config] = None) -> list[Handler]:
    """In precedence order (all before the generic pom handlers)."""
    return [SourceEncoding(), StripUtf8Bom(), CarryOverManagedVersion(), EnforcerRelax(), RemoveWerror(),
            SystemPathRelocate()]
