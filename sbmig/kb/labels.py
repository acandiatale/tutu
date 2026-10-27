"""Deterministic fix labels: *how* a fix changed the code, derived from the FileChange set and the
errors (no LLM).

``label_changes(changes, errors)`` → :class:`LabelResult` (labels most-specific first, an ``old → new``
replacement when the labels imply one, the error-case symbol and machine-readable details). The error records
(:mod:`sbmig.kb.error_cases`) label every fix attempt — a v2 fix script's diff included — with it;
``symbol_for_error(s)`` gives the cross-project error symbol (records, case catalog names and matching).

The heuristics are deliberately simple; anything they cannot explain is ``generic-rewrite``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

from sbmig.build.signatures import _symbol_name, classify_javac
from sbmig.core.models import BuildError, ErrorKind, FileChange

ARROW = " → "

# most specific first; labels are always returned in this order
LABEL_ORDER: tuple[str, ...] = (
    "package-rename",
    "annotation-replace",
    "type-rename",
    "import-rename",
    "method-rename",
    "override-signature",
    "method-signature-adapt",
    "dependency-replace",
    "dependency-version",
    "dependency-add",
    "dependency-remove",
    "plugin-version",
    "pom-property",
    "spring-property-rename",
    "config-value",
    "remove-code",
    "add-code",
    "generic-rewrite",
)

@dataclass
class LabelResult:
    labels: list[str] = field(default_factory=list)
    replacement: str = ""
    symbol: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- symbol
def symbol_for_error(e: BuildError) -> str:
    if (
        e.kind in (ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION, ErrorKind.PARENT_RESOLUTION)
        and e.artifact is not None
    ):
        return e.artifact.key
    msg = e.message or ""
    if classify_javac(msg) == "package_does_not_exist":
        m = re.search(r"package (\S+) does not exist", msg)
        if m:
            return f"package {m.group(1)}"
    if e.symbol:
        name = _symbol_name(e.symbol)
        if name:
            return name
    if e.artifact is not None:
        return e.artifact.key
    parts = (e.signature_generic or "").split("|")
    if len(parts) >= 3 and parts[2] and " " not in parts[2] and len(parts[2]) <= 120:
        return parts[2]
    return ""


def symbol_for_errors(errors: Iterable[BuildError]) -> str:
    """The error-case symbol: javac symbol simple name, 'package X', the artifact key g:a, or the L2 key."""
    for e in errors or []:
        s = symbol_for_error(e)
        if s:
            return s
    return ""


# --------------------------------------------------------------------------- helpers
def _basename(p: str) -> str:
    return PurePosixPath((p or "").replace("\\", "/")).name


def _is_pom(path: str) -> bool:
    return _basename(path) == "pom.xml"


def _is_resource_config(path: str) -> Optional[str]:
    p = "/" + (path or "").replace("\\", "/").lstrip("/")
    if "/src/main/resources/" not in p and "/src/test/resources/" not in p:
        return None
    name = _basename(p).lower()
    if name.endswith(".properties"):
        return "properties"
    if name.endswith((".yml", ".yaml")):
        return "yaml"
    return None


def _is_java(path: str) -> bool:
    return (path or "").endswith(".java")


def _old_new(ch: FileChange) -> tuple[str, str]:
    if ch.op == "modify":
        return ch.search or "", ch.replace or ""
    if ch.op == "create":
        return "", ch.content or ""
    if ch.op == "delete":
        return ch.content or "", ""
    return "", ""


def _simple(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[-1]


def _pkg(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[0] if "." in fqcn else ""


def _add(labels: list[str], label: str) -> None:
    if label not in labels:
        labels.append(label)


# --------------------------------------------------------------------------- pom
_DEP_RE = re.compile(r"<dependency>(.*?)</dependency>", re.DOTALL)
_PLUGIN_RE = re.compile(r"<plugin>(.*?)</plugin>", re.DOTALL)
_LEAF_RE = re.compile(r"<([A-Za-z_][\w.\-]*)>\s*([^<>]*?)\s*</\1>")
_GAV_TAGS = {
    "groupId",
    "artifactId",
    "version",
    "scope",
    "type",
    "classifier",
    "optional",
    "relativePath",
    "packaging",
    "modelVersion",
    "name",
    "description",
    "url",
    "module",
    "phase",
    "goal",
    "id",
    "source",
    "target",
    "release",
    "encoding",
    "include",
    "exclude",
    "exclusion",
}
_STRUCT_RE = re.compile(
    r"<(parent|exclusions|executions|configuration|build|dependencyManagement|pluginManagement)"
    r">.*?</\1>",
    re.DOTALL,
)


def _tag(block: str, tag: str) -> Optional[str]:
    m = re.search(
        rf"<{tag}>\s*([^<]*?)\s*</{tag}>", re.sub(r"<exclusions>.*?</exclusions>", "", block, flags=re.S)
    )
    return m.group(1) if m else None


def _blocks(rx: re.Pattern[str], text: str, default_group: Optional[str] = None) -> list[dict]:
    out = []
    for m in rx.finditer(text):
        b = m.group(1)
        g = _tag(b, "groupId") or default_group
        a = _tag(b, "artifactId")
        if a:
            out.append({"g": g or "", "a": a, "v": _tag(b, "version"), "scope": _tag(b, "scope")})
    return out


def _bare_block(text: str) -> Optional[dict]:
    """A fragment without <dependency>/<plugin> wrapper that still carries coordinates (partial
    search/replace)."""
    if "<dependency>" in text or "<plugin>" in text:
        return None
    a, v = _tag(text, "artifactId"), _tag(text, "version")
    if not a and not v:
        return None
    return {"g": _tag(text, "groupId") or "", "a": a or "", "v": v, "scope": _tag(text, "scope")}


def _properties(text: str) -> dict[str, str]:
    t = _DEP_RE.sub("", text)
    t = _PLUGIN_RE.sub("", t)
    t = _STRUCT_RE.sub("", t)
    return {k: v for k, v in _LEAF_RE.findall(t) if k not in _GAV_TAGS}


def _key(d: dict) -> str:
    return f"{d['g']}:{d['a']}" if d.get("g") else d.get("a", "")


def _label_pom(old: str, new: str, errors: list[BuildError], res: LabelResult) -> None:
    labels, det = res.labels, res.details
    deps_o, deps_n = _blocks(_DEP_RE, old), _blocks(_DEP_RE, new)
    plugins_o = _blocks(_PLUGIN_RE, old, "org.apache.maven.plugins")
    plugins_n = _blocks(_PLUGIN_RE, new, "org.apache.maven.plugins")
    bare_o, bare_n = _bare_block(old), _bare_block(new)
    if bare_o is not None or bare_n is not None:
        bo, bn = (
            bare_o or {"g": "", "a": "", "v": None, "scope": None},
            bare_n or {"g": "", "a": "", "v": None, "scope": None},
        )
        for b in (bo, bn):  # coordinates missing from the fragment → the error
            if not b["a"]:
                art = next((e.artifact for e in errors if e.artifact is not None), None)
                if art is not None:
                    b["g"], b["a"] = art.group_id, art.artifact_id
        if bo["a"] or bn["a"]:
            bo["a"] = bo["a"] or bn["a"]
            bn["a"] = bn["a"] or bo["a"]
            bo["g"] = bo["g"] or bn["g"]
            bn["g"] = bn["g"] or bo["g"]
            deps_o, deps_n = deps_o + [bo], deps_n + [bn]
        elif bo["v"] != bn["v"]:
            _add(labels, "dependency-version")
            det.setdefault("dependency_versions", []).append({"key": "", "old": bo["v"], "new": bn["v"]})
    _diff_artifacts(deps_o, deps_n, res, kind="dependency")
    _diff_artifacts(plugins_o, plugins_n, res, kind="plugin")
    props_o, props_n = _properties(old), _properties(new)
    for k in sorted(set(props_o) | set(props_n)):
        a, b = props_o.get(k), props_n.get(k)
        if a != b:
            _add(labels, "pom-property")
            det.setdefault("pom_properties", []).append({"key": k, "old": a, "new": b})


def _diff_artifacts(old: list[dict], new: list[dict], res: LabelResult, kind: str) -> None:
    labels, det = res.labels, res.details
    by_o = {_key(d): d for d in old}
    by_n = {_key(d): d for d in new}
    removed = [k for k in by_o if k not in by_n]
    added = [k for k in by_n if k not in by_o]
    for k in by_o:
        if k in by_n and by_o[k]["v"] != by_n[k]["v"]:
            if kind == "plugin":
                _add(labels, "plugin-version")
                det.setdefault("plugin_versions", []).append(
                    {"key": k, "old": by_o[k]["v"], "new": by_n[k]["v"]}
                )
            else:
                _add(labels, "dependency-version")
                det.setdefault("dependency_versions", []).append(
                    {"key": k, "old": by_o[k]["v"], "new": by_n[k]["v"]}
                )
    if kind == "plugin":
        if removed or added:
            _add(labels, "generic-rewrite")
        return
    if len(removed) == 1 and len(added) == 1:
        _add(labels, "dependency-replace")
        det.setdefault("dependency_replace", []).append(
            {"old": removed[0], "new": added[0], "new_version": by_n[added[0]]["v"]}
        )
        return
    for k in added:
        _add(labels, "dependency-add")
        d = by_n[k]
        det.setdefault("dependency_add", []).append({"key": k, "version": d["v"], "scope": d["scope"]})
    for k in removed:
        _add(labels, "dependency-remove")
        det.setdefault("dependency_remove", []).append({"key": k})


# --------------------------------------------------------------------------- properties / yaml
_PROP_LINE_RE = re.compile(r"^\s*([^#!\s=:][^=:\s]*)\s*[=:]\s*(.*?)\s*$")
_YAML_LINE_RE = re.compile(r"^(\s*)(?:-\s+)?([\w.\-\[\]\"']+)\s*:(?:\s+(.*?))?\s*$")


def _parse_properties(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in text.splitlines():
        if ln.strip().startswith(("#", "!")):
            continue
        m = _PROP_LINE_RE.match(ln)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _parse_yaml_fragment(text: str) -> dict[str, str]:
    """Indentation-based flattening that also works on partial fragments: {'spring.datasource.url': '…'}."""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = _YAML_LINE_RE.match(ln)
        if not m:
            continue
        indent, key, val = len(m.group(1)), m.group(2).strip("\"'"), (m.group(3) or "")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full = ".".join([*(k for _, k in stack), key])
        if val and not val.startswith("#"):
            out[full] = val.strip("\"'")
        else:
            stack.append((indent, key))
    return out


def _label_config(old: str, new: str, fmt: str, res: LabelResult) -> None:
    parse = _parse_properties if fmt == "properties" else _parse_yaml_fragment
    a, b = parse(old), parse(new)
    removed = [k for k in a if k not in b]
    added = [k for k in b if k not in a]
    renames: list[tuple[str, str]] = []
    for k in list(removed):  # same value under a new key → rename
        hit = next((n for n in added if b[n] == a[k]), None)
        if hit is not None:
            renames.append((k, hit))
            removed.remove(k)
            added.remove(hit)
    if len(removed) == 1 and len(added) == 1:
        renames.append((removed[0], added[0]))
        if a[removed[0]] != b[added[0]]:
            _add(res.labels, "config-value")
            res.details.setdefault("config_values", []).append(
                {"key": added[0], "old": a[removed[0]], "new": b[added[0]]}
            )
        removed, added = [], []
    for o, n in renames:
        _add(res.labels, "spring-property-rename")
        res.details.setdefault("property_renames", []).append({"old": o, "new": n})
    for k in a:
        if k in b and a[k] != b[k]:
            _add(res.labels, "config-value")
            res.details.setdefault("config_values", []).append({"key": k, "old": a[k], "new": b[k]})
    if removed or added:
        _add(res.labels, "config-value")
        res.details.setdefault("config_keys", []).append({"removed": removed, "added": added})


# --------------------------------------------------------------------------- java
_IMPORT_RE = re.compile(r"^import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;")
_TOKEN_RE = re.compile(r"@?[A-Za-z_$][\w$]*|\S")
_METHOD_DECL_RE = re.compile(
    r"^(?:@\w+\s+)*(?:(?:public|protected|private|static|final|abstract|synchronized|default)\s+)*"
    r"(?:<[^>]+>\s+)?[\w.<>\[\],?\s]+\s+\w+\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\s*[{;]?\s*$"
)
_CALL_RE = re.compile(r"\w\s*\(")


def _norm(line: str) -> str:
    return re.sub(r"\s+", "", line)


def _code_lines(text: str) -> list[str]:
    return [
        ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("//", "*", "/*"))
    ]


def _imports(lines: list[str]) -> list[str]:
    out = []
    for ln in lines:
        m = _IMPORT_RE.match(ln)
        if m:
            out.append(m.group(2))
    return out


def _pair_imports(removed: list[str], added: list[str]) -> list[tuple[str, str]]:
    rem, add = list(removed), list(added)
    pairs: list[tuple[str, str]] = []
    for o in list(rem):  # same simple name, different package
        hit = next((n for n in add if _simple(n) == _simple(o)), None)
        if hit is not None:
            pairs.append((o, hit))
            rem.remove(o)
            add.remove(hit)
    if len(rem) == 1 and len(add) == 1:  # a single leftover pair → renamed type
        pairs.append((rem[0], add[0]))
        rem, add = [], []
    return pairs


def _package_rename(pairs: list[tuple[str, str]]) -> Optional[tuple[str, str, bool]]:
    """(old_pkg, new_pkg, recursive) when every pair is the same package move (≥2 types, or a wildcard
    import)."""
    if not pairs:
        return None
    if len(pairs) == 1 and not (pairs[0][0].endswith(".*") and pairs[0][1].endswith(".*")):
        return None
    norm = []
    for o, n in pairs:
        if o.endswith(".*") and n.endswith(".*"):
            norm.append((o[:-2], n[:-2]))
        elif _simple(o) == _simple(n):
            norm.append((_pkg(o), _pkg(n)))
        else:
            return None
    cand_o, cand_n = min(norm, key=lambda p: len(p[0]))
    if cand_o == cand_n or not cand_o:
        return None
    recursive = False
    for o, n in norm:
        if o == cand_o and n == cand_n:
            continue
        if o.startswith(cand_o + ".") and n == cand_n + o[len(cand_o) :]:
            recursive = True
            continue
        return None
    return cand_o, cand_n, recursive


def _token_diff(a: str, b: str) -> tuple[list[tuple[list[str], list[str]]], list[str]]:
    ta, tb = _TOKEN_RE.findall(a), _TOKEN_RE.findall(b)
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            out.append((ta[i1:i2], tb[j1:j2]))
    return out, ta


def _classify_line_pair(
    old: str,
    new: str,
    type_pairs: list[tuple[str, str]],
    context: str,
    res: LabelResult,
    errors: list[BuildError],
) -> str:
    diffs, toks = _token_diff(old, new)
    simple_pairs = {(_simple(o), _simple(n)) for o, n in type_pairs}
    if diffs and all(len(x) == 1 and len(y) == 1 for x, y in diffs):
        reps = [(x[0], y[0]) for x, y in diffs]
        if all(x.startswith("@") and y.startswith("@") for x, y in reps):
            for x, y in reps:
                res.details.setdefault("annotations", []).append({"old": x[1:], "new": y[1:]})
            return "annotation-replace"
        if all((x.lstrip("@"), y.lstrip("@")) in simple_pairs for x, y in reps):
            return "type-rename"
        if (
            len(reps) == 1
            and re.match(r"^[a-z_$][\w$]*$", reps[0][0])
            and re.match(r"^[a-z_$][\w$]*$", reps[0][1])
        ):
            x, y = reps[0]
            idx = next((i for i, t in enumerate(toks) if t == x), -1)
            before = toks[idx - 1] if idx > 0 else ""
            after = toks[idx + 1] if 0 <= idx < len(toks) - 1 else ""
            before2 = toks[idx - 2] if idx > 1 else ""
            # `.old(` call, or `::old` method reference (the tokenizer splits `::` into two `:` tokens)
            if (before == "." and after == "(") or (before == ":" and before2 == ":"):
                owner = _method_owner(x, errors)
                res.details.setdefault("method_renames", []).append({"owner": owner, "old": x, "new": y})
                return "method-rename"
    if _METHOD_DECL_RE.match(new) and _METHOD_DECL_RE.match(old) and "@Override" in context:
        return "override-signature"
    if _CALL_RE.search(old) and _CALL_RE.search(new):
        return "method-signature-adapt"
    return "generic-rewrite"


def _method_owner(method: str, errors: list[BuildError]) -> str:
    for e in errors:
        if e.symbol and _symbol_name(e.symbol) == method and e.location:
            m = re.search(r"(?:class|interface|enum|record)\s+([\w.$]+)", e.location) or re.search(
                r"of type ([\w.$]+)", e.location
            )
            if m:
                return m.group(1)
    return ""


def _label_java(pairs_src: list[tuple[str, str]], errors: list[BuildError], res: LabelResult) -> None:
    removed_all: list[str] = []
    added_all: list[str] = []
    line_pairs: list[tuple[str, str, str]] = []
    only_removed: list[str] = []
    only_added: list[str] = []
    for old, new in pairs_src:
        a, b = _code_lines(old), _code_lines(new)
        sm = difflib.SequenceMatcher(a=[_norm(x) for x in a], b=[_norm(x) for x in b], autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            ra, rb = a[i1:i2], b[j1:j2]
            removed_all += ra
            added_all += rb
            ca = [x for x in ra if not _IMPORT_RE.match(x)]
            cb = [x for x in rb if not _IMPORT_RE.match(x)]
            n = min(len(ca), len(cb))
            for k in range(n):
                line_pairs.append((ca[k], cb[k], old))
            only_removed += ca[n:]
            only_added += cb[n:]
    imp_o, imp_n = _imports(removed_all), _imports(added_all)
    type_pairs = _pair_imports(imp_o, imp_n)
    if type_pairs:
        res.details["type_pairs"] = [list(p) for p in type_pairs]
    pkg = _package_rename(type_pairs)
    if pkg:
        res.details["package_rename"] = {"old": pkg[0], "new": pkg[1], "recursive": pkg[2]}
    unpaired_imports = len(imp_o) + len(imp_n) - 2 * len(type_pairs)
    kinds: list[str] = []
    for o, n, ctx in line_pairs:
        k = _classify_line_pair(o, n, type_pairs, ctx, res, errors)
        if k not in kinds:
            kinds.append(k)
    # the import-only part
    if type_pairs:
        if pkg:
            _add(res.labels, "package-rename")
        elif "type-rename" in kinds or "annotation-replace" in kinds:
            pass
        else:
            _add(res.labels, "import-rename")
    for k in kinds:
        _add(res.labels, k)
    if "annotation-replace" in kinds and type_pairs:
        ann = {(d["old"], d["new"]) for d in res.details.get("annotations", [])}
        if any((_simple(o), _simple(n)) in ann for o, n in type_pairs):
            _add(res.labels, "type-rename")
    if only_removed or (imp_o and not imp_n and not type_pairs):
        _add(res.labels, "remove-code")
    if only_added or (imp_n and not imp_o and not type_pairs):
        _add(res.labels, "add-code")
    if unpaired_imports and type_pairs and not (only_removed or only_added):
        _add(res.labels, "add-code" if len(imp_n) > len(imp_o) else "remove-code")


# --------------------------------------------------------------------------- public: label_changes
def _replacement(res: LabelResult) -> str:
    det = res.details
    for label in res.labels:
        if label == "package-rename" and det.get("package_rename"):
            p = det["package_rename"]
            return f"{p['old']}{ARROW}{p['new']}"
        if label in ("type-rename", "import-rename", "annotation-replace") and det.get("type_pairs"):
            ann = det.get("annotations") or []
            pairs = det["type_pairs"]
            if (
                label == "annotation-replace"
                and ann
                and not any((_simple(o), _simple(n)) == (a["old"], a["new"]) for o, n in pairs for a in ann)
            ):
                return f"@{ann[0]['old']}{ARROW}@{ann[0]['new']}"
            return "; ".join(f"{o}{ARROW}{n}" for o, n in pairs[:3])
        if label == "annotation-replace" and det.get("annotations"):
            a = det["annotations"][0]
            return f"@{a['old']}{ARROW}@{a['new']}"
        if label == "method-rename" and det.get("method_renames"):
            m = det["method_renames"][0]
            owner = f"{_simple(m['owner'])}." if m.get("owner") else ""
            return f"{owner}{m['old']}{ARROW}{m['new']}"
        if label == "dependency-replace" and det.get("dependency_replace"):
            d = det["dependency_replace"][0]
            return f"{d['old']}{ARROW}{d['new']}"
        if label == "dependency-version" and det.get("dependency_versions"):
            d = det["dependency_versions"][0]
            return f"{d['key'] + ' ' if d['key'] else ''}{d['old'] or '?'}{ARROW}{d['new'] or '?'}"
        if label == "dependency-add" and det.get("dependency_add"):
            d = det["dependency_add"][0]
            return f"+ {d['key']}{' ' + d['version'] if d.get('version') else ''}"
        if label == "dependency-remove" and det.get("dependency_remove"):
            return f"- {det['dependency_remove'][0]['key']}"
        if label == "plugin-version" and det.get("plugin_versions"):
            d = det["plugin_versions"][0]
            return f"{d['key']} {d['old'] or '?'}{ARROW}{d['new'] or '?'}"
        if label == "pom-property" and det.get("pom_properties"):
            d = det["pom_properties"][0]
            return (
                f"{d['key']}: {d['old'] if d['old'] is not None else '∅'}{ARROW}"
                f"{d['new'] if d['new'] is not None else '∅'}"
            )
        if label == "spring-property-rename" and det.get("property_renames"):
            d = det["property_renames"][0]
            return f"{d['old']}{ARROW}{d['new']}"
        if label == "config-value" and det.get("config_values"):
            d = det["config_values"][0]
            return f"{d['key']}: {d['old']}{ARROW}{d['new']}"
    return ""


def label_changes(changes: list[FileChange], errors: Optional[list[BuildError]] = None) -> LabelResult:
    """Deterministically label a fix (see FixLabel in sbmig.core.models). Never raises."""
    errors = list(errors or [])
    res = LabelResult(symbol=symbol_for_errors(errors))
    try:
        java: list[tuple[str, str]] = []
        for ch in changes or []:
            old, new = _old_new(ch)
            if ch.op == "rename":
                _add(res.labels, "generic-rewrite")
                continue
            if _is_pom(ch.path):
                if ch.op != "modify":
                    _add(res.labels, "generic-rewrite")
                    continue
                _label_pom(old, new, errors, res)
                continue
            fmt = _is_resource_config(ch.path)
            if fmt:
                _label_config(old, new, fmt, res)
                continue
            if _is_java(ch.path):
                if ch.op == "delete":
                    _add(res.labels, "remove-code")
                elif ch.op == "create":
                    _add(res.labels, "add-code")
                else:
                    java.append((old, new))
                continue
            if old and not new:
                _add(res.labels, "remove-code")
            elif new and not old:
                _add(res.labels, "add-code")
            elif old != new:
                _add(res.labels, "generic-rewrite")
        if java:
            _label_java(java, errors, res)
        if not res.labels and changes:
            res.labels.append("generic-rewrite")
    except Exception as e:  # pragma: no cover - labels are best effort
        res.details["error"] = f"{type(e).__name__}: {e}"
        if "generic-rewrite" not in res.labels:
            res.labels.append("generic-rewrite")
    res.labels = sorted(set(res.labels), key=LABEL_ORDER.index)
    res.replacement = _replacement(res)
    return res
