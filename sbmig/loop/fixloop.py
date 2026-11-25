"""Maven 빌드 오류를 LLM이 작성한 수정 스크립트로 고치는 수정 루프 모듈. 검증 빌드가 실패하면 오케스트레이터가 호출한다.
한 라운드는 빌드 -> 직전 시도 결과 평가 -> 새 오류 기록 -> 케이스로 묶기 -> 케이스마다 수정 시도 -> 커밋 순서다.
- 케이스마다 저장된 스크립트 재사용 -> LLM 보강 -> LLM 신규 순서로 시도한다. 한도를 다 쓰면 그 케이스는 차단된다
- 시도 결과는 재빌드로만 평가한다. 대상 오류가 모두 사라지고 빌드가 그 단계까지 가야 개선으로 본다
- 개선되지 않은 시도는 같은 케이스의 다음 시도 전에 되돌린다
- 근본 원인 케이스부터 고치고, 이번 라운드에 이미 고친 파일이나 모듈 pom에 걸린 케이스는 다음 빌드로 미룬다
- 루프는 항상 빌드 뒤에 끝난다. 의존성 버전은 LLM이 추측하지 않고 커밋 전에 ``_fill_versions``가 채운다
- 누락된 버전, 리소스 필터링처럼 확인된 정보만으로 고칠 수 있는 케이스는 LLM 호출 전에 sbmig가 직접 고친다

The build-fix loop (ARCHITECTURE §3, docs/v2/PLAN-v2.1.md §3 / §3.1 / §3.2).

Invariant: every applied change is followed by a real build. A loop round is
    build → score previous tries (revert regressions, credit the stored scripts, settle the error list's tries)
    → add the build's NEW errors to the error list → stop on BUILD SUCCESS → cluster by case → decide → fix + commit
and the loop only exits *after* a build: on success, when the fix rounds are used up (`loop.phases.verify.
max_iterations`, 50; the last round's changes are still built and scored), when nothing actionable is left, when only
blocked clusters remain, or when the run's time budget is exceeded. LLM calls are not capped unless
`loop.max_llm_calls` > 0.

The ladder per case (`sbmig.triage.scripts`): **KB search** (the log line says what it found: "KB 탐색: 저장된
스크립트 없음 (처음 보는 케이스) → LLM 에 새 수정 요청" / "KB 탐색: 저장본 v2 (proven, 3 projects) → 실행") → the
stored script runs (source `kb-script`, no LLM call) → when it fails: rollback → **LLM augment** (the prompt carries
the script and what it did; an answer with a script → the new version runs and is stored, `llm-augment`; an answer
with `no_script_reason` + edits → the edits are applied directly, `llm-edit`) → no stored script: **LLM new**
(`llm-new`: edits applied directly and the script stored as `unverified`; only a script → it runs and is stored). Per
case and run: 1 stored + `script.max_llm_tries_per_case` (20) LLM asks, of them at most
`script.max_augments_per_case` (15) augments (used up → the next ask is a fresh `new`), then BLOCKED
(repeated_failure). When an
answer's first choice does not apply (the edits fail / change nothing, or its script fails), the other half of the
same answer is applied instead. Edits go through `sbmig.script.apply_edits` (the script helper API's guards); changes
are committed `fix(script:<case>:v<N> <mode>)` / `fix(edits:<case> <source>)`.

Every try is one `FixTry` of the run's error list (`sbmig.loop.entries.RunErrors`: begin_try when its source is
known, finish_try with 원인 / 해결 / applied / files / 수정 notes / diff / commit / result, again after scoring; resolve
on improved / build_ok; block for a blocked case). The 수정 notes (`FixTry.edits`, `sbmig.loop.notes`) describe
every modification of a committed change — the LLM's `desc` / `<changes>` with the lines from the diff, a note made
from the diff for whatever the LLM did not describe (a reused stored script: the diff only) — and each is one INFO
line `수정: <path> <lines> — <change>` (the diff itself stays at DEBUG). Every log line carries the stage
(`sbmig.core.stage`): `fix r3` for the round, `fix r3 7a52b1cea8` for a case's work in it.

Ordering and scoring:
- the cases of a round come root causes first (`sbmig.triage.cluster.CAUSE_RANK`); a case whose error files
  are ALL error files of cases already worked on in this round is deferred, as is a compile case whose
  module's pom.xml a fix of this round changed (`_deferral`);
- rollback before the next try of a case (reset when it is HEAD, else `git revert`) unless that try was
  improved / build_ok; the errors it had removed come back into the case and into the next scoring's baseline;
- scoring (`_score`): "not visible" is not "resolved". `improved` only when every targeted error is gone AND
  the build reached their stage, `inconclusive` when the build cannot tell (no KB credit), `partial` = some
  gone and nothing new in the touched files; structural kinds after a pom edit revert that try;
- also here: blocked clusters (run-wide `BlockedRegistry`), stall / oscillation, the LLM cool-down (L5) and
  the configuration-error stop, library-advisory hints and the merge-conflict reference copies (never a patch
  target).

Dependency version policy (docs/INTERFACES.md "v2.1d"): before every LLM call `_library_hints` puts the facts
of the libraries behind the case into `FixContext.library_hints` (the table entry `<kb>/libraries.yml` + shipped, and
what the repository has: same-major releases with jakarta ✓ / javax, the higher majors; INFO `의존성 버전 조회: …`);
after every applied try whose commit touched a pom, `_read_back` lists its major upgrades / library replacements
(`FixAttempt.major_upgrades`, `FixTry.major_upgrades`) with the 운영자 확인 guide (`<verify>` → one `verify_guide` LLM
call → sbmig's synthesized one; WARNING `메이저 업그레이드: … — 운영자 확인 필요`). Nothing is reverted or blocked for
it; every call into `sbmig.build.depchanges` / `repo_versions` / `jakarta_libraries` is best effort.

Versions carried over from the as-is build, never guessed by the LLM ("v2.1l"):
`VersionResolver` is the one resolution order (the new parent chain manages it → empty; the as-is build's version;
for a dependency the LLM just added, the repository's newest release; else unknown). `_fill_versions` runs after an
applied script / edits and before the commit, on the poms the try changed; a `pom_invalid|missing_version` case is
first tried without the LLM (`_version_attempt`: source `sbmig-version`, commit `fix(sbmig-version): <case>`, no
stored script, not a ladder step — `_previous(ladder=True)` leaves it out; a partial one is not rolled back); the
"Dependency versions" prompt section starts with the `as-is build:` / `new parent chain:` facts (`_version_facts`).

Binary files under resource filtering ("v2.1m"): a second zero-LLM pre-try beside
`sbmig-version` (`_pre_tries`: (source, targets, run) — `_pre_try_for` picks one on the triage decision, it runs after
the round's deferral + `_pre_try_deferral`, `_pre_attempt` is the shared attempt / commit / key sequence): a
maven-resources-plugin / maven-war-plugin filtering failure (`reactive_targets`) gets the extensions of
`binary_resource_plan(errors=…)` written by `apply_non_filtered_extensions` (source `sbmig-resources`, commit
`fix(sbmig-resources): <cluster id>`, once per (pom, extension set) per run); the plan's lines and files lead the
prompt facts when it could not apply (`_resources_facts`).
"""
from __future__ import annotations

import contextvars
import logging
import posixpath
import re
import threading
import time
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NamedTuple, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import FixContext, TriageContext
from sbmig.core.models import (GAV, AdvisoryFinding, BlockReason, BuildError, BuildResult, DecisionKind, ErrorCluster,
                               ErrorKind, FileChange, FixAttempt, IterationRecord, JarCoordinate, KBMatch, LLMUsage,
                               MergeConflict,
                               Phase,
                               PhaseResult, ProjectProfile, ScriptBase, ScriptRunResult, TriageDecision, utcnow)
from sbmig.core.workspace import Workspace

from sbmig.core.stage import stage

from sbmig.analyze.bundled_jars import providers
from sbmig.restructure.bundled_jars import repository_state_fn
from sbmig.restructure.system_jars import WiredJar, bundled_jar_followup, wire_bundled_jar
from sbmig.errors.cases import OUT_OF_SCOPE, SCOPE_FULL, SCOPE_REDUCED

from .blocked_registry import (BlockedRegistry, attribute, compose_suggestion, is_in_house, is_javax_api_artifact,
                               sanitize_errors)
from .budget import LoopBudget
from .callee import takes_keyword
from .components import LazyFixer, ScriptFixerMissing, profile_summary
from .entries import CaseScripts, RunErrors, script_path
from .notes import (answer_notes, fallback_guide, log_major_upgrades, note_line, operator_changes, pom_changed,
                    table_entry, try_notes)

log = logging.getLogger("sbmig.loop")

TEST_COMPILING_GOALS = ("test-compile", "package", "verify", "install", "deploy", "test")
TEST_RUNNING_GOALS = ("test", "package", "verify", "install", "deploy")
RESOLUTION_PREFIXES = ("parent_resolution:", "pom_invalid:", "dependency_resolution:", "plugin_resolution:")
# 쿨다운과 "LLM 사용 불가"로 세는 전송이나 설정 실패를 찾는다. 형식을 어긴 응답은 모델 탓이라 여기 넣지 않는다
# 클라이언트가 남긴 HTTP 상태, 전송 예외 클래스, 알려진 설정 문구만 본다. `config` 같은 단어만으로는 판단하지 않는다
_LLM_TRANSPORT_RE = re.compile(
    r"(?i)\bstatus=(?:none|40[134]|408|429|5\d\d)\b|\bcause=[A-Za-z_][\w.]*|response_path|invalid json|"
    r"cannot find the reply text|not found in the response|api[_ ]?key\b[^.]*\bnot set|needs an api key|"
    r"\bllm[._][\w.]*\s+is not set|unknown llm\.provider|cannot be imported")
_LLM_TRANSPORT_CLASS_RE = re.compile(      # 영어 단어가 아니라 예외 클래스 이름이라 대소문자를 구분한다
    r"\b(?:ConnectionError|ConnectTimeout|ConnectTimeoutError|ConnectError|ReadTimeout|ReadTimeoutError|Timeout|"
    r"TimeoutError|TimeoutException|WriteTimeout|PoolTimeout|ProxyError|SSLError|TransportError|RemoteProtocolError|"
    r"RemoteDisconnected|ChunkedEncodingError|ConnectionResetError|ConnectionRefusedError|ConnectionAbortedError|"
    r"NewConnectionError|MaxRetryError|NameResolutionError|LLMHardDeadline|LLMConfigError|MissingComponentError|"
    r"PluginLoadError)\b")
# 게이트웨이가 요청 내용 때문에 거절한 경우다. 너무 긴 요청, 형식 오류, 토큰 한도가 그렇다
# 프롬프트의 문제이므로 "LLM 사용 불가"로 세지 않는다
_LLM_REQUEST_REJECTED_RE = re.compile(r"status=4(?:00|13|22)\b|content[ _-]?filter|"
                                      r"finish_reason=(?:length|max_tokens)\b")
# 프롬프트가 너무 크다는 400은 다른 프롬프트도 같은 본문을 받을 수 있으므로 설정 오류로 보지 않는다
_LLM_PROMPT_TOO_BIG_RE = re.compile(r"(?i)context[ _-]?length|too (?:long|large|many tokens)|"
                                    r"max(?:imum)?[ _-]?(?:context|tokens?|input)|token limit|payload too large|"
                                    r"finish_reason=(?:length|max_tokens)")
_LLM_400_BODY_RE = re.compile(r"status=400\)?:?\s*(?P<body>.*)", re.S)
# 서로 다른 프롬프트 2개가 같은 HTTP 400 본문을 받으면 설정 오류로 보고 그 실행의 LLM 호출을 멈춘다
LLM_CONFIG_ERROR_PROMPTS = 2
_BLOCKED_RAT_RE = re.compile(r"blocked:\s*(?P<r>[a-z_]+)")
REFERENCE_MAX_CHARS = 12_000         # 수정 프롬프트에 넣는 병합 탈락 복사본 하나의 최대 길이
# Windows에는 SIGUSR1이 없으므로 루프가 오래 조용하면 감시 스레드가 스스로 경고하고 스레드 덤프를 남긴다
WATCHDOG_S = 30.0                    # 감시 스레드가 깨어나는 주기
WATCHDOG_QUIET_S = 60.0              # 이 시간 동안 진행이 없으면 경고한다
WATCHDOG_DUMP_S = 300.0              # 이 시간이 지나면 실행 폴더에 스레드 덤프를 남긴다
REFERENCE_MAX_FILES = 3              # 케이스당 병합 탈락 복사본 수
_TRUNCATED_MARK = "\n… (truncated)"
# 개선되지 않은 시도는 되돌리고, 개선되었거나 빌드에 성공한 시도만 트리에 남긴다
# NO_ROLLBACK의 나머지 상태는 이미 트리에 없거나 아직 결과가 나오지 않아 되돌릴 것이 없다
SCRIPT_KEEP = frozenset({"improved", "build_ok"})
NO_ROLLBACK = frozenset({"improved", "build_ok", "reverted", "pending", "apply_failed", "blocked"})
SCRIPT_BASE_LOG = 50                 # 보강 프롬프트에 넣는 ScriptBase.log 줄 수
SCRIPT_HISTORY = 10                  # ScriptBase.history 줄 수
FIXER_WITHOUT_SCRIPTS_FLAG = "fixer-without-scripts"
ROUND_TRIP = "round_trip"            # 왕복 수정에 참여한 저장 버전의 KB 결과
# 이 메모가 붙은 시도는 최종 트리에 없으므로 운영자 확인 목록에서도 뺀다
ROLLED_BACK_NOTE = "rolled back before "
try:    # 오류 목록과 같은 정의를 쓴다. 비정상 종료 빌드의 대체 오류와 오류 종류별 Maven 단계 번호다
    from sbmig.errors.entries import STAGE, error_stage
    from sbmig.errors.entries import SYNTHETIC_PREFIXES as _SYNTHETIC_PREFIXES
except ImportError:  # pragma: no cover - 오류 목록 패키지는 sbmig 안에 있다
    _SYNTHETIC_PREFIXES = ("mvn exited with code", "build timed out")
    STAGE = {
        ErrorKind.POM_INVALID: 0, ErrorKind.PARENT_RESOLUTION: 0,
        ErrorKind.DEPENDENCY_RESOLUTION: 1, ErrorKind.PLUGIN_RESOLUTION: 1,
        ErrorKind.ENFORCER: 2, ErrorKind.REWRITE: 2,
        ErrorKind.COMPILE: 3, ErrorKind.UNKNOWN: 3,
        ErrorKind.TEST_COMPILE: 4,
        ErrorKind.TEST_FAILURE: 6,
    }

    def error_stage(e: BuildError) -> Optional[int]:
        """오류 종류별로 Maven이 그 오류를 보고하는 빌드 단계 번호를 반환한다. 번호가 작을수록 이른 단계다.
        공용 정의를 가져오지 못할 때 쓰는 대체 정의다."""
        if e.kind == ErrorKind.PLUGIN_EXECUTION:
            return 2 if "resources" in (e.goal or "").lower() else 5
        return STAGE.get(e.kind)
TRY_NOTE_CHARS = 600                 # FixTry.note에 합쳐 넣는 실패, 되돌림, 평가 메모의 최대 길이
NOTES_LOGGED = 20                    # 변경 하나당 INFO로 남기는 `수정:` 줄 수. 나머지는 errors.md에만 남는다


def fmt_error(e: BuildError) -> str:
    """오류 하나를 ``파일:줄 메시지`` 형식의 한 줄로 만든다. 보강 프롬프트의 오류 목록에 쓴다.
    파일이 없으면 모듈 이름을, 모듈도 없으면 ``-``를 앞에 쓴다."""
    loc = e.file or e.module or "-"
    if e.file and e.line:
        loc += f":{e.line}"
    msg = " ".join((e.message or e.raw or "").split())
    return f"{loc} {msg[:300]}".strip()



def _status_paths(ws: Any) -> set[str]:
    fn = getattr(ws, "status_paths", None)
    try:
        return set(fn()) if callable(fn) else set()
    except Exception:  # noqa: BLE001 - 가짜 작업 공간이나 git 문제
        return set()


def _restore_build_byproducts(ws: Any, before: set[str]) -> list[str]:
    """빌드가 target/ 밖에 만든 파일을 빌드 직후 치운다. lib/에 복사된 jar 같은 파일이다.
    시도의 커밋과 파일 목록에 실제로 고친 내용만 남기기 위해서다. 치운 경로 목록을 반환한다.

    Delegates to sbmig.core.workspace.restore_build_byproducts; any failure is logged and gives []."""
    fn = getattr(ws, "restore_build_byproducts", None)
    if not callable(fn):
        return []
    try:
        return list(fn(before) or [])
    except Exception as e:  # noqa: BLE001 - never fail a build over the bookkeeping
        log.debug("build byproducts not restored: %s", e)
        return []

def sleep_seconds(seconds: float) -> None:
    """LLM 쿨다운 동안 주어진 초만큼 기다린다. 테스트가 한 번에 바꿔 넣을 수 있도록 모듈 함수로 둔다."""
    time.sleep(seconds)


def _first_message(c: ErrorCluster, limit: int = 200) -> str:
    """케이스 머리 줄에 쓸 첫 오류 메시지를 반환한다. 메시지에 심볼이 없으면 ``(symbol …)``을 붙인다."""
    if not c.errors:
        return "-"
    e = c.errors[0]
    text = (e.message or e.raw or "").strip()
    if e.symbol and e.symbol not in text:
        text += f" (symbol {e.symbol})"
    text = " ".join(text.split()) or "-"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _first_sentence(text: str, limit: int = 160) -> str:
    """LLM 문장(원인, 해결, 스크립트가 없는 이유, llm-error)의 첫 문장을 한 줄로 반환한다."""
    t = " ".join((text or "").strip().split())
    for sep in (". ", "。", "\n"):
        if sep in t:
            t = t.split(sep, 1)[0] + ("." if sep == ". " else "")
            break
    return t if len(t) <= limit else t[:limit - 1] + "…"


def cut_lines(text: str, limit: int) -> str:
    """텍스트를 ``limit``자 안에서 줄 단위로 자르고, 잘린 줄 수를 ``(… N more lines)``로 덧붙인다.

    A first line longer than `limit` is cut itself (with "…")."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > limit:
            break
        out.append(line)
        used += cost
    if not out:
        out, rest = [lines[0][: max(0, limit - 1)] + "…"], len(lines) - 1
    else:
        rest = len(lines) - len(out)
    return "\n".join(out) + (f"\n(… {rest} more line{'s' if rest != 1 else ''})" if rest else "")


def case_stage(round_no: int, c: ErrorCluster) -> str:
    """라운드 안에서 케이스 하나를 처리할 때 쓰는 로그 단계 태그 ``fix r3 7a52b1cea8``을 만든다."""
    cid = c.case_id or (c.id[len("case:"):] if c.id.startswith("case:") else c.id)
    return f"fix r{round_no} {cid}"


def _case_header(c: ErrorCluster) -> str:
    """케이스 머리 줄을 만든다. 예: ``4 error(s) in 2 file(s): package jakarta.xml.bind does not exist``"""
    files = len({(e.file or "").replace("\\", "/") for e in c.errors if e.file})
    where = f" in {files} file(s)" if files else (f" in {c.module}" if c.module else "")
    return f"{len(c.errors)} error(s){where}: {_first_message(c)}"


def _class_of(c: ErrorCluster) -> str:
    """라운드 머리 줄에 쓸 케이스의 원인 분류(``package_does_not_exist`` 등)를 반환한다."""
    try:
        from sbmig.triage.cluster import error_class
        cls = next((error_class(e) for e in c.errors if error_class(e)), "")
    except Exception:  # pragma: no cover - triage는 sbmig 코드다
        cls = ""
    return cls or c.kind.value


def is_llm_transport_error(analysis: str) -> bool:
    """``llm-error: …`` 분석 문구가 전송이나 설정 실패면 True를 반환한다.
    이런 실패만 쿨다운과 "LLM 사용 불가"로 센다.
    형식을 어긴 응답이나 내용 때문에 거절된 요청은 프롬프트의 문제라 False다."""
    a = (analysis or "").strip()
    if not a.lower().startswith("llm-error:"):
        return False
    if "did not follow the protocol" in a:
        return False
    if _LLM_REQUEST_REJECTED_RE.search(a):
        return False
    return bool(_LLM_TRANSPORT_RE.search(a) or _LLM_TRANSPORT_CLASS_RE.search(a))


def llm_400_body(analysis: str) -> Optional[str]:
    """HTTP 400 ``llm-error``의 본문을 공백을 줄여 200자까지 반환한다. 400이 아니면 None을 반환한다.
    프롬프트가 너무 크다는 400도 None이다. 큰 프롬프트 둘이 같은 본문을 받는 것은 정상이라 설정 오류로 보지 않는다."""
    a = (analysis or "").strip()
    if not a.lower().startswith("llm-error:") or "status=400" not in a or _LLM_PROMPT_TOO_BIG_RE.search(a):
        return None
    m = _LLM_400_BODY_RE.search(a)
    body = " ".join((m.group("body") if m else "").split())
    body = body.split(" [sbmig] ", 1)[0]
    return body[:200] or "(empty body)"


def build_target_args(profile: Optional[ProjectProfile], target: Optional[str]) -> tuple[Optional[str], list[str]]:
    """``target``을 빌드할 때 쓸 모듈과 추가 Maven 인자를 반환한다.
    루트 aggregator가 그 모듈을 포함하면 루트에서 ``-pl <target> -am``으로 빌드한다.
    프로젝트 안의 공유 라이브러리도 함께 빌드되고 오류 경로가 루트 기준으로 남기 때문이다.
    그렇지 않으면 그 모듈 디렉토리 안에서 빌드한다."""
    if not target or profile is None:
        return target, []
    t = target.strip("/")
    for p in profile.poms or []:
        if p.path == "pom.xml" and p.modules:
            mods = {m.strip("/").removeprefix("./") for m in p.modules}
            if t in mods:
                return None, ["-pl", t, "-am"]
    return target, []


def _case_of_errors(errors: list[BuildError]) -> str:
    """케이스 id가 없는 클러스터의 케이스 id를 첫 오류로 구해 반환한다. 구하지 못하면 빈 문자열이다."""
    if not errors:
        return ""
    try:
        from sbmig.triage.cluster import case_id_of
        return case_id_of(errors[0])
    except Exception:  # pragma: no cover - triage는 sbmig 코드다
        return ""


def _reduced(c: ErrorCluster, errors: list[BuildError]) -> ErrorCluster:
    """주어진 오류만 담은 새 클러스터를 만들어 반환한다. 서명 목록도 그 오류에 맞춘다."""
    sigs: list[str] = []
    for e in errors:
        s = e.signature or f"{e.kind.value}|{e.message}"
        if s not in sigs:
            sigs.append(s)
    return c.model_copy(update={"errors": list(errors), "signatures": sigs})


@dataclass
class _Round:
    """수정 라운드 하나의 상태를 담는 클래스.
    ``covered``와 ``poms``는 이번 라운드에 고친 오류 파일과 pom을 케이스 ID에 연결한다. 미룰 케이스를 고를 때 쓴다.
    ``restored``는 되돌려서 다시 생긴 오류다. 다음 시도 결과 평가의 기준이 된다."""
    phase: Phase
    iteration: int
    build: BuildResult
    target: Optional[str]
    stall_level: int
    others: dict
    n: int = 0
    applied_any: bool = False
    pending: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    hopeless: set = field(default_factory=set)
    restored: list = field(default_factory=list)
    covered: dict = field(default_factory=dict)    # 오류 파일 경로: 이번 라운드에 그 파일을 고친 케이스 ID
    poms: dict = field(default_factory=dict)       # pom.xml 경로: 이번 라운드에 그 pom을 고친 케이스 ID
    round: int = 1                                 # 로그 단계 `fix r<round>`와 FixTry.round에 쓰는 라운드 번호


@dataclass
class _Pending:
    """적용한 뒤 결과 평가를 기다리는 시도와 그 커밋 범위를 담는 클래스. 되돌릴 때 pre / post sha를 쓴다."""
    attempt: FixAttempt
    pre_sha: str
    post_sha: str


def _label(c: ErrorCluster) -> str:
    try:
        from sbmig.triage.cluster import cluster_label
        return cluster_label(c)
    except Exception:  # pragma: no cover - triage는 sbmig 코드지만 방어한다
        return c.id


def _script_name(a: FixAttempt) -> str:
    """시도 이름 ``script abc1234567:v2 (augment)``를 만든다. 힌트와 로그에 쓴다."""
    return f"script {a.script_case_id or '?'}:v{a.script_version} ({a.script_mode or '?'})"


def _related(cluster: ErrorCluster, attempts: list[FixAttempt]) -> list[FixAttempt]:
    """클러스터 id가 같거나 서명을 하나라도 공유하는 시도를 찾는다. 다른 파일의 시도도 포함한다.
    프롬프트 힌트로만 쓰고 시도 횟수 한도에는 반영하지 않는다."""
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def _same_cluster(cluster: ErrorCluster, attempts: list[FixAttempt],
                  canon: Optional[Callable[[Optional[str]], str]] = None) -> list[FixAttempt]:
    """이 클러스터에 대한 시도를 찾는다. id가 같거나, 파일이 같고 서명이 겹치는 시도다.
    시도 순서, 시도 횟수 한도, 되돌리기는 이 목록을 기준으로 한다.

    "Same file" includes a file that was moved (`canon`)."""
    from sbmig.triage.decide import same_cluster_attempts
    return same_cluster_attempts(cluster, attempts, canon)


def call_edit_applier(fn: Callable[..., Any], ws: Any, edits: list, cfg: Any, errors: list) -> Any:
    """플러그인 edit applier를 ``fn(ws, edits, *, cfg, errors=None)`` 형태로 호출한다.
    ``errors``는 함수가 그 인자를 받을 때만 넘기므로 ``errors``를 받지 않는 회사 플러그인도 그대로 실행된다.
    시그니처를 확인할 수 없으면 ``errors``를 넘긴다.

    `errors` = the case's errors; passed when the signature takes it or `**kwargs`."""
    from sbmig.loop.entries import _accepted
    opt = _accepted(fn, (ws, edits), {"cfg": cfg}, {"errors": errors})
    kw = {"errors": errors} if opt is None else opt
    return fn(ws, edits, cfg=cfg, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# 의존성 버전. as-is 빌드의 버전을 이어받고 LLM이 추측하지 않는다
# ---------------------------------------------------------------------------------------------------------------------
MISSING_VERSION_SIG = "pom_invalid|missing_version"   # "'…version' for g:a is missing" 오류의 공통 서명
# 아래는 LLM을 호출하지 않는 사전 시도마다 FixAttempt.source에 기록하는 값이다
SBMIG_VERSION_SOURCE = "sbmig-version"               # 누락된 버전 채우기
SBMIG_RESOURCES_SOURCE = "sbmig-resources"           # nonFilteredFileExtensions 추가
SBMIG_JAKARTA_SOURCE = "sbmig-jakarta"               # javax 시절 의존성을 jakarta 빌드로 바꾸기
SBMIG_BUNDLED_SOURCE = "sbmig-bundled"               # 트리에 들어 있는 jar 연결
SBMIG_PARENT_SOURCE = "sbmig-parent"                 # as-is 사내 부모 버전 맞추기
RESOURCE_FACTS_FILES = 10                             # 리소스 필터링 프롬프트 정보에 이름을 넣는 바이너리 파일 수
PRE_TRY_SOURCES = frozenset({SBMIG_VERSION_SOURCE, SBMIG_RESOURCES_SOURCE, SBMIG_JAKARTA_SOURCE,
                             SBMIG_BUNDLED_SOURCE, SBMIG_PARENT_SOURCE})
# BlockedItem.suggestion과 notes에 남기는 근거 문장의 머리말
JAKARTA_SEARCH_PREFIX = "repository search for a jakarta build of"
UNMAPPED_PREFIX = "the library behind"                                # 의존성을 찾지 못한 클래스 문장의 머리말
# 클래스 패키지를 선언된 의존성과 맞춰 볼 때 아무것도 구별하지 못하므로 무시하는 토큰
_GENERIC_TOKENS = frozenset({"com", "org", "net", "io", "kr", "co", "java", "javax", "jakarta", "api", "core", "lib",
                             "libs", "common", "commons", "util", "utils", "web", "app", "impl", "internal", "base"})
VERSION_FACTS_MAX = 12                                # 프롬프트 "as-is build: …" 정보에 넣는 케이스당 아티팩트 수
VERSION_TRY_APPLY_FAILURES = 3                        # (pom, g:a) 하나에 대해 적용에 실패해도 되는 사전 시도 횟수
PRE_TRY_APPLY_FAILURES = VERSION_TRY_APPLY_FAILURES   # 모든 사전 시도에 같은 한도를 쓴다
# 프레임워크 프로젝트의 "Scope of this run" 절 길이 한도와, 되돌린 시도의 이유 문장 머리말
SCOPE_FACTS_MAX_CHARS = 1200
FRAMEWORK_GUARD_PREFIX = "framework project:"
# 프레임워크 검사가 예외로 끝나 시도를 거부할 때 남기는 이유 문장이다. 예외 내용은 넣지 않는다
FRAMEWORK_GUARD_INCOMPLETE = (
    f"{FRAMEWORK_GUARD_PREFIX} the check of the attempt's poms could not be completed — nothing of it was kept. "
    "This is a framework project: every <parent> and the framework artifact's declaration and version must stay "
    "exactly as they are; fix the code instead, or answer <blocked> naming the framework artifact when only its "
    "owner can fix it")


@dataclass(frozen=True)
class _PreTry:
    """LLM을 호출하지 않는 사전 시도 한 종류를 담는 클래스. ``sbmig-version``, ``sbmig-resources`` 같은 것이다.
    ``targets(c)``는 고칠 pom 목록을 반환하고, 케이스가 이 종류가 아니면 None을 반환한다.
    ``run(pr, st, c)``는 적용하면 True를 반환한다. 그 케이스의 이번 라운드 시도가 된다.

    `targets` also feeds the deferral check."""
    source: str
    targets: Callable[[ErrorCluster], list[str] | None]
    run: Callable[..., bool]
VERSION_SOURCE_TEXT = {"asis": "as-is 빌드의 버전", "repository": "저장소의 최신 릴리스",          # 로그, 수정 설명
                       "boot-bom": "목표 Boot BOM 의 버전 (부모 체인이 관리하지 않음)",
                       "chain-property": "부모 체인의 버전 속성"}
VERSION_SOURCE_NOTE = {"asis": "the as-is build", "repository": "newest release in the repository",   # 시도 메모
                       "boot-bom": "the target Spring Boot BOM, not managed by the parent chain",
                       "chain-property": "a version property of the parent chain"}


# groupId별 버전 속성 키워드와 하위 그룹 포함 여부다
# org.springframework는 그 그룹만 보는데, security 등 하위 그룹은 버전 체계가 다르기 때문이다
VERSION_PROPERTY_FAMILIES: tuple[tuple[str, str, bool], ...] = (
    ("org.springframework.boot", "spring-boot", True),
    ("org.springframework.cloud", "spring-cloud", True),
    ("org.springframework.data", "spring-data", True),
    ("org.springframework", "spring-framework", False),
)
CHAIN_PROPERTY_FACTS_MAX = 20                         # 프롬프트당 "parent chain property: …" 줄 수
# "package <p> does not exist"면 <p>를 접두어로 갖는 가장 긴 groupId의 관리 artifactId를 보여 준다
PACKAGE_FACTS_ARTIFACTS = 25                          # `managed artifacts under <group>` 한 줄의 artifactId 수
PACKAGE_FACTS_LINES = 5                               # 프롬프트당 그런 줄 수. 없는 패키지마다 한 줄이다
_PACKAGE_MISSING_RE = re.compile(r"package\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)\s+does not exist")
# `cannot find symbol … symbol: class X`에서 X를 찾는다. 패키지는 오류 파일의 import 줄에서 얻는다
_SYMBOL_CLASS_RE = re.compile(r"symbol:\s+class\s+([A-Za-z_]\w*)")
BUNDLED_READ_CHARS = 200_000                          # 번들 jar 확인에서 import를 찾으려고 읽는 오류 파일 길이
BUNDLED_TRUSTED_SOURCES = ("pom_properties", "sha1_lookup", "name_table")   # 저장소에서 확인한 jar 좌표의 출처
# 새 체인이나 목표 Boot BOM이 관리하는 라이브러리 줄의 끝 문장이다. 저장소 목록과 결과는 붙이지 않는다
API_CHANGED_TAIL = " — the API changed, fix the code and leave the version"
OTHER_ERRORS_CHARS = 4000                             # FixContext.other_errors_summary 길이. 줄 단위로 자른다
# 다른 케이스의 시도가 남긴 오류를 받은 케이스의 프롬프트 절 상한이다. 절은 줄이지 않으므로 여기서 길이를 맞춘다
# 기본 예산 60,000자의 10분의 1쯤이고, diff는 지금 케이스 파일의 것만 넣는다
EARLIER_FIX_CHARS = 6000
EARLIER_FIX_ATTEMPTS = 2                              # 프롬프트당 설명하는 시도 수
EARLIER_FIX_ERROR_LINES = 3                           # 그 시도의 케이스 오류 줄 수
EARLIER_FIX_DIFF_LINES = 40                           # 시도당 이 케이스 파일의 +/- / @@ 줄 수
EARLIER_FIX_FILES = 4                                 # 시도당 diff를 보여 주는 이 케이스 파일 수
EARLIER_FIX_LINE_CHARS = 200                          # diff 한 줄 길이. 압축된 소스가 있어서 자른다
EARLIER_FIX_OTHER_FILES = 6                           # 한 줄에 이름만 넣는 커밋의 다른 파일 수
# 수정도 스크립트도 없는 답이나 <blocked> 답은 거절로 본다. 프로젝트 밖의 원인이라는 뜻이다
# script.max_declines_per_case번 거절하면 그 케이스와 원인이 같은 케이스를 needs_owner로 차단한다
DECLINED_NOTE = "declined: "                          # 거절한 답의 FixAttempt.notes 머리말
NEEDS_OWNER_KO = "소유팀 결정 필요"                    # BlockedItem.suggestion 한국어 문장의 끝
# 답을 해석하지 못한 것이 아니라 스크립트나 수정이 없었다는 뜻의 ScriptProposal.error 값
_DECLINE_ERRORS = ("no <script> block", "no <edits> block", "no script and no edits", "no edits:")
NON_IDEMPOTENT_NOTE = "non-idempotent: "              # FixAttempt.notes 머리말
# 거절한 케이스도 triage가 붙인 구체적인 차단 사유를 유지한다. 운영자 문서와 attn 코드가 이 사유를 쓴다
# needs_owner는 사유가 없는 클러스터에만 쓴다
DECLINE_KEEPS_REASONS = frozenset({BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE,
                                   BlockReason.ARTIFACT_NOT_FOUND, BlockReason.PLUGIN_INCOMPATIBLE})
_STEM_SUFFIXES = ("-starter", "-bom", "-parent")


def artifact_stem(artifact_id: str) -> str:
    """artifactId 끝의 ``-starter`` / ``-bom`` / ``-parent``를 떼어 기본 이름을 반환한다."""
    for suf in _STEM_SUFFIXES:
        if artifact_id.endswith(suf) and len(artifact_id) > len(suf):
            return artifact_id[: -len(suf)]
    return artifact_id


def _property_base(name: str) -> str | None:
    for suf in (".version", "-version"):
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return None


def version_property_matches(gav: Any, properties: Iterable[str], *, in_house: bool = False) -> list[str]:
    """부모 체인의 ``*.version`` / ``*-version`` 속성 중 이 아티팩트의 것을 구체적인 순서로 반환한다.
    - 이름이 artifactId나 그 기본 이름으로 시작하는 속성
    - 그룹 키워드인 속성. ``spring-boot``, ``spring-cloud``, ``spring-data``, ``spring-framework``다
    - 사내 아티팩트는 기본 이름이 속성 이름 + ``-``로 시작하는 속성도 넣는다. ``xxx-reuse-web``이면 ``xxx-reuse.version``이다

    The chain's version properties (`*.version` / `*-version`) that belong to this artifact, most specific first: the
    name starts with the artifactId or its stem, or is the group's family keyword (`spring-boot`, `spring-cloud`,
    `spring-data`, `spring-framework` for `org.springframework` itself; dotted spellings too), or — in-house only —
    the stem starts with the property's base + `-` (the family: `xxx-reuse-web` → `xxx-reuse.version`)."""
    a = str(getattr(gav, "artifact_id", "") or "")
    g = str(getattr(gav, "group_id", "") or "")
    if not a:
        return []
    stem = artifact_stem(a)
    fams = [kw for grp, kw, sub in VERSION_PROPERTY_FAMILIES if g == grp or (sub and g.startswith(grp + "."))][:1]
    kws = [x for kw in fams for x in (kw, kw.replace("-", "."))]
    scored: list[tuple[int, str]] = []
    for name in properties:
        base = _property_base(str(name))
        if not base:
            continue
        score = 0
        for pfx in dict.fromkeys((a, stem)):
            if base == pfx:
                score = max(score, 400 + len(pfx))
            elif base.startswith((pfx + ".", pfx + "-")):
                score = max(score, 300 + len(pfx))
        for kw in kws:
            if base == kw or base.startswith((kw + ".", kw + "-")):
                score = max(score, 200)
        if in_house and stem.startswith(base + "-"):
            score = max(score, 100 + len(base))
        if score:
            scored.append((score, str(name)))
    return [n for _sc, n in sorted(scored, key=lambda x: (-x[0], x[1]))]


def chain_company_properties(facts: Any, cfg: Any) -> tuple[dict[str, str], dict[str, str]]:
    """회사 부모 체인의 pom이 정의한 속성과 각 속성을 정의한 pom을 반환한다.
    정의한 pom의 groupId가 사내 부모 그룹이나 그 하위 그룹이거나 ``is_in_house``가 True일 때만 넣는다.
    Spring / Spring Boot pom과 sbmig가 추가한 Boot BOM의 속성은 회사 체인에서 참조할 수 없으므로 뺀다.
    정보에 ``company_properties``가 이미 있으면 그것을 쓴다.

    The properties DEFINED BY THE COMPANY'S OWN chain poms and their defining pom: the defining pom's groupId is a
    root's group (or under it) or in-house (`is_in_house`); never a Spring / Spring Boot pom (`org.springframework*`)
    nor the Boot add-on — a `${lombok.version}` taken from the Boot BOM of a `spring-boot-starter-parent` chain would
    not resolve on the company chain. `facts.company_properties` / `company_property_sources` when the facts carry
    them (non-empty); else the same rule applied to `properties` / `property_sources` here."""
    own = getattr(facts, "company_properties", None)
    if isinstance(own, dict) and own:                 # 로더가 나눠 둔 값이 있으면 그것을 쓴다
        return dict(own), dict(getattr(facts, "company_property_sources", None) or {})
    props = dict(getattr(facts, "properties", None) or {})
    sources = dict(getattr(facts, "property_sources", None) or {})
    roots = {str(r).split(":")[0] for r in getattr(facts, "roots", None) or [] if r}
    keep: dict[str, str] = {}
    keep_src: dict[str, str] = {}
    for name, value in props.items():
        src = sources.get(name) or ""
        parts = src.split(":")
        g = parts[0] if len(parts) >= 2 else ""
        if not g or g == "org.springframework" or g.startswith("org.springframework."):
            continue                                  # 출처를 모르거나 Spring / Boot pom이면 뺀다
        if any(g == x or g.startswith(x + ".") for x in roots) or \
                is_in_house(GAV(group_id=g, artifact_id=parts[1]), cfg):
            keep[name], keep_src[name] = value, src
    return keep, keep_src


def chain_own_managed(facts: Any, boot: Callable[[], dict] | dict | None = None) -> dict[str, str]:
    """사내 부모 체인이 직접 관리하는 버전을 반환한다.
    sbmig가 추가한 Boot BOM(``boot_bom_added``)의 항목은 실제 빌드가 관리하지 않으므로 뺀다.
    정보에 ``chain_managed``가 있으면 그것을 쓰고, 없으면 ``managed``에서 Boot BOM과 값이 같은 항목을 뺀다.

    `chain_managed` (also read under its older name `managed_by_chain`) is filled by the loader only when
    it added the Boot BOM. Without it: an entry the chain declares itself at another version than the BOM's
    stays."""
    # 로더가 채운 `chain_managed`를 먼저 쓰고 없으면 이전 이름 `managed_by_chain`을 쓴다
    own = getattr(facts, "chain_managed", None)
    if own is None:
        own = getattr(facts, "managed_by_chain", None)
    if isinstance(own, dict):
        return dict(own)
    managed = dict(getattr(facts, "managed", None) or {})
    if not getattr(facts, "boot_bom_added", False):
        return managed
    try:
        bom = boot() if callable(boot) else (boot or {})
    except Exception as e:  # noqa: BLE001 - no BOM facts: nothing to take out
        log.debug("Boot BOM facts for the chain's own keys unavailable: %s: %s", type(e).__name__, e)
        bom = {}
    return {k: v for k, v in managed.items() if not (k in bom and bom[k] == v)}


def _module_parent_coords(ws: Any, module: str) -> list[str] | None:
    """모듈 pom의 ``<parent>`` 좌표와, 프로젝트 안 최상위 조상 pom의 ``<parent>`` 좌표를 반환한다.
    모듈은 g:a나 artifactId로 찾는다. 그런 모듈 pom이 없으면 None을 반환한다.

    [the module pom's own `<parent>` g:a:v, the `<parent>` of its top-most in-project ancestor]: the
    restructurer's `_parent_managed` / `_chain` rule (follow `<relativePath>`, default `../pom.xml`, stop at an empty
    one, outside the project, a missing pom or an artifactId mismatch). The pom is matched by g:a, else by artifactId
    (as `VersionResolver._module` does). None when no pom of the tree is that module."""
    from sbmig.restructure.pom_editor import PomEditor

    def editor(rel: str) -> Any:
        try:
            return PomEditor(ws.abs(rel))
        except Exception:  # noqa: BLE001 - an unreadable pom is no module
            return None

    eds = {_norm_path(p): editor(p) for p in ws.pom_files()}
    aid = module.split(":")[-1]
    hit = next((p for p, ed in eds.items() if ed is not None and ed.artifact_id()
                and f"{ed.effective_group_id() or ''}:{ed.artifact_id()}" == module), None)
    if hit is None:
        hit = next((p for p, ed in eds.items() if ed is not None and aid and ed.artifact_id() == aid), None)
    if hit is None:
        return None
    coords: list[str] = []
    par = eds[hit].get_parent()
    if par is not None:
        coords.append(f"{par.group_id}:{par.artifact_id}:{par.version or ''}")
    chain, cur = [hit], hit
    for _ in range(20):
        ed = eds.get(cur)
        parent = ed.get_parent() if ed is not None else None
        rp = ed.parent_relative_path() if ed is not None else None
        if parent is None or rp == "":
            break
        target = posixpath.normpath(posixpath.join(posixpath.dirname(cur), rp or "../pom.xml"))
        if not target.endswith(".xml"):
            target = posixpath.join(target, "pom.xml")
        ped = eds.get(target)
        if target.startswith("..") or target in chain or ped is None or ped.artifact_id() != parent.artifact_id:
            break
        chain.append(target)
        cur = target
    top = eds.get(chain[-1])
    tpar = top.get_parent() if top is not None and len(chain) > 1 else None
    if tpar is not None:
        coords.append(f"{tpar.group_id}:{tpar.artifact_id}:{tpar.version or ''}")
    return coords


class VersionResolver:
    """버전이 없는 의존성의 버전을 정하는 클래스. 재구성과 수정 루프가 같은 규칙을 쓴다.
    - 새 부모 체인이 관리하면 버전을 쓰지 않는다
    - 목표 Boot BOM이 관리하면 그 버전을 쓴다. 단 as-is 버전이 더 새것이면 as-is 버전을 쓴다
    - as-is 빌드의 버전을 쓴다
    - 호출하는 쪽이 허용하면 저장소의 최신 릴리스를 쓴다. 그래도 없으면 모르는 것으로 둔다
    예외를 던지지 않는다. 저장소 조회에 실패하면 찾지 못한 것으로 본다.

    Resolution order for a `<dependencies>` entry without `<version>` ("v2.1l"):
    1. the new parent chain manages it (the post-restructure capture; the declaring module's `by_module` entry
       decides when the capture has one, else the merged `managed`; without the capture the parent-chain facts
       WITHOUT their Boot add-on) -> `(None, "managed")`;
    1b. the TARGET Boot BOM manages it -> `(v, "boot-bom")`, unless the as-is version is newer (then
       `(as-is, "asis")`, never a downgrade);
    2. the as-is build resolved it (`by_module[...].direct` -> `.managed` -> merged `managed` -> merged
       `direct`) -> `(v, "asis")`;
    3. only with `allow_repository` (a dependency the LLM just added, a missing-version case): the newest
       release `RepositoryLookup.versions` lists that `is_prerelease` does not reject -> `(v, "repository")`;
    4. `(None, "unknown")`.
    `asis` / `after` are depmgmt dicts (`sbmig.build.effective.load_effective`); `repository` a zero-arg
    callable returning the run's `RepositoryLookup` (or None)."""

    def __init__(self, asis: dict | None = None, after: dict | None = None,
                 repository: Callable[[], Any] | None = None, chain: Any = None, boot: Any = None,
                 per_parent: Callable[[str], dict[str, str] | None] | None = None):
        """두 캡처와 저장소 조회 함수, 재구성 뒤 캡처가 없을 때 쓸 사내 부모 체인 정보를 받아 둔다.
        ``chain``과 ``boot``는 dict이거나 dict를 반환하는 함수다. 함수는 처음 필요할 때 한 번만 호출한다.

        `chain`: the parent-chain facts used when the post-restructure capture is missing (a capture-shaped
        dict). `boot`: the target Boot BOM's managed versions (g:a -> version). `per_parent`: module (g:a or
        artifactId) -> the managed versions of the company parent that module's pom inherits, or
        None when that is not known (no per-parent sets, no such pom): then the merged `chain` view answers."""
        self.asis: dict = asis if isinstance(asis, dict) else {}
        self.after: dict = after if isinstance(after, dict) else {}
        self._repository = repository
        self._repo_cache: dict[str, str | None] = {}
        self._chain_src = chain
        self._chain: dict | None = None
        self._boot_src = boot
        self._boot: dict | None = None
        self._per_parent = per_parent

    @classmethod
    def for_workspace(cls, ws: Any, repository: Callable[[], Any] | None = None,
                      cfg: Any = None, *, reduced: bool = False) -> VersionResolver:
        """실행 폴더의 as-is 캡처와 재구성 뒤 캡처를 읽어 만든다. 읽지 못한 캡처는 빈 캡처로 본다.
        ``cfg``가 있고 재구성 뒤 캡처가 없으면 사내 부모 체인 정보로 첫 단계를 판단한다.
        ``reduced``면 프로젝트가 자기 부모를 그대로 쓰므로 as-is 캡처만 쓴다. 사내 체인과 목표 Boot BOM은 쓰지 않는다.

        Captures: `depmgmt.json` and `depmgmt-after-restructure.json`; the chain fallback is
        `load_parent_chain_facts(cfg).as_capture()` (the restructure's fallback). `reduced` = a framework
        project (scope `jdk-jakarta`, "v2.1z-ak"): the as-is capture is both `asis` and `after`, so a
        version-less dependency is never filled from the Boot BOM there."""
        asis: dict | None = None
        after: dict | None = None
        try:
            from sbmig.build.effective import AFTER_RESTRUCTURE_FILE, load_effective
            asis = load_effective(ws)
            after = asis if reduced else load_effective(ws, name=AFTER_RESTRUCTURE_FILE)
        except Exception as e:  # noqa: BLE001 - no captures: only the repository step is left
            log.debug("dependency version captures unavailable: %s: %s", type(e).__name__, e)
        if reduced:
            return cls(asis, after, repository)

        def boot() -> dict:
            # 모듈 속성으로 호출해야 테스트의 가짜 구현(tests/conftest.py)이 적용된다
            from sbmig.build import boot_bom
            return dict(boot_bom.load_boot_bom_facts(cfg).managed)

        loaded: dict[str, Any] = {}

        def chain() -> dict | None:
            from sbmig.build import parent_chain
            facts = parent_chain.load_parent_chain_facts(cfg)
            loaded["facts"] = facts
            data = facts.as_capture()
            data["managed"] = chain_own_managed(facts, boot)
            # Spring / Boot pom의 속성은 회사 체인에서 참조할 수 없으므로 회사 체인 pom이 정의한 속성만 쓴다
            data["company_properties"], data["company_property_sources"] = chain_company_properties(facts, cfg)
            return data

        parents: dict[Any, list[str] | None] = {}

        def per_parent(module: str) -> dict[str, str] | None:
            # 모듈 pom이 실제로 상속하는 사내 부모의 관리 버전을 쓴다. 재구성의 _parent_managed와 같은 규칙이다
            facts = loaded.get("facts")
            fn = getattr(facts, "managed_for", None)
            if facts is None or not callable(fn) or not getattr(facts, "root_managed", None):
                return None
            sig = (module, tuple(sorted(ws.pom_files())))
            if sig not in parents:
                parents[sig] = _module_parent_coords(ws, module)
            coords = parents[sig]
            if coords is None:
                return None
            roots = {":".join(str(r).split(":")[:2]) for r in getattr(facts, "roots", None) or []}
            root = next((x for x in coords if ":".join(x.split(":")[:2]) in roots), None)
            return dict(fn(root, own=True))

        return cls(asis, after, repository, chain=chain if cfg is not None else None,
                   boot=boot if cfg is not None else None, per_parent=per_parent if cfg is not None else None)

    @staticmethod
    def _has(data: dict) -> bool:
        return bool(data.get("managed") or data.get("direct") or data.get("by_module"))

    @property
    def has_asis(self) -> bool:
        """as-is 캡처에 내용이 있으면 True를 반환한다."""
        return self._has(self.asis)

    @property
    def has_after(self) -> bool:
        """재구성 뒤 캡처에 내용이 있으면 True를 반환한다."""
        return self._has(self.after)

    @property
    def chain(self) -> dict:
        """재구성 뒤 캡처 대신 쓸 사내 부모 체인 정보를 반환한다. 처음 쓸 때 한 번 읽고, 읽지 못하면 빈 dict다."""
        if self._chain is None:
            data: Any = None
            try:
                data = self._chain_src() if callable(self._chain_src) else self._chain_src
            except Exception as e:  # noqa: BLE001 - no facts: step 1 knows nothing
                log.debug("parent chain facts unavailable: %s: %s", type(e).__name__, e)
            self._chain = data if isinstance(data, dict) else {}
        return self._chain

    @property
    def boot(self) -> dict:
        """목표 Boot BOM이 관리하는 g:a별 버전을 반환한다. 처음 쓸 때 한 번 읽고, 읽지 못하면 빈 dict다."""
        if self._boot is None:
            data: Any = None
            try:
                data = self._boot_src() if callable(self._boot_src) else self._boot_src
            except Exception as e:  # noqa: BLE001 - no BOM facts: the step is skipped
                log.debug("target Boot BOM facts unavailable: %s: %s", type(e).__name__, e)
            self._boot = dict(data) if isinstance(data, dict) else {}
        return self._boot

    def boot_version(self, key: str, module: str | None = None) -> str | None:
        """새 부모 체인이 관리하지 않는 g:a의 목표 Boot BOM 버전을 반환한다.
        체인이 관리하거나 BOM에 없으면 None을 반환한다."""
        if self.managed_version(key, module):
            return None
        return self.boot.get(key) or None

    @property
    def has_chain(self) -> bool:
        """새 부모 체인이 관리하는 버전을 알고 있으면 True를 반환한다.
        모르면 첫 단계에서 아무것도 관리되지 않는 것으로 본다."""
        return self.has_after or self._has(self.chain)

    @staticmethod
    def _module(data: dict, module: str | None) -> dict | None:
        """캡처의 ``by_module``에서 모듈 항목을 g:a로, 없으면 artifactId로 찾는다.
        찾지 못하면 None을 반환한다."""
        if not module:
            return None
        by = data.get("by_module") or {}
        own = by.get(module)
        if own is None:
            aid = module.split(":")[-1]
            own = next((v for k, v in by.items() if aid and str(k).split(":")[-1] == aid), None)
        return own if isinstance(own, dict) else None

    def managed_version(self, key: str, module: str | None = None) -> str | None:
        """새 부모 체인이 관리하는 g:a의 버전을 반환한다. 없으면 None을 반환한다.
        캡처에 선언한 모듈의 항목이 있으면 그 모듈의 effective dependencyManagement만 본다.
        다른 모듈이 관리한다고 이 모듈도 관리되는 것은 아니기 때문이다.
        모듈 항목이 없으면 합친 ``managed``를 보고, 재구성 뒤 캡처가 없으면 ``chain``의 관리 버전을 본다.

        Without the post-restructure capture: the set of the company parent the module's pom
        inherits (`per_parent`; a parent that is no configured root manages nothing) when the facts carry per-parent
        sets and the module's pom is found, else the merged `managed` as before."""
        if not self.has_after:
            chain = self.chain
            if module and callable(self._per_parent):
                try:
                    mine = self._per_parent(module)
                except Exception as e:  # noqa: BLE001 - unknown parent: the merged view below
                    log.debug("per-parent managed versions of %s unavailable: %s: %s", module, type(e).__name__, e)
                    mine = None
                if mine is not None:
                    return mine.get(key) or None
            return (chain.get("managed") or {}).get(key) or None
        own = self._module(self.after, module)
        if own is not None:
            return (own.get("managed") or {}).get(key) or None
        return (self.after.get("managed") or {}).get(key) or None

    def asis_fact(self, key: str, module: str | None = None) -> tuple[str | None, str]:
        """as-is 빌드가 g:a에 쓴 버전과 그 출처를 반환한다. 찾지 못하면 ``(None, "")``을 반환한다.
        모듈의 직접 선언 -> 모듈의 관리 버전 -> 합친 관리 버전 -> 합친 직접 선언 순서로 찾는다.
        모듈 밖에서는 다른 모듈이 직접 쓴 버전보다 부모가 관리한 버전이 맞기 때문이다.

        `how` names the map the hit came from: "declared" for a
        `direct` map, "managed by <parent g:a:v>" for a `managed` one — never crossed."""
        own = self._module(self.asis, module)
        parent_of_module = (own.get("parent") if own else None) or self.asis.get("parent")
        maps: list[tuple[dict, str | None]] = []       # (표, 관리하는 부모). 부모가 None이면 직접 선언 표다
        if own is not None:
            maps += [(own.get("direct") or {}, None), (own.get("managed") or {}, parent_of_module or "")]
        maps += [(self.asis.get("managed") or {}, self.asis.get("parent") or ""), (self.asis.get("direct") or {}, None)]
        for table, parent in maps:
            v = table.get(key)
            if v:
                if parent is None:
                    return v, "declared"
                return v, f"managed by {parent}" if parent else "managed"
        return None, ""

    def repository_version(self, key: str) -> str | None:
        """저장소에 있는 g:a의 최신 정식 릴리스를 반환한다. 프리릴리스는 뺀다.
        조회 함수가 없거나 조회에 실패하면 None을 반환한다. 결과는 키별로 캐시한다.

        `RepositoryLookup.versions` lists oldest -> newest."""
        if key in self._repo_cache:
            return self._repo_cache[key]
        found: str | None = None
        try:
            lookup = self._repository() if callable(self._repository) else None
            g, _, a = key.partition(":")
            if lookup is not None and g and a:
                from sbmig.build.repo_versions import is_prerelease
                found = next((v for v in reversed(list(lookup.versions(g, a) or [])) if v and not is_prerelease(v)),
                             None)
        except Exception as e:  # noqa: BLE001 - 저장소 조회 실패는 모르는 것으로 본다
            log.debug("repository versions of %s unavailable: %s: %s", key, type(e).__name__, e)
            found = None
        self._repo_cache[key] = found
        return found

    def resolve(self, key: str, module: str | None = None, *, allow_repository: bool = False,
                check_managed: bool = True) -> tuple[str | None, str]:
        """클래스의 규칙대로 버전과 출처를 반환한다. 예외를 던지지 않는다.
        출처는 managed / boot-bom / asis / repository / unknown 중 하나다.
        ``check_managed=False``면 첫 단계를 건너뛴다. 빌드가 이미 버전이 없다고 알린 의존성일 때다."""
        try:
            if check_managed and self.managed_version(key, module):
                return None, "managed"
            v, _how = self.asis_fact(key, module)
            # 목표 Boot BOM의 버전을 as-is 버전보다 먼저 따르되, as-is 버전이 더 새것이면 버전을 내리지 않는다
            bv = self.boot.get(key) or None
            if bv:
                from sbmig.plan.versions import compare_versions
                if v and compare_versions(v, bv) == 1:
                    return v, "asis"
                return bv, "boot-bom"
            if v:
                return v, "asis"
            if allow_repository and key.startswith("org.springframework"):
                # 체인이나 BOM이 관리하지 않는 Spring 아티팩트는 목표 라인에 릴리스가 없다. 저장소 최신은 단종 버전이라 고정하지 않는다
                # spring-dao 2.0.8 같은 버전이 Spring 7 옆에 들어오기 때문이다. 그 아티팩트는 LLM이 지우거나 바꾼다
                log.info("의존성 버전: %s 은 Spring 계열인데 체인 / BOM 이 관리하지 않음 — 저장소 최신으로 고정하지 않는다(단종 라인)", key)
                return None, "unknown"
            if allow_repository:
                v = self.repository_version(key)
                if v:
                    return v, "repository"
        except Exception as e:  # noqa: BLE001 - never raises: unknown
            log.debug("version of %s not resolved: %s: %s", key, type(e).__name__, e)
        return None, "unknown"


class BuildFixLoop:
    """빌드 오류를 케이스별 수정 시도로 고치는 수정 루프 클래스. 검증 빌드가 실패하면 오케스트레이터가 ``run``을 호출한다.
    다음 시도 방식은 triage가 정하고, 시도 실행과 결과 평가, 되돌리기, KB 실적 기록은 이 클래스가 한다.
    KB가 없으면 LLM 스크립트를 저장하지 않고 v0로 실행한다. 재사용도 하지 않는다.

    The orchestrator makes one per run and shares `budget` / `blocked` / `errors` across them.
    `script_runner` = ScriptRunnerLike (the orchestrator passes a lazy `sbmig.script.ScriptRunner(cfg)`);
    `edit_applier` = `fn(ws, edits, *, cfg, errors=None) -> ScriptRunResult` (None =
    `sbmig.script.apply_edits`; `errors` = the case's errors, passed only when the signature takes it); the
    KB's case store is `kb.cases`; `errors` = the run's error list (`RunErrors`, set by the orchestrator;
    inactive by default). `edit_guard` ("v2.1z-bi") = `fn(ws, pre_head, touched_paths) -> str | None`, asked
    before every commit of an attempt (sbmig's pre-tries included); a non-empty answer rejects the attempt
    like the framework guard (None = no check)."""

    def __init__(self, cfg: Config, ws: Workspace, build_runner: Any, triager: Any, fixer: Any, kb: Any,
                 profile: ProjectProfile, plan_notes: Optional[list[str]] = None,
                 budget: Optional[LoopBudget] = None, *, dry_run: bool = False,
                 history: Optional[list[FixAttempt]] = None, blocked: Optional[BlockedRegistry] = None,
                 locator: Any = None, advisories: Optional[list[AdvisoryFinding]] = None,
                 script_runner: Any = None, edit_applier: Optional[Callable[..., ScriptRunResult]] = None,
                 errors: Optional[RunErrors] = None,
                 edit_guard: Optional[Callable[[Workspace, str, list[str]], Optional[str]]] = None):
        """협력 객체와 실행 중 상태를 준비한다. 시도 이력, 차단 목록, 플래그, LLM 장애 상태 같은 것이다.
        ``history``는 같은 실행의 여러 루프가 공유할 수 있다. 시도 순서와 횟수 한도는 이 이력을 기준으로 한다."""
        self.cfg = cfg
        self.ws = ws
        self.build_runner = build_runner
        self.triager = triager
        self.fixer = fixer
        self.kb = kb
        self.script_runner = script_runner
        self.edit_applier = edit_applier
        self.edit_guard = edit_guard
        self.errors: RunErrors = errors if errors is not None else RunErrors(None)
        self.profile = profile
        self.plan_notes = list(plan_notes or [])
        self.budget = budget or LoopBudget.from_config(cfg)
        self.dry_run = dry_run
        self.history: list[FixAttempt] = history if history is not None else []
        self.flags: list[str] = []
        self.followups: list[str] = []
        self.credited: list[str] = []
        self.escalated: dict[str, TriageDecision] = {}
        self.test_compile_skipped = False
        self.tests_skipped = False               # 실행 중 테스트 실패만 남아 -DskipTests로 패키징했는지
        # 빌드 대상별로 기록한다. MSA 서비스 하나의 대체 경로가 다른 서비스의 최종 빌드를 바꾸면 안 된다
        # 키 ""는 프로젝트 전체다. `skips_for`가 읽는다
        self.test_compile_skipped_targets: set[str] = set()
        self.tests_skipped_targets: set[str] = set()
        self._profile_summary: Optional[str] = None
        # 실행 전체의 차단 목록이다. 오케스트레이터가 모든 단계에 같은 객체를 넘긴다
        self.blocked = blocked if blocked is not None else BlockedRegistry()
        self.locator = locator                   # 클래스가 속한 아티팩트를 찾는다. None이면 찾지 않는다
        # 실행의 라이브러리 권고다. 클러스터마다 맞춰 FixContext.advisory_hints에 넣는다
        self.advisories: list[AdvisoryFinding] = list(advisories or [])
        self.llm_unavailable: Optional[str] = None
        self.llm_config_error: Optional[str] = None   # 서로 다른 프롬프트가 받은 같은 HTTP 400 본문
        self._http400: dict[str, set[str]] = {}  # 400 본문: 프롬프트 해시 목록. 해시가 없으면 클러스터 id를 쓴다
        self.aborted: Optional[str] = None       # 실행을 멈춰야 할 때의 사유. 설정한 부모를 찾지 못한 경우 등
        # 멈춤 요청으로 루프를 빠져나왔는지, 마지막 run()의 마지막 빌드, 루프 끝에 되돌린 시도 수를 남긴다
        # 오케스트레이터는 트리가 그 마지막 빌드와 다를 때만 최종 빌드를 다시 한다
        # stopped_by_request는 실행 내내 유지되고 나머지 셋은 마지막 run()의 값이다
        self.stopped_by_request = False
        self.left_on_stop = False
        # LLM 호출 전에 확인한 중지 요청("stop")이나 실행 시간 한도("time"). 이 라운드에는 더 호출하지 않는다
        self._ask_halt = ""
        self.last_build: Optional[BuildResult] = None
        self.end_rollbacks = 0
        self._llm_streak: list[tuple[ErrorCluster, str]] = []
        self._milestone = "idle"                 # 감시 스레드가 보는 마지막 진행 지점
        self._milestone_at = time.monotonic()
        self._busy: Optional[str] = None         # 진행 중인 이 루프의 Maven 빌드나 OpenRewrite 실행
        self._quiet_from = 0.0                   # 감시 스레드가 그런 호출이나 LLM 호출을 마지막으로 본 시각
        self._streak_attempts: list[str] = []    # 지금 이어지는 전송 실패의 시도 id
        self.llm_cooldown_used = False           # 쿨다운은 실행당 한 번이다
        self._cooled_attempts: set[str] = set()  # 쿨다운을 일으킨 연속 실패의 시도. 다시 세지 않는다
        self._sleep: Optional[Callable[[float], None]] = None   # 테스트용. None이면 모듈의 `sleep_seconds`
        # 재구성의 병합 충돌 목록이다. 오케스트레이터가 넣고 수정 프롬프트 힌트로 쓴다
        self.merge_conflicts: list[MergeConflict] = []
        # 재구성 결과와 모드는 오케스트레이터가 넣어 주며, 프롬프트의 "Project after the restructure" 절을 만들 때 쓴다
        self.restructure_results: list[Any] = []
        self.restructure_modes: list[Optional[str]] = []
        self._layout_cache: Optional[tuple[int, list[str]]] = None
        # 적용한 시도의 파일에 처음 나타난 오류의 (파일, 서명): 그 시도
        self._introduced: dict[tuple[str, str], FixAttempt] = {}
        # 개선으로 평가된 시도가 없앤 (파일, 서명)과 그 시도다. 다른 케이스의 시도가 그 오류를 되살리면 악화로 본다
        # trade-back은 (오류를 되살린 케이스, 해결했던 케이스) 쌍마다 센다
        # `loop.oscillation_limit`에 이르면 OSCILLATION으로 차단할 케이스에 넣는다(케이스 키: (시도, 근거))
        self._resolved_by: dict[tuple[str, str], FixAttempt] = {}
        self._resolved_lines: dict[tuple[str, str], frozenset[int]] = {}   # 그 시도 전 빌드가 보여 준 줄 번호
        self._trade_backs: Counter = Counter()
        self._trade_back_blocks: dict[str, tuple[FixAttempt, str]] = {}
        # 왕복에 참여한 시도와 그 이유다. 저장된 버전의 실적을 round_trip으로 기록한다(`_credit_script`)
        self._round_trips: dict[int, str] = {}
        # 거절한 답의 원인 문장과 needs_owner로 차단한 케이스다. 원인이 같은 케이스는 LLM 호출 없이 차단한다
        # _owner_blocked 항목은 (케이스 id, 파일, 아티팩트 키, 사유 문장)이다
        self._declines: dict[int, str] = {}
        self._owner_blocked: list[tuple[str, frozenset[str], frozenset[str], str]] = []
        self._phase_items: list[str] = []
        self._cur_phase: Phase = Phase.JDK21
        # 실적을 기록한 스크립트(시도: 기록한 결과)와 다음 시도 전에 되돌린 시도
        self._credited: dict[int, str] = {}
        # 프레임워크 검사가 되돌린 시도. KB에는 out_of_scope로 기록한다
        self._out_of_scope: set[int] = set()
        self._rolled_back: set[int] = set()
        self._ref_paths: dict[int, set[str]] = {}   # 시도: LLM에 보여 준 병합 충돌 참고 파일 이름
        self._target: Optional[str] = None
        # 시도별 기록이다. 오류 목록의 (케이스 id, 시도 번호, 라운드), 적용한 diff, 저장된 스크립트 실행이 결과를
        # 정하는지, 수정 적용 결과, 걸린 시간, 평가 집계(사라진 수, 대상 수)
        self._tries: dict[int, tuple[str, int, int]] = {}
        self._diffs: dict[int, str] = {}
        self._script_ran: dict[int, bool] = {}
        self._edit_results: dict[int, ScriptRunResult] = {}
        self._seconds: dict[int, float] = {}
        self._tally: dict[int, tuple[int, int]] = {}
        # 시도 id 문자열은 MSA 서비스별 루프에서 겹칠 수 있으므로 시도별 상태는 FixAttempt 객체의 id()를 키로 쓴다
        self._try_seq: dict[str, int] = {}       # 케이스 id: 시도 번호. 오류 목록이 FixTry를 주지 않을 때 쓴다
        self._answers: dict[int, Any] = {}       # 시도: 적용한 LLM 답. 수정 설명을 가져온다
        self._notes: dict[int, list] = {}        # 시도: 수정 메모 목록. 변경마다 EditNote 하나다
        self._match_hints: dict[str, list[str]] = {}   # 클러스터 id: kb.match 힌트 줄. 그 케이스의 지난 해결 방법이다
        # 의존성 버전 정보다. 라이브러리별 "Dependency versions" 프롬프트 줄(실행 동안 캐시), 저장소 조회기
        # (처음 쓸 때 만들고 실패하면 끈다), 라이브러리별 저장소 조회 결과
        self._library_hint_cache: dict[str, list[str]] = {}
        self._repo_lookup: Any = None
        self._repo_lookup_off = False
        self._repo_facts: dict[str, Any] = {}    # g:a: RepositoryVersions. 읽어 보기에서 같은 메이저 후보를 알려 준다
        # 버전 결정기다. 처음 쓸 때 만든다
        self._version_resolver: VersionResolver | None = None
        # 같은 사전 시도를 반복하지 않도록 이미 확인한 키와 적용 실패 횟수를 기억한다
        # 키는 (출처, pom 또는 모듈, g:a 또는 확장자)다
        self._pre_tried: set[tuple[str, str, str]] = set()
        self._pre_failures: Counter = Counter()
        self._jakarta_plans: dict[str, Any] = {}     # 클러스터 id: sbmig-jakarta 계획. 라운드마다 한 번 계산한다
        self._javax_owner_libs: dict[str, list[GAV]] = {}   # 클러스터 id: classpath 없이 찾은 라이브러리
        self._javax_owner: dict[str, Optional[str]] = {}    # 클러스터 id: 그때 기준으로 삼은 클래스
        # 프레임워크 아티팩트 자신이 javax로 컴파일되어 차단한 케이스와 그 프레임워크 좌표
        self._framework_blocks: dict[str, GAV] = {}
        self._cases = CaseScripts.of(kb)
        # 재사용할 버전을 고를 때 이 실행의 범위를 쓰도록 어댑터에 알려 준다. triage도 같은 어댑터를 쓴다
        self._cases.run_scope = self._run_scope       # sbmig.triage.scripts.plan_script_step이 읽는다
        attach = getattr(triager, "attach_run", None)
        if callable(attach):
            try:
                attach(blocked_registry=self.blocked, ws=ws, cases=self._cases if self._cases.active else None)
            except Exception as e:  # pragma: no cover - 다른 triage 구현
                log.warning("triager.attach_run failed: %s", e)
        # knowledge/notes를 triage에도 넘겨 `replacement: true` 노트가 있는 라이브러리는 차단하지 않게 한다
        self._notes_index: Optional[list[Any]] = None
        if callable(attach):
            try:
                attach(notes=self._knowledge_notes())
            except Exception as e:  # pragma: no cover - 그 키워드를 받지 않는 다른 triage
                log.debug("triager.attach_run(notes=…) not accepted: %s", e)

    @property
    def cases(self) -> CaseScripts:
        """KB 케이스 저장소를 ``CaseScripts`` 어댑터로 반환한다. KB가 없으면 비활성 상태다."""
        return self._cases

    # ------------------------------------------------------------------ 공개 진입점
    def _mark(self, what: str) -> None:
        """감시 스레드가 볼 수 있도록 루프가 지금 하는 일을 기록한다. sbmig.log에도 DEBUG 한 줄을 남긴다."""
        self._milestone, self._milestone_at = what, time.monotonic()
        log.debug("→ %s", what)

    @contextmanager
    def _busy_with(self, what: str):
        """Maven 빌드처럼 스스로 진행 로그를 남기는 호출 동안 감시 스레드가 경고하지 않게 한다.
        호출이 끝나면 조용한 시간을 처음부터 다시 잰다."""
        prev, self._busy = self._busy, what
        try:
            yield
        finally:
            self._busy = prev
            self._quiet_from = time.monotonic()

    def _activity(self) -> Optional[str]:
        """스스로 진행 로그를 남기는 작업이 실행 중이면 그 이름을 반환한다. 없으면 None을 반환한다.
        이 루프의 빌드, LLM HTTP 호출, Maven 자식 프로세스가 그런 작업이다.
        None일 때만 감시 스레드가 경고하므로 긴 LLM 호출을 멈춘 것으로 오해하지 않는다.

        Sources: `self._busy`, `sbmig.fix.client.llm_calls_in_flight`, `sbmig.build.mvn.live_processes` (e.g.
        the classpath attribution)."""
        if self._busy:
            return self._busy
        try:
            from sbmig.fix.client import llm_calls_in_flight
            if llm_calls_in_flight():
                return "LLM call"
        except Exception:  # pragma: no cover - fix 패키지는 sbmig 코드지만 방어한다
            pass
        try:
            from sbmig.build.mvn import live_processes
            if live_processes():
                return "Maven child process"
        except Exception:  # pragma: no cover
            pass
        return None

    def _start_watchdog(self, phase: Phase) -> threading.Event:
        """감시 데몬 스레드를 시작하고 멈출 때 쓸 Event를 반환한다.
        루프가 ``WATCHDOG_QUIET_S`` 동안 조용하면 마지막 진행 지점을 WARNING으로 남긴다.
        ``WATCHDOG_DUMP_S``가 지나면 실행 폴더에 스레드 덤프를 기록한다. 운영자는 이 줄로 멈췄는지 판단한다.

        A slow LLM / build logs its own heartbeat, so a quiet watchdog line points at the code between those
        (hints, prompt packing, patch apply, git). While such a call is in progress (`_activity`) the watchdog
        neither warns nor dumps; the quiet time counts from its end."""
        stop = threading.Event()
        started = time.monotonic()
        dumped_for: set[float] = set()

        def beat() -> None:
            while not stop.wait(WATCHDOG_S):
                now = time.monotonic()
                if self._activity():
                    self._quiet_from = now
                    continue
                quiet = now - max(self._milestone_at, self._quiet_from)
                if quiet >= WATCHDOG_QUIET_S:
                    log.warning("fix loop quiet for %.0fs since '%s' (loop running %.0fs) — still working or "
                                "stuck? a thread dump follows after %.0fs (also: GET /api/threads, kill -USR1 <pid>)",
                                quiet, self._milestone, time.monotonic() - started, WATCHDOG_DUMP_S)
                if quiet >= WATCHDOG_DUMP_S and self._milestone_at not in dumped_for:
                    dumped_for.add(self._milestone_at)
                    try:
                        from sbmig.core.diag import dump_threads
                        dump_threads(self.ws.run_dir)
                    except Exception as e:  # pragma: no cover - 진단 때문에 루프가 멈추면 안 된다
                        log.warning("thread dump failed: %s", e)

        threading.Thread(target=contextvars.copy_context().run, args=(beat,), name="fixloop-watchdog",
                         daemon=True).start()
        return stop

    def run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None,
            first_build: Optional[BuildResult] = None) -> PhaseResult:
        """감시 스레드를 켜고 수정 루프를 실행한다.
        ``first_build``는 루프를 시작하게 한 실패한 ``clean package`` 결과다.
        같은 트리를 다시 빌드하지 않고 이 결과를 첫 빌드로 쓴다. 그 오류는 호출한 쪽이 이미 오류 목록에 넣었다."""
        self._cur_phase = phase
        stop = self._start_watchdog(phase)
        try:
            return self._run(phase, goal, max_iterations, target, first_build)
        finally:
            stop.set()
            self._mark("idle")

    def _run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None,
             first_build: Optional[BuildResult] = None) -> PhaseResult:
        """수정 루프 본체를 실행한다.
        바깥 반복 한 번이 빌드 한 번이다. 시도 결과 평가 -> 오류 목록 갱신 -> 성공이면 종료 -> 정체와 진동 확인 -> 종료 조건 확인 순서다.
        안쪽 반복은 수정 라운드다. 트리가 그대로면 재빌드하지 않고 같은 빌드 결과로 각 케이스의 다음 시도를 실행한다.
        이런 라운드도 ``max_iterations``에 들어간다. 루프는 항상 빌드 뒤에 끝난다."""
        pr = PhaseResult(phase=phase)
        self._phase_items = []
        self.left_on_stop, self.last_build, self.end_rollbacks = False, None, 0
        self._ask_halt = ""
        # trade-back 기록은 이번 run에만 속한다. 같은 루프 객체로 다른 서비스나 대상을 고칠 때 넘어가지 않게 비운다
        self._resolved_by, self._resolved_lines, self._trade_backs, self._trade_back_blocks = {}, {}, Counter(), {}
        self._round_trips = {}
        self._cur_phase = phase
        self._target = target
        self._mark(f"loop start ({goal})")
        self.ws.journal({"event": "loop_start", "phase": phase.value, "goal": goal, "max_iterations": max_iterations,
                         "target": target, "dry_run": self.dry_run, "blocked_known": len(self.blocked),
                         "first_build": first_build.log_path if first_build is not None else None})
        prev_build: Optional[BuildResult] = None
        prev_restored: list[BuildError] = []     # 직전 라운드에서 되돌려 다시 생긴 오류. 평가 기준에 더한다
        prev_multiset: Optional[Counter] = None
        window: list[Counter] = []               # 이전 빌드들의 (위치, 서명)별 개수. 진동을 찾을 때 쓴다
        oscillations = 0
        stall_level = 0
        pending: list[_Pending] = []
        fix_rounds = 0
        build_no = 0
        last: Optional[BuildResult] = None
        while True:
            build_no += 1
            # 빌드가 다음 라운드를 연다. 마지막 라운드 뒤의 빌드는 그 라운드에 속한다
            label = fix_rounds + 1 if fix_rounds < max_iterations else max(1, fix_rounds)
            with stage(f"fix r{label}"):
                reused = build_no == 1 and first_build is not None
                if reused:
                    build = first_build
                    self.ws.journal({"event": "build", "phase": phase.value, "iteration": build_no,
                                     "goal": build.goal, "target": target, "ok": build.ok,
                                     "exit_code": build.exit_code, "errors": build.error_count,
                                     "log": build.log_path, "reused": True})
                    log.info("build 1 = the failed final build (%d error(s), %s) — not built again",
                             build.error_count, build.log_path or "-")
                else:
                    self._mark(f"mvn {goal} (build {build_no})")
                    build = self._build(goal, target, f"{phase.value}-{build_no}", phase, build_no)
                last = build
                pr.iterations = build_no
                # 직전 라운드의 시도를 이 빌드로 평가한다. 악화된 시도는 여기서 되돌리고 그 트리를 다시 빌드한다
                reverted = self._score(pr, pending, build, prev_build, prev_restored)
                scored = [p.attempt for p in pending]
                # 되돌린 뒤 재빌드에서는 그 라운드에서 판단할 수 없었던 시도를 다시 평가한다
                # pom 수정이 만든 새 의존성 오류가 대상 오류를 가린 경우다. 그때까지 FixTry는 대기 상태로 둔다
                pending = [p for p in pending if p.attempt.outcome == "inconclusive"] if reverted else []
                again = {id(p.attempt) for p in pending}
                for p in pending:
                    p.attempt.notes.append("scored again on the rebuild after the revert")
                # 보이지 않는다고 해결된 것은 아니다. 되돌린 라운드의 빌드, 오류를 읽지 못한 실패, 타임아웃은
                # 어떤 오류도 해결한 것으로 처리하지 않는다(void)
                void = bool(reverted) or (not build.ok and not build.errors) or bool(build.timed_out)
                added = 0
                if not reused:     # 다시 쓰는 첫 빌드의 오류는 이미 목록에 있다
                    # 결과를 정하기 전에 오류 목록에 넣는다. 대기 중인 시도의 항목은 열어 두고 resolve가 처리한다
                    # 다른 케이스의 수정으로 사라진 항목은 여기서 해결 처리하고, 그 파일이나 pom을 바꾼 시도를
                    # ``resolved_by``에 남긴다
                    added = self.errors.add_build(build, f"fix r{label}", round=fix_rounds, void=void, scope=target)
                self._after_scoring([a for a in scored if id(a) not in again], build_no)
                if not reused:
                    self._log_build(build_no, build, scored, added, void)
                rec = IterationRecord(phase=phase, iteration=build_no, build_ok=build.ok,
                                      error_count=build.error_count, signatures=build.signature_multiset(),
                                      build_log=build.log_path)
                pr.iteration_records.append(rec)
                if build.ok:
                    pr.build_ok = True
                    break
                if reverted:
                    pr.notes.append(f"build {build_no}: regressed attempt(s) reverted → rebuilding the reverted tree")
                    continue
                # 노드 경계까지 기다리면 웹이 Job을 지워 보고서가 사라지므로 멈춤 요청(stop.request)은 빌드 사이에 확인한다
                # 여기서 빠져나가면 다음 노드나 finalize가 aborted로 마무리한다
                if getattr(self.ws, "stop_requested", lambda: False)():
                    self._leave_on_stop(pr, phase, build_no)
                    break
                # Maven의 HTTP 차단 미러에 막혔으면 부모 버전 맞추기, 사전 시도, LLM 어느 것으로도 풀 수 없어 바로 멈춘다
                if self.http_blocked_abort(pr, phase, build, build_no):
                    break
                # 사내 부모 pom을 찾지 못하면 아무것도 빌드할 수 없고 코드 수정으로도 풀 수 없어 실행을 멈춘다
                if self._parent_abort(pr, phase, build, build_no):
                    break
                # 정체는 오류 집합이 직전 빌드와 같은 경우, 진동은 그보다 이전 빌드와 같은 경우다
                # 정체가 쌓이면 상위 모델로 바꾸고, 진동이 2번 일어나면 관련 케이스를 OSCILLATION으로 차단한다
                # 다른 파일이나 다른 모듈의 같은 오류는 다른 오류이므로 (위치, 서명)별 횟수로 비교한다
                multiset = Counter(_pair(e) for e in build.errors)
                stalled = prev_multiset is not None and multiset == prev_multiset
                oscillating = (not stalled) and any(m == multiset for m in window[:-1])
                stall_level = stall_level + 1 if (stalled or oscillating) else 0
                rec.stalled, rec.stall_level = (stalled or oscillating), stall_level
                if stalled:
                    self.ws.journal({"event": "stall", "phase": phase.value, "iteration": build_no,
                                     "stall_level": stall_level, "error_count": build.error_count})
                if oscillating:
                    oscillations += 1
                    self.ws.journal({"event": "oscillation", "phase": phase.value, "iteration": build_no,
                                     "count": oscillations, "stall_level": stall_level,
                                     "error_count": build.error_count})
                    pr.notes.append(f"build {build_no}: error set equals an earlier one (oscillation "
                                    f"#{oscillations}) — treated as a stall")
                window = (window + [multiset])[-max(2, self.cfg.loop.oscillation_window):]
                clusters = self._cluster(build.errors, target)
                # 다른 케이스가 해결한 오류를 한도만큼 되살린 케이스는 여기서 OSCILLATION으로 차단한다
                self._apply_trade_back_blocks(pr, phase, build_no, clusters)
                if fix_rounds >= max_iterations:
                    log.info("build %d: %d error(s) in %d case(s) — max fix rounds (%d) used up; the loop ends here "
                             "and the final build carries the rest (reported as unresolved)", build_no,
                             build.error_count, len(clusters), max_iterations)
                if oscillating and oscillations >= max(1, int(getattr(self.cfg.loop, "oscillation_limit", 2) or 2)):
                    self._block_oscillation(pr, phase, build_no, clusters)
                if self._only_blocked(pr, clusters, build_no):
                    break
                if fix_rounds >= max_iterations:
                    pr.notes.append(f"max iterations ({max_iterations}) reached with {build.error_count} error(s) "
                                    "left")
                    break
                if self.budget.time_exceeded():
                    self._leave_on_time(pr, phase, build_no, clusters)
                    break
            applied_any = False
            new_pending: list[_Pending] = []
            restored: list[BuildError] = []
            while True:
                fix_rounds += 1
                with stage(f"fix r{fix_rounds}"):
                    new_pending, applied_any, decisions, tried, back = self._fix_round(
                        pr, phase, build_no, build, target, stall_level, clusters, round_no=fix_rounds,
                        max_rounds=max_iterations)
                restored += back
                if not applied_any and tried and fix_rounds < max_iterations and not self.dry_run:
                    # 같은 빌드로 다시 LLM을 호출하기 전에 중지 요청과 시간 한도를 확인한다
                    self._halt_before_ask()
                rec.decisions += decisions
                rec.cluster_count = len(rec.decisions)
                rec.attempt_ids = [a.id for a in pr.attempts if a.iteration == build_no]
                # 무엇이든 적용됐거나, 시도가 없거나, 라운드나 시간을 다 썼거나, 모두 차단됐으면 안쪽 반복을 끝낸다
                if self.dry_run or applied_any or not tried or fix_rounds >= max_iterations or self._ask_halt \
                        or self.budget.time_exceeded() or all(self.blocked.covering(c) for c in clusters):
                    break
                # 트리가 그대로라 재빌드해도 오류가 같으므로 같은 빌드 결과로 다음 시도를 실행한다
                # 이 라운드도 라운드 수에 들어가고, 케이스당 한도는 script.max_llm_tries_per_case다
                self.ws.journal({"event": "reround", "phase": phase.value, "iteration": build_no,
                                 "fix_round": fix_rounds + 1})
            pending = new_pending
            if self._ask_halt and not applied_any:
                # LLM 호출 전에 중지 요청이나 시간 한도에 걸렸고 바뀐 것이 없다. 트리가 마지막 빌드 그대로라 여기서 끝낸다
                # 무엇이든 적용됐으면 다음 빌드 뒤의 확인이 루프를 끝낸다
                if self._ask_halt == "stop":
                    self._leave_on_stop(pr, phase, build_no)
                else:
                    self._leave_on_time(pr, phase, build_no, clusters)
                break
            if self.dry_run:
                pr.notes.append("dry-run: decisions computed and journaled, nothing applied")
                break
            if not applied_any:
                if self._only_blocked(pr, clusters, build_no):
                    break
                pr.notes.append(f"build {build_no}: no actionable fixes (all clusters skipped/escalated/blocked/failed "
                                "to apply) — rebuilding would not change anything")
                break
            prev_build, prev_multiset, prev_restored = build, multiset, restored

        assert last is not None
        # 끝까지 결과를 판단할 수 없었던 시도도 마지막에 한 번 KB 실적으로 기록한다
        self._credit_scripts(pr, [a for a in pr.attempts if a.outcome == "inconclusive"])
        # 대체 빌드로 통과했는지는 메모 문구가 아니라 대체 빌드의 결과로 판단한다
        fell_back = False
        if not pr.build_ok and not self.dry_run and not self.aborted:
            fell_back = self._maybe_skip_test_compile(pr, phase, goal, target, last)
        if not pr.build_ok and not self.dry_run and not self.aborted:
            fell_back = self._maybe_skip_tests(pr, phase, goal, target, last) or fell_back
        self.last_build = last
        # 도움이 되지 않은 케이스의 마지막 시도는 다음 시도 전처럼 루프 끝에서도 되돌려 결과물에 남기지 않는다
        if not pr.build_ok and not self.dry_run and not self.aborted:
            self.end_rollbacks = self._rollback_unhelpful(pr, phase, build_no)
        if pr.build_ok and not fell_back:
            pr.remaining_errors = []
        elif not pr.remaining_errors:
            pr.remaining_errors = list(last.errors)
        pr.ok = pr.build_ok
        pr.blocked = [it for it in (self.blocked.get(i) for i in self._phase_items) if it is not None]
        pr.finished_at = utcnow()
        self.ws.journal({"event": "loop_end", "phase": phase.value, "ok": pr.ok, "iterations": pr.iterations,
                         "attempts": len(pr.attempts), "remaining_errors": len(pr.remaining_errors),
                         "blocked": [b.id for b in pr.blocked], "llm_calls_used": self.budget.llm_calls,
                         "aborted": self.aborted})
        return pr

    # ------------------------------------------------------------------ 시도 결과 평가 뒤 오류 목록과 로그
    def _leave_on_stop(self, pr: PhaseResult, phase: Phase, build_no: int) -> None:
        """중지 요청으로 수정 루프를 끝낼 때 메모와 저널을 남긴다. 빌드 사이와 LLM 호출 전 확인에서 같이 쓴다."""
        pr.notes.append(f"build {build_no}: stop requested (stop.request) — leaving the fix loop before the next build")
        log.warning("stop requested (stop.request) — leaving the fix loop after build %d (no further fix)", build_no)
        self.ws.journal({"event": "stop_request", "phase": phase.value, "iteration": build_no, "where": "fix loop"})
        self.left_on_stop = self.stopped_by_request = True

    def _leave_on_time(self, pr: PhaseResult, phase: Phase, build_no: int, clusters: list[ErrorCluster]) -> None:
        """실행 시간 한도로 수정 루프를 끝낼 때 기록을 남기고 남은 케이스를 차단한다."""
        pr.notes.append("run time budget exhausted: stopping the build-fix loop")
        self._flag("time-budget-exhausted")
        self.ws.journal({"event": "budget_exhausted", "phase": phase.value, "kind": "time"})
        self._block_remaining(pr, phase, build_no, clusters, BlockReason.BUDGET_EXHAUSTED,
                              f"run time budget ({self.cfg.loop.max_total_minutes} min) exhausted before "
                              "this cluster was fixed")

    def _halt_before_ask(self) -> str:
        """LLM 호출 전에 중지 요청과 실행 시간 한도를 확인한다.
        걸리면 "stop"이나 "time"을 반환하고, 이번 루프 실행에서는 더 호출하지 않는다. 걸리지 않으면 빈 문자열이다.

        Checked before every LLM request (`propose_script`, `verify_guide`): the workspace's stop check (the web's
        `stop.request`; the remediation's stop and deadline) first, then the run time budget. The answer sticks for
        the rest of the loop run (`_ask_halt`, reset by `_run`); `_run` then ends the loop the way a stop or
        the time budget found between builds does."""
        if not self._ask_halt:
            try:
                stop = bool(getattr(self.ws, "stop_requested", lambda: False)())
            except Exception:  # noqa: BLE001 - 읽을 수 없는 중지 표시는 중지 요청으로 보지 않는다
                stop = False
            if stop:
                self._ask_halt = "stop"
            elif self.budget.time_exceeded():
                self._ask_halt = "time"
            if self._ask_halt:
                self.ws.journal({"event": "llm_ask_skipped", "phase": self._cur_phase.value if self._cur_phase else "",
                                 "why": self._ask_halt})
                log.info("LLM 요청 전에 %s 확인 — 이 라운드에서 더 묻지 않는다",
                         "중지 요청" if self._ask_halt == "stop" else "실행 시간 한도")
        return self._ask_halt

    def _after_scoring(self, scored: list[FixAttempt], build_no: int) -> None:
        """직전 라운드에서 평가한 시도를 마무리한다.
        - 오류 목록의 FixTry 결과와 메모를 맞추고, 개선이나 부분 해결이면 사라진 항목을 해결 처리한다
        - 실행한 저장 스크립트에 실적을 기록하고, 시도마다 결과를 로그 한 줄로 남긴다"""
        for a in scored:
            case_id, n, rnd = self._tries.get(id(a), (a.script_case_id, 0, 0))
            gone, targeted = self._tally.get(id(a), (0, len(a.cluster_errors)))
            self._sync_try(a)
            # 부분 해결이어도 사라진 항목은 이 시도로 해결 처리하고, 남은 항목은 열어 둔 채 다음 시도에 넘긴다
            if a.outcome in ("improved", "build_ok", "partial") and n:
                self.errors.resolve(case_id, rnd, n)
            self._credit_script(a)
            what = {"improved": "resolved", "build_ok": "BUILD SUCCESS", "partial": "partly resolved",
                    "no_change": "no change", "regressed": "regressed (kept: later fixes depend on it)",
                    "reverted": "regressed → 되돌림", "inconclusive": "inconclusive (빌드가 확인 못 함)"}.get(
                        a.outcome, a.outcome)
            left = targeted - gone
            with stage(f"fix r{rnd or '?'} {case_id or a.cluster_id}"):
                log.info("결과 (build %d): resolved %d/%d%s → %s%s", build_no, gone, targeted,
                         f", {left} 남음" if left and a.outcome != "build_ok" else "", what,
                         f" — {a.notes[-1]}" if a.outcome in ("regressed", "reverted", "inconclusive") and a.notes
                         else "")

    def _log_build(self, build_no: int, build: BuildResult, scored: list[FixAttempt], added: int, void: bool) -> None:
        """빌드 결과를 로그 한 줄로 요약한다. 오류 수, 새로 추가한 항목, 케이스별 해결 수, 남은 항목이다.

        `build 2: 2 error(s); 새 에러 1건 추가; resolved 23 (case 7a52b1cea8 4/4, …), open 2`"""
        if build.ok:
            head = f"build {build_no}: BUILD SUCCESS"
        elif void:
            head = (f"build {build_no}: {build.error_count} error(s) — "
                    + ("timed out" if build.timed_out else "no parsed errors" if not build.errors else
                       "reverted tree") + " (not added to the error list)")
        else:
            head = f"build {build_no}: {build.error_count} error(s)" + (f"; 새 에러 {added}건 추가" if added else "")
        parts = []
        total = 0
        for a in scored:
            gone, targeted = self._tally.get(id(a), (0, 0))
            total += gone
            parts.append(f"case {a.script_case_id or a.cluster_id} {gone}/{targeted}")
        if scored:
            head += f"; resolved {total} (" + ", ".join(parts[:6]) + (", …" if len(parts) > 6 else "") + ")"
        if not build.ok:
            n_open = self.errors.open_count()
            head += f", open {n_open if n_open is not None else build.error_count}"
        log.info("%s", head)

    # ------------------------------------------------------------------ 차단 기록
    def _touch_item(self, item_id: str) -> None:
        if item_id not in self._phase_items:
            self._phase_items.append(item_id)

    def _only_blocked(self, pr: PhaseResult, clusters: list[ErrorCluster], build_no: int) -> bool:
        """남은 클러스터가 모두 이미 차단됐으면 True를 반환한다. 고칠 수 없는 오류에 라운드를 쓰지 않기 위해서다."""
        if not clusters:
            return False
        items = [self.blocked.covering(c) for c in clusters]
        if not all(items):
            return False
        for it in items:
            self._touch_item(it.id)
        ids = list(dict.fromkeys(it.id for it in items))
        pr.notes.append(f"build {build_no}: only blocked cluster(s) remain ({len(clusters)} cluster(s), "
                        f"{len(ids)} blocked item(s)) — ending the phase; see 'Blocked' in the report")
        self.ws.journal({"event": "only_blocked", "phase": pr.phase.value, "iteration": build_no,
                         "clusters": [c.id for c in clusters], "blocked": ids})
        return True

    def _candidate_for(self, c: ErrorCluster, d: TriageDecision):
        """BLOCKED 결정의 근거를 ``BlockedCandidate``로 반환한다.
        triage가 주지 않았으면 결정 문구의 ``blocked: <사유>``에서 읽고, 사유를 모르면 반복 실패로 본다."""
        from sbmig.triage.blocked import BlockedCandidate
        getter = getattr(self.triager, "blocked_candidate", None)
        cand = None
        if callable(getter):
            try:
                cand = getter(c.id)
            except Exception:  # pragma: no cover
                cand = None
        if cand is None:
            m = _BLOCKED_RAT_RE.search(d.rationale or "")
            try:
                reason = BlockReason(m.group("r")) if m else BlockReason.REPEATED_FAILURE
            except ValueError:
                reason = BlockReason.REPEATED_FAILURE
            cand = BlockedCandidate(reason=reason, evidence=[d.rationale], errors=list(c.errors))
        return cand

    def _register_blocked(self, phase: Phase, iteration: int, c: ErrorCluster, cand, rationale: str,
                          target: Optional[str] = None) -> list[BuildError]:
        """``cand``에 든 오류를 차단 항목으로 등록하고 오류 목록에서도 그 케이스를 차단 처리한다.
        javax나 외부 API 오류면 classpath에서 라이브러리를 찾아 조치 방법 문구를 만든다.
        차단하지 않은 나머지 오류를 반환한다. 이 오류는 계속 고친다."""
        key = self.blocked.key
        covered = {key(e) for e in (cand.errors or c.errors)}
        errs = [e for e in c.errors if key(e) in covered] or list(c.errors)
        rest = [e for e in c.errors if key(e) not in covered]
        attempts = len(_same_cluster(c, self.history, self.blocked.canonical_file))
        reason = cand.reason
        artifact: Optional[GAV] = None
        owner = cand.owner_class
        notes: list[str] = []
        required, found = cand.required_type, cand.found_type
        if reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
            owner, required, found = self._classpath_hints(errs, owner, required, found)
            artifact, owner, notes = attribute(self.locator, self.ws, self.cfg, owner, errs, c.module or target)
        elif reason in (BlockReason.ARTIFACT_NOT_FOUND, BlockReason.PARENT_NOT_FOUND, BlockReason.REPOSITORY_UNREACHABLE):
            artifact = next((e.artifact for e in errs if e.artifact is not None), None)
        elif reason == BlockReason.NEEDS_OWNER:
            # javax 라이브러리 거절이나 없는 아티팩트 케이스도 인수인계 표에 그 라이브러리가 남도록 한다
            artifact = next((e.artifact for e in errs if e.artifact is not None), None)
            if artifact is None and _mentions_javax(errs):
                owner, required, found = self._classpath_hints(errs, owner, required, found)
                artifact, owner, notes = attribute(self.locator, self.ws, self.cfg, owner, errs, c.module or target)
        elif reason == BlockReason.REACTOR_DUPLICATE:
            # Maven 메시지에서 찾은 중복 프로젝트 좌표를 차단 표의 아티팩트 칸에 쓴다
            from sbmig.triage.blocked import errors_text, reactor_duplicate_coordinates
            coords = reactor_duplicate_coordinates(errors_text(errs))
            try:
                artifact = GAV.parse(coords) if coords else None
            except ValueError:
                artifact = None
        elif getattr(cand, "plugin", None):
            try:
                artifact = GAV.parse(cand.plugin)
            except ValueError:
                artifact = None
        item, created = self.blocked.block(c, reason, phase, errors=errs, iteration=iteration, attempts=attempts,
                                           required_type=required, found_type=found,
                                           owner_class=owner, artifact=artifact, notes=[rationale, *notes])
        if getattr(cand, "suggestion", ""):          # 사람이 케이스에 고정해 둔 `block` 조치 문구
            item.suggestion = cand.suggestion
        elif created or not item.suggestion:
            extra = ""
            if reason == BlockReason.PLUGIN_INCOMPATIBLE:
                extra = getattr(cand, "plugin", None) or ""
            elif reason == BlockReason.LLM_UNAVAILABLE:
                extra = self.llm_unavailable or ""
            item.suggestion = compose_suggestion(item, self.cfg, settings_file=self.cfg.build.settings_file,
                                                 repositories=list(getattr(cand, "repositories", []) or []),
                                                 extra=extra)
            if reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE, BlockReason.NEEDS_OWNER):
                # jakarta 빌드가 없다는 문장과 그 이유가 되는 EOL 권고를 붙인다
                item.suggestion = self._with_eol_advisory(item.suggestion, artifact)
        # 프레임워크 프로젝트에서 javax 라이브러리가 프레임워크 아티팩트면 소유팀이 jakarta 빌드를 내야 한다고 안내한다
        fw = self._framework_blocked_artifact(c, reason, artifact)
        if fw is not None:
            item.suggestion = framework_owner_suggestion(fw.coordinates if fw.version else fw.key, item.required_type)
            if item.artifact is None or is_javax_api_artifact(item.artifact):
                item.artifact = fw                       # 마지막 fill_via 단계는 이 항목을 건드리지 않는다
        if fw is None and reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.NEEDS_OWNER) and _mentions_javax(errs):
            # 어디를 찾아봤는지 볼 수 있도록 탐색 기록을 조치 방법 뒤에 붙이고 메모에도 남긴다
            # 의존성을 찾지 못한 클래스는 찾지 못했다고 함께 기록한다
            evidence = self._jakarta_evidence(c, owner, artifact)
            if evidence:
                if evidence not in item.suggestion:
                    item.suggestion = f"{item.suggestion}; {evidence}" if item.suggestion else evidence
                if evidence not in item.notes:
                    item.notes.append(evidence)
        self._touch_item(item.id)
        case_id = c.case_id or _case_of_errors(errs)
        if case_id:
            self.errors.block(case_id, f"{reason.value}: {_first_sentence(item.suggestion or rationale, 300)}")
        log.info("blocked (%s): %s → Handover", reason.value,
                 _first_sentence(rationale.split(" — ", 1)[-1] if " — " in rationale else rationale, 240))
        self.ws.journal({"event": "blocked", "phase": phase.value, "iteration": iteration, "id": item.id,
                         "created": created, "reason": reason.value, "cluster": c.id,
                         "signatures": [e.signature for e in errs][:10], "files": item.files[:10],
                         "artifact": item.artifact.coordinates if item.artifact else None,
                         "owner_class": item.owner_class, "required_type": item.required_type,
                         "found_type": item.found_type, "attempts": item.attempts, "suggestion": item.suggestion,
                         "rest_errors": len(rest)})
        return rest

    def _framework_blocked_artifact(self, c: ErrorCluster, reason: BlockReason, artifact: Optional[GAV]) -> Optional[GAV]:
        """차단할 javax 케이스의 라이브러리가 프레임워크 아티팩트면 그 좌표를, 아니면 None을 반환한다."""
        if not self._reduced_scope() or reason not in (BlockReason.EXTERNAL_JAVAX_API,
                                                       BlockReason.EXTERNAL_API_INCOMPATIBLE, BlockReason.NEEDS_OWNER):
            return None
        fw = self._framework_blocks.get(c.id)
        if fw is None and artifact is not None and self._is_framework_key(artifact.key):
            fw = artifact
        return fw

    def _classpath_hints(self, errs: list[BuildError], owner: Optional[str], required: Optional[str],
                         found: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """비어 있는 소유 클래스, 요구 타입, 실제 타입을 classpath 도구로 채운다. 도구에 그 기능이 있을 때만 쓴다.

        sbmig.build.classpath.owner_fqcn / type_mismatch."""
        loc = self.locator
        if loc is None:
            return owner, required, found
        for e in errs[:3]:
            try:
                if owner is None and hasattr(loc, "owner_fqcn"):
                    o = loc.owner_fqcn(e)
                    if o and not o.startswith(("javax.", "jakarta.")):   # 타입 불일치만 있으면 타입 이름이 나온다
                        owner = o
                if (required is None or found is None) and hasattr(loc, "type_mismatch"):
                    r, f = loc.type_mismatch(e)
                    required, found = required or r, found or f
            except Exception as ex:  # pragma: no cover - 실패해도 계속한다
                log.debug("classpath hints failed: %s", ex)
        return owner, required, found

    def _block_simple(self, phase: Phase, iteration: int, c: ErrorCluster, reason: BlockReason, why: str) -> None:
        """아직 차단되지 않은 오류만 주어진 사유로 차단한다.
        예산 소진, LLM 사용 불가, 진동처럼 루프가 직접 정하는 차단에 쓴다."""
        from sbmig.triage.blocked import BlockedCandidate
        _blocked, rest = self.blocked.partition(c)
        if not rest:
            return
        cc = _reduced(c, rest)
        self._register_blocked(phase, iteration, cc, BlockedCandidate(reason=reason, evidence=[why], errors=rest),
                               f"blocked: {reason.value} — {why}")

    def _block_remaining(self, pr: PhaseResult, phase: Phase, iteration: int, clusters: list[ErrorCluster],
                         reason: BlockReason, why: str) -> None:
        for c in clusters:
            self._block_simple(phase, iteration, c, reason, why)

    def _block_oscillation(self, pr: PhaseResult, phase: Phase, iteration: int, clusters: list[ErrorCluster]) -> None:
        """진동이 반복되면 이 단계에서 실제로 시도한 클러스터만 OSCILLATION으로 차단한다.
        L1 서명이 같을 뿐인 다른 클러스터는 차단하지 않는다."""
        involved = [c for c in clusters if _same_cluster(c, pr.attempts, self.blocked.canonical_file)] or clusters
        why = (f"fixes kept toggling between the same error sets (A↔B↔A) {int(getattr(self.cfg.loop, 'oscillation_limit', 2) or 2)} "
               "time(s) within loop.oscillation_window (loop.oscillation_limit)")
        pr.notes.append(f"build {iteration}: oscillation repeated → blocking {len(involved)} cluster(s)")
        self._block_remaining(pr, phase, iteration, involved, BlockReason.OSCILLATION, why)

    @staticmethod
    def build_text(build: Any) -> str:
        """실패한 빌드나 OpenRewrite 실행의 오류 메시지, 원문, 요약, 로그 파일 내용을 합쳐 반환한다."""
        parts = [f"{e.message}\n{e.raw or ''}" for e in (getattr(build, "errors", None) or [])]
        parts += [str(getattr(build, "summary", "") or ""), str(getattr(build, "error", "") or "")]
        lp = getattr(build, "log_path", "") or ""
        if lp:
            try:
                p = Path(lp)
                if p.is_file():
                    parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return "\n".join(parts)

    def http_blocked_abort(self, pr: Optional[PhaseResult], phase: Phase, build: Any, iteration: int, *,
                           where: str = "build") -> bool:
        """실패한 Maven 실행에 HTTP 차단의 증거가 있으면 실행을 멈추고 True를 반환한다. 증거가 없으면 False다.
        증거는 ``Blocked mirror for repositories`` 문구나 ``http://0.0.0.0``이다. 미러 id만으로는 판단하지 않는다.
        멈출 때 플래그 ``aborted:http-blocked``, 저널 ``maven_http_blocked``, ERROR 한 줄, 인수인계 한 줄을 남긴다.

        Aborts once (an already aborted run answers True again). The journal lists the repositories, never
        credentials. No parent alignment, zero-LLM pre-try or LLM call can change it."""
        from sbmig.build.env_errors import HTTP_BLOCKED_FLAG, http_blocked_followup, http_blocked_reason, http_blocker_hit
        if build is None or getattr(build, "ok", False):
            return False
        hit = http_blocker_hit(self.build_text(build))
        if hit is None:
            return False
        if self.aborted:
            return True
        self.aborted = f"{http_blocked_reason(hit)} — run aborted: fix the Maven settings (see the Handover)"
        settings = self.cfg.build.settings_file or "~/.m2/settings.xml (default)"
        log.error("%s; Maven settings: %s", self.aborted, settings)
        self.ws.journal({"event": "maven_http_blocked", "phase": phase.value, "iteration": iteration, "where": where,
                         **hit.as_json(), "settings": settings,
                         "log": getattr(build, "log_path", None) or None})
        self._flag(HTTP_BLOCKED_FLAG)
        self._followup(http_blocked_followup(hit, self.cfg))
        if pr is not None:
            pr.notes.append(f"{where} {iteration}: {self.aborted}")
        self.ws.journal({"event": "abort", "phase": phase.value, "iteration": iteration, "reason": self.aborted})
        return True

    def _parent_abort(self, pr: PhaseResult, phase: Phase, build: BuildResult, iteration: int) -> bool:
        """설정한 사내 부모 pom을 찾지 못하면 실행을 멈추고 True를 반환한다. 아무것도 빌드할 수 없기 때문이다.
        settings, Nexus, ``artifacts.parent``를 확인하라는 차단 항목을 남긴다. 코드로 풀 수 없어 LLM은 호출하지 않는다.

        Parents = root parent + boot_parent / comp_parent."""
        if self._reduced_scope():
            return self._framework_parent_abort(pr, phase, build, iteration)
        keys = set(self.cfg.artifacts.parent_keys())
        # 설정 버전이 아닌 as-is 버전을 찾지 못한 경우는 사전 시도 sbmig-parent가 맞추므로 멈추지 않는다
        # 설정 버전 자체이거나 버전을 모를 때, 또는 openrewrite.unresolvable_asis_parent: abort일 때만 멈춘다
        errs = [e for e in build.errors if e.kind == ErrorKind.PARENT_RESOLUTION
                and e.artifact is not None and e.artifact.key in keys and self._parent_miss_aborts(e)]
        # 사내 부모가 <build><extensions>에 선언한 확장(wagon-webdav 등)을 Maven이 받지 못한 경우다
        # 상속된 확장은 프로젝트에서 뺄 수 없고 어떤 goal도 실행되지 않으므로 LLM 없이 멈춘다
        ext_errs = self._inherited_extension_errors(build) if not errs else []
        if not errs and not ext_errs:
            return False
        from sbmig.triage.blocked import BlockedCandidate, repositories_in
        target = errs or ext_errs
        c = self.triager.cluster(target, self.profile)[0]
        coords = target[0].artifact.coordinates
        cand = BlockedCandidate(reason=BlockReason.PARENT_NOT_FOUND, errors=target,
                                repositories=repositories_in("\n".join(f"{e.message}\n{e.raw}" for e in target)),
                                evidence=[target[0].message[:300]])
        if errs:
            from sbmig.restructure.asis_parent import classify_gav
            miss = classify_gav(target[0].artifact, self.cfg, self.ws)
            settings = self.cfg.build.settings_file or "default"
            if miss.kind == "configured":
                coords = miss.coordinates
                self._register_blocked(phase, iteration, c, cand, f"blocked: parent_not_found — configured parent {coords} "
                                                                  "is not resolvable; run aborted")
                self.aborted = (f"Maven cannot resolve the parent {coords}, which is the configured version "
                                f"({miss.spec_name} = {miss.target.coordinates if miss.target else coords}) — the repository "
                                f"does not serve it (settings: {settings}); fix the repository / settings, or the version "
                                f"in {miss.spec_name}, and rerun")
            else:                                           # `unresolvable_asis_parent: abort`일 때의 as-is 버전
                self._register_blocked(phase, iteration, c, cand, f"blocked: parent_not_found — as-is parent {coords} "
                                                                  "is not resolvable; run aborted "
                                                                  "(openrewrite.unresolvable_asis_parent: abort)")
                self.aborted = (f"Maven cannot resolve the as-is parent {coords}; the configured version is "
                                f"{miss.target.coordinates if miss.target else '?'} ({miss.spec_name}) — the run stops because "
                                f"openrewrite.unresolvable_asis_parent is abort (settings: {settings}); set it to align "
                                "to use the configured version instead")
        else:
            declared = self._extension_declarer(coords)
            cand.suggestion = (f"build extension `{coords}` declared by the company parent `{declared}` cannot be downloaded: make "
                               "the repository serve its pom, jar, parent poms and dependencies, or drop the extension from the parent "
                               "(a deploy transport is not needed for packaging); nothing in the project can change an inherited extension")
            self._register_blocked(phase, iteration, c, cand, f"blocked: parent_not_found — build extension {coords} "
                                                              f"inherited from the company parent {declared} cannot be "
                                                              "downloaded; run aborted (nothing in the project can fix it)")
            self.aborted = (f"the company parent {declared} declares the build extension {coords}, which Maven cannot "
                            f"download from the repository (settings: {self.cfg.build.settings_file or 'default'}) — every "
                            "Maven call fails before any goal; make Nexus serve its pom, jar, parent poms and dependencies "
                            "(or drop the extension from the parent: a deploy transport is not needed for packaging) and rerun")
        self._flag("aborted:parent-not-found")
        self._followup(f"run aborted: {self.aborted}")
        pr.notes.append(f"build {iteration}: {self.aborted}")
        self.ws.journal({"event": "abort", "phase": phase.value, "iteration": iteration, "reason": self.aborted})
        return True

    def _framework_parent_abort(self, pr: PhaseResult, phase: Phase, build: BuildResult, iteration: int) -> bool:
        """프레임워크 프로젝트에서 부모 pom을 찾지 못하면 LLM을 호출하지 않고 실행을 멈춘다.
        프로젝트가 자기 부모를 그대로 써야 하므로 사내 부모가 아니어도 멈춘다. 프로젝트 안의 모듈인 부모는 수정 루프에 맡긴다.

        No `sbmig-parent` alignment in a reduced run: nothing in the project may replace its parent."""
        own = self._own_module_keys()
        errs = [e for e in build.errors if e.kind == ErrorKind.PARENT_RESOLUTION and e.artifact is not None
                and e.artifact.group_id and e.artifact.artifact_id and e.artifact.key not in own]
        if not errs:
            return False
        from sbmig.triage.blocked import BlockedCandidate, repositories_in
        c = self.triager.cluster(errs, self.profile)[0]
        coords = errs[0].artifact.coordinates
        settings = self.cfg.build.settings_file or "default"
        fw = self._framework_label()
        cand = BlockedCandidate(reason=BlockReason.PARENT_NOT_FOUND, errors=errs,
                                repositories=repositories_in("\n".join(f"{e.message}\n{e.raw}" for e in errs)),
                                evidence=[errs[0].message[:300]])
        cand.suggestion = (f"parent `{coords}` is not resolvable with settings `{settings}`: this is a framework project "
                           f"({fw}) and keeps its own parent — make the repository serve it (or fix the settings / mirror) "
                           "and rerun; nothing in the project can replace it")
        self._register_blocked(phase, iteration, c, cand, f"blocked: parent_not_found — parent {coords} of a framework "
                                                          "project is not resolvable; run aborted")
        # 변환 단계의 중단 사유(Orchestrator._reduced_parent_abort)와 같은 문장이라 화면의 한국어 번역 규칙 하나로 맞는다
        self.aborted = (f"Maven cannot resolve the parent pom {coords}; a framework project keeps its own parent pom, so "
                        f"the repository must serve it (settings: {settings}). Make the repository serve it and rerun")
        self._flag("aborted:parent-not-found")
        self._followup(f"run aborted: {self.aborted}")
        pr.notes.append(f"build {iteration}: {self.aborted}")
        self.ws.journal({"event": "abort", "phase": phase.value, "iteration": iteration, "reason": self.aborted})
        return True

    def _parent_miss_aborts(self, e: BuildError) -> bool:
        """사내 부모 g:a를 찾지 못한 오류로 실행을 멈춰야 하면 True를 반환한다.
        설정 버전이거나 분류에 실패하면 멈춘다. as-is 버전이면 ``openrewrite.unresolvable_asis_parent: abort``일 때만 멈춘다."""
        try:
            from sbmig.restructure.asis_parent import classify_gav
            kind = classify_gav(e.artifact, self.cfg, self.ws).kind
        except Exception as ex:  # noqa: BLE001 - 분류하지 못하면 멈춘다
            log.debug("parent miss classification failed: %s", ex)
            return True
        if kind == "asis-version":
            return getattr(self.cfg.openrewrite, "unresolvable_asis_parent", "align") == "abort"
        return kind == "configured"

    def _chain_extensions(self) -> dict[str, str]:
        """사내 부모 체인이 선언한 빌드 확장을 ``{g:a: g:a:v}`` 형태로 반환한다. 읽지 못하면 빈 dict를 반환한다.
        확장을 선언한 pom은 ``_extension_declarer``가 쓰도록 기억해 둔다."""
        try:
            from sbmig.build import parent_chain
            facts = parent_chain.load_parent_chain_facts(self.cfg)
            out: dict[str, str] = {}
            for coord in getattr(facts, "build_extensions", []) or []:
                parts = coord.split(":")
                if len(parts) >= 2:
                    out[f"{parts[0]}:{parts[1]}"] = coord
            self._chain_extension_sources = dict(getattr(facts, "build_extension_sources", {}) or {})
            return out
        except Exception as e:  # noqa: BLE001 - no chain facts: no abort on this ground
            log.debug("parent chain extensions unavailable: %s: %s", type(e).__name__, e)
            return {}

    def _extension_declarer(self, coords: str) -> str:
        srcs = getattr(self, "_chain_extension_sources", {}) or {}
        return srcs.get(coords) or next((v for k, v in srcs.items() if k.rsplit(":", 1)[0] == coords.rsplit(":", 1)[0]), "?")

    def _inherited_extension_errors(self, build: BuildResult) -> list[BuildError]:
        """이 빌드의 `Unresolveable build extension` 오류 중 사내 부모 체인이 선언한 확장의 오류를 찾아 반환한다."""
        cands = [e for e in build.errors if e.kind == ErrorKind.POM_INVALID and e.artifact is not None
                 and "build extension" in (e.message or "").lower()]
        if not cands or self._reduced_scope():         # 프레임워크 프로젝트는 사내 부모 체인을 상속하지 않는다
            return []
        chain = self._chain_extensions()
        # g:a:v까지 같아야 한다. 프로젝트 pom이 다른 버전으로 다시 선언한 확장은 프로젝트의 것이라 LLM 케이스로 남긴다
        # Maven은 확장을 g:a로 합치고 자식 pom의 선언이 이긴다
        return [e for e in cands if chain.get(e.artifact.key) == e.artifact.coordinates] if chain else []

    def skips_for(self, target: Optional[str]) -> tuple[bool, bool]:
        """``target`` 빌드에 적용할 (test-compile 건너뜀, 테스트 실행 건너뜀) 여부를 반환한다.
        서비스 빌드는 자기 서비스의 대체 빌드 결과만, 프로젝트 전체 빌드는 모든 서비스의 결과를 물려받는다.
        MSA 서비스 하나의 대체 빌드가 다른 서비스의 최종 빌드를 바꾸면 안 되기 때문이다.

        A loop on the whole project ("") applies to every target; a whole-project build (target None) inherits
        any service's fallback because its reactor contains that service."""
        t = _target_key(target)
        tc, ts = self.test_compile_skipped_targets, self.tests_skipped_targets
        if not t:
            return bool(tc), bool(ts)
        return (t in tc or "" in tc), (t in ts or "" in ts)

    @staticmethod
    def test_compile_skipped_in(pr: PhaseResult) -> bool:
        """이 단계가 test-compile을 건너뛴 대체 빌드로 통과했으면 True를 반환한다. 단계 메모로 판단한다."""
        return any("test-compile-skipped" in n for n in pr.notes)

    @staticmethod
    def tests_skipped_in(pr: PhaseResult) -> bool:
        """이 단계가 테스트 실행을 건너뛴 -DskipTests 대체 빌드로 통과했으면 True를 반환한다."""
        return any("tests-skipped:" in n for n in pr.notes)

    # ------------------------------------------------------------------ 빌드
    def _build(self, goal: str, target: Optional[str], label: str, phase: Phase, iteration: int,
               skip_test_compile: bool = False, skip_tests: bool = False) -> BuildResult:
        """루프의 빌드를 한 번 실행한다. 설정에 따라 ``-Dmaven.test.skip=true``를 붙인다.
        빌드 부산물을 치우고 ``refine_build``로 케이스 키를 다듬는다.
        그래야 시도 결과 평가, 오류 목록, 클러스터, 차단 목록이 같은 L1 / L2 서명을 본다."""
        module, extra = build_target_args(self.profile, target)
        skip_test_compile = skip_test_compile or bool(getattr(self.cfg.build, "skip_test_compile", False))
        kw: dict[str, Any] = {"module": module, "label": label, "skip_test_compile": skip_test_compile}
        if extra:
            kw["extra_args"] = extra
        before = _status_paths(self.ws)
        with self._busy_with(f"mvn {goal} ({label})"):
            if skip_tests:
                # skip_tests를 받지 않는 이전 빌드 실행기에는 -DskipTests 인자로 넘긴다
                # 빌드 안에서 난 TypeError로 빌드를 두 번 실행하지 않도록 호출 전에 시그니처로 판단한다
                if takes_keyword(self.build_runner.run, "skip_tests"):
                    res = self.build_runner.run(goal, **kw, skip_tests=True)
                else:
                    kw["extra_args"] = [*extra, "-DskipTests"]
                    res = self.build_runner.run(goal, **kw)
            else:
                res = self.build_runner.run(goal, **kw)
        _restore_build_byproducts(self.ws, before)
        refine_build(res)
        self.ws.journal({"event": "build", "phase": phase.value, "iteration": iteration, "goal": goal,
                         "target": target, "extra_args": extra, "ok": res.ok, "exit_code": res.exit_code, "errors": res.error_count,
                         "duration_s": res.duration_s, "log": res.log_path, "skip_test_compile": skip_test_compile,
                         "skip_tests": skip_tests})
        return res

    # ------------------------------------------------------------------ 시도 결과 평가
    def _score(self, pr: PhaseResult, pending: list[_Pending], build: BuildResult,
               prev: Optional[BuildResult], restored: Optional[list[BuildError]] = None) -> bool:
        """직전 라운드의 시도를 이 빌드 결과로 평가한다. 되돌려서 트리가 바뀌었으면 True를 반환한다.
        - improved: 대상 오류가 모두 사라지고 빌드가 그 단계까지 갔다
        - partial: 일부만 사라지고 수정한 파일에 새 오류가 없다
        - regressed: 수정한 파일에 새 오류가 생겼거나, 오류 수가 ``regression_ratio``배를 넘었거나,
          pom 수정 뒤 새 구조적 오류나 더 이른 단계의 오류가 대상을 가렸다. 되돌린다
        - inconclusive: 타임아웃이나 읽은 오류가 없는 실패처럼 빌드로 판단할 수 없다. KB 실적 없이 다시 평가한다

        `restored` = the errors that round's rollbacks brought back: part of the baseline (`prev`), so they
        are never "new errors in a touched file" nor counted by the ×regression_ratio rule.

        Outcomes: `build_ok`; `improved` = every targeted error gone AND the build reached the stage that reports them;
        `partial` = some gone, the rest remain, nothing new in the touched files; `no_change`; `regressed` = new
        errors in a touched file (by (file, signature) occurrences), the ×regression_ratio rule, or — after a pom
        edit — a new structural error kind / new errors of an earlier stage that hide the targeted ones (reverted);
        `inconclusive` = the build cannot tell (timed out, failed without parsed errors, stopped at an earlier stage
        this attempt did not cause): no KB credit, scored again on a revert rebuild.

        Trade-back ("v2.1z-bz", `loop.trade_back: revert`; the default is `observe`): an attempt that would be
        `improved` is `regressed` when a touched file shows again (new against `prev` + `restored`) an error
        that an earlier `improved` attempt of ANOTHER case had resolved and that is still in the tree
        (`_resolved_by`); it is reverted like a structural regression (also behind a later kept commit, `git
        revert`), counted per pair of cases and journaled (`trade_back`, `_settle_trade_backs`). `off` = no
        trade-back check."""
        if not pending:
            return False
        real = [e for e in build.errors if not _synthetic(e)]
        cur_count = Counter(_pair(e) for e in build.errors)
        restored = list(restored or [])
        prev_errors = (list(prev.errors) if prev is not None else []) + restored
        prev_count = Counter(_pair(e) for e in prev_errors)
        base_total = (prev.error_count if prev is not None else 0) + len(restored)
        total = build.error_count
        ratio = self.cfg.loop.regression_ratio
        struct_kinds = set(self.cfg.loop.structural_regression_kinds)
        prev_kinds = {e.kind.value for e in prev_errors}
        new_struct = sorted({e.kind.value for e in real if e.kind.value in struct_kinds} - prev_kinds)
        # 타임아웃이거나 읽은 오류가 없는 실패 빌드로는 어떤 시도도 판단할 수 없으므로 모두 판단 불가로 둔다
        blind = not build.ok and (bool(build.timed_out) or not real)
        structural: set[str] = set()
        tb_mode = self._trade_back_mode()
        trade_on = tb_mode != "off"
        observed: dict[str, list[tuple[BuildError, FixAttempt]]] = {}  # observe 모드에서 trade-back이었을 시도
        traded: dict[str, list[tuple[BuildError, FixAttempt]]] = {}   # 시도 id: (되살아난 오류, 그 오류를 없앤 시도)
        unrevertable: set[str] = set()     # git revert에 실패한 trade-back. 실패 메모만 남긴다
        left_of: dict[int, tuple[int, int]] = {}
        for p in pending:
            a = p.attempt
            a.errors_after = total
            if build.ok:
                a.outcome, a.cluster_resolved = "build_ok", True
                n = len(a.cluster_errors) or 1
                self._tally[id(a)] = (n, n)
                continue
            left, targeted = _targeted_left(a, cur_count)
            left_of[id(a)] = (left, targeted)
            self._tally[id(a)] = (targeted - left, targeted)
            # True: 사라졌고 빌드가 보여 줄 수 있었다. None: 사라졌지만 빌드가 그 단계까지 못 갔다. False: 남아 있다
            a.cluster_resolved = (False if left else
                                  True if not blind and _visible_in(a.cluster_errors, build, real) else None)
        any_resolved = any(p.attempt.cluster_resolved for p in pending)
        for p in pending:
            a = p.attempt
            if build.ok:
                continue
            if blind:
                a.outcome, a.cluster_resolved = "inconclusive", None
                a.notes.append("inconclusive: the build " + ("timed out" if build.timed_out else
                               "failed without parseable errors") + " — it cannot show whether this attempt worked "
                               "(no KB credit)")
                continue
            touched = _touched(a)
            pom_touched = any(t.rsplit("/", 1)[-1] == "pom.xml" for t in touched)
            # pom을 고친 시도 뒤에 새 구조적 오류(pom 무효, 의존성 해결 실패 등)가 나타나면 오류 수와 상관없이 되돌린다
            uncovered = self._uncovered_stage(a, left_of[id(a)], real, new_struct) if new_struct and pom_touched else []
            if uncovered:
                # pom 문제를 고치면 빌드가 다음 단계로 가서 가려져 있던 "Could not find artifact" 같은 오류가 처음 보일 수 있다
                # 대상 오류가 모두 사라졌고 새 구조적 오류가 모두 더 뒤 단계에 있으면 악화로 보지 않는다
                kinds = ", ".join(sorted({e.kind.value for e in uncovered}))
                a.notes.append(f"the build advanced past the fixed pom problem: {len(uncovered)} {kinds} error(s) of a later "
                               "stage were hidden behind it — not a regression of this attempt")
            elif new_struct and pom_touched:
                a.outcome = "regressed"
                a.notes.append(f"regressed: structural error kind(s) {', '.join(new_struct)} appeared after this "
                               "attempt edited a pom.xml → reverted regardless of error counts")
                a.introduced_errors = sanitize_errors([e for e in real if e.kind.value in new_struct], 10)
                structural.add(a.id)
                continue
            left, targeted = left_of[id(a)]
            if a.cluster_resolved is None:
                # 대상 오류가 이 빌드에 없지만 빌드가 그 단계까지 가지 못했다
                stage = _stage_of(a.cluster_errors)
                earlier = [e for e in _new_errors(real, prev_count)
                           if (error_stage(e) if error_stage(e) is not None else 99) < stage]
                if pom_touched and earlier:
                    a.outcome = "regressed"
                    kinds = ", ".join(sorted({e.kind.value for e in earlier}))
                    a.notes.append(f"regressed: new {kinds} error(s) appeared after this attempt edited a pom.xml and "
                                   "hide the targeted errors → reverted")
                    a.introduced_errors = sanitize_errors(earlier, 10)
                    structural.add(a.id)
                    continue
                a.outcome, a.cluster_resolved = "inconclusive", None
                a.notes.append("inconclusive: the build stopped before the stage of the targeted errors ("
                               + ", ".join(sorted({e.kind.value for e in real})[:4]) + " first) — whether they are "
                               "gone is unknown (no KB credit)")
                continue
            # 다른 파일에 같은 L1 서명이 있어도 수정한 파일에 생긴 오류는 새 오류이므로 (파일, 서명) 횟수로 센다
            new_in_touched = _new_in_files(build.errors, touched, prev_count)
            before = max(a.errors_before, base_total) if restored else a.errors_before
            ratio_regress = (not any_resolved) and total > max(before, 1) * ratio
            if not a.cluster_resolved and (new_in_touched or ratio_regress):
                a.outcome = "regressed"
                a.introduced_errors = sanitize_errors(new_in_touched, 10)   # 보강 프롬프트에 악화 이유로 넣는다
                why = (f"{len(new_in_touched)} new error(s) in touched files" if new_in_touched
                       else f"errors {before} → {total} (> ×{ratio})")
                a.notes.append(f"regressed: {why}")
            elif a.cluster_resolved:
                # 대상 오류는 사라졌지만 다른 케이스의 시도가 해결해 둔 오류를 수정한 파일에 되살렸으면 악화로 본다
                # 그 시도의 변경을 이 시도가 되돌린 경우만이다(diff 줄 비교). 같은 종류라도 새로 생긴 후속 오류는 악화로 보지 않는다
                back = self._trade_back_of(a, p, new_in_touched) if trade_on else []
                names = ", ".join(dict.fromkeys(f"attempt {r.id} (case {_case_key(r)})" for _e, r in back))
                files = ", ".join(dict.fromkeys(_pair(e)[0] for e, _r in back))
                if back and tb_mode == "revert":
                    a.outcome = "regressed"
                    a.introduced_errors = sanitize_errors([e for e, _r in back], 10)
                    a.notes.append(f"regressed: trade-back — this attempt put back the lines {names} had changed in "
                                   f"{files} and {len(back)} error(s) it had resolved are on them again "
                                   "(loop.trade_back)")
                    traded[a.id] = back
                else:
                    a.outcome = "improved"
                    if back:                       # observe 모드는 결과만 기록하고 평가는 바꾸지 않는다
                        a.notes.append(f"trade-back observed (loop.trade_back: observe, scored as before): this attempt "
                                       f"put back the lines {names} had changed in {files} and {len(back)} error(s) "
                                       "it had resolved are on them again")
                        observed[a.id] = back
            elif 0 < left < targeted:
                a.outcome = "partial"
                a.notes.append(f"partial: {targeted - left} of {targeted} targeted error(s) gone, {left} remain")
            else:
                a.outcome = "no_change"
        # 수정 스크립트의 실적은 아래 되돌리기로 결과가 정해진 뒤 호출하는 쪽이 기록한다
        reverted = False
        # 뒤의 개선된 커밋이 앞 커밋에 의존할 수 있으므로 HEAD부터 연속으로 악화된 커밋 묶음만 reset한다
        # 구조적 악화는 예외로, 뒤에 커밋이 있어도 git revert로 되돌린다
        if not build.ok and self.cfg.loop.revert_on_regression and any(p.attempt.outcome == "regressed" for p in pending):
            commits: list[str] = []
            for p in pending:
                if p.post_sha not in commits:
                    commits.append(p.post_sha)
            for sha in reversed(commits):
                group = [p for p in pending if p.post_sha == sha]
                if not all(p.attempt.outcome == "regressed" for p in group):
                    break
                self.ws.revert_to(group[0].pre_sha)
                reverted = True
                for p in group:
                    p.attempt.outcome = "reverted"
                    p.attempt.notes.append(f"reverted to {group[0].pre_sha[:10]}")
                self.ws.journal({"event": "revert", "phase": pr.phase.value, "to": group[0].pre_sha,
                                 "commit": sha, "attempts": [p.attempt.id for p in group]})
            # trade-back도 뒤 커밋이 의존해도 git revert로 되돌린다. 남겨 두면 다른 케이스의 오류가 계속 살아 있다
            for p in pending:
                a = p.attempt
                if a.outcome != "regressed" or (a.id not in structural and a.id not in traded):
                    continue
                what = "structural" if a.id in structural else "trade-back"
                group = [q for q in pending if q.post_sha == p.post_sha]
                res = self.ws._git("revert", "--no-edit", p.post_sha, check=False)   # noqa: SLF001
                if res.returncode != 0:
                    self.ws._git("revert", "--abort", check=False)                      # noqa: SLF001
                    a.notes.append(f"{what} revert of {p.post_sha[:10]} failed: {(res.stderr or '').strip()[:200]}")
                    if what != "structural":
                        unrevertable.add(a.id)     # 남는 이유는 실패 메모가 설명한다
                    continue
                reverted = True
                for q in group:
                    q.attempt.outcome = "reverted"
                    q.attempt.notes.append(f"reverted commit {p.post_sha[:10]} (git revert: "
                                           + ("structural regression)" if what == "structural" else "trade-back)"))
                ev = {"event": "revert", "phase": pr.phase.value, "commit": p.post_sha, "mode": "git-revert",
                      "structural": new_struct if what == "structural" else [],
                      "attempts": [q.attempt.id for q in group]}
                if what != "structural":
                    ev["trade_back"] = True
                self.ws.journal(ev)
            for p in pending:
                if p.attempt.outcome == "regressed" and p.attempt.id not in unrevertable:
                    p.attempt.notes.append("not reverted: later commits that improved things depend on it")
        if trade_on:
            self._settle_trade_backs(pr, pending, traded, build, prev_count, observed=observed)
        # 남은 시도가 수정한 파일에 새로 생긴 오류는 그 시도와 연결해 프롬프트에 "introduced by script:<case>:v<N>"으로 알린다
        # 이 빌드에 없는 오류의 기록은 지운다. 나중에 다른 시도 때문에 다시 생기면 그 시도를 가리키게 하기 위해서다
        # 같은 평가 안에서는 앞선 대기 시도가 그 오류를 차지한다
        present = {_pair(e) for e in build.errors} if not build.ok else set()
        for k in [k for k in self._introduced if k not in present]:
            del self._introduced[k]
        if not build.ok:
            claimed: set[tuple[str, str]] = set()
            for p in pending:
                a = p.attempt
                if a.outcome == "reverted" or not a.applied:
                    continue
                for e in _new_in_files(build.errors, _touched(a), prev_count):
                    k = _pair(e)
                    if k not in claimed:
                        claimed.add(k)
                        self._introduced[k] = a
        for p in pending:
            self.ws.journal({"event": "attempt_scored", "phase": pr.phase.value, "attempt": p.attempt.id,
                             "cluster": p.attempt.cluster_id, "outcome": p.attempt.outcome,
                             "cluster_resolved": p.attempt.cluster_resolved, "errors_after": total})
        return reverted

    # ------------------------------------------------------------------ trade-back
    def _trade_back_mode(self) -> str:
        """``loop.trade_back`` 값을 반환한다. observe / revert / off 중 하나이고 알 수 없는 값은 observe로 본다.

        `observe` (default): the verdict is journaled, nothing else changes."""
        try:
            v = str(getattr(self.cfg.loop, "trade_back", "observe") or "observe").strip().lower()
        except Exception:  # noqa: BLE001 - a strange setting means the default
            return "observe"
        return v if v in ("observe", "revert", "off") else "observe"

    def _trade_back_of(self, a: FixAttempt, p: _Pending,
                       new_in_touched: list[BuildError]) -> list[tuple[BuildError, FixAttempt]]:
        """이 시도가 다른 케이스의 시도 R이 고친 줄을 되돌려 R이 없앤 오류 E가 다시 생겼으면 (E, R) 목록을 반환한다.
        줄 번호가 없는 오류나 읽지 못한 diff는 판단하지 않는다.

        "v2.1z-bz", the four conditions of the contract for one error E of `new_in_touched`:
        1. E is back: E's (file, signature) pair was resolved by an earlier `improved` attempt R of ANOTHER case that is
           still in the tree (`_resolved_by`), and E is new in `a`'s touched files (`_new_in_files`: a rollback's
           `restored` errors are baseline);
        2. a hunk H of R's `-U0` diff in that file (`R.base_commit..R.commit`) covers, on its OLD side, a line the build
           before R reported the pair on (`_resolved_lines`; `_covers`);
        3. ONE hunk G of `a`'s own diff (`p.pre_sha..p.post_sha`) reverses H (`_reverses`);
        4. E (as reported by this build) is where G put H's lines back (`_put_back_at`: on one of the lines G added
           back that H had removed; for a pure addition H, G's new-side position).
        E without a line, a pair without a remembered line: no verdict for E. A diff that cannot be read: no verdict for
        the errors of that file (logged); the other files of the attempt are still judged. Each (range, file) is read
        once, only for errors that pass condition 1."""
        try:
            cands: list[tuple[BuildError, FixAttempt, frozenset[int]]] = []
            for e in new_in_touched:
                k = _pair(e)
                r = self._resolved_by.get(k)
                if r is None or r is a or _same_attempt_case(a, r) or not self._still_in_tree(r):
                    continue
                lines = self._resolved_lines.get(k) or frozenset()
                if e.line is None or not lines or not k[0]:
                    continue                                    # 줄 번호가 없는 오류는 판단하지 않는다
                cands.append((e, r, lines))
        except Exception as ex:  # noqa: BLE001 - bookkeeping never breaks the scoring
            log.warning("trade-back check for %s failed (scored as before): %s: %s", a.id, type(ex).__name__, ex)
            return []
        if not cands:
            return []
        hunks: dict[tuple[str, str, str], Optional[list[_DiffHunk]]] = {}

        def of(base: Optional[str], head: Optional[str], rel: str) -> list[_DiffHunk]:
            k = (base or "", head or "", rel)
            if k not in hunks:
                try:
                    if not base or not head:
                        raise ValueError("commit range unknown")
                    hunks[k] = self._diff_sides(base, head, rel)
                except Exception as ex:  # noqa: BLE001 - an unreadable diff gives no verdict for that file
                    hunks[k] = None
                    log.warning("trade-back check for %s: diff of %s unreadable, no trade-back verdict for that file "
                                "(scored as before): %s: %s", a.id, rel, type(ex).__name__, ex)
            got = hunks[k]
            if got is None:
                raise LookupError(rel)
            return got

        out: list[tuple[BuildError, FixAttempt]] = []
        for e, r, lines in cands:
            rel = _pair(e)[0]
            try:
                theirs, mine = of(r.base_commit, r.commit, rel), of(p.pre_sha, p.post_sha, rel)
            except LookupError:
                continue
            line = int(e.line or 0)
            if any(any(_covers(h.old_start, h.old_len, ln) for ln in lines)
                   and any(_reverses(g, h) and _put_back_at(g, h, line) for g in mine)
                   for h in theirs):
                out.append((e, r))
        return out

    def _diff_sides(self, base: str, head: str, rel: str) -> list[_DiffHunk]:
        """``base..head`` 사이 한 파일의 diff를 hunk 목록으로 반환한다. git이나 디코딩에 실패하면 예외를 던진다.

        `git diff -U0 base head -- rel` parsed by `_diff_hunks` (ranges + countable added / removed lines),
        decoded strictly with the workspace's encoding candidates."""
        raw = self.ws._git_bytes("diff", "--no-color", "--no-renames", "-U0", base, head,  # noqa: SLF001
                                 "--", rel, check=False)
        if raw.returncode != 0:
            raise RuntimeError(f"git diff {base[:10]}..{head[:10]} -- {rel} failed: "
                               f"{(raw.stderr or b'').decode('utf-8', 'replace').strip()[:200]}")
        return _diff_hunks(_decode_strict(raw.stdout or b"", self.ws.encoding_candidates))

    def _settle_trade_backs(self, pr: PhaseResult, pending: list[_Pending],
                            traded: dict[str, list[tuple[BuildError, FixAttempt]]], build: BuildResult,
                            prev_count: Counter, *,
                            observed: Optional[dict[str, list[tuple[BuildError, FixAttempt]]]] = None) -> None:
        """되돌리기가 끝난 뒤 trade-back을 케이스 쌍마다 세어 저널에 남긴다. 한도에 이르면 그 케이스를 차단 대기에 넣는다.
        이어서 트리에 없는 시도의 해결 기록을 지우고, 이번에 개선된 시도가 없앤 오류를 기록한다. 예외를 던지지 않는다.

        After the reverts: count each trade-back per (case that brought the errors back, case that had resolved them),
        journal `trade_back`, and once the count reaches `loop.oscillation_limit` queue the first case for an
        OSCILLATION block (`_apply_trade_back_blocks`, at the next clustering). Then forget the resolutions of attempts
        no longer in the tree and remember what this build's `improved` attempts resolved: their targeted (file,
        signature) pairs that the previous build had and this one has not, with the lines that build showed them on.
        `observed` (mode observe): counted and journaled the same way (`"mode": "observe"`, `at_limit`), never queued
        for a block. Never raises."""
        try:
            limit = max(1, int(getattr(self.cfg.loop, "oscillation_limit", 2) or 2))
            observed = observed or {}
            for p in pending:
                a = p.attempt
                mode = "revert" if a.id in traded else "observe"
                back = traded.get(a.id) or observed.get(a.id)
                if not back:
                    continue
                self._record_round_trip(a, back)
                by_case: dict[str, list[tuple[BuildError, FixAttempt]]] = {}
                for e, r in back:
                    by_case.setdefault(_case_key(r), []).append((e, r))
                mine = _case_key(a)
                for rc, items in by_case.items():
                    key = (mine, rc)
                    self._trade_backs[key] += 1
                    n = self._trade_backs[key]
                    resolvers = list(dict.fromkeys(r.id for _e, r in items))
                    self.ws.journal({"event": "trade_back", "phase": pr.phase.value, "attempt": a.id, "case": mine,
                                     "resolved_by": resolvers, "resolved_case": rc,
                                     "errors": [_first_sentence(fmt_error(e), 200) for e, _r in items][:10],
                                     "count": n, "limit": limit, "outcome": a.outcome, "mode": mode,
                                     "at_limit": n >= limit})
                    log.warning("trade-back(%s): %s (case %s)이(가) case %s의 %s이(가) 바꾼 줄을 되돌려 오류 %d건이 다시 "
                                "나타났다 → %s (%d/%d)", mode, a.id, mine, rc, ", ".join(resolvers), len(items),
                                a.outcome, n, limit)
                    if mode == "revert" and n >= limit and mine not in self._trade_back_blocks:
                        why = (f"trade-back: attempts of case {mine} undid the fix of case {rc} (attempt "
                               f"{resolvers[-1]}) and brought back the errors it had resolved {n} time(s) "
                               f"(loop.oscillation_limit {limit})")
                        self._trade_back_blocks[mine] = (a, why)
            for k in [k for k, r in self._resolved_by.items() if not self._still_in_tree(r)]:
                del self._resolved_by[k]
                self._resolved_lines.pop(k, None)
            if build.ok:
                return
            cur = Counter(_pair(e) for e in build.errors)
            for p in pending:
                a = p.attempt
                if a.outcome != "improved" or not a.applied:
                    continue
                for e in a.cluster_errors:
                    k = _pair(e)
                    if prev_count[k] > 0 and cur[k] == 0:
                        self._resolved_by[k] = a
                        self._resolved_lines[k] = frozenset(int(x.line) for x in a.cluster_errors
                                                            if _pair(x) == k and x.line)
        except Exception as ex:  # noqa: BLE001 - bookkeeping never breaks the scoring
            log.warning("trade-back bookkeeping failed: %s: %s", type(ex).__name__, ex)

    def _record_round_trip(self, b: FixAttempt, back: list[tuple[BuildError, FixAttempt]]) -> None:
        """trade-back으로 판단된 시도 B를 ``_round_trips``에 기록한다. 예외를 던지지 않는다.
        B가 트리에 남으면 B가 되살린 오류를 고쳤던 시도 A도 기록하고 A의 실적을 바로 다시 기록한다.

        One verdict ("v2.1z-ca", The loop.): B → `_round_trips` with the B reason (it is credited
        `round_trip` by `_credit_script` when `_after_scoring` reaches it). When B stays in the tree (its outcome is
        not `reverted`) each resolver A → `_round_trips` with the A reason and is credited again right away: A was
        credited at an earlier build and is not among this round's scored attempts. A reverted B leaves A alone. The
        first reason recorded for an attempt stays. Never raises."""
        try:
            by_r: dict[int, tuple[FixAttempt, list[str]]] = {}
            for e, r in back:
                files = by_r.setdefault(id(r), (r, []))[1]
                rel = _pair(e)[0]
                if rel and rel not in files:
                    files.append(rel)
            if not by_r:
                return
            first, files = next(iter(by_r.values()))
            self._round_trips.setdefault(
                id(b), f"round trip: put back the lines attempt {first.id} (case {_case_key(first)}) had changed in "
                       f"{_files_named(files)}; the error(s) it had resolved came back")
            if b.outcome == "reverted":
                return                                  # A의 수정은 트리에 남아 있으므로 A의 실적은 그대로 둔다
            for r, rfiles in by_r.values():
                self._round_trips.setdefault(
                    id(r), f"round trip: the error(s) this version resolved came back when attempt {b.id} (case "
                           f"{_case_key(b)}) put back the lines it had changed in {_files_named(rfiles)}")
                self._credit_script(r)
        except Exception as ex:  # noqa: BLE001 - bookkeeping never breaks the scoring
            log.warning("round-trip credit bookkeeping for %s failed: %s: %s", b.id, type(ex).__name__, ex)

    def _apply_trade_back_blocks(self, pr: PhaseResult, phase: Phase, iteration: int,
                                 clusters: list[ErrorCluster]) -> None:
        """trade-back 한도에 이른 케이스가 이 빌드에 있으면 OSCILLATION으로 차단한다. 예외를 던지지 않는다.
        이 빌드에 없는 케이스는 대기 목록에 그대로 둔다."""
        if not self._trade_back_blocks:
            return
        try:
            for key, (a, why) in list(self._trade_back_blocks.items()):
                hit = [c for c in clusters if _same_case(a, c)]
                if not hit:
                    continue
                for c in hit:
                    self._block_simple(phase, iteration, c, BlockReason.OSCILLATION, why)
                del self._trade_back_blocks[key]
                pr.notes.append(f"build {iteration}: case {key} blocked (oscillation) — {why}")
        except Exception as ex:  # noqa: BLE001
            log.warning("trade-back block failed: %s: %s", type(ex).__name__, ex)

    @staticmethod
    def _uncovered_stage(a: FixAttempt, left_targeted: tuple[int, int], real: list[BuildError],
                         new_struct: list[str]) -> list[BuildError]:
        """pom을 고친 시도가 대상 오류를 모두 없앴고 새 구조적 오류가 모두 대상보다 뒤 단계면 그 오류 목록을 반환한다.
        이런 오류는 시도가 만든 것이 아니라 pom 문제에 가려져 있던 다음 단계의 오류다.
        아니면 빈 목록을 반환하고, 그 시도는 평소대로 악화로 되돌린다."""
        left, _targeted = left_targeted
        if left:
            return []
        stage = _stage_of(a.cluster_errors)
        new = [e for e in real if e.kind.value in new_struct]
        if not new or any((error_stage(e) if error_stage(e) is not None else 99) <= stage for e in new):
            return []
        return new

    # ------------------------------------------------------------------ 수정
    def _kb_match(self, cluster: ErrorCluster) -> KBMatch:
        """KB에서 이 클러스터를 조회한다. 사람이 ``block``으로 고정한 케이스와 마지막 해결 힌트가 나온다.
        조회에 실패하면 빈 결과를 반환한다."""
        try:
            m = self.kb.match(cluster, self.ws, self.profile)
            return m if m is not None else KBMatch()
        except Exception as e:
            log.warning("kb.match failed for %s: %s", cluster.id, e)
            return KBMatch()

    def _plan(self, clusters: list[ErrorCluster]) -> list[tuple[ErrorCluster, Optional[TriageDecision], list[str]]]:
        """triage로 이번 라운드의 처리 계획을 세운다.
        근본 원인 순으로 (클러스터, 가려짐 결정, 가리는 클러스터 목록)을 반환한다.
        pom 문제가 있으면 그 모듈의 컴파일 오류는 가려 두고 원인을 먼저 고친다."""
        planner = getattr(self.triager, "plan_iteration", None)
        if not callable(planner):
            return [(c, None, []) for c in clusters]
        return [(it.cluster, it.skip, list(it.masked_by)) for it in planner(clusters)]

    def _decide(self, phase: Phase, iteration: int, stall_level: int, cluster: ErrorCluster,
                match: KBMatch) -> TriageDecision:
        """triage에서 SCRIPT / BLOCKED / SKIP / ESCALATE 결정을 받는다.
        차단 정책(``loop.blocked_policy``), 시도 한도, 고정된 케이스는 triage가 판단한다.
        triage가 예외를 던지면 루프가 멈추지 않도록 ESCALATE를 반환한다."""
        ctx = TriageContext(phase=phase, iteration=iteration, stall_level=stall_level,
                            previous_attempts=self._previous(cluster),
                            resolved_elsewhere=self._resolved_elsewhere(cluster),
                            llm_asks_total=self._llm_asks_total(cluster), llm_calls_used=self.budget.llm_calls,
                            llm_calls_budget=self.budget.max_llm_calls)
        try:
            return self.triager.decide(cluster, match, ctx, self.profile)
        except Exception as e:
            log.exception("triage.decide failed for %s", cluster.id)
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.ESCALATE,
                                  rationale=f"triage failed: {type(e).__name__}: {e}")

    def _previous(self, cluster: ErrorCluster, *, ladder: bool = True) -> list[FixAttempt]:
        """이 클러스터에 대해 결과가 이미 평가된 시도를 반환한다. 시도 순서와 횟수 한도는 이 목록을 기준으로 한다.
        이번 라운드에 대기 중인 시도와 LLM 쿨다운을 일으킨 전송 실패는 뺀다. 쿨다운으로 한도가 줄면 안 되기 때문이다.
        ``ladder=True``면 사전 시도와 다른 위치에서 한 시도를 뺀다. ``ladder=False``는 프롬프트의 이전 시도 목록이다.

        Not other files that share an L1 signature. The zero-LLM pre-tries (`PRE_TRY_SOURCES`) are not ladder
        steps, so they never use up an LLM try; the prompt's history keeps them. `ladder=True` keeps the
        attempts made HERE only (`made_here`: one of the places of the errors an
        attempt targeted is a place of the case's errors now), so the caps hold per case, run and set of places;
        `ladder=False` (the prompt's "Previous attempts on this case") keeps every place ("v2.1z-cd")."""
        return [a for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)
                if a.id not in self._cooled_attempts and a.outcome != "pending"
                and not (ladder and (a.source in PRE_TRY_SOURCES or not self._made_here(cluster, a)))]

    def _made_here(self, cluster: ErrorCluster, a: FixAttempt) -> bool:
        """지금 케이스 오류가 있는 위치에서 한 시도면 True를 반환한다. 옮겨진 파일은 현재 경로로 비교한다.

        `sbmig.triage.decide.made_here` with the registry's mapping of moved files."""
        from sbmig.triage.decide import made_here
        return made_here(cluster, a, self.blocked.canonical_file)

    def _resolved_elsewhere(self, cluster: ErrorCluster) -> list[FixAttempt]:
        """다른 위치에서 저장 버전을 실행해 이 케이스를 해결한 이번 실행의 시도를 오래된 순서로 반환한다.
        edits로 적용해 스크립트를 실행하지 않은 시도, 저장하지 않은 v0, 왕복에 참여한 시도는 넣지 않는다.

        The case's attempts made at OTHER places that ended `improved` / `build_ok` by running a stored version
        (version > 0, `_script_ran`: not an answer applied as edits), oldest first: the candidates of the ladder's
        first rung at a new place (`TriageContext.resolved_elsewhere`, INTERFACES "v2.1z-cd" rule 2). An attempt the
        loop recorded in `_round_trips` is none, also when its `round_trip` credit did not reach the store."""
        return [a for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)
                if a.id not in self._cooled_attempts and a.decision.kind == DecisionKind.SCRIPT
                and a.outcome in ("improved", "build_ok") and int(a.script_version or 0) > 0
                and self._script_ran.get(id(a)) and id(a) not in self._round_trips
                and not self._made_here(cluster, a)]

    def _llm_asks_total(self, cluster: ErrorCluster) -> int:
        """이번 실행에서 모든 위치를 합쳐 이 케이스에 한 LLM 요청 수를 반환한다. 재실행과 재사용은 세지 않는다.

        The case's LLM asks (new + augment) in this run over EVERY place: the attempts `_previous(ladder=True)` would
        count if every place were here (scored, not cooled down, no pre-try), the bound of rule 4 (INTERFACES
        "v2.1z-cd"). Reuses and reruns ask nothing and are not counted."""
        from sbmig.triage.scripts import LLM_MODES
        return sum(1 for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)
                   if a.id not in self._cooled_attempts and a.outcome != "pending" and a.source not in PRE_TRY_SOURCES
                   and a.decision.kind == DecisionKind.SCRIPT and a.script_mode in LLM_MODES)

    def _sibling_hints(self, cluster: ErrorCluster, limit: int = 3) -> list[str]:
        """이번 실행에서 다른 범위의 같은 오류를 어떻게 고쳤는지, 무엇이 실패했는지 힌트 줄로 반환한다.
        프롬프트 힌트로만 쓴다.

        A case already spans its files, so this is the same case in another scope (another MSA service's
        loop)."""
        mine = {id(a) for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)}
        out: list[str] = []
        for a in reversed(_related(cluster, self.history)):
            if id(a) in mine or a.outcome in ("pending", "blocked") or a.id in self._cooled_attempts:
                continue
            where = next((e.file for e in a.cluster_errors if e.file), None) or a.cluster_id
            sig = next((s for s in a.cluster_signatures if s in cluster.signatures), "")
            who = self._attempt_name(a) if a.decision.kind == DecisionKind.SCRIPT else a.decision.kind.value
            if a.outcome in ("improved", "build_ok", "partial"):
                text = (f"this run: the same error ({sig}) in {where} was {'partly ' if a.outcome == 'partial' else ''}"
                        f"fixed by attempt {a.id} ({who}): {_change_line(a)}")
            else:
                text = (f"this run: attempt {a.id} ({who}) on the same error ({sig}) in {where} did not fix it "
                        f"({a.outcome}): {_change_line(a)}")
            out.append(text[:400])
            if len(out) >= limit:
                break
        return out

    def _cluster(self, errors: list[BuildError], target: Optional[str]) -> list[ErrorCluster]:
        """오류를 케이스(L2 서명)마다 클러스터 하나로 묶는다.
        한 클러스터는 여러 파일에 걸칠 수 있다. 루프의 빌드 대상인 MSA 서비스 안의 오류만 담는다."""
        return self.triager.cluster(errors, self.profile, scope=target)

    def _fix_round(self, pr: PhaseResult, phase: Phase, iteration: int, build: BuildResult, target: Optional[str],
                   stall_level: int, clusters: Optional[list[ErrorCluster]] = None, *, round_no: int = 1,
                   max_rounds: int = 0) -> tuple[list[_Pending], bool, list[TriageDecision], bool, list[BuildError]]:
        """수정 라운드 하나를 실행한다. 케이스마다 한 번까지 시도하고 ``loop.max_clusters_per_iteration``개 케이스까지 다룬다.
        미룬 케이스와 차단된 케이스는 개수에 넣지 않는다.
        가리고 있는 케이스가 차단되거나 에스컬레이트되어 손댈 수 없으면 가려진 케이스도 다룬다.

        The caller sets the log stage `fix r<round_no>`. Returns (pending tries, anything applied, decisions,
        a try was made, errors brought back by rollbacks)."""
        if clusters is None:
            clusters = self._cluster(build.errors, target)
        planned = self._plan(clusters)
        limit = max(1, self.cfg.loop.max_clusters_per_iteration)
        n_open = self.errors.open_count()
        order = list(dict.fromkeys(_class_of(c) for c, skip, _ in planned if skip is None))
        log.info("%d open → %d case(s); 순서: %s%s (fix round %d%s%s)",
                 n_open if n_open is not None else build.error_count, len(clusters),
                 ", ".join(order[:3]) + (", …" if len(order) > 3 else "") if order else "-",
                 " 먼저" if len(order) > 1 else "", round_no, f"/{max_rounds}" if max_rounds else "",
                 f", stall level {stall_level}" if stall_level else "")
        self.ws.journal({"event": "triage", "phase": phase.value, "iteration": iteration,
                         "clusters": [{"id": c.id, "kind": c.kind.value, "file": c.file, "module": c.module,
                                       "case_id": c.case_id, "errors": len(c.errors),
                                       "files": len({e.file for e in c.errors if e.file}), "priority": c.priority}
                                      for c in clusters],
                         "masked": [c.id for c, skip, _ in planned if skip is not None]})
        # n은 같은 빌드의 다음 라운드에서도 시도 id가 겹치지 않게 이어서 센다
        st = _Round(phase=phase, iteration=iteration, build=build, target=target, stall_level=stall_level,
                    others={c.id: _label(c) for c in clusters}, round=round_no,
                    n=sum(1 for a in pr.attempts if a.iteration == iteration))
        masked: list[tuple[ErrorCluster, TriageDecision, list[str]]] = []
        handled = 0
        before = sum(1 for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT)
        for c, skip, by in planned:
            if self._ask_halt:
                break                                   # 중지 요청이나 시간 한도에 걸리면 나머지 케이스는 건너뛴다
            c2 = self._unblocked_part(st, c)
            if c2 is None:
                continue
            if skip is not None:
                masked.append((c2, skip, by))
                continue
            if handled >= limit:
                continue
            if self._handle_cluster(pr, st, c2) not in ("blocked", "deferred"):
                handled += 1
        # 가리고 있는 pom 문제 등이 차단되거나 에스컬레이트되어 손댈 수 없으면 고칠 수 있는 컴파일 오류를 더 숨기지 않는다
        for c, skip, by in masked:
            if self._ask_halt:
                break
            if by and all(b in st.hopeless for b in by) and handled < limit:
                if self._handle_cluster(pr, st, c, prefix="(unmasked: its pom problem was escalated/blocked) ") \
                        not in ("blocked", "deferred"):
                    handled += 1
            else:
                st.decisions.append(skip)
                self._journal_decision(st, skip)
                with stage(case_stage(st.round, c)):
                    log.info("%s — 건너뜀: %s", _case_header(c), skip.rationale)
        tried = sum(1 for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT) > before
        return st.pending, st.applied_any, st.decisions, tried, list(st.restored)

    def _unblocked_part(self, st: "_Round", c: ErrorCluster) -> Optional[ErrorCluster]:
        """이미 차단된 오류를 뺀 클러스터를 반환한다. 남는 오류가 없으면 BLOCKED 결정을 기록하고 None을 반환한다."""
        blocked, rest = self.blocked.partition(c)
        if not blocked:
            return c
        if rest:
            return _reduced(c, rest)
        item = self.blocked.covering(c)
        assert item is not None
        self._touch_item(item.id)
        st.hopeless.add(c.id)
        d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                           rationale=f"blocked: {item.reason.value} — already blocked ({item.id}, first seen in "
                                     f"{item.phase.value}); skipped")
        st.decisions.append(d)
        self._journal_decision(st, d)
        with stage(case_stage(st.round, c)):
            log.info("%s — 이미 blocked (%s, %s): 건너뜀", _case_header(c), item.reason.value, item.id)
        return None

    def _journal_decision(self, st: "_Round", d: TriageDecision) -> None:
        self.ws.journal({"event": "decision", "phase": st.phase.value, "iteration": st.iteration,
                         **d.model_dump(mode="json")})

    def _script_step(self, c: ErrorCluster) -> Any:
        """triage의 마지막 SCRIPT 결정이 정한 시도 방식을 반환한다. reuse / augment / new이고, 한도에 이르면 mode가 None이다.
        triage가 주지 않으면 ``plan_script_step``으로 계산한다."""
        getter = getattr(self.triager, "script_step", None)
        step = None
        if callable(getter):
            try:
                step = getter(c.id)
            except Exception:  # pragma: no cover - 다른 triage 구현
                step = None
        if step is None:
            from sbmig.triage.scripts import plan_script_step
            step = plan_script_step(c, self._previous(c), self.cases if self.cases.active else None, self.cfg,
                                    elsewhere=self._resolved_elsewhere(c), llm_total=self._llm_asks_total(c))
        return step

    def _deferral(self, st: "_Round", c: ErrorCluster, step: Any) -> tuple[list[str], str]:
        """``c``를 다음 빌드까지 미뤄야 하면 (원인 케이스 목록, "files" 또는 "pom")을 반환한다. ([], "")면 지금 시도한다.
        - "files": 이번 라운드의 수정이 그 오류 파일 전부에 이미 실행됐다
        - "pom": 이번 라운드의 수정이 컴파일 케이스의 모듈 pom이나 루트 pom을 바꿨다
        - 재사용, 파일이 없는 오류, 라운드의 첫 케이스는 미루지 않는다
        근본 원인을 고치면 뒤따르는 케이스는 재빌드에서 대개 사라지므로 원인 하나에 LLM 호출 한 번으로 끝난다.


        * "files": the cases whose script already ran in this round on every error file of `c`. A script that ran
          counts whatever it did (the second case of a missing dependency finds the pom already fixed: no change, yet
          its files wait for the rebuild); an attempt without a script (no LLM answer) covers nothing.
        * "pom": a compile case whose error files all belong to modules whose pom.xml a script of this round CHANGED
          (`nearest_pom` of each file; a changed root pom covers every module) — the pom fix (a dependency, a compiler
          argument) usually clears them.

        Never deferred: a reuse (no LLM call; it may be the actual fix), a case with a file-less error (pom / module
        level), the first case of a round (nothing ran yet). When nothing of the round changed the tree, the loop
        asks again on the same build (re-round) with the next rung for the causes — the deferred cases come back on
        every new round."""
        if step is None or step.mode == "reuse" or not c.errors or not (st.covered or st.poms):
            return [], ""
        files = [_norm_path(e.file) for e in c.errors if e.file]
        if len(files) < len(c.errors):
            return [], ""
        if st.covered and all(f in st.covered for f in files):
            return list(dict.fromkeys(st.covered[f] for f in files)), "files"
        if st.poms and c.kind in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE):
            root = st.poms.get("pom.xml")
            by: list[str] = []
            for f in dict.fromkeys(files):
                owner = root or st.poms.get(_norm_path(self._nearest_pom(f) or ""))
                if owner is None:
                    return [], ""
                by.append(owner)
            return list(dict.fromkeys(by)), "pom"
        return [], ""

    def _nearest_pom(self, rel: str) -> Optional[str]:
        """파일이 속한 모듈의 pom을 반환한다. 가장 가까운 상위 pom.xml이다.
        알 수 없으면 None을 반환한다. 그 경우 미루지 않을 뿐이다."""
        try:
            from sbmig.fix.context import nearest_pom
            return nearest_pom(self.ws, rel)
        except Exception as e:  # noqa: BLE001 - 미루기는 최적화일 뿐이라 실패해도 시도를 막지 않는다
            log.debug("nearest pom of %s unavailable: %s", rel, e)
            return None

    def _handle_cluster(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str = "") -> str:
        """케이스 하나를 결정 -> 시도 -> 커밋 순서로 처리한다. 로그는 모두 케이스 단계 태그 ``fix r<n> <case_id>`` 아래에 남는다.
        blocked / escalated / skipped / deferred / halted / attempted 중 하나를 반환한다.

        "blocked" = nothing left to do for this cluster; "deferred" = its files are being fixed by an earlier
        case of this round; "halted" = a stop / the time budget was seen before an LLM ask."""
        with stage(case_stage(st.round, c)):
            return self._handle_case(pr, st, c, prefix)

    def _handle_case(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str) -> str:
        """케이스 하나를 처리한다.
        시도 순서를 다 썼으면 반복 실패로 차단한다. LLM이 필요한데 쓸 수 없거나 예산이 없으면 그 사유로 차단한다.
        일부 오류만 차단되면 나머지 오류로 다시 결정한다. 4번까지다. 미룬 케이스는 시도로 세지 않는다."""
        from sbmig.triage.blocked import BlockedCandidate
        log.info("%s", _case_header(c))
        self._mark(f"KB 탐색 for {c.id}")
        m = self._kb_match(c)
        self._match_hints[c.id] = list(m.hints or [])
        for _ in range(4):
            self._mark(f"deciding {c.id}")
            d = self._decide(st.phase, st.iteration, st.stall_level, c, m)
            if prefix:
                d.rationale = prefix + d.rationale
            step = self._script_step(c) if d.kind == DecisionKind.SCRIPT else None
            seen_step = step
            # LLM을 호출하는 시도는 호출 전에 중지 요청과 시간 한도를 확인한다. 걸리면 이 케이스를 시도하지 않는다
            if step is not None and step.needs_llm and self._halt_before_ask():
                return "halted"
            # 소유팀 결정이 필요해 차단된 케이스와 파일이나 아티팩트가 같은 케이스는 LLM 호출 없이 같은 사유로 차단한다
            if step is not None and step.needs_llm:
                owner = self._owner_cause(c)
                if owner is not None:
                    return self._block_same_cause(st, c, owner, prefix)
            forced = None
            # 시도 순서를 다 썼거나, LLM이 필요한데 쓸 수 없거나 예산이 없으면 강제로 차단한다
            # 저장된 스크립트 재사용은 LLM을 쓰지 않으므로 LLM이 없어도 막지 않는다
            if step is not None and step.mode is None:
                forced = BlockedCandidate(reason=BlockReason.REPEATED_FAILURE, errors=list(c.errors),
                                          evidence=[(step.why or "fix-script limits reached") + "; needs a human"])
            elif step is not None and step.needs_llm and self.llm_unavailable:
                forced = BlockedCandidate(reason=BlockReason.LLM_UNAVAILABLE, errors=list(c.errors),
                                          evidence=[f"LLM unavailable ({self.llm_unavailable})"])
            elif step is not None and step.needs_llm and not self.budget.llm_allowed():
                forced = BlockedCandidate(reason=BlockReason.BUDGET_EXHAUSTED, errors=list(c.errors),
                                          evidence=[f"LLM budget exhausted ({self.budget.llm_text()})"])
            # 프레임워크 프로젝트에서 javax 라이브러리가 프레임워크 아티팩트 자신이면 프로젝트에서 고칠 수 없으므로
            # LLM 없이 javax 사유로 바로 차단한다
            if forced is None and d.kind == DecisionKind.SCRIPT:
                forced = self._framework_javax_block(c)
            # 사전 시도는 시도 순서, 한도, LLM 장애와 상관없이 LLM보다 먼저 sbmig가 한 번 고쳐 본다
            # 같은 라운드에 다른 케이스가 바꾼 pom이면 기다려야 하므로 아래 미루기 검사 뒤에 실행한다
            pre, pre_poms = self._pre_try_for(c, d) if forced is None or forced.reason != BlockReason.EXTERNAL_JAVAX_API \
                else (None, [])
            # 번들 jar를 연결하려는데 저장소가 응답하지 않았으면 jar가 없다고 보지 않고 저장소 장애로 차단한다
            if pre is not None and pre.source == SBMIG_BUNDLED_SOURCE:
                unreachable = self._bundled_unreachable(c)
                if unreachable is not None:
                    forced, pre, pre_poms = unreachable, None, []
            if forced is not None:
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                                   rationale=prefix + f"blocked: {forced.reason.value} — {forced.evidence[0]}")
                step = None
            # 같은 라운드에 앞 케이스가 이미 그 파일이나 모듈 pom을 고쳤으면 재빌드에서 대개 함께 사라지므로 미룬다
            by, why = self._deferral(st, c, step) if d.kind == DecisionKind.SCRIPT else ([], "")
            if not by and pre is not None:              # 사전 시도가 고칠 pom을 이번 라운드에 누가 바꿨으면 기다린다
                by, why = self._pre_try_deferral(st, pre_poms)
            if by:
                # 시도가 아니므로 FixAttempt를 만들지 않는다. 시도 순서, 한도, 차단 정책, 정체 확인에 들어가지 않는다
                # 다음 라운드에 케이스가 남아 있으면 다시 본다
                names = ", ".join(x[len("case:"):] if x.startswith("case:") else x for x in by[:3])
                what = ("its module's pom was changed by case" if why == "pom" else "its files are being fixed by case")
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.SKIP,
                                   rationale=prefix + f"deferred: {what} {names} this round — waiting for the rebuild")
                st.decisions.append(d)
                self._journal_decision(st, d)
                log.info("deferred: %s %s → 다음 빌드까지 대기 (시도로 세지 않음)", what, names)
                return "deferred"
            # 사전 시도가 적용되면 그것이 이번 라운드의 시도이고, 아니면 같은 라운드에서 평소 시도 순서로 넘어간다
            if pre is not None and pre.run(pr, st, c):
                return "attempted"
            st.decisions.append(d)
            self._journal_decision(st, d)
            kb_line = self._kb_line(c, d, seen_step)
            if kb_line:
                log.info("%s", kb_line)
            if d.kind == DecisionKind.ESCALATE:
                log.info("escalated: %s", d.rationale)
                self.escalated[c.id] = d
                st.hopeless.add(c.id)
                return "escalated"
            if d.kind == DecisionKind.BLOCKED:
                cand = forced or self._candidate_for(c, d)
                rest = self._register_blocked(st.phase, st.iteration, c, cand, d.rationale, st.target)
                if not rest:
                    st.hopeless.add(c.id)
                    return "blocked"
                # javax 생성자 불일치처럼 케이스의 일부만 차단되면 나머지 오류는 계속 고친다
                c = _reduced(c, rest)
                prefix = "(rest of a partially blocked case) "
                log.info("%d error(s) of the case are not blocked → working on them", len(rest))
                continue
            if d.kind == DecisionKind.SKIP:
                log.info("건너뜀: %s", d.rationale)
                return "skipped"
            if self.dry_run:
                log.info("dry-run: decided (%s), nothing applied", d.rationale)
                return "skipped"
            self._script_attempt(pr, st, c, d, step)
            return "attempted"      # 케이스당 라운드마다 한 번만 시도한다
        return "attempted"

    def _kb_line(self, c: ErrorCluster, d: TriageDecision, step: Any) -> str:
        """로그에 남길 "KB 탐색: …" 한 줄을 만든다. 시도 방식을 모르면 빈 문자열을 반환한다.
        SCRIPT 결정이면 시도 방식의 줄을 쓰고, 아니면 찾은 내용과 결정을 쓴다.

        e.g. "KB 탐색: 저장된 스크립트 없음 → blocked (…)"."""
        if step is None:
            getter = getattr(self.triager, "script_step", None)
            try:
                step = getter(c.id) if callable(getter) else None
            except Exception:  # pragma: no cover - 다른 triage 구현
                step = None
        if step is None:
            return ""
        if d.kind == DecisionKind.SCRIPT or (step.mode is None and d.kind == DecisionKind.BLOCKED
                                             and "repeated_failure" in (d.rationale or "")):
            return step.kb_line
        m = _BLOCKED_RAT_RE.search(d.rationale or "")
        what = d.kind.value + (f" ({m.group('r')})" if m else "")
        return f"KB 탐색: {step.kb_found or '-'} → {what}"

    # ------------------------------------------------------------------ 시도
    def _attempt_name(self, a: FixAttempt) -> str:
        """힌트와 로그에 쓰는 시도 이름 ``script <case>:v<N> (<mode>)``을 만든다."""
        return _script_name(a)

    def _project(self) -> str:
        """KB 기록에 쓰는 프로젝트 이름을 반환한다.
        ``sbmig.kb.common.project_name``을 쓰고, 가져오지 못하면 프로필 이름을 쓴다."""
        try:
            from sbmig.kb.common import project_name
            return project_name(self.profile, self.ws)
        except Exception:  # noqa: BLE001 - KB 패키지가 없으면 프로필 이름을 쓴다
            return (getattr(self.profile, "name", "") or "") or self.ws.root.name

    def _script_attempt(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, d: TriageDecision, step: Any) -> None:
        """케이스를 한 번 시도한다. 아래 순서를 지켜야 한다.
        1. 보강의 기준(base)을 트리가 바뀌기 전에 만든다
        2. 직전 시도가 개선이나 빌드 성공이 아니면 되돌린다. 그 시도가 없앴던 오류가 케이스로 돌아온다
        3. 저장 스크립트(``kb-script``)를 실행하거나 LLM 답을 적용(``_llm_try``)하고 커밋한다
        새 시도가 아무것도 바꾸지 못하면 되돌리기를 취소해 트리를 마지막 빌드 때 상태로 둔다."""
        t0 = time.monotonic()
        case_id = step.case_id
        base = self._script_base(c, step) if step.mode == "augment" else None
        head_before = self.ws.head()
        rolled = self._rollback_previous(c, st)
        restored = rolled[1] if rolled else []
        if restored:                                    # 되돌린 트리 기준의 케이스로 바꾼다
            c = _reduced(c, [*c.errors, *restored])
        st.n += 1
        att = self._new_attempt(st.phase, st.iteration, st.n, c, d, st.build)
        att.errors_before = st.build.error_count + len(st.restored)
        att.script_mode, att.script_case_id = step.mode, case_id
        if rolled is not None and rolled[2]:
            att.notes.append(rolled[2])
        if step.mode == "reuse":
            meta = step.meta
            att.source = "kb-script"
            att.script_version, att.script_code = step.version, step.code
            att.analysis = f"stored fix script {case_id} v{step.version} (reused, no LLM call)"
            att.notes.append(f"reused stored v{step.version} ({getattr(meta, 'status', '?')}): "
                             f"{getattr(meta, 'successes', 0)} success(es) in "
                             f"{len(getattr(meta, 'projects', None) or [])} project(s), "
                             f"{getattr(meta, 'failures', 0)} failure(s)")
            self._begin_try(att, st)
            self._run_script(att, c, step.code, st)
        else:
            self._llm_try(att, c, st, step, base)
        # 수정이 실행된 오류 파일과 바뀐 pom을 기록해 같은 라운드의 뒤 케이스가 다음 빌드까지 기다리게 한다
        # 정적 검사에서 거부된 스크립트는 실행된 것이 아니다
        if self._ran_on_files(att):
            for e in c.errors:
                if e.file:
                    st.covered.setdefault(_norm_path(e.file), c.id)
        if att.applied:
            for ch in att.changes:
                for path in (ch.path, ch.new_path):
                    if path and PurePosixPath(_norm_path(path)).name == "pom.xml":
                        st.poms.setdefault(_norm_path(path), c.id)
        # 직전 시도를 되돌렸고 새 시도가 적용됐으면 직전 시도는 최종 트리에 없으므로 "rolled back"으로 표시해 운영자 확인에서 뺀다
        # 새 시도가 아무것도 바꾸지 못했으면 되돌리기를 취소해 트리를 마지막 빌드 때 상태로 둔다
        if rolled is not None and rolled[3]:
            prev_att = rolled[0]
            if att.applied:
                self._rolled_back.add(id(prev_att))
                prev_att.notes.append(ROLLED_BACK_NOTE + (f"v{att.script_version}" if att.script_version
                                                           else f"attempt {att.id}"))
                self._sync_try(prev_att)
            else:
                self._undo_rollback(st, prev_att, head_before, restored, att)
        self._seconds[id(att)] = time.monotonic() - t0
        self._record(pr, att)
        if att.outcome == "apply_failed":
            self._credit_script(att)
        self._sync_try(att)
        self._after_decline(st, c, att)                 # 거절한 케이스를 needs_owner로 차단한다. 시도 기록 뒤에 한다

    def _llm_try(self, att: FixAttempt, c: ErrorCluster, st: "_Round", step: Any, base: Optional[ScriptBase]) -> None:
        """신규나 보강 시도에서 LLM에 원인, 해결 방법, edits, 스크립트를 요청하고 그 답을 적용한다.
        - 보강 답에 스크립트가 있으면 새 버전으로 저장하고 실행한다. 스크립트 없이 edits만 있으면 edits를 적용한다
        - 신규 답에 edits가 있으면 edits를 바로 적용하고 스크립트는 unverified로 저장만 한다
        - 먼저 고른 쪽이 적용되지 않으면 같은 답의 나머지 쪽을 적용하고 시도에 메모를 남긴다

        Rules (§3.1):

        * augment + script → the new version is stored and runs (`llm-augment`); augment without a script but with
          edits (`no_script_reason`) → the edits are applied directly (`llm-edit`);
        * new + edits → the edits are applied directly, the script (when there is one) is stored `unverified`
          (`llm-new`; without a script `llm-edit`); new + script only → it is stored and runs (`llm-new`);
        * the first choice did not apply (the edits failed / changed nothing, or the script failed) → the other half
          of the same answer is applied instead (noted on the try)."""
        t0 = time.monotonic()
        ans = self._ask_llm(att, c, st, step, base)
        secs = time.monotonic() - t0
        requested = "llm-augment" if att.script_mode == "augment" else "llm-new"
        if ans is None:                                 # 쓸 수 있는 답이 없으면 실패를 시도에 남긴다
            att.source = requested
            log.info("LLM 답변 없음 (%.0fs): %s", secs, _first_sentence("; ".join(att.apply_errors) or "-", 240))
            self._begin_try(att, st)
            return
        self._answers[id(att)] = ans
        code = (ans.code or "").strip("\n")
        edits = list(ans.edits or [])
        att.analysis = ans.analysis or ""
        att.solution = getattr(ans, "solution", "") or ""
        att.no_script_reason = getattr(ans, "no_script_reason", "") or ""
        self._log_answer(secs, att, code, edits)
        # <blocked>나 분석만 있는 답은 프로젝트 밖의 일이라는 거절로 보고 같은 질문을 반복하지 않는다
        declined = self._declined_reason(ans, code, edits)
        if declined:
            fact = self._decline_contradiction(c, declined)
            carries = bool(code.strip() or edits)
            if fact:
                # sbmig가 찾은 정보(jakarta 빌드, 번들 jar, 대체 방법 노트)와 맞지 않는 거절은 받지 않는다
                # 거절 횟수에 넣지 않고 다음 라운드에 그 정보와 함께 다시 요청한다
                # 받지 않은 거절에 edits나 script가 있으면 버리지 않고 보통 답처럼 적용한다
                att.notes.append(f"decline rejected: {fact}" + ("; the answer's edits / script are applied" if carries else ""))
                log.info("LLM 의 거절을 받지 않음: %s — %s%s", _first_sentence(declined, 160), fact,
                         "; 답에 든 edits / script 는 적용한다" if carries else "")
                self.ws.journal({"event": "decline_rejected", "case": att.script_case_id or c.case_id or c.id,
                                 "attempt": att.id, "reason": declined[:300], "fact": fact, "applied": carries})
                if not carries:
                    att.source = requested
                    self._begin_try(att, st)
                    return self._fail(att, f"the answer declined ({_first_sentence(declined, 200)}) but {fact} — asked "
                                           "again with the fact")
            else:
                att.source = requested
                self._begin_try(att, st)
                return self._decline(att, c, ans, declined, ignored=carries)
        # 보강했다는 스크립트가 기준 버전과 같으면 결과도 같으므로 다시 실행하지 않고, edits가 있으면 그것을 쓴다
        if code.strip() and base is not None and att.script_mode == "augment" \
                and code.strip() == (base.code or "").strip():
            why = f"the augmented script is identical to its base v{base.version} — not run again"
            if not edits:
                att.source = requested
                self._begin_try(att, st)
                return self._fail(att, why)
            att.notes.append(why + "; applying the answer's edits")
            code = ""
        if not code.strip() and not edits:
            att.source = requested
            self._begin_try(att, st)
            why = "LLM answered with neither a script nor edits"
            if att.no_script_reason:
                why += f" (no script: {_first_sentence(att.no_script_reason, 200)})"
            if ans.error:
                why += f": {ans.error}"
            if ans.requested_files:
                why += f" (asked for: {', '.join(ans.requested_files[:5])})"
            return self._fail(att, why)
        has_code = bool(code.strip())
        att.source = requested if has_code else "llm-edit"
        if has_code:
            att.script_code = code
        # 신규는 이 프로젝트를 바로 고치려고 edits를 먼저 적용하고, 스크립트는 다음 프로젝트용으로 unverified 저장한다
        # 보강은 저장된 스크립트를 고치는 것이 목적이라 스크립트가 먼저다. 스크립트가 없을 때만 edits를 쓴다
        edits_first = bool(edits) and (att.script_mode == "new" or not has_code)
        if edits_first:
            self._begin_try(att, st)
            self._apply_edits(att, c, edits, st)
            # 중복 스크립트인지는 그 스크립트를 실행할 때만 의미가 있다
            runnable = has_code and self._store_script(att, c, step, ans, code, check_duplicate=not att.applied)
            if has_code and att.applied:
                att.notes.append(f"script v{att.script_version} stored (unverified), not run: the edits fixed it here"
                                 if att.script_version else "script not stored, not run: the edits fixed it here")
            if not att.applied and runnable:
                self._switch(att, f"the edits did not apply → running the answer's script v{att.script_version} "
                                  "instead")
                self._run_script(att, c, code, st)
            return
        runnable = self._store_script(att, c, step, ans, code)
        self._begin_try(att, st)
        if runnable:
            self._run_script(att, c, code, st)
        if not att.applied and edits:
            if self._script_ran.get(id(att)):           # 실행하고 실패한 스크립트는 바로 실패 실적을 남긴다
                self._credit_script(att)
                self._script_ran[id(att)] = False
            self._switch(att, f"script v{att.script_version} did not apply → applying the answer's edits instead")
            self._apply_edits(att, c, edits, st)

    def _declines_allowed(self) -> int:
        """케이스당 허용하는 거절 횟수 ``script.max_declines_per_case``를 반환한다.
        0 이하면 거절로 분류하지 않고 적용 실패로 처리한다. 값이 잘못되면 1이다."""
        try:
            return int(getattr(getattr(self.cfg, "script", None), "max_declines_per_case", 1))
        except (TypeError, ValueError):
            return 1

    def _declined_reason(self, ans: Any, code: str, edits: list) -> str:
        """LLM 답이 거절이면 그 차단 사유 문장을, 아니면 빈 문자열을 반환한다.
        거절은 ``<blocked>``가 있거나, edits도 스크립트도 없이 분석만 있는 답이다.
        전송 실패, 형식을 어긴 답, 파일을 더 요청하는 답, 쓸 수 없는 edits는 거절이 아니라 적용 실패다.
        ``script.max_declines_per_case``가 0 이하면 항상 빈 문자열을 반환한다."""
        if self._declines_allowed() <= 0 or ans is None:
            return ""
        blocked = " ".join(str(getattr(ans, "blocked_reason", "") or "").split())
        if blocked:
            return blocked
        if (code or "").strip() or edits:
            return ""
        analysis = "\n".join(x for x in str(getattr(ans, "analysis", "") or "").splitlines()
                             if not x.strip().startswith("[sbmig]")).strip()
        if not analysis or analysis.startswith("llm-error"):
            return ""
        err = " ".join(str(getattr(ans, "error", "") or "").split())
        if err and not err.startswith(_DECLINE_ERRORS):
            return ""                                   # 해석 실패, 쓸 수 없는 edits, 파일을 더 요청한 답
        why = str(getattr(ans, "no_script_reason", "") or "").strip()
        if why and why != "(no reason given)":
            return " ".join(why.split())
        return _first_sentence(analysis, 300)

    def _decline_count(self, case_id: str, c: Optional[ErrorCluster] = None) -> int:
        """이번 실행에서 케이스가 거절된 횟수를 시도 메모 ``declined: …``로 세어 반환한다.
        ``c``가 있으면 지금 오류가 있는 위치에서 받은 거절만 센다.

        `script.max_declines_per_case` holds per set of places ("v2.1z-cd")."""
        if not case_id:
            return 0
        return sum(1 for a in self.history if a.script_case_id == case_id
                   and any(n.startswith(DECLINED_NOTE) for n in a.notes)
                   and (c is None or self._made_here(c, a)))

    def _decline(self, att: FixAttempt, c: ErrorCluster, ans: Any, reason: str, *, ignored: bool = False) -> None:
        """거절된 답을 기록한다. 적용한 것은 없다.
        시도 결과는 ``blocked``, 메모는 ``declined: <사유>``, 저널은 ``declined``다.
        needs_owner 차단은 시도가 이력에 들어간 뒤 ``_after_decline``이 한다."""
        case = att.script_case_id or c.case_id or c.id
        n = self._decline_count(case, c) + 1
        limit = max(1, self._declines_allowed())
        att.outcome = "blocked"
        att.notes.append(DECLINED_NOTE + _first_sentence(reason, 400))
        if ignored:
            att.notes.append("the answer named a blocker (<blocked>): its edits / script were not applied")
        self._declines[id(att)] = reason
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
        except Exception:  # pragma: no cover - 적용한 것이 없지만 방어한다
            pass
        log.info("LLM 이 거절함 (declined %d/%d): %s — 프로젝트 밖의 일, 다시 묻지 않음", n, limit,
                 _first_sentence(reason, 240))
        self.ws.journal({"event": "declined", "case": case, "attempt": att.id, "reason": reason[:500],
                         "explicit": bool(str(getattr(ans, "blocked_reason", "") or "").strip()),
                         "declines": n, "limit": limit, "mode": att.script_mode, "ignored": ignored})

    def _after_decline(self, st: "_Round", c: ErrorCluster, att: FixAttempt) -> None:
        """거절 횟수가 ``script.max_declines_per_case``에 이르면 케이스를 needs_owner로 차단한다.
        그 뒤로는 LLM을 더 호출하지 않는다."""
        reason = self._declines.pop(id(att), None)
        if reason is None:
            return
        case = att.script_case_id or c.case_id or c.id
        if self._decline_count(case, c) < max(1, self._declines_allowed()):
            return
        self._block_needs_owner(st, c, reason)

    def _block_needs_owner(self, st: "_Round", c: ErrorCluster, reason: str, same_as: str = "") -> None:
        """거절된 케이스나 원인이 같은 다른 케이스를 LLM 호출 없이 차단한다.
        - triage가 ``DECLINE_KEEPS_REASONS``의 구체적인 사유를 주면 그 사유와 조치 방법 문구를 쓴다
        - 분류되지 않은 오류만 needs_owner로 차단하고, 조치 방법에 소유팀 결정이 필요하다는 말을 붙인다
        - 처음 거절한 케이스는 원인이 같은 케이스를 알아보도록 파일과 아티팩트를 기억해 둔다

        Operators, the handbook and the attn codes key on the specific reasons; the LLM's sentence goes into
        the rationale. needs_owner suggestion: "<blocker sentence> — 소유팀 결정 필요" ("same cause as <case>: …" for
        a sibling)."""
        from sbmig.triage.blocked import BlockedCandidate, classify_blocked
        sentence = _first_sentence(reason, 300)
        text = f"same cause as {same_as}: {sentence}" if same_as else sentence
        rest_c: Optional[ErrorCluster] = c
        try:
            pol = classify_blocked(c, self.profile, self.cfg, ws=self.ws)
        except Exception as e:  # noqa: BLE001 - 분류에 실패하면 아래에서 needs_owner로 차단한다
            log.debug("classification of the declined %s failed: %s: %s", c.id, type(e).__name__, e)
            pol = None
        if pol is not None and pol.reason in DECLINE_KEEPS_REASONS:
            pol.evidence = [*pol.evidence, f"the LLM declined: {text}"]
            rest = self._register_blocked(st.phase, st.iteration, c, pol,
                                          f"blocked: {pol.reason.value} — {pol.detail()}; the LLM declined: {text}",
                                          st.target)
            rest_c = _reduced(c, rest) if rest else None
        if rest_c is not None:
            cand = BlockedCandidate(reason=BlockReason.NEEDS_OWNER, errors=list(rest_c.errors), evidence=[text],
                                    suggestion=f"{text} — {NEEDS_OWNER_KO}")
            self._register_blocked(st.phase, st.iteration, rest_c, cand,
                                   f"blocked: {BlockReason.NEEDS_OWNER.value} — {text}", st.target)
        st.hopeless.add(c.id)
        if not same_as:
            files = frozenset(_norm_path(e.file) for e in c.errors if e.file)
            arts = frozenset(e.artifact.key for e in c.errors
                             if e.artifact is not None and e.artifact.group_id and e.artifact.artifact_id)
            self._owner_blocked.append((c.case_id or c.id, files, arts, sentence))

    def _owner_cause(self, c: ErrorCluster) -> Optional[tuple[str, str]]:
        """``c``가 소유팀 결정으로 차단된 케이스와 원인이 같으면 (그 케이스, 사유 문장)을, 아니면 None을 반환한다.
        ``c``의 오류 파일이 모두 그 케이스의 오류 파일에 들어 있거나 같은 아티팩트를 가리키면 원인이 같다고 본다."""
        if not self._owner_blocked:
            return None
        me = c.case_id or c.id
        files = {_norm_path(e.file) for e in c.errors if e.file}
        arts = {e.artifact.key for e in c.errors if e.artifact is not None and e.artifact.group_id
                and e.artifact.artifact_id}
        for case, ofiles, oarts, sentence in self._owner_blocked:
            if case == me:
                continue
            if (files and ofiles and files <= ofiles) or (arts and oarts and arts & oarts):
                return case, sentence
        return None

    def _block_same_cause(self, st: "_Round", c: ErrorCluster, owner: tuple[str, str], prefix: str) -> str:
        """원인이 같은 케이스를 LLM 호출 없이 needs_owner로 차단하고 "blocked"를 반환한다.
        결정을 기록하고 저널 ``same_cause_blocked``를 남긴다."""
        case, sentence = owner
        d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                           rationale=prefix + f"blocked: {BlockReason.NEEDS_OWNER.value} — same cause as {case} "
                                              f"(declined: {sentence}); no LLM call")
        st.decisions.append(d)
        self._journal_decision(st, d)
        log.info("blocked (needs_owner): same cause as %s — LLM 에 묻지 않음", case)
        self.ws.journal({"event": "same_cause_blocked", "case": c.case_id or c.id, "as": case})
        self._block_needs_owner(st, c, sentence, same_as=case)
        return "blocked"

    def _switch(self, att: FixAttempt, note: str) -> None:
        """LLM 답에서 먼저 고른 쪽이 적용되지 않으면 그 실패를 메모로 옮기고 같은 시도를 나머지 쪽으로 이어 간다."""
        att.notes += [f"first choice failed: {e}" for e in att.apply_errors]
        att.apply_errors = []
        att.outcome = "pending"
        att.notes.append(note)
        log.info("%s", note)

    def _log_answer(self, secs: float, att: FixAttempt, code: str, edits: list) -> None:
        """LLM 답을 로그 한 줄로 요약한다. 걸린 시간, 원인과 해결의 첫 문장, edits 개수, 스크립트 유무다.

        `LLM 답변 (21s): 원인 … / 해결 … / edits 1 / script 있음`"""
        parts = [f"원인 {_first_sentence(att.analysis, 140) or '-'}", f"해결 {_first_sentence(att.solution, 140) or '-'}",
                 f"edits {len(edits)}"]
        if code.strip():
            parts.append("script 있음")
        elif att.no_script_reason:
            parts.append(f"script 없음 (이유: {_first_sentence(att.no_script_reason, 140)})")
        else:
            parts.append("script 없음")
        log.info("LLM 답변 (%.0fs): %s", secs, " / ".join(parts))

    def _begin_try(self, att: FixAttempt, st: "_Round") -> None:
        """시도의 출처가 정해진 뒤 오류 목록에 이 시도의 FixTry를 연다.
        출처는 kb-script / llm-new / llm-augment / llm-edit다. 오류 목록이 번호를 주지 않으면 케이스별로 직접 매긴다."""
        case_id = att.script_case_id or _case_of_errors(att.cluster_errors)
        source = att.source or ("kb-script" if att.script_mode == "reuse" else
                                "llm-augment" if att.script_mode == "augment" else "llm-new")
        tr = self.errors.begin_try(case_id, st.round, source, script_version=int(att.script_version or 0))
        n = int(getattr(tr, "n", 0) or 0)
        if not n:
            n = self._try_seq.get(case_id, 0) + 1
        self._try_seq[case_id] = max(n, self._try_seq.get(case_id, 0))
        self._tries[id(att)] = (case_id, n, st.round)

    def _sync_try(self, att: FixAttempt) -> None:
        """FixAttempt의 내용을 FixTry에 옮긴다. 적용, 결과 평가, 되돌리기 뒤마다 모든 필드를 다시 맞춘다.
        diff는 ``errors.diff_chars``까지만 남긴다. 전체 diff는 changes.patch에 있다.

        The `optional` fields (edit notes, major upgrades, verify guide) are dropped by an error list of the
        older shape."""
        t = self._tries.get(id(att))
        if t is None:
            return
        case_id, n, _rnd = t
        cap = int(getattr(getattr(self.cfg, "errors", None), "diff_chars", 0) or 0)
        diff = self._diffs.get(id(att), "")
        if cap and len(diff) > cap:
            diff = diff[:cap] + "\n… (truncated: the full diff is in changes.patch)"
        applied = ("edits" if att.edits_applied else "script") if att.applied else "none"
        self.errors.finish_try(case_id, n, analysis=att.analysis, solution=att.solution,
                               no_script_reason=att.no_script_reason, applied=applied,
                               files=list(dict.fromkeys(ch.new_path or ch.path for ch in att.changes)), diff=diff,
                               commit=att.commit or "", result=att.outcome, note=_try_note(att),
                               llm_calls=int(att.llm_usage.calls or 0),
                               seconds=round(self._seconds.get(id(att), 0.0), 1),
                               optional={"script_version": int(att.script_version or 0),
                                         "edits": list(self._notes.get(id(att), [])),
                                         "major_upgrades": list(att.major_upgrades), "verify": att.verify,
                                         "verify_source": att.verify_source})

    def _ran_on_files(self, att: FixAttempt) -> bool:
        """스크립트나 edits가 실제로 파일에 실행됐으면 True를 반환한다. 정적 검사에서 거부된 것은 실행되지 않은 것이다."""
        res = att.script_result
        if res is not None and res.status != "rejected":
            return True
        er = self._edit_results.get(id(att))
        return er is not None and er.status != "rejected"

    def _script_base(self, c: ErrorCluster, step: Any) -> Optional[ScriptBase]:
        """보강 프롬프트에 넣을 기준 ``ScriptBase``를 만든다. 예외를 던지지 않는다.
        이어서 고칠 버전, 이 프로젝트에서 그 버전이 한 일, 남은 오류, 새로 생긴 오류, 버전 이력을 담는다.
        만들 수 없으면 None을 반환하고 신규 요청으로 넘어간다.

        A stored version that did not run here carries what the store knows of it."""
        try:
            a = step.base_attempt
            if a is not None:
                res = a.script_result
                changed = [ch.new_path or ch.path for ch in a.changes] or (list(res.changed_files) if res else [])
                err = (res.error if res is not None else "") or "; ".join(a.apply_errors)
                if res is None and a.edits_applied:
                    err = (f"this script was stored but not run here: the answer's edits were applied instead "
                           f"(outcome {a.outcome})" + (f"; {err}" if err else ""))
                return ScriptBase(case_id=step.case_id, version=int(a.script_version or 0), code=a.script_code,
                                  outcome=a.outcome, status=(res.status if res is not None else ""), error=err[:2000],
                                  log=list(res.log[-SCRIPT_BASE_LOG:]) if res is not None else [],
                                  changed_files=list(dict.fromkeys(changed))[:100],
                                  remaining_errors=[fmt_error(e) for e in c.errors][:30],
                                  introduced_errors=[fmt_error(e) for e in a.introduced_errors][:20],
                                  history=self._script_history(step.case_id, c),
                                  session_id=getattr(a, "llm_session_id", None),   # 케이스 세션을 이어 쓴다
                                  session_model=getattr(a, "llm_session_model", "") or "")   # 그 세션의 모델
            return ScriptBase(case_id=step.case_id, version=int(getattr(step.meta, "version", 0) or 0),
                              code=step.code, remaining_errors=[fmt_error(e) for e in c.errors][:30],
                              history=self._script_history(step.case_id, c))
        except Exception as e:  # noqa: BLE001 - 기준을 못 만들면 신규 요청으로 간다
            log.warning("script base for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return None

    def _script_history(self, case_id: str, c: ErrorCluster) -> list[str]:
        """보강 프롬프트에 넣을 이력 줄을 만든다.
        저장된 버전마다 상태, 성공과 실패 수, 마지막 결과를 한 줄로 쓰고 이번 실행의 시도를 덧붙인다.

        'v1 [proven]: 2 ok / 1 failed — last: no_change' per stored version, then this run's tries."""
        out: list[str] = []
        info = self.cases.find(case_id) if self.cases.active else None
        mine = self._run_scope()
        for m in list(getattr(info, "scripts", None) or [])[-SCRIPT_HISTORY:]:
            # 다른 범위의 실행이 만든 버전은 그 범위를 밝힌다. 성공, 실패 수는 그 범위의 것이다
            sc = (getattr(m, "scope", "") or "").strip() if isinstance(getattr(m, "scope", ""), str) else ""
            other = f", written by a {sc} run" if (sc or SCOPE_FULL) != mine else ""
            out.append(f"v{m.version} [{m.status}{other}]: {m.successes} ok / {m.failures} failed — last: "
                       f"{m.last_result or '-'}")
        for a in _same_cluster(c, self.history, self.blocked.canonical_file):
            if a.decision.kind == DecisionKind.SCRIPT:
                st = a.script_result.status if a.script_result is not None else "-"
                how = "edits" if a.edits_applied else f"script {st}"
                out.append(f"this run: {a.id} {a.source or a.script_mode} v{a.script_version} → {a.outcome} ({how})")
        return out[-2 * SCRIPT_HISTORY:]

    def _rollback_previous(self, c: ErrorCluster, st: "_Round"
                           ) -> Optional[tuple[FixAttempt, list[BuildError], str, bool]]:
        """케이스의 직전 시도가 개선이나 빌드 성공이 아니면 되돌린다. 되돌릴 것이 없으면 None을 반환한다.
        그 시도가 HEAD면 reset, 아니면 ``git revert``로 되돌린다.
        (시도, 다시 생긴 오류, 다음 시도용 메모, 되돌렸는지)를 반환한다.
        revert가 충돌로 실패하면 메모만 남기고 다음 버전은 그 상태 위에서 실행된다."""
        prev = [a for a in _same_cluster(c, self.history, self.blocked.canonical_file)
                if a.decision.kind == DecisionKind.SCRIPT and a.applied and a.commit]
        if not prev:
            return None
        a = prev[-1]
        if a.outcome in NO_ROLLBACK or id(a) in self._rolled_back or _kept_partial(a):
            return None
        how = ""
        what = f"v{a.script_version}" if a.script_version and not a.edits_applied else (a.source or "edits")
        try:
            if self.ws.head() == a.commit and a.base_commit:
                self.ws.revert_to(a.base_commit)
                how = "reset"
            else:
                res = self.ws._git("revert", "--no-edit", a.commit, check=False)   # noqa: SLF001
                if res.returncode != 0:
                    self.ws._git("revert", "--abort", check=False)                  # noqa: SLF001
                    why = (res.stderr or res.stdout or "").strip()[:200]
                    a.notes.append(f"rollback before the next attempt failed: {why}")
                    log.warning("롤백 실패: %s (%s, %s) — the next try runs on top of it (%s)", what, a.id, a.outcome,
                                why)
                    return a, [], (f"the previous attempt {a.id} ({what}, {a.outcome}) could not be rolled back "
                                   f"({why}): this version ran on top of it"), False
                how = "git revert"
        except Exception as e:  # noqa: BLE001 - git 문제면 그 위에서 계속한다
            a.notes.append(f"rollback before the next attempt failed: {type(e).__name__}: {e}")
            return a, [], (f"the previous attempt {a.id} could not be rolled back ({type(e).__name__}): this "
                           "version ran on top of it"), False
        # 그 시도가 없앤 오류는 대상 오류 중 이번 빌드에 없는 것이다. 다음 시도 결과 평가의 기준에 더한다
        # 앞 모듈에서 멈춘 빌드처럼 이번 빌드가 보여 줄 수 없었던 오류는 되살아난 것으로 세지 않는다
        real_now = [e for e in st.build.errors if not _synthetic(e)]
        restored = [e for e in _gone(a.cluster_errors, st.build) if _visible_in([e], st.build, real_now)]
        st.restored += restored
        self.ws.journal({"event": "script_rollback", "phase": st.phase.value, "iteration": st.iteration,
                         "attempt": a.id, "case": a.script_case_id, "version": a.script_version,
                         "outcome": a.outcome, "commit": a.commit, "mode": how, "restored_errors": len(restored)})
        log.info("롤백: %s (%s, %s) 되돌림 (%s)%s", what, a.id, a.outcome, how,
                 f" — {len(restored)} error(s) it had removed are back" if restored else "")
        return a, restored, (f"the previous attempt {a.id} ({what}, {a.outcome}) was rolled back ({how}) before this "
                             "one"), True

    def _rollback_unhelpful(self, pr: PhaseResult, phase: Phase, iteration: int) -> int:
        """루프가 끝날 때 케이스마다 마지막으로 적용한 시도가 no_change나 inconclusive면 되돌린다. 되돌린 수를 반환한다.
        도움이 안 된 시도가 결과물에 들어가지 않게 하기 위해서다. partial 시도는 케이스 상태와 상관없이 남긴다.
        호출하는 쪽은 되돌린 트리로 최종 빌드를 다시 한다.

        Applies however the loop ended (cap, budget, stall, stop, a case blocked at its cap). `NO_ROLLBACK`
        and regressed-kept tries stay, a rollback already done stays done. A PARTIAL try stays (blocked or
        not, LLM or zero-LLM pre-try): it removed some errors and no next try comes, so taking it out would
        only hand the owner a tree with more errors; before a case's NEXT try `_rollback_previous` keeps its
        own rule. Newest first (a reset to the base needs the try at HEAD); a revert that fails is a note."""
        last: dict[str, FixAttempt] = {}
        for a in pr.attempts:
            if a.decision.kind == DecisionKind.SCRIPT and a.applied and a.commit:
                last[a.cluster_id] = a
        order = {id(a): i for i, a in enumerate(pr.attempts)}
        todo = [a for a in last.values()
                if a.outcome in ("no_change", "inconclusive") and id(a) not in self._rolled_back]
        done = 0
        for a in sorted(todo, key=lambda x: order[id(x)], reverse=True):
            what = f"v{a.script_version}" if a.script_version and not a.edits_applied else (a.source or "edits")
            try:
                if self.ws.head() == a.commit and a.base_commit:
                    if self.ws.is_dirty():
                        self.ws.discard_uncommitted()
                    self.ws.revert_to(a.base_commit)
                    how = "reset"
                else:
                    res = self.ws._git("revert", "--no-edit", a.commit, check=False)   # noqa: SLF001
                    if res.returncode != 0:
                        self.ws._git("revert", "--abort", check=False)                  # noqa: SLF001
                        why = (res.stderr or res.stdout or "").strip()[:200]
                        a.notes.append(f"rollback before the final build failed: {why}")
                        log.warning("롤백 실패: %s (%s, %s) — 최종 빌드에 남음 (%s)", what, a.id, a.outcome, why)
                        continue
                    how = "git revert"
            except Exception as e:  # noqa: BLE001 - git 문제면 시도를 트리에 남기고 메모한다
                a.notes.append(f"rollback before the final build failed: {type(e).__name__}: {e}")
                continue
            self._rolled_back.add(id(a))
            a.notes.append(ROLLED_BACK_NOTE + "the final build (it did not help)")
            self._sync_try(a)
            done += 1
            self.ws.journal({"event": "script_rollback", "phase": phase.value, "iteration": iteration,
                             "attempt": a.id, "case": a.script_case_id, "version": a.script_version,
                             "outcome": a.outcome, "commit": a.commit, "mode": how, "restored_errors": 0,
                             "where": "loop end"})
            log.info("롤백: %s (%s, %s) 되돌림 (%s) — 루프 끝, 최종 빌드 전", what, a.id, a.outcome, how)
        if done:
            pr.notes.append(f"{done} attempt(s) that did not help rolled back before the final build")
        return done

    def _undo_rollback(self, st: "_Round", prev_att: FixAttempt, head_before: str, restored: list[BuildError],
                       att: FixAttempt) -> None:
        """다음 시도가 아무것도 바꾸지 못했으면 되돌렸던 시도를 되살려 트리를 마지막 빌드 때 상태로 둔다."""
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
            self.ws.revert_to(head_before)
        except Exception as e:  # noqa: BLE001
            log.warning("could not undo the rollback of %s: %s: %s", prev_att.id, type(e).__name__, e)
            return
        for e in restored:
            if e in st.restored:
                st.restored.remove(e)
        att.notes.append(f"the rollback of {prev_att.id} was undone: this attempt changed nothing")
        log.info("롤백 취소: %s 이(가) 아무것도 바꾸지 않아 %s 을(를) 되살림", att.id, prev_att.id)
        self.ws.journal({"event": "script_rollback_undone", "phase": st.phase.value, "iteration": st.iteration,
                         "attempt": prev_att.id, "next_attempt": att.id, "head": head_before})

    def _ask_llm(self, att: FixAttempt, c: ErrorCluster, st: "_Round", step: Any,
                 base: Optional[ScriptBase]) -> Any:
        """FixContext를 만들어 LLM에게 신규나 보강 답을 받는다. 원인, 해결 방법, edits, 스크립트가 들어 있다.
        힌트로 KB의 마지막 해결, 다른 범위의 같은 오류, 권고, 병합 충돌 참고 파일, 의존성 버전 정보를 넣는다.
        전송 실패, 설정 오류, propose_script가 없는 fixer처럼 답을 받지 못하면 None을 반환하고 이유를 시도에 남긴다.
        정체가 쌓였거나 케이스의 세 번째 LLM 요청부터는 상위 모델(``llm.escalation_model``)을 요청한다."""
        # 앞 시도가 만든 오류라면 새 스크립트보다 그 스크립트를 보강하는 편이 낫기 때문에 그 스크립트를 먼저 알린다
        # 그 시도가 트리에 남아 있고 diff를 읽을 수 있으면 힌트 한 줄 대신 그 시도를 설명하는 절을 넣는다
        earlier, described = self._earlier_fix_context(c, att)
        hints = [*self._match_hints.get(c.id, []), *self._introduced_hints(c, skip=described), *self._sibling_hints(c)]
        other = cut_lines("\n".join(v for k, v in st.others.items() if k != c.id), OTHER_ERRORS_CHARS)
        self._mark(f"advisory hints for {c.id}")
        adv_hints, att.advisory_ids = self._advisory_hints(c)
        self._mark(f"merge-conflict hints for {c.id}")
        ref_notes, ref_files = self._merge_conflict_hints(att, c)
        if ref_files:
            self._ref_paths[id(att)] = {_norm_path(k) for k in ref_files}
        mode = step.mode if base is not None or step.mode == "new" else "new"
        if step.mode == "augment" and base is None:
            att.notes.append("augment without a usable base → asking for a new fix")
        elif mode == "augment" and self._decline_count(step.case_id, c):
            # 거절한 케이스에는 보강을 요청하지 않는다. script.max_declines_per_case가 1보다 클 때만 여기에 온다
            mode, base = "new", None
            att.notes.append("the case was declined before → asking for a new fix, not an augment")
        att.script_mode = mode
        if mode == "augment":
            att.script_parent = int(base.version) or None
        # 정체일 때와 케이스의 세 번째 LLM 요청부터는 상위 모델을 쓴다. 보강을 다 쓴 뒤의 신규 요청도 포함한다
        escalate = st.stall_level >= self.cfg.loop.stall_limit or int(getattr(step, "llm_used", 0) or 0) >= 2
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        self._mark(f"library hints for {c.id}")
        lib_hints = self._library_hints(c)
        previous = self._previous(c, ladder=False)
        # 오류를 없앴던 이 케이스의 시도는 그 오류(파일과 서명)가 다시 나타났을 때만 따로 보여 준다
        # 빌드가 더 진행해 처음 보는 파일의 같은 케이스는 다시 나타난 것이 아니다
        now = {_pair(e) for e in c.errors}
        back = [a for a in previous if a.outcome in SCRIPT_KEEP and any(_pair(e) in now for e in a.cluster_errors)]
        ctx = FixContext(phase=st.phase, iteration=st.iteration, stall_level=st.stall_level,
                         profile_summary=self._summary(), plan_notes=self.plan_notes, kb_hints=hints,
                         advisory_hints=adv_hints, previous_attempts=previous, other_errors_summary=other,
                         use_escalation_model=escalate, reference_notes=ref_notes, reference_files=ref_files,
                         script_mode=mode, case_id=step.case_id, signature_generic=sig,
                         script_base=base if mode == "augment" else None, library_hints=lib_hints,
                         layout_facts=self._layout_facts(),
                         scope="jdk-jakarta" if self._reduced_scope() else "full", scope_facts=self._scope_facts(),
                         earlier_fix=earlier, resolved_before=back)
        self._mark(f"fixer.propose_script for {c.id} ({mode}: prompt packing + LLM call)")
        try:
            proposal = self.fixer.propose_script(self.ws, c, ctx)
        except ScriptFixerMissing as e:
            self._fail(att, str(e))
            self._no_script_fixer(att, c, str(e))
            return None
        except Exception as e:
            # 실패한 호출도 호출한 것이므로 예산에 센다
            self.budget.charge_llm(1)
            log.warning("fixer.propose_script failed for %s: %s", c.id, e)
            self._fail(att, f"LLM fixer failed: {type(e).__name__}: {e}")
            if is_llm_transport_error(f"llm-error: {type(e).__name__}: {e}"):
                self._llm_transport_failure(att, c, f"{type(e).__name__}: {e}")
            return None
        # 플러그인이 제안 대신 None이나 다른 값을 반환하면 예외와 같이 이 시도만 실패로 처리한다
        if proposal is None or not all(hasattr(proposal, k) for k in ("llm_usage", "analysis")):
            self.budget.charge_llm(1)
            why = f"LLM fixer returned no script proposal ({type(proposal).__name__})"
            log.warning("fixer.propose_script for %s: %s", c.id, why)
            self._fail(att, why)
            return None
        self.budget.charge_llm(max(1, proposal.llm_usage.calls))
        self._mark(f"LLM answered for {c.id}")
        att.model, att.prompt_hash, att.llm_usage = proposal.model, proposal.prompt_hash, proposal.llm_usage
        att.llm_session_id = getattr(proposal, "session_id", None)      # 보강에서 이어 쓸 게이트웨이 세션
        att.llm_session_model = getattr(proposal, "session_model", "") or ""   # 그 세션의 모델
        att.analysis = proposal.analysis
        if is_llm_transport_error(proposal.analysis):
            self._fail(att, proposal.analysis.strip()[:300])
            self._llm_transport_failure(att, c, proposal.analysis.strip()[len("llm-error:"):].strip()[:300])
            return None
        self._llm_streak, self._streak_attempts = [], []
        if self._llm_config_check(att, c, proposal.analysis, proposal.prompt_hash):
            return None
        if escalate:
            att.notes.append(f"escalation model requested (stall level {st.stall_level})"
                             if st.stall_level >= self.cfg.loop.stall_limit else
                             f"escalation model requested (augment {step.augments_used + 1} of the case)")
        return proposal

    def _layout_facts(self) -> list[str]:
        """프롬프트의 "Project after the restructure" 절의 줄을 만든다. 예외를 던지지 않는다.
        오케스트레이터가 넘긴 재구성 결과와 설정으로 만들고, 결과 수가 바뀔 때만 다시 만든다.
        재구성이 없었으면 빈 목록을 반환한다.

        Inputs: `restructure_results` / `restructure_modes`, `cfg.artifacts` / `cfg.layout`
        (`sbmig.loop.layout_facts`)."""
        results = list(self.restructure_results or [])
        if not results or self._reduced_scope():     # 프레임워크 프로젝트는 재구성하지 않는다
            return []
        if self._layout_cache is not None and self._layout_cache[0] == len(results):
            return list(self._layout_cache[1])
        try:
            from .layout_facts import layout_fact_lines
            case = getattr(getattr(self.profile, "case_type", None), "value", None) or \
                str(getattr(self.profile, "case_type", "") or "")
            lines = layout_fact_lines(results, self.cfg, modes=list(self.restructure_modes or []), case=case,
                                      read_artifact_id=self._artifact_id_of)
        except Exception as e:  # noqa: BLE001 - 정보 절 때문에 시도가 멈추면 안 된다
            log.debug("layout facts unavailable: %s: %s", type(e).__name__, e)
            lines = []
        self._layout_cache = (len(results), list(lines))
        if lines:
            self.ws.journal({"event": "layout_facts", "lines": lines})
        return list(lines)

    def _artifact_id_of(self, rel: str) -> str:
        """프로젝트 pom의 artifactId를 반환한다. 읽을 수 없으면 빈 문자열을 반환한다."""
        ed = self._pom_editor(rel)
        try:
            return str(ed.artifact_id() or "") if ed is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    def _introduced_attempts(self, c: ErrorCluster) -> list[FixAttempt]:
        """이 케이스의 오류를 처음 남긴 다른 케이스의 시도를 오류 순서대로 ``EARLIER_FIX_ATTEMPTS``개까지 반환한다.

        `_score` records them in `_introduced`."""
        seen: list[FixAttempt] = []
        for e in c.errors:
            a = self._introduced.get(_pair(e))
            if a is not None and a not in seen and not _same_case(a, c):
                seen.append(a)
        return seen[:EARLIER_FIX_ATTEMPTS]

    def _introduced_hints(self, c: ErrorCluster, skip: Iterable[int] = ()) -> list[str]:
        """이 케이스의 오류를 처음 남긴 앞 시도를 알려 주는 힌트 줄을 만든다. 없으면 빈 목록을 반환한다.
        힌트는 ``introduced by script:<case>:v<N>``으로 시작한다. edits 시도면 그 시도 이름만 쓴다.
        ``skip``에 있는 시도는 따로 절로 설명하므로 힌트를 만들지 않는다."""
        skipped = set(skip)
        out: list[str] = []
        for a in self._introduced_attempts(c):
            if id(a) in skipped:
                continue
            if a.script_version and a.script_case_id and not a.edits_applied:
                out.append(f"introduced by script:{a.script_case_id}:v{a.script_version} (attempt {a.id}) — prefer "
                           "augmenting that script")
            else:
                out.append(f"introduced by {self._attempt_name(a)} "
                           f"(attempt {a.id}) — its change is in the tree; fix what it left, do not undo it")
        return out

    def _still_in_tree(self, a: FixAttempt) -> bool:
        """시도가 적용되어 커밋됐고, 커밋 범위를 알고, 아직 되돌리지 않았으면 True를 반환한다."""
        return bool(a.applied and a.commit and a.base_commit) \
            and a.outcome not in ("reverted", "pending", "apply_failed", "blocked") \
            and id(a) not in self._rolled_back and not any(ROLLED_BACK_NOTE in n for n in a.notes)

    def _earlier_fix_context(self, c: ErrorCluster, att: FixAttempt) -> tuple[str, set[int]]:
        """이 케이스의 오류를 남긴 다른 케이스의 시도를 설명하는 절의 본문과, 설명한 시도의 id() 집합을 반환한다.
        트리에 없거나 diff를 읽지 못한 시도는 빼고 힌트 한 줄로 남긴다. 예외를 던지지 않는다.

        "v2.1z-by": the body of the section "Earlier fix these errors come from (it is in the tree)"
        (`FixContext. earlier_fix`, capped at EARLIER_FIX_CHARS) and the ids of the attempts it describes.
        Only an attempt that resolved its own case (`SCRIPT_KEEP`) is described; one kept without resolving it
        (regressed behind a later commit, partial, inconclusive) keeps the hint, as does an attempt not in the
        tree, without a commit range, whose diff does not read (git, decoding) or whose diff has no line in
        this case's files (`_introduced_hints`); every problem is logged at DEBUG. Journal
        `earlier_fix_context` when a section is built."""
        try:
            from sbmig.fix.context import earlier_fix_text
            case_files = {_norm_path(e.file) for e in c.errors if e.file}
            fixes, described, ids = [], set(), []
            for a in self._introduced_attempts(c):
                # 자기 케이스를 해결하지 못한 시도는 되돌리면 안 되는 수정이 아니므로 힌트 한 줄만 남긴다
                if a.outcome not in SCRIPT_KEEP or not self._still_in_tree(a):
                    continue
                try:
                    fx = self._earlier_fix(a, case_files)
                except Exception as e:  # noqa: BLE001 - git이나 디코딩 문제면 힌트 한 줄로 남긴다
                    log.debug("earlier fix of %s for %s unavailable: %s: %s", a.id, c.id, type(e).__name__, e)
                    continue
                if fx is None:
                    continue
                fixes.append(fx)
                described.add(id(a))
                ids.append(a.id)
            if not fixes:
                return "", set()
            text = earlier_fix_text(fixes, EARLIER_FIX_CHARS)
            self.ws.journal({"event": "earlier_fix_context", "case": c.case_id or c.id, "attempt": att.id,
                             "described": ids, "chars": len(text)})
            return text, described
        except Exception as e:  # noqa: BLE001 - 이 절 때문에 시도가 멈추면 안 된다
            log.debug("earlier fix context for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return "", set()

    def _earlier_fix(self, a: FixAttempt, case_files: set[str]) -> Any:
        """시도의 커밋에서 이 케이스 파일의 diff 줄과 다른 파일 이름을 읽어 ``EarlierFix``를 반환한다.
        이 케이스 파일에 바뀐 줄이 없으면 None을 반환한다. git이나 디코딩 문제는 예외를 던진다.

        The `EarlierFix` of one attempt: its error lines, the +/- / @@ lines of `git diff base..commit` for the files of
        the CURRENT case (decoded strictly with the workspace's encoding candidates; ValueError when none fits), the
        other files of the commit named only. None when no line of this case's files changed."""
        from sbmig.fix.context import EarlierFix, dedupe_errors, format_error_line
        res = self.ws._git("diff", "--name-only", "-z", "--no-renames", a.base_commit, a.commit,  # noqa: SLF001
                           check=False)
        if res.returncode != 0:
            raise RuntimeError(f"git diff --name-only failed: {(res.stderr or '').strip()[:200]}")
        changed = [p for p in (res.stdout or "").split("\0") if p]
        mine = [p for p in changed if _norm_path(p) in case_files]
        shown = mine[:EARLIER_FIX_FILES]
        diffs: list[tuple[str, list[str], int]] = []
        left = EARLIER_FIX_DIFF_LINES
        for rel in shown:
            raw = self.ws._git_bytes("diff", "--no-color", "--no-renames", a.base_commit, a.commit,  # noqa: SLF001
                                     "--", rel, check=False)
            if raw.returncode != 0:
                raise RuntimeError(f"git diff of {rel} failed")
            lines = _diff_change_lines(_decode_strict(raw.stdout or b"", self.ws.encoding_candidates))
            take = lines[:max(left, 0)]
            left -= len(take)
            if lines:
                diffs.append((_norm_path(rel), take, len(lines) - len(take)))
        if not any(ls for _p, ls, _m in diffs):
            return None
        rest = [_norm_path(p) for p in mine if p not in shown]
        others = [_norm_path(p) for p in changed if p not in mine]
        other_names = others[:EARLIER_FIX_OTHER_FILES] + \
            ([f"… {len(others) - EARLIER_FIX_OTHER_FILES} more"] if len(others) > EARLIER_FIX_OTHER_FILES else [])
        case_names = rest[:EARLIER_FIX_OTHER_FILES] + \
            ([f"… {len(rest) - EARLIER_FIX_OTHER_FILES} more"] if len(rest) > EARLIER_FIX_OTHER_FILES else [])
        errs = dedupe_errors(list(a.cluster_errors))
        return EarlierFix(attempt_id=a.id, name=self._attempt_name(a),
                          error_lines=[format_error_line(e) for e in errs[:EARLIER_FIX_ERROR_LINES]],
                          more_errors=max(0, len(errs) - EARLIER_FIX_ERROR_LINES), diffs=diffs,
                          other_files=other_names, case_files=case_names)

    def _store_script(self, att: FixAttempt, c: ErrorCluster, step: Any, ans: Any, code: str, *,
                      check_duplicate: bool = True) -> bool:
        """LLM이 작성한 스크립트를 KB 케이스에 저장한다. KB가 없으면 저장하지 않고 v0로 둔다.
        - 실행할 스크립트는 실패해도 이력이 남도록 실행 전에 저장한다. edits 답에 딸린 스크립트는 unverified로 저장한다
        - 보강한 버전에는 부모 버전(``parent``)과 만든 이유(``reason``)를 남긴다
        False를 반환하면 같은 버전이 이번 실행에서 이미 실행됐다는 뜻이라 실행하지 않는다.

        `kb.cases.add_script`, source = the try's. `reason` = `v1: <the parent's failure in words>`."""
        store = self.cases
        if not store.active:
            att.notes.append("fix script not stored (no knowledge base): v0")
            return True
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        lineage: dict[str, Any] = {"parent": att.script_parent, "reason": ""}
        if att.script_mode == "augment":
            base_att = getattr(step, "base_attempt", None)
            note = self._run_reason(base_att) if base_att is not None else ""
            meta = getattr(step, "meta", None)
            if not note and meta is None and att.script_parent:      # 이전 실행이 저장한 버전
                info = store.find(step.case_id)
                meta = next((x for x in (getattr(info, "scripts", None) or [])
                             if int(getattr(x, "version", 0) or 0) == att.script_parent), None)
            if not note and meta is not None:
                note = str(getattr(meta, "last_failure", "") or getattr(meta, "last_result", "") or "")
            note = note or "augment requested"
            lineage["reason"] = f"v{att.script_parent}: {note}" if att.script_parent else note
        # 이전 저장소가 받지 않는 키워드는 어댑터(sbmig.loop.entries.CaseScripts)가 뺀다
        cs = store.add_script(step.case_id, sig, code, source=att.source or "llm-new", run_id=self.ws.run_id,
                              project=self._project(), analysis=getattr(ans, "analysis", "") or "",
                              solution=getattr(ans, "solution", "") or "", scope=self._run_scope(), **lineage)
        if cs is None:
            att.notes.append("fix script not stored (kb.cases.add_script failed): v0")
            return True
        att.script_version = int(getattr(cs, "version", 0) or 0)
        # 저장 로그는 저장소가 직접 남기므로 여기서는 DEBUG 한 줄만 남긴다
        log.debug("stored as v%d (%s) → %s", att.script_version, getattr(cs, "status", "unverified"),
                  script_path(step.case_id, cs))
        # 같은 코드를 거부하는 기준은 이 위치에서 한 시도뿐이다. 다른 위치에서 실행한 버전은 여기서 다시 실행한다
        ran = next((a for a in _same_cluster(c, self.history, self.blocked.canonical_file)
                    if a.decision.kind == DecisionKind.SCRIPT and a.script_version == att.script_version
                    and att.script_version and a.script_result is not None and self._made_here(c, a)), None)
        self.ws.journal({"event": "script_stored", "case": step.case_id, "version": att.script_version,
                         "mode": att.script_mode, "source": att.source, "attempt": att.id,
                         "status": getattr(cs, "status", ""), "duplicate_of": ran.id if ran is not None else None})
        if ran is not None and check_duplicate:
            self._fail(att, f"the LLM answered with v{att.script_version} again (identical code), which already "
                            f"ran in this run ({ran.id}: {ran.outcome}) — not run again")
            return False
        return True

    def _run_script(self, att: FixAttempt, c: ErrorCluster, code: str, st: "_Round") -> None:
        """스크립트를 프로젝트에 실행한다.
        ``sbmig.script``가 정적 검사, ``python -I`` 별도 프로세스, 프로젝트 폴더 한정, 타임아웃을 적용한다.
        거부, 오류, 타임아웃이면 아무것도 적용하지 않고 작업 트리를 복원한다.
        성공해서 바뀐 것이 있으면 스크립트가 보고한 파일만 커밋한다."""
        case_id, v = att.script_case_id, att.script_version
        runner = self.script_runner
        if runner is None:
            return self._fail(att, "no script runner (sbmig.script) in this run")
        pre = self.ws.head()
        self._script_ran[id(att)] = True
        self._mark(f"fix script {case_id}:v{v} on {c.id}")
        with self._busy_with(f"fix script {case_id}:v{v}"):
            try:
                res = runner.run(self.ws, code, errors=list(c.errors))
            except Exception as e:  # noqa: BLE001 - 실행기는 예외를 던지지 않지만 방어한다
                res = ScriptRunResult(status="error", error=f"script runner failed: {type(e).__name__}: {e}")
        att.script_result = res
        name = f"script v{v}" if v else "script (unstored)"
        if res.status != "ok":
            why = f"script {res.status}: {(res.error or '').strip()[:400] or 'no detail'}"
            self._fail(att, why)                   # 일부만 적용하지 않는다. 작업 트리는 복원된다
            if res.problems:
                att.notes.append("static check: " + "; ".join(res.problems[:5]))
            log.info("적용 실패 (%s %s): %s — 변경 없음", name, res.status, _first_sentence(res.error or "-", 240))
            return
        self._drop_reference_writes(att, pre)
        # sbmig의 버전 채우기를 스크립트의 두 번째 변경으로 오인하지 않도록 버전 채우기보다 먼저 실행한다
        self._idempotency_check(att, c, code, res)
        self._fill_versions(att, c, res, pre, st)      # 버전 없는 의존성에 버전을 채워 같은 커밋에 넣는다
        if self._framework_rejected(att, pre, list(res.changed_files or []), name):
            return
        if self._edit_guard_rejected(att, pre, list(res.changed_files or []), name):
            return
        self._mark(f"git commit for {c.id}")
        sha = self._commit(f"fix(script:{case_id}:v{v} {att.script_mode}): {c.id}", pre, paths=res.changed_files)
        if sha is None:
            att.outcome = "apply_failed"
            att.apply_errors.append("no file changed")
            log.info("적용 실패 (%s): 실행은 ok, 바뀐 파일 없음", name)
            return
        self._applied(att, c, st, pre, sha, name, list(res.changed_files))

    def _idempotency_check(self, att: FixAttempt, c: ErrorCluster, code: str, res: ScriptRunResult) -> None:
        """파일을 바꾸고 ``ok``로 끝난 스크립트를 그 결과 위에서 한 번 더 실행해 멱등인지 확인한다. 예외를 던지지 않는다.
        두 번째 실행이 또 파일을 바꾸면 그 변경만 되돌리고, 저장된 버전을 ``non_idempotent``로 표시해 재사용하지 않는다.
        ``script.idempotency_check``로 끈다.

        The first run's result is committed as usual and the attempt's outcome stays whatever the rebuild
        scores; a note goes on the attempt and the journal gets `script_non_idempotent`. Runs before the
        version fill so sbmig's own pom edits are never mistaken for the script's."""
        try:
            if att.source in PRE_TRY_SOURCES or self.script_runner is None \
                    or not getattr(getattr(self.cfg, "script", None), "idempotency_check", True) \
                    or not res.changed_files or not self.ws.is_dirty():
                return
            snap: dict[str, Optional[bytes]] = {}
            for f in dict.fromkeys(_norm_path(x) for x in res.changed_files if x):
                full = self.ws.abs(f)
                snap[f] = full.read_bytes() if full.is_file() else None
        except Exception as e:  # noqa: BLE001 - 확인 단계일 뿐이라 시도를 실패시키지 않는다
            log.debug("idempotency check of %s skipped: %s: %s", att.id, type(e).__name__, e)
            return
        v = int(att.script_version or 0)
        self._mark(f"idempotency check of {att.script_case_id}:v{v}")
        with self._busy_with(f"fix script {att.script_case_id}:v{v} (second run)"):
            try:
                res2 = self.script_runner.run(self.ws, code, errors=list(c.errors))
            except Exception as e:  # noqa: BLE001 - 실행기는 예외를 던지지 않지만 방어한다
                res2 = ScriptRunResult(status="error", error=f"script runner failed: {type(e).__name__}: {e}")
        changed2 = list(dict.fromkeys(_norm_path(x) for x in (res2.changed_files or []) if x))
        self._restore_first_run(snap, changed2)
        if not changed2:
            log.debug("idempotency check of %s: the second run changed nothing (%s)", att.id, res2.status)
            return
        shown = ", ".join(changed2[:3]) + (f" (+{len(changed2) - 3})" if len(changed2) > 3 else "")
        name = f"script v{v}" if v else "the unstored script"
        note = (f"{NON_IDEMPOTENT_NOTE}a second run of {name} changed {shown} again — those second changes were "
                "undone" + ("; the stored version is marked non_idempotent (never reused)" if v else ""))
        att.notes.append(note)
        marked = bool(v and att.script_case_id and self.cases.active
                      and self.cases.mark_non_idempotent(att.script_case_id, v, note))
        log.warning("비멱등 스크립트: %s 를 한 번 더 돌렸더니 %s 을(를) 또 바꿈 — 두 번째 변경은 되돌림%s", name, shown,
                    ", 재사용 안 함 (non_idempotent)" if marked else "")
        self.ws.journal({"event": "script_non_idempotent", "case": att.script_case_id, "version": v,
                         "attempt": att.id, "files": changed2[:20], "second_status": res2.status, "marked": marked})

    def _restore_first_run(self, snap: dict[str, Optional[bytes]], changed2: list[str]) -> None:
        """두 번째 실행이 바꾼 파일을 첫 실행 직후 상태로 되돌린다.
        첫 실행이 바꾼 파일은 기억해 둔 내용으로 복원하고, 그때 없던 파일은 지운다.
        나머지는 HEAD 내용으로 복원하고 HEAD에 없으면 지운다. 시도는 아직 커밋 전이다."""
        for f in changed2:
            try:
                full = self.ws.abs(f)
                if f in snap:
                    data = snap[f]
                    if data is None:
                        if full.is_file():
                            full.unlink()
                    else:
                        full.parent.mkdir(parents=True, exist_ok=True)
                        full.write_bytes(data)
                elif self.ws._git("cat-file", "-e", f"HEAD:{f}", check=False).returncode == 0:   # noqa: SLF001
                    self.ws._git("checkout", "HEAD", "--", f, check=False)                     # noqa: SLF001
                elif full.is_file():
                    full.unlink()
            except Exception as e:  # noqa: BLE001 - 실패해도 계속한다. 커밋은 첫 실행의 파일만 담는다
                log.warning("could not undo the second run's change of %s: %s: %s", f, type(e).__name__, e)

    def _apply_edits(self, att: FixAttempt, c: ErrorCluster, edits: list, st: "_Round",
                     subject: str | None = None, apply_fn: Callable[[], ScriptRunResult] | None = None) -> None:
        """LLM 답의 ``<edits>``를 ``sbmig.script.apply_edits``로 적용하고 커밋한다.
        스크립트와 같은 안전장치를 쓴다. 프로젝트 폴더 안만 바꾸고 거부 규칙도 같다.
        실패하면 아무것도 적용하지 않고 작업 트리를 복원한다. 커밋 제목은 ``fix(edits:<case> <source>)``다.

        `subject` overrides the commit subject (`fix(sbmig-version): <case>`). `apply_fn` (sbmig's own pom
        change that edits the files itself, e.g. `apply_non_filtered_extensions`) replaces the edit applier;
        the rest (failure handling, commit of the files it reports, `_applied`) is the same."""
        pre = self.ws.head()
        self._mark(f"applying {len(edits)} edit(s) for {c.id}")
        if apply_fn is None:
            try:                                   # 추적용일 뿐이다. 무엇이 바뀌었는지는 diff로 남는다
                from sbmig.script import edits_script
                log.debug("edits as a script:\n%s", edits_script(edits))
            except Exception:  # noqa: BLE001 - sbmig.script에 이 함수가 없을 수 있다
                pass
        with self._busy_with(f"edits for {att.script_case_id}"):
            try:
                if apply_fn is not None:           # sbmig가 직접 pom을 바꾸는 사전 시도
                    res = apply_fn()
                elif self.edit_applier is not None:
                    res = call_edit_applier(self.edit_applier, self.ws, edits, self.cfg, list(c.errors))
                else:                              # 자체 실행기다. `write`에는 script.max_code_chars 한도가 없다
                    from sbmig.script import apply_edits
                    res = apply_edits(self.ws, edits, cfg=self.cfg, errors=list(c.errors))
            except Exception as e:  # noqa: BLE001 - ImportError나 고장 난 applier면 아무것도 적용하지 않는다
                res = ScriptRunResult(status="error", error=f"the edits could not be applied: {type(e).__name__}: {e}")
        if res is None:
            res = ScriptRunResult(status="error", error="the edit applier returned nothing")
        self._edit_results[id(att)] = res
        if res.status != "ok":
            self._fail(att, f"edits {res.status}: {(res.error or '').strip()[:400] or 'no detail'}")
            if res.problems:
                att.notes.append("edit check: " + "; ".join(res.problems[:5]))
            log.info("적용 실패 (edits %s): %s — 변경 없음", res.status, _first_sentence(res.error or "-", 240))
            return
        self._drop_reference_writes(att, pre)
        self._fill_versions(att, c, res, pre, st)      # 버전 없는 의존성에 버전을 채워 같은 커밋에 넣는다
        if self._framework_rejected(att, pre, list(res.changed_files or []), "edits"):
            return
        if self._edit_guard_rejected(att, pre, list(res.changed_files or []), att.source or "edits"):
            return
        self._mark(f"git commit for {c.id}")
        if subject is None:
            subject = f"fix(edits:{att.script_case_id} {att.source or 'llm'}): {c.id}"
        sha = self._commit(subject, pre, paths=res.changed_files)
        if sha is None:
            att.outcome = "apply_failed"
            att.apply_errors.append("the edits changed no file")
            log.info("적용 실패 (edits): 바뀐 파일 없음")
            return
        att.edits_applied = len(edits) or len(res.changed_files or [])
        self._applied(att, c, st, pre, sha, "edits", list(res.changed_files))

    def _applied(self, att: FixAttempt, c: ErrorCluster, st: "_Round", pre: str, sha: str, how: str,
                 reported: list[str]) -> None:
        """커밋된 변경을 정리하고 시도를 결과 평가 대기 목록에 넣는다.
        - 시도의 파일 목록과 diff를 남기고, ``적용(…)`` 줄과 수정 메모마다 ``수정: …`` INFO 줄을 남긴다
        - pom을 바꿨으면 변경을 다시 읽어 확인하고, 바뀐 파일이 많으면 경고한다

        Notes = the LLM's descriptions of the edits / script plus lines from the diff; a reused stored script
        has no answer, so its notes come from the diff only. The full diff goes to DEBUG, the FixTry keeps a
        capped one."""
        att.applied, att.commit = True, sha
        try:
            files = self._changed_between(pre, sha)
        except Exception:  # pragma: no cover - git 문제면 수정이 보고한 파일 목록을 쓴다
            files = list(reported)
        att.changes = [FileChange(path=f, op="modify" if self.ws.exists(f) else "delete") for f in files]
        stats = self._numstat(pre, sha)
        try:
            diff = self.ws.diff(pre, sha)
        except Exception as e:  # noqa: BLE001 - 커밋은 있고 diff 텍스트만 없다
            diff = ""
            log.debug("diff %s..%s unavailable: %s", pre[:10], sha[:10], e)
        self._diffs[id(att)] = diff
        shown = [f"{p} (+{a} −{d})" for p, a, d in stats] or files
        log.info("적용(%s): %s, 커밋 %s", how, ", ".join(shown[:8]) + (f", … (+{len(shown) - 8})" if len(shown) > 8
                                                                       else ""), sha[:7])
        llm_notes = answer_notes(self._answers.get(id(att)), edits=how == "edits")
        try:                                           # as-is / to-be 줄은 한쪽에 errors.snippet_lines까지
            notes = try_notes(llm_notes, diff,
                              max_lines=int(getattr(getattr(self.cfg, "errors", None), "snippet_lines", 0) or 0))
        except TypeError as e:                         # max_lines를 받지 않는 이전 try_notes
            if "max_lines" not in str(e):
                raise
            notes = try_notes(llm_notes, diff)
        self._notes[id(att)] = notes
        for n in notes[:NOTES_LOGGED]:
            log.info("수정: %s", note_line(n))
        if len(notes) > NOTES_LOGGED:
            log.info("수정: … %d more (errors.md)", len(notes) - NOTES_LOGGED)
        if diff:
            log.debug("diff:\n%s", diff)
        if pom_changed(files or reported):             # 메이저 업그레이드와 라이브러리 교체는 운영자가 확인해야 한다
            self._read_back(att, c, st, pre, sha, diff)
        warn = int(getattr(getattr(self.cfg, "script", None), "max_files_changed_warn", 0) or 0)
        if warn and len(files) > warn:
            note = (f"the fix changed {len(files)} files (more than script.max_files_changed_warn = {warn}) — "
                    "check its diff")
            att.notes.append(note)
            log.warning("%s", note)
        st.pending.append(_Pending(att, pre, sha))
        st.applied_any = True

    # ------------------------------------------------------------------ 의존성 버전 정책
    def _read_back(self, att: FixAttempt, c: ErrorCluster, st: _Round, pre: str, sha: str, diff: str) -> None:
        """적용한 커밋의 pom 변경에서 메이저 업그레이드와 라이브러리 교체를 찾아 ``att.major_upgrades``에 넣는다.
        운영자 확인 가이드도 함께 붙인다. 빌드 통과가 목표라 메이저 버전이 바뀌어도 되돌리거나 차단하지 않는다.
        실패는 DEBUG 한 줄로만 남긴다.

        `sbmig.build.depchanges`; the guide comes from `_verify_guide`."""
        try:
            changes = operator_changes(self.ws, pre, sha, self.cfg)
            if not changes:
                return
            att.major_upgrades = changes
            guide, source = self._verify_guide(att, c, st, changes, diff)
            att.verify, att.verify_source = guide, source
            offered = {k: v for k, v in ((ch.key, self._same_major_offer(ch)) for ch in changes) if v}
            log_major_upgrades(changes, guide, self.cfg, offered)
            for ch in changes:
                self.ws.journal({"event": "major_upgrade", "attempt": att.id, "case": att.script_case_id,
                                 "change": ch.model_dump(mode="json"), "guide_source": source})
        except Exception as e:  # noqa: BLE001 - 보고용 정보라 시도를 실패시키지 않는다
            log.debug("dependency read-back for %s failed: %s: %s", att.id, type(e).__name__, e)

    def _verify_guide(self, att: FixAttempt, c: ErrorCluster, st: _Round, changes: list, diff: str
                      ) -> tuple[str, str]:
        """운영자 확인 가이드를 만들어 (가이드, "llm" 또는 "sbmig")를 반환한다.
        답의 ``<verify>``를 먼저 쓰고, 없으면 LLM에 한 번 요청하고, 그것도 없으면 sbmig가 만든다.
        저장 스크립트 재사용과 사전 시도는 LLM을 호출하지 않는다. 재사용이 LLM 없이 끝난다는 장점을 지키기 위해서다.

        The LLM call needs `libraries.verify_guide_llm`, an available LLM, budget and no stop / time halt
        (docs/INTERFACES.md "v2.1d")."""
        ans = self._answers.get(id(att))
        text = str(getattr(ans, "verify", "") or "").strip() if ans is not None else ""
        if text:
            return text, "llm"
        fx = self._verify_fixer()
        if fx is not None and att.source != "kb-script" and att.source not in PRE_TRY_SOURCES \
                and getattr(getattr(self.cfg, "libraries", None), "verify_guide_llm", True) \
                and not self.dry_run and not self.llm_unavailable and self.budget.llm_allowed() \
                and not self._halt_before_ask():
            ctx = FixContext(phase=st.phase, iteration=st.iteration, profile_summary=self._summary(),
                             case_id=att.script_case_id, scope="jdk-jakarta" if self._reduced_scope() else "full")
            self._mark(f"fixer.verify_guide for {c.id}")
            got = ""
            try:
                try:
                    got = fx.verify_guide(self.ws, c, ctx, list(changes), diff, solution=att.solution or "")
                except TypeError as e:              # `solution`을 받지 않는 VerifyGuideLike
                    if "solution" not in str(e):
                        raise
                    got = fx.verify_guide(self.ws, c, ctx, list(changes), diff)
                got = str(got or "").strip()
            except Exception as e:  # noqa: BLE001 - 예외를 던지지 않는 계약이지만 방어한다
                log.debug("verify_guide failed for %s: %s: %s", c.id, type(e).__name__, e)
            self._charge_verify_usage(att, fx)
            if got:
                return got, "llm"
        return fallback_guide(changes, self._lang()), "sbmig"

    def _verify_fixer(self) -> Any:
        """``verify_guide``를 가진 fixer를 반환한다. 없으면 None을 반환하고 sbmig가 가이드를 만든다.
        fixer 자신이거나 ``LazyFixer`` 뒤의 실제 fixer다. 가이드 하나 때문에 LLM 클라이언트를 새로 만들지 않도록
        실제 fixer는 propose_script가 이미 만들어 둔 경우에만 쓴다."""
        fx = self.fixer
        if callable(getattr(fx, "verify_guide", None)):
            return fx
        inner = getattr(fx, "_fixer", None) if isinstance(fx, LazyFixer) else None
        return inner if inner is not None and callable(getattr(inner, "verify_guide", None)) else None

    def _charge_verify_usage(self, att: FixAttempt, fx: Any) -> None:
        """verify_guide 호출의 사용량을 예산과 시도에 더한다. 일찍 실패했어도 요청은 했으므로 1회 이상으로 센다.

        `fixer.last_verify_usage` (an LLMUsage); a fixer without it = one call."""
        u = getattr(fx, "last_verify_usage", None)
        if not isinstance(u, LLMUsage):
            u = LLMUsage(calls=1)
        elif not u.calls:
            u = u.model_copy(update={"calls": 1})
        self.budget.charge_llm(max(1, int(u.calls or 0)))
        att.llm_usage = att.llm_usage.add(u)

    def _lang(self) -> str:
        """sbmig가 만드는 가이드의 언어를 ``report.language``에서 반환한다. ko 또는 en이고 기본값은 ko다."""
        lang = str(getattr(getattr(self.cfg, "report", None), "language", "") or "ko").lower()
        return lang if lang in ("en", "ko") else "ko"

    def _same_major_offer(self, ch: Any) -> str:
        """저장소나 표가 제시했던 같은 메이저의 최신 jakarta 릴리스를 반환한다. 없으면 빈 문자열을 반환한다.
        메이저 업그레이드 대신 쓸 수 있었던 버전을 경고 줄에 함께 보여 줄 때 쓴다."""
        try:
            rv = self._repo_facts.get(ch.key)
            if rv is not None:
                hit = [v.version for v in rv.same_major if v.jakarta]
                if hit:
                    return hit[-1]
            entry = table_entry(ch.key, self.cfg)
            if entry is not None and entry.version:
                from sbmig.build.depchanges import major_of
                if major_of(entry.version) is not None and major_of(entry.version) == major_of(ch.before):
                    return entry.version
        except Exception as e:  # noqa: BLE001
            log.debug("same-major offer for %s unavailable: %s", getattr(ch, "key", "?"), e)
        return ""

    def _library_hints(self, c: ErrorCluster) -> list[str]:
        """프롬프트의 "Dependency versions" 절을 만든다. 예외를 던지지 않는다.
        - 맨 앞은 정보 줄이다. as-is 버전, 새 부모 체인과 목표 Boot BOM의 관리 버전, 체인 속성, 패키지와 리소스 정보다
        - 그다음 라이브러리마다 저장소 정보와 판단을 붙인다. javax 케이스에만 붙인다
        - 새 체인이나 Boot BOM이 관리하는 라이브러리에는 정보 줄 하나만 둔다. API가 바뀐 것이니 버전을 올리라고 안내하지 않는다
        라이브러리별 결과는 실행 동안 캐시한다.

        Facts: `_version_facts` (the errors' artifacts and the libraries `_case_libraries` locates on the
        classpath), `_chain_property_facts`, `_package_facts`, `_resources_facts`. Repository lines:
        `RepositoryLookup.candidates` -> `hint_lines`, only when an error names `javax.` or the library is in
        the javax table."""
        libs: list[GAV] = []
        try:
            libs = self._case_libraries(c) or self._javax_libraries(c)   # 못 찾으면 클래스 이름으로 찾는다
        except Exception as e:  # noqa: BLE001 - 힌트 때문에 시도가 멈추면 안 된다
            log.debug("case libraries for %s unavailable: %s: %s", c.id, type(e).__name__, e)
        facts = self._version_facts(c, libs) + self._chain_property_facts(c, libs) + self._package_facts(c) + \
            self._resources_facts(c)
        try:
            out: list[str] = list(facts)
            javax_case = _mentions_javax(c.errors)
            listed: list[str] = []
            for gav in libs:
                if self._is_framework_key(gav.key):
                    # 프레임워크 아티팩트는 저장소 목록이나 판단 없이 바꾸지 말라는 정보 한 줄만 보낸다
                    out.append(f"framework artifact {gav.key}: keep its declaration and version as they are — if it is "
                               "still compiled against javax.*, only its owner can fix that (answer <blocked> naming it)")
                    continue
                if self._known_version(gav.key, self._lib_module(c)):
                    continue                            # 정보 줄로 충분하다. 목록과 판단은 붙이지 않는다
                if not (javax_case or table_entry(gav.key, self.cfg) is not None):
                    continue                            # 저장소 정보는 javax 라이브러리에만 붙인다
                ck = f"{gav.key}#javax" if javax_case else gav.key
                lines = self._library_hint_cache.get(ck)
                if lines is None:
                    lines = self._library_lines(gav.key, gav.version, javax_case)
                    self._library_hint_cache[ck] = lines
                out += lines
                listed.append(gav.key)
            out = list(dict.fromkeys(x for x in out if x))
            if out:
                self.ws.journal({"event": "library_hints", "case": c.case_id or c.id,
                                 "keys": [g.key for g in libs], "listed": listed, "lines": out})
            return out
        except Exception as e:  # noqa: BLE001 - 힌트 때문에 시도가 멈추면 안 된다
            log.debug("library hints for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return list(facts)

    def _lib_module(self, c: ErrorCluster) -> Optional[str]:
        """classpath에서 라이브러리를 찾을 모듈을 반환한다. 첫 컴파일 오류의 모듈이고, 없으면 클러스터의 모듈이다."""
        return next((e.module for e in c.errors if e.module and e.kind in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE)),
                    None) or c.module

    def _known_version(self, key: str, module: Optional[str]) -> Optional[tuple[str, str]]:
        """새 부모 체인이나 목표 Boot BOM이 이 g:a를 관리하면 (관리하는 쪽, 버전)을 반환한다. 아니면 None을 반환한다."""
        try:
            r = self._versions()
            mv = r.managed_version(key, module) if r.has_chain else None
            if mv:
                return self._chain_label(), mv
            bv = r.boot_version(key, module)
            if bv:
                return "target Boot BOM", bv
        except Exception as e:  # noqa: BLE001
            log.debug("managed version of %s unavailable: %s: %s", key, type(e).__name__, e)
        return None

    def _case_libraries(self, c: ErrorCluster) -> list[GAV]:
        """케이스와 관련된 외부 라이브러리를 g:a당 하나씩 ``libraries.max_artifacts_per_case``개까지 찾아 반환한다.
        오류의 ``artifact``를 쓰고, 없으면 앞쪽 컴파일 오류의 소유 클래스나 오류 줄에서 호출한 클래스를
        classpath에서 찾는다. 사내 아티팩트와 프로젝트 클래스는 뺀다."""
        from .blocked_registry import is_in_house
        cap = int(getattr(getattr(self.cfg, "libraries", None), "max_artifacts_per_case", 3) or 0)
        if cap <= 0:
            return []
        found: dict[str, GAV] = {}
        for e in c.errors:
            a = e.artifact
            if a is None or not a.group_id or not a.artifact_id or is_in_house(a, self.cfg):
                continue
            found.setdefault(a.key, a)
            if len(found) >= cap:
                return list(found.values())
        loc = self.locator
        if found or loc is None or not hasattr(loc, "locate_class"):
            if not found:
                log.debug("library hints for %s: no artifact on the errors and no classpath locator", c.id)
            return list(found.values())
        seen: set[str] = set()

        def take(where: Any) -> bool:
            a = getattr(where, "artifact", None) if where is not None else None
            if a is None or getattr(where, "in_project", False) or is_in_house(a, self.cfg):
                return False
            found.setdefault(a.key, a)
            return len(found) >= cap

        for e in [x for x in c.errors if x.kind in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE)][:3]:
            names: list[str] = []
            if hasattr(loc, "owner_fqcn"):
                names.append(loc.owner_fqcn(e) or "")
            if hasattr(loc, "type_mismatch"):
                names.append((loc.type_mismatch(e) or (None, None))[1] or "")     # 타입 불일치의 실제 타입
            for fq in names:
                if not fq or fq in seen or fq.startswith(("java.", "javax.", "jakarta.")):
                    continue
                seen.add(fq)
                self._mark(f"locating {fq} for {c.id}")
                if take(loc.locate_class(self.ws, self.cfg, fq, c.module)):
                    return list(found.values())
            # 오류 줄에서 호출한 라이브러리 클래스를 찾는다. 차단 목록과 같은 방법이다(locate_type_owner)
            # 예: `cannot access javax.servlet…`이나 `incompatible types` 오류의 `LegacyRequestHelper`
            if hasattr(loc, "locate_type_owner"):
                self._mark(f"locating the type owner of {e.file}:{e.line} for {c.id}")
                if take(loc.locate_type_owner(self.ws, self.cfg, e, c.module)):
                    return list(found.values())
        if not found:
            log.debug("library hints for %s: no external library located behind the first errors", c.id)
        return list(found.values())

    def _repository(self) -> Any:
        """이 실행의 저장소 조회기를 반환한다. Nexus와 미러의 maven-metadata.xml을 읽는다.
        ``libraries.lookup``이 꺼졌거나 만들 수 없으면 None을 반환한다. 그 뒤로는 실행 내내 조회하지 않고 표만 쓴다.

        `RepositoryLookup`, built on first use; one WARNING when it cannot be built."""
        if self._repo_lookup is not None or self._repo_lookup_off:
            return self._repo_lookup
        if not getattr(getattr(self.cfg, "libraries", None), "lookup", True):
            self._repo_lookup_off = True
            return None
        try:
            from sbmig.build.repo_versions import RepositoryLookup
            try:
                # project_root를 주면 pom에 선언된 저장소도 조회한다
                self._repo_lookup = RepositoryLookup(self.cfg, project_root=self.ws.root)
            except TypeError:                           # 그 키워드를 받지 않는 테스트용이나 플러그인 조회기
                self._repo_lookup = RepositoryLookup(self.cfg)
        except Exception as e:  # noqa: BLE001 - 조회하지 못하면 프롬프트에는 표와 정책만 들어간다
            self._repo_lookup_off = True
            log.warning("의존성 버전 조회 끔: 저장소 조회를 시작할 수 없음 (%s: %s) — 이 실행은 표만 사용",
                        type(e).__name__, str(e)[:200])
        return self._repo_lookup

    def _library_lines(self, key: str, current: str | None, javax: bool = False) -> list[str]:
        """라이브러리 하나에 대한 프롬프트 줄을 만든다. 표 항목, 저장소 정보, jakarta 빌드 탐색 줄이다.
        ``javax``면 javax 케이스이므로 비슷한 이름의 좌표를 찾은 결과도 넣는다.
        찾은 내용은 ``의존성 버전 조회: …`` INFO 줄로 남긴다."""
        entry = table_entry(key, self.cfg)
        if entry is not None:
            log.info("표 항목: %s → %s (approved%s)", key, getattr(entry, "version", "?"),
                     ", allow_major" if getattr(entry, "allow_major", False) else "")
        rv = None
        lookup = self._repository()
        if lookup is not None:
            self._mark(f"repository lookup {key}")
            try:
                rv = lookup.candidates(key, current)
            except Exception as e:  # noqa: BLE001 - 예외를 던지지 않는 조회기지만 방어한다
                log.debug("repository lookup %s failed: %s: %s", key, type(e).__name__, e)
                rv = None
        if rv is not None:
            self._repo_facts[key] = rv
            log.info("%s", _versions_line(rv, key, current))
            try:
                from sbmig.build.repo_versions import hint_lines
                lines = [str(x) for x in (hint_lines(rv, entry) or []) if x]
            except Exception as e:  # noqa: BLE001
                log.debug("hint_lines for %s failed: %s: %s", key, type(e).__name__, e)
                lines = []
            lines += self._discovery_lines(key, current, entry, rv, javax)
            if lines:
                return lines
        else:
            self._journal_lookup(key, current, None, None)
        if entry is not None:
            try:
                from sbmig.core.jakarta_libraries import library_hint_line
                line = str(library_hint_line(entry, current) or "")
                return [line] if line else []
            except Exception as e:  # noqa: BLE001
                log.debug("library_hint_line for %s failed: %s: %s", key, type(e).__name__, e)
        return []

    # ------------------------------------------------------------------ jakarta 빌드 탐색
    def _discover(self, key: str, current: Optional[str], rv: Any = None) -> Any:
        """라이브러리 ``key``의 jakarta 빌드를 찾아 ``JakartaDiscovery``를 반환한다. 조회기가 없으면 None을 반환한다.
        ``rv``는 이미 받아 둔 RepositoryVersions이고, 없으면 ``_repo_facts``의 것을 쓴다.
        결과는 조회기에 캐시되므로 후보는 라이브러리당 한 번만 조회한다."""
        lookup = self._repository()
        if lookup is None:
            return None
        try:
            from sbmig.build.jakarta_discovery import discover_jakarta_builds
            self._mark(f"jakarta discovery {key}")
            g, _, a = key.partition(":")
            return discover_jakarta_builds(lookup, g, a, current, table_entry=table_entry(key, self.cfg), cfg=self.cfg,
                                           rv=rv if rv is not None else self._repo_facts.get(key))
        except Exception as e:  # noqa: BLE001 - 탐색은 보조 정보다
            log.debug("jakarta discovery %s failed: %s: %s", key, type(e).__name__, e)
            return None

    def _discovery_lines(self, key: str, current: Optional[str], entry: Any, rv: Any, javax: bool) -> list[str]:
        """프롬프트에 붙일 탐색 줄을 만들고 저널 ``library_lookup``을 남긴다. 저장소를 읽지 못했으면 빈 목록이다.
        ``hint_lines``가 이미 보여 준 내용은 반복하지 않는다. 새로 쓰는 것은 다음과 같다.
        - 버전 한정어나 jar 스캔으로 sbmig가 새로 확인한 같은 좌표의 빌드
        - 다른 좌표의 빌드
        - javax 케이스에서 비슷한 이름까지 찾았지만 없었다는 한 줄"""
        if rv is None or (rv.error and not rv.same_major and not rv.higher):
            self._journal_lookup(key, current, rv, None)
            return []
        d = self._discover(key, current, rv)
        self._journal_lookup(key, current, rv, d)
        if d is None:
            return []
        try:
            known = {c.version for c in [*rv.same_major, *rv.higher] if c.jakarta is True}
            out: list[str] = []
            for b in d.builds:
                if b.javax or (b.source == "same" and (not b.verified or b.version in known)):
                    continue
                if b.source != "same" and not b.verified:
                    out.append(f"repository also lists {b.text()} — not verified as a jakarta build, not applied "
                               f"automatically")
                    continue
                out.append(f"repository also has {b.text()} — a jakarta build of {key}: switch the declaration to it "
                           f"instead of working around the javax API")
            searched_siblings = bool(d.probed) or any(x.startswith("Nexus search") and "no Nexus" not in x
                                                       for x in d.searched)
            if javax and not out and not d.verified and not known and searched_siblings:
                out.append(f"no jakarta build of {key} was found in the repository ({d.searched_text()})")
            return out
        except Exception as e:  # noqa: BLE001
            log.debug("discovery lines for %s failed: %s: %s", key, type(e).__name__, e)
            return []

    def _journal_lookup(self, key: str, current: Optional[str], rv: Any, d: Any) -> None:
        """저널 ``library_lookup``을 남긴다. journal.jsonl만 보고도 저장소에서 무엇을 찾았는지 알 수 있게 하기 위해서다."""
        try:
            self.ws.journal({"event": "library_lookup", "key": key, "current": current,
                             "source": getattr(rv, "source", "") if rv is not None else "no lookup",
                             "versions_found": (len(getattr(rv, "same_major", [])) + len(getattr(rv, "higher", [])))
                             if rv is not None else 0,
                             "jakarta_builds": [b.coordinates for b in getattr(d, "verified", [])] if d is not None else [],
                             "unverified": [b.coordinates for b in getattr(d, "builds", []) if not b.verified]
                             if d is not None else [],
                             "searched": d.searched_text() if d is not None else "",
                             "urls_tried": list(getattr(rv, "urls_tried", []) or []) if rv is not None else [],
                             "settings_files": list(getattr(self._repo_lookup, "settings_files", []) or []),
                             "repositories": [f"{u} ({o})" for u, _c, o in getattr(self._repo_lookup, "repository_urls",
                                                                                      lambda: [])()],
                             "error": (getattr(rv, "error", "") if rv is not None else "") or getattr(d, "error", "")})
        except Exception as e:  # noqa: BLE001
            log.debug("library_lookup journal for %s failed: %s: %s", key, type(e).__name__, e)

    def _javax_cluster(self, c: ErrorCluster) -> Any:
        """케이스가 ``external_javax_api``로 분류되면 그 ``BlockedCandidate``를, 아니면 None을 반환한다."""
        if not _mentions_javax(c.errors):
            return None
        try:
            from sbmig.triage.blocked import classify_blocked
            cand = classify_blocked(c, self.profile, self.cfg, ws=self.ws)
        except Exception as e:  # noqa: BLE001
            log.debug("javax classification of %s failed: %s: %s", c.id, type(e).__name__, e)
            return None
        return cand if cand is not None and cand.reason == BlockReason.EXTERNAL_JAVAX_API else None

    def _declared_dependencies(self) -> list[tuple[str, GAV]]:
        """프로젝트 pom이 선언한 (pom, 의존성) 쌍을 반환한다.
        직접 선언과 dependencyManagement 항목을 넣고 프로젝트 자신의 모듈은 뺀다."""
        own = self._own_module_keys()
        out: list[tuple[str, GAV]] = []
        for p in self.ws.pom_files():
            ed = self._pom_editor(p)
            if ed is None:
                continue
            try:
                deps = [*ed.dependencies(), *ed.managed_dependencies()]
            except Exception:  # noqa: BLE001
                continue
            for g in deps:
                if g.group_id and g.artifact_id and g.key not in own:
                    out.append((_norm_path(p), g))
        return out

    def _owner_dependency(self, owner_class: Optional[str], deps: list[tuple[str, GAV]]) -> Optional[GAV]:
        """classpath로 소유 클래스의 라이브러리를 찾지 못할 때 선언된 의존성에서 찾아 반환한다. 없으면 None이다.
        패키지 토큰을 groupId, artifactId 토큰과 비교해 가장 많이 겹치는 것을 고른다.
        ``com.vendor8.grid.GridExporter``면 vendor8, vendor, grid가 토큰이다. 동점이면 groupId가 패키지 접두어인 것이다.
        그래도 없으면 로컬 저장소의 의존성 jar 안에 그 클래스 파일이 있는지 확인한다."""
        if not owner_class or not deps:
            return None
        from sbmig.build.jakarta_discovery import stem
        pkg = owner_class.rsplit(".", 1)[0] if "." in owner_class else ""
        tokens = {t.lower() for t in re.split(r"[.$]", pkg) if t}
        tokens |= {stem(t) for t in list(tokens)}
        tokens = {t for t in tokens if len(t) >= 3 and t not in _GENERIC_TOKENS}
        best: Optional[GAV] = None
        best_score = 0
        for _p, g in deps:
            own = {t.lower() for t in re.split(r"[.\-_]", f"{g.group_id}.{g.artifact_id}") if t}
            own |= {stem(t) for t in list(own)}
            own = {t for t in own if len(t) >= 3 and t not in _GENERIC_TOKENS}
            score = len(tokens & own) * 2 + (1 if pkg and (pkg + ".").startswith(g.group_id + ".") else 0)
            if score > best_score:
                best, best_score = g, score
        if best is not None:
            return best
        # 로컬 저장소에서 선언된 의존성 jar를 정해진 개수까지 열어 클래스 파일을 찾는다
        try:
            from sbmig.build.mvn import local_repo_path
            root = local_repo_path(self.cfg)
        except Exception:  # noqa: BLE001
            return None
        entry = owner_class.replace(".", "/").split("$", 1)[0] + ".class"
        for _p, g in deps[:40]:
            d = root.joinpath(*g.group_id.split("."), g.artifact_id)
            if not d.is_dir():
                continue
            versions = [g.version] if g.version and "${" not in g.version else []
            if not versions:
                try:
                    versions = sorted(x.name for x in d.iterdir() if x.is_dir())[-2:]
                except OSError:
                    versions = []
            for v in versions:
                jar = d / v / f"{g.artifact_id}-{v}.jar"
                if not jar.is_file() or jar.stat().st_size > 50 * 1024 * 1024:
                    continue
                try:
                    import zipfile
                    with zipfile.ZipFile(jar) as zf:
                        if entry in zf.namelist():
                            return g.model_copy(update={"version": v}) if not g.version or "${" in g.version else g
                except Exception:  # noqa: BLE001 - 깨진 jar는 건너뛴다
                    continue
        return None

    def _javax_libraries(self, c: ErrorCluster) -> list[GAV]:
        """javax 케이스와 관련된 라이브러리를 classpath 없이 ``_owner_dependency``로 찾는다. 결과는 클러스터별로 캐시한다.
        찾지 못하면 저널 ``library_unmapped``를 남긴다. 프롬프트에 저장소 줄이 없는 이유를 저널로 알 수 있게 하기 위해서다."""
        if c.id in self._javax_owner_libs:
            return self._javax_owner_libs[c.id]
        out: list[GAV] = []
        cand = self._javax_cluster(c)
        if cand is not None:
            owner = cand.owner_class
            try:
                owner = self._classpath_hints(list(c.errors), owner, cand.required_type, cand.found_type)[0] or owner
            except Exception:  # noqa: BLE001
                pass
            if not owner:
                owner = self._owner_from_source(c)      # `incompatible types`는 소유 클래스를 알려 주지 않는다
            self._javax_owner[c.id] = owner
            deps = self._declared_dependencies()
            g = self._owner_dependency(owner, deps)
            # 첫 소유 클래스는 어느 라이브러리에나 있는 타입일 수 있으므로 맞지 않으면 다음 소유 클래스로 다시 찾는다
            for other in [o for o in (getattr(cand, "owner_classes", None) or []) if o and o != owner]:
                if g is not None:
                    break
                g = self._owner_dependency(other, deps)
                if g is not None:
                    owner = other
                    self._javax_owner[c.id] = owner
            if g is not None:
                out.append(g)
                self.ws.journal({"event": "library_mapped", "case": c.case_id or c.id, "owner_class": owner,
                                 "key": g.key, "version": g.version, "how": "package tokens / local jar"})
                log.info("javax 라이브러리 귀속(폴백): %s → %s", owner, g.coordinates)
            else:
                self.ws.journal({"event": "library_unmapped", "case": c.case_id or c.id, "owner_class": owner,
                                 "declared": len(deps)})
                log.info("javax 라이브러리 귀속 실패: %s 를 선언된 의존성 %d개와 맞추지 못함 (classpath · 패키지 토큰 · 로컬 jar)",
                         owner or "?", len(deps))
        self._javax_owner_libs[c.id] = out
        return out

    def _owner_from_source(self, c: ErrorCluster) -> Optional[str]:
        """``incompatible types``처럼 소유 클래스를 알려 주지 않는 오류에서 오류 줄의 외부 클래스 FQCN을 반환한다.
        오류 줄의 식별자를 그 파일의 import로 찾는다. JDK, javax, jakarta, 프로젝트 클래스는 뺀다.
        찾지 못하면 None을 반환한다. 파일은 읽기만 한다."""
        try:
            from sbmig.triage.blocked import is_project_class
        except Exception:  # noqa: BLE001
            is_project_class = None  # type: ignore[assignment]
        for e in c.errors:
            if not e.file or not e.line:
                continue
            try:
                lines = self.ws.read(_norm_path(e.file)).splitlines()
            except Exception:  # noqa: BLE001
                continue
            if e.line > len(lines):
                continue
            imports = re.findall(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", "\n".join(lines[:400]), re.M)
            by_simple = {imp.rsplit(".", 1)[-1]: imp for imp in imports}
            window = " ".join(lines[max(0, e.line - 2): e.line + 1])

            def external(fq: str) -> bool:
                if not fq or fq.startswith(("java.", "javax.", "jakarta.")):
                    return False
                try:
                    return not (is_project_class is not None and is_project_class(self.ws, self.profile, fq))
                except Exception:  # noqa: BLE001
                    return True

            # 오류 줄의 식별자를 먼저 본다. 필드나 지역 변수로 호출하면 이름이 안 보이므로 파일의 첫 외부 import를 쓴다
            for ident in re.findall(r"\b([A-Z]\w*)\b", window):
                fq = by_simple.get(ident)
                if fq and external(fq):
                    return fq
            for fq in imports:
                if external(fq):
                    return fq
        return None

    def _jakarta_evidence(self, c: ErrorCluster, owner: Optional[str], artifact: Optional[GAV]) -> str:
        """차단 항목에 붙일 근거 문장을 만든다.
        jakarta 빌드 탐색 기록(``JAKARTA_SEARCH_PREFIX``)이나 의존성을 찾지 못한 클래스(``UNMAPPED_PREFIX``)를 쓴다."""
        try:
            libs = [artifact] if artifact is not None else []
            if not libs:
                libs = self._case_libraries(c) or self._javax_libraries(c)
            if not libs:
                who = owner or self._javax_owner.get(c.id) or self._owner_from_source(c)
                if not who:
                    return ""                      # 소유 클래스도 모르면 덧붙일 정보가 없다
                return (f"{UNMAPPED_PREFIX} {who} could not be matched to a dependency (classpath, package tokens, local "
                        f"jars) — run mvn dependency:tree in the module and look the library's jakarta build up in Nexus")
            lib = libs[0]
            d = self._discover(lib.key, lib.version)
            if d is None:
                return f"{JAKARTA_SEARCH_PREFIX} {lib.key}: no repository lookup (libraries.lookup off or unreachable)"
            found = ", ".join(b.coordinates for b in d.verified)
            return (f"{JAKARTA_SEARCH_PREFIX} {lib.key}: {d.searched_text()}"
                    + (f" — verified build(s): {found}" if found else " — none verified"))
        except Exception as e:  # noqa: BLE001
            log.debug("jakarta evidence for %s failed: %s: %s", c.id, type(e).__name__, e)
            return ""

    def _decline_contradiction(self, c: ErrorCluster, reason: str) -> str:
        """LLM의 거절과 맞지 않는 sbmig의 정보를 반환한다. 없으면 빈 문자열을 반환한다.
        대체 방법 노트, 번들 jar, 확인된 jakarta 빌드를 이 순서로 확인한다."""
        noted = self._note_contradiction(c)                  # 노트가 프로젝트 코드로 고치는 방법을 안다
        if noted:
            return noted
        bundled = self._bundled_contradiction(c, reason)     # 없다는 라이브러리가 프로젝트에 jar로 들어 있다
        if bundled:
            return bundled
        if not self._jakarta_enabled() or not re.search(r"(?i)jakarta|javax", reason or ""):
            return ""
        try:
            libs = self._case_libraries(c) or self._javax_libraries(c)
            for lib in libs:
                if self._is_framework_key(lib.key):    # 프레임워크 아티팩트는 jakarta 빌드가 있어도 프로젝트에서 바꾸지 않는다
                    continue
                d = self._discover(lib.key, lib.version)
                b = d.best() if d is not None else None
                if b is not None:
                    return f"the repository has {b.coordinates} ({b.how}; found by {b.source}) — a jakarta build of {lib.key}"
        except Exception as e:  # noqa: BLE001
            log.debug("decline contradiction for %s failed: %s: %s", c.id, type(e).__name__, e)
        return ""

    def _knowledge_notes(self) -> list[Any]:
        """``knowledge/notes``의 노트 목록을 반환한다. 한 번만 읽고, 읽지 못하면 빈 목록을 반환한다.
        triage에도 같은 목록을 넘긴다(``attach_run(notes=…)``). 테스트는 ``_notes_index``에 직접 넣는다."""
        if self._notes_index is None:
            try:
                from sbmig.fix.context import load_notes_index
                self._notes_index = load_notes_index(self.cfg.resolve_path("knowledge/notes"))
            except Exception as e:  # noqa: BLE001 - 노트는 힌트라 폴더가 없어도 루프를 멈추지 않는다
                log.debug("notes index unavailable: %s: %s", type(e).__name__, e)
                self._notes_index = []
        return self._notes_index

    def _note_contradiction(self, c: ErrorCluster) -> str:
        """케이스의 오류 텍스트나 import 줄에 ``replacement: true`` 노트가 맞으면 그 노트를 알리는 문장을 반환한다.
        맞는 노트가 없으면 빈 문자열을 반환한다. 대체 방법을 아는 라이브러리의 거절을 받지 않기 위해 쓴다."""
        try:
            from sbmig.fix.context import replacement_notes
            from sbmig.triage.blocked import note_text
            case = getattr(getattr(self.profile, "case_type", None), "value", None)
            hits = replacement_notes(self._knowledge_notes(), note_text(self.ws, c.errors), case=case, phase=None)
        except Exception as e:  # noqa: BLE001 - 노트 문제로 루프가 멈추면 안 된다
            log.debug("note contradiction for %s failed: %s: %s", c.id, type(e).__name__, e)
            return ""
        if not hits:
            return ""
        n = hits[0]
        return (f"the migration note '{n.title}' ({n.file}) gives the replacement as project code — the fix is inside the "
                f"project, not a blocker")

    def _bundled_contradiction(self, c: ErrorCluster, reason: str) -> str:
        """좌표나 라이브러리가 없다는 거절인데 없는 패키지를 프로젝트 안의 jar가 제공하면 그 정보를 반환한다.
        아니면 빈 문자열을 반환한다."""
        if not re.search(r"(?i)coordinates|artifact|library|jar|좌표|라이브러리|아티팩트", reason or ""):
            return ""
        try:
            provs = self._bundled_providers(c)
        except Exception as e:  # noqa: BLE001
            log.debug("bundled contradiction for %s: %s: %s", c.id, type(e).__name__, e)
            return ""
        if not provs:
            return ""
        pkg, jar, _files = provs[0]
        return f"`{jar.path}` in the project provides package `{pkg}` — the library is not missing, it is bundled; wire it"

    def _jakarta_plan(self, c: ErrorCluster) -> dict[str, Any]:
        """사전 시도 ``sbmig-jakarta``의 계획을 세운다. 클러스터별로 라운드마다 한 번 만든다.
        javax 라이브러리마다 탐색 -> 확인된 가장 좋은 빌드 선택 -> 선언한 pom 찾기 순서로 항목을 만든다.
        새 좌표를 새 부모 체인이나 Boot BOM이 관리하면 버전 없이 쓴다.
        사내 부모, 필수, 선택 아티팩트와 프레임워크 아티팩트는 바꾸지 않는다.

        Item = {pom, old g:a, build, version (None when managed), managed}."""
        if c.id in self._jakarta_plans:
            return self._jakarta_plans[c.id]
        plan: dict[str, Any] = {"items": [], "notes": [], "keys": [], "libs": []}
        self._jakarta_plans[c.id] = plan
        if self.dry_run or self._javax_cluster(c) is None:
            return plan
        libs = self._case_libraries(c) or self._javax_libraries(c)
        plan["libs"] = [g.key for g in libs]
        if not libs:
            return plan
        protected = self._protected_version_keys()
        deps = self._declared_dependencies()
        for lib in libs:
            d = self._discover(lib.key, lib.version)
            if d is None:
                plan["notes"].append(f"{lib.key}: no repository lookup")
                continue
            plan["notes"].append(f"{JAKARTA_SEARCH_PREFIX} {lib.key}: {d.searched_text()}")
            b = d.best()
            if b is None:
                continue
            if self._is_framework_key(lib.key):         # 프레임워크 아티팩트는 다른 라인으로 바꾸지 않는다
                plan["notes"].append(f"{lib.key} is the framework artifact of this project: never swapped by sbmig "
                                     f"({b.coordinates} found) — its owner must provide a jakarta build")
                continue
            if lib.key in protected:
                plan["notes"].append(f"{lib.key} is a configured in-house artifact: not swapped by sbmig ({b.coordinates} found)")
                continue
            poms = list(dict.fromkeys(p for p, g in deps if g.key == lib.key))
            if not poms:
                plan["notes"].append(f"{lib.key}: no project pom declares it (transitive?) — not swapped")
                continue
            for pom in poms:
                module = self._coords(self._pom_editor(pom))
                try:
                    managed = self._versions().managed_version(b.key, module) or self._versions().boot_version(b.key, module)
                except Exception:  # noqa: BLE001
                    managed = None
                version = None if managed else b.version
                key = (SBMIG_JAKARTA_SOURCE, pom, f"{lib.key}->{b.coordinates}")
                if key in self._pre_tried:
                    continue
                plan["items"].append({"pom": pom, "old": lib.key, "build": b, "version": version,
                                      "managed": managed or ""})
                plan["keys"].append(key)
        return plan

    def _jakarta_enabled(self) -> bool:
        """``libraries.discover_jakarta_builds``가 켜져 있으면 True를 반환한다.
        꺼져 있으면 사전 시도, 거절 반박, 탐색 없이 릴리스 목록만 보여 준다."""
        return bool(getattr(getattr(self.cfg, "libraries", None), "discover_jakarta_builds", True))

    def _jakarta_targets(self, c: ErrorCluster) -> list[str] | None:
        """javax 라이브러리 케이스이고 적용할 jakarta 빌드가 있으면 수정할 pom 목록을, 아니면 None을 반환한다."""
        if not self._jakarta_enabled() or not _mentions_javax(c.errors):
            return None
        plan = self._jakarta_plan(c)
        return list(dict.fromkeys(i["pom"] for i in plan["items"])) or None

    def _jakarta_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """확인된 jakarta 빌드로 의존성 선언을 바꾸는 사전 시도를 LLM 호출 없이 실행한다. 적용하면 True를 반환한다.
        좌표가 다르면 ``replace_dependency``를 쓰고, 같으면 버전과 classifier를 바꾼다. 관리되는 버전이면 버전을 지운다.
        javax에서 jakarta로 가는 상위 메이저 변경은 유일하게 허용된 메이저 변경이라 적용하고 운영자 확인 줄을 남긴다.
        커밋은 ``fix(sbmig-jakarta): <cluster id>``이고 (pom, 이전 좌표, 새 좌표)마다 실행당 한 번만 시도한다.

        Scored and rolled back like any try; the read-back lists the major change with sbmig's synthesized
        guide."""
        plan = self._jakarta_plan(c)
        items = list(plan["items"])
        case_id = c.case_id or c.id
        self.ws.journal({"event": "jakarta_try", "phase": st.phase.value, "iteration": st.iteration, "case": case_id,
                         "libraries": plan["libs"],
                         "plan": [{"pom": i["pom"], "from": i["old"], "to": i["build"].coordinates, "version": i["version"],
                                   "managed_by": i["managed"], "source": i["build"].source, "how": i["build"].how,
                                   "major": i["build"].major_change} for i in items],
                         "notes": plan["notes"]})
        if not items:
            return False
        from sbmig.restructure.pom_editor import PomEditor
        what = "; ".join(f"{i['old']} → {i['build'].coordinates} in {i['pom']}" + (" (version managed)" if i["managed"] else "")
                         for i in items)
        log.info("sbmig jakarta 빌드 교체 (LLM 0회): %s", what)
        notes = [f"jakarta build: {i['old']} → {i['build'].coordinates} in {i['pom']} ({i['build'].how}; found by "
                 f"{i['build'].source}" + (f"; version managed by {i['managed']}" if i["managed"] else "")
                 + ("; a higher major — operator check" if i["build"].major_change else "") + ")" for i in items]
        notes += plan["notes"]

        def apply() -> ScriptRunResult:
            changed: list[str] = []
            for i in items:
                b = i["build"]
                og, _, oa = i["old"].partition(":")
                ed = PomEditor(self.ws.abs(i["pom"]))
                if (og, oa) != (b.group, b.artifact):
                    ed.replace_dependency(og, oa, GAV(group_id=b.group, artifact_id=b.artifact, version=i["version"],
                                                      classifier=b.classifier))
                    if i["version"] is None:
                        ed.set_dependency_version(b.group, b.artifact, None)
                else:
                    if b.classifier:
                        ed.replace_dependency(og, oa, GAV(group_id=b.group, artifact_id=b.artifact,
                                                          version=i["version"], classifier=b.classifier))
                    ed.set_dependency_version(b.group, b.artifact, i["version"])
                ed.save()
                if i["pom"] not in changed:
                    changed.append(i["pom"])
            return ScriptRunResult(status="ok", changed_files=changed)

        applied = self._pre_attempt(
            pr, st, c, source=SBMIG_JAKARTA_SOURCE, apply_fn=apply, notes=notes, keys=list(plan["keys"]),
            rationale=f"sbmig-jakarta: the repository has a jakarta build of the javax-era dependency ({what}) — swapped "
                      "without an LLM call",
            analysis=("a dependency still compiled against javax.* is called with jakarta types: "
                      + ", ".join(i["old"] for i in items)),
            solution="sbmig switches the declaration to the jakarta build it verified in the repository — no LLM call")
        self._jakarta_plans.pop(c.id, None)             # 다음 라운드에 다시 계획한다. 적용한 키는 기억해 둔다
        return applied

    # ------------------------------------------------------------------ 프로젝트 안의 jar(번들 jar)
    def _bundled_enabled(self) -> bool:
        """``intake.bundled_jars``가 켜져 있고 분석 단계가 번들 jar를 찾았으면 True를 반환한다."""
        if not bool(getattr(getattr(self.cfg, "intake", None), "bundled_jars", True)):
            return False
        return bool(getattr(self.profile, "bundled_jars", None) or [])

    def _bundled_managed_keys(self) -> set[str]:
        """목표 부모 체인이나 Boot BOM이 관리하는 g:a 집합을 반환한다.
        읽을 수 없으면 빈 집합을 반환한다. 그때는 어떤 jar도 대체된 것으로 보지 않는다."""
        try:
            return set(self._managed_keys())
        except Exception as e:  # noqa: BLE001 - 캡처도 체인 정보도 없으면 관리되는 것이 없다고 본다
            log.debug("managed keys unavailable for the bundled-jar check: %s: %s", type(e).__name__, e)
            return set()

    def _bundled_superseded(self) -> list[JarCoordinate]:
        """목표 부모 체인이나 Boot BOM이 g:a를 관리하는 번들 jar를 반환한다. WEB-INF/lib의 오래된 spring-webmvc 4.x 같은 것이다.
        이런 jar는 관리 버전으로 대체된다. 이전 버전을 연결하면 두 Spring 버전이 섞이므로 절대 연결하지 않는다.
        LLM에는 이전 jar를 다시 넣지 말고 코드를 고치라는 정보만 준다."""
        managed = self._bundled_managed_keys()
        if not managed:
            return []
        return [j for j in getattr(self.profile, "bundled_jars", None) or []
                if j.group_id and j.artifact_id and f"{j.group_id}:{j.artifact_id}" in managed]

    def _bundled_wireable(self) -> list[JarCoordinate]:
        """연결할 수 있는 번들 jar를 반환한다.
        servlet, jsp, el, Tomcat 같은 컨테이너 API jar(``scope == "provided"``)와 ``_bundled_superseded``의 jar는
        뺀다."""
        skip = {j.path for j in self._bundled_superseded()}
        return [j for j in getattr(self.profile, "bundled_jars", None) or []
                if (j.scope or "compile") != "provided" and j.path not in skip]

    def _bundled_missing_packages(self, c: ErrorCluster) -> dict[str, list[str]]:
        """케이스에 없는 패키지와 그 오류 파일 목록을 반환한다. 예외를 던지지 않는다.
        ``package <p> does not exist``의 ``<p>``를 쓴다. ``cannot find symbol … class X``면 오류 파일의 ``import
        <p>.X;``에서 찾는다. ``javax.*``와 ``jakarta.*``는 jakarta 사전 시도가 다루므로 뺀다.

        The error file is read up to `BUNDLED_READ_CHARS`."""
        out: dict[str, list[str]] = {}
        texts: dict[str, str] = {}

        def add(pkg: str, f: str) -> None:
            if pkg.startswith(("javax.", "jakarta.")) or pkg in ("javax", "jakarta"):
                return
            files = out.setdefault(pkg, [])
            if f and f not in files:
                files.append(f)

        try:
            for e in c.errors:
                f = _norm_path(e.file) if e.file else ""
                blob = f"{e.message or ''}\n{e.raw or ''}"
                m = _PACKAGE_MISSING_RE.search(blob)
                if m:
                    add(m.group(1), f)
                    continue
                s = _SYMBOL_CLASS_RE.search(blob)
                if s is None or not f:
                    continue
                if f not in texts:
                    try:
                        texts[f] = self.ws.read(f)[:BUNDLED_READ_CHARS]
                    except Exception:  # noqa: BLE001 - 읽을 수 없는 파일은 import도 없다고 본다
                        texts[f] = ""
                im = re.search(r"(?m)^\s*import\s+([A-Za-z_][\w.]*)\." + re.escape(s.group(1)) + r"\s*;", texts[f])
                if im:
                    add(im.group(1), f)
        except Exception as e:  # noqa: BLE001 - 정보 때문에 루프가 멈추면 안 된다
            log.debug("bundled packages for %s: %s: %s", c.id, type(e).__name__, e)
        return out

    def _bundled_providers(self, c: ErrorCluster) -> list[tuple[str, JarCoordinate, list[str]]]:
        """(없는 패키지, 그 패키지를 제공하는 번들 jar, 오류 파일) 목록을 반환한다. 패키지마다 ``providers``의 첫 jar를 쓴다."""
        if not self._bundled_enabled():
            return []
        out: list[tuple[str, JarCoordinate, list[str]]] = []
        jars = self._bundled_wireable()
        for pkg, files in self._bundled_missing_packages(c).items():
            try:
                hits = list(providers(jars, pkg) or [])
            except Exception as e:  # noqa: BLE001
                log.debug("providers(%s): %s: %s", pkg, type(e).__name__, e)
                hits = []
            if hits:
                out.append((pkg, hits[0], files))
        return out

    def _bundled_owning_pom(self, f: str, poms: list[str]) -> str:
        """현재 트리에서 오류 파일을 포함하는 가장 깊은 pom을 반환한다. 없으면 루트 pom을 반환한다."""
        from sbmig.analyze.pom_parser import owning_pom
        from sbmig.core.models import PomInfo
        infos = [PomInfo(path=p, artifact_id="") for p in poms]
        hit = owning_pom(f, infos) if f else None
        return _norm_path(hit.path) if hit is not None else "pom.xml"

    def _bundled_plan(self, c: ErrorCluster) -> list[tuple[str, JarCoordinate, str]]:
        """아직 시도하지 않은 (모듈 pom, jar, 패키지) 목록을 반환한다.
        오류 파일의 모듈은 재구성 뒤 현재 트리의 pom으로 정한다. 이미 선언된 jar는 뺀다."""
        provs = self._bundled_providers(c)
        if not provs:
            return []
        poms = [_norm_path(p) for p in self.ws.pom_files()]
        plan: list[tuple[str, JarCoordinate, str]] = []
        for pkg, jar, files in provs:
            targets = list(dict.fromkeys(self._bundled_owning_pom(f, poms) for f in files)) or ["pom.xml"]
            for pom in targets:
                if (SBMIG_BUNDLED_SOURCE, pom, jar.path) in self._pre_tried:
                    continue
                if self._bundled_declared(pom, jar):             # 재구성이나 사람이 이미 연결했다
                    continue
                if not any(p == pom and j.path == jar.path for p, j, _ in plan):
                    plan.append((pom, jar, pkg))
        return plan

    def _with_eol_advisory(self, suggestion: str, artifact: Any) -> str:
        """차단된 아티팩트가 권고표의 EOL 항목이면 조치 방법 뒤에 EOL 제목과 대체 라이브러리를 붙여 반환한다.
        분석 결과를 먼저 보고, 없으면 ``knowledge/libraries/advisories.yml``의 ``artifacts`` glob으로 찾는다.
        소유팀이 jakarta 빌드가 없는 이유와 다음에 할 일을 한 줄에서 읽게 하기 위해서다.
        권고가 없으면 문구를 그대로 반환한다. 예외를 던지지 않는다."""
        try:
            key = ""
            if artifact is not None:
                g = getattr(artifact, "group_id", "") or ""
                a = getattr(artifact, "artifact_id", "") or ""
                key = f"{g}:{a}" if g and a else str(getattr(artifact, "coordinates", "") or "")
                key = ":".join(key.split(":")[:2])
            if not key or "EOL:" in (suggestion or ""):
                return suggestion
            title = repl = ""
            for f in self.advisories:
                if key in (":".join(x.split(":")[:2]) for x in (f.artifacts or [])):
                    title, repl = f.title, f.replacement or ""
                    break
            if not title:
                import fnmatch
                from sbmig.advisory import load_advisories
                if not hasattr(self, "_advisory_catalog"):
                    try:
                        self._advisory_catalog = load_advisories(self.cfg)
                    except Exception:  # noqa: BLE001 - 표가 없으면 권고 문장도 없다
                        self._advisory_catalog = None
                cat = self._advisory_catalog
                entries = (cat.entries() if callable(getattr(cat, "entries", None)) else list(cat)) if cat is not None else []
                for adv in entries:
                    if any(fnmatch.fnmatchcase(key, pat) for pat in (adv.artifacts or [])):
                        title, repl = adv.title, adv.replacement or ""
                        break
            if not title:
                return suggestion
            tail = f"EOL: {title}" + (f" — replacement: {repl}" if repl else "")
            return f"{suggestion.rstrip()}; {tail}" if suggestion else tail
        except Exception as e:  # noqa: BLE001 - 덧붙이는 문장 때문에 차단이 실패하면 안 된다
            log.debug("eol advisory lookup for %s: %s: %s", artifact, type(e).__name__, e)
            return suggestion

    def _bundled_declared(self, pom: str, jar: JarCoordinate) -> bool:
        """그 pom이 jar의 g:a를 이미 선언했으면 True를 반환한다. 재구성이 연결했거나 사람이 넣은 경우다.
        좌표를 모르면 False를 반환한다."""
        if not (jar.group_id and jar.artifact_id):
            return False
        ed = self._pom_editor(pom)
        try:
            return bool(ed is not None and ed.has_dependency(jar.group_id, jar.artifact_id))
        except Exception:  # noqa: BLE001 - 읽을 수 없는 pom은 선언이 없다고 본다
            return False

    def _bundled_targets(self, c: ErrorCluster) -> list[str] | None:
        """케이스에 없는 패키지를 번들 jar가 제공하면 그 jar를 연결할 모듈 pom 목록을 반환한다.
        모듈 pom은 오류 파일의 ``owning_pom``이고, 없으면 루트 pom이다.
        설정이 꺼졌거나, 번들 jar나 제공하는 jar가 없거나, 모두 시도했으면 None을 반환한다."""
        if self.dry_run or not self._bundled_enabled():
            return None
        plan = self._bundled_plan(c)
        return list(dict.fromkeys(p for p, _j, _k in plan)) or None

    def _bundled_in_repository(self, jar: JarCoordinate) -> bool:
        """jar 자신의 좌표가 저장소에 있으면 True를 반환한다. ``absent``와 ``unreadable``은 False다."""
        return self._bundled_repo_state(jar)[0] == "present"

    def _bundled_repo_state(self, jar: JarCoordinate) -> tuple[str, str]:
        """jar 자신의 좌표를 저장소에서 찾아 ``present`` / ``absent`` / ``unreadable``과 사유를 반환한다.
        좌표는 pom.properties, sha1 조회, 이름표에서 얻은 버전 있는 좌표만 쓴다. 좌표나 조회기가 없으면 absent다.

        `repository_state_fn`; `unreadable` = no remote answered."""
        g, a, v = jar.group_id, jar.artifact_id, jar.version
        if jar.source not in BUNDLED_TRUSTED_SOURCES or not (g and a and v):
            return "absent", ""
        lookup = self._repository()
        if lookup is None:
            return "absent", ""
        self._mark(f"repository lookup {g}:{a}:{v}")
        try:
            state, why = repository_state_fn(lookup)(g, a, v)
        except Exception as e:  # noqa: BLE001 - 조회에 실패하면 응답이 없는 것으로 본다
            state, why = "unreadable", f"{type(e).__name__}: {e}"[:300]
        return state, why

    def _bundled_unreachable(self, c: ErrorCluster) -> Any:
        """번들 jar 사전 시도가 연결할 jar에 대해 저장소가 응답하지 않았으면 ``repository_unreachable`` 차단 후보를 반환한다.
        jar가 없는 것이 아니라 저장소 장애이기 때문이다. 아니면 None을 반환한다.

        Only when `loop.blocked_policy.repository_unreachable` is 0 (the shipped value); the candidate covers
        the whole case and names the jar, its coordinates and the reason."""
        from sbmig.triage.blocked import BlockedCandidate
        if int(self.cfg.loop.blocked_policy.get(BlockReason.REPOSITORY_UNREACHABLE.value, 0)) > 0:
            return None
        for pom, jar, pkg in self._bundled_plan(c):
            state, why = self._bundled_repo_state(jar)
            if state != "unreadable":
                continue
            name = PurePosixPath(jar.path).name
            coords = f"{jar.group_id}:{jar.artifact_id}:{jar.version}"
            evidence = (f"bundled jar `{name}` ({coords}) could not be looked up: the repository did not answer "
                        f"({why or 'no reason given'})")
            suggestion = (f"bundled jar `{name}` provides package `{pkg}` needed by `{pom}`, but the repository did not "
                          f"answer whether it has `{coords}` ({why or 'no reason given'}) — not wired and not called "
                          "missing; not a project problem: check the repository and the network, then rerun")
            return BlockedCandidate(reason=BlockReason.REPOSITORY_UNREACHABLE, errors=list(c.errors),
                                    evidence=[evidence], suggestion=suggestion)
        return None

    def _bundled_boot_poms(self, module_pom: str = "") -> list[str]:
        """현재 트리에서 spring-boot-maven-plugin을 가진 pom과 분석 단계의 boot 모듈 pom을 반환한다.
        ``module_pom``이 있으면 그 모듈 자체이거나 그 모듈에 의존하는 boot pom만 남긴다.
        MSA처럼 boot 모듈이 여럿일 때 관계없는 서비스에 system 의존성을 넣지 않기 위해서다. 맞는 것이 없으면 전부 반환한다."""
        from sbmig.restructure.restructurer import BOOT_PLUGIN
        out: list[str] = []
        for p in self.ws.pom_files():
            ed = self._pom_editor(p)
            try:
                if ed is not None and ed.has_plugin(*BOOT_PLUGIN, where="build"):
                    out.append(_norm_path(p))
            except Exception as e:  # noqa: BLE001 - 읽을 수 없는 pom은 boot pom이 아니다
                log.debug("boot plugin check %s: %s: %s", p, type(e).__name__, e)
        for m in getattr(self.profile, "boot_modules", None) or []:
            rel = _norm_path(f"{m.strip('/')}/pom.xml") if m and m not in (".", "") else "pom.xml"
            if rel not in out and self.ws.exists(rel):
                out.append(rel)
        if not module_pom or len(out) <= 1:
            return out
        mod = self._pom_editor(module_pom)
        try:
            g, a = (mod.effective_group_id() or "", mod.artifact_id() or "") if mod is not None else ("", "")
        except Exception:  # noqa: BLE001 - 모듈 pom을 읽을 수 없으면 boot pom을 모두 남긴다
            return out
        if not a:
            return out
        mp = _norm_path(module_pom)
        kept: list[str] = []
        for b in out:
            if b == mp:
                kept.append(b)
                continue
            ed = self._pom_editor(b)
            try:
                if ed is not None and ed.has_dependency(g, a):
                    kept.append(b)
            except Exception as e:  # noqa: BLE001 - 읽을 수 없는 boot pom은 아래 기본값으로 남긴다
                log.debug("boot pom dependency check %s: %s: %s", b, type(e).__name__, e)
        return kept or out

    def _bundled_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """사전 시도 ``sbmig-bundled``를 실행한다. 적용하면 True를 반환한다.
        케이스에 없는 패키지를 제공하는 번들 jar를 오류 파일의 모듈 pom에 ``wire_bundled_jar``로 연결한다.
        저장소에 jar 좌표가 있으면 일반 의존성으로, 없으면 ``<module>/lib/<name>``에 복사해 system scope로 연결한다.
        좌표는 추측하지 않고 인수인계 항목을 남긴다. ``intake.unknown_jar: skip``이면 저장소에 없는 jar는 연결하지 않는다.

        Commit `fix(sbmig-bundled): <cluster id>`, scored and rolled back like any try; journal
        `bundled_jar_wired` + one Handover line per wiring. Once per (pom, jar) per run."""
        plan = self._bundled_plan(c)
        if not plan:
            return False
        policy = str(getattr(getattr(self.cfg, "intake", None), "unknown_jar", "system") or "system")
        items: list[tuple[str, JarCoordinate, str, bool]] = []
        for pom, jar, pkg in plan:
            state, _why = self._bundled_repo_state(jar)
            if state == "unreadable":
                # 저장소가 응답하지 않은 jar는 없다고 보지 않으므로 연결하지도 않고 Nexus 등록 안내도 쓰지 않는다
                self._pre_tried.add((SBMIG_BUNDLED_SOURCE, pom, jar.path))
                continue
            has = state == "present"
            if not has and policy == "skip":
                self._pre_tried.add((SBMIG_BUNDLED_SOURCE, pom, jar.path))
                self._followup(f"bundled jar `{PurePosixPath(jar.path).name}` provides package `{pkg}` needed by "
                               f"`{pom}` but is not in the repository and intake.unknown_jar is skip — declare it "
                               "as a Maven dependency (publish it to Nexus first)")
                continue
            items.append((pom, jar, pkg, has))
        if not items:
            return False
        case_id = c.case_id or c.id
        group = ""
        root = self._pom_editor("pom.xml") if self.ws.exists("pom.xml") else None
        try:
            group = (root.effective_group_id() if root is not None else "") or ""
        except Exception:  # noqa: BLE001
            group = ""
        wired: list[WiredJar] = []
        what = "; ".join(f"{PurePosixPath(j.path).name} → {p} ({'compile' if h else 'system'})" for p, j, _k, h in items)
        notes = [f"bundled jar {j.path} provides package {k} → wired into {p} as "
                 + (f"a compile dependency {j.group_id}:{j.artifact_id}:{j.version} (the repository has it)" if h
                    else "a system dependency (copied under the module's lib/)")
                 for p, j, k, h in items]
        log.info("sbmig 동봉 jar 연결 (LLM 0회): %s", what)

        def apply() -> ScriptRunResult:
            changed: list[str] = []
            for pom, jar, _pkg, has in items:
                w = wire_bundled_jar(self.ws, self.cfg, module_pom=pom, jar=jar, repository_has=has,
                                     boot_poms=self._bundled_boot_poms(pom), project_group=group)
                wired.append(w)
                for f in (pom, w.copied_to, *(w.boot_poms or [])):
                    if f and f not in changed:
                        changed.append(f)
            return ScriptRunResult(status="ok", changed_files=changed)

        applied = self._pre_attempt(
            pr, st, c, source=SBMIG_BUNDLED_SOURCE, apply_fn=apply, notes=notes,
            keys=[(SBMIG_BUNDLED_SOURCE, p, j.path) for p, j, _k, _h in items],
            rationale=f"sbmig-bundled: a jar in the project provides the missing package(s) ({what}) — wired without "
                      "an LLM call",
            analysis=("the build lacks package(s) that a jar bundled in the project (declared by no pom) provides: "
                      + ", ".join(sorted({k for _p, _j, k, _h in items}))),
            solution="sbmig declares the bundled jar as a dependency of the module (system scope when the repository "
                     "lacks it) — no LLM call")
        if applied:
            for w in wired:
                coords = f"{w.group_id}:{w.artifact_id}:{w.version}"
                self.ws.journal({"event": "bundled_jar_wired", "stage": "loop", "case": case_id, "jar": w.jar.path, "pom": w.module_pom,
                                 "coordinates": coords, "scope": w.scope, "placeholder": w.placeholder})
                try:
                    self._followup(bundled_jar_followup(w))
                except Exception as e:  # noqa: BLE001 - 인수인계 줄은 보조 정보다
                    log.debug("bundled_jar_followup: %s: %s", type(e).__name__, e)
        return applied

    def _bundled_facts(self, c: ErrorCluster) -> list[str]:
        """번들 jar가 제공하는 패키지를 프롬프트에 알리는 줄을 만든다. 예외를 던지지 않는다.
        (jar, 패키지)마다 "the project bundles `<jar>` which provides package `<p>` …" 한 줄을 쓴다.
        대체된 jar에는 이전 jar를 다시 넣지 말라는 줄을 쓴다. 모두 ``PACKAGE_FACTS_ARTIFACTS``개까지다."""
        out: list[str] = []
        try:
            if not self._bundled_enabled():
                return []
            jars = self._bundled_wireable()
            for pkg in self._bundled_missing_packages(c):
                try:
                    hits = list(providers(jars, pkg) or [])
                except Exception:  # noqa: BLE001
                    hits = []
                for jar in hits:
                    if len(out) >= PACKAGE_FACTS_ARTIFACTS:
                        return out
                    if jar.source == "pom_properties" and jar.group_id and jar.artifact_id and jar.version:
                        coords = f"{jar.group_id}:{jar.artifact_id}:{jar.version}"
                    elif jar.group_id and jar.artifact_id and jar.version:
                        coords = f"{jar.group_id}:{jar.artifact_id}:{jar.version} ({jar.source})"
                    else:
                        coords = "unknown — no pom.properties, not in the jar name table"
                    name = PurePosixPath(jar.path).name
                    line = (f"the project bundles `{jar.path}` which provides package `{pkg}` (coordinates: {coords}) — "
                            f"wire it as a dependency (system scope at `${{project.basedir}}/lib/{name}` when the "
                            "repository lacks it) instead of calling the library missing")
                    if line not in out:
                        out.append(line)
            # 대체된 jar는 반대로 알린다. 체인이 그 g:a를 관리하므로 이전 jar를 다시 넣으면 안 된다
            superseded = self._bundled_superseded()
            if superseded:
                for pkg in self._bundled_missing_packages(c):
                    for jar in providers(superseded, pkg) or []:
                        if len(out) >= PACKAGE_FACTS_ARTIFACTS:
                            return out
                        line = (f"the as-is tree bundled `{jar.path}` ({jar.group_id}:{jar.artifact_id}:{jar.version or '?'}) which "
                                f"provided package `{pkg}`, but the target parent chain manages `{jar.group_id}:{jar.artifact_id}` at a "
                                "newer line — that package is gone or relocated there: do NOT re-add the old jar or pin the old "
                                "version (two lines of one library never coexist); migrate the code to the current API of the "
                                "managed artifact, or answer <blocked> when the API has no replacement")
                        if line not in out:
                            out.append(line)
        except Exception as e:  # noqa: BLE001 - 정보 때문에 시도가 멈추면 안 된다
            log.debug("bundled facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
        return out

    # ------------------------------------------------------------------ 의존성 버전
    def _versions(self) -> VersionResolver:
        """이 루프의 버전 결정기를 반환한다. 처음 쓸 때 한 번 만들고 저장소 조회기는 ``_repository()``의 것을 쓴다."""
        if self._version_resolver is None:
            self._version_resolver = VersionResolver.for_workspace(self.ws, repository=self._repository,
                                                                   cfg=self.cfg, reduced=self._reduced_scope())
        return self._version_resolver

    # ------------------------------------------------------------------ 사내 프레임워크 프로젝트
    def _reduced_scope(self) -> bool:
        """사내 프레임워크 프로젝트라 JDK와 jakarta만 올리는 실행이면 True를 반환한다."""
        return bool(getattr(self.profile, "reduced_scope", False))

    def _run_scope(self) -> str:
        """KB에 기록할 이 실행의 이관 범위(jdk-jakarta 또는 full)를 반환한다."""
        return SCOPE_REDUCED if self._reduced_scope() else SCOPE_FULL

    def _chain_label(self) -> str:
        """프롬프트 정보 줄에 쓸 부모 체인 이름을 반환한다. 범위를 줄인 실행은 프로젝트 자신의 부모다."""
        return "parent chain" if self._reduced_scope() else "new parent chain"

    def _framework_keys(self) -> list[str]:
        """이 프로젝트에서 찾은 프레임워크 아티팩트의 g:a 목록을 반환한다. 범위를 줄인 실행이 아니면 빈 목록이다."""
        fw = getattr(self.profile, "framework", None)
        if not self._reduced_scope() or fw is None:
            return []
        try:
            return list(fw.keys())
        except Exception:  # noqa: BLE001 - a foreign profile object
            return []

    def _is_framework_key(self, key: Optional[str]) -> bool:
        """g:a가 프레임워크 아티팩트면 True를 반환한다. 범위를 줄인 실행이 아니면 항상 False다.

        A framework artifact: one of the evidence keys, or any g:a the `framework.artifacts` patterns match (a sibling
        framework module the project did not use yet counts too). Always False outside a reduced run."""
        if not key or not self._reduced_scope():
            return False
        k = ":".join(str(key).split(":")[:2])
        if k in self._framework_keys():
            return True
        fwc = getattr(self.cfg, "framework", None)
        try:
            return bool(fwc is not None and fwc.matches_key(k))
        except Exception:  # noqa: BLE001
            return False

    def _framework_label(self) -> str:
        """사유 문장에 쓸 프레임워크 아티팩트 이름을 반환한다."""
        keys = self._framework_keys()
        if keys:
            return ", ".join(keys[:3])
        fw = getattr(self.profile, "framework", None)
        ev = list(getattr(fw, "evidence", None) or [])
        return ev[0].artifact if ev else "the in-house framework artifact"

    def _framework_versions(self, key: str) -> set[str]:
        """분석 단계가 찾은 프레임워크 아티팩트 g:a의 버전 집합을 반환한다."""
        fw = getattr(self.profile, "framework", None)
        return {e.version for e in list(getattr(fw, "evidence", None) or []) if e.artifact == key and e.version}

    def _scope_facts(self) -> list[str]:
        """프롬프트의 "Scope of this run" 절의 줄을 만든다. 범위를 줄인 실행이 아니면 빈 목록을 반환한다.

        `FixContext.scope_facts` of a reduced run (≤ `SCOPE_FACTS_MAX_CHARS`): what the run does, one line per
        framework evidence (`framework artifact <g:a> <version> (<source>: <where>) — never change its version, never
        remove or replace it`), what a fix may do and what it must not. The evidence lines are cut first when the
        section would grow too long."""
        if not self._reduced_scope():
            return []
        t = getattr(self.cfg, "target", None)
        java = getattr(t, "java_version", None) or "21"
        head = (f"this run: Java {java} and javax.* to jakarta.* only; the parents, the module layout and the Spring / "
                "Spring Boot line are the project's own, sbmig did not restructure anything")
        may = ("a fix may: move project code and JSPs to jakarta.*, add jakarta API dependencies, use the jakarta build of "
               "a library still on javax.* (rule 14)")
        must_not = ("a fix must not: add Spring Boot or the company parents / artifacts, change any <parent>, move modules "
                    "or sources")
        blocked = ("a framework artifact still on javax.* (or a library arriving only through one): answer <blocked> "
                   "naming it, its owner must provide a jakarta build")
        fw = getattr(self.profile, "framework", None)
        ev_lines: list[str] = []
        for e in list(getattr(fw, "evidence", None) or []):
            where = e.where or "?"
            if e.source == "transitive" and e.via:
                where += " via " + " > ".join(e.via[:4])
            line = (f"framework artifact {e.artifact}{(' ' + e.version) if e.version else ''} ({e.source}: {where}) — "
                    "never change its version, never remove or replace it")
            if line not in ev_lines:
                ev_lines.append(line)
        fixed = [head, may, must_not, blocked]
        budget = SCOPE_FACTS_MAX_CHARS - sum(len(x) + 3 for x in fixed)
        shown: list[str] = []
        for ln in ev_lines:
            if budget - (len(ln) + 3) < 0:
                break
            shown.append(ln)
            budget -= len(ln) + 3
        if len(shown) < len(ev_lines):
            more = f"… {len(ev_lines) - len(shown)} more framework evidence line(s) (same rule)"
            if shown and budget < len(more) + 3:
                shown.pop()
                more = f"… {len(ev_lines) - len(shown)} more framework evidence line(s) (same rule)"
            shown.append(more)
        return [head, *shown, may, must_not, blocked]

    def _framework_guard(self, att: FixAttempt, pre: str, reported: list[str]) -> str:
        """범위를 줄인 실행에서 시도가 프레임워크 아티팩트 선언이나 ``<parent>``를 바꿨는지 커밋 전에 확인한다.
        바꿨으면 다음 요청에 보낼 이유 문장을, 아니면 빈 문자열을 반환한다. sbmig 사전 시도는 검사하지 않는다.
        검사가 예외로 끝나면 시도를 받지 않는다. 예외를 던지지 않는다.

        The post-apply check of a reduced run (before the commit): every project pom the attempt touched is compared
        with its text at `pre` — the framework artifacts' declarations (`<dependencies>` and `<dependencyManagement>`:
        g:a, classifier, type, version with the pom's own properties resolved) and the `<parent>` block (g:a:v and
        relativePath). A removed or changed framework declaration, a framework declaration added at a version the
        project never used, and any changed / added / removed `<parent>` make the sentence the caller records with
        `_fail` (the working tree is restored, the next ask sees it among the previous attempts). sbmig's own
        pre-tries are not checked (they never touch these by construction). A pom that does not parse (before or
        after the attempt) is left to the build: no finding for it. Fails closed: any other exception inside the
        check rejects the attempt with `FRAMEWORK_GUARD_INCOMPLETE` (no exception text of the project in it, one
        WARNING line with the exception type). Never raises."""
        if not self._reduced_scope() or att.source in PRE_TRY_SOURCES:
            return ""
        try:
            return self._framework_guard_unsafe(pre, reported)
        except Exception as e:  # noqa: BLE001 - 검사를 끝내지 못하면 시도를 받지 않는다
            # 프로젝트의 예외 문구는 이유 문장에 넣지 않는다
            log.warning("적용 취소 (%s): 프레임워크 검사를 끝내지 못함 (%s)", att.id, type(e).__name__)
            return FRAMEWORK_GUARD_INCOMPLETE

    def _framework_javax_block(self, c: ErrorCluster) -> Any:
        """javax 케이스의 라이브러리가 프레임워크 아티팩트 자신이면 ``external_javax_api`` 차단 후보를 반환한다.
        범위를 줄인 실행이 아니거나 프레임워크 아티팩트가 아니면 None을 반환한다.

        A reduced run's javax case whose library (classpath attribution, else the owner-class fallback) IS a framework
        artifact cannot be fixed in the project: the triage's own `external_javax_api` candidate comes back with the
        framework suggestion (no pre-try, no LLM call). The libraries reached only THROUGH a framework artifact are
        known only after the final `dependency:tree` pass (`framework_via_suggestions`). Never raises."""
        if not self._reduced_scope():
            return None
        try:
            cand = self._javax_cluster(c)
            if cand is None:
                return None
            libs = self._case_libraries(c) or self._javax_libraries(c)
            fw = next((g for g in libs if self._is_framework_key(g.key)), None)
            if fw is None:
                return None
        except Exception as e:  # noqa: BLE001 - 이 확인 때문에 케이스 처리가 멈추면 안 된다
            log.debug("framework javax check for %s failed: %s: %s", c.id, type(e).__name__, e)
            return None
        cand.suggestion = framework_owner_suggestion(fw.coordinates if fw.version else fw.key, cand.required_type)
        cand.evidence = [f"the javax-era library is the framework artifact {fw.key} (framework project: its version "
                         "is kept)", *list(cand.evidence or [])]
        self._framework_blocks[c.id] = fw
        log.info("프레임워크 아티팩트가 javax 로 컴파일됨: %s — 프로젝트에서 고칠 수 없어 LLM 없이 차단", fw.key)
        return cand

    def _framework_rejected(self, att: FixAttempt, pre: str, reported: list[str], name: str) -> bool:
        """프레임워크 선언이나 ``<parent>``를 바꾼 시도를 커밋하지 않고 되돌린다. 되돌렸으면 True를 반환한다.

        The guard's consequence: the attempt fails (`_fail` restores the working tree; its sentence is in
        `apply_errors`, which the next ask shows among the previous attempts), a log line and journal
        `framework_guard`."""
        why = self._framework_guard(att, pre, reported)
        if not why:
            return False
        self._fail(att, why)
        self._out_of_scope.add(id(att))
        if why != FRAMEWORK_GUARD_INCOMPLETE:            # 검사를 끝내지 못한 경우는 _framework_guard가 이미 한 줄 남겼다
            log.warning("적용 취소 (%s): 프레임워크 프로젝트에서 바꾸면 안 되는 것을 바꿈 — %s", name, _first_sentence(why, 300))
        self.ws.journal({"event": "framework_guard", "attempt": att.id, "case": att.script_case_id, "reason": why})
        return True

    def _edit_guard_rejected(self, att: FixAttempt, pre: str, reported: list[str], name: str) -> bool:
        """호출하는 쪽이 준 ``edit_guard``가 거부한 시도를 커밋하지 않고 되돌린다. 되돌렸으면 True를 반환한다.
        sbmig 사전 시도도 검사한다. 검사 함수가 예외를 던지면 거부로 본다.

        "v2.1z-bi": the caller's `edit_guard(ws, pre, touched)` before the commit of ANY attempt (LLM script /
        edits, a stored script, sbmig's pre-tries alike). `touched` = repo-relative POSIX paths added,
        modified or deleted since `pre` (untracked new files included; the attempt's own report when git
        cannot say). A non-empty answer fails the attempt like `_framework_rejected` (`_fail`: working tree
        restored, the sentence in `apply_errors`, shown to the next ask), a log line and journal `edit_guard`;
        a guard that raises is a rejection `edit guard failed: <Type>: <msg>`. No guard → nothing is
        computed."""
        guard = self.edit_guard
        if guard is None:
            return False
        try:
            touched = list(self.ws.changed_files_since(pre)) if pre else []
        except Exception:  # noqa: BLE001 - git이 없는 대체 작업 공간이면 시도가 보고한 파일을 쓴다
            touched = []
        touched = sorted(dict.fromkeys(_norm_path(f) for f in (touched or reported or []) if f))
        try:
            why = str(guard(self.ws, pre, touched) or "")
        except Exception as e:  # noqa: BLE001 - 고장 난 검사로 변경이 통과하면 안 된다
            why = f"edit guard failed: {type(e).__name__}: {e}"
        if not why.strip():
            return False
        self._fail(att, why)
        log.warning("적용 취소 (%s): 호출자 검사가 변경을 거부함 — %s", name, _first_sentence(why, 300))
        self.ws.journal({"event": "edit_guard", "attempt": att.id, "case": att.script_case_id, "source": att.source,
                         "reason": why, "paths": touched[:20]})
        return True

    def _framework_guard_unsafe(self, pre: str, reported: list[str]) -> str:
        from sbmig.restructure.pom_editor import PomEditError, PomEditor
        try:
            changed = list(self.ws.changed_files_since(pre)) if pre else []
        except Exception:  # noqa: BLE001 - git이 없는 대체 작업 공간
            changed = []
        poms = [p for p in dict.fromkeys(_norm_path(f) for f in [*changed, *(reported or [])] if f)
                if PurePosixPath(p).name == "pom.xml"]
        problems: list[str] = []
        if not poms:
            return ""
        # 다른 pom에 정의된 속성으로 버전을 쓰는 선언도 보도록 프로젝트의 모든 pom을 읽어 부모 체인으로 속성을 푼다
        # 루트에서 바꾼 속성이 모듈의 프레임워크 선언 버전을 바꿀 수 있기 때문이다
        try:
            every = [_norm_path(p) for p in self.ws.pom_files()]
        except Exception:  # noqa: BLE001 - 대체 작업 공간이면 바뀐 pom만 본다
            every = []
        every = list(dict.fromkeys([*poms, *every]))
        # 읽지 못하거나 파싱하지 못하는 pom은 시도 전이든 뒤든 빌드에 맡긴다. 그 밖의 예외는 검사 실패로 본다
        # 권한이 000인 파일이나 pom.xml이라는 이름의 디렉토리는 OSError로 온다
        def _text(read: Callable[[], Optional[str]]) -> Optional[str]:
            try:
                return read()
            except OSError:
                return None

        eds0 = _parse_poms({p: _text(lambda p=p: self.ws.file_at(pre, p)) if pre else None for p in every},
                           PomEditor, PomEditError)
        eds1 = _parse_poms({p: _text(lambda p=p: self.ws.read(p) if self.ws.exists(p) else None) for p in every},
                           PomEditor, PomEditError)
        for p in poms:
            if p not in eds0:
                continue                                 # 새 pom에는 지킬 것이 없다
            ed0, ed1 = eds0[p], eds1.get(p)
            if ed1 is None and self.ws.exists(p):
                continue                                 # 파싱하지 못하는 pom은 빌드에 맡긴다
            b = self._framework_decls(ed0, _chain_properties(p, eds0))
            a = self._framework_decls(ed1, _chain_properties(p, eds1)) if ed1 is not None else {}
            known = {v for (_m, k, _c, _t), v in b.items() if v} | \
                {v for (_m, k, _c, _t) in b for v in self._framework_versions(k)}
            for ident, v in b.items():
                where = "dependencyManagement" if ident[0] else "dependencies"
                if ident not in a:
                    problems.append(f"removed the framework artifact {ident[1]} from the <{where}> of {p}")
                elif a[ident] != v:
                    problems.append(f"changed the version of the framework artifact {ident[1]} in {p} from "
                                    f"{v or '(managed)'} to {a[ident] or '(none)'}")
            for ident, v in a.items():
                if ident not in b and v and v not in known | self._framework_versions(ident[1]):
                    problems.append(f"added the framework artifact {ident[1]} at version {v} to {p}")
            if ed1 is not None:
                p0, p1 = self._parent_of(ed0), self._parent_of(ed1)
                if p0 != p1:
                    problems.append(f"changed the <parent> of {p} from {p0[0] or '(none)'} to {p1[0] or '(none)'}"
                                    if p0[0] != p1[0] else f"changed the <parent> relativePath of {p}")
        # 시도가 건드리지 않은 pom이라도 부모 pom의 속성이 바뀌어 프레임워크 버전이 달라졌으면 같은 문제로 본다
        for p in every:
            if p in poms or p not in eds0 or p not in eds1:
                continue
            b = self._framework_decls(eds0[p], _chain_properties(p, eds0))
            if not b:
                continue
            a = self._framework_decls(eds1[p], _chain_properties(p, eds1))
            for ident, v in b.items():
                if ident in a and a[ident] != v:
                    problems.append(f"changed the version of the framework artifact {ident[1]} in {p} from "
                                    f"{v or '(managed)'} to {a[ident] or '(none)'} (through a property in "
                                    f"{', '.join(poms[:3])})")
        if not problems:
            return ""
        shown = "; ".join(problems[:4]) + (f"; … (+{len(problems) - 4})" if len(problems) > 4 else "")
        return (f"{FRAMEWORK_GUARD_PREFIX} the attempt {shown} — nothing of it was kept. This is a framework project: "
                "every <parent> and the framework artifact's declaration and version must stay exactly as they are; fix "
                "the code instead, or answer <blocked> naming the framework artifact when only its owner can fix it")

    def _framework_decls(self, ed: Any, props: Optional[dict[str, str]] = None
                         ) -> dict[tuple[bool, str, str, str], Optional[str]]:
        """pom의 프레임워크 아티팩트 선언을 ``{(관리 여부, g:a, classifier, type): 버전}``으로 반환한다.
        버전의 속성은 풀어서 쓴다. ``props``가 있으면 부모 체인까지 모은 속성으로 푼다."""
        out: dict[tuple[bool, str, str, str], Optional[str]] = {}
        for managed in (False, True):
            for g in (ed.managed_dependencies() if managed else ed.dependencies()):
                if g.group_id and g.artifact_id and self._is_framework_key(g.key):
                    v = g.version
                    if v and "${" in v:
                        try:
                            v = (_resolve_props(v, props) if props is not None else ed.resolve(v)) or v
                        except Exception:  # noqa: BLE001
                            pass
                    out[(managed, g.key, g.classifier or "", g.type or "jar")] = v or None
        return out

    @staticmethod
    def _parent_of(ed: Any) -> tuple[str, Optional[str]]:
        """pom의 ``<parent>`` 좌표 g:a:v와 relativePath를 반환한다. 부모가 없으면 ("", None)이다."""
        g = ed.get_parent()
        coords = f"{g.group_id}:{g.artifact_id}:{g.version or ''}" if g is not None else ""
        return coords, ed.parent_relative_path() if g is not None else None

    def _protected_version_keys(self) -> set[str]:
        """sbmig가 버전을 채우거나 바꾸지 않는 g:a 집합을 반환한다.
        사내 부모, 역할 부모, 필수, 선택 아티팩트(별칭 포함), ``remove_when_present``, 프레임워크 아티팩트다.
        재구성 단계가 건드리지 않는 목록과 같다."""
        A = getattr(self.cfg, "artifacts", None)
        if A is None:
            return set()
        keys: set[str] = set()
        for spec in (getattr(A, "parent", None), getattr(A, "boot_parent", None), getattr(A, "comp_parent", None),
                     *(getattr(A, "mandatory", None) or [])):
            if spec is not None:
                keys.add(spec.key)
        try:
            keys |= set(self.cfg.optional_keys())
        except Exception:  # noqa: BLE001 - 다른 형태의 설정이면 선택 아티팩트 키만 쓴다
            keys |= {s.key for s in getattr(A, "optional", None) or []}
        keys |= {k for k in getattr(A, "remove_when_present", None) or [] if ":" in k}
        # 프레임워크 프로젝트에서는 프레임워크 아티팩트 버전을 sbmig가 쓰거나 바꾸지 않는다
        keys |= set(self._framework_keys())
        return keys

    def _pom_editor(self, rel: str) -> Any:
        """현재 트리의 pom 편집기를 반환한다. 읽지 못하면 None을 반환한다."""
        try:
            from sbmig.restructure.pom_editor import PomEditor
            return PomEditor(self.ws.abs(rel))
        except Exception as e:  # noqa: BLE001 - 파싱하지 못하는 pom에는 채울 것이 없다
            log.debug("pom %s not readable: %s: %s", rel, type(e).__name__, e)
            return None

    def _own_module_keys(self) -> set[str]:
        """프로젝트 자신의 모듈 좌표를 g:a와 ``${project.groupId}:a`` 두 형태로 반환한다.
        형제 모듈 의존성의 버전을 채우지 않기 위해 쓴다."""
        own: set[str] = set()
        for p in self.ws.pom_files():
            ed = self._pom_editor(p)
            a = ed.artifact_id() if ed is not None else None
            if a:
                own |= {f"{ed.effective_group_id()}:{a}", f"${{project.groupId}}:{a}"}
        return own

    @staticmethod
    def _coords(ed: Any) -> str | None:
        a = ed.artifact_id() if ed is not None else None
        return f"{ed.effective_group_id() or ''}:{a}" if a else None

    def _pom_chain(self, rel: str) -> list[str]:
        """pom에서 시작해 프로젝트 안의 부모 pom을 차례로 찾아 [pom, 부모 pom, …]을 반환한다.
        ``<relativePath>``를 따라 10단계까지 올라간다. 값이 없으면 ``../pom.xml``이다.
        빈 ``<relativePath/>``, 프로젝트 밖의 경로, artifactId 불일치에서 멈춘다."""
        out = [rel]
        cur = rel
        for _ in range(10):
            ed = self._pom_editor(cur)
            parent = ed.get_parent() if ed is not None else None
            rp = ed.parent_relative_path() if ed is not None else None
            if parent is None or rp == "":
                break
            target = posixpath.normpath(posixpath.join(posixpath.dirname(cur), rp or "../pom.xml"))
            if not target.endswith(".xml"):
                target = posixpath.join(target, "pom.xml")
            if target.startswith("..") or target in out or not self.ws.exists(target):
                break                                # 프로젝트 밖이거나 순환이거나 파일이 없다
            ped = self._pom_editor(target)
            if ped is None or ped.artifact_id() != parent.artifact_id:
                break
            out.append(target)
            cur = target
        return out

    @staticmethod
    def _managed_shape(ed: Any) -> tuple[str, tuple[str, ...]]:
        """pom의 부모 좌표와 BOM import 목록을 반환한다.
        시도가 sbmig가 따라갈 수 없는 방식으로 버전 관리를 바꿨는지 비교할 때 쓴다.
        일반 dependencyManagement 항목은 따로 읽으므로 넣지 않는다."""
        p = ed.get_parent()
        parent = f"{p.group_id}:{p.artifact_id}:{p.version or ''}" if p is not None else ""
        boms = tuple(sorted(f"{g.key}:{g.version or ''}" for g in ed.managed_dependencies()
                            if (g.scope or "") == "import"))
        return parent, boms

    def _reported_missing(self, st: _Round | None, rel: str, aid: str | None, key: str) -> bool:
        """이 라운드의 빌드나 되돌려서 다시 생긴 오류가 이 pom의 g:a에 버전이 없다고 보고했으면 True를 반환한다."""
        if st is None:
            return False
        for e in [*st.build.errors, *st.restored]:
            if e.signature_generic != MISSING_VERSION_SIG or e.artifact is None or e.artifact.key != key:
                continue
            f = _norm_path(e.file or "")
            if (f and f == rel) or (not f and aid and e.module == aid):
                return True
        return False

    def _fill_versions(self, att: FixAttempt, c: ErrorCluster, res: ScriptRunResult, pre: str,
                       st: _Round | None = None) -> list[tuple[str, str, str, str]]:
        """적용한 스크립트나 edits가 바꾼 pom에서 버전 없는 ``<dependencies>`` 항목에 버전을 채운다.
        시도의 커밋에 함께 들어가도록 커밋 전에 실행한다. 쓴 버전 목록을 반환하고 예외를 던지지 않는다.
        - 시도가 추가한 의존성은 관리 버전, as-is, 저장소 순서로 채운다
        - 원래 있던 의존성은 빌드가 버전이 없다고 보고했을 때만 as-is 버전으로 채운다
        - 시도가 버전을 지운 의존성은 재구성 뒤 캡처가 있을 때만 채운다
        - 시도가 버전을 써서 추가한 의존성을 새 부모 체인이 관리하면 ``<version>``을 지운다
        - 원래 있던 명시 버전, 부모나 BOM import를 바꾼 pom, 보호 좌표, 자기 모듈, system scope는 건드리지 않는다

        Steps (VersionResolver): 1 managed, 2 as-is, 3 repository. A dependency the attempt ADDED (absent from
        the pom at `pre`) by steps 1–3; one already there by step 2, only when the build reported it missing;
        one whose version the attempt dropped by steps 1–2, only with the post-restructure capture (without it
        the LLM's "the parent manages it" stands: an as-is version could be an old one). Step 1 also counts a
        `<dependencyManagement>` entry of the pom or an in-project parent (the tree as the attempt left it);
        without the post-restructure capture that is all step 1 sees, because an added dependency left empty
        would come back as pom_invalid and the structural-regression rule would revert the whole try.
        A dependency the attempt ADDED with an explicit version the new chain manages (step 1: the capture, else the
        parent-chain facts) loses that `<version>` — managed wins (the as-is build never had it, no owner's pin to
        respect), with the `${prop}` the attempt added only for it; journal `version_filled` source `managed`,
        `removed` = the explicit text. An added IN-HOUSE dependency (version-less or literal) the chain does
        not manage gets the chain's family version property `${prop}` (`_chain_property_for`; source
        `chain-property`, `property`), protected in-house coordinates included. Explicit versions that were
        already there are never touched (the restructure's rules own those). A pom whose parent or BOM imports
        the attempt changed (or one below such a pom) is left alone. Protected keys, own modules and `system`
        scope are skipped. Returns [(pom, key, version, source)] of the versions written. Never raises."""
        try:
            return self._fill_versions_unsafe(att, c, res, pre, st)
        except Exception as e:  # noqa: BLE001 - 버전 채우기 때문에 시도가 실패하면 안 된다
            log.debug("version fill for %s failed: %s: %s", att.id, type(e).__name__, e)
            return []

    def _fill_versions_unsafe(self, att: FixAttempt, c: ErrorCluster, res: ScriptRunResult, pre: str,
                              st: _Round | None) -> list[tuple[str, str, str, str]]:
        poms = [p for p in dict.fromkeys(_norm_path(f) for f in (res.changed_files or []) if f)
                if PurePosixPath(p).name == "pom.xml" and self.ws.exists(p)]
        if not poms:
            return []
        from sbmig.restructure.pom_editor import PomEditor
        before: dict[str, Any] = {}
        reshaped: set[str] = set()
        for p in poms:
            text = self.ws.file_at(pre, p) if pre else None
            ed0 = PomEditor.from_string(text, p) if text is not None else None
            before[p] = ed0
            cur = self._pom_editor(p)
            if ed0 is None or cur is None or self._managed_shape(ed0) != self._managed_shape(cur):
                reshaped.add(p)          # 새 pom이거나 시도가 버전 관리 방식을 바꿨다
        r = self._versions()
        protected = self._protected_version_keys()
        own: set[str] | None = None
        filled: list[tuple[str, str, str, str]] = []
        removed: list[tuple[str, str, str, str, Any, Any]] = []   # (pom, key, 지운 버전, 관리 버전, 시도 전 pom, 편집기)
        replaced: dict[tuple[str, str], str] = {}     # (pom, key): 체인 속성으로 바꾼 원래 값
        case = att.script_case_id or c.case_id or c.id
        for p in poms:
            if p in reshaped:
                continue
            chain = self._pom_chain(p)
            if any(q in reshaped for q in chain[1:]):
                continue
            ed = self._pom_editor(p)
            if ed is None:
                continue
            module = self._coords(ed)
            aid = ed.artifact_id()
            pre_versions = {g.key: g.version for g in before[p].dependencies()}
            local: set[str] = set()                  # 이 pom이나 프로젝트 안의 부모 pom이 관리하는 키
            for q in chain:
                qed = ed if q == p else self._pom_editor(q)
                if qed is not None:
                    local |= {g.key for g in qed.managed_dependencies()}
            for el in ed.dependency_elements(False):
                g = ed.gav_of(el)
                key = g.key
                if not g.group_id or not g.artifact_id or "${" in key:
                    continue
                if g.version and key in pre_versions:   # 원래 있던 명시 버전은 재구성 규칙의 몫이다
                    continue
                if own is None:
                    own = self._own_module_keys()
                if key in own or (g.scope or "") == "system" or self._is_framework_key(key):
                    continue
                # 시도가 추가한 사내 의존성은 체인이 관리하면 버전 없이 두고, 아니면 체인의 버전 속성 ``${prop}``을 쓴다
                # 값이 아니라 참조이므로 부모 버전이 오르면 함께 따라간다. 사내 필수, 선택 좌표도 같다
                if key not in pre_versions and (not g.version or "${" not in g.version) and is_in_house(g, self.cfg):
                    jar = (g.type or "jar") == "jar" and not g.classifier
                    mv = r.managed_version(key, module) if jar else None
                    if mv:
                        if g.version:
                            ed.remove_version(el)
                            removed.append((p, key, g.version, mv, before[p], ed))
                        continue
                    prop = self._chain_property_for(g, r)
                    if prop:
                        ref = "${" + prop + "}"
                        ed.set_ref_version(el, ref)
                        filled.append((p, key, ref, "chain-property"))
                        if g.version:
                            replaced[(p, key)] = g.version
                        continue
                if key in protected:
                    continue
                if g.version:                        # 버전을 써서 추가했으면 새 체인의 관리 버전이 이긴다
                    if (g.type or "jar") == "jar" and not g.classifier:   # 관리 버전은 type:classifier별이다
                        mv = r.managed_version(key, module)
                        if mv:
                            ed.remove_version(el)
                            removed.append((p, key, g.version, mv, before[p], ed))
                    continue
                if key in local:
                    continue
                if key not in pre_versions:          # 이 시도가 추가했다
                    # 비워 두면 다음 빌드의 pom_invalid가 구조적 악화로 시도 전체를 되돌리므로 as-is나 저장소 버전을 쓴다
                    v, src = r.resolve(key, module, allow_repository=True)
                elif pre_versions[key]:              # 시도가 버전을 지웠다
                    # 새 체인을 모르면 이전 Boot 2 버전 같은 as-is 버전을 되살리는 것이 더 나빠 LLM의 판단을 그대로 둔다
                    if not r.has_chain:
                        continue
                    v, src = r.resolve(key, module)
                elif self._reported_missing(st, p, aid, key):   # 원래 있었고 빌드가 버전이 없다고 보고했다
                    v, src = r.resolve(key, module, check_managed=False)
                else:                                # 원래 있었고 마지막 빌드가 관리 버전으로 풀었다
                    continue
                if not v:
                    continue
                ed.set_ref_version(el, v)
                filled.append((p, key, v, src))
            for _p, _k, explicit, _mv, ed0, _ed in [x for x in removed if x[0] == p]:
                self._drop_added_property(ed, ed0, explicit, p)
            if ed.changed:
                ed.save()
        for p, key, explicit, mv, _ed0, _ed in removed:
            att.notes.append(f"version removed by sbmig: {key} {explicit} in {p} (the {self._chain_label()} manages it at "
                             f"{mv})")
            log.info("버전 제거: %s %s → 부모 체인이 관리 (%s) — %s", key, explicit, mv, p)
            self.ws.journal({"event": "version_filled", "case": case, "attempt": att.id, "key": key, "version": mv,
                             "source": "managed", "removed": explicit, "pom": p})
        for p, key, v, src in filled:
            how = VERSION_SOURCE_TEXT.get(src, src)
            att.notes.append(f"version filled by sbmig: {key} {v} in {p} ({VERSION_SOURCE_NOTE.get(src, src)})")
            log.info("버전 채움: %s → %s (%s) — %s", key, v, how, p)
            extra: dict[str, Any] = {}
            if src == "chain-property":
                extra["property"] = v[2:-1]
                if (p, key) in replaced:
                    extra["replaced"] = replaced[(p, key)]
            self.ws.journal({"event": "version_filled", "case": case, "attempt": att.id, "key": key, "version": v,
                             "source": src, "pom": p, **extra})
        return filled

    def _chain_property_for(self, g: GAV, r: VersionResolver) -> str | None:
        """사내 의존성 g에 쓸 회사 체인의 버전 속성 이름을 반환한다. 없으면 None을 반환한다.
        필수, 선택 아티팩트 설정의 ``property_name``이 체인에 있으면 그것을 쓴다. 재구성과 같은 순서다.
        없으면 ``version_property_matches``의 가장 구체적인 것을 쓴다. 회사 pom이 정의한 속성만 본다."""
        props = r.chain.get("company_properties") or {}
        if not props:
            return None
        A = getattr(self.cfg, "artifacts", None)
        for spec in [*(getattr(A, "mandatory", None) or []), *(getattr(A, "optional", None) or [])]:
            if (spec.key == g.key or g.key in (spec.aliases or [])) and spec.property_name in props:
                return spec.property_name
        names = version_property_matches(g, props, in_house=True)
        return names[0] if names else None

    def _chain_property_facts(self, c: ErrorCluster, libs: Optional[list[GAV]] = None) -> list[str]:
        """케이스의 아티팩트와 라이브러리에 관련된 회사 체인 버전 속성을 프롬프트 정보 줄로 만든다. 예외를 던지지 않는다.
        ``parent chain property: <name>=<value> (<정의한 pom>)`` 형식으로 ``CHAIN_PROPERTY_FACTS_MAX``줄까지 쓴다.
        회사 pom이 정의한 속성만 쓴다.

        `libs` = the libraries located on the classpath (compile errors carry no `e.artifact`)."""
        try:
            found = {e.artifact.key: e.artifact for e in c.errors
                     if e.artifact is not None and e.artifact.group_id and e.artifact.artifact_id}
            for g in libs or []:
                if g.group_id and g.artifact_id:
                    found.setdefault(g.key, g)
            gavs = list(found.values())
            if not gavs:
                return []
            r = self._versions()
            props = r.chain.get("company_properties") or {}
            if not props:
                return []
            sources = r.chain.get("company_property_sources") or {}
            out: list[str] = []
            for gav in gavs:
                for name in version_property_matches(gav, props, in_house=is_in_house(gav, self.cfg)):
                    line = f"parent chain property: {name}={props[name]} ({sources.get(name) or 'parent chain'})"
                    if line not in out:
                        out.append(line)
            return out[:CHAIN_PROPERTY_FACTS_MAX]
        except Exception as e:  # noqa: BLE001 - 정보 때문에 시도가 멈추면 안 된다
            log.debug("chain property facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _drop_added_property(self, ed: Any, ed0: Any, version: str, rel: str) -> None:
        """지운 ``<version>``이 이 시도가 추가한 ``${속성}``이고 이제 어느 pom도 참조하지 않으면 그 속성도 지운다.
        원래 있던 속성이나 다른 곳에서 쓰는 속성은 그대로 둔다."""
        from sbmig.restructure.pom_editor import property_ref
        prop = property_ref(version)
        if not prop or not ed.has_property(prop) or prop in ed0.properties():
            return
        ref = "${" + prop + "}"
        if ref in ed.to_string():
            return
        for q in self.ws.pom_files():
            if _norm_path(q) != rel:
                try:
                    if ref in self.ws.read(q):
                        return
                except Exception:  # noqa: BLE001 - 읽을 수 없는 pom이 있으면 속성을 남긴다
                    return
        ed.remove_property(prop)

    def _declaring_pom(self, e: BuildError) -> str | None:
        """버전 없음 오류의 g:a를 버전 없이 선언한 pom을 반환한다. 찾지 못하면 None을 반환한다.
        오류의 pom, 그다음 같은 artifactId의 모듈 pom 순서로 찾고, 각각 프로젝트 안의 부모까지 본다."""
        key = e.artifact.key if e.artifact is not None else ""
        cands: list[str] = []
        f = _norm_path(e.file or "")
        if f and PurePosixPath(f).name == "pom.xml" and self.ws.exists(f):
            cands.append(f)
        if e.module:
            for p in self.ws.pom_files():
                if p not in cands:
                    ed = self._pom_editor(p)
                    if ed is not None and ed.artifact_id() == e.module:
                        cands.append(p)
        for p in cands:
            for q in self._pom_chain(p):
                ed = self._pom_editor(q)
                if ed is None:
                    continue
                for g in [*ed.dependencies(), *ed.managed_dependencies()]:
                    if g.key == key and not g.version:
                        return q
        return None

    def _version_plan(self, c: ErrorCluster
                      ) -> tuple[list[tuple[str, str, str, str, str]], list[str], bool, list[tuple[str, str, str]]]:
        """누락 버전 케이스를 LLM 없이 고칠 계획을 세운다.
        아직 시도하지 않은 (pom, g:a) 쌍마다 as-is 빌드 -> 저장소 순서로 버전을 찾는다.
        찾지 못한 쌍은 바로 기억해 시도 순서가 두 번 늦어지지 않게 한다. 찾은 쌍은 적용된 뒤 ``_version_attempt``가 기억한다.
        ([(pom, g, a, 버전, 출처)], 찾지 못한 것, 새 쌍이 있었는지, 찾은 쌍)을 반환한다."""
        errs = [e for e in c.errors if e.signature_generic == MISSING_VERSION_SIG and e.artifact is not None
                and e.artifact.group_id and e.artifact.artifact_id]
        if not errs:
            return [], [], False, []
        # 쌍은 오류가 가리키는 (pom 또는 모듈, g:a)다. 이미 확인한 쌍은 결과가 같으므로 다시 찾지 않는다
        new = [e for e in errs if (SBMIG_VERSION_SOURCE, _norm_path(e.file or "") or (e.module or ""), e.artifact.key)
               not in self._pre_tried]
        if not new:
            return [], [], False, []
        r = self._versions()
        protected = self._protected_version_keys()
        own = self._own_module_keys()
        plan: list[tuple[str, str, str, str, str]] = []
        unknown: list[str] = []
        pairs: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for e in new:
            key = e.artifact.key
            pair = (SBMIG_VERSION_SOURCE, _norm_path(e.file or "") or (e.module or ""), key)
            if pair in seen:                         # 케이스 안에 같은 쌍이 두 번 있다
                continue
            seen.add(pair)
            pom = self._declaring_pom(e)
            if self._is_framework_key(key):
                self._pre_tried.add(pair)            # 프레임워크 아티팩트 버전은 sbmig가 쓰지 않는다
                unknown.append(f"{key} (framework artifact: its version is never written by sbmig)")
                continue
            if key in protected or key in own:
                self._pre_tried.add(pair)            # 할 일이 없으니 기억만 한다
                unknown.append(f"{key} (company / own module: not filled by sbmig)")
                continue
            if pom is None:
                self._pre_tried.add(pair)
                unknown.append(f"{key} (no pom declares it without a version)")
                continue
            v, src = r.resolve(key, self._coords(self._pom_editor(pom)), allow_repository=True, check_managed=False)
            if not v:
                self._pre_tried.add(pair)            # 못 찾았으면 한 번만 기억하고 시도 순서로 넘어간다
                unknown.append(key)
                continue
            pairs.append(pair)
            if not any(x[0] == pom and f"{x[1]}:{x[2]}" == key for x in plan):
                plan.append((pom, e.artifact.group_id, e.artifact.artifact_id, v, src))
        return plan, unknown, bool(seen), pairs

    def _pre_tries(self) -> tuple[_PreTry, ...]:
        """LLM을 호출하지 않는 사전 시도 목록을 반환한다. 순서대로 보고 처음 해당하는 하나만 쓴다.
        사내 부모 버전 맞추기 -> 누락 버전 -> 리소스 필터링 -> 번들 jar -> jakarta 빌드 순서다.
        프레임워크 프로젝트는 자기 부모를 그대로 쓰므로 부모 버전 맞추기가 없다."""
        parent = () if self._reduced_scope() else (
            _PreTry(SBMIG_PARENT_SOURCE, self._parent_targets, self._parent_attempt),)
        return (*parent,
                _PreTry(SBMIG_VERSION_SOURCE, self._version_targets, self._version_attempt),
                _PreTry(SBMIG_RESOURCES_SOURCE, self._resources_targets, self._resources_attempt),
                _PreTry(SBMIG_BUNDLED_SOURCE, self._bundled_targets, self._bundled_attempt),
                _PreTry(SBMIG_JAKARTA_SOURCE, self._jakarta_targets, self._jakarta_attempt))

    def _pre_try_for(self, c: ErrorCluster, d: TriageDecision) -> tuple[_PreTry | None, list[str]]:
        """이 케이스에 먼저 실행할 사전 시도와 고칠 pom을 반환한다. 해당하지 않으면 (None, [])을 반환한다.
        triage가 SCRIPT로 정했거나, LLM을 쓸 수 없거나 예산이 없어서만 막혔을 때만 본다.
        parent_not_found로 막힌 케이스는 ``sbmig-parent``만 본다. 사람이 고정한 차단은 그대로 둔다."""
        if self.dry_run:
            return None, []
        tries = self._pre_tries()
        if d.kind != DecisionKind.SCRIPT:
            m = _BLOCKED_RAT_RE.search(d.rationale or "") if d.kind == DecisionKind.BLOCKED else None
            # triage가 parent_not_found로 차단한 케이스도 sbmig-parent 사전 시도는 먼저 실행한다
            if m is not None and m.group("r") == BlockReason.PARENT_NOT_FOUND.value:
                tries = tuple(t for t in tries if t.source == SBMIG_PARENT_SOURCE)
            elif m is None or m.group("r") not in (BlockReason.LLM_UNAVAILABLE.value, BlockReason.BUDGET_EXHAUSTED.value):
                return None, []
        for pre in tries:
            try:
                poms = pre.targets(c)
            except Exception as e:  # noqa: BLE001 - 판단 함수가 실패하면 사전 시도가 없는 것으로 본다
                log.debug("pre-try %s for %s: %s: %s", pre.source, c.id, type(e).__name__, e)
                poms = None
            if poms is not None:
                return pre, poms
        return None, []

    def _parent_misses(self, c: ErrorCluster) -> list[Any]:
        """부모를 찾지 못한 오류 중 사전 시도가 맞출 대상을 반환한다.
        사내 부모 g:a의 as-is 버전이고 이번 실행에서 아직 시도하지 않은 것이다.
        ``openrewrite.unresolvable_asis_parent: align``일 때만 반환하고 프레임워크 프로젝트는 빈 목록이다."""
        if self._reduced_scope() or getattr(self.cfg.openrewrite, "unresolvable_asis_parent", "align") != "align":
            return []
        from sbmig.restructure.asis_parent import classify_gav
        out = []
        for e in c.errors:
            if e.kind != ErrorKind.PARENT_RESOLUTION or e.artifact is None:
                continue
            m = classify_gav(e.artifact, self.cfg, self.ws)
            if m.kind == "asis-version" and (SBMIG_PARENT_SOURCE, m.coordinates, "") not in self._pre_tried \
                    and all(x.coordinates != m.coordinates for x in out):
                out.append(m)
        return out

    def _parent_targets(self, c: ErrorCluster) -> list[str] | None:
        """사내 부모의 as-is 버전을 찾지 못한 케이스면 그 부모를 선언한 pom 목록을, 아니면 None을 반환한다."""
        if not any(e.kind == ErrorKind.PARENT_RESOLUTION for e in c.errors):
            return None
        misses = self._parent_misses(c)
        if not misses:
            return None
        from sbmig.restructure.asis_parent import declared_parents
        keys = {(m.gav.key, m.gav.version) for m in misses}
        return [rel for rel, g, _p in declared_parents(self.ws) if (g.key, g.version) in keys] or None

    def _parent_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """사전 시도 ``sbmig-parent``를 실행한다. 적용하면 True, 맞출 것이 없으면 False를 반환한다.
        저장소에 없는 사내 부모의 as-is 버전을 선언한 모든 pom에 설정 버전을 쓴다(``align_asis_parent``).
        커밋 ``fix(sbmig-parent): <cluster id>``, 저널 ``asis_parent_aligned``, 플래그 ``asis-parent-aligned``를 남긴다.
        부모 좌표마다 실행당 한 번만 시도한다."""
        from sbmig.restructure.asis_parent import align_asis_parent
        misses = self._parent_misses(c)
        if not misses:
            return False
        results: list[tuple[Any, Any]] = []

        def apply() -> ScriptRunResult:
            changed: list[str] = []
            for m in misses:
                r = align_asis_parent(self.ws, m)
                results.append((m, r))
                changed += [p for p in r.poms if p not in changed]
            return ScriptRunResult(status="ok", changed_files=changed)

        what = ", ".join(f"{m.coordinates} → {m.target.version} ({m.spec_name})" for m in misses)
        log.info("sbmig 부모 버전 맞춤 (LLM 0회): %s", what)
        applied = self._pre_attempt(
            pr, st, c, source=SBMIG_PARENT_SOURCE, apply_fn=apply,
            notes=[f"as-is parent {m.coordinates} is not in the repository → the configured {m.target.coordinates} "
                   f"({m.spec_name})" for m in misses],
            keys=[(SBMIG_PARENT_SOURCE, m.coordinates, "") for m in misses],
            rationale=f"sbmig-parent: the as-is version of the company parent is not in the repository ({what}) — no LLM call",
            analysis="the build cannot resolve an as-is version of a configured company parent: "
                     + ", ".join(m.coordinates for m in misses),
            solution="sbmig sets the configured parent version in those poms — no LLM call")
        for m, r in results:
            if not r.poms:
                continue
            self.ws.journal({"event": "asis_parent_aligned", "phase": st.phase.value, "iteration": st.iteration,
                             "parent": m.gav.key, "from": m.gav.version, "to": m.target.version,
                             "target": m.target.coordinates, "spec": m.spec_name, "poms": list(r.poms),
                             "properties": dict(r.properties), "where": "fix loop", "applied": applied})
            if applied:
                self._flag("asis-parent-aligned")
                from sbmig.restructure.asis_parent import loop_aligned_followup
                self._followup(loop_aligned_followup(m, r.poms))
        return applied

    def _version_targets(self, c: ErrorCluster) -> list[str] | None:
        """누락 버전 케이스면 오류의 pom 목록을, 아니면 None을 반환한다. pom이 없으면 빈 목록이다."""
        if not any(e.signature_generic == MISSING_VERSION_SIG for e in c.errors):
            return None
        return list(dict.fromkeys(_norm_path(e.file) for e in c.errors
                                  if e.signature_generic == MISSING_VERSION_SIG and e.file))

    def _resources_targets(self, c: ErrorCluster) -> list[str] | None:
        """resources나 war 플러그인의 필터링 복사가 실패한 케이스면 고칠 pom 목록을, 아니면 None을 반환한다.
        그런 케이스에서만 pom을 읽도록 정규식으로 먼저 거른다."""
        from sbmig.restructure.resources_filtering import REACTIVE_RE, reactive_targets
        errs = list(c.errors)
        if not any(REACTIVE_RE.search(f"{e.message or ''}\n{e.raw or ''}") for e in errs):
            return None
        return list(reactive_targets(self.ws, errs)) or None

    def _pre_try_deferral(self, st: _Round, poms: list[str]) -> tuple[list[str], str]:
        """사전 시도를 다음 빌드까지 미뤄야 하면 (원인 케이스 목록, "pom" 또는 "files")를 반환한다.
        이번 라운드에 다른 케이스의 수정이 대상 pom이나 루트 pom을 바꿨거나 그 pom에서 실행됐으면 기다린다.
        한 pom의 두 시도가 함께 평가되거나 앞 시도의 git revert가 이 시도의 변경과 충돌하지 않게 하기 위해서다."""
        files = list(dict.fromkeys(_norm_path(f) for f in poms if f))
        by = [st.poms[f] for f in files if f in st.poms]
        if "pom.xml" in st.poms and st.poms["pom.xml"] not in by:
            by.append(st.poms["pom.xml"])
        if by:
            return list(dict.fromkeys(by)), "pom"
        by = [st.covered[f] for f in files if f in st.covered]
        return (list(dict.fromkeys(by)), "files") if by else ([], "")

    def _pre_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster, *, source: str, rationale: str,
                     analysis: str, solution: str, notes: list[str], keys: list[tuple[str, str, str]],
                     edits: list | None = None, apply_fn: Callable[[], ScriptRunResult] | None = None) -> bool:
        """사전 시도 한 번을 공통 순서로 실행하고 적용하면 True를 반환한다.
        결정과 시도 기록 -> ``_begin_try`` -> ``_apply_edits`` -> 키 정리 -> 미루기 기록 -> 이력 기록 순서다.
        적용된 키만 확인한 것으로 기억한다. 적용에 실패한 키는 ``PRE_TRY_APPLY_FAILURES``번까지 다시 시도한다.

        script_mode `new`, no stored script, commit `fix(<source>): <cluster id>`; `apply_fn` edits the files
        itself and takes the same commit / scoring / rollback path as edits."""
        t0 = time.monotonic()
        d = TriageDecision(cluster_id=c.id, kind=DecisionKind.SCRIPT, rationale=rationale)
        st.decisions.append(d)
        self._journal_decision(st, d)
        st.n += 1
        att = self._new_attempt(st.phase, st.iteration, st.n, c, d, st.build)
        att.errors_before = st.build.error_count + len(st.restored)
        att.script_mode, att.script_case_id, att.source = "new", c.case_id or c.id, source
        att.analysis, att.solution = analysis, solution
        att.notes += notes
        self._begin_try(att, st)
        self._apply_edits(att, c, list(edits or []), st, subject=f"fix({source}): {c.id}", apply_fn=apply_fn)
        # applier 예외, 커밋 실패, Windows의 일시적 파일 잠금 같은 적용 실패는 다음 라운드에 다시 시도한다
        for key in keys:
            if att.applied:
                self._pre_tried.add(key)
            else:
                self._pre_failures[key] += 1
                if self._pre_failures[key] >= PRE_TRY_APPLY_FAILURES:
                    self._pre_tried.add(key)
        if att.applied:                                 # 바꾼 pom의 모듈 컴파일 케이스는 다음 빌드를 기다린다
            for ch in att.changes:
                for path in (ch.path, ch.new_path):
                    if path and PurePosixPath(_norm_path(path)).name == "pom.xml":
                        st.poms.setdefault(_norm_path(path), c.id)
        self._seconds[id(att)] = time.monotonic() - t0
        self._record(pr, att)
        self._sync_try(att)
        return att.applied

    def _version_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """누락 버전 케이스를 LLM 없이 고친다. LLM 예산은 쓰지 않는다.
        계획한 버전을 ``set_dependency_version`` 편집으로 만들어 ``_apply_edits``로 적용한다. 커밋은 ``fix(sbmig-version):
        <case>``다. 적용하면 True를 반환한다. False면 같은 라운드에서 평소 시도 순서로 이어진다."""
        plan, unknown, fresh, pairs = self._version_plan(c)
        if not fresh:
            return False
        case_id = c.case_id or c.id
        self.ws.journal({"event": "version_try", "phase": st.phase.value, "iteration": st.iteration, "case": case_id,
                         "resolved": [{"pom": p, "key": f"{g}:{a}", "version": v, "source": src}
                                      for p, g, a, v, src in plan], "unknown": unknown})
        if not plan:
            log.info("sbmig 버전 채움: %s — as-is 빌드·저장소에 버전 없음 → LLM 사다리 (사실은 프롬프트에)",
                     ", ".join(unknown[:5]) + (f", … (+{len(unknown) - 5})" if len(unknown) > 5 else ""))
            return False
        what = ", ".join(f"{g}:{a} {v}" for _p, g, a, v, _s in plan)
        edits: list[Any] = []
        notes: list[str] = []
        from sbmig.core.models import Edit
        for p, g, a, v, src in plan:
            how = VERSION_SOURCE_TEXT.get(src, src)
            notes.append(f"version {g}:{a} {v} in {p} ({VERSION_SOURCE_NOTE.get(src, src)})")
            edits.append(Edit(op="pom", path=p, desc=f"{p} 의 {g}:{a} 의존성에 버전 {v} 을(를) 적습니다 ({how})",
                              pom={"set_dependency_version": {"group_id": g, "artifact_id": a, "version": v}}))
        if unknown:
            notes.append("no version found (as-is build, repository): " + ", ".join(unknown[:10]))
        log.info("sbmig 버전 채움 (LLM 0회): %s", ", ".join(
            f"{g}:{a} → {v} ({VERSION_SOURCE_TEXT.get(src, src)})" for _p, g, a, v, src in plan))
        return self._pre_attempt(
            pr, st, c, source=SBMIG_VERSION_SOURCE, edits=edits, notes=notes, keys=pairs,
            rationale=f"sbmig-version: the missing version(s) from the as-is build / the repository ({what}) — "
                      "no LLM call",
            analysis=(f"the build reports dependency versions missing: the {self._chain_label()} does not manage "
                      + ", ".join(f"{g}:{a}" for _p, g, a, _v, _s in plan)),
            solution="sbmig writes the versions the as-is build used (else the newest release in the repository) — "
                     "no LLM call")

    def _resources_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """리소스 필터링 케이스를 LLM 없이 고친다. 적용하면 True, 추가할 것이 없으면 False를 반환한다.
        ``binary_resource_plan``이 찾은 확장자를 ``apply_non_filtered_extensions``로 ``nonFilteredFileExtensions``에
        넣는다. 커밋은 ``fix(sbmig-resources): <cluster id>``이고 (pom, 확장자 묶음)마다 실행당 한 번만 시도한다.
        확장자가 없는 바이너리 파일은 인수인계 후속 조치로 남긴다."""
        from sbmig.restructure.resources_filtering import apply_non_filtered_extensions, binary_resource_plan
        plan = binary_resource_plan(self.ws, self.cfg, errors=list(c.errors))
        for fu in plan.followups:
            self._followup(fu)
        keys = {pom: (SBMIG_RESOURCES_SOURCE, pom, ",".join(exts))
                for pom, exts in plan.extensions_by_pom.items() if exts}
        fresh = [pom for pom, k in keys.items() if k not in self._pre_tried]
        seen = (SBMIG_RESOURCES_SOURCE, c.case_id or c.id, "")          # 추가할 것이 없다는 저널은 한 번만 남긴다
        if not fresh:
            if seen not in self._pre_tried:
                self._pre_tried.add(seen)
                self.ws.journal({"event": "resources_try", "phase": st.phase.value, "iteration": st.iteration,
                                 "case": c.case_id or c.id, "lines": [], "files": list(plan.files),
                                 "followups": list(plan.followups)})
                log.info("sbmig 리소스 필터링: 추가할 nonFilteredFileExtensions 없음 → LLM 사다리 (사실은 프롬프트에)")
            return False
        lines = plan.lines(only=fresh)
        self.ws.journal({"event": "resources_try", "phase": st.phase.value, "iteration": st.iteration,
                         "case": c.case_id or c.id, "lines": lines, "files": list(plan.files),
                         "followups": list(plan.followups)})
        log.info("sbmig 리소스 필터링 (LLM 0회): %s", "; ".join(lines))
        notes = [f"nonFilteredFileExtensions: {ln}" for ln in lines] + list(plan.followups)

        def apply() -> ScriptRunResult:
            return ScriptRunResult(status="ok", changed_files=apply_non_filtered_extensions(self.ws, plan, poms=fresh))

        return self._pre_attempt(
            pr, st, c, source=SBMIG_RESOURCES_SOURCE, apply_fn=apply, notes=notes, keys=[keys[p] for p in fresh],
            rationale=f"sbmig-resources: binary files under resource filtering ({'; '.join(lines)}) — no LLM call",
            analysis=("maven-resources-plugin / maven-war-plugin read binary files of a filtered resource as text: "
                      + ", ".join(plan.files[:RESOURCE_FACTS_FILES] or ["(the file the error names)"])),
            solution="sbmig adds their extensions to nonFilteredFileExtensions (filtering stays on) — no LLM call")

    def _resources_facts(self, c: ErrorCluster) -> list[str]:
        """리소스 필터링 케이스의 프롬프트 정보 줄을 만든다. 예외를 던지지 않는다.
        계획이 찾은 pom별 확장자와 필터링되는 바이너리 파일을 ``RESOURCE_FACTS_FILES``개까지 쓴다.
        사전 시도가 적용되지 못했을 때 LLM이 참고한다."""
        try:
            if self._resources_targets(c) is None:
                return []
            from sbmig.restructure.resources_filtering import binary_resource_plan
            plan = binary_resource_plan(self.ws, self.cfg, errors=list(c.errors))
            out = [f"binary resources under filtering: {ln} (nonFilteredFileExtensions)" for ln in plan.lines()]
            out += [f"binary file under resource filtering: {f}" for f in plan.files[:RESOURCE_FACTS_FILES]]
            return out
        except Exception as e:  # noqa: BLE001 - 정보 때문에 시도가 멈추면 안 된다
            log.debug("resource facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _version_facts(self, c: ErrorCluster, libs: Optional[list[GAV]] = None) -> list[str]:
        """프롬프트 "Dependency versions" 절의 앞부분 정보 줄을 만든다. 예외를 던지지 않는다.
        - 오류의 아티팩트와 classpath에서 찾은 라이브러리마다 as-is 빌드 버전과 새 체인, 목표 Boot BOM의 관리 버전을 쓴다
        - 메이저가 다르면 메이저 변화를 덧붙이고, classpath 라이브러리의 관리 줄 끝에는 ``API_CHANGED_TAIL``을 붙인다
        - 캡처가 없으면 그 줄을 쓰지 않는다. 모르는 것을 선언되지 않았다고 말하지 않기 위해서다
        라이브러리는 ``VERSION_FACTS_MAX``개까지다.

        Lines: `as-is build: <g:a> <v> (<how>)`, `<chain label>: manages <g:a> at <v>`, `target Boot BOM:
        manages …`; in-house artifacts included."""
        try:
            mods: dict[str, str | None] = {}
            located: set[str] = set()
            for e in c.errors:
                a = e.artifact
                if a is not None and a.group_id and a.artifact_id and a.key not in mods:
                    mods[a.key] = e.module
            for g in libs or []:
                if g.group_id and g.artifact_id and g.key not in mods:
                    mods[g.key] = self._lib_module(c)
                    located.add(g.key)
            if not mods:
                return []
            r = self._versions()
            if not (r.has_asis or r.has_chain or r.boot):
                return []
            out: list[str] = []
            for key, module in list(mods.items())[:VERSION_FACTS_MAX]:
                asis_v: str | None = None
                if r.has_asis:
                    asis_v, how = r.asis_fact(key, module)
                    out.append(f"as-is build: {key} {asis_v} ({how})" if asis_v else f"as-is build: {key} not declared")
                tail = API_CHANGED_TAIL if key in located else ""
                mv = r.managed_version(key, module) if r.has_chain else None
                if mv:
                    out.append(f"{self._chain_label()}: manages {key} at {mv}{_major_delta(asis_v, mv)}{tail}")
                bv = r.boot_version(key, module)
                if bv:                               # 체인이 관리하지 않을 때만 목표 Boot BOM 줄을 쓴다
                    out.append(f"target Boot BOM: manages {key} at {bv}{_major_delta(asis_v, bv)}{tail}")
            return out
        except Exception as e:  # noqa: BLE001 - 정보 때문에 시도가 멈추면 안 된다
            log.debug("version facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _package_facts(self, c: ErrorCluster) -> list[str]:
        """javax가 아닌 ``package <p> does not exist`` 케이스의 정보 줄을 만든다. 예외를 던지지 않는다.
        새 체인과 목표 Boot BOM이 관리하는 키 중 ``<p>``의 접두어인 가장 긴 groupId의 artifactId를 나열한다.
        ``org.springframework.batch.item``이면 ``org.springframework.batch``다. 맞는 그룹이 없으면 쓰지 않는다.
        의존성을 추가해야 할 때 LLM이 좌표를 짐작하지 않게 하기 위해서다. 번들 jar 정보 줄이 먼저 온다.

        One `managed artifacts under <group> (add the one that holds <p>, version-less): …` line per package
        (≤ `PACKAGE_FACTS_LINES`, ≤ `PACKAGE_FACTS_ARTIFACTS` names each)."""
        try:
            pkgs: list[str] = []
            for e in c.errors:
                for text in (e.message, e.raw):
                    m = _PACKAGE_MISSING_RE.search(text or "")
                    if m and not m.group(1).startswith("javax.") and m.group(1) not in pkgs:
                        pkgs.append(m.group(1))
                        break
            bundled = self._bundled_facts(c)
            if not pkgs:
                return bundled
            keys = self._managed_keys()
            if not keys:
                return bundled
            by_group: dict[str, set[str]] = {}
            for k in keys:
                g, _, a = k.partition(":")
                if g and a:
                    by_group.setdefault(g, set()).add(a)
            out: list[str] = []
            for pkg in pkgs[:PACKAGE_FACTS_LINES]:
                hits = [g for g in by_group if pkg == g or pkg.startswith(g + ".")]
                if not hits:
                    continue
                group = max(hits, key=len)
                arts = sorted(by_group[group])
                shown = ", ".join(arts[:PACKAGE_FACTS_ARTIFACTS]) + \
                    (f", … (+{len(arts) - PACKAGE_FACTS_ARTIFACTS})" if len(arts) > PACKAGE_FACTS_ARTIFACTS else "")
                line = f"managed artifacts under {group} (add the one that holds {pkg}, version-less): {shown}"
                if line not in out:
                    out.append(line)
            return bundled + out
        except Exception as e:  # noqa: BLE001 - 정보 때문에 시도가 멈추면 안 된다
            log.debug("package facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _managed_keys(self) -> set[str]:
        """새 부모 체인과 목표 Boot BOM이 관리하는 g:a 집합을 반환한다.
        재구성 뒤 캡처가 있으면 전체와 모듈별 항목을, 없으면 부모 체인 정보를 쓴다."""
        r = self._versions()
        keys: set[str] = set()
        if r.has_after:
            keys |= set((r.after.get("managed") or {}).keys())
            for mod in (r.after.get("by_module") or {}).values():
                if isinstance(mod, dict):
                    keys |= set((mod.get("managed") or {}).keys())
        else:
            keys |= set((r.chain.get("managed") or {}).keys())
        keys |= set(r.boot.keys())
        return {str(k) for k in keys if k}

    def _numstat(self, a: str, b: str) -> list[tuple[str, int, int]]:
        """두 커밋 사이에 바뀐 파일마다 (경로, 추가 줄 수, 삭제 줄 수)를 반환한다.
        바이너리 파일은 0 / 0이고 이름이 바뀐 파일은 새 경로로 쓴다. git이 없으면 빈 목록이다."""
        try:
            out = self.ws._git("diff", "--numstat", "-z", a, b, check=False).stdout or ""   # noqa: SLF001
        except Exception:  # noqa: BLE001 - git이 없는 대체 작업 공간
            return []
        toks = out.split("\0")
        res: list[tuple[str, int, int]] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if not t.strip():
                i += 1
                continue
            parts = t.split("\t")
            if len(parts) < 3:
                i += 1
                continue
            add, rem, path = parts[0], parts[1], parts[2]
            if not path and i + 2 < len(toks):             # 이름 변경은 "<add>\t<rem>\t" NUL 이전 경로 NUL 새 경로다
                path = toks[i + 2]
                i += 3
            else:
                i += 1
            res.append((path, int(add) if add.isdigit() else 0, int(rem) if rem.isdigit() else 0))
        return res

    def _drop_reference_writes(self, att: FixAttempt, pre: str) -> None:
        """수정이 병합 충돌 참고 파일의 경로에 파일을 썼으면 커밋 전에 되돌린다.
        참고 파일은 프롬프트에 보여 주려고 실행 폴더에 둔 것이라 빌드나 수정의 대상이 아니다."""
        refs = self._ref_paths.get(id(att))
        if not refs:
            return
        try:
            hit = [f for f in self.ws.changed_files_since(pre) if _norm_path(f) in refs]
            for f in hit:
                tracked = self.ws._git("cat-file", "-e", f"{pre}:{f}", check=False).returncode == 0   # noqa: SLF001
                if tracked:
                    self.ws._git("checkout", pre, "--", f, check=False)                            # noqa: SLF001
                elif self.ws.exists(f):
                    self.ws.delete(f)
        except Exception as e:  # noqa: BLE001 - 실패해도 계속한다. 남은 것은 diff에 보인다
            log.warning("could not undo writes to reference copies for %s: %s", att.id, e)
            return
        if hit:
            att.notes.append(f"dropped {len(hit)} change(s) aimed at a read-only reference copy: {', '.join(hit[:3])}")

    def _credit_script(self, att: FixAttempt) -> None:
        """저장된 스크립트 버전의 실적을 ``kb.cases.credit``으로 기록한다.
        - 그 버전을 실행한 결과가 시도 결과를 정한 경우만, 시도마다 한 번 기록한다
        - inconclusive였다가 다시 평가되면 한 번 더 기록한다
        - edits와 함께 저장만 된 스크립트, v0, 드라이런, KB 없음이면 기록하지 않는다
        - 왕복에 참여한 시도는 결과 대신 round_trip으로 한 번 기록한다

        The run record goes along: round, try n, failure reason, error counts, files, scope, code. An attempt
        in `_round_trips` is credited `round_trip` with that reason instead of its outcome, also when it was
        already credited with another result (the resolver A; same round / n, so the store finds the earlier
        record), once; it gets the note `stored v<N> credited as round_trip: …` and its FixTry is synced. An
        attempt whose `script_mode` is `reuse` passes `reused=True` (its success leaves the `round_trip`
        mark)."""
        if self.dry_run or att.decision.kind != DecisionKind.SCRIPT or not att.script_version \
                or not att.script_case_id or att.outcome == "pending" or not self._script_ran.get(id(att)):
            return
        done = self._credited.get(id(att))
        trip = self._round_trips.get(id(att))
        # 왕복에 참여한 시도는 이미 다른 결과로 기록됐어도 round_trip으로 한 번 더 기록한다
        if trip is not None and done == ROUND_TRIP:
            return
        if trip is None and done is not None and done != "inconclusive":
            return
        if not self.cases.active:
            return
        # 횟수와 함께 남기는 실행 기록이다. 라운드, 시도 번호, 실패 이유, 오류 수, 파일
        _cid, n, rnd = self._tries.get(id(att), (att.script_case_id, 0, 0))
        if trip is not None:
            reason = trip
        else:
            reason = "" if att.outcome in ("improved", "build_ok") else self._run_reason(att)
        run = {"reason": reason, "round": int(rnd or 0), "n": int(n or 0), "errors_before": att.errors_before,
               "errors_left": att.errors_after, "errors_new": len(att.introduced_errors),
               "files": list(dict.fromkeys(ch.new_path or ch.path for ch in att.changes))}
        # 프레임워크 검사가 되돌린 시도는 스크립트 실패가 아니므로 out_of_scope로 넘긴다
        # 그 버전이 이 실행과 같은 범위면 저장소가 apply_failed로 기록한다. 옮겨진 케이스 id는 code로 찾는다
        result = OUT_OF_SCOPE if id(att) in self._out_of_scope and att.outcome == "apply_failed" else att.outcome
        if trip is not None:
            result = ROUND_TRIP                     # 왕복은 이 시도의 성공이 아니다
        run["scope"] = self._run_scope()
        run["code"] = att.script_code or ""
        # LLM 없이 재사용한 버전의 성공은 왕복 표시를 지우지 않는다. 표시 전에 계획된 재사용일 수 있다
        if att.script_mode == "reuse":
            run["reused"] = True
        # 이전 저장소가 받지 않는 키워드는 어댑터(sbmig.loop.entries.CaseScripts)가 뺀다
        if not self.cases.credit(att.script_case_id, att.script_version, result, project=self._project(),
                                 run_id=self.ws.run_id, **run):
            return
        self._credited[id(att)] = ROUND_TRIP if trip is not None else att.outcome
        self.ws.journal({"event": "script_credit", "case": att.script_case_id, "version": att.script_version,
                         "attempt": att.id, "outcome": att.outcome, "reason": reason,
                         **({"credited": result} if result != att.outcome else {})})
        if trip is not None:
            note = f"stored v{att.script_version} credited as round_trip: not reused without an LLM answer"
            if note not in att.notes:
                att.notes.append(note)
            self._sync_try(att)

    def _run_reason(self, att: FixAttempt) -> str:
        """평가된 시도가 케이스를 해결하지 못한 이유를 한 줄로 반환한다. 개선이나 빌드 성공이면 빈 문자열이다.
        case.json만 보고도 어느 버전이 왜 실패했고 왜 다음 버전이 생겼는지 알 수 있게 하기 위해서다.

        ScriptRun.reason / an augment's CaseScript.reason: `no_change: 2 of 2 targeted error(s) remained —
        <the first one>`, else the try's notes and the first new error of a regression."""
        if att is None or att.outcome in ("improved", "build_ok", "pending"):
            return ""
        head = ""
        if att.outcome == "no_change":
            gone, targeted = self._tally.get(id(att), (0, len(att.cluster_errors)))
            head = (f"no_change: {max(0, targeted - gone)} of {targeted} targeted error(s) remained" if targeted
                    else "no_change: the targeted error(s) remained")
            first = next((fmt_error(e) for e in att.cluster_errors), "")
            if first:
                head += f" — {_first_sentence(first, 160)}"
        note = _try_note(att)
        if att.outcome in ("regressed", "reverted") and att.introduced_errors:
            note += ("; " if note else "") + f"new: {_first_sentence(fmt_error(att.introduced_errors[0]), 160)}"
        try:
            from sbmig.errors.cases import run_reason
            return run_reason(att.outcome, note, head)
        except Exception:  # noqa: BLE001 - KB 패키지는 sbmig 안에 있지만 방어한다
            return "; ".join(x for x in (head, note) if x) or att.outcome

    def _credit_scripts(self, pr: PhaseResult, attempts: list[FixAttempt]) -> None:
        """여러 시도의 실적을 ``_credit_script``로 차례로 기록한다."""
        for a in attempts:
            self._credit_script(a)

    def _no_script_fixer(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """``propose_script``가 없는 tier-2 fixer를 처리한다. 이 실행은 저장된 스크립트 재사용만 할 수 있다.
        인프라 문제가 아니므로 ``infra:llm`` 대신 ``fixer-without-scripts`` 플래그와 후속 조치 한 줄을 남긴다.
        그 케이스는 llm_unavailable로 차단한다."""
        if not self.llm_unavailable:
            self.llm_unavailable = why[:300]
            setter = getattr(self.triager, "set_llm_unavailable", None)
            if callable(setter):
                setter(self.llm_unavailable)
            self._flag(FIXER_WITHOUT_SCRIPTS_FLAG)
            self._followup(f"{why} — no LLM fix script could be written in this run; the cases that needed one are "
                           "blocked (llm_unavailable)")
            log.error("%s", why)
            self.ws.journal({"event": "fixer_without_scripts", "phase": att.phase.value, "iteration": att.iteration,
                             "reason": why})
        self._block_simple(att.phase, att.iteration, c, BlockReason.LLM_UNAVAILABLE, self.llm_unavailable)

    def _new_attempt(self, phase: Phase, iteration: int, n: int, c: ErrorCluster, d: TriageDecision,
                     build: BuildResult) -> FixAttempt:
        """새 FixAttempt를 만든다. id는 ``<phase>-<build>-<n>``이다.
        대상 오류를 담고 현재 HEAD를 ``base_commit``으로 기록한다. ``base_commit..commit``이 이 시도의 변경이다."""
        att = FixAttempt(id=f"{phase.value}-{iteration}-{n}", phase=phase, iteration=iteration, cluster_id=c.id,
                         decision=d, errors_before=build.error_count, cluster_signatures=list(c.signatures),
                         cluster_errors=list(c.errors))   # 결과 평가와 오류 목록에 쓴다
        try:   # 되돌릴 때 base_commit..commit을 쓴다
            att.base_commit = self.ws.head()
        except Exception as e:  # noqa: BLE001 - git이 없으면 diff만 없다
            log.debug("base commit for %s unavailable: %s", att.id, e)
        return att

    def _record(self, pr: PhaseResult, att: FixAttempt) -> None:
        """시도를 단계 결과와 실행 이력에 넣고 저널에 한 줄 남긴다. 시도 순서와 횟수 한도는 이 이력을 기준으로 한다."""
        pr.attempts.append(att)
        self.history.append(att)
        self.ws.journal({"event": "attempt", "phase": att.phase.value, "iteration": att.iteration,
                         "attempt": att.id, "cluster": att.cluster_id, "decision": att.decision.kind.value,
                         "applied": att.applied, "commit": att.commit, "outcome": att.outcome,
                         "model": att.model, "files": [ch.path for ch in att.changes],
                         "apply_errors": att.apply_errors, "analysis": att.analysis[:500],
                         "solution": att.solution[:500], "source": att.source, "edits_applied": att.edits_applied,
                         "no_script_reason": att.no_script_reason[:300], "advisory_ids": list(att.advisory_ids),
                         "script": ({"mode": att.script_mode, "case": att.script_case_id,
                                     "version": att.script_version, "parent": att.script_parent,
                                     "status": att.script_result.status if att.script_result is not None else None,
                                     "error": (att.script_result.error or "")[:300] if att.script_result else "",
                                     "log_lines": len(att.script_result.log) if att.script_result else 0}
                                    if att.decision.kind == DecisionKind.SCRIPT else None)})

    def _commit(self, message: str, pre: str, paths: Optional[list[str]] = None) -> Optional[str]:
        """시도를 커밋하고 sha를 반환한다. 바뀐 것이 없으면 None을 반환한다.
        직전 빌드가 lib/에 복사한 jar 같은 파일은 수정이 아니므로 스크립트나 edits가 보고한 파일만 넣는다."""
        if paths is not None:
            if not paths:
                return None
            sha = self.ws.commit(message, paths=paths)
            return sha if sha != pre else None
        if not self.ws.is_dirty():
            return None
        sha = self.ws.commit(message)
        return sha if sha != pre else None

    def _fail(self, att: FixAttempt, why: str) -> None:
        """시도를 적용 실패로 표시하고, 일부만 적용된 상태가 남지 않도록 커밋되지 않은 변경을 버린다."""
        att.outcome = "apply_failed"
        att.apply_errors.append(why)
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
        except Exception:  # pragma: no cover
            pass

    def _summary(self) -> str:
        """프롬프트에 넣을 프로젝트 요약을 반환한다. 실행 중에 한 번만 만든다."""
        if self._profile_summary is None:
            self._profile_summary = profile_summary(self.profile)
        return self._profile_summary

    def _advisory_hints(self, c: ErrorCluster) -> tuple[list[str], list[str]]:
        """이 클러스터에 맞는 라이브러리 권고의 (힌트, 권고 id)를 반환한다.
        권고로 차단하지는 않고 프롬프트 힌트로만 쓴다.

        `sbmig.advisory.hints_for`."""
        if not self.advisories:
            return [], []
        try:
            from sbmig.advisory.hints import hints_for
            root = getattr(self.cfg, "repo_root", None)
            root = Path(root) if isinstance(root, (str, Path)) else None
            hints, ids = hints_for(c.errors, self.advisories, self.profile, notes_root=root)
        except Exception as e:  # pragma: no cover - hints_for는 예외를 던지지 않지만 import를 방어한다
            log.warning("advisory hints unavailable: %s", e)
            return [], []
        if ids:
            log.debug("advisory hints for %s (%s):\n%s", c.id, ", ".join(ids), "\n".join(f"- {h}" for h in hints))
        return list(hints), list(ids)

    def _llm_transport_failure(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """LLM 전송이나 설정 실패를 기록하고, 연속 2번이 되면 대응한다.
        - 실행에서 처음이면 ``llm.cooldown_seconds``만큼 쉬고 다시 시도한다. 그 시도들은 케이스의 LLM 한도에 세지 않는다
        - 두 번째 연속 실패이거나 쿨다운을 쓸 수 없으면 실행 끝까지 LLM 사용 불가로 둔다
        LLM이 필요한 케이스는 llm_unavailable로 차단되고 저장된 스크립트 재사용만 계속한다."""
        self._llm_streak.append((c, why))
        self._streak_attempts.append(att.id)
        att.notes.append(f"LLM transport/config failure: {why[:200]}")
        if self.llm_unavailable or len(self._llm_streak) < 2:
            return
        if self._llm_cooldown(att, why):
            return
        self.llm_unavailable = why[:300] or "LLM transport errors"
        self._flag("llm-unavailable")
        setter = getattr(self.triager, "set_llm_unavailable", None)
        if callable(setter):
            setter(self.llm_unavailable)
        self._followup(f"the LLM was unavailable during this run ({self.llm_unavailable}); cases that needed it "
                       "are listed as blocked (llm_unavailable) — proven stored fix scripts were still reused")
        self.ws.journal({"event": "llm_unavailable", "phase": att.phase.value, "iteration": att.iteration,
                         "reason": self.llm_unavailable, "clusters": [x.id for x, _ in self._llm_streak],
                         "cooldown_used": self.llm_cooldown_used})
        for x, _w in self._llm_streak:
            self._block_simple(att.phase, att.iteration, x, BlockReason.LLM_UNAVAILABLE,
                               f"LLM unavailable ({self.llm_unavailable})")
        self._llm_streak, self._streak_attempts = [], []

    def _llm_config_check(self, att: FixAttempt, c: ErrorCluster, analysis: str, prompt_hash: str) -> bool:
        """HTTP 400 답을 확인한다. 400이면 시도를 실패로 두고 True를 반환한다. 이 시도는 여기서 끝난다.
        서로 다른 프롬프트 ``LLM_CONFIG_ERROR_PROMPTS``개가 같은 본문을 받으면 설정 문제로 보고 실행의 LLM 호출을 멈춘다.
        지원하지 않는 모델 이름이나 페이로드 필드 같은 문제다. 플래그 ``llm-config-error``와 후속 조치 한 줄을 남긴다."""
        body = llm_400_body(analysis)
        if body is None:
            return False
        self._fail(att, analysis.strip()[:300])
        seen = self._http400.setdefault(body, set())
        seen.add(prompt_hash or f"{c.id}:{att.id}")
        if len(seen) < LLM_CONFIG_ERROR_PROMPTS or self.llm_unavailable:
            return True
        self.llm_config_error = body
        self.llm_unavailable = f"LLM configuration error: HTTP 400 '{body[:120]}' for {len(seen)} different prompts"
        self._flag("llm-config-error")
        setter = getattr(self.triager, "set_llm_unavailable", None)
        if callable(setter):
            setter(self.llm_unavailable)
        log.error("LLM configuration error: the gateway answered HTTP 400 '%s' to %d different prompts — no more LLM "
                  "calls in this run", body[:200], len(seen))
        self._followup(f"the LLM gateway rejected {len(seen)} different prompts with the same HTTP 400 ({body[:160]}) "
                       "— a configuration error (model name / payload fields in .env or llm.*), not the prompts; LLM "
                       "calls stopped for this run, fix the setting and rerun")
        self.ws.journal({"event": "llm_config_error", "phase": att.phase.value, "iteration": att.iteration,
                         "body": body, "prompts": len(seen)})
        self._block_simple(att.phase, att.iteration, c, BlockReason.LLM_UNAVAILABLE, self.llm_unavailable)
        return True

    def _llm_cooldown(self, att: FixAttempt, why: str) -> bool:
        """게이트웨이 503 같은 일시 장애에 대비해 LLM을 포기하기 전에 실행당 한 번 쉰다. 쉬었으면 True를 반환한다.
        이미 썼거나, 꺼져 있거나, 남은 실행 시간보다 길면 False를 반환한다. 그러면 호출한 쪽이 LLM 사용 불가로 둔다."""
        seconds = max(0, int(getattr(self.cfg.llm, "cooldown_seconds", 0) or 0))
        if self.llm_cooldown_used or seconds <= 0:
            return False
        b = self.budget
        if b.max_minutes > 0 and b.time_left() <= seconds:
            att.notes.append(f"LLM cool-down skipped: less than {seconds}s left of the run time budget")
            return False
        clusters = [x.id for x, _ in self._llm_streak]
        self.llm_cooldown_used = True
        self._flag("llm-cooldown")
        att.notes.append(f"LLM cool-down: 2 transport/config failures in a row → pausing {seconds}s, then retrying "
                         "(once per run; the next streak marks the LLM unavailable)")
        self.ws.journal({"event": "llm_cooldown", "phase": att.phase.value, "iteration": att.iteration,
                         "seconds": seconds, "reason": why[:300], "clusters": clusters})
        log.warning("LLM transport failed twice in a row (%s) — cooling down %ss before retrying", why[:200], seconds)
        (self._sleep or sleep_seconds)(seconds)
        self._cooled_attempts |= set(self._streak_attempts)
        self._llm_streak, self._streak_attempts = [], []
        return True

    # ------------------------------------------------------------------ 병합 충돌 힌트
    def _merge_conflict_hints(self, att: FixAttempt, c: ErrorCluster) -> tuple[list[str], dict[str, str]]:
        """``c``와 관련된 병합 충돌의 (참고 메모, 참고 파일)을 반환한다. 병합은 먼저 온 파일을 남긴다.
        남긴 파일이 오류 파일이거나 오류가 그 클래스 이름을 언급하면 관련된 것으로 본다.
        버려진 쪽 파일은 읽기 전용 참고로만 보여 준다. 필요한 멤버를 옮겨 오게 하고 두 번째 모듈을 다시 만들지 않게 한다."""
        if not self.merge_conflicts:
            return [], {}
        try:
            return self._merge_conflict_hints_unsafe(att, c)
        except Exception as e:  # pragma: no cover - 힌트 때문에 시도가 멈추면 안 된다
            log.warning("merge-conflict hints unavailable for %s: %s", c.id, e)
            return [], {}

    def _merge_conflict_hints_unsafe(self, att: FixAttempt, c: ErrorCluster) -> tuple[list[str], dict[str, str]]:
        lay = self.cfg.layout
        rp = getattr(self.profile, "root_pom", None)
        pname = (getattr(rp, "artifact_id", None) or getattr(self.profile, "name", "") or "")
        base = _norm_path(lay.module_path("comp", pname) or f"{lay.module_dir}/{lay.comp_module}")
        files = {_norm_path(f) for f in [c.file, *(e.file for e in c.errors)] if f}
        text = "\n".join(f"{e.message}\n{e.symbol or ''}\n{e.location or ''}\n{e.raw}" for e in c.errors)
        notes: list[str] = []
        refs: dict[str, str] = {}
        for mc in self.merge_conflicts:
            if mc.identical or getattr(mc, "union", False) or not mc.saved_copies:
                continue
            kept = _norm_path(f"{base}/{mc.path}")
            rel = _norm_path(mc.path)
            by_file = kept in files
            stem = PurePosixPath(rel).name
            stem = stem[:-len(".java")] if stem.endswith(".java") else ""
            by_symbol = bool(stem) and re.search(rf"(?<![\w$]){re.escape(stem)}(?![\w$])", text) is not None
            if not (by_file or by_symbol):
                continue
            dropped = ", ".join(f"`{d}`" for d in mc.dropped_from) or "another module"
            notes.append(f"{kept} was merged first-wins from module `{mc.kept_from}`; the copy from {dropped} was "
                         "dropped (saved under the run directory). If this error is about a member that only the "
                         "dropped copy had, port it from the reference copy below — do not re-create a second module.")
            att.notes.append(f"merge-conflict hint: {kept}")
            for saved in mc.saved_copies:
                if len(refs) >= REFERENCE_MAX_FILES:
                    break
                label = _norm_path(saved)
                try:
                    content = (self.ws.run_dir / saved).read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    notes.append(f"(the saved copy {label} could not be read: {type(e).__name__})")
                    continue
                if len(content) > REFERENCE_MAX_CHARS:
                    content = content[:REFERENCE_MAX_CHARS - len(_TRUNCATED_MARK)] + _TRUNCATED_MARK
                refs[label] = content
        if notes:
            self.ws.journal({"event": "merge_conflict_hints", "phase": att.phase.value, "iteration": att.iteration,
                             "cluster": c.id, "reference_files": list(refs)})
        return notes, refs

    def _changed_between(self, a: str, b: str) -> list[str]:
        """두 커밋 사이에 바뀐 파일 목록을 반환한다.
        ``changed_between``은 NUL로 구분하므로 한글이나 공백이 있는 파일 이름도 그대로 받는다."""
        changed = getattr(self.ws, "changed_between", None)
        if callable(changed):
            return changed(a, b)
        out = self.ws._git("diff", "--name-only", a, b).stdout   # noqa: SLF001 - changed_between이 없는 대체 작업 공간
        return [l.strip() for l in out.splitlines() if l.strip()]

    # ------------------------------------------------------------------ 대체 빌드
    def _maybe_skip_test_compile(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                                 last: BuildResult) -> bool:
        """남은 오류가 모두 테스트 컴파일 오류면 ``-Dmaven.test.skip=true``로 다시 빌드한다. 통과하면 True를 반환한다.
        통과하면 플래그 ``test-compile-skipped``를 남기고 테스트 오류는 담당 팀의 후속 조치로 남긴다.
        기본 설정 ``build.skip_test_compile``은 처음부터 테스트를 컴파일하지 않으므로 보통은 이 경우가 없다."""
        if not self.cfg.build.allow_skip_test_compile_fallback:
            return False
        if not any(g in goal.split() for g in TEST_COMPILING_GOALS):
            return False
        if not last.errors or any(e.kind != ErrorKind.TEST_COMPILE for e in last.errors):
            return False
        self.ws.journal({"event": "test_compile_fallback", "phase": phase.value, "test_errors": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests", phase, pr.iterations + 1, skip_test_compile=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("test-compile fallback (-Dmaven.test.skip=true) also failed")
            return False
        pr.build_ok = True
        self.test_compile_skipped = True
        self.test_compile_skipped_targets.add(_target_key(target))
        self._flag("test-compile-skipped")
        pr.remaining_errors = list(last.errors)
        pr.notes.append(f"test-compile-skipped: main code builds, {len(last.errors)} test compile error(s) remain "
                        "(-Dmaven.test.skip=true)")
        for e in last.errors:
            where = f"{e.file}:{e.line}" if e.file else (e.module or "?")
            self._followup(f"test sources do not compile (build passes only with -Dmaven.test.skip=true): "
                           f"{where} {e.message}")
        return True

    def _maybe_skip_tests(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                          last: BuildResult) -> bool:
        """메인 코드는 빌드되고 테스트 실행 실패만 남았으면 ``-DskipTests``로 다시 빌드한다. 통과하면 True를 반환한다.
        이런 실패는 대개 외부 설정이나 빌드 시점에 접근할 수 없는 인프라 때문이다.
        통과하면 플래그 ``tests-skipped``를 남기고 실패한 테스트마다 후속 조치를 남긴다. 최종 보고에서는 미해결이다."""
        if not getattr(self.cfg.build, "allow_skip_tests_fallback", False):
            return False
        if not any(g in goal.split() for g in TEST_RUNNING_GOALS):
            return False
        if not last.errors or any(e.kind != ErrorKind.TEST_FAILURE for e in last.errors):
            return False
        self.ws.journal({"event": "tests_fallback", "phase": phase.value, "failing": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests-run", phase, pr.iterations + 1, skip_tests=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("tests fallback (-DskipTests) also failed")
            return False
        pr.build_ok = True
        self.tests_skipped = True
        self.tests_skipped_targets.add(_target_key(target))
        self._flag("tests-skipped")
        pr.remaining_errors = list(last.errors)
        pr.notes.append(f"tests-skipped: main code builds and packages, {len(last.errors)} test(s) fail at runtime "
                        "(-DskipTests)")
        for e in last.errors:
            who = e.symbol or e.module or "?"
            self._followup(f"test fails at runtime (packaged with -DskipTests): {who} — {e.message} — likely an "
                           "externalized setting (see Handover 'Properties to set') or infrastructure not reachable "
                           "at build time")
        return True

    def _flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def _followup(self, text: str) -> None:
        if text not in self.followups:
            self.followups.append(text)


VERSIONS_LOGGED = 6                  # `의존성 버전 조회:` 줄에 목록마다 보여 주는 최신 버전 수


def _versions_text(cands: list) -> str:
    """후보 버전 목록을 ``2.5.0, 2.6.0 (jakarta ✓)`` 같은 텍스트로 만든다.
    최신 ``VERSIONS_LOGGED``개를 쓰고 pom을 확인한 버전에는 jakarta / javax 표시를 붙인다."""
    items = []
    for v in cands:
        tag = " (jakarta ✓)" if getattr(v, "jakarta", None) is True else (
            " (javax)" if getattr(v, "jakarta", None) is False else "")
        items.append(f"{v.version}{tag}")
    if len(items) > VERSIONS_LOGGED:
        items = ["…"] + items[-VERSIONS_LOGGED:]
    return ", ".join(items)


def _versions_line(rv: Any, key: str, current: str | None) -> str:
    """저장소 조회 결과를 로그 한 줄로 만든다. 현재 버전, 같은 메이저 후보, 상위 메이저 후보다.
    조회에 실패했으면 ``의존성 버전 조회 실패: <key>``로 시작하는 줄을 만든다."""
    k = getattr(rv, "key", "") or key
    if getattr(rv, "error", "") and not (rv.same_major or rv.higher):
        return f"의존성 버전 조회 실패: {k} — {' '.join(str(rv.error).split())[:200]}"
    cur = getattr(rv, "current", None) or current or "?"
    line = f"의존성 버전 조회: {k} {cur} → 같은 메이저 {_versions_text(rv.same_major) or '없음'}"
    if rv.higher:
        line += f" / 상위 {_versions_text(rv.higher)}"
    return line




def _try_note(att: FixAttempt) -> str:
    """FixTry에 남길 메모를 만든다.
    적용 오류, 되돌리기, 결과 평가 메모를 중복 없이 합치고 ``TRY_NOTE_CHARS``자에서 자른다."""
    parts = list(dict.fromkeys([*att.apply_errors, *att.notes]))
    text = "; ".join(" ".join(p.split()) for p in parts if p)
    return text if len(text) <= TRY_NOTE_CHARS else text[:TRY_NOTE_CHARS - 1] + "…"


_PROP_REF_RE = re.compile(r"\$\{([^}]+)\}")
_CHAIN_DEPTH = 12                                     # 프레임워크 검사가 따라가는 프로젝트 부모 체인 단계 수


def _parse_poms(texts: dict[str, Optional[str]], editor: Any, unparsable: type[BaseException]) -> dict[str, Any]:
    """``{경로: pom 텍스트}``를 ``{경로: PomEditor}``로 바꾼다. 없거나 파싱하지 못한 pom은 빠진다.
    파싱 오류(``unparsable``)만 건너뛰고 다른 예외는 호출한 쪽으로 던진다.

    Only the editor's own "does not parse" error (`unparsable`, PomEditError: undecodable bytes, malformed XML,
    a root that is not <project>) leaves a pom out; any other exception travels to the caller."""
    out: dict[str, Any] = {}
    for p, text in texts.items():
        if text is None:
            continue
        try:
            out[p] = editor.from_string(text, p)
        except unparsable:                               # 파싱하지 못하는 pom은 빌드에 맡긴다
            continue
    return out


def _parent_pom_path(path: str, ed: Any, eds: dict[str, Any]) -> Optional[str]:
    """프로젝트 안에서 pom의 부모 pom 경로를 반환한다. 없으면 None을 반환한다.
    relativePath(기본 ``../pom.xml``)를 먼저 보고, 맞지 않으면 g:a로 찾는다."""
    g = ed.get_parent()
    if g is None:
        return None
    rel = ed.parent_relative_path()
    if rel is None:
        rel = "../pom.xml"
    if rel.strip():
        cand = posixpath.normpath(posixpath.join(posixpath.dirname(path), rel.strip().replace("\\", "/")))
        if not cand.endswith("pom.xml"):
            cand = posixpath.join(cand, "pom.xml")
        other = eds.get(cand)
        if other is not None and other.artifact_id() == g.artifact_id:
            return cand
    for p, other in eds.items():
        if p != path and other.artifact_id() == g.artifact_id and (other.effective_group_id() or "") == (g.group_id or ""):
            return p
    return None


def _chain_properties(path: str, eds: dict[str, Any]) -> dict[str, str]:
    """pom과 프로젝트 안의 부모 체인에서 속성을 모아 반환한다. 자식 pom의 값이 이긴다.

    Plus project.version / groupId / artifactId of the pom itself. Parents outside the project are not
    read."""
    chain: list[Any] = []
    cur: Optional[str] = path
    seen: set[str] = set()
    while cur and cur not in seen and cur in eds and len(chain) < _CHAIN_DEPTH:
        seen.add(cur)
        chain.append(eds[cur])
        cur = _parent_pom_path(cur, eds[cur], eds)
    props: dict[str, str] = {}
    for ed in reversed(chain):
        try:
            props.update(ed.properties())
        except Exception:  # noqa: BLE001
            continue
    if chain:
        me = chain[0]
        for k, fn in (("project.version", me.effective_version), ("project.groupId", me.effective_group_id),
                      ("project.artifactId", me.artifact_id)):
            try:
                props.setdefault(k, fn() or "")
            except Exception:  # noqa: BLE001
                pass
    return props


def _resolve_props(value: str, props: dict[str, str]) -> str:
    """``${name}``을 ``props``로 여러 단계까지 푼다. 풀지 못한 참조는 그대로 둔다."""
    for _ in range(_CHAIN_DEPTH):
        new = _PROP_REF_RE.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def _kept_partial(a: FixAttempt) -> bool:
    """부분 해결된 사전 시도면 True를 반환한다. 이런 시도는 다음 시도 전에 되돌리지 않는다.
    as-is 빌드, 저장소, 파일 스캔으로 확인한 내용을 썼고, 남은 오류는 sbmig가 찾지 못한 것뿐이기 때문이다."""
    return a.source in PRE_TRY_SOURCES and a.outcome == "partial"


def framework_owner_suggestion(framework: str, api: Optional[str] = None, path: Optional[list[str]] = None) -> str:
    """프레임워크 아티팩트 때문에 차단된 javax 항목의 조치 문구를 만든다.
    프레임워크 아티팩트를 밝히고 소유팀이 jakarta 빌드를 내야 한다고 쓴다.

    `framework` = g:a or g:a:v still compiled against javax.*; with `path`, the javax API / library arrives
    through it ("v2.1z-ak")."""
    tail = (f"it cannot be fixed in the project — the owner of the framework artifact `{framework}` must provide a "
            "jakarta build of it (sbmig keeps the framework version as it is)")
    if path:
        what = f"`{api}`" if api else "a javax-era library"
        return f"{what} comes through the framework artifact `{framework}` ({' → '.join(path)}): {tail}"
    still = f" (it still takes `{api}`)" if api else ""
    return f"`{framework}` is the in-house framework artifact of this project and is still compiled against javax.*{still}: {tail}"


def framework_via_suggestions(items: Iterable[Any], profile: Any, cfg: Any = None) -> int:
    """프레임워크 아티팩트를 거쳐 들어온 javax 차단 항목의 조치 문구를 프레임워크 문구로 바꾸고 바꾼 수를 반환한다.
    범위를 줄인 실행이 아니면 0을 반환한다. 예외를 던지지 않는다.

    Called by the orchestrator's final pass right after `blocked_registry.fill_via`: an `external_javax_api` /
    `external_api_incompatible` item whose `via` path (the `dependency:tree` path from the module to the javax
    jar) or `via_direct` contains a framework artifact. Items already naming a framework artifact are kept."""
    if not getattr(profile, "reduced_scope", False):
        return 0
    fwc = getattr(cfg, "framework", None) if cfg is not None else None
    fw = getattr(profile, "framework", None)
    keys = set(fw.keys()) if fw is not None else set()

    def is_fw(coords: Optional[str]) -> bool:
        k = ":".join(str(coords or "").split(":")[:2])
        if not k or k.count(":") != 1:
            return False
        try:
            return k in keys or bool(fwc is not None and fwc.matches_key(k))
        except Exception:  # noqa: BLE001
            return False

    changed = 0
    for item in items or []:
        try:
            if item.reason not in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE):
                continue
            if item.artifact is not None and is_fw(item.artifact.key):
                continue                                  # 이미 프레임워크 아티팩트 자신으로 차단됐다
            path = [str(x) for x in (item.via or [])]
            hit = next((x for x in path[1:] if is_fw(x)), None)
            if hit is None and item.via_direct is not None and is_fw(item.via_direct.key):
                hit = item.via_direct.coordinates
            if hit is None:
                continue
            api = item.artifact.coordinates if item.artifact is not None else None
            new = framework_owner_suggestion(hit, api, path or [hit])
            if new != item.suggestion:
                item.suggestion = new
                changed += 1
        except Exception as e:  # noqa: BLE001 - 보고용이라 실패로 다루지 않는다
            log.debug("framework via suggestion skipped: %s: %s", type(e).__name__, e)
    return changed


def _major_delta(before: Optional[str], after: Optional[str]) -> str:
    """as-is 버전과 관리 버전의 메이저가 다르면 메이저 변화 표시를 반환한다.
    둘 중 하나를 모르거나 메이저가 같으면 빈 문자열을 반환한다.

    `" (major X → Y)"`."""
    from sbmig.build.depchanges import major_of
    a, b = (major_of(before) if before else None), (major_of(after) if after else None)
    return f" (major {a} → {b})" if a is not None and b is not None and a != b else ""


def _mentions_javax(errors: Iterable[BuildError]) -> bool:
    """케이스 오류의 메시지, 심볼, 위치, 원문 중에 ``javax.``가 있으면 True를 반환한다.
    저장소 목록과 판단은 이런 케이스에만 붙인다."""
    return any("javax." in (t or "") for e in errors for t in (e.message, e.symbol, e.location, e.raw))


def _synthetic(e: BuildError) -> bool:
    """Maven이 비정상 종료되거나 타임아웃일 때 실행기가 대신 넣는 오류면 True를 반환한다.
    이런 오류로는 빌드가 어디까지 갔는지 알 수 없다."""
    return e.kind == ErrorKind.UNKNOWN and not e.file and (e.message or "").startswith(_SYNTHETIC_PREFIXES)


def _place(e: BuildError) -> str:
    """오류의 위치를 반환한다. 파일이 있으면 정규화한 파일, 없고 모듈이 있으면 ``@<module>``, 둘 다 없으면 ''이다.

    Maven stops at the first failing module, so a module-level error (no file) shows in one module per build:
    the module tells the occurrence of module 2 from the one of module 1 ("v2.1z-cb")."""
    if e.file:
        return _norm_path(e.file)
    return f"@{e.module}" if e.module else ""


def _pair(e: BuildError) -> tuple[str, str]:
    """시도 결과 평가와 정체, 진동 판단에 쓰는 (위치, 서명) 쌍을 반환한다.
    L1 서명에는 파일과 모듈이 없어 다른 곳의 같은 오류가 같은 서명이 되므로 위치와 짝지어 횟수로 비교한다."""
    return _place(e), (e.signature or f"{e.kind.value}|{e.message}")


def _touched(a: FixAttempt) -> set[str]:
    """시도가 바꾼 파일 경로를 반환한다. 이름이 바뀐 파일은 이전 경로와 새 경로를 모두 넣는다."""
    return {_norm_path(p) for c in a.changes for p in (c.path, c.new_path) if p}


def _targeted_left(a: FixAttempt, cur: Counter) -> tuple[int, int]:
    """(빌드에 아직 남은 대상 오류 수, 전체 대상 오류 수)를 (위치, 서명) 횟수로 세어 반환한다.
    오류 기록이 없는 시도는 서명만으로 전부 남았는지 전부 사라졌는지 판단한다."""
    if not a.cluster_errors:
        sigs = set(a.cluster_signatures)
        return (1 if any(s in sigs for _f, s in cur) else 0), 1
    targeted = Counter(_pair(e) for e in a.cluster_errors)
    return sum(min(n, cur[k]) for k, n in targeted.items()), sum(targeted.values())


def _stage_of(errors: list[BuildError]) -> int:
    """이 오류들이 보고되는 가장 이른 빌드 단계 번호를 반환한다. 단계를 아는 오류가 없으면 99를 반환한다."""
    return min((error_stage(e) for e in errors if error_stage(e) is not None), default=99)


def _visible_in(errors: list[BuildError], build: BuildResult, real: list[BuildError]) -> bool:
    """지금은 없는 이 오류들을 이 빌드가 보고할 수 있었으면 True를 반환한다.
    그 모듈이 빌드됐거나 빌드가 그 단계까지 갔을 때다. 아니면 해결이 아니라 보이지 않을 뿐이라 시도는 판단 불가가 된다.

    The same notion as the error list's visibility."""
    if build.ok:
        return True
    if not real:
        return False
    built = set(build.modules_built or [])
    failed = {build.failed_module} | {x.module for x in real if x.module}
    # 빌드가 어디까지 갔는지 본다. 파일 없는 UNKNOWN 오류는 JVM 비정상 종료나 BUILD FAILURE 줄이라 단계를 알려 주지 않는다
    reached = max((error_stage(x) if error_stage(x) is not None else -1
                   for x in real if x.kind != ErrorKind.UNKNOWN or x.file), default=-1)
    for e in errors:
        if _synthetic(e):
            return False                         # 대체 오류는 사라져도 해결된 것이 아니다
        m = e.module
        if m and m in built:
            continue
        if m and build.failed_module and m not in failed:
            return False                         # 다른 모듈이 실패해서 그 모듈은 실행되지 않았다
        st = error_stage(e)
        if st is None or reached < st:
            return False
    return True


def _new_errors(errors: list[BuildError], prev: Counter) -> list[BuildError]:
    """직전 빌드보다 늘어난 (위치, 서명) 오류를 새 오류로 반환한다."""
    seen: Counter = Counter()
    out: list[BuildError] = []
    for e in errors:
        k = _pair(e)
        seen[k] += 1
        if seen[k] > prev[k]:
            out.append(e)
    return out


def _new_in_files(errors: list[BuildError], files: set[str], prev: Counter) -> list[BuildError]:
    """``files`` 안에서 새로 생긴 오류를 (파일, 서명) 횟수로 찾아 반환한다.
    다른 파일에 이미 있던 L1 서명이어도 이 파일에서는 새 오류다."""
    return [e for e in _new_errors([e for e in errors if e.file and _norm_path(e.file) in files], prev)]


def _target_key(target: Optional[str]) -> str:
    """빌드 대상을 정규화한 키를 반환한다. 빈 문자열은 프로젝트 전체이고, 끝의 pom.xml은 뗀다.
    대체 빌드 결과를 대상별로 기억할 때 쓴다."""
    t = _norm_path(target or "").strip("/")
    if t.endswith("pom.xml"):
        t = t[:-len("pom.xml")].rstrip("/")
    return t


def _gone(errors: list[BuildError], build: BuildResult) -> list[BuildError]:
    """``errors`` 중 이 빌드에 더 이상 없는 오류를 (위치, 서명) 횟수로 반환한다. 시도가 없앤 오류다."""
    cur = Counter(_pair(e) for e in build.errors)
    seen: Counter = Counter()
    out: list[BuildError] = []
    for e in errors:
        k = _pair(e)
        seen[k] += 1
        if seen[k] > cur[k]:
            out.append(e)
    return out


def _change_line(a: FixAttempt) -> str:
    """시도가 바꾼 내용을 한 줄로 만든다. 다른 범위의 같은 오류 힌트에 쓴다.
    바꾼 파일 목록을 쓰고, 없으면 원인 분석의 첫 문장을 쓴다."""
    if a.changes:
        files = [c.new_path or c.path for c in a.changes]
        return f"changed {len(files)} file(s): {', '.join(files[:4])}" + (" …" if len(files) > 4 else "")
    return _first_sentence(a.analysis or "", 160) or "-"


def _same_case(a: FixAttempt, c: ErrorCluster) -> bool:
    """시도가 이 케이스의 시도면 True를 반환한다. 케이스 id가 없는 사전 시도는 클러스터 id로 비교한다."""
    if a.cluster_id and a.cluster_id == c.id:
        return True
    return bool(a.script_case_id) and a.script_case_id == (c.case_id or c.id)


def _case_key(a: FixAttempt) -> str:
    """시도의 케이스 id를 반환한다. 케이스 id가 없으면(사전 시도) 클러스터 id에서 ``case:``를 뗀 값을 쓴다."""
    k = a.script_case_id or a.cluster_id or ""
    return k.removeprefix("case:")


def _files_named(files: list[str]) -> str:
    """파일 목록의 첫 파일을 반환한다. 더 있으면 ``(+N more)``를 붙인다."""
    if not files:
        return "?"
    return files[0] + (f" (+{len(files) - 1} more)" if len(files) > 1 else "")


def _same_attempt_case(a: FixAttempt, b: FixAttempt) -> bool:
    """두 시도의 클러스터 id나 케이스 id가 같으면 True를 반환한다."""
    if a.cluster_id and a.cluster_id == b.cluster_id:
        return True
    return bool(_case_key(a)) and _case_key(a) == _case_key(b)


class _DiffHunk(NamedTuple):
    """``-U0`` diff의 hunk 하나를 담는 클래스. 이전 쪽과 새 쪽 범위, 셀 수 있는 추가 줄과 지운 줄이다."""
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    added: frozenset[str]
    removed: frozenset[str]
    added_at: tuple[tuple[int, str], ...] = ()     # 셀 수 있는 + 줄마다 (새 쪽 줄 번호, 공백을 뗀 내용)


_HUNK_HEAD = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_hunks(text: str) -> list[_DiffHunk]:
    """unified diff를 hunk 목록으로 나눈다. 줄은 앞뒤 공백을 떼고, 빈 줄과 문자나 숫자가 없는 줄은 뺀다.

    One `_DiffHunk` per `@@ -l[,n] +l[,n] @@` (a missing count is 1, git's convention); the headers
    (`diff --git` / `---` / `+++`) before a hunk are skipped; each +/- line stripped of surrounding whitespace and
    counted only when it has a letter or a digit (`}` / `);` / blank do not); duplicates collapse (sets)."""
    out: list[_DiffHunk] = []
    # cur = [old_start, old_len, new_start, new_len, added, removed, added_at, 다음 새 쪽 줄 번호]
    cur: Optional[list[Any]] = None

    def close() -> None:
        if cur is not None:
            out.append(_DiffHunk(cur[0], cur[1], cur[2], cur[3], frozenset(cur[4]), frozenset(cur[5]), tuple(cur[6])))

    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln.startswith("diff --git"):
            close()
            cur = None
            continue
        m = _HUNK_HEAD.match(ln)
        if m:
            close()
            new_start = int(m[3])
            cur = [int(m[1]), int(m[2]) if m[2] is not None else 1, new_start,
                   int(m[4]) if m[4] is not None else 1, set(), set(), [], new_start]
            continue
        if cur is None or ln[:1] not in ("+", "-"):
            continue
        body = ln[1:].strip()
        countable = any(ch.isalnum() for ch in body)
        if ln[0] == "+":
            if countable:
                cur[4].add(body)
                cur[6].append((cur[7], body))
            cur[7] += 1
        elif countable:
            cur[5].add(body)
    close()
    return out


def _covers(start: int, length: int, line: int) -> bool:
    """hunk 한쪽 범위가 그 줄을 덮으면 True를 반환한다. 길이가 0이면 시작 줄 하나로 본다.

    `start..start+length-1`; for `length == 0` git's `-l,0` / `+l,0` names the line AFTER which lines were inserted
    (old side) or removed (new side), 0 = before the first line: that single position `l` is the range."""
    if length > 0:
        return start <= line < start + length
    return line == start


def _put_back_at(g: _DiffHunk, h: _DiffHunk, line: int) -> bool:
    """g가 h를 되돌린 자리에 그 줄이 있으면 True를 반환한다.
    h가 지운 줄이 있으면 g가 되살린 그 줄들의 위치를, 없으면 g의 새 쪽 범위를 본다.

    Condition 4: where `g` put `h`'s lines back. When `h` removed countable lines, `line` must be the new-side number of
    one of the lines `g` added back that `h` had removed (`added_at`): `-U0` joins adjacent changes into one hunk, so
    "inside g's new-side range" alone would also cover a rewritten line next to a restored one. When `h` removed
    nothing (a pure addition that `g` deletes), `g`'s new-side position (`_covers`)."""
    if h.removed:
        return any(n == line for n, t in g.added_at if t in h.removed)
    return _covers(g.new_start, g.new_len, line)


def _reverses(g: _DiffHunk, h: _DiffHunk) -> bool:
    """hunk g가 hunk h를 통째로 되돌리면 True를 반환한다. h가 추가한 줄을 모두 지우고 지운 줄을 모두 되살린 경우다.

    `g` removes every countable line `h` added and adds back every countable line `h` removed; a hunk `h` without a
    countable line is never reversed."""
    return bool(h.added or h.removed) and h.added <= g.removed and h.removed <= g.added


def _decode_strict(data: bytes, encodings: Iterable[str]) -> str:
    """바이트를 후보 인코딩으로 차례로 엄격하게 디코딩한다. 맞는 인코딩이 없으면 ValueError를 던진다."""
    for enc in [*encodings, "utf-8"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("the diff decodes with none of the encoding candidates")


def _diff_change_lines(text: str) -> list[str]:
    """unified diff에서 머리말을 빼고 @@ 줄과 +/- 줄만 남긴다. 줄마다 ``EARLIER_FIX_LINE_CHARS``자로 자른다."""
    out: list[str] = []
    in_hunk = False
    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln.startswith("@@"):
            in_hunk = True
        elif not in_hunk or not ln.startswith(("+", "-")):
            if ln.startswith("diff --git "):
                in_hunk = False
            continue
        out.append(ln if len(ln) <= EARLIER_FIX_LINE_CHARS else ln[:EARLIER_FIX_LINE_CHARS - 1] + "…")
    return out


def _norm_path(p: str) -> str:
    p = (p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def refine_build(build: Optional[BuildResult]) -> Optional[BuildResult]:
    """빌드 오류의 케이스 키를 그 자리에서 다듬고 빌드 결과를 그대로 반환한다.
    대략적인 ``build`` / ``other`` 키를 javac 줄로 더 나눈다.
    시도 결과 평가, 오류 목록, 클러스터, 차단 목록이 같은 L1 / L2 서명을 보도록 빌드 직후 한 번만 실행한다.

    `sbmig.triage.cluster.refine_signatures`; a failure keeps the parser's keys (one WARNING)."""
    if build is None or not build.errors:
        return build
    try:
        from sbmig.triage.cluster import refine_signatures
        refine_signatures(build.errors)
    except Exception as e:  # pragma: no cover - 실패하면 파서가 만든 키를 그대로 쓴다
        log.warning("signature refinement failed: %s: %s", type(e).__name__, e)
    return build
