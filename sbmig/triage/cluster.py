"""오류 묶기 — 한 빌드의 오류들을 케이스(L2 서명) 단위 클러스터로 묶고, 근본 원인이 먼저 오게 정렬한다.
흐름상 위치: 수정 루프가 빌드마다 ``Triager.cluster`` → ``cluster_cases`` 를 부른다. 오류 목록·KB
케이스·채점이 같은 키를 보도록, 루프와 오케스트레이터도 빌드 직후 ``refine_signatures`` 를 먼저 적용한다. 핵심
진입점: ``cluster_cases``(v2 케이스 클러스터, id ``case:<case_id>[@scope]``), ``case_id_of`` /
``l2_case_id``(케이스 ID = sha1(L2)[:10] — KB 폴더 ``cases/<case_id>/`` 의 이름),
``refine_signature``(뭉뚱그린 컴파일 L2 를 오류 원문 줄로 세분화), ``cause_rank`` / ``CAUSE_RANK``(근본 원인
순서). 왜 세분화하나: ``build`` / ``other`` 같은 포괄 L2 로 두면 무관한 실패가 한 케이스가 되어 저장된
스크립트가 엉뚱한 문제에 재사용된다. 왜 근본 원인 먼저인가: jakarta.xml.bind-api 하나가 빠지면 파일 2개에
케이스 10개가 생긴다(2026-10-26) — 원인 케이스의 스크립트를 먼저 받고 결과 케이스들은 재빌드를 기다리게 한다.

Error clustering (ARCHITECTURE §4).

v2 (flow-v2, docs/INTERFACES.md "Script fix loop (L)"): the fix loop works on **cases** — `cluster_cases` groups the
errors of a build by their L2 signature (`signature_generic`) ACROSS files, so one fix script fixes the same cause in
every file (v1 asked the LLM once per file). The case id is ``sha1(signature_generic)[:10]``
(v2.1: the error list's `ErrorEntry.case_id` and ``<kb>/cases/<case_id>/``); an error without an L2
gets ``sha1(kind|file|L1)[:10]``. A case cluster has ``id = "case:<case_id>"``, ``case_id``, ``file = None`` and the
common module (None when the case spans modules).

**Refined L2 for catch-all compile signatures** (`refine_signature`, applied to every error before it is grouped —
and by the fix loop / orchestrator right after each build, so the error list, the KB cases, the scoring and
the clusters all see the same key): a COMPILE / TEST_COMPILE L2 is a *catch-all* when its error class (2nd segment)
is ``build`` (a file-less ``Compilation failure`` / ``Fatal error compiling`` summary — what Maven prints when javac
wrote non-standard output such as ``option --boot-class-path cannot be used together with --release`` or
``warnings found and -Werror specified``) or ``other`` (no javac class matched), or when its 3rd segment (the key) is
empty (``compile|cannot_find_symbol`` without a symbol). Such an L2 would give unrelated failures one case id — and
a stored script would be reused for the wrong problem — so it becomes ``<kind>|<class>|<line>``, ``<line>`` = the
first meaningful line of the error's ``raw`` block: Maven level prefixes, the ``Failed to execute goal … on project
x:`` wrapper and the ``Compilation failure`` / ``COMPILATION ERROR`` / ``Fatal error compiling`` summary phrases
removed; Maven / javac noise lines (``-> [Help 1]``, ``Usage: javac …``, ``use --help …``, ``N errors``, re-run hints)
and a bare javac class phrase (``cannot find symbol``) skipped; the line normalized like an L2
(`sbmig.build.signatures.normalize_message`: urls / paths / positions / hex / versions / numbers masked, project
types → ``*``), lowercased, a leading ``<path>:`` / ``error:`` dropped, ``|`` → ``/``, at most 120 characters. An L1
that said no more than the coarse L2 (L1 == L2, the file-less summaries) gets the refined value too, so scoring by
(file, L1) tells two such failures apart. No meaningful line → the L2 stays as it was. Refining is idempotent (the
key is computed from ``raw``, never from the current L2). Every other L2 keeps `case_id_for` as is.

v1 clusters (`cluster_errors`, still used by callers outside the fix loop) are keyed by
* ``(kind, file)``          for COMPILE / TEST_COMPILE (falls back to the module when the file is unknown)
* ``(kind, artifact.key)``  for DEPENDENCY / PLUGIN / PARENT resolution and POM_INVALID with an artifact
* ``(kind, module)``        otherwise

Priorities (both, lower = handled first): PARENT_RESOLUTION 10, POM_INVALID 15, DEPENDENCY_RESOLUTION 20,
PLUGIN_RESOLUTION 25, ENFORCER 30, REWRITE 35, COMPILE 50 + reactor index, TEST_COMPILE 80 + reactor index,
PLUGIN_EXECUTION 90, TEST_FAILURE 95, UNKNOWN 99. Unknown modules get +40; a case takes the lowest priority of its
errors (its earliest reactor module). v1 ties: error count desc, then id.

v2 cases of the same priority are ordered **root causes first** by their javac error class (`CAUSE_RANK`, below):
a missing package / class file before the symbols it hides, an import before its body usages, the symbols before
the type mismatches and overrides they cause (2026-10-26 probe: one missing `jakarta.xml.bind-api` gave 10 cases in
2 files; the fix loop defers the consequences — `BuildFixLoop._deferral` — so the cause's script is asked for first).
Then error count (desc), then id.


"""
from __future__ import annotations

import hashlib
import posixpath
import re
from typing import Iterable, Optional

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind, ProjectProfile

BASE_PRIORITY: dict[ErrorKind, int] = {
    ErrorKind.PARENT_RESOLUTION: 10,
    ErrorKind.POM_INVALID: 15,
    ErrorKind.DEPENDENCY_RESOLUTION: 20,
    ErrorKind.PLUGIN_RESOLUTION: 25,
    ErrorKind.ENFORCER: 30,
    ErrorKind.REWRITE: 35,
    ErrorKind.COMPILE: 50,
    ErrorKind.TEST_COMPILE: 80,
    ErrorKind.PLUGIN_EXECUTION: 90,
    ErrorKind.TEST_FAILURE: 95,
    ErrorKind.UNKNOWN: 99,
}
UNKNOWN_MODULE_OFFSET = 40

ARTIFACT_KINDS = frozenset({ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION,
                            ErrorKind.PARENT_RESOLUTION})
FILE_KINDS = frozenset({ErrorKind.COMPILE, ErrorKind.TEST_COMPILE})
# resolution-type clusters mask compile clusters of the same module (they usually vanish once the pom is fixed)
RESOLUTION_KINDS = frozenset({ErrorKind.PARENT_RESOLUTION, ErrorKind.POM_INVALID,
                              ErrorKind.DEPENDENCY_RESOLUTION, ErrorKind.PLUGIN_RESOLUTION})


def cluster_key(e: BuildError) -> str:
    """v1 클러스터 키: 컴파일 오류는 파일(없으면 모듈), 해결형 오류는 아티팩트, 그 밖은 모듈."""
    if e.kind in FILE_KINDS:
        return e.file or f"module:{e.module or '-'}"
    if e.kind in ARTIFACT_KINDS or (e.kind == ErrorKind.POM_INVALID and e.artifact is not None):
        if e.artifact is not None:
            return e.artifact.key
    return e.module or e.file or "-"


def cluster_id(kind: ErrorKind, key: str) -> str:
    """v1 클러스터 id ``<kind>:<sha1(key)[:8]>``."""
    return f"{kind.value}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"


def module_index(profile: Optional[ProjectProfile], module: Optional[str], file: Optional[str] = None) -> Optional[int]:
    """모듈의 reactor 순번(``profile.poms`` 안의 위치 — 분석기가 reactor 순서를 지킨다). 모듈 이름으로 못
    찾으면 파일을 품은 가장 깊은 pom 디렉토리로 찾는다. 앞 모듈의 컴파일 오류를 먼저 고치려는 것.

    Reactor index of a module: position in `profile.poms` (analyzer keeps reactor order)."""
    if profile is None or not profile.poms:
        return None
    if module:
        for i, p in enumerate(profile.poms):
            d = posixpath.dirname(p.path)
            if module in (p.artifact_id, d, posixpath.basename(d)):
                return i
    if file:
        best: Optional[tuple[int, int]] = None      # (dir length, index): deepest pom dir containing the file
        for i, p in enumerate(profile.poms):
            d = posixpath.dirname(p.path)
            if d == "" or file == d or file.startswith(d + "/"):
                if best is None or len(d) > best[0]:
                    best = (len(d), i)
        if best is not None:
            return best[1]
    return None


def priority_for(kind: ErrorKind, profile: Optional[ProjectProfile], module: Optional[str],
                 file: Optional[str]) -> int:
    """우선순위(낮을수록 먼저): 종류별 기본값(parent → pom → 의존성 → 플러그인 … → 컴파일), 컴파일 계열은
    + reactor 순번(모르는 모듈은 +40). parent / 의존성이 풀려야 컴파일 오류가 의미가 있기 때문이다."""
    base = BASE_PRIORITY.get(kind, 99)
    if kind in FILE_KINDS:
        idx = module_index(profile, module, file)
        return base + (idx if idx is not None else UNKNOWN_MODULE_OFFSET)
    return base


def cluster_errors(errors: list[BuildError], profile: Optional[ProjectProfile] = None) -> list[ErrorCluster]:
    """v1 클러스터(파일 / 아티팩트 / 모듈 단위). 수정 루프는 쓰지 않고 루프 밖 호출자만 쓴다."""
    groups: dict[tuple[ErrorKind, str], list[BuildError]] = {}
    for e in errors:
        groups.setdefault((e.kind, cluster_key(e)), []).append(e)
    out: list[ErrorCluster] = []
    for (kind, key), errs in groups.items():
        first = errs[0]
        sigs: list[str] = []
        for e in errs:
            s = e.signature or f"{e.kind.value}|{e.message}"
            if s not in sigs:
                sigs.append(s)
        file = first.file if kind in FILE_KINDS else (first.file if all(x.file == first.file for x in errs) else None)
        module = next((e.module for e in errs if e.module), None)
        out.append(ErrorCluster(id=cluster_id(kind, key), kind=kind, file=file, module=module, errors=errs,
                                signatures=sigs, priority=priority_for(kind, profile, module, first.file)))
    out.sort(key=lambda c: (c.priority, -len(c.errors), c.id))
    return out


def cluster_label(c: ErrorCluster) -> str:
    """로그용 짧은 이름: v1 은 ``compile <파일> (3)``, 케이스는
    ``compile case <id> <L2 뒷부분> (N in M file(s))``.

    Short human label: 'compile src/main/java/Foo.java (3)'; a case: 'compile case abc1234567
    cannot_find_symbol|clientIp (7 in 4 files)'."""
    if c.case_id:
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        sig = sig.split("|", 1)[1] if "|" in sig else sig
        return (f"{c.kind.value} case {c.case_id}{' ' + sig[:80] if sig else ''} "
                f"({len(c.errors)} in {len(case_files(c))} file(s))")
    where = c.file or (c.errors[0].artifact.key if c.errors and c.errors[0].artifact else None) or c.module or "-"
    return f"{c.kind.value} {where} ({len(c.errors)})"


# ---------------------------------------------------------------------------------------------------------------------
# v2: cases (one L2 signature across files)
# ---------------------------------------------------------------------------------------------------------------------
CASE_PREFIX = "case:"


def _posix(path: Optional[str]) -> str:
    # 회사 PC 는 Windows — 역슬래시 경로도 같은 케이스 키가 나오도록 posix 로 맞춘다
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def l2_case_id(signature_generic: Optional[str]) -> str:
    """L2 서명의 케이스 ID ``sha1(signature_generic)[:10]`` (서명이 없으면 ""). ``ErrorEntry.case_id`` 와
    KB 폴더 ``<kb>/cases/<case_id>/`` 의 계약이다 — 바꾸면 쌓아 둔 KB 와 매칭이 끊긴다.

    The case id of an L2 signature: ``sha1(signature_generic)[:10]`` ("" without one) — the contract of
    `ErrorEntry.case_id` and ``<kb>/cases/<case_id>/`` (v2.1, docs/v2/PLAN-v2.1.md §3.3)."""
    if not signature_generic:
        return ""
    return hashlib.sha1(signature_generic.encode("utf-8")).hexdigest()[:10]


def case_id_of(e: BuildError) -> str:
    """오류 하나의 케이스 ID: L2 가 있으면 그 ID, 없으면 ``sha1(kind|file|L1)[:10]``(파일은 posix 경로).

    The case of one error: its L2 id, or — no L2 — ``sha1(kind|file|L1)[:10]`` (file = posix path, '' for none;
    L1 = the signature, else ``kind|message``)."""
    cid = l2_case_id(e.signature_generic)
    if cid:
        return cid
    key = "|".join((e.kind.value, _posix(e.file), e.signature or f"{e.kind.value}|{e.message}"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def case_files(c: ErrorCluster) -> list[str]:
    """클러스터 오류들의 파일 목록(중복 없이, posix, 오류 순서). 같은 라운드 미루기 판단에도 쓰인다.

    The distinct files of a cluster's errors (posix, in error order)."""
    return list(dict.fromkeys(_posix(e.file) for e in c.errors if e.file))


# 근본 원인 순서(낮을수록 먼저): 없는 패키지 / 클래스 → import 줄 → 심볼 → 타입 불일치 → override. 앞쪽이
# 고쳐지면 뒤쪽은 대개 저절로 사라지므로, 뒤쪽 케이스에 LLM 호출을 쓰지 않게 한다
# v2 root-cause order of compile cases (lower = earlier; the error class is the 2nd segment of the L2 signature,
# `sbmig.build.signatures.classify_javac` for an error without one). A `cannot_find_symbol` whose location is a
# package is an import line (`import a.b.Gone;`): it goes before the body usages of the same symbol.
CAUSE_RANK: dict[str, int] = {
    "package_does_not_exist": 0,
    "cannot_access": 1, "class_not_found_in_package": 1,
    "cannot_find_symbol@import": 2, "static_import_only": 2,
    "cannot_find_symbol": 3,
    "incompatible_types": 4, "cannot_be_applied": 4, "no_suitable_method": 4,
    "method_does_not_override": 5, "does_not_override_abstract": 5, "not_abstract_and_does_not_override": 5,
}
OTHER_CAUSE_RANK = 6                   # everything else (illegal_start, has_private_access, deprecated_removed, …)


def error_class(e: BuildError) -> str:
    """컴파일 오류의 javac 오류 종류(L2 의 두 번째 칸, 없으면 메시지로 분류). 위치가 ``package …`` 인
    ``cannot_find_symbol`` 은 import 줄이라
    ``cannot_find_symbol@import`` 로 따로 센다. 컴파일 계열이 아니면 "".

    The javac error class of a compile error (`cannot_find_symbol`, …; `cannot_find_symbol@import` for an import
    line); "" for other kinds."""
    if e.kind not in FILE_KINDS:
        return ""
    parts = (e.signature_generic or "").split("|")
    cls = parts[1] if len(parts) > 1 and parts[0] == e.kind.value else ""
    if not cls:
        try:
            from sbmig.build.signatures import classify_javac
            cls = classify_javac(e.message or "")
        except Exception:  # pragma: no cover - the build package is ours
            cls = ""
    if cls == "cannot_find_symbol" and (e.location or "").strip().startswith("package "):
        return "cannot_find_symbol@import"
    return cls


def cause_rank(c: ErrorCluster) -> int:
    """케이스의 근본 원인 순위(가장 원인에 가까운 오류 기준).
    컴파일 계열이 아니면 0 — 기본 우선순위가 이미 앞에 둔다.

    Root-cause rank of a case (`CAUSE_RANK`; its most causal error). 0 for non-compile kinds — their priority
    (`BASE_PRIORITY`) already puts them first."""
    if c.kind not in FILE_KINDS:
        return 0
    return min((CAUSE_RANK.get(error_class(e), OTHER_CAUSE_RANK) for e in c.errors), default=OTHER_CAUSE_RANK)


# ---------------------------------------------------------------------------------------------------------------------
# v2: refined L2 for catch-all compile signatures (module docstring)
# ---------------------------------------------------------------------------------------------------------------------
COARSE_CLASSES = frozenset({"build", "other"})
REFINED_MAX_CHARS = 120
_LEVEL_RE = re.compile(r"^\s*(?:\[(?:ERROR|WARNING|WARN|INFO|DEBUG|FATAL)\]\s*)+", re.IGNORECASE)
_WRAPPER_RE = re.compile(r"^failed to execute goal\b.*?\bon project\s+[^\s:]+\s*:?\s*", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"^(?:(?:compilation failure|compilation error|fatal error compiling)\s*:?\s*)+", re.IGNORECASE)
_NOISE_RE = re.compile(
    r"(?i)^(?:-> \[help \d+\].*|\[help \d+\].*|usage: javac\b.*|use --help for a list of possible options\.?|"
    r"to see the full stack trace\b.*|re-run maven using\b.*|for more information about the errors\b.*|"
    r"after correcting the problems\b.*|mvn <args>.*|[#\d]+ (?:errors?|warnings?)|build failure|total time\b.*|"
    r"finished at\b.*|-+)$")
_LEAD_RE = re.compile(r"^(?:<path>\s*:?\s*|(?:error|warning)\s*:\s*)+")
_BARE_JAVAC = frozenset({"cannot find symbol", "method does not override or implement a method from a supertype",
                         "static import only from classes and interfaces", "incompatible types"})


def is_coarse_l2(e: BuildError) -> bool:
    """뭉뚱그린 컴파일 L2 인가(종류가 ``build`` / ``other`` 이거나 세 번째 칸이 비었음). 모듈 docstring 참고.

    A catch-all compile L2 (class ``build`` / ``other``, or no key segment) — see the module docstring."""
    if e.kind not in FILE_KINDS or not e.signature_generic:
        return False
    parts = e.signature_generic.split("|")
    if parts[0] != e.kind.value:
        return False
    return len(parts) < 3 or not parts[2].strip() or parts[1] in COARSE_CLASSES


def _normalize_line(text: str, module: Optional[str]) -> str:
    """세분화 키 줄을 L2 처럼 정규화(URL·경로·위치·hex·버전·숫자 가림, 프로젝트 타입 → ``*``, 소문자)."""
    try:
        from sbmig.build.signatures import _mask_project_types, normalize_message
    except Exception:  # pragma: no cover - the build package is ours
        return " ".join(text.split()).lower()
    return _mask_project_types(normalize_message(text, module=module, lower=False, max_chars=400)).lower()


def refined_key_line(e: BuildError) -> str:
    """``e.raw`` 에서 처음 나오는 의미 있는 줄(Maven 머리말·요약
    문구·잡음 줄·맨 javac 문구 제외)을 정규화해 돌려준다. 없으면 "".

    The first meaningful, non-generic line of `e.raw`, normalized (module docstring); "" when there is none."""
    for line in (e.raw or "").splitlines():
        text = _SUMMARY_RE.sub("", _WRAPPER_RE.sub("", _LEVEL_RE.sub("", line).strip())).strip()
        if not text or _NOISE_RE.match(text):
            continue
        norm = _LEAD_RE.sub("", _normalize_line(text, e.module)).strip()
        norm = _LEAD_RE.sub("", _SUMMARY_RE.sub("", norm)).strip(" :;,.-")
        if not norm or norm in _BARE_JAVAC or _NOISE_RE.match(norm):
            continue
        return norm.replace("|", "/")[:REFINED_MAX_CHARS].rstrip()
    return ""


def refine_signature(e: BuildError) -> BuildError:
    """뭉뚱그린 컴파일 L2 에 세분화 키를 준다(제자리 수정). 멱등 — 키를 현재 L2 가 아니라 ``raw`` 에서
    계산한다. L1 이 L2 와 같았으면(파일 없는 요약) L1 도 바꿔서 채점이 두 실패를 구분하게 한다.

    Give a catch-all compile L2 its refined key (in place; the module docstring has the rule). Idempotent."""
    if not is_coarse_l2(e):
        return e
    line = refined_key_line(e)
    if not line:
        return e
    parts = e.signature_generic.split("|")
    refined = "|".join((e.kind.value, parts[1] if len(parts) > 1 and parts[1] else "other", line))
    if refined != e.signature_generic:
        if not e.signature or e.signature == e.signature_generic:
            e.signature = refined                   # an L1 that said no more than the coarse L2
        e.signature_generic = refined
    return e


def refine_signatures(errors: Iterable[BuildError]) -> list[BuildError]:
    """모든 오류에 ``refine_signature`` (제자리), 리스트로 돌려준다.

    `refine_signature` for every error (in place); returns them as a list."""
    out = list(errors or [])
    for e in out:
        refine_signature(e)
    return out


def cluster_cases(errors: list[BuildError], profile: Optional[ProjectProfile] = None,
                  scope: Optional[str] = None) -> list[ErrorCluster]:
    """v2 클러스터: 파일을 가로질러 케이스(L2) 하나당 하나 — 스크립트 하나가 모든 파일의 같은 원인을 고친다
    (v1 은 파일마다 LLM 을 불렀다). MSA 서비스별 루프면 id 뒤에 ``@<scope>`` 를 붙여 두 서비스의 시도가 서로의
    시도 횟수에 섞이지 않게 하고, ``case_id`` 는 그대로
    둔다(KB 키). 정렬: 우선순위 → 근본 원인 → 오류 수 → id.

    v2 clusters: one per case (L2) across files. `scope` (a targeted loop's build target, MSA services) is appended
    to the id (``case:<id>@<scope>``) so two services' attempts on the same case never count against each other —
    `case_id` stays the plain case id (the script store key). Catch-all compile L2s are refined first
    (`refine_signature`)."""
    groups: dict[str, list[BuildError]] = {}
    for e in refine_signatures(errors):
        groups.setdefault(case_id_of(e), []).append(e)
    suffix = f"@{_posix(scope).strip('/')}" if scope and _posix(scope).strip("/") else ""
    out: list[ErrorCluster] = []
    for cid, errs in groups.items():
        first = errs[0]
        sigs: list[str] = []
        for e in errs:
            s = e.signature or f"{e.kind.value}|{e.message}"
            if s not in sigs:
                sigs.append(s)
        modules = list(dict.fromkeys(e.module for e in errs if e.module))
        module = modules[0] if len(modules) == 1 and all(e.module for e in errs) else None
        prio = min(priority_for(e.kind, profile, e.module, e.file) for e in errs)
        out.append(ErrorCluster(id=f"{CASE_PREFIX}{cid}{suffix}", kind=first.kind, file=None, module=module,
                                errors=errs, signatures=sigs, priority=prio, case_id=cid))
    out.sort(key=lambda c: (c.priority, cause_rank(c), -len(c.errors), c.id))     # root causes first
    return out
