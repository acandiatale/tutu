r"""외부 설정 목록(round 8): as-is 코드가 읽는 설정 키마다 "패키지된 리소스가 정의하는가" 를 가린다. 왜:
레거시 앱은 외장 Tomcat 이 값을 대 주었다(catalina.properties, ``-D``, JNDI ``<Resource>``, web.xml
``<context-param>``, ``file:`` 속성 파일) — 실행 jar 로 바꾸면 그 공급 경로가 사라져 빌드는 통과해도 실행 시
깨진다. 흐름상 위치: ``analyze_project`` 가 ``externalized.enabled`` 일 때 부르고(MSA 는 배포 서비스별), 결과
``profile.properties`` 로 재구성이 컨테이너 전용 키의 Boot 키를 표식 자리값으로 써 넣고, 보고서 인수인계에
실린다. 진입점: :func:`scan_properties` (키마다 ``defined`` / ``defaulted`` / ``external``),
:func:`load_rules` (규칙은 ``knowledge/config/externalized.yml`` — 회사 규칙은 코드가 아니라 설정에, 불변조건
2), :func:`filtered_binary_resources` (필터링되는 리소스 아래 바이너리 — v2 에서는 구조 검토가 사실 줄로
쓴다). 정규식 기반이고, 깨지거나 너무 큰 파일은 메모(notes)가 될 뿐 예외는 없다.

Externalized-configuration inventory (round 8): which settings does the as-is code read, and does a packaged
resource define them?

Legacy apps ran on an external Tomcat whose values never lived in the source (catalina.properties, `-D`, JNDI
`<Resource>/<Environment>`, web.xml `<context-param>`, `file:` property files). An executable boot-jar loses those
supply routes. `scan_properties(...)` lists every reference, classifies each key as `defined` (a packaged resource
defines it), `defaulted` (every reference carries a code default) or `external` (nowhere), and maps container-only
keys to the Boot keys sbmig writes as sentinel placeholders (restructure does the writing).

Rules (JNDI / context-param / system-property mapping, Boot defaults, Korean guide lines) live in
`knowledge/config/externalized.yml` (invariant 2) and are loaded by `load_rules(path)`.

Regex based, no parser dependency; a broken or oversized file becomes an inventory note, never an exception.
Reference sources (`PropertyRef.source`):
  value-annotation         `${…}` in @Value and any other annotation argument (@Scheduled(cron="${x}"), …)
  configuration-properties @ConfigurationProperties("p") → key `p.*` (a prefix)
  environment              env.getProperty / getRequiredProperty / containsProperty on an Environment-typed receiver
  system-property          System.getProperty("k"[, "d"])                 (rules.system_property.skip skipped)
  system-env               System.getenv("K"), `${env.K}` in XML        (informational: no placeholder)
  xml-placeholder          `${…}` in Spring XML / web.xml / context.xml
  context-param            web.xml <context-param> (default = the as-is value)
  jndi                     java:comp/env/… literals, InitialContext lookups, jee:jndi-lookup, jndiName, context.xml
                           <Resource>/<Environment>, web.xml <resource-ref>/<env-entry> (normalized to java:comp/env/)
  property-source          `${…}` inside a @PropertySource / placeholder-configurer location
  config-placeholder       `${…}` inside the value of a packaged definition file (`spring.datasource.url=${DB_URL}` →
                           DB_URL is read at runtime; `${DB_URL:x}` in a value counts as a default → `defaulted`)
  profiles                 the string "spring.profiles.active" in Java
@ConditionalOnProperty / @ConditionalOnBooleanProperty(prefix=, name=|value=) are `value-annotation` reads of
`prefix.name`; an unset key is a valid state (the condition decides), so they carry a pseudo default (`defaulted`
unless defined), `boot_keys=[]` and a note — a written placeholder would flip the condition.

Per-service inventory (MSA, round 9): `scan_properties(..., services=[deployables])`. A service directory is
dirname(`ServiceInfo.root_pom`) ("" for a root service); every ref and definition file belongs to the deepest service
directory that contains it. Refs inside a service are matched against that service's definitions only (main + test as
in the project-wide mode) and get `PropertyFinding.service = <name>`; refs outside every service (shared code) are
matched against every definition in the project (defined when any service defines it) and get `service=None`. A key
read by two services yields one finding per service, so `findings` is unique on (`service`, `key`) and sorted by
(key, service). `PropertyInventory.services` lists the services scanned; `services=None` / `[]` = the project-wide
round-8 behaviour (`service` stays None everywhere).

Notes: `bootstrap*.properties|yml` definitions count as defined, plus an inventory note when the poms lack
spring-cloud-starter-bootstrap. A UTF-8 BOM is stripped before parsing. `src/main/resources/config/*` counts as packaged.
Relaxed binding: lookup compares `norm_key` (my-key == myKey == MY_KEY); each spelling read stays its own finding.

Not detected (by design):
  - `\${x}` (Spring 6.2+ escape) is a literal; `$${x}` is recorded as `x` (Spring resolves the inner placeholder)
  - SpEL `#{systemProperties['x']}` / `#{environment['x']}` (only `${…}` inside SpEL is read)
  - non-literal keys: `System.getProperty(PREFIX + "x")`, `env.getProperty(KEY)` with a constant
  - `@Value` in Kotlin / Groovy / Scala sources (only *.java is scanned; the language edge finding covers them)
  - `@Value("${my.list}")` against a YAML list (only `my.list[0]` … are defined, as in Spring)
  - definitions outside `src/{main,test}/resources` (root, config/, META-INF/ and named classpath: locations)


"""
from __future__ import annotations

import bisect
import fnmatch
import logging
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from sbmig.core.models import PomInfo, PropertyFinding, PropertyInventory, PropertyRef, ServiceInfo
from sbmig.core.workspace import is_build_output_path

from .source_scanner import _COMMENT_RE, ScanResult

log = logging.getLogger("sbmig.analyze")

MAX_FILE_BYTES = 1_000_000
MAX_REFS_PER_KEY = 50            # refs kept per finding (the Handover shows cfg.externalized.max_refs_per_key)
SKIP_PARTS = ("node_modules", ".git")   # + Maven build output (round 10: a `target` package is source)
JNDI_PREFIX = "java:comp/env/"
_DEF_EXT = (".properties", ".yml", ".yaml")
_MAVEN_BUILTIN = re.compile(r"^(project|pom|maven|settings|env)\.|^basedir$")
_BUILTIN_SKIP = re.compile(r"^(random\.|project\.|pom\.)|^basedir$")
_KEY_OK = re.compile(r"^[A-Za-z0-9_.\-\[\]]+$")


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
@dataclass
class JndiRule:
    """JNDI 이름 규칙: ``match`` (fnmatch) 에 맞는 이름을 어떤
    Boot 키로 바꿀지(``{name}`` / ``{leaf}`` 치환)와 안내."""
    match: str
    boot_keys: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ContextParamRule:
    """알려진 web.xml context-param 하나의 규칙: 건너뛸지, 대응 Boot 키(None = 기본 규칙), 안내."""
    skip: bool = False
    boot_keys: Optional[list[str]] = None
    note: str = ""


@dataclass
class ExternalizedRules:
    """``externalized.yml`` 을 읽은 규칙 묶음: JNDI, context-param(기본 Boot 키 + 알려진 것), 건너뛸
    시스템 속성, Boot 기본값이라 자리값이 필요 없는 키, 한국어 안내 줄, 로더 메모(파일 없음 / 깨짐)."""
    jndi: list[JndiRule] = field(default_factory=list)
    context_param_default: str = "server.servlet.context-parameters.{name}"
    context_param_known: dict[str, ContextParamRule] = field(default_factory=dict)
    system_property_skip: list[str] = field(default_factory=list)
    boot_defaults: list[str] = field(default_factory=list)
    guide: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # loader remarks (missing / unparsable rules file)


def _str_list(v: Any) -> list[str]:
    """YAML 값을 문자열 목록으로(None → [], 단일 값 → [값])."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x is not None]
    return [str(v)]


def load_rules(path: Path) -> ExternalizedRules:
    """``knowledge/config/externalized.yml`` 을 읽는다. 파일이 없거나 깨지면
    빈 규칙 + 메모(예외 없음 — 규칙 파일 하나 때문에 분석이 멈추면 안 된다).

    Parse `knowledge/config/externalized.yml`. A missing or broken file → empty rules + a note (never raises)."""
    path = Path(path)
    if not path.is_file():
        return ExternalizedRules(notes=[f"externalized-config rules file not found: {path} (no mapping rules applied)"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("top level is not a mapping")
    except Exception as e:  # noqa: BLE001 - a broken rules file must not stop the analysis
        return ExternalizedRules(notes=[f"externalized-config rules file unreadable: {path}: {e}"])
    rules = ExternalizedRules()
    for item in data.get("jndi") or []:
        if isinstance(item, dict) and item.get("match"):
            rules.jndi.append(JndiRule(match=str(item["match"]), boot_keys=_str_list(item.get("boot_keys")),
                                       note=str(item.get("note") or "")))
    cp = data.get("context_param") or {}
    if isinstance(cp, dict):
        if cp.get("default_boot_key"):
            rules.context_param_default = str(cp["default_boot_key"])
        for name, spec in (cp.get("known") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            rules.context_param_known[str(name)] = ContextParamRule(
                skip=bool(spec.get("skip", False)),
                boot_keys=_str_list(spec["boot_keys"]) if "boot_keys" in spec else None,
                note=str(spec.get("note") or ""))
    sp = data.get("system_property") or {}
    rules.system_property_skip = _str_list(sp.get("skip") if isinstance(sp, dict) else None)
    rules.boot_defaults = _str_list(data.get("boot_defaults"))
    rules.guide = _str_list(data.get("guide"))
    return rules


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def norm_key(key: str) -> str:
    """Spring 완화 바인딩 비교용 키: 소문자, ``-`` ``_`` ``.`` 제거(db.url == DB_URL == db-url).

    Relaxed-binding comparison form: lowercase, `-` `_` `.` removed (db.url == DB_URL == db-url)."""
    return re.sub(r"[-_.]", "", key.lower())


def _segments(key: str) -> list[str]:
    """키를 점 단위 조각으로(조각마다 소문자, ``-`` ``_``
    제거) — ``@ConfigurationProperties`` 접두어 비교용."""
    return [re.sub(r"[-_]", "", s.lower()) for s in key.split(".") if s]


def _strip_java_comments(text: str) -> str:
    """source_scanner 의 주석 제거와 같되, 주석 속 줄바꿈은 남긴다 — 위치(offset)를 줄 번호로 바꿀 수 있게.

    `strip_comments` of source_scanner, but newlines inside comments are kept so offsets map to lines."""
    return _COMMENT_RE.sub(lambda m: m.group(1) or "\n" * m.group(0).count("\n"), text)


_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _strip_xml_comments(text: str) -> str:
    """XML 주석을 지우되 줄바꿈은 남긴다(줄 번호 유지)."""
    return _XML_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


class _Lines:
    """텍스트 위치 → 1부터 세는 줄 번호(이진 탐색)."""
    def __init__(self, text: str):
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", text)]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


# 보고서·KB 에 남는 원문 조각에서 비밀번호류 값을 가린다(키 이름이 password / secret … 이면 값을 ***)
_SECRET_RE = re.compile(r"(?i)((?:password|passwd|pwd|secret|credential)\w*\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)")


def _raw(text: str) -> str:
    """참조 원문(공백 정리, 비밀 값 가림, 200자까지) — ``PropertyRef.raw``."""
    s = re.sub(r"\s+", " ", text).strip()
    s = _SECRET_RE.sub(lambda m: m.group(1) + ('"***"' if m.group(2).startswith(('"', "'")) else "***"), s)
    return s[:200]


def _skipped(rel: str) -> bool:
    """node_modules / .git / Maven 빌드 출력 아래면
    건너뛴다(``target`` 이라는 자바 패키지는 소스로 남긴다)."""
    return any(p in SKIP_PARTS for p in rel.replace("\\", "/").split("/")) or is_build_output_path(rel)


def _is_test(rel: str) -> bool:
    """테스트 소스 / 리소스 경로인지."""
    return "src/test/" in rel or rel.startswith("test/")


def _unescape_java(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def iter_placeholders(s: str, base: int = 0) -> Iterable[tuple[str, Optional[str], int]]:
    """``s`` 안의 모든 ``${key[:default]}`` 에 대해 (키, 기본값 또는 None, 위치). 중첩된 기본값 안의
    것도 낸다. 앞에 역슬래시가 붙은 ``${x}`` (Spring 6.2+
    이스케이프)는 자리값이 아니다. 괄호가 닫히지 않으면 거기서 멈춘다.

    (key, default|None, offset) for every `${key[:default]}` in `s`, nested defaults included."""
    i = 0
    n = len(s)
    while True:
        i = s.find("${", i)
        if i < 0:
            return
        if i > 0 and s[i - 1] == "\\":            # `\${x}`: Spring 6.2+ escape → a literal, not a placeholder
            i += 2
            continue
        depth, j = 1, i + 2
        while j < n and depth:
            if s.startswith("${", j):
                depth += 1
                j += 2
                continue
            if s[j] == "}":
                depth -= 1
            elif s[j] == "{":
                depth += 1
            j += 1
        if depth:
            return
        body = s[i + 2:j - 1]
        d, colon = 0, -1
        k = 0
        while k < len(body):
            if body.startswith("${", k):
                d += 1
                k += 2
                continue
            if body[k] == "}":
                d -= 1
            elif body[k] == ":" and d == 0:
                colon = k
                break
            k += 1
        key = (body if colon < 0 else body[:colon]).strip()
        default = None if colon < 0 else body[colon + 1:]
        yield key, default, base + i
        if default and "${" in default:
            yield from iter_placeholders(default, base + i + 2 + colon + 1)
        i = j


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------
class _Collector:
    """스캔 중에 모으는 것: 참조(PropertyRef, 중복 제거), 메모, ``file:`` 공급 경로, classpath 위치, 프로필,
    테스트 인라인 정의, 컨테이너 시스템 속성, 자기 참조 정의."""

    def __init__(self, rules: ExternalizedRules, maven_props: set[str], filtered: list[str]):
        self.rules = rules
        self.maven_props = maven_props
        self.filtered = filtered
        self.refs: list[PropertyRef] = []
        self._seen: set[tuple] = set()
        self.notes: list[str] = list(rules.notes)
        self.file_locations: list[str] = []          # file: / ${…} property locations (external supply routes)
        self.classpath_locations: list[str] = []     # classpath: files named by @PropertySource / placeholders
        # v2.1x D6: `/WEB-INF/x.properties` locations (servlet-context files) — the part after WEB-INF/; the restructure
        # moves such files to the boot stash and points the copies there, so their keys are definitions
        self.webinf_locations: list[str] = []
        self.profiles: set[str] = set()
        self.inline_defs: list[tuple[str, str]] = []   # @SpringBootTest(properties=…) / DynamicPropertyRegistry
        self.container_props: dict[str, set[str]] = defaultdict(set)
        # (norm key, file) of `log.dir=${LOG_DIR}`-style self references: the hosting entry never defines the
        # placeholder it reads (relaxed matching would otherwise call LOG_DIR "defined" by log.dir itself)
        self.self_defs: set[tuple[str, str]] = set()
        self.sys_skip = set(rules.system_property_skip)

    def in_filtered(self, rel: str) -> bool:
        """Maven 리소스 필터링(``<filtering>true``)이 걸린 디렉토리 아래 파일인지."""
        return any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in self.filtered)

    def add(self, key: str, source: str, file: str, line: Optional[int], raw: str,
            default: Optional[str] = None) -> None:
        """참조 하나를 기록한다. 이상한 키·건너뛸 시스템 속성(컨테이너 속성으로 따로
        셈)·내장 키(project.* 등)는 버리고, (키, 출처, 파일, 줄)이 같은 것은 한 번만."""
        key = key.strip()
        if not key:
            return
        if source not in ("jndi", "context-param") and not _KEY_OK.match(key.rstrip("*")):
            return
        if source in ("xml-placeholder", "property-source", "config-placeholder", "value-annotation", "environment",
                      "system-property"):
            if key in self.sys_skip:
                self.container_props[key].add(file)
                return
            if _BUILTIN_SKIP.match(key):
                return
        sig = (key, source, file, line)
        if sig in self._seen:
            return
        self._seen.add(sig)
        self.refs.append(PropertyRef(key=key, source=source, file=file, line=line, default=default,  # type: ignore[arg-type]
                                     scope="test" if _is_test(file) else "main", raw=_raw(raw)))

    def placeholder(self, key: str, default: Optional[str], source: str, file: str, line: int, raw: str,
                    filtered: bool = False) -> None:
        """``${key}`` 하나를 기록한다. 필터링되는 파일에서 pom 속성이면 빌드 때 Maven 이 풀어 주므로 무시,
        ``env.X`` 는 환경 변수 X 로 기록."""
        if filtered and (key in self.maven_props or _MAVEN_BUILTIN.match(key)):
            return                                  # resolved by Maven resource filtering at build time
        if key.startswith("env."):
            self.add(key[4:], "system-env", file, line, raw)
            return
        self.add(key, source, file, line, raw, default)


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------
_JAVA_HINTS = ("${", "getProperty", "containsProperty", "getenv", "java:", "ConfigurationProperties",
               "PropertySource", "lookup", "spring.profiles", "Profile", "getDataSource", "ConditionalOn")
_CONDITIONAL = ("ConditionalOnProperty", "ConditionalOnBooleanProperty")
COND_RAW = "@ConditionalOn"          # PropertyRef.raw prefix of a @ConditionalOnProperty read (see _classify)
_STR = r'"((?:\\.|[^"\\\n])*)"'
_STR_RE = re.compile(_STR)
_ANNOT_START_RE = re.compile(r"@([\w.]+)\s*\(")
_CONFPROPS_RE = re.compile(r"@(?:[\w.]+\.)?ConfigurationProperties\s*\(\s*(?:(?:prefix|value)\s*=\s*)?" + _STR)
_ENV_CALL_RE = re.compile(r"(?:\b(\w+)|getEnvironment\s*\(\s*\))\s*\.\s*(getProperty|getRequiredProperty|containsProperty)"
                          r"\s*\(\s*" + _STR + r"\s*((?:,(?:[^()]|\([^()]*\))*)?)\)")
_ENV_DECL_RE = re.compile(r"\b(?:Environment|ConfigurableEnvironment|PropertyResolver|ConfigurablePropertyResolver|"
                          r"StandardEnvironment)\s+(\w+)\s*[;=,)]")
_GETENV_RE = re.compile(r"\bSystem\s*\.\s*getenv\s*\(\s*" + _STR + r"\s*\)")
_JNDI_LIT_RE = re.compile(r'"(java:comp/env/[\w./-]+)"')
_LOOKUP_RE = re.compile(r"\.\s*(?:lookup|getDataSource)\s*\(\s*" + _STR + r"\s*\)")
_PROFILE_ANNOT_RE = re.compile(r"@(?:Profile|ActiveProfiles)\s*\(([^)]*)\)")


def _balanced(src: str, open_idx: int) -> int:
    """``open_idx`` 의 ``(`` 와 짝인 ``)`` 바로 뒤 위치(문자열 리터럴 안의 괄호는 무시). 짝이 없으면 len(src).

    Index just after the `)` matching the `(` at open_idx (string literals respected); len(src) if unbalanced."""
    depth, i, n = 0, open_idx, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            m = _STR_RE.match(src, i)
            i = m.end() if m else i + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _split_args(s: str) -> list[str]:
    """어노테이션 / 메서드 인자 문자열을 최상위 쉼표로 나눈다(괄호·중괄호·문자열 안의 쉼표는 무시)."""
    out, depth, cur, i = [], 0, "", 0
    while i < len(s):
        c = s[i]
        if c == '"':
            m = _STR_RE.match(s, i)
            if m:
                cur += m.group(0)
                i = m.end()
                continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip():
        out.append(cur.strip())
    return out


def _lit_or_expr(arg: str) -> str:
    """인자가 문자열 리터럴이면 그 값, 아니면 식 그대로(기본값 기록용)."""
    m = re.fullmatch(_STR, arg.strip())
    return _unescape_java(m.group(1)) if m else arg.strip()


def _location(col: _Collector, loc: str, file: str, line: int, raw: str, source: str) -> None:
    """속성 파일 위치 하나: 안의 ``${…}`` 는 참조로, ``classpath:`` 는 정의 파일
    후보로, ``file:`` / 자리값 경로는 외부 공급 경로(패키지되지 않음)로 기록."""
    loc = loc.strip()
    if not loc:
        return
    for key, default, _ in iter_placeholders(loc):
        col.placeholder(key, default, source, file, line, raw)
    if loc.startswith(("classpath:", "classpath*:")):
        # v2.1x D7: `${…}` (a profile / environment segment) → `*` glob: the packaged files it may name are definitions
        col.classpath_locations.append(profile_glob(loc.split(":", 1)[1].lstrip("/")))
    elif _webinf_sub(loc) is not None:
        col.webinf_locations.append(_webinf_sub(loc) or "")
    elif loc.startswith("file:") or "${" in loc:
        col.file_locations.append(f"{file}:{line}: {loc}")


def profile_glob(path: str) -> str:
    """위치 안의 ``${…}`` 를 ``*`` 로(v2.1x D7): ``config/${env}/app.properties`` → ``config/*/app.properties``.

    `${…}` segments of a location → `*` (v2.1x D7)."""
    return re.sub(r"\$\{[^}]*\}", "*", path)


def _webinf_sub(loc: str) -> Optional[str]:
    """``/WEB-INF/x`` · ``WEB-INF/x`` 위치 → ``x`` (``${…}`` 가 있으면 glob), 아니면 None."""
    m = re.match(r"^/?WEB-INF/(.+)$", loc.strip())
    return profile_glob(m.group(1)) if m else None


def scan_java(col: _Collector, rel: str, text: str) -> None:
    """자바 파일 하나에서 설정 참조를 모은다: 어노테이션 인자의 ``${…}`` (@Value 등), @PropertySource 위치,
    @ConfigurationProperties 접두어, Environment.getProperty, System.getProperty / getenv, JNDI 이름·lookup,
    프로필. 테스트의 인라인 속성(@SpringBootTest(properties=…), DynamicPropertyRegistry)은 정의로 센다."""
    # 싼 사전 검사: 힌트 문자열이 하나도 없으면 정규식을 돌리지 않는다(파일 수천 개 프로젝트)
    if not any(h in text for h in _JAVA_HINTS):
        return
    src = _strip_java_comments(text)
    lines = _Lines(src)
    # annotations: ${…} in any argument; @PropertySource locations; @ConfigurationProperties prefix
    for m in _ANNOT_START_RE.finditer(src):
        name = m.group(1).rsplit(".", 1)[-1]
        end = _balanced(src, m.end() - 1)
        body = src[m.end():end - 1]
        ln = lines.line(m.start())
        if name in ("SpringBootTest", "TestPropertySource"):
            # inline `properties = {"k=v"}` are definitions (tests load them); locations are test-only files
            for s in _STR_RE.finditer(body):
                lit = _unescape_java(s.group(1))
                km = re.match(r"\s*([\w.\-\[\]]+)\s*[=:]", lit)
                if km and not lit.startswith(("classpath", "file:")):
                    col.inline_defs.append((km.group(1), rel))
            continue
        if name in _CONDITIONAL:
            _conditional(col, rel, ln, body, src[m.start():end])
            continue
        if name in ("PropertySource", "PropertySources"):
            for s in _STR_RE.finditer(body):
                _location(col, _unescape_java(s.group(1)), rel, lines.line(m.end() + s.start()), src[m.start():end],
                          "property-source")
            continue
        if "${" not in body:
            continue
        for s in _STR_RE.finditer(body):
            lit = _unescape_java(s.group(1))
            for key, default, _ in iter_placeholders(lit):
                col.placeholder(key, default, "value-annotation", rel, lines.line(m.end() + s.start()),
                                src[m.start():end])
    for m in _CONFPROPS_RE.finditer(src):
        prefix = _unescape_java(m.group(1)).strip()
        if prefix:
            col.add(prefix + ".*", "configuration-properties", rel, lines.line(m.start()), m.group(0))
    env_vars = set(_ENV_DECL_RE.findall(src))
    for m in _ENV_CALL_RE.finditer(src):
        recv, key, rest = m.group(1), _unescape_java(m.group(3)), m.group(4)
        args = [a for a in _split_args(rest.lstrip(",")) if a]
        args = [a for a in args if not a.endswith(".class")]
        default = _lit_or_expr(args[0]) if args and m.group(2) == "getProperty" else None
        ln = lines.line(m.start())
        if recv == "System":
            col.add(key, "system-property", rel, ln, m.group(0), default)
        # 받는 쪽이 Environment 타입으로 선언된 변수이거나 이름에 env 가 들어갈 때만 — 아무 객체의 getProperty
        # 를 설정 참조로 세지 않으려는 것(Properties.getProperty 등)
        elif recv is None or recv in env_vars or "env" in recv.lower():
            col.add(key, "environment", rel, ln, m.group(0), default)
        if key == "spring.profiles.active":
            col.add(key, "profiles", rel, ln, m.group(0))
    for m in _GETENV_RE.finditer(src):
        col.add(_unescape_java(m.group(1)), "system-env", rel, lines.line(m.start()), m.group(0))
    for m in _JNDI_LIT_RE.finditer(src):
        col.add(m.group(1), "jndi", rel, lines.line(m.start()), _line_text(src, lines, m.start()))
    if "InitialContext" in src or "javax.naming" in src or "JndiDataSourceLookup" in src or "JndiTemplate" in src:
        for m in _LOOKUP_RE.finditer(src):
            name = _unescape_java(m.group(1)).strip()
            if not name or name.rstrip("/") in ("java:comp/env", "java:comp") or "${" in name:
                continue
            col.add(name if name.startswith("java:") else JNDI_PREFIX + name.lstrip("/"), "jndi", rel,
                    lines.line(m.start()), m.group(0))
    for m in re.finditer(r'"spring\.profiles\.active"(?:\s*,\s*' + _STR + ')?', src):
        col.add("spring.profiles.active", "profiles", rel, lines.line(m.start()), _line_text(src, lines, m.start()))
        if m.group(1):
            col.profiles.update(_profile_names(m.group(1)))
    if "DynamicPropertyRegistry" in src:
        for m in re.finditer(r"\.\s*add\s*\(\s*" + _STR, src):
            col.inline_defs.append((_unescape_java(m.group(1)), rel))
    for m in _PROFILE_ANNOT_RE.finditer(src):
        for s in _STR_RE.finditer(m.group(1)):
            for p in re.split(r"[\s,&|()!]+", s.group(1)):
                if p:
                    col.profiles.add(p)


def _strings(arg: str) -> list[str]:
    """인자 안의 모든 자바 문자열 리터럴 값."""
    return [_unescape_java(x.group(1)) for x in _STR_RE.finditer(arg)]


def _conditional(col: _Collector, rel: str, line: int, body: str, raw: str) -> None:
    """@ConditionalOnProperty 의 ``prefix.name`` 마다 읽기 참조 하나. 키가 비어
    있는 것도 정상 상태(조건이 정한다)라서 가짜 기본값을 달아 ``defaulted`` 로 두고, 자리값은 절대 쓰지 않는다
    — 값을 써 넣으면 조건이 뒤집힌다.

    @ConditionalOnProperty(prefix=, name=|value=, havingValue=, matchIfMissing=): every `prefix.name` is a read.
    An unset key is a valid state (the condition decides), so the ref carries a pseudo default → `defaulted` unless
    defined; never a sentinel placeholder (a written value would flip the condition)."""
    prefix, names, if_missing = "", [], False
    for arg in _split_args(body):
        attr, eq, val = arg.partition("=")
        if eq and re.fullmatch(r"\s*\w+\s*", attr):
            attr = attr.strip()
        else:
            attr, val = "value", arg
        if attr == "prefix":
            prefix = next(iter(_strings(val)), "").strip()
        elif attr in ("name", "value"):
            names += [n.strip() for n in _strings(val) if n.strip()]
        elif attr == "matchIfMissing":
            if_missing = val.strip() == "true"
    default = "(unset: condition matches, matchIfMissing)" if if_missing else "(unset: condition does not match)"
    for n in names:
        key = f"{prefix.rstrip('.')}.{n}" if prefix else n
        col.add(key, "value-annotation", rel, line, raw, default)


def _line_text(src: str, lines: _Lines, off: int) -> str:
    """위치 ``off`` 가 있는 줄 전체."""
    ln = lines.line(off)
    start = lines.starts[ln - 1]
    end = src.find("\n", start)
    return src[start:end if end >= 0 else len(src)]


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------
_CTX_PARAM_RE = re.compile(r"<context-param\b[^>]*>(.*?)</context-param>", re.S)
_PARAM_NAME_RE = re.compile(r"<param-name>\s*(.*?)\s*</param-name>", re.S)
_PARAM_VALUE_RE = re.compile(r"<param-value>\s*(.*?)\s*</param-value>", re.S)
_JEE_LOOKUP_RE = re.compile(r"<jee:jndi-lookup\b[^>]*?\bjndi-name\s*=\s*\"([^\"]+)\"", re.S)
_JNDI_PROP_RE = re.compile(r"<property\b[^>]*?\bname\s*=\s*\"jndiName\"[^>]*?\bvalue\s*=\s*\"([^\"]+)\"", re.S)
_JNDI_PROP_TEXT_RE = re.compile(r"<property\b[^>]*?\bname\s*=\s*\"jndiName\"[^>]*>\s*<value>\s*([^<]+?)\s*</value>", re.S)
_TC_TAG_RE = re.compile(r"<(Resource|Environment|ResourceLink)\b([^>]*)>", re.S)
_ATTR_RE = re.compile(r"([\w:.-]+)\s*=\s*\"([^\"]*)\"")
_RES_REF_RE = re.compile(r"<(?:resource-ref|resource-env-ref)\b[^>]*>.*?<(?:res-ref-name|resource-env-ref-name)>\s*"
                         r"([^<]+?)\s*</", re.S)
_ENV_ENTRY_RE = re.compile(r"<env-entry\b[^>]*>(.*?)</env-entry>", re.S)
_JNDI_XML_LIT_RE = re.compile(r"(java:comp/env/[\w./-]+)")
_PLACEHOLDER_CFG = ("property-placeholder", "PlaceholderConfigurer", "PropertiesFactoryBean", "util:properties",
                    "property-override")
_LOCATION_RE = re.compile(r"((?:classpath\*?|file):[^\s\"'<>,;]+\.(?:properties|ya?ml))(?![\w.])")
_WEBINF_LOC_RE = re.compile(r"(?<![\w:/.\-])(/?WEB-INF/[^\s\"'<>,;]+\.(?:properties|ya?ml))(?![\w.])")


def _jndi_name(name: str) -> str:
    """JNDI 이름 정규화: ``java:`` 로 시작하지 않으면 ``java:comp/env/`` 를 붙인다."""
    name = name.strip()
    return name if name.startswith("java:") else JNDI_PREFIX + name.lstrip("/")


def scan_xml(col: _Collector, rel: str, text: str, *, web_xml: bool = False, context_xml: bool = False) -> None:
    """XML 하나에서 참조를 모은다: 모든 ``${…}``, web.xml 이면 context-param /
    resource-ref / env-entry, context.xml 이면 ``<Resource>`` / ``<Environment>``,
    Spring 의 jndi-lookup / jndiName, 자리값 설정기의 파일 위치."""
    src = _strip_xml_comments(text)
    lines = _Lines(src)
    filtered = col.in_filtered(rel)
    for key, default, off in iter_placeholders(src):
        col.placeholder(key, default, "xml-placeholder", rel, lines.line(off), _line_text(src, lines, off),
                        filtered=filtered)
    if web_xml:
        for m in _CTX_PARAM_RE.finditer(src):
            nm, vm = _PARAM_NAME_RE.search(m.group(1)), _PARAM_VALUE_RE.search(m.group(1))
            if nm and nm.group(1).strip():
                value = re.sub(r"\s+", " ", vm.group(1)).strip() if vm else None
                col.add(nm.group(1).strip(), "context-param", rel, lines.line(m.start()),
                        f"<context-param> {nm.group(1).strip()}", value)
        for m in _RES_REF_RE.finditer(src):
            col.add(_jndi_name(m.group(1)), "jndi", rel, lines.line(m.start()), m.group(0)[:200])
        for m in _ENV_ENTRY_RE.finditer(src):
            n = re.search(r"<env-entry-name>\s*([^<]+?)\s*</", m.group(1))
            v = re.search(r"<env-entry-value>\s*([^<]*?)\s*</", m.group(1))
            if n:
                col.add(_jndi_name(n.group(1)), "jndi", rel, lines.line(m.start()), m.group(0),
                        v.group(1) if v else None)
    if context_xml:
        for m in _TC_TAG_RE.finditer(src):
            attrs = dict(_ATTR_RE.findall(m.group(2)))
            name = attrs.get("name")
            if not name:
                continue
            default = attrs.get("value") if m.group(1) == "Environment" else attrs.get("url")
            col.add(_jndi_name(name), "jndi", rel, lines.line(m.start()), m.group(0), default)
    for rx in (_JEE_LOOKUP_RE, _JNDI_PROP_RE, _JNDI_PROP_TEXT_RE):
        for m in rx.finditer(src):
            if "${" not in m.group(1):
                col.add(_jndi_name(m.group(1)), "jndi", rel, lines.line(m.start()), m.group(0))
    for m in _JNDI_XML_LIT_RE.finditer(src):
        col.add(m.group(1), "jndi", rel, lines.line(m.start()), _line_text(src, lines, m.start()))
    if any(h in src for h in _PLACEHOLDER_CFG):
        for m in _LOCATION_RE.finditer(src):
            loc = m.group(1)
            ln = lines.line(m.start())
            if loc.startswith("classpath"):
                # v2.1x D7: `${…}` segments → `*` glob (the packaged profile files count as definitions)
                col.classpath_locations.append(profile_glob(loc.split(":", 1)[1].lstrip("/")))
            else:
                col.file_locations.append(f"{rel}:{ln}: {loc}")
        # v2.1x D6: servlet-context locations (`/WEB-INF/config/app.properties`) — packaged with the web app
        for m in _WEBINF_LOC_RE.finditer(src):
            sub = _webinf_sub(m.group(1))
            if sub and sub not in col.webinf_locations:
                col.webinf_locations.append(sub)
        # ${catalina.base}/conf/x.properties style locations (no file: prefix)
        for m in re.finditer(r"\"(\$\{[^}]+\}[^\"]*\.(?:properties|ya?ml))\"", src):
            col.file_locations.append(f"{rel}:{lines.line(m.start())}: {m.group(1)}")


# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------
def parse_properties(text: str) -> list[tuple[str, str, int]]:
    """.properties 파일의 (키, 값, 줄) 목록. 구분자 ``=`` /
    ``:`` / 공백, ``\\`` 줄 이음, ``#`` / ``!`` 주석을 처리한다.

    (key, value, line) of a .properties file: `=`, `:` or whitespace separators, `\\` continuations, #/! comments."""
    out: list[tuple[str, str, int]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start = i
        line = lines[i].lstrip()
        i += 1
        if not line or line[0] in "#!":
            continue
        while _odd_backslashes(line) and i < len(lines):
            line = line[:-1] + lines[i].lstrip()
            i += 1
        k, j = [], 0
        while j < len(line):
            c = line[j]
            if c == "\\" and j + 1 < len(line):
                k.append(line[j + 1])
                j += 2
                continue
            if c in "=: \t\f":
                break
            k.append(c)
            j += 1
        rest = line[j:].lstrip(" \t\f")
        if rest[:1] in ("=", ":"):
            rest = rest[1:].lstrip(" \t\f")
        key = "".join(k)
        if key:
            out.append((key, rest, start + 1))
    return out


def _odd_backslashes(s: str) -> bool:
    """줄 끝 역슬래시가 홀수 개면 다음 줄로 이어진다(짝수면 문자 그대로의 역슬래시)."""
    n = len(s) - len(s.rstrip("\\"))
    return n % 2 == 1


def _flatten(obj: Any, prefix: str, out: dict[str, Any]) -> None:
    """YAML 문서를 Spring 식 평면 키로(``a.b``, 목록은 ``a[0]``). 빈 dict / 목록도 키로 남긴다."""
    if isinstance(obj, dict):
        if prefix and not obj:
            out[prefix] = ""
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if prefix and not obj:
            out[prefix] = ""
        for idx, v in enumerate(obj):
            _flatten(v, f"{prefix}[{idx}]", out)
    elif prefix:
        out[prefix] = obj


def _profile_names(v: Any) -> list[str]:
    """프로필 값(목록 또는 쉼표 문자열, 프로필 식 ``a & !b`` 포함)에서 프로필 이름들."""
    if v is None:
        return []
    items = v if isinstance(v, list) else str(v).split(",")
    out = []
    for it in items:
        for p in re.split(r"[\s,&|()!]+", str(it)):
            if p:
                out.append(p)
    return out


@dataclass
class _Definitions:
    """정의된 키 색인(main / test 따로): 완화 바인딩 키 → 파일,
    접두어 비교용 조각 목록, 패키지된 파일, 추가 순서 기록."""
    main: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))     # norm key → files
    test: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    main_segs: list[tuple[list[str], str]] = field(default_factory=list)
    test_segs: list[tuple[list[str], str]] = field(default_factory=list)
    packaged: list[str] = field(default_factory=list)
    log: list[tuple[str, str, bool]] = field(default_factory=list)    # (key, file, test) in add order

    def add(self, key: str, file: str, test: bool) -> None:
        """정의 하나를 더한다(``log`` 에도 남겨 MSA 서비스별 색인을 다시 만들 수 있게)."""
        self.log.append((key, file, test))
        d, segs = (self.test, self.test_segs) if test else (self.main, self.main_segs)
        files = d[norm_key(key)]
        if file not in files:
            files.append(file)
        segs.append((_segments(key), file))

    def lookup(self, key: str, include_test: bool) -> list[str]:
        """키를 정의한 파일들. ``p.*`` (ConfigurationProperties 접두어)는 그 아래 키가 하나라도
        있으면 정의된 것. ``include_test`` 는 테스트에서만 읽히는 키일 때 test 리소스도 본다."""
        if key.endswith(".*"):
            want = _segments(key[:-2])
            pools = [self.main_segs] + ([self.test_segs] if include_test else [])
            files: list[str] = []
            for pool in pools:
                for segs, f in pool:
                    if len(segs) > len(want) and [s.split("[")[0] for s in segs[:len(want)]] == want \
                            and f not in files:
                        files.append(f)
            return files
        nk = norm_key(key)
        files = list(self.main.get(nk, []))
        if include_test:
            files += [f for f in self.test.get(nk, []) if f not in files]
        return files


def _definition_files(ws: Any, col: _Collector) -> tuple[list[str], list[str]]:
    """정의 파일 (main, test): resources 바로 아래, ``config/`` · ``META-INF/``
    한 단계, 그리고 설정이 이름으로 부른 ``classpath:`` 위치. 이름으로 불렸는데 패키지에 없는 파일은
    메모(컨테이너 클래스패스에서 오던 것).

    (main, test) definition files: resources root, config/, META-INF + named classpath: locations."""
    main: list[str] = []
    test: list[str] = []
    all_res: list[str] = []
    for ext in ("*.properties", "*.yml", "*.yaml"):
        for rel in ws.glob(ext):
            rel = rel.replace("\\", "/")
            if not _skipped(rel) and ("src/main/resources/" in rel or "src/test/resources/" in rel):
                all_res.append(rel)
    located = set(col.classpath_locations)
    # v2.1x D6: WEB-INF property files named by a location are packaged with the web app (the restructure points the
    # legacy-config copies at their boot stash copy) — their keys are definitions, never `__SET_ME__`
    if col.webinf_locations:
        for ext in ("*.properties", "*.yml", "*.yaml"):
            for rel in ws.glob(ext):
                rel = rel.replace("\\", "/")
                i = ("/" + rel).find("/WEB-INF/")
                if i < 0 or _skipped(rel) or "/src/test/" in "/" + rel:
                    continue
                sub = ("/" + rel)[i + len("/WEB-INF/"):]
                if any(fnmatch.fnmatchcase(sub, w) for w in col.webinf_locations) and rel not in main:
                    main.append(rel)
    for rel in sorted(set(all_res)):
        root = "src/test/resources/" if "src/test/resources/" in rel else "src/main/resources/"
        sub = rel.split(root, 1)[1]
        top = "/" not in sub or sub.startswith(("config/", "META-INF/")) and sub.count("/") == 1
        is_located = sub in located or any(fnmatch.fnmatchcase(sub, loc) for loc in located if "*" in loc)
        if top or is_located:
            (test if root.startswith("src/test") else main).append(rel)
    for loc in sorted(located):
        if not any(r.endswith("resources/" + loc) for r in all_res) and "*" not in loc:
            col.notes.append(f"property file classpath:{loc} is named by the configuration but not packaged "
                             "(it came from the container classpath, e.g. tomcat/lib or shared.loader)")
    return main, test


def _read(ws: Any, rel: str, col: _Collector) -> Optional[str]:
    """파일을 읽는다(1 MB 초과·읽기 실패는 메모 + None, UTF-8 BOM 제거)."""
    try:
        size = ws.abs(rel).stat().st_size if hasattr(ws, "abs") else 0
        if size > MAX_FILE_BYTES:
            col.notes.append(f"skipped {rel}: larger than 1 MB")
            return None
        text = ws.read(rel)
        return text.removeprefix("\ufeff")      # a UTF-8 BOM must not stick to the first key
    except Exception as e:  # noqa: BLE001
        col.notes.append(f"skipped {rel}: unreadable ({e.__class__.__name__})")
        return None


def _scan_definitions(ws: Any, col: _Collector, files: list[str], test: bool, defs: _Definitions) -> None:
    """정의 파일들을 읽어 키를 색인에 넣고, 프로필 이름(파일
    이름·on-profile·profiles.active)을 모으며, 값 안의 ``${…}`` 는 ``config-placeholder``
    참조로 기록한다(``log.dir=${LOG_DIR}`` 같은 자기 참조는 따로 표시)."""
    for rel in files:
        text = _read(ws, rel, col)
        if text is None:
            continue
        base = posixpath.basename(rel)
        m = re.match(r"(?:application|bootstrap)-([\w.-]+)\.(?:properties|ya?ml)$", base)
        if m and not test:
            col.profiles.add(m.group(1))
        filtered = col.in_filtered(rel)
        entries: list[tuple[str, Any, Optional[int]]] = []
        if rel.endswith(".properties"):
            entries = [(k, v, ln) for k, v, ln in parse_properties(text)]
        else:
            try:
                docs = list(yaml.safe_load_all(text))
            except Exception as e:  # noqa: BLE001
                col.notes.append(f"skipped {rel}: unparsable YAML ({str(e).splitlines()[0][:120]})")
                continue
            for doc in docs:
                if doc is None:
                    continue
                if not isinstance(doc, dict):
                    continue
                flat: dict[str, Any] = {}
                _flatten(doc, "", flat)
                for pk in ("spring.config.activate.on-profile", "spring.profiles"):
                    if pk in flat and not test:
                        col.profiles.update(_profile_names(flat[pk]))
                    for k in [k for k in flat if k.startswith(pk + "[")]:
                        if not test:
                            col.profiles.update(_profile_names(flat[k]))
                entries += [(k, v, None) for k, v in flat.items()]
        if not test:
            defs.packaged.append(rel)
        tlines = None
        for key, value, ln in entries:
            defs.add(key, rel, test)
            if key == "spring.profiles.active" or key.startswith("spring.profiles.active["):
                if not test:
                    col.profiles.update(_profile_names(value))
            if isinstance(value, str) and "${" in value:
                for pk, pd, _ in iter_placeholders(value):
                    if norm_key(pk) == norm_key(key):
                        col.self_defs.add((norm_key(key), rel))
                    if ln is None:
                        tlines = tlines or _Lines(text)
                        idx = text.find("${" + pk)
                        line = tlines.line(idx) if idx >= 0 else None
                    else:
                        line = ln
                    col.placeholder(pk, pd, "config-placeholder", rel, line or 0, f"{key}={value}",
                                    filtered=filtered)


# ---------------------------------------------------------------------------
# Maven filtering
# ---------------------------------------------------------------------------
_RESOURCES_RE = re.compile(r"<build\b.*?<resources>(.*?)</resources>", re.S)
_RESOURCE_RE = re.compile(r"<resource>(.*?)</resource>", re.S)


def filtered_resource_dirs(ws: Any, poms: list[PomInfo], col: Optional[_Collector] = None) -> list[str]:
    """``<build><resources>`` 중 ``<filtering>true`` 인 디렉토리들(루트 기준). 그 안의 ``${pom 속성}`` 은
    빌드 때 Maven 이 풀어 주므로 외부 설정 참조로 치지 않는다. plugins / pluginManagement 블록은 뺀다."""
    out: list[str] = []
    for p in poms:
        try:
            text = _strip_xml_comments(ws.read(p.path))
        except Exception:  # noqa: BLE001
            continue
        base = posixpath.dirname(p.path.replace("\\", "/"))
        m = _RESOURCES_RE.search(re.sub(r"<pluginManagement>.*?</pluginManagement>|<plugins>.*?</plugins>", "",
                                        text, flags=re.S))
        if not m:
            continue
        for r in _RESOURCE_RE.finditer(m.group(1)):
            if not re.search(r"<filtering>\s*true\s*</filtering>", r.group(1)):
                continue
            d = re.search(r"<directory>\s*([^<]+?)\s*</directory>", r.group(1))
            directory = d.group(1) if d else "src/main/resources"
            directory = re.sub(r"^\$\{(?:project\.)?basedir\}/?", "", directory).strip("/")
            rel = posixpath.normpath(posixpath.join(base, directory)) if base else posixpath.normpath(directory)
            if rel not in out:
                out.append(rel)
    return sorted(out)


# ---------------------------------------------------------------------------
# 필터링되는 리소스 아래의 바이너리 파일. 주의: 아래 영어 설명의 "handler" (resources-binary-filtering, E12)는
# v1 이야기다 — v2 에는 그 핸들러가 없고, 구조 검토(sbmig.review.inputs)가 이 목록을 사실 줄로 LLM 에 넘긴다
# binary files under a filtered resource (handler `resources-binary-filtering`, catalog E12)
# ---------------------------------------------------------------------------
# maven-resources-plugin / maven-war-plugin copy jpg, jpeg, gif, bmp, png unfiltered by default; every other binary
# under a `<filtering>true</filtering>` resource is read as text — resources-plugin ≥ 3.2 (what the Boot 2.4+ / Boot 4
# parents manage) fails the copy with `MalformedInputException: Input length = 1`, 2.x silently corrupted it (broken
# webfonts). The handler adds these as `<nonFilteredFileExtension>` entries (the plugin's own image list is kept).
BINARY_RESOURCE_EXTENSIONS: tuple[str, ...] = (
    "eot", "ttf", "otf", "woff", "woff2",                                   # webfonts (Bootstrap / FontAwesome)
    "ico", "webp", "tif", "tiff", "psd",                                    # images outside the plugin default
    "pdf", "hwp", "hwpx", "xls", "xlsx", "doc", "docx", "ppt", "pptx",      # documents / report templates
    "zip", "jar", "gz", "tgz", "7z", "rar", "tar",                          # archives
    "jks", "p12", "pfx", "keystore", "cer", "crt", "der",                   # keystores / certificates
    "mp3", "mp4", "wav", "ogg", "swf",                                      # media
    "class", "ser", "dat", "bin", "db", "sqlite", "mdb",                    # binary data
)
MAX_BINARY_RESOURCES = 200
# never binary, whatever the bytes say (a cp949 .properties is text the filter must see)
TEXT_RESOURCE_EXTENSIONS = frozenset({
    "properties", "xml", "yml", "yaml", "json", "txt", "text", "html", "htm", "xhtml", "jsp", "jspf", "jspx", "tag",
    "tld", "js", "mjs", "ts", "css", "scss", "sass", "less", "map", "svg", "sql", "ddl", "md", "csv", "tsv", "vm",
    "ftl", "ftlh", "mustache", "hbs", "xsd", "dtd", "wsdl", "xsl", "xslt", "jrxml", "conf", "cfg", "ini", "sh", "bat",
    "cmd", "groovy", "java", "kt", "py", "factories", "imports", "mf", "log", "lst", "tpl", "rtf", "ics", "vcf",
})
SNIFF_BYTES = 8192


def looks_binary(path: Any) -> bool:
    """확장자를 모르는 리소스의 내용 검사: 앞 8 KB 에 NUL 바이트가 있거나 UTF-8 도 cp949 도
    아니면 바이너리. 회사 실행(2026-10-26)에서 ``static/img/.dm/`` 아래 ``.items.dmdb`` 가 필터 복사 중
    MalformedInputException 을 낸 사례에서 나왔다. 못 읽으면 바이너리 아님. 예외 없음.

    Content sniff for a resource of an unknown extension (round 10 follow-up, company run 2026-10-26: an
    `.items.dmdb` under `static/img/.dm/` failed the filtered copy with MalformedInputException): a NUL byte in the
    first 8 KB, or bytes that are neither UTF-8 nor cp949. Never raises (unreadable = not binary)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in head:
        return True
    for enc in ("utf-8", "cp949"):
        try:
            head.decode(enc)
            return False
        except UnicodeDecodeError as e:
            if e.start >= len(head) - 4:          # a multi-byte sequence cut by the 8 KB window
                return False
    return True


PLUGIN_UNFILTERED_DEFAULTS = frozenset({"jpg", "jpeg", "gif", "bmp", "png"})   # the plugins copy these as-is already


def _binary_by_content(name: str) -> bool:
    """내용 검사가 필요한 이름인지: 텍스트로 알려진 확장자도,
    플러그인이 원래 안 거르는 이미지 확장자도 아닌 것."""
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    return ext not in TEXT_RESOURCE_EXTENSIONS and ext not in PLUGIN_UNFILTERED_DEFAULTS
_RESOURCE_SETS_RE = re.compile(r"<(resources|testResources|webResources)>(.*?)</\1>", re.S)
_ANY_RESOURCE_RE = re.compile(r"<(resource|testResource)>(.*?)</\1>", re.S)
_PATTERN_RE = re.compile(r"<(include|exclude)>\s*([^<]+?)\s*</\1>")


def _ant_re(pattern: str) -> "re.Pattern[str]":
    """Ant / plexus 경로 패턴 → 정규식(``*.properties`` = 맨
    위만, ``**/*.properties`` = 모든 깊이, 끝 ``/`` = ``/**``).

    Ant / plexus path pattern → regex over a resource-relative posix path: `*.properties` = top level only,
    `**/*.properties` = any depth, a trailing `/` means `/**`."""
    pat = pattern.strip().replace("\\", "/").lstrip("/")
    if pat.endswith("/"):
        pat += "**"
    out: list[str] = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _filtered_resource_specs(ws: Any, poms: list[PomInfo]) -> list[tuple[str, list, list]]:
    """``<filtering>true`` 리소스마다 (루트 기준 디렉토리, include 정규식,
    exclude 정규식): resources / testResources / war 의 webResources / copy-resources 실행까지, profile 안의
    것도 센다(profile 이 켜지면 깨지니까).

    (root-relative dir, include regexes, exclude regexes) per `<filtering>true</filtering>` resource: `<build>`
    `<resources>` / `<testResources>`, the war plugin's `<webResources>` and `copy-resources` executions (plugins
    blocks are *not* skipped here, unlike `filtered_resource_dirs`); profile resources count too (one needless
    extension list beats a failing build when the profile is active)."""
    specs: list[tuple[str, list, list]] = []
    for p in poms:
        try:
            text = _strip_xml_comments(ws.read(p.path))
        except Exception:  # noqa: BLE001
            continue
        base = posixpath.dirname(p.path.replace("\\", "/"))
        for m in _RESOURCE_SETS_RE.finditer(text):
            kind = m.group(1)
            for r in _ANY_RESOURCE_RE.finditer(m.group(2)):
                body = r.group(2)
                if not re.search(r"<filtering>\s*true\s*</filtering>", body):
                    continue
                d = re.search(r"<directory>\s*([^<]+?)\s*</directory>", body)
                if d:
                    directory = d.group(1)
                elif kind == "webResources":
                    continue
                else:
                    directory = "src/test/resources" if kind == "testResources" else "src/main/resources"
                directory = re.sub(r"^\$\{(?:project\.)?basedir\}/?", "", directory.replace("\\", "/")).strip("/")
                if "${" in directory:
                    continue
                rel = posixpath.normpath(posixpath.join(base, directory)) if base else posixpath.normpath(directory)
                incl = [_ant_re(x) for k, x in _PATTERN_RE.findall(body) if k == "include"]
                excl = [_ant_re(x) for k, x in _PATTERN_RE.findall(body) if k == "exclude"]
                specs.append((rel.strip("/"), incl, excl))
    return specs


def filtered_binary_resources(ws: Any, poms: list[PomInfo],
                              extensions: Iterable[str] = BINARY_RESOURCE_EXTENSIONS) -> list[str]:
    """필터링되는 리소스가 필터를 통과시킬 바이너리 파일들(include / exclude 반영, 정렬, 최대
    200개): 알려진 바이너리 확장자이거나, 모르는 확장자인데 내용이 바이너리(:func:`looks_binary`)인 것. 빌드는
    통과해도 jar 안의 바이트가 바뀐다(웹폰트 깨짐 등). 예외 없음.

    Root-relative binary files a filtered resource would run through the filter (includes / excludes honoured),
    sorted and capped at MAX_BINARY_RESOURCES: a known binary extension, or (round 10 follow-up) a file of an unknown
    extension whose content is binary (`looks_binary`). Never raises."""
    try:
        specs = _filtered_resource_specs(ws, poms)
        if not specs:
            return []
        exts = {e.lower().lstrip(".") for e in extensions}
        out: set[str] = set()
        for rel in ws.glob("*"):
            rel = rel.replace("\\", "/")
            name = posixpath.basename(rel)
            if _skipped(rel):
                continue
            known = "." in name and name.rsplit(".", 1)[1].lower() in exts
            if not known and not _binary_by_content(name):
                continue
            for d, incl, excl in specs:
                if d in ("", "."):
                    sub = rel
                elif rel.startswith(d + "/"):
                    sub = rel[len(d) + 1:]
                else:
                    continue
                if incl and not any(x.match(sub) for x in incl):
                    continue
                if any(x.match(sub) for x in excl):
                    continue
                if known or looks_binary(ws.abs(rel)):
                    out.add(rel)
                break
        return sorted(out)[:MAX_BINARY_RESOURCES]
    except Exception:  # noqa: BLE001 - never fail the analysis over this
        log.exception("filtered binary resource scan failed")
        return []


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
def _fmt(tpl: str, name: str, leaf: str) -> str:
    """규칙 문자열의 ``{name}`` / ``{leaf}`` 치환."""
    return tpl.replace("{name}", name).replace("{leaf}", leaf)


def _classify(key: str, refs: list[PropertyRef], defs: _Definitions, rules: ExternalizedRules,
              file_routes: bool, self_defs: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset()
              ) -> PropertyFinding:
    """키 하나의 판정: 공급자(jndi / context-param / system-property / property-source:file), 재구성이
    자리값으로 쓸 Boot 키(``boot_keys``; 빈 목록 = 자리값 없음), 안내, 상태(``defined`` 패키지에 정의
    / ``defaulted`` 모든 참조에 코드 기본값 / ``external`` 어디에도 없음 — 실행 시 공급 필요)."""
    sources = {r.source for r in refs}
    test_only = all(r.scope == "test" for r in refs)
    boot_keys: list[str] = [key]
    supplied_by: Optional[str] = None
    note = ""
    boot_defaults = {norm_key(k) for k in rules.boot_defaults}
    if "jndi" in sources:
        supplied_by = "jndi"
        leaf = key.rstrip("/").rsplit("/", 1)[-1]
        rule = next((r for r in rules.jndi if fnmatch.fnmatchcase(key, r.match)), None)
        if rule:
            boot_keys = [_fmt(t, key, leaf) for t in rule.boot_keys]
            note = _fmt(rule.note, key, leaf)
        else:
            boot_keys = []
            note = f"JNDI {key}: no rule in externalized.yml matches — map it manually"
    elif "context-param" in sources:
        supplied_by = "context-param"
        known = rules.context_param_known.get(key)
        if known and known.skip:
            boot_keys = []
        elif known and known.boot_keys is not None:
            boot_keys = list(known.boot_keys)
        else:
            boot_keys = [_fmt(rules.context_param_default, key, key)] if rules.context_param_default else []
        note = known.note if known else "web.xml context-param (embedded Tomcat does not read web.xml)"
    elif key.endswith(".*"):
        boot_keys = []
        note = f"@ConfigurationProperties prefix {key[:-2]}: set the bound properties {key[:-2]}.<field>"
    elif sources == {"system-env"}:
        boot_keys = []
        note = "environment variable (System.getenv / ${env.*}): set it in the runtime environment"
    elif sources == {"system-property"}:
        supplied_by = "system-property"
        boot_keys = []          # application.properties never feeds System.getProperty: only -D works (no placeholder)
        note = "read via System.getProperty: pass -D" + key + "=… on the java command line"
    if norm_key(key) in boot_defaults:
        boot_keys = []
        note = "Boot default" + (f"; {note}" if note else "")
    if supplied_by is None and "xml-placeholder" in sources and any(
            r.file.endswith(("web.xml", "context.xml")) for r in refs if r.source == "xml-placeholder"):
        supplied_by = "system-property"
    if refs and all(r.raw.startswith(COND_RAW) for r in refs):
        boot_keys = []
        note = "read by @ConditionalOnProperty only: unset is a valid state (the condition decides), no placeholder"
    if supplied_by is None and file_routes and sources & {"value-annotation", "environment", "xml-placeholder",
                                                          "property-source", "config-placeholder"}:
        supplied_by = "property-source:file"

    nk = norm_key(key)
    # 자기 참조(``log.dir=${LOG_DIR}``)는 정의로 치지 않는다 — 완화 바인딩으로는 LOG_DIR 이 log.dir 자신에
    # 의해 정의된 것처럼 보이므로
    defined_in = [f for f in defs.lookup(key, include_test=test_only) if (nk, f) not in self_defs]
    if not defined_in and boot_keys and boot_keys != [key]:
        per = [defs.lookup(b, include_test=test_only) for b in boot_keys]
        if all(per):
            defined_in = sorted({f for fs in per for f in fs})
    if defined_in:
        status = "defined"
    elif refs and all(r.default is not None and r.source not in ("context-param", "jndi") for r in refs):
        status = "defaulted"
    else:
        status = "external"
    return PropertyFinding(key=key, status=status, refs=refs, defined_in=defined_in,  # type: ignore[arg-type]
                           boot_keys=boot_keys, supplied_by=supplied_by, note=note)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def _is_context_xml(rel: str) -> bool:
    """Tomcat 의 ``META-INF/context.xml`` 인지."""
    return rel.endswith("META-INF/context.xml")


def scan_properties(ws: Any, poms: list[PomInfo], scan: ScanResult, rules: ExternalizedRules,
                    cfg: Any = None, *, services: Optional[list[ServiceInfo]] = None) -> PropertyInventory:
    """외부 설정 목록(모듈 docstring 참고). ``services`` (MSA 배포 서비스, round 9)를 주면
    서비스별 목록, None / [] 이면 프로젝트 전체. 예외를 내지 않는다 — 문제는 ``notes`` 로 (어떤 이상한
    프로젝트에서도 분석은 살아남아야 한다).

    Inventory of externalized settings (see module docstring). `services` (MSA deployables, round 9) switches to a
    per-service inventory; None / [] = project-wide. Never raises: problems become `notes`."""
    try:
        return _scan_properties(ws, poms, scan, rules, cfg, services or [])
    except Exception as e:  # noqa: BLE001 - the analysis must survive any odd project
        log.exception("externalized-config scan failed")
        return PropertyInventory(notes=list(rules.notes) + [f"externalized-config scan failed: {e!r}"])


def _service_dirs(services: list[ServiceInfo]) -> list[tuple[str, str]]:
    """서비스마다 (디렉토리, 이름), 깊은 것부터. 디렉토리 = root_pom 의 디렉토리(루트 서비스는 "").

    (dir, name) per service, deepest first; dir = dirname(root_pom), "" for a root service."""
    out = [(posixpath.dirname(s.root_pom.replace("\\", "/")).strip("/"), s.name) for s in services]
    return sorted(out, key=lambda x: (-len(x[0]), x[0]))


def _owner(rel: str, dirs: list[tuple[str, str]]) -> Optional[str]:
    """``rel`` 을 품는 서비스(가장 깊은 것). None = 어느 서비스에도 속하지 않는 공유 코드.

    The service whose directory holds `rel` (deepest wins); None = outside every service (shared code)."""
    for d, name in dirs:
        if d == "" or rel == d or rel.startswith(d + "/"):
            return name
    return None


def _scan_properties(ws: Any, poms: list[PomInfo], scan: ScanResult, rules: ExternalizedRules,
                     cfg: Any, services: list[ServiceInfo]) -> PropertyInventory:
    """:func:`scan_properties` 의 본체: 자바 → XML(Spring XML, web.xml, context.xml) → 정의 파일(main,
    test, 인라인) → (MSA 면 서비스별 정의 색인) → 키마다 판정 → 메모(잘림, file: 경로, 컨테이너 속성)."""
    maven_props: set[str] = set()
    for p in poms:
        maven_props.update(p.properties)
    col = _Collector(rules, maven_props, filtered_resource_dirs(ws, poms))

    for j in scan.files:
        if _skipped(j.path):
            continue
        text = _read(ws, j.path, col)
        if text is not None:
            scan_java(col, j.path, text)

    xml_files: dict[str, tuple[bool, bool]] = {}
    for rel in scan.xml_bean_configs:
        xml_files[rel] = (False, False)
    for rel in scan.web_xml_paths:
        xml_files[rel] = (True, False)
    for rel in ws.glob("context.xml"):
        rel = rel.replace("\\", "/")
        if _is_context_xml(rel) and not _skipped(rel):
            xml_files[rel] = (False, True)
    for rel in ws.glob("*.xml"):
        rel = rel.replace("\\", "/")
        if rel in xml_files or _skipped(rel) or posixpath.basename(rel) == "pom.xml":
            continue
        if "src/main/resources/" in rel or "src/main/webapp/WEB-INF/" in rel:
            text = _read(ws, rel, col)
            # only Spring XML: mybatis mappers / logback / ehcache use ${…} for their own substitution
            if text is not None and "springframework.org/schema" in text:
                xml_files[rel] = (False, False)
    for rel in sorted(xml_files):
        if _skipped(rel):
            continue
        text = _read(ws, rel, col)
        if text is not None:
            web, ctx = xml_files[rel]
            scan_xml(col, rel, text, web_xml=web, context_xml=ctx)

    main_files, test_files = _definition_files(ws, col)
    defs = _Definitions()
    _scan_definitions(ws, col, main_files, False, defs)
    _scan_definitions(ws, col, test_files, True, defs)
    for key, rel in col.inline_defs:
        defs.add(key, rel, _is_test(rel))
    _bootstrap_note(col, main_files, poms)

    dirs = _service_dirs(services)
    # MSA: 서비스 안의 참조는 그 서비스의 정의만 보고 판정한다(다른
    # 서비스의 application.yml 이 대신 정의해 주지 않는다)
    svc_defs: dict[Optional[str], _Definitions] = {None: defs}     # shared code: every service's definitions
    if dirs:
        for _, name in dirs:
            svc_defs[name] = _Definitions()
        for key, rel, test in defs.log:
            owner = _owner(rel, dirs)
            if owner is not None:
                svc_defs[owner].add(key, rel, test)

    by_key: dict[tuple[Optional[str], str], list[PropertyRef]] = defaultdict(list)
    for r in col.refs:
        by_key[(_owner(r.file, dirs) if dirs else None, r.key)].append(r)
        if r.source == "context-param" and r.key in ("spring.profiles.active", "spring.profiles.default") \
                and r.default:
            col.profiles.update(_profile_names(r.default))
    findings: list[PropertyFinding] = []
    truncated = 0
    for svc, key in sorted(by_key, key=lambda sk: (sk[1], sk[0] or "")):
        refs = sorted(by_key[(svc, key)], key=lambda r: (r.file, r.line or 0, r.source))
        if len(refs) > MAX_REFS_PER_KEY:
            truncated += 1
            refs = refs[:MAX_REFS_PER_KEY]
        f = _classify(key, refs, svc_defs[svc], rules, bool(col.file_locations), col.self_defs)
        f.service = svc
        findings.append(f)
    if truncated:
        col.notes.append(f"{truncated} key(s) had more than {MAX_REFS_PER_KEY} references; the list was truncated")
    if col.file_locations:
        shown = sorted(set(col.file_locations))
        col.notes.append("external property files (supply route property-source:file, not packaged): "
                         + "; ".join(shown[:10]) + (f" (+{len(shown) - 10} more)" if len(shown) > 10 else ""))
    if col.container_props:
        col.notes.append("container system properties referenced (no placeholder; embedded Tomcat does not set "
                         "them): " + ", ".join(f"{k} ({len(v)} file(s))" for k, v in sorted(col.container_props.items())))
    return PropertyInventory(findings=findings, packaged_files=sorted(set(defs.packaged)),
                             profiles=sorted(col.profiles), filtered_resources=col.filtered,
                             filtered_binary_files=filtered_binary_resources(ws, poms),
                             notes=_dedupe(col.notes), services=[s.name for s in services])


def _bootstrap_note(col: _Collector, main_files: list[str], poms: list[PomInfo]) -> None:
    """bootstrap.* 정의 파일이 있는데 spring-cloud-starter-bootstrap
    이 없으면 메모: Boot 4 에서는 그 파일을 읽지 않는다."""
    boot = [f for f in main_files if re.match(r"bootstrap(-[\w.-]+)?\.(properties|ya?ml)$", posixpath.basename(f))]
    if boot and not any(d.artifact_id == "spring-cloud-starter-bootstrap" for p in poms for d in p.dependencies):
        col.notes.append("bootstrap.* files define keys (counted as defined: " + ", ".join(boot[:5])
                         + "), but on Boot 4 bootstrap.* is read only by the Spring Cloud bootstrap context "
                         "(spring-cloud-starter-bootstrap, off by default since Spring Cloud 2020.0) — move the keys "
                         "to application.* or use spring.config.import")


def _dedupe(xs: list[str]) -> list[str]:
    """순서를 지키며 중복 제거."""
    seen: set[str] = set()
    return [x for x in xs if not (x in seen or seen.add(x))]
