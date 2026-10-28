"""오류 서명(L1 / L2) — 빌드 오류 한 건을 PC·실행·프로젝트에 상관없이 같은 문자열로 만든다. 케이스 = L2 하나.
흐름상 위치: 로그 파서가 오류를 만들 때마다 ``fill_signatures`` → ``BuildError.signature``(L1) /
``signature_generic``(L2). 수정 루프는 L2 로 오류를 케이스로 묶고 KB 에서 찾는다(case_id = sha1(L2) 앞 10자리,
``sbmig.errors.common.case_id_of``). 뭉뚱그린 컴파일 L2(``build`` / ``other``)는 묶기 전에
``sbmig.triage.cluster.refine_signature`` 가 원문 줄로 세분한다. L1(구체): 같은 문제의 같은 오류 — 프로젝트
소유 패키지·타입은 ``*`` 로 가린다. L2(일반): 프로젝트가 달라도 같은 종류(예
``compile|cannot_find_symbol|BASE64Encoder``). L2 는 서로 다른 문제를 합치면 안 된다(round 10): 패키지 오류는
패키지 전체, @Override / static import 오류는 소스에서 읽은 이름, plugin_execution 은 목표 + 정규화한 원인.
규칙: 절대경로·줄 번호·모듈 이름·프로젝트 소유 위치는 넣지 않는다. 전부 BuildError 의 순수 함수다(같은 오류 →
같은 서명). 여기를 바꾸면 KB 의 기존 케이스와 매칭이 끊긴다(case_id 가 L2 의 해시).

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

Rules: no absolute paths, no line numbers, no module names, no project-owned locations. Everything here is a pure
function of the ``BuildError`` so the same error always yields the same signature.


"""
from __future__ import annotations

import re

from sbmig.core.models import BuildError, ErrorKind

MAX_MESSAGE_CHARS = 160
GENERIC_WORDS = 6
CAUSE_WORDS = 10           # plugin_execution L2: words of the normalized cause after the plugin goal

# 라이브러리 소유 패키지: 이 접두어의 위치·타입은 프로젝트가 달라도 뜻이 같아 서명에 남긴다. 그 밖(프로젝트
# 자신의 패키지)은 L1 에서 '*'. 사내 groupId 는 ``register_external_prefixes`` 로 더해진다.
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

# javac 메시지 첫 줄 → 안정된 분류 이름(L1 / L2 의 둘째 칸). 순서가 중요하다: 위에서 먼저 맞는 것이 이긴다(예
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

# normalize_message 가 지우는 것들(실행·PC·프로젝트마다 다른 부분): URL, Windows 절대 / 상대 경로, 유닉스
# 경로, 16진 주소(객체 해시), '@ line n, column m' 위치, [줄,칸], 범위 '[1.0,2.0)', 버전, 숫자, 'on project
# <모듈>'. 남는 것은 문제의 '종류' 를 말하는 단어뿐이다.
_URL_RE = re.compile(r"\b(?:https?|file|ftp)://\S+")
_WINPATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s:;,'\"()\[\]]+")
# 상대 Windows 경로(src\main\resources\…): Windows Maven 이 리소스 경로를 이렇게 찍는다. 안 가리면 폰트·데이터
# 파일마다 케이스가 따로 생겨, 첫 폰트를 고친 뒤에도 두
# 번째 폰트가 KB 와 매칭되지 않았다(회사 실행 2026-10-27).
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
    """메시지에서 기계·실행마다 다른 것(URL, 경로, 위치, 버전, 숫자, 모듈 이름, JAVA_HOME)을 지운다. 순서가
    중요하다: URL → 경로 → 위치 → 범위 → 버전 → 숫자(버전을 먼저 ``<v>`` 로 바꿔야 '#.#.#' 로 쪼개지지
    않는다). 기본은 소문자, 공백 정리, ``max_chars`` 로 자름.

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
    # 모듈 이름(artifactId)도 프로젝트마다 다르므로 <module>
    # 로 바꾼다(같은 문제가 다른 모듈에서 나도 같은 서명).
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
    """javac(또는 kotlinc) 진단 첫 줄 → 안정된 분류 이름(``_JAVAC_CLASSES`` 의 첫 매치, kotlin 'unresolved
    reference' 는 cannot_find_symbol, 없으면 ``other``).

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
    """패키지 접두어(예: 사내 라이브러리 groupId)를 '라이브러리 코드' 로 등록한다 — L1 에서 '*' 로 가려지지
    않는다. 프로세스 전역이며 ``MavenRunner`` 를 만들 때 등록된다.

    Treat additional package prefixes (e.g. in-house library group ids) as library code in signatures."""
    for p in prefixes:
        p = (p or "").strip()
        if p:
            _EXTRA_EXTERNAL_PREFIXES.add(p if p.endswith(".") else p + ".")


def external_prefixes_from_config(cfg) -> list[str]:
    """``build.external_package_prefixes`` + 사내 부모 pom(base-parent) / 필수 / 선택 아티팩트의 groupId.

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
    """symbol 에서 이름만 뽑는다(L2 의 키): 'class X' → 'X', 'method bar(int)' → 'bar', 'static method
    x(…)' → 'x', FQCN 은 단순 이름으로.

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
    """location 정규화: 라이브러리 위치는 그대로, 프로젝트 소유 위치는 '*' — 같은 라이브러리 문제가 다른
    프로젝트에서도 같은 L1 이 되게.

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


def _first_words(text: str, n: int = GENERIC_WORDS) -> str:
    return " ".join(text.split()[:n])


def _javac_key(cls: str, msg: str, err: BuildError) -> str:
    """symbol 줄이 없는 javac 분류의 L2 키: 분류마다 메시지에서 핵심 이름 하나(클래스.메서드, 예외 타입, 변환
    'A->B')를 뽑는다. @Override / static import 는 파서가 소스에서 읽은 symbol, illegal_start 는 javac 문구 앞
    8단어, 그 밖은 정규화 메시지의 앞 6단어.

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
    if cls in ("method_does_not_override", "static_import_only"):
        # javac names neither the method nor the imported class; the log parser reads them from the source line
        # into `symbol` ("method getErrorPath", "class org.junit.Assert") — without it the kind has no key
        return _symbol_name(err.symbol)
    if cls == "illegal_start":
        return _first_words(normalize_message(msg, module=err.module), 8)    # "illegal start of type", "';' expected"
    return _first_words(normalize_message(msg, module=err.module))


_FQCN_RE = re.compile(r"\b[a-z_][\w]*(?:\.[a-z_][\w]*)*\.[A-Z][\w$]*(?:\.[A-Z][\w$]*)*\b")


def _mask_project_types(text: str) -> str:
    """외부 접두어가 아닌 FQCN(프로젝트 소유 타입)을 '*' 로 바꾼다.

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
    """plugin_execution 의 L2 키 ``<목표>|<원인>``: 감싸는 접두어('Failed to execute goal …', 'Execution x of
    goal …')와 괄호를 떼고, 경로·버전·숫자·프로젝트 타입을 가린 원인의 앞 10단어. 원인에 예외 클래스가 있으면
    그 예외 + 뒤 3단어(어느 파일에서 났든 같은 케이스). 그래서 'webxml attribute is required' 와 'could not
    copy webapp sources' 가 더는 한 케이스가 아니다.

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
        # 예외 클래스가 곧 케이스다('MalformedInputException: Input length = 1' 은 어느 파일·경로에서 나도
        # 같다) + 실패 종류를 가르는 뒤의 몇 단어.
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
    """컴파일 오류의 (L1, L2). 애노테이션 프로세서 붕괴 → 'compile|annotation_processor|lombok' 등;
    파일·줄·symbol 이 없는 빌드 단위 메시지 → 'compile|build|<앞 6단어>'; 그 밖은 javac
    분류별(package_does_not_exist 는 패키지 전체, cannot_find_symbol 은 L1 = symbol + location / L2 =
    이름, 나머지는 ``_javac_key``)."""
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
        # 패키지 전체를 키로: javax.xml.bind 와 javax.xml.ws 는 고치는
        # 법이 다른 문제다(예전 3토막 L2 는 둘을 한 케이스로 합쳤다).
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
        return _join(prefix, cls, sym, _norm_location(err.location)), _join(prefix, cls, name)
    norm = _mask_project_types(normalize_message(msg, module=err.module, lower=False))
    l1 = _join(prefix, cls, norm, _mask_project_types(_norm_symbol(err.symbol)), _norm_location(err.location))
    key = _javac_key(cls, msg, err)
    return l1, _join(prefix, cls, key)


def signature_for(err: BuildError) -> tuple[str, str]:
    """오류 하나의 (L1 구체, L2 일반) 서명. 순수·결정적. 컴파일 → ``_compile_signature``; 해결
    실패(의존성 / parent / 플러그인)는 좌표로(L2 = 'kind|g:a', L1 = 거기에 버전 — 버전이 달라도 같은 케이스);
    누락된 version → 'pom_invalid|missing_version'; plugin_execution
    → 목표 + 원인; 나머지는 정규화 메시지(L1 전체, L2 앞 6단어).

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
    if kind == ErrorKind.POM_INVALID and err.artifact is not None and "version" in norm and "missing" in norm:
        a = err.artifact
        return f"{prefix}|missing_version|{a.group_id}:{a.artifact_id}", f"{prefix}|missing_version"
    if kind == ErrorKind.PLUGIN_EXECUTION:
        return _join(prefix, norm), _join(prefix, _plugin_execution_key(err) or _first_words(norm))
    return _join(prefix, norm), _join(prefix, _first_words(norm))


def fill_signatures(err: BuildError) -> BuildError:
    """``signature_for`` 결과를 오류의 ``signature`` / ``signature_generic`` 에 써 넣고 그 오류를 돌려준다."""
    err.signature, err.signature_generic = signature_for(err)
    return err
