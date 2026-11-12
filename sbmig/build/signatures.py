"""빌드 오류의 서명(L1 / L2)을 만드는 모듈.
같은 오류는 PC, 실행, 프로젝트가 달라도 같은 문자열이 되고, L2 하나가 케이스 하나다.
로그 파서가 오류마다 ``fill_signatures``를 호출한다.
수정 루프는 L2로 오류를 케이스로 묶고 KB에서 찾는다(case_id는 sha1(L2)의 앞 10자리).
- L1은 같은 문제에서 난 같은 오류를 가리킨다. 프로젝트 소유 패키지와 타입은 ``*``로 가린다
- L2는 프로젝트가 달라도 같은 종류인 오류를 가리킨다. 서로 다른 문제를 한 L2로 합치면 안 된다
- 라이브러리 타입의 멤버 심볼은 ``타입.이름``으로, 클래스 같은 심볼은 이름만 쓴다
- 절대경로, 줄 번호, 모듈 이름, 프로젝트 소유 위치는 서명에 넣지 않는다. 서명은 BuildError만으로 결정된다
- 규칙을 바꾸면 KB의 기존 케이스와 매칭이 끊긴다. 규칙 버전은 ``SIGNATURE_VERSION``이다

Error signatures (ARCHITECTURE §5).

* **L1** (specific): stable across machines and runs of the *same* problem, e.g.
  ``compile|cannot_find_symbol|class WebSecurityConfigurerAdapter|package org.springframework.security.config...``
* **L2** (generic): groups the same *kind* of problem across projects, e.g.
  ``compile|cannot_find_symbol|WebSecurityConfigurerAdapter``. L2 is the case-catalog key (one case per L2, a hit
  applies the case's action), so it must not merge different problems (round 10): package errors keep the whole
  package (``…|javax.xml.bind`` ≠ ``…|javax.xml.ws``), ``method_does_not_override`` / ``static_import_only`` carry the
  method / class the log parser read from the source line, ``illegal_start`` the javac phrase, ``plugin_execution``
  the plugin goal plus the normalized cause (``plugin_execution|war:war|error assembling war: webxml attribute is
  required``).
* A **member** symbol (``method …``, ``variable …``, ``constructor …``, ``static …``) is keyed by the library type that
  owns it: a ``cannot_find_symbol`` whose ``location`` is a library-owned type (``variable x of type T`` or
  ``class | interface | enum | record | @interface T``, ``T`` without type arguments / array brackets) has the L2
  ``compile|cannot_find_symbol|org.springframework.http.HttpHeaders.keySet``; a ``no_suitable_method`` takes the owner
  of the first library-owned ``… is not applicable`` candidate line of ``raw``
  (``compile|no_suitable_method|org.springframework.data.repository.PagingAndSortingRepository.findAll``). Class-like
  symbols, project-owned or package locations and missing locations keep the bare name, so an import line and the
  usages of one missing class stay one case. ``SIGNATURE_VERSION`` names the rules in force.

Rules: no absolute paths, no line numbers, no module names, no project-owned locations. Everything here is a pure
function of the ``BuildError`` so the same error always yields the same signature.


"""
from __future__ import annotations

import re
from collections.abc import Iterable

from sbmig.core.models import BuildError, ErrorKind

# 서명 규칙 버전. L2가 달라지는 규칙 변경마다 1씩 올리고, 바뀐 오류 분류를 ``SIGNATURE_CHANGES``에 넣는다.
# KB 재서명은 그 분류만 다시 계산한다
# 1은 버전을 매기기 전 규칙이고, 2는 멤버 심볼에 라이브러리 타입을 붙이는 규칙이다
# The signature rule version. Raise it with every rule change that alters an L2 `signature_for` produces and list
# the error classes (2nd L2 segment, prefixed by nothing: the class name alone) the change touches in
# SIGNATURE_CHANGES: the KB re-sign recomputes ONLY stored records of those classes signed by an older version and
# merely stamps the rest (recomputing every L2 from a 600-character raw would disturb cases whose rule never changed).
SIGNATURE_VERSION = 2
SIGNATURE_CHANGES: dict[int, frozenset[str]] = {
    2: frozenset({"cannot_find_symbol", "no_suitable_method"}),
}


def classes_changed_since(version: int) -> frozenset[str]:
    """``version`` 규칙으로 서명된 기록 중 지금 규칙에서 L2가 달라질 수 있는 오류 분류를 반환한다.
    0과 1은 같은 규칙으로 보고, 현재 버전 이상이면 빈 집합을 반환한다.

    The error classes (2nd L2 segment) whose L2 may differ between rule `version` and the current rules
    (0 and 1 are the same rules); empty for the current version or a newer one."""
    v = max(int(version or 0), 1)
    out: set[str] = set()
    for changed_in, classes in SIGNATURE_CHANGES.items():
        if changed_in > v:
            out |= classes
    return frozenset(out)


MAX_MESSAGE_CHARS = 160
GENERIC_WORDS = 6
CAUSE_WORDS = 10           # plugin_execution L2: words of the normalized cause after the plugin goal

# 라이브러리 소유 패키지. 이 접두어의 위치와 타입은 서명에 남기고, 프로젝트 패키지는 L1에서 '*'로 가린다.
# 사내 groupId는 ``register_external_prefixes``로 더한다
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

# javac 메시지 첫 줄을 일정한 분류 이름으로 바꾼다. 먼저 일치한 것이 우선하므로 순서가 중요하다(예
# 'is not abstract and does not override' 가 'does not override abstract' 보다 먼저).
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

# normalize_message가 지우는 실행, PC, 프로젝트마다 다른 부분. 문제의 종류를 나타내는 단어만 남긴다
_URL_RE = re.compile(r"\b(?:https?|file|ftp)://\S+")
_WINPATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s:;,'\"()\[\]]+")
# Windows Maven은 리소스 경로를 상대 경로로 출력하므로 가리지 않으면 폰트, 데이터 파일마다 케이스가 따로 생긴다
# a RELATIVE Windows path (`src\main\resources\static\fonts\a.otf`): Maven on Windows prints resource paths this
# way, and an unmasked file name made every font / data file its own case (company run 2026-10-27: the KB never
# matched the second font although the first was fixed)
_RELWINPATH_RE = re.compile(r"(?<![\w.])(?:[\w.\-@$+~]+\\){1,}[\w.\-@$+~]+")
_EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Exception|Error|Throwable))\b")
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
    """메시지에서 PC와 실행마다 달라지는 부분을 지운다.
    버전을 먼저 ``<v>``로 바꿔야 숫자를 처리할 때 쪼개지지 않으므로 처리 순서가 중요하다.

    Strip everything machine/run specific from a message: urls, paths, positions, versions, numbers, module name.

    The result is lowercased (by default), whitespace-collapsed and truncated to ``max_chars``.


    """
    s = msg or ""
    s = _HELP_RE.sub("", s)
    s = _URL_RE.sub("<url>", s)
    s = _WINPATH_RE.sub("<path>", s)
    s = _RELWINPATH_RE.sub("<path>", s)
    s = _POS_RE.sub("", s)
    s = _LINECOL_RE.sub("", s)
    s = _ABS_PATH_RE.sub("<path>", s)
    s = _PATH_RE.sub(lambda m: "<path>" if "/" in m.group(0) else m.group(0), s)
    s = _HEX_RE.sub("<hex>", s)
    # 모듈 이름은 프로젝트마다 다르므로 <module>로 바꾼다.
    # 오류가 난 모듈뿐 아니라 이번 로그의 reactor artifactId를 모두 가린다
    s = _mask_modules(s, module)
    s = re.sub(r"\bon project [\w.\-]+", "on project <module>", s)
    s = re.sub(r"\(JAVA_HOME=[^)]*\)", "", s)
    s = _RANGE_RE.sub("<range>", s)
    s = _VERSION_RE.sub("<v>", s)
    s = _NUMBER_RE.sub("#", s)
    s = _WS_RE.sub(" ", s).strip().rstrip(":").strip()
    if lower:
        s = s.lower()
    return s[:max_chars].rstrip()


# 가리지 않는 모듈 이름.
# Maven / javac 문장에 흔한 단어여서 가리면 서로 다른 문제가 한 케이스로 합쳐질 수 있다.
# 짧은 이름도 같은 이유로 가리지 않는다
# Module names never masked: they are ordinary words of Maven / javac text, so masking them would merge or
# split cases on wording rather than on project identity. Names shorter than MIN_MODULE_NAME_CHARS are skipped too.
MODULE_NAME_STOPLIST: frozenset[str] = frozenset({
    "web", "app", "api", "core", "common", "test", "tests", "main", "build", "parent", "module", "project",
    "resources", "compile", "jar", "war", "pom", "plugin", "source", "target", "class", "error", "file", "java",
    "lib", "util", "utils", "base", "root", "server", "client", "service", "services", "batch", "boot", "back",
    "front",
})
MIN_MODULE_NAME_CHARS = 3

# 이번 파싱의 reactor artifactId(프로세스 전역).
# 다음 프로젝트에서 앞 프로젝트의 이름을 가리지 않도록 파싱마다 교체한다
_REACTOR_MODULES: frozenset[str] = frozenset()
_MASK_CACHE: dict[tuple[str, ...], re.Pattern[str] | None] = {}


def set_reactor_modules(names: Iterable[str]) -> None:
    """이번 빌드 로그의 reactor artifactId 목록을 등록한다.
    누적하지 않고 교체하며, 빈 목록이면 등록을 해제한다.

    Replace (never accumulate) the process-wide set of reactor artifactIds masked as ``<module>``; the log parser
    calls it at the start of every parse, so a fleet's next project never sees the previous project's names."""
    global _REACTOR_MODULES
    _REACTOR_MODULES = frozenset(n.strip() for n in (names or ()) if n and n.strip())


def reactor_modules() -> frozenset[str]:
    """마지막 ``set_reactor_modules`` 호출로 등록된 reactor artifactId를 반환한다.

    The reactor artifactIds registered by the last ``set_reactor_modules`` call."""
    return _REACTOR_MODULES


_ARTIFACT_LIKE_RE = re.compile(r"[-_.\d]|[A-Z]")


def _maskable(name: str) -> bool:
    """등록된 모듈 이름을 메시지에서 가려도 되는지 확인한다.
    3자 이상이고, 제외 목록에 없고, artifactId처럼 생겨야 한다.
    ``user``, ``config`` 같은 소문자 한 단어는 메시지의 일반 단어와 겹치므로 가리지 않는다.
    오류가 난 모듈의 이름은 항상 가린다.

    A registry name is masked only when it looks like an artifactId (a separator, a digit or an uppercase letter) and
    is neither short nor stoplisted; a plain lowercase word (`user`, `config`) would mask ordinary message words. The
    error's OWN module is always masked, as before v2.1z."""
    return (len(name) >= MIN_MODULE_NAME_CHARS and name not in MODULE_NAME_STOPLIST
            and bool(_ARTIFACT_LIKE_RE.search(name)))


def _mask_modules(s: str, module: str | None) -> str:
    names = {n for n in _REACTOR_MODULES if _maskable(n)}
    if module and module.strip():
        names.add(module.strip())                 # the error's own module: always, whatever its name (pre-v2.1z rule)
    if not names:
        return s
    # 'pricing-core-comp'가 'pricing-core'보다 먼저 일치해야 하므로 긴 이름부터 정렬한다
    key = tuple(sorted(names, key=lambda n: (-len(n), n)))
    rx = _MASK_CACHE.get(key)
    if rx is None:
        if len(_MASK_CACHE) > 256:
            _MASK_CACHE.clear()
        rx = re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(n) for n in key) + r")(?![\w-])")
        _MASK_CACHE[key] = rx
    return rx.sub("<module>", s)


def classify_javac(msg: str) -> str:
    """javac / kotlinc 오류 첫 줄을 일정한 분류 이름으로 바꾼다.
    일치하는 분류가 없으면 ``other``를 반환한다.

    Map a javac (or kotlinc) diagnostic first line to a stable message class."""
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
    """사내 groupId 같은 패키지 접두어를 라이브러리 코드로 등록해 L1에서 가리지 않게 한다.
    프로세스 전역 설정이고 ``MavenRunner``를 만들 때 등록된다.

    Treat additional package prefixes (e.g. in-house library group ids) as library code in signatures."""
    for p in prefixes:
        p = (p or "").strip()
        if p:
            _EXTRA_EXTERNAL_PREFIXES.add(p if p.endswith(".") else p + ".")


def external_prefixes_from_config(cfg) -> list[str]:
    """``build.external_package_prefixes``와 사내 부모 / 필수 / 선택 아티팩트의 groupId를 모아 반환한다.

    Explicit cfg.build.external_package_prefixes + group ids of the in-house parent/mandatory/optional artifacts."""
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
    """symbol에서 L2 키로 쓸 이름만 뽑는다. FQCN은 단순 이름으로 바꾼다.

    'class WebSecurityConfigurerAdapter' → 'WebSecurityConfigurerAdapter'; 'method bar(int)' → 'bar'."""
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
    """location을 정규화한다. 라이브러리 위치는 그대로 두고 프로젝트 소유 위치는 '*'로 바꾼다.

    Keep library locations, mask project-owned ones.

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


# 멤버 심볼의 kind 단어. 이것으로 시작하지 않으면 클래스 같은 심볼이므로, import 줄과 사용처가 한 케이스가
# 되도록 위치를 키에 넣지 않는다
# The kind words of a MEMBER symbol; anything else (class / interface / enum / record / annotation / package / type /
# no kind word) is class-like and keeps its rules-1 key whatever the location.
_MEMBER_KINDS = ("method ", "variable ", "constructor ", "static ")
_LOC_VAR_TYPE_RE = re.compile(r"^variable \S+ of type (.+)$")
_LOC_TYPE_RE = re.compile(r"^(?:class|interface|enum|record|@interface)\s+(.+)$")
_QUALIFIED_TYPE_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")
# javac 후보 줄의 형태. Maven이 줄 앞에 [ERROR]를 붙이기도 한다
_CANDIDATE_RE = re.compile(
    r"^\s*(?:\[ERROR\]\s*)?(?:method|constructor)\s+(?:<[^()]*?>\s*)?([\w$]+(?:\.[\w$]+)*)\.(?:<[^()]*?>)?([\w$]+)\("
    r"[^\n]*?\)\s+is not applicable", re.M)


def _bare_type(text: str) -> str:
    """위치의 타입 문자열에서 타입 인자와 배열 괄호를 뗀 한정 이름을 반환한다.
    한정 이름이 아니면 빈 문자열을 반환한다.

    The qualified name of a location type with every type argument (nested ones, ones with spaces / commas) and
    array brackets removed; "" when what is left is not a qualified name (a type variable, ``capture#1 of ?``)."""
    t = text.strip()
    prev = None
    while prev != t:                                            # innermost <…> first, until none is left
        prev, t = t, re.sub(r"<[^<>]*>", "", t)
    t = re.sub(r"\s*\[\s*\]", "", t).replace("...", "").strip()
    return t if _QUALIFIED_TYPE_RE.match(t) else ""


def _library_owner(location: str | None) -> str:
    """location이 가리키는 라이브러리 소유 타입의 한정 이름을 반환한다.
    프로젝트 소유 타입이나 패키지이거나 위치가 없으면 빈 문자열을 반환한다.

    The library-owned type a javac `location:` names (``variable x of type T`` or ``class … T``, `_is_external`),
    else "" (project-owned, a package, a type variable, no location)."""
    if not location:
        return ""
    loc = _WS_RE.sub(" ", location).strip()
    m = _LOC_VAR_TYPE_RE.match(loc) or _LOC_TYPE_RE.match(loc)
    if not m:
        return ""
    typ = _bare_type(m.group(1))
    return typ if typ and _is_external(typ) else ""


def _candidate_owner(raw: str, name: str) -> str:
    """no_suitable_method 오류의 후보 줄에서 처음 나오는 라이브러리 소유 타입을 반환한다.
    후보의 메서드 이름이 'found for' 뒤의 이름과 같을 때만 쓰고, 없으면 빈 문자열을 반환한다.

    The owner type of the first `… is not applicable` candidate line of `raw` whose owner is library-owned and whose
    method / constructor name is the one javac printed after ``found for``; "" when there is none."""
    if not raw or not name:
        return ""
    for m in _CANDIDATE_RE.finditer(raw):
        owner, member = m.group(1), m.group(2)
        if member == name and _is_external(owner):
            return owner
    return ""


def _first_words(text: str, n: int = GENERIC_WORDS) -> str:
    return " ".join(text.split()[:n])


def _javac_key(cls: str, msg: str, err: BuildError) -> str:
    """symbol 줄이 없는 javac 오류의 L2 키를 만든다.
    분류마다 메시지에서 핵심 이름 하나를 뽑고, 없으면 정규화한 메시지의 앞 단어를 쓴다.

    Main identifier for the L2 signature of non-symbol javac classes."""
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
        name = grab(r"no suitable (?:method|constructor) found for (\w+)", lambda m: m.group(1))
        # 다른 타입의 같은 이름 메서드와 섞이지 않도록 후보 줄의 라이브러리 타입을 키에 넣는다
        owner = _candidate_owner(err.raw, name)
        return f"{owner}.{name}" if owner else name
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
    if cls in ("method_does_not_override", "static_import_only"):
        # javac names neither the method nor the imported class; the log parser reads them from the source line
        # into `symbol` ("method getErrorPath", "class org.junit.Assert") — without it the kind has no key
        return _symbol_name(err.symbol)
    if cls == "illegal_start":
        return _first_words(normalize_message(msg, module=err.module), 8)    # "illegal start of type", "';' expected"
    return _first_words(normalize_message(msg, module=err.module))


_FQCN_RE = re.compile(r"\b[a-z_][\w]*(?:\.[a-z_][\w]*)*\.[A-Z][\w$]*(?:\.[A-Z][\w$]*)*\b")


def _mask_project_types(text: str) -> str:
    """외부 접두어로 시작하지 않는 FQCN(프로젝트 소유 타입)을 '*'로 바꾼다.

    Replace fully qualified names outside EXTERNAL_PACKAGE_PREFIXES by '*' (project-owned types)."""
    return _FQCN_RE.sub(lambda m: m.group(0) if _is_external(m.group(0)) else "*", text)


# "Failed to execute goal g:a:v:goal (id) on project x: <cause>" — the parser already cuts this prefix off, a message
# built elsewhere (or a hand-made BuildError) may still carry it
_FAILED_GOAL_PREFIX_RE = re.compile(
    r"^\s*Failed to execute goal (?:(?P<coords>[\w.\-]+:[\w.\-]+:[\w.\-]+:[\w.\-]+)(?: \([^)]*\))? )?"
    r"on project [\w.\-]+\s*:?\s*", re.IGNORECASE)
_EXECUTION_PREFIX_RE = re.compile(
    r"^\s*Execution [\w.\-]+ of goal (?P<coords>[\w.\-]+:[\w.\-]+:[\w.\-]+:[\w.\-]+) failed\s*:?\s*", re.IGNORECASE)
_PARENS_RE = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])")


def _plugin_goal(coords: str) -> str:
    """g:maven-war-plugin:3.4.0:war → war:war (the short form the parser stores in BuildError.goal)."""
    parts = coords.split(":")
    if len(parts) < 4:
        return ""
    a = parts[1]
    m = re.match(r"^maven-(.+)-plugin$", a) or re.match(r"^(.+?)-maven-plugin$", a)
    return f"{m.group(1) if m else a}:{parts[3]}"


def _plugin_execution_key(err: BuildError) -> str:
    """plugin_execution 오류의 L2 키 ``<goal>|<원인>``을 만든다.
    감싸는 접두어를 떼고 경로, 버전, 숫자를 가린 원인의 앞 단어를 쓴다.
    원인에 예외 클래스가 있으면 그 예외 이름과 뒤의 3단어를 쓴다.

    ``<goal>|<cause>`` for a plugin execution failure: the plugin goal (``war:war``) and the first CAUSE_WORDS
    words of the cause, wrapper prefixes and parentheticals removed, paths / versions / numbers / project types
    masked. ``webxml attribute is required`` and ``could not copy webapp sources`` no longer share a case."""
    msg = err.message or ""
    goal = err.goal or ""
    while True:
        m = _FAILED_GOAL_PREFIX_RE.match(msg) or _EXECUTION_PREFIX_RE.match(msg)
        if not m:
            break
        if not goal and m.group("coords"):
            goal = _plugin_goal(m.group("coords"))
        msg = msg[m.end():]
    msg = _PARENS_RE.sub("", _HELP_RE.sub("", msg))
    cause = _mask_project_types(normalize_message(msg, module=err.module, lower=False, max_chars=400))
    exc = _EXCEPTION_RE.search(cause)
    if exc:
        # 어느 파일에서 났든 예외 클래스가 곧 케이스이고, 실패 종류를 구분하는 뒤의 몇 단어만 더한다
        # the exception class is the case ("MalformedInputException: Input length = 1" from any file, any path),
        # plus the few words after it that tell the kind of failure apart
        tail = _first_words(cause[exc.end():].lstrip(" :"), 3).rstrip(" :;,.=-")
        return _join(goal, (exc.group(1) + (" " + tail if tail else "")).lower())
    cause = _first_words(cause.lower(), CAUSE_WORDS).rstrip(" :;,.=-")
    return _join(goal, cause)


def _join(*parts: str) -> str:
    return "|".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def _compile_signature(err: BuildError) -> tuple[str, str]:
    """컴파일 오류의 (L1, L2)를 만든다.
    애노테이션 프로세서 실패, 위치 없는 빌드 단위 메시지, javac 분류별 규칙으로 나눠 처리한다."""
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
        # javax.xml.bind와 javax.xml.ws는 고치는 방법이 다르므로 패키지 전체를 키로 쓴다
        # the whole package: javax.xml.bind and javax.xml.ws (or two in-house packages under kr.co.acme) are different
        # problems with different fixes — the old 3-segment L2 made them one catalog case
        m = re.search(r"package (\S+) does not exist", msg)
        pkg = m.group(1) if m else normalize_message(msg)
        return _join(prefix, cls, pkg), _join(prefix, cls, pkg)
    if cls == "cannot_find_symbol":
        sym = _norm_symbol(err.symbol)
        if not sym:                                             # kotlin "Unresolved reference: Foo" / 'Foo'
            m = re.search(r"unresolved reference:?\s*'?([\w.]+)'?", msg, re.IGNORECASE)
            sym = m.group(1) if m else ""
        name = _symbol_name(sym) or sym
        l1 = _join(prefix, cls, sym, _norm_location(err.location))
        # 라이브러리 타입의 멤버 심볼은 타입까지 키에 넣는다.
        # 클래스 같은 심볼은 import 줄과 사용처가 한 케이스가 되도록 이름만 쓴다
        if err.symbol and name and sym.startswith(_MEMBER_KINDS):
            owner = _library_owner(err.location)
            if owner:
                return l1, _join(prefix, cls, f"{owner}.{name}")
        return l1, _join(prefix, cls, name)
    norm = _mask_project_types(normalize_message(msg, module=err.module, lower=False))
    l1 = _join(prefix, cls, norm, _mask_project_types(_norm_symbol(err.symbol)), _norm_location(err.location))
    key = _javac_key(cls, msg, err)
    return l1, _join(prefix, cls, key)


def signature_for(err: BuildError) -> tuple[str, str]:
    """오류 하나의 (L1, L2) 서명을 만든다. 같은 입력에는 항상 같은 결과를 반환한다.
    해결 실패 오류는 좌표로 만들고 버전은 L1에만 넣으므로, 버전이 달라도 같은 케이스가 된다.

    Return (L1 specific, L2 generic) signatures for one error. Pure and deterministic."""
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
    if kind == ErrorKind.POM_INVALID and err.artifact is not None and "build extension" in err.message.lower():
        # 받지 못한 build extension은 확장마다 케이스를 만들고 버전은 L1에만 넣는다
        a = err.artifact
        l2 = f"{prefix}|build_extension|{a.group_id}:{a.artifact_id}"
        return (f"{l2}:{a.version}" if a.version else l2), l2
    if kind == ErrorKind.POM_INVALID and err.artifact is not None and "version" in norm and "missing" in norm:
        a = err.artifact
        return f"{prefix}|missing_version|{a.group_id}:{a.artifact_id}", f"{prefix}|missing_version"
    if kind == ErrorKind.PLUGIN_EXECUTION:
        return _join(prefix, norm), _join(prefix, _plugin_execution_key(err) or _first_words(norm))
    return _join(prefix, norm), _join(prefix, _first_words(norm))


def fill_signatures(err: BuildError) -> BuildError:
    """``signature_for``의 결과를 오류의 ``signature`` / ``signature_generic``에 채우고 그 오류를 반환한다."""
    err.signature, err.signature_generic = signature_for(err)
    return err
