"""Error signatures (ARCHITECTURE §5).

* **L1** (specific): stable across machines and runs of the *same* problem, e.g.
  ``compile|cannot_find_symbol|class WebSecurityConfigurerAdapter|package org.springframework.security.config...``
* **L2** (generic): groups the same *kind* of problem across projects, e.g.
  ``compile|cannot_find_symbol|WebSecurityConfigurerAdapter``

Rules: no absolute paths, no line numbers, no module names, no project-owned locations. Everything here is a pure
function of the ``BuildError`` so the same error always yields the same signature.
"""
from __future__ import annotations

import re

from sbmig.core.models import BuildError, ErrorKind

MAX_MESSAGE_CHARS = 160
GENERIC_WORDS = 6

# Locations / types in these namespaces are library-owned and therefore meaningful across projects. Anything else
# (the project's own packages) is replaced by "*" in L1 so the same library problem matches in another project.
EXTERNAL_PACKAGE_PREFIXES: tuple[str, ...] = (
    "java.", "javax.", "jakarta.", "jdk.", "sun.", "com.sun.", "kotlin.", "scala.", "groovy.",
    "org.springframework.", "org.apache.", "org.hibernate.", "org.junit.", "org.mockito.", "org.slf4j.",
    "org.assertj.", "org.hamcrest.", "org.aspectj.", "org.thymeleaf.", "org.mybatis.", "org.mapstruct.",
    "org.openrewrite.", "org.jboss.", "org.eclipse.", "org.glassfish.", "org.yaml.", "org.json.", "org.bouncycastle.",
    "org.quartz.", "org.flywaydb.", "org.liquibase.", "org.testcontainers.", "org.elasticsearch.", "org.redisson.",
    "com.fasterxml.", "com.google.", "com.querydsl.", "com.zaxxer.", "com.mysql.", "com.netflix.", "com.amazonaws.",
    "com.github.", "com.squareup.", "com.nimbusds.", "com.auth0.", "com.mongodb.", "com.rabbitmq.", "com.oracle.",
    "io.swagger.", "io.springfox.", "springfox.", "io.micrometer.", "io.netty.", "io.projectreactor.", "reactor.",
    "io.jsonwebtoken.", "io.lettuce.", "io.github.resilience4j.", "io.opentelemetry.", "io.restassured.",
    "lombok.", "feign.", "net.sf.", "net.bytebuddy.", "ch.qos.", "freemarker.", "redis.", "okhttp3.", "retrofit2.",
)

_JAVAC_CLASSES: list[tuple[str, re.Pattern[str]]] = [
    ("package_does_not_exist", re.compile(r"\bpackage \S+ does not exist")),
    ("cannot_find_symbol", re.compile(r"\bcannot find symbol")),
    ("static_import_only", re.compile(r"static import only from classes and interfaces")),
    ("not_abstract_and_does_not_override", re.compile(r"is not abstract and does not override abstract method")),
    ("does_not_override_abstract", re.compile(r"does not override abstract method")),
    ("method_does_not_override", re.compile(r"method does not override or implement a method from a supertype")),
    ("cannot_inherit_from_final", re.compile(r"cannot inherit from final")),
    ("cannot_access", re.compile(r"\bcannot access\b")),
    ("class_not_found_in_package", re.compile(r"\bclass file for \S+ not found|\bclass \S+ not found in")),
    ("no_suitable_method", re.compile(r"no suitable (?:method|constructor) found")),
    ("cannot_be_applied", re.compile(r"cannot be applied to")),
    ("unreported_exception", re.compile(r"unreported exception")),
    ("has_private_access", re.compile(r"has private access in")),
    ("deprecated_removed", re.compile(r"has been deprecated and marked for removal|has been removed")),
    ("is_not_public", re.compile(r"is not public in .*cannot be accessed from outside package|is not public in")),
    ("incompatible_types", re.compile(r"incompatible types|cannot be converted to|bad operand types")),
    ("illegal_start", re.compile(
        r"illegal start of|'.+' expected|<identifier> expected|class, interface, enum,? or record expected|"
        r"reached end of file while parsing|not a statement|unclosed (?:string|comment|character)")),
]

_URL_RE = re.compile(r"\b(?:https?|file|ftp)://\S+")
_WINPATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s:;,'\"()\[\]]+")
_PATH_RE = re.compile(r"(?<![\w.])(?:\.{0,2}/)?(?:[\w.\-@$+~]+/){2,}[\w.\-@$+~]+/?")
_ABS_PATH_RE = re.compile(r"(?<![\w])/(?:[\w.\-@$+~]+/)+[\w.\-@$+~]*")
_HEX_RE = re.compile(r"@?\b0x[0-9a-fA-F]+\b|@[0-9a-f]{6,}\b")
_POS_RE = re.compile(r"\s*@\s*line\s+\d+,\s*column\s+\d+", re.IGNORECASE)
_LINECOL_RE = re.compile(r"\[\d+,\d+\]|:\d+:\d+\b|\(\d+,\s*\d+\)")
_RANGE_RE = re.compile(r"[\[\(][^\[\]\(\)\s]*\d[^\[\]\(\)\s]*[\]\)]")
_VERSION_RE = re.compile(r"(?<![\w-])v?\d+(?:\.\d+)+(?:[-.][A-Za-z0-9]+)*\b")
_NUMBER_RE = re.compile(r"(?<![\w])\d+(?![\w])")
_HELP_RE = re.compile(r"\s*->\s*\[Help \d+\]\s*$")
_WS_RE = re.compile(r"\s+")


def normalize_message(msg: str, *, module: str | None = None, lower: bool = True,
                      max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Strip everything machine/run specific from a message: urls, paths, positions, versions, numbers, module name.

    The result is lowercased (by default), whitespace-collapsed and truncated to ``max_chars``.
    """
    s = msg or ""
    s = _HELP_RE.sub("", s)
    s = _URL_RE.sub("<url>", s)
    s = _WINPATH_RE.sub("<path>", s)
    s = _POS_RE.sub("", s)
    s = _LINECOL_RE.sub("", s)
    s = _ABS_PATH_RE.sub("<path>", s)
    s = _PATH_RE.sub(lambda m: "<path>" if "/" in m.group(0) else m.group(0), s)
    s = _HEX_RE.sub("<hex>", s)
    if module:
        s = re.sub(r"(?<![\w.-])" + re.escape(module) + r"(?![\w-])", "<module>", s)
    s = re.sub(r"\bon project [\w.\-]+", "on project <module>", s)
    s = re.sub(r"\(JAVA_HOME=[^)]*\)", "", s)
    s = _RANGE_RE.sub("<range>", s)
    s = _VERSION_RE.sub("<v>", s)
    s = _NUMBER_RE.sub("#", s)
    s = _WS_RE.sub(" ", s).strip().rstrip(":").strip()
    if lower:
        s = s.lower()
    return s[:max_chars].rstrip()


def classify_javac(msg: str) -> str:
    """Map a javac (or kotlinc) diagnostic first line to a stable message class."""
    m = (msg or "").strip()
    for name, rx in _JAVAC_CLASSES:
        if rx.search(m):
            return name
    if re.search(r"unresolved reference", m, re.IGNORECASE):           # kotlin
        return "cannot_find_symbol"
    return "other"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _simple(name: str) -> str:
    name = name.strip().rstrip(";,.:")
    name = re.sub(r"<.*>$", "", name)                           # generics
    return name.rsplit(".", 1)[-1] if "." in name and not name.endswith(")") else name


_EXTRA_EXTERNAL_PREFIXES: set[str] = set()


def register_external_prefixes(prefixes) -> None:
    """Treat additional package prefixes (e.g. in-house library group ids) as library code in signatures."""
    for p in prefixes:
        p = (p or "").strip()
        if p:
            _EXTRA_EXTERNAL_PREFIXES.add(p if p.endswith(".") else p + ".")


def external_prefixes_from_config(cfg) -> list[str]:
    """Explicit cfg.build.external_package_prefixes + group ids of the in-house parent/mandatory/optional artifacts."""
    out: list[str] = list(getattr(cfg.build, "external_package_prefixes", []) or [])
    arts = cfg.artifacts
    for spec in [arts.parent, *arts.mandatory, *arts.optional]:
        if spec.group_id:
            out.append(spec.group_id)
    return out


def _is_external(fqcn: str) -> bool:
    return any(fqcn.startswith(p) or fqcn + "." == p for p in (*EXTERNAL_PACKAGE_PREFIXES, *_EXTRA_EXTERNAL_PREFIXES))


def _norm_symbol(symbol: str | None) -> str:
    """'class Foo' / 'method bar(int,java.lang.String)' / 'variable x' → collapsed, kept verbatim (case kept)."""
    if not symbol:
        return ""
    return _WS_RE.sub(" ", symbol).strip()


def _symbol_name(symbol: str | None) -> str:
    """'class WebSecurityConfigurerAdapter' → 'WebSecurityConfigurerAdapter'; 'method bar(int)' → 'bar'."""
    if not symbol:
        return ""
    s = _norm_symbol(symbol)
    parts = s.split(" ", 1)
    if len(parts) == 2 and parts[0] in {"class", "interface", "enum", "record", "method", "variable", "constructor",
                                         "package", "static", "annotation", "type"}:
        s = parts[1]
        if s.startswith("method ") or s.startswith("variable ") or s.startswith("class "):   # "static method x"
            s = s.split(" ", 1)[1]
    s = s.split("(", 1)[0].strip()
    return _simple(s) if "." in s else s


def _norm_location(location: str | None) -> str:
    """Keep library locations, mask project-owned ones.

    'package org.springframework.security.config' → kept; 'class com.example.sec.SecurityConfig' → 'class *';
    'variable http of type org.springframework...HttpSecurity' → 'variable of type org.springframework...HttpSecurity'.
    """
    if not location:
        return ""
    loc = _WS_RE.sub(" ", location).strip()
    m = re.match(r"^variable \S+ of type (\S+)$", loc)
    if m:
        typ = re.sub(r"<.*>$", "", m.group(1))
        return f"variable of type {typ if _is_external(typ) else '*'}"
    m = re.match(r"^(class|interface|enum|record|package|@interface)\s+(\S+)", loc)
    if m:
        kind, name = m.group(1), re.sub(r"<.*>$", "", m.group(2))
        return f"{kind} {name if _is_external(name) else '*'}"
    return normalize_message(loc, lower=False, max_chars=80)


def _generic_package(pkg: str) -> str:
    parts = pkg.split(".")
    n = 3 if parts and parts[0] in {"org", "com", "io", "net", "de", "fr", "uk", "co", "kr", "jp", "cn", "edu"} else 2
    return ".".join(parts[:n])


def _first_words(text: str, n: int = GENERIC_WORDS) -> str:
    return " ".join(text.split()[:n])


def _javac_key(cls: str, msg: str, err: BuildError) -> str:
    """Main identifier for the L2 signature of non-symbol javac classes."""
    def grab(rx: str, fmt=lambda m: _simple(m.group(1))) -> str:
        m = re.search(rx, msg)
        return fmt(m) if m else ""

    if cls == "cannot_access":
        return grab(r"cannot access (\S+)")
    if cls == "class_not_found_in_package":
        return grab(r"class (?:file for )?(\S+) not found")
    if cls == "not_abstract_and_does_not_override" or cls == "does_not_override_abstract":
        return grab(r"abstract method (\w+)\(.*?\) in (\S+)",
                    lambda m: f"{_simple(m.group(2))}.{m.group(1)}")
    if cls == "no_suitable_method":
        return grab(r"no suitable (?:method|constructor) found for (\w+)", lambda m: m.group(1))
    if cls == "cannot_be_applied":
        return grab(r"(?:method|constructor) (\w+) in (?:class|interface|enum|record) (\S+?)(?:<.*?>)? cannot",
                    lambda m: f"{_simple(m.group(2))}.{m.group(1)}")
    if cls == "unreported_exception":
        return grab(r"unreported exception (\S+)")
    if cls == "has_private_access":
        return grab(r"(\S+) has private access in (\S+)", lambda m: f"{_simple(m.group(2))}.{m.group(1)}")
    if cls == "is_not_public":
        return grab(r"(\S+) is not public in")
    if cls == "cannot_inherit_from_final":
        return grab(r"cannot inherit from final (\S+)")
    if cls == "deprecated_removed":
        return grab(r"(\w+(?:\(.*?\))?) in (\S+) has been", lambda m: f"{_simple(m.group(2))}.{m.group(1).split('(')[0]}")
    if cls == "incompatible_types":
        return grab(r"(\S+) cannot be converted to (\S+)",
                    lambda m: f"{_simple(m.group(1))}->{_simple(m.group(2))}")
    if cls in ("method_does_not_override", "static_import_only", "illegal_start"):
        return ""
    return _first_words(normalize_message(msg, module=err.module))


_FQCN_RE = re.compile(r"\b[a-z_][\w]*(?:\.[a-z_][\w]*)*\.[A-Z][\w$]*(?:\.[A-Z][\w$]*)*\b")


def _mask_project_types(text: str) -> str:
    """Replace fully qualified names outside EXTERNAL_PACKAGE_PREFIXES by '*' (project-owned types)."""
    return _FQCN_RE.sub(lambda m: m.group(0) if _is_external(m.group(0)) else "*", text)


def _join(*parts: str) -> str:
    return "|".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def _compile_signature(err: BuildError) -> tuple[str, str]:
    prefix = err.kind.value
    msg = err.message or ""
    if msg.startswith("annotation-processor:"):
        detail = msg[len("annotation-processor:"):].strip()
        exc = re.search(r"\b((?:java|javax|com|org|lombok)\.[\w.$]*(?:Error|Exception))\b", detail)
        key = "lombok" if "lombok" in (detail + err.raw).lower() else (_simple(exc.group(1)) if exc else "")
        return (_join(prefix, "annotation_processor", normalize_message(detail, module=err.module)),
                _join(prefix, "annotation_processor", key or _first_words(normalize_message(detail))))
    if err.file is None and err.line is None and not err.symbol:
        norm = normalize_message(msg, module=err.module)
        return _join(prefix, "build", norm), _join(prefix, "build", _first_words(norm))

    cls = classify_javac(msg)
    if cls == "package_does_not_exist":
        m = re.search(r"package (\S+) does not exist", msg)
        pkg = m.group(1) if m else normalize_message(msg)
        return _join(prefix, cls, pkg), _join(prefix, cls, _generic_package(pkg))
    if cls == "cannot_find_symbol":
        sym = _norm_symbol(err.symbol)
        if not sym:                                             # kotlin "Unresolved reference: Foo" / 'Foo'
            m = re.search(r"unresolved reference:?\s*'?([\w.]+)'?", msg, re.IGNORECASE)
            sym = m.group(1) if m else ""
        name = _symbol_name(sym) or sym
        return _join(prefix, cls, sym, _norm_location(err.location)), _join(prefix, cls, name)
    norm = _mask_project_types(normalize_message(msg, module=err.module, lower=False))
    l1 = _join(prefix, cls, norm, _norm_symbol(err.symbol), _norm_location(err.location))
    key = _javac_key(cls, msg, err)
    return l1, _join(prefix, cls, key)


def signature_for(err: BuildError) -> tuple[str, str]:
    """Return (L1 specific, L2 generic) signatures for one error. Pure and deterministic."""
    kind = err.kind
    prefix = kind.value
    if kind in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE):
        return _compile_signature(err)
    if kind in (ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PARENT_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION) \
            and err.artifact is not None:
        a = err.artifact
        l2 = f"{prefix}|{a.group_id}:{a.artifact_id}"
        return (f"{l2}:{a.version}" if a.version else l2), l2
    norm = normalize_message(err.message, module=err.module)
    if kind == ErrorKind.POM_INVALID and err.artifact is not None and "version" in norm and "missing" in norm:
        a = err.artifact
        return f"{prefix}|missing_version|{a.group_id}:{a.artifact_id}", f"{prefix}|missing_version"
    return _join(prefix, norm), _join(prefix, _first_words(norm))


def fill_signatures(err: BuildError) -> BuildError:
    err.signature, err.signature_generic = signature_for(err)
    return err
