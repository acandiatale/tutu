"""Strict validation of declarative OpenRewrite recipes produced by the LLM (KB recipe candidates).

Only a small whitelist of mechanical recipes is allowed, each with its required option keys (and a few known
optional keys). Anything else — unknown recipe types, unknown/missing options, a name that is not one PascalCase
word, more than 12 entries, ``$``/shell/script-looking content — is rejected.

KB recipe names are one PascalCase word without dots (``LegacyRequestHelperClientIpToRemoteClientIp``, 2026-10-25).
Every OpenRewrite and company recipe name is fully qualified (it has dots), so a KB recipe can never shadow one.
Names written before that date carry the prefix ``com.company.kb.``; they stay valid and are read as they are,
and ``short_recipe_name`` drops the prefix when the LLM still writes it.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import yaml

RECIPE_TYPE = "specs.openrewrite.org/v1beta/recipe"
LEGACY_NAME_PREFIX = "com.company.kb."         # KB recipe names before 2026-10-25 (still accepted)
MAX_RECIPE_ENTRIES = 12
MAX_VALUE_LEN = 300

# recipe id -> (required option keys, allowed optional option keys)
WHITELIST: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "org.openrewrite.java.ChangeType": (
        ("oldFullyQualifiedTypeName", "newFullyQualifiedTypeName"), ("ignoreDefinition",)),
    "org.openrewrite.java.ChangePackage": (("oldPackageName", "newPackageName"), ("recursive",)),
    "org.openrewrite.java.ChangeMethodName": (
        ("methodPattern", "newMethodName"), ("matchOverrides", "ignoreDefinition")),
    "org.openrewrite.java.ChangeStaticFieldToMethod": (
        ("oldClassName", "oldFieldName", "newClassName", "newMethodName"), ("newTarget",)),
    "org.openrewrite.java.ReplaceConstant": (("owningType", "constantName", "literalValue"), ()),
    "org.openrewrite.java.RemoveAnnotation": (("annotationPattern",), ()),
    "org.openrewrite.java.dependencies.ChangeDependency": (
        ("oldGroupId", "oldArtifactId", "newGroupId", "newArtifactId"),
        ("newVersion", "versionPattern", "overrideManagedVersion")),
    "org.openrewrite.java.dependencies.UpgradeDependencyVersion": (
        ("groupId", "artifactId", "newVersion"), ("versionPattern", "overrideManagedVersion")),
    "org.openrewrite.java.dependencies.AddDependency": (
        ("groupId", "artifactId", "version"),
        ("scope", "onlyIfUsing", "type", "classifier", "optional", "versionPattern", "acceptTransitive")),
    "org.openrewrite.java.dependencies.RemoveDependency": (("groupId", "artifactId"), ("scope",)),
    "org.openrewrite.maven.ChangePluginGroupIdAndArtifactId": (
        ("oldGroupId", "oldArtifactId"), ("newGroupId", "newArtifactId")),
    "org.openrewrite.maven.UpgradePluginVersion": (
        ("groupId", "artifactId", "newVersion"), ("versionPattern", "addVersionIfMissing")),
    "org.openrewrite.maven.ChangePropertyValue": (("key", "newValue"), ("addIfMissing", "trustParent")),
    "org.openrewrite.maven.RemoveProperty": (("propertyName",), ()),
    "org.openrewrite.java.spring.ChangeSpringPropertyKey": (("oldPropertyKey", "newPropertyKey"), ("except",)),
    "org.openrewrite.java.spring.ChangeSpringPropertyValue": (
        ("propertyKey", "newValue"), ("oldValue", "regex", "relaxedBinding")),
    "org.openrewrite.properties.ChangePropertyKey": (
        ("oldPropertyKey", "newPropertyKey"), ("relaxedBinding",)),
    "org.openrewrite.yaml.ChangePropertyKey": (
        ("oldPropertyKey", "newPropertyKey"), ("relaxedBinding", "except")),
    "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta": ((), ()),
}

ALLOWED_TOP_KEYS = {"type", "name", "displayName", "description", "recipeList", "tags"}
_NAME_RE = re.compile(r"^(?:com\.company\.kb\.)?[A-Z][A-Za-z0-9]*$")
_NAME_LINE_RE = re.compile(r"^name:[^\n]*$", re.MULTILINE)
# anything that looks like interpolation, shell or script content (checked on the whole text)
_DANGEROUS_TEXT_RE = re.compile(r"\$|`|<script|#!/|&&|\|\||\beval\(|\bexec\(|Runtime\.getRuntime|ProcessBuilder")
# extra checks for option values
_DANGEROUS_VALUE_RE = re.compile(r";|\brm\s+-|\bcurl\s|\bwget\s|\bbash\b|\bsh\s+-c|[\x00-\x08\x0b-\x1f]")
_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    """Remove a surrounding ```yaml … ``` fence (or the first fenced block if prose surrounds it)."""
    t = (text or "").strip()
    m = _FENCE_RE.match(t)
    if m:
        return m.group(1).strip()
    inner = re.search(r"```[A-Za-z0-9_-]*\s*\n(.*?)\n\s*```", t, re.DOTALL)
    if inner:
        return inner.group(1).strip()
    return t


def is_none_answer(text: str) -> bool:
    t = strip_fences(text).strip().strip(".").strip()
    return t.upper() == "NONE" or t.upper().startswith("NONE\n") or t == ""


def _check_scalar(value: Any, where: str, errors: list[str]) -> None:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return
    if not isinstance(value, str):
        errors.append(f"{where}: value must be a scalar, got {type(value).__name__}")
        return
    if not value.strip():
        errors.append(f"{where}: empty value")
    if len(value) > MAX_VALUE_LEN:
        errors.append(f"{where}: value too long ({len(value)} > {MAX_VALUE_LEN})")
    if "\n" in value:
        errors.append(f"{where}: multi-line value not allowed")
    if _DANGEROUS_VALUE_RE.search(value) or _DANGEROUS_TEXT_RE.search(value):
        errors.append(f"{where}: suspicious content in value {value!r}")


def _check_entry(idx: int, entry: Any, errors: list[str]) -> None:
    where = f"recipeList[{idx}]"
    if isinstance(entry, str):
        rid, opts = entry.strip(), None
    elif isinstance(entry, dict) and len(entry) == 1:
        rid, opts = next(iter(entry.items()))
        rid = str(rid).strip()
    else:
        errors.append(f"{where}: must be a recipe id or a single-key mapping {{recipeId: {{options}}}}")
        return
    if rid not in WHITELIST:
        errors.append(f"{where}: recipe type not allowed: {rid}")
        return
    required, optional = WHITELIST[rid]
    if opts is None:
        opts = {}
    if not isinstance(opts, dict):
        errors.append(f"{where} {rid}: options must be a mapping")
        return
    missing = [k for k in required if k not in opts or opts[k] is None or opts[k] == ""]
    if missing:
        errors.append(f"{where} {rid}: missing required option(s): {', '.join(missing)}")
    unknown = [k for k in opts if k not in required and k not in optional]
    if unknown:
        errors.append(f"{where} {rid}: unknown option(s): {', '.join(map(str, unknown))}")
    for k, v in opts.items():
        if v is None:
            continue
        _check_scalar(v, f"{where} {rid}.{k}", errors)
    if rid == "org.openrewrite.maven.ChangePluginGroupIdAndArtifactId" and not (
            opts.get("newGroupId") or opts.get("newArtifactId")):
        errors.append(f"{where} {rid}: needs newGroupId and/or newArtifactId")


def validate_recipe_yaml(text: str) -> tuple[bool, list[str], Optional[dict]]:
    """Validate one declarative recipe document. Returns (ok, errors, parsed_document_or_None)."""
    errors: list[str] = []
    body = strip_fences(text)
    if not body.strip():
        return False, ["empty recipe"], None
    if _DANGEROUS_TEXT_RE.search(body):
        errors.append("recipe text contains '$', backticks or shell/script-looking content")
    try:
        docs = [d for d in yaml.safe_load_all(body) if d is not None]
    except yaml.YAMLError as e:
        return False, errors + [f"invalid YAML: {e}"], None
    if len(docs) != 1:
        return False, errors + [f"expected exactly one YAML document, got {len(docs)}"], None
    doc = docs[0]
    if not isinstance(doc, dict):
        return False, errors + ["recipe document must be a mapping"], None

    extra = set(map(str, doc)) - ALLOWED_TOP_KEYS
    if extra:
        errors.append(f"unknown top-level key(s): {', '.join(sorted(extra))}")
    if doc.get("type") != RECIPE_TYPE:
        errors.append(f"type must be {RECIPE_TYPE}")
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name is required (one PascalCase word)")
    elif not is_kb_recipe_name(name):
        errors.append(f"name must be one PascalCase word (letters and digits, no dots), e.g. RenameFooToBar: {name}")
    for key in ("displayName", "description"):
        val = doc.get(key)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{key} is required (non-empty string)")
    if "tags" in doc and not (isinstance(doc["tags"], list) and all(isinstance(t, str) for t in doc["tags"])):
        errors.append("tags must be a list of strings")
    rl = doc.get("recipeList")
    if not isinstance(rl, list) or not rl:
        errors.append("recipeList must be a non-empty list")
    else:
        if len(rl) > MAX_RECIPE_ENTRIES:
            errors.append(f"recipeList has {len(rl)} entries (max {MAX_RECIPE_ENTRIES})")
        for i, entry in enumerate(rl):
            _check_entry(i, entry, errors)
    return (not errors), errors, doc


def is_kb_recipe_name(name: str) -> bool:
    """One PascalCase word, or a legacy ``com.company.kb.<PascalCase>`` name."""
    return bool(_NAME_RE.match(name or ""))


def short_recipe_name(name: str) -> str:
    """``com.company.kb.Foo`` → ``Foo``; any other name unchanged."""
    return name[len(LEGACY_NAME_PREFIX):] if (name or "").startswith(LEGACY_NAME_PREFIX) else name


def with_recipe_name(text: str, name: str) -> str:
    """The recipe yml with its top-level ``name:`` line set to ``name`` (one-document recipes)."""
    return _NAME_LINE_RE.sub(f"name: {name}", text, count=1)


def recipe_names_in_yaml(text: str) -> list[str]:
    """All `name:` values of recipe documents in a (possibly multi-document) yml. Never raises."""
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return []
    out = []
    for d in docs:
        if isinstance(d, dict) and isinstance(d.get("name"), str) and (
                d.get("type") in (None, RECIPE_TYPE)):
            out.append(d["name"])
    return out
