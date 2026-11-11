"""Maven 빌드 로그 → 구조화된 ``BuildError`` 목록. 오류 목록과 케이스(L2 서명)의 출발점이다.
흐름상 위치: ``MavenRunner`` 가 실패한 빌드마다(변환 후 컴파일·검증 빌드·수정 루프 재빌드) ``parse_maven_log``
를 부르고, OpenRewrite 실패 메시지 정리에도 쓰인다. 각 오류의 L1 / L2 서명은 ``signatures.fill_signatures`` 가
채운다. 방식: 한 번 훑는 줄 단위 상태 기계 — 모듈 / 목표(goal) 문맥을 따라가며 javac 진단(이어지는 symbol /
location 줄 포함), 의존성·parent·플러그인 해결 실패, enforcer, 잘못된 pom, 플러그인 실행 실패를 블록으로 모아
오류로 만든다. 지킬 것: 실패한 빌드에서 빈 목록을 돌려주지 않는다(못 알아들으면 UNKNOWN 하나). 경로는 프로젝트
기준 상대경로로 정리하고(Windows 드라이브·``file:`` 표기 포함) 메시지의 루트 경로는 지운다 — 그래야 서명이 PC
마다 같다. 함정: javac 는 앞 단계(구문) 오류가 있으면 뒤 단계(타입) 오류를 보여 주지 않고, Maven reactor 는 첫
실패 모듈 뒤를 SKIPPED 로 둔다 → 로그 한 번은 오류의 일부만
보여 준다(그래서 수정 루프가 라운드마다 다시 빌드·파싱한다).

Maven log → structured ``BuildError`` list.

A single pass, line-oriented state machine. It understands (Maven 3.6 … 3.9 output, ``-B`` batch mode):

* module / goal context: ``-----< g:a >-----`` headers, ``Building <name> <version>``,
  ``--- <plugin>:<ver>:<goal> (<id>) @ <artifactId> ---`` (short ``compiler:3.13.0:compile`` and the older
  ``maven-compiler-plugin:3.8.1:compile`` forms) and ``Failed to execute goal … on project X``;
* javac / kotlinc diagnostics with ``symbol:`` / ``location:`` / ``required:`` / ``found:`` / ``reason:``
  continuation lines (both the ``COMPILATION ERROR :`` block, where continuations are unprefixed, and the final
  ``[ERROR]``-prefixed repetition; duplicates are merged);
* annotation processor crashes (lombok on new JDKs);
* dependency / parent / plugin resolution, enforcer rules, invalid or non-parseable POMs, test failures, generic
  plugin execution failures.

Everything that is not understood but makes the build fail ends up as one ``UNKNOWN`` error so the caller never
gets an empty list for a failed build.


"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from sbmig.core.models import GAV, BuildError, ErrorKind

from .signatures import classify_javac, fill_signatures, set_reactor_modules

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")
# 줄 앞의 타임스탬프(로그 설정에 따라 붙음)를 떼고 [INFO] / [ERROR] 레벨을 읽는다.
_TS_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[T ])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\s+(?=\[)")
_LEVEL_RE = re.compile(r"^\[(INFO|ERROR|WARNING|WARN|FATAL|DEBUG)\] ?")

# 문맥 패턴: '-----< g:a >-----' 머리줄·'Building <name> <ver>' → 지금 빌드 중인 모듈(이후 오류의 module),
# '--- compiler:3.13.0:compile (id) @ app ---' → 지금 도는 플러그인 목표(BuildError.goal 의 짧은 이름
# ``compiler:compile``), 'Failed to execute goal <좌표> (<id>) on project <모듈>: <메시지>' → Maven 의 최종
# 실패 줄(failed_goal 블록).
_HEADER_RE = re.compile(r"^-+< (?P<g>[\w.\-]+):(?P<a>[\w.\-]+) >-+\s*$")
_BUILDING_RE = re.compile(r"^Building (?P<name>.+?) (?P<ver>[^\s:]+?)(?:\s+\[\d+/\d+\])?\s*$")
_MOJO_RE = re.compile(
    r"^--- (?:(?P<g>[\w.\-]+):)?(?P<p>[\w.\-]+):(?P<v>[\w.\-]+):(?P<goal>[\w.\-]+)"
    r"(?: \((?P<id>[^)]*)\))? @ (?P<a>[\w.\-]+) ---")
_FAILED_GOAL_RE = re.compile(
    r"^Failed to execute goal (?:(?P<coords>[\w.\-]+:[\w.\-]+:[\w.\-]+:[\w.\-]+)(?: \((?P<id>[^)]*)\))? )?"
    r"on project (?P<proj>[\w.\-]+)\s*:?\s*(?P<msg>.*)$")
# javac 진단 '<파일>.java:[줄,칸] 메시지'(Windows 드라이브 경로, groovy / scala / kotlin 파일 포함)와 kotlinc
# 의 'e: <파일>.kt: (줄, 칸): 메시지'. 이어지는 'symbol: / location: / required: / found: / reason:' 줄은 L1 /
# L2 서명의 재료라서 원래 오류에 붙인다(``_CONT_RE``).
_JAVAC_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^\s\[\]][^\[\]]*?\.(?:java|groovy|scala|kt)):\[(?P<line>\d+)(?:,(?P<col>\d+))?\]"
    r"\s*(?P<msg>.*)$")
_KOTLIN_RE = re.compile(
    r"^e: (?:file://)?(?P<file>\S+?\.kts?)(?::\s*\((?P<line>\d+),\s*(?P<col>\d+)\):|:(?P<line2>\d+):(?P<col2>\d+):?)"
    r"\s*(?P<msg>.*)$")
_CONT_RE = re.compile(r"^\s+(?P<key>symbol|location|required|found|reason)\s*:\s*(?P<val>.*)$")
_COORDS = r"[\w.\-]+:[\w.\-]+(?::[\w.\-]+){1,3}"
# 블록을 여는 문구: 의존성 해결 실패, 플러그인 해결 실패, enforcer 규칙, pom 처리 문제, 누락된
# version(재구성으로 parent 가 바뀌어 관리 버전이 사라졌을 때 흔하다), 파싱 안 되는 pom, parent / import BOM
# 해결 실패. ``-> [Help n]`` 꼬리는 블록의 끝 표시.
_DEP_START_RE = re.compile(r"Could not resolve dependencies for project|Failed to collect dependencies at|"
                           r"Failed to resolve requested artifacts")
_PLUGIN_RES_RE = re.compile(r"Plugin (?P<coords>[\w.\-]+:[\w.\-]+:[\w.\-]+) or one of its dependencies could not be "
                            r"resolved|Error resolving version for plugin '(?P<ga>[\w.\-]+:[\w.\-]+)'|"
                            r"No plugin found for prefix '(?P<prefix>[\w.\-]+)'|Plugin not found for prefix")
_RULE_RE = re.compile(r"^Rule (?P<n>\d+): (?P<rule>\S+?) failed with message:?\s*(?P<rest>.*)$")
# 2026-11-05 (회사 PC): `Unresolveable build extension: Plugin org.apache.maven.wagon:wagon-webdav:jar:1.0-beta-2 or one of its
# dependencies could not be resolved: Failed to read artifact descriptor …` — <build><extensions> 의 확장(배포 전송 wagon 등)을 Maven 이
# 모델 단계에서 못 받으면 어떤 goal 도 돌지 않는다. 좌표를 읽어 확장별 케이스로 만든다 (g:a[:type]:v, type 은 jar 등 선택)
# Maven 3 prints `Plugin.getId()` = g:a:v (no type) here; the `:jar:` form appears only in the "Failed to read artifact descriptor for
# g:a:jar:v" tail — the optional type group accepts both; Maven 4 spells it "Unresolvable" (review 2026-11-05)
_BUILD_EXT_RE = re.compile(r"Unresolv(?:e)?able build extension: Plugin (?P<g>[\w.\-]+):(?P<a>[\w.\-]+):"
                           r"(?:(?P<t>jar|maven-plugin|pom|bundle):)?(?P<v>[\w.\-]+) or one of its dependencies could not be resolved")
BUILD_EXTENSION_RE = _BUILD_EXT_RE      # public name for the orchestrator's OpenRewrite-failure check
_POM_PROBLEMS_RE = re.compile(r"Some problems were encountered while processing the POMs")
_PROJECT_PROBLEMS_RE = re.compile(
    r"^\s*The project (?P<gav>\S*) \((?P<path>[^)]*)\) has \d+ errors?")
_MISSING_VERSION_RE = re.compile(r"'(?P<field>[\w.]+\.version)' for (?P<coords>[\w.\-]+:[\w.\-]+)(?::[\w.\-]+)* is missing")
_NON_PARSEABLE_RE = re.compile(r"Non-parseable POM (?P<path>.+?\.xml|[^:]+):\s*(?P<msg>.*)$")
# Maven 이 pom 문제 뒤에 붙이는 위치: "@ line 88, column 21"(그 프로젝트의 pom 자체), 또는 상속된 문제면
# "@ <부모 g:a:v>, <부모 pom 경로>, line 88, column 21" — 문제가 있는 파일은 뒤쪽 경로다(회사 실행 2026-11-05: 루트 pom 의
# version 없는 spring-dao 가 자식 모듈 pom 의 오류로 기록돼 LLM 이 자식 pom 만 고쳤다)
# the location Maven appends to a POM problem: `@ line N, column M` (the project's own pom) or, for an inherited
# problem, `@ <parent g:a:v>, <parent pom path>, line N, column M` — the file to fix is that path
_POM_LOCATION_RE = re.compile(r"\s@\s+(?:(?P<gav>[\w.\-]+:[\w.\-]+:[\w.\-]+(?::[\w.\-]+)*),\s+)?"
                              r"(?:(?P<path>[^,]+?\.(?:xml|pom)),\s+)?line (?P<line>\d+)(?:, column (?P<col>\d+))?\s*$")
_PARENT_RE = re.compile(r"Non-resolvable parent POM(?: for (?P<child>[\w.\-]+:[\w.\-]+:[\w.\-]+))?")
_IMPORT_RE = re.compile(r"Non-resolvable import POM")
_TEST_ENTRY_RE = re.compile(r"^\s{2,}(?P<entry>(?:Run \d+: )?\S.*)$")
_HELP_SUFFIX_RE = re.compile(r"\s*->\s*\[Help \d+\]\s*$")

# Maven 이 오류 끝에 붙이는 안내 문구 — 오류가 아니므로 버린다(블록을 닫는 신호로만 쓴다).
_BOILERPLATE = (
    "-> [Help", "[Help ", "To see the full stack trace", "Re-run Maven using", "For more information about the errors",
    "After correcting the problems", "mvn <args> -rf", "mvn <goals> -rf", "The build could not read",
    "Please refer to dump files", "Please refer to ",
)

# 컴파일러 계열 플러그인: 그 'Failed to execute goal … Compilation failure' 줄은 javac 진단이 이미 오류로
# 잡혔으면 중복이라 버린다. _AP_MARKERS = 애노테이션 프로세서(lombok 등)가 새 JDK 에서 죽을 때의 흔적.
_COMPILER_PLUGINS = {"compiler", "kotlin", "groovy", "gmavenplus", "scala", "aspectj", "ajc"}
_AP_MARKERS = ("com.sun.tools.javac", "lombok", "ExceptionInInitializerError", "TypeTag", "IllegalAccessError",
               "NoSuchFieldError", "annotation processor", "AnnotationProcessor", "Processor")


def strip_ansi(text: str) -> str:
    """ANSI 색·커서 코드 제거(``-B`` 없이 돈 Maven, Windows 콘솔 출력도 같은 모양으로 파싱하려고)."""
    return ANSI_RE.sub("", text)


def plugin_short_name(artifact_id: str) -> str:
    """플러그인 artifactId 를 짧은 이름으로(BuildError.goal 과 L2 서명의 ``compiler:compile`` 꼴).

    maven-compiler-plugin → compiler, kotlin-maven-plugin → kotlin, compiler → compiler."""
    m = re.match(r"^maven-(.+)-plugin$", artifact_id)
    if m:
        return m.group(1)
    m = re.match(r"^(.+?)-maven-plugin$", artifact_id)
    if m:
        return m.group(1)
    return artifact_id


def parse_coords(coords: str) -> GAV:
    """Maven 좌표 문자열 g:a[:type[:classifier]]:version → GAV(칸 수로 의미를 정한다).

    Maven coordinates g:a[:type[:classifier]]:version → GAV."""
    parts = coords.strip().split(":")
    if len(parts) == 2:
        return GAV(group_id=parts[0], artifact_id=parts[1])
    if len(parts) == 3:
        return GAV(group_id=parts[0], artifact_id=parts[1], version=parts[2])
    if len(parts) == 4:
        return GAV(group_id=parts[0], artifact_id=parts[1], type=parts[2], version=parts[3])
    return GAV(group_id=parts[0], artifact_id=parts[1], type=parts[2], classifier=parts[3], version=parts[-1])


def _split_level(line: str) -> tuple[str | None, str]:
    """줄 → (레벨, 본문). 타임스탬프를 떼고, ``[ERROR] [ERROR] …`` 처럼 겹친 레벨은 하나로,
    WARN 은 WARNING 으로 맞춘다."""
    line = _TS_RE.sub("", line.rstrip("\r\n"))
    m = _LEVEL_RE.match(line)
    if not m:
        return None, line
    level = m.group(1)
    body = line[m.end():]
    while True:                                          # "[ERROR] [ERROR] Some problems..."
        m2 = _LEVEL_RE.match(body)
        if not m2:
            break
        body = body[m2.end():]
    if level == "WARN":
        level = "WARNING"
    return level, body


def _is_boilerplate(body: str) -> bool:
    b = body.strip()
    return not b or any(b.startswith(p) for p in _BOILERPLATE)


# ---------------------------------------------------------------------------
# path handling
# ---------------------------------------------------------------------------
def _root_prefixes(project_root: Path | str | None) -> list[str]:
    """로그에서 지울 프로젝트 루트 접두어 후보(긴 것부터). 심볼릭 링크를 푼 경로도 넣고, macOS 는 /tmp 와
    /private/tmp 가 같은 곳이라 두 표기를 다 넣는다."""
    if project_root is None or str(project_root) == "":
        return []
    raw = _clean_path(str(project_root)).rstrip("/")
    cands = {raw}
    try:
        cands.add(_clean_path(str(Path(project_root).resolve())).rstrip("/"))
    except OSError:
        pass
    for c in sorted(cands):                              # copy: the set grows inside the loop
        if c.startswith("/private/"):
            cands.add(c[len("/private"):])
        elif c.startswith(("/tmp/", "/var/")):
            cands.add("/private" + c)
    return sorted(cands, key=len, reverse=True)


_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:/|$)")


def _clean_path(path: str) -> str:
    """경로 하나를 로그 표기 → 슬래시·스킴 없는 꼴로. Windows 의 javac 는 ``/C:/work/proj/src/…``, kotlinc 와
    일부 플러그인은 ``file:///C:/…``(퍼센트 인코딩)로 찍는다 → 모두 ``C:/work/proj/src/…``. 앞의 ``/C:/``
    때문에 ``ws.abs`` 가 드라이브가 두 번 붙은 경로를 만들던 일이 있었다(round 10).

    One path as Maven / javac / kotlinc print it → forward slashes, no ``file:`` scheme, and a Windows drive path
    without the URI-style leading slash. On Windows the in-process javac (plexus ``toUri().getPath()``) prints
    ``/C:/work/proj/src/…``; kotlinc and some plugins print ``file:///C:/…`` or ``file:/C:/…`` (percent-encoded).
    All become ``C:/work/proj/src/…`` — a leading ``/C:/`` made ``ws.abs`` build ``C:\\C:\\…`` (round 10)."""
    p = path.strip().replace("\\", "/")
    if p[:5].lower() == "file:":
        p = unquote(p[5:])
        if p.startswith("///") or (p.startswith("//") and _DRIVE_RE.match(p[2:])):
            p = p[2:]                                   # file:///C:/x, file:///work/x, file://C:/x
    if p.startswith("/") and _DRIVE_RE.match(p[1:]):
        p = p[1:]                                       # /C:/work/x → C:/work/x
    return p


def _is_drive_path(p: str) -> bool:
    return bool(_DRIVE_RE.match(p))


def _relativize(path: str, prefixes: list[str]) -> str:
    """``path`` 를 처음 맞는 프로젝트 루트 기준 상대경로(POSIX)로, 안 맞으면 정리만 한 경로. Windows 접두어는
    대소문자를 무시한다(``c:/work`` 와 ``C:/Work`` 는 같은 디렉토리).

    ``path`` relative to the first matching project-root prefix (POSIX separators), else the cleaned path.
    Windows prefixes (``C:/…``) compare case-insensitively: ``c:/work`` / ``C:/Work`` name the same directory."""
    p = _clean_path(path)
    for pre in prefixes:
        fold = _is_drive_path(pre)
        a, b = (p.lower(), pre.lower()) if fold else (p, pre)
        if a == b:
            return "."
        if a.startswith(b + "/"):
            return str(PurePosixPath(p[len(pre) + 1:]))
    return p


@lru_cache(maxsize=64)
def _scrub_regex(prefixes: tuple[str, ...]) -> re.Pattern[str] | None:
    """프로젝트 루트의 모든 표기(``/work/proj/``, ``file:///work/proj/``, ``C:/work/proj/``, 역슬래시 표기,
    ``/C:/work/proj/``, ``file:///c:/…``)를 한 번에 지우는 정규식. 메시지·raw 에서 PC 마다 다른 절대경로를
    없애야 L1 / L2 서명이 PC·실행마다 같아진다.

    One alternation for every project-root prefix in every spelling a log uses: ``/work/proj/``,
    ``file:///work/proj/``, ``C:/work/proj/``, ``C:\\work\\proj\\``, ``/C:/work/proj/``, ``file:///c:/…`` (Windows
    prefixes case-insensitively)."""
    alts: list[str] = []
    for pre in prefixes:                                # longest first (`_root_prefixes` sorts them)
        if not pre:
            continue
        body = "".join(r"[/\\]" if ch == "/" else re.escape(ch) for ch in pre)
        if _is_drive_path(pre):
            alts.append(r"(?i:(?:file:/{0,3})?/?" + body + r")[/\\]")
        else:
            alts.append(r"(?:file:(?://)?)?" + body + "/")
    return re.compile("|".join(alts)) if alts else None


def _scrub(text: str, prefixes: list[str]) -> str:
    rx = _scrub_regex(tuple(prefixes))
    return rx.sub("", text) if rx is not None and text else text


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
@dataclass
class _Block:
    """여러 줄에 걸친 오류 덩어리(failed_goal / dep / plugin_res / pom / project / rule / tests). 닫힐 때
    ``_finish_<kind>`` 가 오류로 만든다."""
    kind: str                         # failed_goal | dep | plugin_res | pom | project | rule | tests
    lines: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    module: str | None = None
    goal: str | None = None
    plugin: str | None = None      # short name (compiler, enforcer, surefire...)
    coords: str | None = None
    msg: str = ""
    pom_file: str | None = None


class _Parser:
    """줄 단위 상태 기계. 현재 모듈 / 목표 문맥, 열린 블록, 직전 javac 오류(이어지는 줄을 붙일 곳)를 들고
    다니며, 두 번 찍힌 같은 javac 오류·같은 의존성 / parent / 플러그인 실패는 키로 합친다."""
    def __init__(self, project_root: Path | str | None):
        self.prefixes = _root_prefixes(project_root)
        self.root = Path(project_root) if project_root not in (None, "") else None
        self.errors: list[BuildError] = []
        self.module: str | None = None
        self.goal: str | None = None
        self._pending_artifact: str | None = None
        self._javac: dict[tuple, BuildError] = {}
        self._last_javac: BuildError | None = None
        self._block: _Block | None = None
        self._pom_errors: dict[str, BuildError] = {}
        self._dep_keys: set[tuple] = set()
        self._parent_keys: set[str] = set()
        self._plugin_keys: set[str] = set()
        self._enforcer_emitted = False
        self._test_entries: list[str] = []
        self._in_results = False
        self._results_section: str | None = None
        self.meaningful_errors: list[str] = []

    # ------------------------------------------------------------- helpers
    def _add(self, err: BuildError) -> BuildError:
        # 서명을 만들기 전에 프로젝트 루트 경로부터 지운다(PC 마다 다른 경로가 L1 / L2 에 섞이지 않게).
        err.raw = _scrub(err.raw, self.prefixes)
        err.message = _scrub(err.message, self.prefixes).strip()
        fill_signatures(err)
        self.errors.append(err)
        return err

    def _rel(self, p: str) -> str:
        return _relativize(p, self.prefixes)

    # ------------------------------------------------------------- main loop
    def feed(self, lines: Iterable[str]) -> None:
        """줄을 하나씩 넣고(ANSI 제거·레벨 분리) 끝나면 열린 블록을 닫는다."""
        for original in lines:
            line = strip_ansi(original).rstrip("\r\n")
            level, body = _split_level(line)
            self._line(line, level, body)
        self._close_block()

    def _line(self, line: str, level: str | None, body: str) -> None:
        """한 줄 처리. 순서가 중요하다: (1) 직전 javac 오류의 이어지는 줄(symbol / location / 들여쓴 줄)을
        먼저 붙이고, (2) INFO 는 문맥만·WARNING 은 enforcer 규칙만 보고, (3) ERROR 의 javac / kotlin 진단과
        surefire 결과, (4) 그 밖은 블록 처리."""
        # 1) javac continuation lines (prefixed or not)
        if self._last_javac is not None and level in (None, "ERROR"):
            m = _CONT_RE.match(body)
            if m:
                key, val = m.group("key"), m.group("val").strip()
                if key == "symbol" and not self._last_javac.symbol:
                    self._last_javac.symbol = val
                elif key == "location" and not self._last_javac.location:
                    self._last_javac.location = val
                self._last_javac.raw += "\n" + line
                return
            if level is None and body.startswith((" ", "\t")) and body.strip():
                self._last_javac.raw += "\n" + line
                return
            if level == "ERROR" and body.startswith("    ") and not _is_boilerplate(body) \
                    and not _JAVAC_RE.match(body.strip()):
                self._last_javac.raw += "\n" + line
                return
        self._last_javac = None

        if level == "ERROR" or level == "FATAL":
            if not _is_boilerplate(body) and not body.startswith("COMPILATION ERROR"):
                self.meaningful_errors.append(body.strip())

        # 2) context lines
        if level == "INFO" or level is None and self._block is None:
            if level == "INFO":
                self._context(body)
                self._close_block()
                self._results(level, body)
            return
        if level == "WARNING":
            m = _RULE_RE.match(body.strip())
            if m and (self._block is None or self._block.kind != "failed_goal"):
                self._close_block()
                self._block = _Block(kind="rule", module=self.module, goal=self.goal)
                self._block.lines.append(body.strip())
                self._block.raw.append(line)
                return
            if self._block is not None and self._block.kind == "rule":
                self._block.lines.append(body.strip())
                self._block.raw.append(line)
                return
            return

        # 3) javac / kotlin diagnostics
        if level == "ERROR":
            stripped = body.strip()
            m = _JAVAC_RE.match(stripped)
            if m:
                self._javac_error(m, line)
                return
            m = _KOTLIN_RE.match(stripped)
            if m:
                self._kotlin_error(m, line)
                return
            if self._results(level, body):
                return

        # 4) block handling
        self._blocks(line, level, body)

    def _context(self, body: str) -> None:
        """INFO 줄로 현재 모듈 / 목표 문맥을 갱신한다: '< g:a >' 머리줄이 artifactId 를 정하고, 'Building
        …' 은 머리줄이 없을 때만 이름을 쓰며, '--- plugin:ver:goal @ a ---' 가 목표를 정한다."""
        m = _HEADER_RE.match(body)
        if m:
            self._pending_artifact = m.group("a")
            self.module = m.group("a")
            self.goal = None
            return
        m = _BUILDING_RE.match(body)
        if m and not m.group("name").endswith(":"):
            self.module = self._pending_artifact or m.group("name")
            self._pending_artifact = None
            self.goal = None
            return
        m = _MOJO_RE.match(body)
        if m:
            self.module = m.group("a")
            self.goal = f"{plugin_short_name(m.group('p'))}:{m.group('goal')}"

    def _results(self, level: str | None, body: str) -> bool:
        """surefire 'Results:' 구간을 따라가며 실패한 테스트 항목을 모은다(v2 는
        테스트를 건너뛰므로 보통 비어 있다).

        Track surefire's 'Results:' section; collect failing test entries."""
        b = body.strip()
        if level == "INFO" and b == "Results:":
            self._in_results = True
            self._results_section = None
            return True
        if not self._in_results:
            return False
        if level == "ERROR" and b in ("Failures:", "Errors:"):
            self._results_section = b
            return True
        if level == "ERROR" and self._results_section and _TEST_ENTRY_RE.match(body):
            entry = body.strip()
            if not entry.startswith("Run "):
                self._test_entries.append(entry)
            return True
        if b.startswith("Tests run:"):
            self._in_results = False
            self._results_section = None
            return level == "ERROR"
        return False

    # ------------------------------------------------------------- javac
    def _kind_for(self, file: str, goal: str | None) -> ErrorKind:
        # Windows javac 경로는 역슬래시다 → 먼저 슬래시로
        # 바꿔야 src/test 아래 오류가 TEST_COMPILE 로 분류된다.
        # Windows javac paths come with backslashes (D:\work\x\src\test\java\…): normalise before the path test, or
        # a test-source error is filed as COMPILE and the test-compile fallback (L7) / `scope test` never apply
        path = "/" + (file or "").replace("\\", "/")
        if (goal or "").endswith("testCompile") or (goal or "").endswith("test-compile") \
                or "/src/test/" in path or "/src/it/" in path:
            return ErrorKind.TEST_COMPILE
        return ErrorKind.COMPILE

    def _goal_module(self) -> tuple[str | None, str | None]:
        if self._block is not None and self._block.kind == "failed_goal":
            return self._block.module, self._block.goal
        return self.module, self.goal

    def _record_javac(self, file: str, line_no: int, col: int | None, msg: str, raw: str) -> None:
        """javac 오류 기록. 'COMPILATION ERROR' 블록과 마지막 [ERROR] 반복이 같은 오류를 두 번 찍으므로 (파일,
        줄, 칸, 메시지) 키로 합치고, 두 번째 것의 이어지는 줄은 ``_Shadow`` 를 통해 첫 오류의 빈 symbol /
        location 만 채운다. 서명은 이어지는 줄이 다 붙은 뒤 ``finish`` 에서 채운다."""
        msg = re.sub(r"^error:\s*", "", msg.strip())
        rel = self._rel(file)
        module, goal = self._goal_module()
        key = (rel, line_no, col, msg)
        existing = self._javac.get(key)
        if existing is not None:
            self._last_javac = _Shadow(existing)           # merge continuations into the first occurrence
            return
        kind = self._kind_for(rel, goal)
        err = BuildError(kind=kind, message=msg, file=rel, line=line_no, column=col, module=module,
                         goal=goal or ("compiler:testCompile" if kind == ErrorKind.TEST_COMPILE else None), raw=raw)
        self.errors.append(err)                             # signatures filled at the end (continuations pending)
        self._javac[key] = err
        self._last_javac = err

    def _javac_error(self, m: re.Match[str], line: str) -> None:
        col = int(m.group("col")) if m.group("col") else None
        self._record_javac(m.group("file"), int(m.group("line")), col, m.group("msg"), line)

    def _kotlin_error(self, m: re.Match[str], line: str) -> None:
        ln = m.group("line") or m.group("line2")
        col = m.group("col") or m.group("col2")
        self._record_javac(m.group("file"), int(ln), int(col) if col else None, m.group("msg"), line)

    # ------------------------------------------------------------- blocks
    def _blocks(self, line: str, level: str | None, body: str) -> None:
        """블록 열기 / 이어 붙이기 / 닫기. 'Failed to execute goal' 이 가장 강하고(열린 블록을 닫고 새로
        연다), 그다음 pom 문제, 플러그인 해결, 의존성 해결, enforcer 규칙 순. 블록 밖의 'Non-resolvable parent
        POM' / 'Non-parseable POM' / FATAL 은 바로 pom 오류. ``-> [Help n]`` 꼬리가 붙은 줄은 그 블록의 끝."""
        b = body.strip()
        if level in ("ERROR", "FATAL"):
            m = _FAILED_GOAL_RE.match(b)
            if m:
                self._close_block()
                coords = m.group("coords")
                plugin = goal = None
                if coords:
                    parts = coords.split(":")
                    plugin = plugin_short_name(parts[1])
                    goal = f"{plugin}:{parts[3]}"
                msg = _HELP_SUFFIX_RE.sub("", m.group("msg")).strip()
                self._block = _Block(kind="failed_goal", module=m.group("proj"), goal=goal, plugin=plugin,
                                     coords=coords, msg=msg, lines=[msg] if msg else [], raw=[line])
                if _HELP_SUFFIX_RE.search(b):
                    self._close_block()
                return
            if _POM_PROBLEMS_RE.search(b):
                self._close_block()
                self._block = _Block(kind="pom", raw=[line])
                return
            m = _PROJECT_PROBLEMS_RE.match(b)
            if m:
                self._close_block()
                gav = m.group("gav")
                module = gav.split(":")[1] if gav.count(":") >= 2 else None
                self._block = _Block(kind="project", module=module, pom_file=self._rel(m.group("path")), raw=[line])
                return
            if self._block is None or self._block.kind not in ("failed_goal",):
                # a "Plugin … or one of its dependencies could not be resolved" line INSIDE a pom / project block is a build-extension
                # problem of that project (Maven 3 prints it type-less there) — not a plugin resolution block of its own
                if _PLUGIN_RES_RE.search(b) and not (self._block and self._block.kind in ("pom", "project")):
                    self._close_block()
                    self._block = _Block(kind="plugin_res", module=self.module, goal=self.goal, lines=[b], raw=[line])
                    if _HELP_SUFFIX_RE.search(b):
                        self._close_block()
                    return
                if _DEP_START_RE.search(b) and not (self._block and self._block.kind in ("pom", "project")):
                    self._close_block()
                    self._block = _Block(kind="dep", module=self.module, lines=[b], raw=[line])
                    if _HELP_SUFFIX_RE.search(b):
                        self._close_block()
                    return
                m = _RULE_RE.match(b)
                if m and self._block is None:
                    self._block = _Block(kind="rule", module=self.module, goal=self.goal, lines=[b], raw=[line])
                    return
            if self._block is None:
                if level == "FATAL" or b.startswith(("Non-resolvable parent POM", "Non-parseable POM")):
                    self._pom_line(b, line, module=self.module, pom_file=None)
                return
            if _is_boilerplate(body):
                if self._block.kind == "project" and not b and self._block.lines or self._block.kind not in ("pom", "project") or b.startswith(("-> [Help", "To see the full", "The build could not read")):
                    self._close_block()
                return
            self._block.lines.append(_HELP_SUFFIX_RE.sub("", b))
            self._block.raw.append(line)
            if _HELP_SUFFIX_RE.search(b) and self._block.kind not in ("pom", "project"):
                self._close_block()
            return
        # level None: unprefixed continuation (legacy enforcer messages, " @ " markers, stack traces)
        if self._block is not None and b and b != "@":
            if self._block.kind in ("rule", "failed_goal", "dep", "plugin_res"):
                if b.startswith("at ") or b.startswith("Caused by"):
                    return
                self._block.lines.append(b)
                self._block.raw.append(line)

    def _close_block(self) -> None:
        blk, self._block = self._block, None
        if blk is None:
            return
        handler = getattr(self, f"_finish_{blk.kind}")
        handler(blk)

    # ------------------------------------------------------------- finishers
    def _finish_pom(self, blk: _Block) -> None:
        for b, raw in zip(blk.lines, blk.raw[1:] or blk.raw):
            self._pom_line(b, raw, module=None, pom_file=None)

    def _finish_project(self, blk: _Block) -> None:
        for b, raw in zip(blk.lines, blk.raw[1:]):
            self._pom_line(b, raw, module=blk.module, pom_file=blk.pom_file)

    def _pom_line(self, b: str, raw: str, module: str | None, pom_file: str | None) -> None:
        """pom 문제 한 줄 → 오류 하나: parent 해결 실패(PARENT_RESOLUTION, parent 좌표로 중복 제거), import
        BOM 실패(DEPENDENCY_RESOLUTION), 나머지는 POM_INVALID(누락된 version 의 좌표, 파싱 안 되는 pom 파일
        경로). 같은 메시지는 한 번만(뒤에 오는 것은 비어 있던 모듈 / 파일만 채운다)."""
        msg = _HELP_SUFFIX_RE.sub("", b).strip()
        if not msg or msg == "@" or _is_boilerplate(msg):
            return
        key = _scrub(msg, self.prefixes)
        # 문제가 있는 pom 과 줄: 위치 꼬리의 경로(상속된 문제 = 부모 pom)가 있으면 그것, 없으면 그 프로젝트의 pom
        pom_file, line_no, col_no = self._pom_location(msg, pom_file)
        m_ext = _BUILD_EXT_RE.search(msg)
        if m_ext:
            # 같은 확장은 한 오류: 요약 줄과 프로젝트 블록 줄의 꼬리(이유)가 달라도 좌표로 합친다
            key = f"build-extension:{m_ext.group('g')}:{m_ext.group('a')}:{m_ext.group('v')}"
        existing = self._pom_errors.get(key)
        if existing is not None:
            if module and not existing.module:
                existing.module = module
                fill_signatures(existing)
            if pom_file and not existing.file:
                existing.file = pom_file
            if line_no and not existing.line:
                existing.line, existing.column = line_no, col_no
            if m_ext and len(msg) > len(existing.message):
                existing.message = msg                      # the project block carries the reason (401 / 404 / timeout)
            return
        if m_ext:
            art = GAV(group_id=m_ext.group("g"), artifact_id=m_ext.group("a"), version=m_ext.group("v"))
            err = self._add(BuildError(kind=ErrorKind.POM_INVALID, message=msg, artifact=art, module=module,
                                       file=pom_file, line=line_no, column=col_no, raw=raw))
            self._pom_errors[key] = err
            return
        if _PARENT_RE.search(msg):
            arts = _artifacts_in(msg)
            parent = arts[0] if arts else None
            child = _PARENT_RE.search(msg).group("child")
            if parent is not None:
                parent = GAV(group_id=parent.group_id, artifact_id=parent.artifact_id, version=parent.version)
            k = parent.coordinates if parent else msg
            if k in self._parent_keys:
                return
            self._parent_keys.add(k)
            err = self._add(BuildError(kind=ErrorKind.PARENT_RESOLUTION, message=msg, artifact=parent,
                                       module=module or (child.split(":")[1] if child else None),
                                       file=pom_file, line=line_no, column=col_no, raw=raw))
            self._pom_errors[key] = err
            return
        if _IMPORT_RE.search(msg):
            arts = _artifacts_in(msg)
            err = self._add(BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message=msg,
                                       artifact=arts[0] if arts else None, module=module, file=pom_file,
                                       line=line_no, column=col_no, raw=raw))
            self._pom_errors[key] = err
            return
        artifact = None
        file = pom_file
        m = _MISSING_VERSION_RE.search(msg)
        if m:
            artifact = parse_coords(m.group("coords"))
        m2 = _NON_PARSEABLE_RE.search(msg)
        if m2 and m2.group("path").endswith(".xml"):
            file = self._rel(m2.group("path"))
        err = self._add(BuildError(kind=ErrorKind.POM_INVALID, message=msg, artifact=artifact, module=module,
                                   file=file, line=line_no, column=col_no, raw=raw))
        self._pom_errors[key] = err

    def _pom_location(self, msg: str, pom_file: str | None) -> tuple[str | None, int | None, int | None]:
        """pom 문제 메시지의 위치 꼬리 → (파일, 줄, 열). 꼬리에 경로가 있으면(상속된 문제: 부모 pom) 그 경로를 프로젝트
        루트 기준으로, 없으면 넘겨받은 그 프로젝트의 pom. 꼬리가 없으면 (pom_file, None, None).

        (file, line, column) from the message's location tail: the path in the tail (an inherited problem names the
        parent pom) relativised, else the project's own pom; no tail → (pom_file, None, None)."""
        m = _POM_LOCATION_RE.search(msg)
        if not m:
            return pom_file, None, None
        path = (m.group("path") or "").strip()
        file = self._rel(path) if path else pom_file
        col = m.group("col")
        return file, int(m.group("line")), (int(col) if col else None)

    def _finish_dep(self, blk: _Block) -> None:
        """의존성 해결 실패 블록 → 아티팩트마다 오류 하나. Maven 버전마다 문구가 달라 여러 패턴을 보고,
        구체적인 문구('Could not find / transfer artifact', 우선순위 3)가 목록 문구(우선순위 1)를 이긴다.
        실패한 프로젝트 자신의 좌표는 빼고, 같은 (모듈, 좌표)는 한 번만. 서명(L2 =
        'dependency_resolution|g:a')이 아티팩트별이라 케이스도 아티팩트별이다."""
        text = "\n".join(blk.lines)
        module = blk.module
        m = re.search(r"Could not resolve dependencies for project (?P<p>" + _COORDS + ")", text)
        if m and not module:
            module = parse_coords(m.group("p")).artifact_id
        found: dict[str, tuple[GAV, str, int]] = {}

        def put(gav: GAV, detail: str, prio: int) -> None:
            k = f"{gav.group_id}:{gav.artifact_id}:{gav.version or ''}"
            if k not in found or prio > found[k][2]:
                found[k] = (gav, detail.strip(), prio)

        for ln in blk.lines:
            for mm in re.finditer(r"Could not (?:find|transfer) artifact (" + _COORDS + r")(?: (?:in|from/to) \S+ \([^)\s]*\))?"
                                  r"(?:: (?!Could not)[^\n]+?(?= -> |$))?", ln):
                put(parse_coords(mm.group(1)), mm.group(0), 3)
            for mm in re.finditer(r"Failure to find (" + _COORDS + r") in \S+", ln):
                put(parse_coords(mm.group(1)), mm.group(0), 3)
            mm = re.search(r"The following artifacts could not be resolved: (.*?)(?:: Could not|: Failure|$)", ln)
            if mm:
                for item in re.findall(r"(" + _COORDS + r")(?: \(([^)]*)\))?", mm.group(1)):
                    put(parse_coords(item[0]), f"Could not resolve artifact {item[0]}" +
                        (f" ({item[1]})" if item[1] else ""), 1)
            mm = re.match(r"^dependency: (" + _COORDS + r")(?: \((\w+)\))?", ln.strip())
            if mm:
                put(parse_coords(mm.group(1)), f"Could not resolve dependency {mm.group(1)}", 1)
            mm = re.search(r"Failed to read artifact descriptor for (" + _COORDS + r")", ln)
            if mm:
                put(parse_coords(mm.group(1)), mm.group(0), 2)
            elif "Failed to collect dependencies at" in ln:
                chain = re.findall(_COORDS, ln.split("Failed to collect dependencies at", 1)[1])
                if chain:
                    put(parse_coords(chain[-1]), f"Failed to collect dependencies at {' -> '.join(chain)}", 2)
        if m:
            project_key = parse_coords(m.group("p"))
            found.pop(f"{project_key.group_id}:{project_key.artifact_id}:{project_key.version or ''}", None)
        raw = "\n".join(blk.raw)
        if not found:
            self._add(BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message=blk.lines[0] if blk.lines else text,
                                 module=module, raw=raw))
            return
        for gav, detail, _prio in found.values():
            gav = GAV(group_id=gav.group_id, artifact_id=gav.artifact_id, version=gav.version,
                      type=gav.type if gav.type not in (None, "jar") else None, classifier=gav.classifier)
            k = (module, gav.coordinates)
            if k in self._dep_keys:
                continue
            self._dep_keys.add(k)
            self._add(BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message=_HELP_SUFFIX_RE.sub("", detail).strip(),
                                 artifact=gav, module=module, raw=raw))

    def _finish_plugin_res(self, blk: _Block) -> None:
        """플러그인 해결 실패 블록 → 오류 하나(좌표 또는 g:a 를 artifact 로, 같은 플러그인은 한 번만)."""
        text = "\n".join(blk.lines)
        m = _PLUGIN_RES_RE.search(text)
        artifact = None
        if m and m.group("coords"):
            artifact = parse_coords(m.group("coords"))
        elif m and m.group("ga"):
            artifact = parse_coords(m.group("ga"))
        key = artifact.coordinates if artifact else text
        if key in self._plugin_keys:
            return
        self._plugin_keys.add(key)
        message = blk.lines[0].rstrip(":") if blk.lines else text
        detail = next((ln for ln in blk.lines[1:] if ln.strip()), "")
        if detail:
            message = f"{message}: {detail.strip()}"
        self._add(BuildError(kind=ErrorKind.PLUGIN_RESOLUTION, message=message, artifact=artifact,
                             module=blk.module, goal=blk.goal, raw="\n".join(blk.raw)))

    def _finish_rule(self, blk: _Block) -> None:
        self._emit_rules(blk.lines, blk.raw, blk.module, blk.goal or "enforcer:enforce")

    def _emit_rules(self, lines: list[str], raw: list[str], module: str | None, goal: str | None) -> int:
        """enforcer 'Rule n: <규칙> failed with message: …' 를 규칙마다 ENFORCER 오류
        하나로. 만든 수를 돌려준다."""
        rules: list[tuple[str, list[str]]] = []
        for ln in lines:
            m = _RULE_RE.match(ln.strip())
            if m:
                rules.append((m.group("rule"), [m.group("rest")] if m.group("rest") else []))
            elif rules and ln.strip():
                if ln.strip().startswith(("Failed to execute goal", "Some Enforcer rules have failed")):
                    continue
                rules[-1][1].append(ln.strip())
        for rule, msg_lines in rules:
            simple = rule.rsplit(".", 1)[-1]
            msg = f"{simple}: " + " ".join(msg_lines) if msg_lines else f"{simple} failed"
            self._add(BuildError(kind=ErrorKind.ENFORCER, message=msg, module=module, goal=goal,
                                 raw="\n".join(raw)))
        if rules:
            self._enforcer_emitted = True
        return len(rules)

    def _finish_failed_goal(self, blk: _Block) -> None:
        """'Failed to execute goal' 블록 마무리. 의존성 / 플러그인 해결이면 그쪽으로 넘긴다. 컴파일러
        계열은 javac 진단이 이미 잡혔으면 버리고(중복), 아니면 COMPILE 오류 하나(애노테이션 프로세서가 죽은
        경우 ``annotation-processor:`` 접두어 — 서명이 lombok 등으로 묶는다). enforcer·surefire 는 각자
        처리, 나머지는 PLUGIN_EXECUTION."""
        text = "\n".join(blk.lines)
        raw = "\n".join(blk.raw)
        if _DEP_START_RE.search(text) or "The following artifacts could not be resolved" in text:
            self._finish_dep(_Block(kind="dep", lines=blk.lines, raw=blk.raw, module=blk.module))
            return
        if _PLUGIN_RES_RE.search(text):
            self._finish_plugin_res(_Block(kind="plugin_res", lines=[ln for ln in blk.lines if ln], raw=blk.raw,
                                           module=blk.module, goal=blk.goal))
            return
        message = blk.msg or next((ln for ln in blk.lines if ln.strip()), "") or "plugin execution failed"
        plugin = blk.plugin or ""
        if plugin in _COMPILER_PLUGINS:
            if True:
                if any(e.module == blk.module or blk.module is None for e in self._javac.values()) or \
                        (self._javac and "Compilation failure" in text):
                    return
                kind = ErrorKind.TEST_COMPILE if (blk.goal or "").endswith("testCompile") else ErrorKind.COMPILE
                if "Fatal error compiling" in text and any(mk in text for mk in _AP_MARKERS) \
                        and "release version" not in text and "invalid target release" not in text:
                    detail = text.split("Fatal error compiling:", 1)[-1].strip().splitlines()[0].strip()
                    msg = f"annotation-processor: {detail}"
                else:
                    msg = message.replace("Compilation failure:", "").strip() or message
                    if msg.startswith("Fatal error compiling:"):
                        msg = msg.split(":", 1)[1].strip() or msg
                self._add(BuildError(kind=kind, message=msg, module=blk.module, goal=blk.goal, raw=raw))
                return
        if plugin == "enforcer":
            if self._emit_rules(blk.lines, blk.raw, blk.module, blk.goal) or self._enforcer_emitted:
                return
            self._add(BuildError(kind=ErrorKind.ENFORCER, message=message, module=blk.module, goal=blk.goal, raw=raw))
            return
        if plugin in ("surefire", "failsafe") and re.search(r"test failures|There was a timeout|"
                                                            r"There are test failures|fork", text):
            entries = self._test_entries or []
            if not entries:
                self._add(BuildError(kind=ErrorKind.TEST_FAILURE, message=message, module=blk.module,
                                     goal=blk.goal, raw=raw))
            for entry in entries:
                sym = entry.split(" ", 1)[0].split(":", 1)[0]
                self._add(BuildError(kind=ErrorKind.TEST_FAILURE, message=entry, symbol=sym, module=blk.module,
                                     goal=blk.goal, raw=raw + "\n" + entry))
            self._test_entries = []
            return
        if not message.strip() and len(blk.lines) > 1:
            message = blk.lines[1]
        self._add(BuildError(kind=ErrorKind.PLUGIN_EXECUTION, message=message, module=blk.module, goal=blk.goal,
                             raw=raw))

    def finish(self, text: str) -> list[BuildError]:
        """마무리: javac 오류마다 경로 지우기·소스에서 symbol 보충·서명 채우기(이어지는 줄이 다 붙은
        뒤라 여기서 한다). 실패한 빌드인데 오류가 하나도 없으면 첫 의미 있는 [ERROR] 줄로 UNKNOWN
        하나를 만든다(빈 목록 금지)."""
        sources = _SourceLines(self.root)
        for err in self._javac.values():
            err.raw = _scrub(err.raw, self.prefixes)
            if not err.symbol:
                _symbol_from_source(err, sources)
            fill_signatures(err)
        failed = "BUILD FAILURE" in text or (bool(self.meaningful_errors) and "BUILD SUCCESS" not in text)
        if not self.errors and failed:
            first = next((e for e in self.meaningful_errors if not e.startswith("COMPILATION")), "BUILD FAILURE")
            tail = "\n".join(strip_ansi(text).splitlines()[-40:])
            self._add(BuildError(kind=ErrorKind.UNKNOWN, message=first, module=self.module, raw=tail))
        return self.errors


# ---------------------------------------------------------------------------
# symbols javac does not print (round 10: the L2 signature is the case-catalog key)
# ---------------------------------------------------------------------------
# javac 가 이름을 찍지 않는 오류(@Override 대상 메서드, static import 대상 클래스)는 소스 줄에서 읽어 symbol
# 을 채운다 — 없으면 모든 프로젝트의 이런 오류가 L2 하나 = 케이스 하나로 뭉친다(round 10).
MAX_SOURCE_BYTES = 2_000_000
_OVERRIDE_RE = re.compile(r"@\s*(?:java\.lang\.)?Override\b")
_ANNOTATION_RE = re.compile(r"@\s*[\w$.]+\s*")
_DECL_NAME_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_STATIC_IMPORT_RE = re.compile(r"^\s*import\s+static\s+([\w$.]+?)\s*\.\s*(?:\*|[\w$]+)\s*;")
_JAVA_NOISE_RE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
_NOT_A_NAME = frozenset({"if", "for", "while", "switch", "catch", "synchronized", "return", "new", "throw", "super",
                         "this", "try", "do", "else", "case", "assert"})


class _SourceLines:
    """프로젝트 소스 줄 캐시(파싱 한 번에 파일당 한 번 읽기). UTF-8 → cp949(MS949) → latin-1 순서로 디코딩한다
    — 찾는 것은 ASCII 식별자뿐이라 latin-1 로도 충분하다.

    Lines of project sources, read once per parse (UTF-8, else MS949, else latin-1 — identifiers are ASCII)."""

    def __init__(self, root: Path | None):
        self.root = root
        self._cache: dict[str, list[str] | None] = {}

    def lines(self, file: str | None) -> list[str] | None:
        """파일의 줄 목록(캐시). 없거나, 못 읽거나, ``MAX_SOURCE_BYTES`` 보다 크면 None."""
        if not file:
            return None
        if file in self._cache:
            return self._cache[file]
        out: list[str] | None = None
        p = Path(file)
        if not p.is_absolute() and not _is_drive_path(file):
            p = self.root / file if self.root is not None else None
        try:
            if p is not None and p.is_file() and p.stat().st_size <= MAX_SOURCE_BYTES:
                data = p.read_bytes()
                for enc in ("utf-8", "cp949", "latin-1"):
                    try:
                        out = data.decode(enc).splitlines()
                        break
                    except UnicodeDecodeError:
                        continue
        except OSError:
            out = None
        self._cache[file] = out
        return out


def _overriding_method(lines: list[str], line_no: int, col: int | None) -> str:
    """(줄, 칸)의 ``@Override`` 가 붙은 메서드 이름. 뒤따르는 다른 애노테이션(괄호 인자 포함)은 건너뛰고, 선언
    머리의 첫 ``name(`` 이 그 메서드다. 주석·문자열은 먼저 지운다.

    Name of the method an ``@Override`` at (line, col) annotates: the annotations after it (with their arguments)
    are skipped, the first ``name(`` of the declaration header is the method."""
    if not (1 <= line_no <= len(lines)):
        return ""
    first = lines[line_no - 1]
    hits = [m.start() for m in _OVERRIDE_RE.finditer(first)]
    start = min(hits, key=lambda h: abs(h - ((col or 1) - 1))) if hits else 0
    text = _JAVA_NOISE_RE.sub(" ", "\n".join([first[start:], *lines[line_no:line_no + 20]]))
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text[i] != "@":
            break
        m = _ANNOTATION_RE.match(text, i)
        if not m:
            return ""
        i = m.end()
        if i < n and text[i] == "(":                    # @RequestMapping(value = "/x", method = GET)
            depth = 0
            while i < n:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
    header = re.split(r"[{;=]", text[i:], maxsplit=1)[0]
    m = _DECL_NAME_RE.search(header)
    return m.group(1) if m and m.group(1) not in _NOT_A_NAME else ""


def _symbol_from_source(err: BuildError, sources: _SourceLines) -> None:
    """``method does not override …`` 와 ``static import only from classes and interfaces`` 는 무엇에 관한
    오류인지 이름을 찍지 않는다 → 이것 없이는 모든 프로젝트의 그런 오류가 L2 하나(= 케이스 하나)로 뭉쳤다.
    보고된 소스 줄에서 ``symbol`` 을 채운다: 애노테이션이 붙은 선언의 ``method <name>``, static import 의
    ``class <qualifier>``. 소스를 못 읽으면 아무것도 안 한다(L2 는 분류 이름만 남는다).

    ``method does not override …`` and ``static import only from classes and interfaces`` never name what they are
    about; without it every such error in the fleet shared one L2 (= one catalog case). Fills ``symbol`` from the
    reported source line: ``method <name>`` of the annotated declaration, ``class <qualifier>`` of the static import.
    Nothing when the source cannot be read (the L2 then stays the bare class)."""
    cls = classify_javac(err.message)
    if cls not in ("method_does_not_override", "static_import_only") or not err.line:
        return
    lines = sources.lines(err.file)
    if not lines:
        return
    if cls == "method_does_not_override":
        name = _overriding_method(lines, err.line, err.column)
        if name:
            err.symbol = f"method {name}"
        return
    if 1 <= err.line <= len(lines):
        m = _STATIC_IMPORT_RE.match(lines[err.line - 1])
        if m:
            err.symbol = f"class {m.group(1)}"


class _Shadow:
    """두 번째로 찍힌 같은 javac 오류의 이어지는 줄을 받는 대리 객체: 원래 오류의 비어 있는 symbol /
    location 만 채우고 raw 는 버린다(원래 오류가 이미 raw 블록을 가짐).

    Continuation sink for a duplicate javac error: fills missing symbol/location of the original only."""

    def __init__(self, target: BuildError):
        self._t = target

    @property
    def symbol(self) -> str | None:
        return self._t.symbol

    @symbol.setter
    def symbol(self, v: str) -> None:
        self._t.symbol = v

    @property
    def location(self) -> str | None:
        return self._t.location

    @location.setter
    def location(self, v: str) -> None:
        self._t.location = v

    @property
    def raw(self) -> str:
        return ""

    @raw.setter
    def raw(self, v: str) -> None:                      # the original already carries its raw block
        pass


def _artifacts_in(msg: str) -> list[GAV]:
    out: list[GAV] = []
    for rx in (r"Could not find artifact (" + _COORDS + ")", r"Failure to find (" + _COORDS + ")",
               r"Could not transfer artifact (" + _COORDS + ")",
               r"The following artifacts could not be resolved: (" + _COORDS + ")",
               r"Failed to read artifact descriptor for (" + _COORDS + ")"):
        for m in re.finditer(rx, msg):
            g = parse_coords(m.group(1))
            if all(o.coordinates != g.coordinates for o in out):
                out.append(g)
    return out


_PARENT_SPLIT_RE = re.compile(r"Non-resolvable parent POM")
_PARENT_HINT_RE = re.compile(r"(?i)\bparent\b")


def parent_coordinates_in(text: str) -> list[GAV]:
    """Maven 문장에서 해석하지 못한 parent pom 의 좌표(g:a:v, 순서대로, 중복 없이). ``Non-resolvable parent POM for …:``
    뒤의 ``Could not find artifact g:a:pom:v`` / ``The following artifacts could not be resolved: g:a:pom:v`` /
    ``Failed to read artifact descriptor for g:a:pom:v`` / ``Could not transfer artifact g:a:pom:v`` 를 읽는다
    (``'parent.relativePath' points at …`` 꼬리가 있든 없든). 그 머리말이 없으면 "parent" 를 말하는 문장의 pom 형식 좌표만.
    버전 없는 좌표도 그대로 돌려준다. 예외 없음.

    The coordinates (g:a:v, in order, de-duplicated) of the parent poms Maven could not resolve: the four message shapes
    after `Non-resolvable parent POM for …:`, with or without the relativePath tail; without that head, only pom-typed
    coordinates of a text that mentions a parent. Never raises."""
    out: list[GAV] = []
    try:
        text = text or ""
        chunks = []
        heads = list(_PARENT_SPLIT_RE.finditer(text))
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            line_end = text.find("\n", m.end())
            chunks.append(text[m.end():min(end, line_end) if line_end != -1 else end])
        if not heads and _PARENT_HINT_RE.search(text):
            chunks = [text]
        for ch in chunks:
            arts = [a for a in _artifacts_in(ch) if a.type in (None, "pom")]
            if not heads:
                arts = [a for a in arts if a.type == "pom"]
            for a in arts[:1] if heads else arts:
                g = GAV(group_id=a.group_id, artifact_id=a.artifact_id, version=a.version)
                if all(o.coordinates != g.coordinates for o in out):
                    out.append(g)
    except Exception:  # noqa: BLE001 - a parser helper never raises
        return out
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
_POM_SKIP_DIRS = frozenset({"target", ".git", ".svn", ".hg", "node_modules", ".idea", ".sbmig", "out", "build"})
_POM_WALK_LIMIT = 2000


def reactor_artifact_ids_from_poms(root: Path | str | None) -> list[str]:
    """프로젝트 트리의 모든 pom.xml 이 선언하는 artifactId(부모 좌표 제외) — 케이스 키의 ``<module>`` 마스킹 레지스트리에
    로그보다 먼저 넣는 이름들. 로그는 fail-fast 리액터가 도달한 모듈만 보여 주므로, 트리에서 읽어야 빌드마다 같다. target/
    등은 건너뛰고, 깨진 pom 은 조용히 넘기며, 절대 예외를 내지 않는다(없는 루트 → []).

    Every artifactId the project's pom.xml files declare (the pom's own, not its `<parent>`), for the `<module>` masking
    registry; read from the tree because a fail-fast reactor log names only the modules it reached. Never raises."""
    if not root:
        return []
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[str] = []
    seen = 0
    try:
        import xml.etree.ElementTree as ET
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _POM_SKIP_DIRS and not d.startswith(".")]
            if "pom.xml" not in filenames:
                continue
            seen += 1
            if seen > _POM_WALK_LIMIT:
                break
            try:
                tree = ET.parse(os.path.join(dirpath, "pom.xml"))
                proj = tree.getroot()
                if proj.tag.rsplit("}", 1)[-1] != "project":
                    continue
                for child in proj:
                    if child.tag.rsplit("}", 1)[-1] == "artifactId" and (child.text or "").strip():
                        out.append(child.text.strip())
                        break
            except Exception:  # noqa: BLE001 - a broken pom is the build's problem, not the parser's
                continue
    except Exception:  # noqa: BLE001
        return sorted(set(out))
    return sorted(set(out))


def parse_maven_log(text: str, project_root: Path | str | None = None, *,
                    known_modules: Iterable[str] = ()) -> list[BuildError]:
    """Maven 로그 전체 → 구조화된 오류 목록(L1 / L2 서명까지 채움). 이상한 입력에도 예외를 내지 않는다.

    Parse a full Maven log into structured errors (signatures filled). Never raises on odd input."""
    # 서명을 채우기 전에 이 로그의 reactor artifactId 를 전부 등록한다(교체): 오류가 자기 모듈보다 먼저 찍힌
    # 다른 모듈의 이름(예: 루트 artifactId)을 담아도 L2 에서 <module> 로 가려져 다른 프로젝트와 같은 케이스가 된다.
    # v2.1z 리뷰 R3: 로그에 찍힌 모듈(실패 지점까지만 도달)에 더해 호출자가 아는 리액터 artifactId(pom 트리 전체)도 넣어, 리액터
    # 순서가 바뀌어도(수정이 모듈 의존성을 추가, -T 병렬 빌드) 빌드마다 같은 이름이 가려지게 한다 — 교체 의미는 그대로
    # the caller's reactor artifactIds (the whole pom tree) join the log's own, so the masking does not depend on how
    # far a fail-fast reactor got; still replaced per parse
    set_reactor_modules([*known_modules, *reactor_artifact_ids(text)])
    parser = _Parser(project_root)
    parser.feed(text.splitlines())
    return parser.finish(text)


def reactor_artifact_ids(text: str) -> list[str]:
    """로그에 나온 reactor 모듈 artifactId 전부(나온 순서, 중복 없음): ``-----< g:a >-----`` 머리줄,
    ``--- plugin:ver:goal (id) @ <a> ---`` 표시, ``Failed to execute goal … on project <a>``. 'Building <name>'
    의 이름은 표시 이름(<name>)일 수 있어 쓰지 않는다 — artifactId 가 확실한 것만.

    Every reactor artifactId the log shows, in order of appearance: ``< g:a >`` headers, ``@ <artifactId>`` mojo
    markers and ``on project <artifactId>`` failure lines (the ``Building <name>`` display name is not certain)."""
    seen: dict[str, None] = {}
    for raw in (text or "").splitlines():
        level, body = _split_level(strip_ansi(raw))
        b = body.strip()
        if not b:
            continue
        m = _HEADER_RE.match(b) or _MOJO_RE.match(b)
        if m:
            seen.setdefault(m.group("a"), None)
            continue
        if level in ("ERROR", "FATAL"):
            m = _FAILED_GOAL_RE.match(b)
            if m:
                seen.setdefault(m.group("proj"), None)
    return list(seen)


_REACTOR_LINE_RE = re.compile(r"^(?P<name>\S.*?)\s+(?:\.+\s*)?(?P<status>SUCCESS|FAILURE|SKIPPED)\b")


def parse_reactor_summary(text: str) -> tuple[list[str], str | None]:
    """(성공한 모듈 목록, 실패한 모듈). 다중 모듈은 Reactor Summary 를, 단일 모듈은 'Building <name>' 줄을
    쓰고, 이름은 가능하면 ``< g:a >`` 머리줄의 artifactId 로 바꾼다.

    (modules_built, failed_module) — artifactIds where known (from the ``< g:a >`` headers), else names.

    Uses the Reactor Summary for multi-module builds; for single-module builds the ``Building <name>`` lines.
    ``modules_built`` lists modules that finished with SUCCESS.
    
    
    """
    names: dict[str, str] = {}
    building: list[str] = []
    pending: str | None = None
    summary: list[tuple[str, str]] = []
    in_summary = False
    failed_project: str | None = None
    for raw in text.splitlines():
        level, body = _split_level(strip_ansi(raw))
        b = body.strip()
        if level == "INFO":
            m = _HEADER_RE.match(b)
            if m:
                pending = m.group("a")
                continue
            m = _BUILDING_RE.match(b)
            if m and not m.group("name").endswith(":") and not in_summary:
                name = m.group("name").strip()
                art = pending or name
                names[name] = art
                building.append(art)
                pending = None
                continue
            if b.startswith("Reactor Summary"):
                in_summary = True
                continue
            if in_summary:
                if b.startswith(("BUILD ", "-----")):
                    if summary:
                        in_summary = False
                    continue
                m = _REACTOR_LINE_RE.match(b)
                if m:
                    summary.append((m.group("name").strip(), m.group("status")))
        elif level in ("ERROR", "FATAL") and failed_project is None:
            m = _FAILED_GOAL_RE.match(b)
            if m:
                failed_project = m.group("proj")
    if summary:
        built = [names.get(n, n) for n, s in summary if s == "SUCCESS"]
        failed = next((names.get(n, n) for n, s in summary if s == "FAILURE"), None)
        return built, failed
    ok = "BUILD SUCCESS" in text
    if ok:
        return building, None
    failed = failed_project or (building[-1] if building else None)
    return [b for b in building if b != failed], failed
