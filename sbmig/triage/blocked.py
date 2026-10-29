"""차단 판정 — 이 빌드 오류가 프로젝트 안에서는 고칠 수 없는 것인지 분류한다(ERROR-HANDLING §1, X1–X6).
흐름상 위치: ``DecisionEngine.decide`` 의 2단계에서 ``classify_blocked`` 를 부른다. 분류만 하고, 그 이유에 LLM
시도를 몇 번 줄지는 ``cfg.loop.blocked_policy`` 가 정한다. 차단된 케이스는 인수인계의 Blocked 로 간다. X1
의존성 API 가 아직 javax 타입 / X2 의존성 API 변경 / X3·X4 아티팩트·parent 가 저장소에 없음 / X5 JDK 21 에서
깨지는 플러그인 / X6 Java 가 아닌 소스(Kotlin·Groovy·Scala, 코드 생성 플러그인) / v2.1k reactor 중복(같은 프로젝트가
reactor 에 두 번 — pom 구조 문제라 스크립트로 못 고침, 접수가 먼저 지우고 남은 것만 여기 온다). 일부러 보수적이다: 애매하면
차단하지 않는다(스크립트에 기회를 준다). javax 패키지 없음 / javax 심볼 없음은 jakarta 레시피나 스크립트
몫이고, 프로젝트가 아직 그 javax 를 import 하거나 소유 클래스가 프로젝트 것이면 X1 이 아니다. 주의: 이것은
"빌드 오류" 의 차단이다. EOL 라이브러리 권고에는 차단 정책이 없다(권고는 안내만, CLAUDE.md).

Blocked-cluster classification (docs/ERROR-HANDLING.md §1, X1–X6).

A cluster is *blocked* when the fix is not inside this project: a dependency still exposes ``javax.*`` types in its
API (X1), a dependency API changed (X2), an artifact / parent is not in any repository (X3/X4), a build plugin is
broken on JDK 21 (X5), the sources are not Java (X6) or a project is reached twice through the reactor's `<module>`
lists (v2.1k, ``reactor_duplicate``: the intake removes such listings; what still reaches the build is a pom structure
problem no script fixes). ``classify_blocked`` only *classifies*; how many LLM attempts
a reason gets before the cluster is blocked is ``cfg.loop.blocked_policy`` (see ``decide.py``).

The rules are deliberately conservative: when in doubt the cluster is **not** blocked (the recipes / fix scripts get
their chance). In particular a missing ``javax.*`` package or a ``cannot find symbol`` of a ``javax`` class is the
jakarta recipe's (or a fix script's) job, never a blocker, and an X1 match is discarded when the project itself still imports the javax
package (the migration did not finish → fixable here) or the owner class is a project class.


"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sbmig.core.models import BlockReason, BuildError, ErrorCluster, ErrorKind, ProjectProfile

# javax EE namespaces that moved to jakarta (javax.annotation.processing etc. are JDK and never match these types)
EE_PACKAGES = ("servlet", "persistence", "validation", "annotation", "inject", "transaction", r"ws\.rs", "jms",
               "mail", "websocket", "el", "faces", r"xml\.bind")
_EE = "(?:" + "|".join(EE_PACKAGES) + ")"
_TYPE = r"[\w$.]+(?:<[^<>]*(?:<[^<>]*>[^<>]*)*>)?"          # FQCN with (one level of nested) generics
_EE_TYPE_RE = re.compile(rf"\b(?P<ns>javax|jakarta)\.(?P<pkg>{_EE})\.[\w$.]*[A-Z][\w$]*")
_CONVERT_RE = re.compile(rf"(?P<found>\b(?:javax|jakarta)\.{_EE}\.[\w$.]+)\s+cannot be converted to\s+"
                         rf"(?P<required>\b(?:javax|jakarta)\.{_EE}\.[\w$.]+)")
_CANNOT_ACCESS_RE = re.compile(rf"cannot access\s+(?P<t>(?:javax|jakarta)\.{_EE}\.[\w$.]+)[\s\S]*?class file for\s+"
                               rf"(?P=t)\s+not found")
_REQUIRED_RE = re.compile(r"^\s*(?:\[ERROR\]\s*)?required\s*:\s*(?P<v>.+?)\s*$", re.M)
_FOUND_RE = re.compile(r"^\s*(?:\[ERROR\]\s*)?found\s*:\s*(?P<v>.+?)\s*$", re.M)

# owner extraction ------------------------------------------------------------------------------------------------
_APPLIED_RE = re.compile(rf"\b(?:constructor|method)\s+[\w$<>]+\s+in\s+(?:class|interface|enum|record)\s+"
                         rf"(?P<owner>{_TYPE})\s+cannot be applied")
_OVERRIDE_RE = re.compile(rf"is not abstract and does not override abstract method\s+(?P<m>[\w$]+)\((?P<args>[^)]*)\)"
                          rf"\s+in\s+(?P<owner>{_TYPE})")
_NO_SUITABLE_RE = re.compile(r"no suitable (?P<what>method|constructor) found for\s+(?P<name>[\w$]+)\((?P<args>[^)]*)\)")
_CANDIDATE_RE = re.compile(rf"^\s*(?:\[ERROR\]\s*)?(?:constructor|method)\s+(?P<member>{_TYPE})\((?P<args>[^)]*)\)"
                           r"\s+is not applicable", re.M)
_LOCATION_TYPE_RE = re.compile(rf"^(?:class|interface|enum|record|@interface)\s+(?P<owner>{_TYPE})\s*$")
_LOCATION_VAR_RE = re.compile(rf"\bof type\s+(?P<owner>{_TYPE})\s*$")
_LOCATION_RAW_RE = re.compile(rf"^\s*(?:\[ERROR\]\s*)?location\s*:\s*(?P<v>.+?)\s*$", re.M)

# infra ---------------------------------------------------------------------------------------------------------------
_NOT_FOUND_RE = re.compile(r"(?i)could not find artifact|could not be resolved|was not found in|"
                           r"failure to find|could not resolve dependencies")
_REPO_RE = re.compile(r"\bin\s+(?P<id>[\w.\-]+)\s+\((?P<url>[a-z]+://[^)\s]+)\)")
_URL_RE = re.compile(r"\b(?:https?|file)://[^\s)'\"]+")
_PLUGIN_BROKEN_RE = re.compile(r"Unsupported class file major version|NoSuchMethodError|"
                               r"IllegalAccessError[\s\S]{0,400}?com\.sun\.tools\.javac|"
                               r"java\.lang\.reflect\.InaccessibleObjectException|InaccessibleObjectException|"
                               r"due to an API incompatibility")
_PLUGIN_GOAL_RE = re.compile(r"\bgoal\s+(?P<g>[\w.\-]+):(?P<a>[\w.\-]+):(?P<v>[\w.\-]+):[\w\-]+")
GENERATOR_PLUGINS = ("protobuf-maven-plugin", "jaxb2-maven-plugin", "maven-jaxb2-plugin", "openapi-generator-maven-plugin",
                     "kotlin-maven-plugin", "gmavenplus-plugin", "groovy-maven-plugin", "scala-maven-plugin")
_FOREIGN_SOURCE_RE = re.compile(r"\.(?:kt|kts|groovy|scala)\b")
# v2.1k: Maven 3 "Project 'g:a:v' is duplicated in the reactor" / older wording "The projects in the reactor contain a
# duplicate …" (DuplicateProjectException) — the whole reactor is refused before anything compiles
_REACTOR_DUP_RE = re.compile(r"is duplicated in the reactor|The projects in the reactor contain a duplicate",
                             re.IGNORECASE)
_REACTOR_DUP_COORD_RE = re.compile(r"Project '(?P<c>[^'\s]+)' is duplicated in the reactor|"
                                   r"duplicate (?:project|module)[^']*'(?P<c2>[^'\s]+)'", re.IGNORECASE)

# 마이그레이션 "대상" API(JDK, javax/jakarta, Spring, Hibernate, Jackson …): 이들의 API 변경은 레시피·LLM 이
# 고칠 일이지 외부 라이브러리 탓(X2)이 아니다
# API owners that are migration *targets*: their API changes are what recipes/LLM are for (never X2)
MIGRATION_TARGET_PREFIXES = (
    "java.", "javax.", "jakarta.", "jdk.", "sun.", "com.sun.", "org.springframework.", "org.hibernate.",
    "com.fasterxml.jackson.", "tools.jackson.", "org.junit.", "junit.", "org.mockito.", "org.assertj.", "org.hamcrest.",
    "org.apache.tomcat.", "org.apache.catalina.", "io.micrometer.", "reactor.", "lombok.", "org.mapstruct.",
    "com.querydsl.", "org.thymeleaf.", "org.flywaydb.", "org.liquibase.", "org.slf4j.", "ch.qos.logback.",
    "org.apache.logging.log4j.", "io.swagger.", "org.springdoc.", "org.aspectj.", "org.testcontainers.",
)


@dataclass
class BlockedCandidate:
    """차단 판정 결과: 이유, 필요 / 실제 타입, 소유 클래스, 근거, 이 판정이 덮는 오류(클러스터의 일부일 수
    있음), 저장소 URL(X3/X4), 플러그인(X5/X6), 사람이 고정한 케이스면 그 ID 와 제안 문장."""
    reason: BlockReason
    required_type: Optional[str] = None
    found_type: Optional[str] = None
    owner_class: Optional[str] = None
    evidence: list[str] = field(default_factory=list)
    errors: list[BuildError] = field(default_factory=list)     # the cluster errors this classification covers
    repositories: list[str] = field(default_factory=list)       # X3/X4: repository URLs named in the error
    plugin: Optional[str] = None                                # X5/X6: plugin g:a:v
    # round 10: a case pinned to `block` by a person (`sbmig kb cases pin --action block[:reason]`): its id and the
    # suggestion text the BlockedItem carries instead of the composed one
    case_id: str = ""
    suggestion: str = ""

    def detail(self) -> str:
        """결정 근거에 쓰는 한 줄 "왜" (``blocked: <이유> — <설명>`` 의 설명 부분).

        One-line 'why' used in decision rationales (`blocked: <reason> — <detail>`)."""
        r = self.reason
        if self.case_id:
            return self.evidence[0] if self.evidence else f"pinned by case {self.case_id}"
        if r == BlockReason.EXTERNAL_JAVAX_API:
            who = self.owner_class or "a dependency API"
            txt = f"{who} requires {self.required_type or 'javax.* types'}"
            return txt + (f" (project supplies {self.found_type})" if self.found_type else "")
        if r == BlockReason.EXTERNAL_API_INCOMPATIBLE:
            return f"library class {self.owner_class} does not offer the API used here" + \
                (f" ({self.evidence[0]})" if self.evidence else "")
        if r in (BlockReason.ARTIFACT_NOT_FOUND, BlockReason.PARENT_NOT_FOUND):
            art = next((e.artifact.coordinates for e in self.errors if e.artifact is not None), None)
            where = f" in {', '.join(self.repositories)}" if self.repositories else ""
            return f"{art or 'artifact'} not found{where}"
        if r == BlockReason.PLUGIN_INCOMPATIBLE:
            return f"build plugin {self.plugin or '?'} fails on JDK 21" + (f": {self.evidence[0]}" if self.evidence else "")
        if r == BlockReason.UNSUPPORTED_SOURCE:
            return self.evidence[0] if self.evidence else "non-Java / generated sources"
        if r == BlockReason.REACTOR_DUPLICATE:
            return self.evidence[0] if self.evidence else "a project is listed twice in the reactor"
        return self.evidence[0] if self.evidence else r.value


# ---------------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------------
def _strip_generics(t: str) -> str:
    """타입 이름에서 ``<…>`` 제네릭 부분을 뺀다(중첩 포함)."""
    out, depth = [], 0
    for ch in t:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip().rstrip(".,;")


def _text(e: BuildError) -> str:
    """판정에 쓰는 오류 전체 글: 메시지 + 원문 + symbol / location 줄."""
    return "\n".join(x for x in (e.message, e.raw, e.symbol and f"symbol: {e.symbol}",
                                 e.location and f"location: {e.location}") if x)


def _ee_types(text: str) -> list[re.Match]:
    return list(_EE_TYPE_RE.finditer(text or ""))


def _javax_pkg_of(t: Optional[str]) -> Optional[str]:
    """'javax.servlet.http.HttpServletRequest' → 'javax.servlet' (the namespace root that moved)."""
    if not t:
        return None
    m = _EE_TYPE_RE.search(t)
    return f"{m.group('ns')}.{m.group('pkg').replace(chr(92), '')}" if m else None


def _owner_from_member(member: str) -> str:
    """'com.acme.Wrapper.Wrapper' (constructor) / 'com.acme.Util.doIt' (method) → 'com.acme.Wrapper' / 'com.acme.Util'."""
    m = _strip_generics(member)
    return m.rsplit(".", 1)[0] if "." in m else m


def extract_owner(e: BuildError) -> Optional[str]:
    """javac 오류가 말하는 API 의 소유 클래스(``in class X``
    / ``location: class X`` / ``… in X``). 못 찾으면 None.

    Owner class of the API a javac error is about (``in class X`` / ``location: class X`` / ``… in X``)."""
    text = _text(e)
    m = _APPLIED_RE.search(text)
    if m:
        return _strip_generics(m.group("owner"))
    m = _OVERRIDE_RE.search(text)
    if m:
        return _strip_generics(m.group("owner"))
    if _NO_SUITABLE_RE.search(text):
        cands = [_owner_from_member(c.group("member")) for c in _CANDIDATE_RE.finditer(text)]
        if cands:
            return cands[0]
    loc = e.location
    if not loc:
        lm = _LOCATION_RAW_RE.search(e.raw or "")
        loc = lm.group("v") if lm else None
    if loc:
        lm2 = _LOCATION_TYPE_RE.match(loc.strip()) or _LOCATION_VAR_RE.search(loc.strip())
        if lm2:
            return _strip_generics(lm2.group("owner"))
    return None


_CACHE: dict[tuple, object] = {}


def _cache_key(ws, what: str) -> Optional[tuple]:
    """캐시 키 (루트, HEAD, 무엇). 루프 안에서 트리는 커밋으로만 바뀌므로 HEAD 가 같으면 소스 색인을 다시 만들
    필요가 없다. HEAD 를 못 읽으면 None(캐시 안 함).

    (root, HEAD, what) — the project tree only changes through commits inside the loop; None = do not cache."""
    try:
        return (str(ws.root), ws.head(), what)
    except Exception:
        return None


def _cached(ws, what: str, fn):
    """HEAD 기준 캐시(256개 넘으면 통째로 비움)."""
    key = _cache_key(ws, what)
    if key is None:
        return fn()
    if key not in _CACHE:
        if len(_CACHE) > 256:
            _CACHE.clear()
        _CACHE[key] = fn()
    return _CACHE[key]


def _source_index(ws) -> tuple[set[str], set[str]]:
    return _cached(ws, "source-index", lambda: _build_source_index(ws))  # type: ignore[return-value]


def _build_source_index(ws) -> tuple[set[str], set[str]]:
    """프로젝트 Java 소스의 (클래스 경로 ``com/acme/Foo`` 집합, 패키지 디렉토리 ``com/acme`` 집합).

    (class paths 'com/acme/Foo', package dirs 'com/acme') of the project's Java sources."""
    classes: set[str] = set()
    pkgs: set[str] = set()
    try:
        files = ws.java_files()
    except Exception:
        return classes, pkgs
    for f in files:
        p = f.replace("\\", "/")
        idx = p.rfind("/java/")
        rel = p[idx + 6:] if idx >= 0 else p
        rel = rel[:-5] if rel.endswith(".java") else rel
        classes.add(rel)
        pkgs.add(posixpath.dirname(rel))
    return classes, pkgs


def is_project_class(ws, profile: Optional[ProjectProfile], fqcn: Optional[str]) -> bool:
    """``fqcn``(또는 그 바깥 클래스)이 이 프로젝트 소스에 선언돼 있는가. 작업 공간이 있으면 소스 트리로,
    없으면 프로필의 패키지 목록 / base_package 로 판단한다.
    프로젝트 클래스면 고칠 수 있으므로 차단하지 않는다.

    True when `fqcn` (or its outer class) is declared by this project's sources.

    With a workspace the source tree decides (``…/java/com/acme/Foo.java``); without one we fall back to the profile:
    the class' package is one of ``profile.source.packages`` or under ``profile.source.base_package``."""
    if not fqcn:
        return False
    name = _strip_generics(fqcn)
    if "." not in name:
        return False
    parts = name.split(".")
    if ws is not None:
        classes, pkgs = _source_index(ws)
        if classes:
            # Outer.Inner → try com/acme/Outer/Inner, com/acme/Outer
            for i in range(len(parts), 1, -1):
                if "/".join(parts[:i]) in classes:
                    return True
            pkg = "/".join(p for p in parts if p[:1].islower())
            return bool(pkg) and pkg in pkgs and pkg.count("/") >= 1
    if profile is not None:
        pkg_parts = [p for p in parts if p[:1].islower()]
        pkg = ".".join(pkg_parts)
        if pkg and pkg in set(profile.source.packages or []):
            return True
        base = profile.source.base_package
        if base and (name == base or name.startswith(base + ".")):
            return True
    return False


def _is_own_file(e: BuildError, fqcn: str) -> bool:
    """소유 클래스가 바로 그 오류가 난 파일의 클래스인가(``…/com/acme/Foo.java`` ↔ ``com.acme.Foo[.Inner]``).

    The owner is the class declared by the very file the error is in (… /com/acme/Foo.java ↔ com.acme.Foo[.Inner])."""
    if not e.file:
        return False
    f = e.file.replace("\\", "/")
    parts = _strip_generics(fqcn).split(".")
    for i in range(len(parts), 1, -1):
        if f.endswith("/" + "/".join(parts[:i]) + ".java") or f == "/".join(parts[:i]) + ".java":
            return True
    return False


def project_imports_package(ws, package: str) -> bool:
    """프로젝트 Java 파일 중 아직 ``package``(예 ``javax.servlet``)를 import 하는 것이 있는가 — 있으면
    마이그레이션이 덜 끝난 것이라 프로젝트 안에서 고칠 수 있다.

    True if any project Java file still imports from `package` (e.g. 'javax.servlet')."""
    if ws is None or not package:
        return False
    return bool(_cached(ws, f"imports:{package}", lambda: _scan_imports(ws, package)))


def _scan_imports(ws, package: str) -> bool:
    needle = re.compile(rf"^\s*import\s+(?:static\s+)?{re.escape(package)}\.", re.M)
    try:
        files = ws.java_files()
    except Exception:
        return False
    for f in files:
        try:
            if needle.search(ws.read(f)):
                return True
        except Exception:
            continue
    return False


def _is_target_api(fqcn: str) -> bool:
    return fqcn.startswith(MIGRATION_TARGET_PREFIXES)


def repositories_in(text: str) -> list[str]:
    """오류 글에 나온 저장소들(``id (url)``, 없으면 맨 URL) —
    어느 Nexus 에서 못 찾았는지 Blocked 표에 보여 준다."""
    out: list[str] = []
    for m in _REPO_RE.finditer(text or ""):
        u = f"{m.group('id')} ({m.group('url')})"
        if u not in out:
            out.append(u)
    if not out:
        for m in _URL_RE.finditer(text or ""):
            if m.group(0) not in out:
                out.append(m.group(0))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# X1 — javax API in a dependency
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class _JavaxHit:
    required: Optional[str]
    found: Optional[str]
    owner: Optional[str]
    evidence: str


def _first_ee(v: str) -> Optional[str]:
    m = _EE_TYPE_RE.search(v or "")
    return m.group(0) if m else None


def _javax_hit(e: BuildError) -> Optional[_JavaxHit]:
    """X1 후보 찾기: (e) 라이브러리 시그니처의 javax 클래스 파일 없음, (a) javax ↔ jakarta 변환 불가, (b)
    required / found 줄, (c) 후보 메서드가 javax 를 받음, (d) 라이브러리 타입의 javax 추상 메서드 미구현."""
    if e.kind not in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE):
        return None
    text = _text(e)
    msg = e.message or ""
    owner = extract_owner(e)
    # (e) cannot access javax.X — "class file for javax.X not found": a *library* signature mentions a javax type that
    #     is no longer on the classpath (project sources would get "package … does not exist" instead)
    ca = _CANNOT_ACCESS_RE.search(text)
    if ca and ca.group("t").startswith("javax."):
        t = ca.group("t")
        return _JavaxHit(t, t.replace("javax.", "jakarta.", 1), owner, ca.group(0).splitlines()[0])
    # `cannot find symbol` / `package javax.x does not exist` are the jakarta recipe's job → never X1
    if re.search(r"cannot find symbol|does not exist|cannot access", msg) and not re.search(
            r"cannot be applied|cannot be converted", text):
        return None
    # (a) X cannot be converted to Y (incompatible types:, reason: argument mismatch; …)
    for m in _CONVERT_RE.finditer(text):
        found, required = m.group("found"), m.group("required")
        if found.split(".", 1)[0] != required.split(".", 1)[0]:
            return _JavaxHit(required, found, owner, m.group(0))
    # (b) required: … / found: … continuation lines
    req = _REQUIRED_RE.search(e.raw or "")
    fnd = _FOUND_RE.search(e.raw or "")
    if req and fnd:
        r_t, f_t = _first_ee(req.group("v")), _first_ee(fnd.group("v"))
        if r_t and f_t and r_t.split(".", 1)[0] != f_t.split(".", 1)[0]:
            return _JavaxHit(r_t, f_t, owner, f"required: {req.group('v')} / found: {fnd.group('v')}")
    # (c) no suitable method/constructor found for m(jakarta.…) — candidates take javax.…
    ns = _NO_SUITABLE_RE.search(text)
    if ns:
        args_types = _first_ee(ns.group("args"))
        for c in _CANDIDATE_RE.finditer(text):
            cand_t = _first_ee(c.group("args"))
            if cand_t and args_types and cand_t.split(".", 1)[0] != args_types.split(".", 1)[0]:
                return _JavaxHit(cand_t, args_types, _owner_from_member(c.group("member")), c.group(0).strip())
    # (d) does not override abstract method m(javax.…) in <library type>
    ov = _OVERRIDE_RE.search(text)
    if ov:
        t = _first_ee(ov.group("args"))
        if t and t.startswith("javax."):
            return _JavaxHit(t, t.replace("javax.", "jakarta.", 1), _strip_generics(ov.group("owner")), ov.group(0))
    return None


def _classify_javax(cluster: ErrorCluster, profile, ws) -> Optional[BlockedCandidate]:
    """X1 판정: 후보 중 프로젝트 소유 API 이거나 프로젝트가 아직 그 javax 패키지를 import 하는 것은 뺀다
    (프로젝트 안에서 고칠 수 있음). 남은 것이 있으면 EXTERNAL_JAVAX_API."""
    hits: list[tuple[BuildError, _JavaxHit]] = []
    for e in cluster.errors:
        h = _javax_hit(e)
        if h is None:
            continue
        if h.owner and (is_project_class(ws, profile, h.owner) or _is_own_file(e, h.owner)):
            continue                             # project-owned API → the project can be fixed
        req_ns = (h.required or "").split(".", 1)[0]
        javax_t = h.required if req_ns == "javax" else h.found
        pkg = _javax_pkg_of(javax_t)
        if req_ns == "jakarta":
            # project hands javax to a jakarta API: only blocked when the javax value cannot come from project code
            if ws is None or (pkg and project_imports_package(ws, pkg)):
                continue
        elif pkg and project_imports_package(ws, pkg):
            continue                             # the project itself still uses javax → migration not finished
        hits.append((e, h))
    if not hits:
        return None
    first = hits[0][1]
    owner = next((h.owner for _, h in hits if h.owner), None)
    return BlockedCandidate(reason=BlockReason.EXTERNAL_JAVAX_API, required_type=first.required,
                            found_type=first.found, owner_class=owner,
                            evidence=[h.evidence for _, h in hits][:5], errors=[e for e, _ in hits])


# ---------------------------------------------------------------------------------------------------------------------
# X2 — dependency API changed (non-javax)
# ---------------------------------------------------------------------------------------------------------------------
def _classify_external_api(cluster: ErrorCluster, profile, ws) -> Optional[BlockedCandidate]:
    """X2 판정: javax / jakarta 와 무관하고, 소유 클래스가 마이그레이션 대상 API 도 프로젝트 클래스도 아닌
    외부 라이브러리의 메서드 없음 / 인자 불일치. 없는 "타입" 은 API 변경이 아니므로 제외한다."""
    hits: list[tuple[BuildError, str, str]] = []
    for e in cluster.errors:
        if e.kind not in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE):
            continue
        text = _text(e)
        if _EE_TYPE_RE.search(text):
            continue                             # javax/jakarta related → X1 or the recipe's job
        owner: Optional[str] = None
        if "cannot find symbol" in (e.message or ""):
            sym = (e.symbol or "").strip()
            if not sym or sym.startswith(("class ", "interface ", "enum ", "record ", "package ")) or \
                    re.match(r"^[\w$]+$", sym):
                continue                         # a missing *type* is not an API change of the location class
            loc = e.location
            if not loc and e.raw:
                lm = _LOCATION_RAW_RE.search(e.raw)
                loc = lm.group("v") if lm else None
            if loc:
                m = _LOCATION_TYPE_RE.match(str(loc).strip()) or _LOCATION_VAR_RE.search(str(loc).strip())
                if m:
                    owner = _strip_generics(m.group("owner"))
        else:
            m = _APPLIED_RE.search(text)
            if m:
                owner = _strip_generics(m.group("owner"))
        if not owner or "." not in owner or _is_target_api(owner) or is_project_class(ws, profile, owner) \
                or _is_own_file(e, owner):
            continue
        hits.append((e, owner, f"{e.message}" + (f" {e.symbol}" if e.symbol else "")))
    if not hits:
        return None
    return BlockedCandidate(reason=BlockReason.EXTERNAL_API_INCOMPATIBLE, owner_class=hits[0][1],
                            evidence=[h[2] for h in hits][:5], errors=[h[0] for h in hits])


# ---------------------------------------------------------------------------------------------------------------------
# X3–X6
# ---------------------------------------------------------------------------------------------------------------------
def _classify_infra(cluster: ErrorCluster) -> Optional[BlockedCandidate]:
    """X3–X5 판정: parent 해결 실패(PARENT_NOT_FOUND), 아티팩트 없음(ARTIFACT_NOT_FOUND), 코드 생성 플러그인
    실패(UNSUPPORTED_SOURCE), JDK 21 에서 깨지는 플러그인(PLUGIN_INCOMPATIBLE)."""
    errs = cluster.errors
    if cluster.kind == ErrorKind.PARENT_RESOLUTION:
        text = "\n".join(_text(e) for e in errs)
        return BlockedCandidate(reason=BlockReason.PARENT_NOT_FOUND, evidence=[errs[0].message[:300]] if errs else [],
                                errors=list(errs), repositories=repositories_in(text))
    if cluster.kind in (ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION):
        hits = [e for e in errs if _NOT_FOUND_RE.search(_text(e))]
        if hits:
            text = "\n".join(_text(e) for e in hits)
            return BlockedCandidate(reason=BlockReason.ARTIFACT_NOT_FOUND, evidence=[hits[0].message[:300]],
                                    errors=hits, repositories=repositories_in(text))
        return None
    if cluster.kind == ErrorKind.PLUGIN_EXECUTION:
        gen = [e for e in errs if any(p in _text(e) for p in GENERATOR_PLUGINS)]
        if gen:
            plug = _plugin_of(gen[0])
            return BlockedCandidate(reason=BlockReason.UNSUPPORTED_SOURCE, plugin=plug,
                                    evidence=[f"code generator plugin {plug or '?'} failed: {gen[0].message[:200]}"],
                                    errors=gen)
        hits = [e for e in errs if _PLUGIN_BROKEN_RE.search(_text(e))]
        if hits:
            m = _PLUGIN_BROKEN_RE.search(_text(hits[0]))
            return BlockedCandidate(reason=BlockReason.PLUGIN_INCOMPATIBLE, plugin=_plugin_of(hits[0]),
                                    evidence=[m.group(0)[:200] if m else hits[0].message[:200]], errors=hits)
    return None


def reactor_duplicate_coordinates(text: str) -> str | None:
    """Maven 의 reactor 중복 메시지에서 중복된 프로젝트 좌표(``g:a:v``)를, 없으면 None.

    The coordinates Maven names in a "duplicated in the reactor" message, else None."""
    m = _REACTOR_DUP_COORD_RE.search(text or "")
    return (m.group("c") or m.group("c2")) if m else None


def _classify_reactor(cluster: ErrorCluster) -> BlockedCandidate | None:
    """v2.1k 판정: "Project 'g:a:v' is duplicated in the reactor" — 한 모듈이 reactor 에 두 번 닿아 Maven 이 reactor
    전체를 거부한다(종류와 상관없이 메시지로 본다: 파서는 이것을 unknown 으로 분류한다). pom 구조 문제라 스크립트로
    고치지 않는다: ``blocked_policy.reactor_duplicate`` 0 이면 LLM 없이 바로 인수인계로 간다. 접수의
    ``dedupe_reactor`` 가 지우지 못한 것(루트 pom 의 두 블록, 같은 좌표의 두 디렉토리, 프로필)만 여기 온다.

    v2.1k: a project reached twice through the reactor's `<module>` lists (message based — the parser files it as
    `unknown`); the evidence names the duplicated coordinates."""
    hits = [e for e in cluster.errors if _REACTOR_DUP_RE.search(_text(e))]
    if not hits:
        return None
    coords = reactor_duplicate_coordinates("\n".join(_text(e) for e in hits))
    what = f"project {coords}" if coords else "a project"
    return BlockedCandidate(reason=BlockReason.REACTOR_DUPLICATE, errors=hits,
                            evidence=[(f"{what} is duplicated in the reactor: it is reached through more than one "
                                       f"<module> listing (a pom structure problem — no code change fixes it)")])


def _plugin_of(e: BuildError) -> Optional[str]:
    """오류의 플러그인 좌표 g:a:v(아티팩트, 없으면 ``goal g:a:v:goal`` 에서)."""
    if e.artifact is not None:
        return e.artifact.coordinates
    m = _PLUGIN_GOAL_RE.search(_text(e))
    return f"{m.group('g')}:{m.group('a')}:{m.group('v')}" if m else None


def _classify_foreign_source(cluster: ErrorCluster) -> Optional[BlockedCandidate]:
    """X6 판정: Kotlin / Groovy / Scala 소스의 컴파일 오류(파일 확장자, ``e:`` 머리, 플러그인 goal)."""
    hits: list[BuildError] = []
    for e in cluster.errors:
        if e.kind not in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE):
            continue
        goal = e.goal or ""
        if (e.file and _FOREIGN_SOURCE_RE.search(e.file)) or re.match(r"(?:\[ERROR\]\s*)?e:\s", e.raw or "") \
                or goal.startswith(("kotlin:", "gplus:", "groovy:", "scala:")):
            hits.append(e)
    if not hits:
        return None
    lang = "Kotlin" if any((e.file or "").endswith((".kt", ".kts")) or (e.goal or "").startswith("kotlin:")
                           for e in hits) else "Groovy/Scala"
    return BlockedCandidate(reason=BlockReason.UNSUPPORTED_SOURCE,
                            evidence=[f"{lang} source does not compile ({hits[0].file or hits[0].module or '?'}): "
                                      f"{hits[0].message[:160]}"], errors=hits)


# ---------------------------------------------------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------------------------------------------------
def classify_blocked(cluster: ErrorCluster, profile: Optional[ProjectProfile], cfg=None, *,
                     ws=None) -> Optional[BlockedCandidate]:
    """클러스터(의 일부)가 프로젝트 안에서 고칠 수 없으면 BlockedCandidate, 아니면 None.
    ``candidate.errors`` 는 일부일 수 있다 — 루프는 그 오류만 차단하고 나머지는 계속 고친다. 순서:
    reactor 중복(v2.1k) → 인프라(X3–X5) → 외국어 소스(X6) → (컴파일이면) javax API(X1) → 외부 API(X2).

    Return a BlockedCandidate when (part of) the cluster cannot be fixed inside the project, else None.

    `candidate.errors` lists the cluster errors the classification covers — it may be a strict subset (e.g. one
    javax constructor mismatch in a file that has other, fixable errors); the loop blocks only those and keeps
    working on the rest."""
    if not cluster.errors:
        return None
    for fn in (_classify_reactor, _classify_infra, _classify_foreign_source):
        c = fn(cluster)
        if c is not None:
            return c
    if cluster.kind in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE):
        return _classify_javax(cluster, profile, ws) or _classify_external_api(cluster, profile, ws)
    return None


def errors_text(errors: Iterable[BuildError]) -> str:
    """오류들의 전체 글을 줄바꿈으로 이은 것."""
    return "\n".join(_text(e) for e in errors)
