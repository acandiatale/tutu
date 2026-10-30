"""빌드-수정 루프(v2.1) — 최종 빌드가 실패하면 오케스트레이터가 부른다. 라운드마다: 빌드 → 직전 시도
채점(악화는 되돌림, 저장 스크립트 실적 기록, 오류 목록의 시도 마감) → 새 오류 추가 → 케이스(L2)로 묶기 →
케이스마다 사다리 → 커밋. 사다리(케이스당·실행당): KB 탐색 → 저장된 스크립트 재사용(LLM 0회) → 실패하면
되돌리고 LLM 보강 → 저장본이 없으면 LLM 신규(edits 는 바로 적용, 스크립트는 unverified 로 저장). 저장본 1 +
LLM 3회(``script.max_augments_per_case``) 뒤 BLOCKED. 채점은 재빌드로만(``_score``): 개선 = 대상 오류가 전부
사라지고 빌드가 그 단계까지 갔을 때뿐("안 보임 ≠ 해결"), 판정 불가 = KB 실적 없음. 개선되지 않은 시도는 그
케이스의 다음 시도 전에 되돌린다. 근본 원인 케이스 먼저, 이번 라운드에 이미 손댄 파일·모듈 pom 의 케이스는
다음 빌드까지 미룬다(시도로 세지 않음). 끝 = 언제나 빌드 뒤: BUILD SUCCESS · 라운드
소진(``loop.phases.verify.max_iterations``, 50) · 손댈 케이스 없음 · 차단만 남음 · 시간 초과. v2.1d: LLM 호출
전 ``_library_hints``(저장소 버전 사실), 적용 뒤 ``_read_back``(메이저
업그레이드 → 운영자 확인; 되돌리거나 차단하지 않음). v2.1i ``fix.mode: edits``: 스크립트를 만들지도 저장·재사용·
보강하지도 않는다 — 케이스마다 LLM 에 edits 만 묻고(앞 시도들은 ``FixContext.previous_attempts`` +
``previous_edits``), 출처 ``llm-edit``, 꼬리표 ``edits:<case>#<n>``, ``1 + script.max_augments_per_case`` 회 뒤 차단.
v2.1l: 의존성 버전은 LLM 이 추측하지 않는다 — 적용된 시도의 pom 에서 버전 없는 의존성은 커밋 전에
``_fill_versions`` 가 채우고(``VersionResolver``: 새 부모 체인 관리 → as-is 빌드 → 방금 추가된 것만 저장소),
``pom_invalid|missing_version`` 케이스는 LLM 전에 sbmig 가 한 번 고친다(출처 ``sbmig-version``, LLM 0회). v2.1m:
리소스 / war 플러그인이 필터링되는 바이너리 파일에서 실패한 케이스도 같은 자리에서 LLM 없이
``nonFilteredFileExtensions`` 를 적는다(출처 ``sbmig-resources``, ``sbmig.restructure.resources_filtering``).

The build-fix loop (ARCHITECTURE §3) — v2.1 (2026-10-27, docs/v2/PLAN-v2.1.md §3 / §3.1 / §3.2).

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
case and run: 1 stored + `script.max_augments_per_case` (3) LLM tries, then BLOCKED (repeated_failure). When an
answer's first choice does not apply (the edits fail / change nothing, or its script fails), the other half of the
same answer is applied instead. Edits go through `sbmig.script.apply_edits` (the script helper API's guards); changes
are committed `fix(script:<case>:v<N> <mode>)` / `fix(edits:<case> <source>)`.

Every try is one `FixTry` of the run's error list (`sbmig.loop.entries.RunErrors`: begin_try when its source is
known, finish_try with 원인 / 해결 / applied / files / 수정 notes / diff / commit / result, again after scoring; resolve
on improved / build_ok; block for a blocked case). v2.1b: the 수정 notes (`FixTry.edits`, `sbmig.loop.notes`) describe
every modification of a committed change — the LLM's `desc` / `<changes>` with the lines from the diff, a note made
from the diff for whatever the LLM did not describe (a reused stored script: the diff only) — and each is one INFO
line `수정: <path> <lines> — <change>` (the diff itself stays at DEBUG). Every log line carries the stage
(`sbmig.core.stage`): `fix r3` for the round, `fix r3 7a52b1cea8` for a case's work in it.

Kept from v2 / round 10: the cases of a round come root causes first (`sbmig.triage.cluster.CAUSE_RANK`) and a case
whose error files are ALL error files of cases already worked on in this round is **deferred**, as is a compile case
whose module's pom.xml a fix of this round changed (`_deferral`); **rollback before the next try** of a case (reset
when it is HEAD, else `git revert`) unless that try was improved / build_ok — the errors it had removed come back
into the case and into the next scoring's baseline; scoring (`_score`): "not visible" is not "resolved" —
`improved` only when every targeted error is gone AND the build reached their stage, `inconclusive` when the build
cannot tell (no KB credit), `partial` = some gone and nothing new in the touched files; structural kinds after a pom
edit revert that try; blocked clusters (run-wide `BlockedRegistry`), stall / oscillation, the LLM cool-down (L5) and
the configuration-error stop, library-advisory hints and the round-9 merge-conflict reference copies (never a patch
target).

v2.1d (dependency version policy, docs/INTERFACES.md "v2.1d"): before every LLM call `_library_hints` puts the facts
of the libraries behind the case into `FixContext.library_hints` (the table entry `<kb>/libraries.yml` + shipped, and
what the repository has: same-major releases with jakarta ✓ / javax, the higher majors; INFO `의존성 버전 조회: …`);
after every applied try whose commit touched a pom, `_read_back` lists its major upgrades / library replacements
(`FixAttempt.major_upgrades`, `FixTry.major_upgrades`) with the 운영자 확인 guide (`<verify>` → one `verify_guide` LLM
call → sbmig's synthesized one; WARNING `메이저 업그레이드: … — 운영자 확인 필요`). Nothing is reverted or blocked for
it; every call into `sbmig.build.depchanges` / `repo_versions` / `jakarta_libraries` is best effort.

v2.1i `fix.mode: edits` (docs/INTERFACES.md "v2.1i", the token comparison): no fix script is generated, stored,
reused, credited or augmented. Every try of a case is a fresh LLM ask (`script_mode` new, `FixContext.fix_mode`
edits, no `script_base`) that carries the case's earlier tries of this run (`previous_attempts` + `previous_edits`:
result, why it failed, whether it is still in the tree, the applied diff / the edits that did not apply); the answer's
edits are applied (`llm-edit`, commit `fix(edits:<case>#<n>): <cluster>`), a script in the answer is ignored. The
budget is `fix.edits_max_asks_per_case` asks per case and run (0 = no cap, `sbmig.triage.scripts.edits_llm_limit`), then
BLOCKED `repeated_failure`; scoring, rollback, the blocked policy and the error list are unchanged.

v2.1l (docs/INTERFACES.md "v2.1l", dependency versions carried over from the as-is build, never guessed by the LLM):
`VersionResolver` is the one resolution order (the new parent chain manages it → empty; the as-is build's version;
for a dependency the LLM just added, the repository's newest release; else unknown). `_fill_versions` runs after an
applied script / edits and before the commit, on the poms the try changed; a `pom_invalid|missing_version` case is
first tried without the LLM (`_version_attempt`: source `sbmig-version`, commit `fix(sbmig-version): <case>`, no
stored script, not a ladder step — `_previous(ladder=True)` leaves it out; a partial one is not rolled back); the
"Dependency versions" prompt section starts with the `as-is build:` / `new parent chain:` facts (`_version_facts`).

v2.1m (docs/INTERFACES.md "v2.1m", binary files under resource filtering): a second zero-LLM pre-try beside
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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import FixContext, TriageContext
from sbmig.core.models import (GAV, AdvisoryFinding, BlockReason, BuildError, BuildResult, DecisionKind, ErrorCluster,
                               ErrorKind, FileChange, FixAttempt, IterationRecord, KBMatch, LLMUsage, MergeConflict,
                               Phase,
                               PhaseResult, ProjectProfile, ScriptBase, ScriptRunResult, TriageDecision, utcnow)
from sbmig.core.workspace import Workspace

from sbmig.core.stage import stage

from .blocked_registry import BlockedRegistry, attribute, compose_suggestion, sanitize_errors
from .budget import LoopBudget
from .components import LazyFixer, ScriptFixerMissing, profile_summary
from .entries import CaseScripts, RunErrors, script_path
from .notes import (answer_notes, fallback_guide, log_major_upgrades, note_line, operator_changes, pom_changed,
                    table_entry, try_notes)

log = logging.getLogger("sbmig.loop")

TEST_COMPILING_GOALS = ("test-compile", "package", "verify", "install", "deploy", "test")
TEST_RUNNING_GOALS = ("test", "package", "verify", "install", "deploy")
RESOLUTION_PREFIXES = ("parent_resolution:", "pom_invalid:", "dependency_resolution:", "plugin_resolution:")
# LLM 전송·설정 실패(게이트웨이·네트워크·키 문제)와 모델이 형식을 어긴 답을 가른다. 전송 실패만 쿨다운 / "LLM
# 불가" 로 센다. 예전 목록은 ``config`` 같은 단어·경로·``line
# 512`` 까지 걸려서 Windows 파일 잠금 오류가 LLM 불가로 찍혔다.
# fixer analysis of a transport/config failure (vs. a protocol-garbage response, which is the model's fault). Round 10:
# only explicit markers — the HTTP status the client reports (`status=<code>`: auth, endpoint 404, timeouts, rate
# limit after the retries, 5xx; `status=None` = no HTTP answer at all), the transport exception (`cause=<Class>` or
# the class name itself) and the known config phrases. The old list matched `config`, bare 5xx-looking numbers
# (`line 512`) and paths, so a Windows `PermissionError [WinError 32] … config …` marked the LLM unavailable.
_LLM_TRANSPORT_RE = re.compile(
    r"(?i)\bstatus=(?:none|40[134]|408|429|5\d\d)\b|\bcause=[A-Za-z_][\w.]*|response_path|invalid json|"
    r"cannot find the reply text|not found in the response|api[_ ]?key\b[^.]*\bnot set|needs an api key|"
    r"\bllm[._][\w.]*\s+is not set|unknown llm\.provider|cannot be imported")
_LLM_TRANSPORT_CLASS_RE = re.compile(      # case-sensitive: exception class names, not the English words
    r"\b(?:ConnectionError|ConnectTimeout|ConnectTimeoutError|ConnectError|ReadTimeout|ReadTimeoutError|Timeout|"
    r"TimeoutError|TimeoutException|WriteTimeout|PoolTimeout|ProxyError|SSLError|TransportError|RemoteProtocolError|"
    r"RemoteDisconnected|ChunkedEncodingError|ConnectionResetError|ConnectionRefusedError|ConnectionAbortedError|"
    r"NewConnectionError|MaxRetryError|NameResolutionError|LLMHardDeadline|LLMConfigError|MissingComponentError|"
    r"PluginLoadError)\b")
# 게이트웨이가 *이 요청의 내용* 을 거절한 것(너무 김, 형식 오류, 토큰 한도) — 프롬프트 탓이지 전송 탓이 아니다
# the gateway rejected *this request* for its content (too long, malformed, cut by the token limit): the prompt's
# fault, not the wire's — such a failure must not count towards "LLM unavailable for the run"
_LLM_REQUEST_REJECTED_RE = re.compile(r"status=4(?:00|13|22)\b|content[ _-]?filter|"
                                      r"finish_reason=(?:length|max_tokens)\b")
# a 400 that says the prompt is too big: different prompts may well get the same text — never a configuration error
_LLM_PROMPT_TOO_BIG_RE = re.compile(r"(?i)context[ _-]?length|too (?:long|large|many tokens)|"
                                    r"max(?:imum)?[ _-]?(?:context|tokens?|input)|token limit|payload too large|"
                                    r"finish_reason=(?:length|max_tokens)")
_LLM_400_BODY_RE = re.compile(r"status=400\)?:?\s*(?P<body>.*)", re.S)
# 서로 다른 프롬프트 2개가 같은 HTTP 400 본문을 받으면 설정 오류(모델 이름·필드) → 그 실행의 LLM 호출 중단
LLM_CONFIG_ERROR_PROMPTS = 2         # the same HTTP 400 body for this many different prompts → configuration error
_BLOCKED_RAT_RE = re.compile(r"blocked:\s*(?P<r>[a-z_]+)")
REFERENCE_MAX_CHARS = 12_000         # one dropped merge copy in the fix prompt (round 9)
# 감시견: 회사 Windows PC 에는 SIGUSR1 이 없어, 오래 조용하면 스스로 경고하고 스레드 덤프를 남긴다
WATCHDOG_S = 30.0                    # the fix loop's watchdog wakes up this often …
WATCHDOG_QUIET_S = 60.0              # … and warns when no milestone was reached for this long (operators: stuck?)
WATCHDOG_DUMP_S = 300.0              # … and writes a thread dump into the run dir by itself after this long (Windows has no SIGUSR1)
REFERENCE_MAX_FILES = 3              # dropped copies per cluster
_TRUNCATED_MARK = "\n… (truncated)"
# 회사 규칙 "개선되지 않으면 되돌린다": 개선·빌드 성공만 트리에 남긴다. NO_ROLLBACK 의 나머지는 이미 트리에
# 없거나 (되돌림·적용 실패·차단) 아직 판정 전(pending)이라 되돌릴 것이 없다
SCRIPT_KEEP = frozenset({"improved", "build_ok"})   # a case's previous script attempt is kept (not rolled back)
NO_ROLLBACK = frozenset({"improved", "build_ok", "reverted", "pending", "apply_failed", "blocked"})
SCRIPT_BASE_LOG = 50                 # ScriptBase.log lines (augment prompt)
SCRIPT_HISTORY = 10                  # ScriptBase.history lines
FIXER_WITHOUT_SCRIPTS_FLAG = "fixer-without-scripts"
# 이 메모가 붙은 시도는 최종 트리에 없다 → 운영자 확인(메이저 업그레이드) 목록에서도 뺀다
ROLLED_BACK_NOTE = "rolled back before "   # the note on a try the next try of its case rolled back (not in the tree)
try:    # one definition each, shared with the error list: the runner's stand-in errors of a crashed build and the
    # Maven stage at which an error kind is reported ("not visible" is not "resolved", round 10)
    from sbmig.errors.entries import STAGE, error_stage
    from sbmig.errors.entries import SYNTHETIC_PREFIXES as _SYNTHETIC_PREFIXES
except ImportError:  # pragma: no cover - the error list package is part of sbmig (E1)
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
        """(대체 정의) 이 종류의 오류를 Maven 이 보고하는 빌드 단계 번호(작을수록 이른 단계)."""
        if e.kind == ErrorKind.PLUGIN_EXECUTION:
            return 2 if "resources" in (e.goal or "").lower() else 5
        return STAGE.get(e.kind)
TRY_NOTE_CHARS = 600                 # FixTry.note: the try's failure / rollback / scoring notes, joined
NOTES_LOGGED = 20                    # `수정:` INFO lines per applied change (the rest: errors.md / errors.jsonl)
PREVIOUS_EDIT_CHARS = 4000           # v2.1i edits mode: one earlier try in FixContext.previous_edits (diff / edits)
PREVIOUS_EDIT_TEXT = 300             # … one old / new / content text of an edit that did not apply
EDITS_SCRIPT_IGNORED = "the answer's script was ignored (fix.mode edits: no fix scripts)"


def fmt_error(e: BuildError) -> str:
    """오류 한 줄 ``파일:줄 메시지`` — 보강 프롬프트의 남은 오류 / 새로 생긴 오류 목록에 쓴다.

    'file:line message' (module or '-' without a file) — remaining / introduced errors in an augment prompt."""
    loc = e.file or e.module or "-"
    if e.file and e.line:
        loc += f":{e.line}"
    msg = " ".join((e.message or e.raw or "").split())
    return f"{loc} {msg[:300]}".strip()



def _status_paths(ws: Any) -> set[str]:
    fn = getattr(ws, "status_paths", None)
    try:
        return set(fn()) if callable(fn) else set()
    except Exception:  # noqa: BLE001 - a workspace fake / git trouble
        return set()


def _restore_build_byproducts(ws: Any, before: set[str]) -> list[str]:
    """v2.1b: 빌드가 target/ 밖에 쓴 파일(lib/ 에 복사된 jar 등)을 빌드 직후 치운다 — 시도의 커밋과 파일
    목록에 수정이 바꾼 것만 남게 하려는 것.

    v2.1b: files a build wrote outside target/ (a jar copied into lib/) are set aside right after the build, so
    an attempt's commit and `files` hold only what the fix changed (sbmig.core.workspace.restore_build_byproducts)."""
    fn = getattr(ws, "restore_build_byproducts", None)
    if not callable(fn):
        return []
    try:
        return list(fn(before) or [])
    except Exception as e:  # noqa: BLE001 - never fail a build over the bookkeeping
        log.debug("build byproducts not restored: %s", e)
        return []

def sleep_seconds(seconds: float) -> None:
    """LLM 쿨다운 대기(모듈 수준 함수라 테스트 conftest 가 모든 루프에서 한 번에 바꿔 끼울 수 있다).

    The LLM cool-down pause (module-level so a test conftest can replace it for every loop)."""
    time.sleep(seconds)


def _first_message(c: ErrorCluster, limit: int = 200) -> str:
    """The case's first error message (`symbol …` appended when the message lacks it) for the case header line."""
    if not c.errors:
        return "-"
    e = c.errors[0]
    text = (e.message or e.raw or "").strip()
    if e.symbol and e.symbol not in text:
        text += f" (symbol {e.symbol})"
    text = " ".join(text.split()) or "-"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _first_sentence(text: str, limit: int = 160) -> str:
    """The first sentence of an LLM text (원인 / 해결 / no-script reason, or the llm-error text), one line."""
    t = " ".join((text or "").strip().split())
    for sep in (". ", "。", "\n"):
        if sep in t:
            t = t.split(sep, 1)[0] + ("." if sep == ". " else "")
            break
    return t if len(t) <= limit else t[:limit - 1] + "…"


def case_stage(round_no: int, c: ErrorCluster) -> str:
    """한 라운드 안에서 한 케이스를 다루는 로그 단계 태그(``fix r3 7a52b1cea8``).

    'fix r3 7a52b1cea8' — the log stage of a case's work in a fix round (§3.2)."""
    cid = c.case_id or (c.id[len("case:"):] if c.id.startswith("case:") else c.id)
    return f"fix r{round_no} {cid}"


def _case_header(c: ErrorCluster) -> str:
    """'4 error(s) in 2 file(s): package jakarta.xml.bind does not exist' (§3.2)."""
    files = len({(e.file or "").replace("\\", "/") for e in c.errors if e.file})
    where = f" in {files} file(s)" if files else (f" in {c.module}" if c.module else "")
    return f"{len(c.errors)} error(s){where}: {_first_message(c)}"


def _class_of(c: ErrorCluster) -> str:
    """The cause class of a case for the round header ('package_does_not_exist', 'dependency_resolution', …)."""
    try:
        from sbmig.triage.cluster import error_class
        cls = next((error_class(e) for e in c.errors if error_class(e)), "")
    except Exception:  # pragma: no cover - triage is ours
        cls = ""
    return cls or c.kind.value


def is_llm_transport_error(analysis: str) -> bool:
    """``llm-error: …`` 가 전송·설정 실패인가(쿨다운 / "LLM 불가" 로 센다). 형식을 어긴 답과 내용 때문에
    거절된 요청은 아니다 — 그건 프롬프트 탓이다.

    `llm-error: …` of a transport / configuration failure (counts towards the cool-down / "LLM unavailable")."""
    a = (analysis or "").strip()
    if not a.lower().startswith("llm-error:"):
        return False
    if "did not follow the protocol" in a:
        return False
    if _LLM_REQUEST_REJECTED_RE.search(a):
        return False
    return bool(_LLM_TRANSPORT_RE.search(a) or _LLM_TRANSPORT_CLASS_RE.search(a))


def llm_400_body(analysis: str) -> Optional[str]:
    """HTTP 400 ``llm-error`` 의 짧은 본문(200자 이하) 또는 None. 프롬프트가 너무 크다는 400 도 None — 큰
    프롬프트 둘이 같은 글을 받는 것은 정상이므로 설정 오류의 근거가 되면 안 된다.

    The short body of an HTTP 400 `llm-error` (whitespace collapsed, ≤ 200 chars), or None — None as well for a
    400 that says the prompt is too big (two big prompts legitimately get the same text)."""
    a = (analysis or "").strip()
    if not a.lower().startswith("llm-error:") or "status=400" not in a or _LLM_PROMPT_TOO_BIG_RE.search(a):
        return None
    m = _LLM_400_BODY_RE.search(a)
    body = " ".join((m.group("body") if m else "").split())
    body = body.split(" [sbmig] ", 1)[0]
    return body[:200] or "(empty body)"


def build_target_args(profile: Optional[ProjectProfile], target: Optional[str]) -> tuple[Optional[str], list[str]]:
    """``target`` 을 빌드할 (모듈, 추가 인자). 루트 aggregator 가 그 모듈을 나열하면 루트에서
    ``-pl <target> -am`` 으로 — 저장소 안 공유 라이브러리도 함께 빌드되고 오류 경로가 루트 기준으로 유지된다.
    아니면 그 디렉토리 안에서 빌드.

    (module, extra_args) for building `target`. When a root aggregator pom lists the target among its modules, build
    from the root with `-pl <target> -am` (in-repo library roots get built too, error paths stay root-relative);
    otherwise build inside the target directory."""
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
    """The case id of a cluster without one (a v1-style cluster: the first error's case)."""
    if not errors:
        return ""
    try:
        from sbmig.triage.cluster import case_id_of
        return case_id_of(errors[0])
    except Exception:  # pragma: no cover - triage is ours
        return ""


def _reduced(c: ErrorCluster, errors: list[BuildError]) -> ErrorCluster:
    """같은 클러스터를 주어진 오류들로 줄인(또는 되살린 오류로 늘린) 사본 — 서명 목록도 맞춘다."""
    sigs: list[str] = []
    for e in errors:
        s = e.signature or f"{e.kind.value}|{e.message}"
        if s not in sigs:
            sigs.append(s)
    return c.model_copy(update={"errors": list(errors), "signatures": sigs})


@dataclass
class _Round:
    """수정 라운드 하나의 상태. ``covered`` / ``poms`` 는 이번 라운드에 수정이 돈 오류 파일·바뀐 pom →
    케이스 ID 로, 같은 파일·모듈의 다른 케이스를 다음 빌드까지 미루는(deferral) 근거다. ``restored`` 는
    롤백이 되살린 오류(다음 채점 기준선)."""
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
    restored: list = field(default_factory=list)   # errors this round's rollbacks brought back (next baseline)
    covered: dict = field(default_factory=dict)    # error file → case id whose fix ran in this round
    poms: dict = field(default_factory=dict)       # pom.xml path → case id whose fix changed it in this round
    round: int = 1                                 # the fix round (log stage `fix r<round>`, FixTry.round)


@dataclass
class _Pending:
    """채점을 기다리는 적용된 시도와 그 커밋 범위(되돌리기에 쓰는 pre / post sha)."""
    attempt: FixAttempt
    pre_sha: str
    post_sha: str


def _label(c: ErrorCluster) -> str:
    try:
        from sbmig.triage.cluster import cluster_label
        return cluster_label(c)
    except Exception:  # pragma: no cover - triage is ours, but stay defensive
        return c.id


def _script_name(a: FixAttempt) -> str:
    """'script abc1234567:v2 (augment)' — who made a script attempt (hints, logs)."""
    return f"script {a.script_case_id or '?'}:v{a.script_version} ({a.script_mode or '?'})"


def _related(cluster: ErrorCluster, attempts: list[FixAttempt]) -> list[FixAttempt]:
    """같은 클러스터 id 이거나 서명을 하나라도 공유하는 시도들(다른
    파일 포함). 힌트 재료일 뿐, 시도 한도에는 쓰지 않는다.

    Same cluster id or any shared signature (other files too): hint material, never a limit (round 10)."""
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def _same_cluster(cluster: ErrorCluster, attempts: list[FixAttempt],
                  canon: Optional[Callable[[Optional[str]], str]] = None) -> list[FixAttempt]:
    """바로 *이* 클러스터에 대한 시도들(같은 id, 또는 옮겨졌을
    수 있는 같은 파일 + 겹치는 서명). 사다리·한도·롤백의 근거.

    The attempts made on THIS cluster (same id, or same — possibly moved — file with an overlapping signature)."""
    from sbmig.triage.decide import same_cluster_attempts
    return same_cluster_attempts(cluster, attempts, canon)


def call_edit_applier(fn: Callable[..., Any], ws: Any, edits: list, cfg: Any, errors: list) -> Any:
    """플러그인 edit applier 호출(A4, 2026-10-28): 문서화된 모양은 ``fn(ws, edits, *, cfg, errors=None)`` 이고,
    ``errors``(이번 케이스의 오류 목록)는 시그니처가 받을 때만 넘긴다(``**kwargs`` 면 넘김). 시그니처를 볼 수 없는
    callable 이면 예전처럼 넘긴다. 문서대로 ``errors`` 없이 만든 회사 플러그인도 TypeError 없이 돈다.

    Call a plugin edit applier `fn(ws, edits, *, cfg, errors=None)`: `errors` (the case's errors) only when the
    signature takes it (or `**kwargs`); a callable whose signature does not inspect gets it as before (A4)."""
    from sbmig.loop.entries import _accepted
    opt = _accepted(fn, (ws, edits), {"cfg": cfg}, {"errors": errors})
    kw = {"errors": errors} if opt is None else opt
    return fn(ws, edits, cfg=cfg, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# v2.1l dependency versions (docs/INTERFACES.md "v2.1l"): carried over from the as-is build, never guessed by the LLM
# ---------------------------------------------------------------------------------------------------------------------
MISSING_VERSION_SIG = "pom_invalid|missing_version"   # the generic signature of "'…version' for g:a is missing"
SBMIG_VERSION_SOURCE = "sbmig-version"               # FixAttempt.source of the deterministic missing-version try
SBMIG_RESOURCES_SOURCE = "sbmig-resources"           # v2.1m: … of the nonFilteredFileExtensions try
RESOURCE_FACTS_FILES = 10                             # binary files named in the v2.1m prompt facts
PRE_TRY_SOURCES = frozenset({SBMIG_VERSION_SOURCE, SBMIG_RESOURCES_SOURCE})   # zero-LLM pre-tries: not ladder steps
VERSION_FACTS_MAX = 12                                # artifacts per case in the "as-is build: …" prompt facts
VERSION_TRY_APPLY_FAILURES = 3                        # zero-LLM tries of a (pom, g:a) that may fail to apply
PRE_TRY_APPLY_FAILURES = VERSION_TRY_APPLY_FAILURES   # … the same cap for every zero-LLM pre-try (v2.1m)


@dataclass(frozen=True)
class _PreTry:
    """LLM 0회 사전 시도 한 종류(v2.1l ``sbmig-version``, v2.1m ``sbmig-resources``): 이 케이스가 해당하는지와 손댈
    pom(미루기 판단용; None = 해당 없음), 그리고 실행(적용되면 True).

    One zero-LLM pre-try kind: `targets(c)` → the poms it would edit (the deferral check; None = not this kind),
    `run(pr, st, c)` → True when it applied (the case's try of the round)."""
    source: str
    targets: Callable[[ErrorCluster], list[str] | None]
    run: Callable[..., bool]
VERSION_SOURCE_TEXT = {"asis": "as-is 빌드의 버전", "repository": "저장소의 최신 릴리스"}      # log lines / edit desc
VERSION_SOURCE_NOTE = {"asis": "the as-is build", "repository": "newest release in the repository"}   # att.notes


class VersionResolver:
    """v2.1l 버전 결정 규칙 하나(재구성·수정 루프 공통, INTERFACES "Resolution order"): 버전 없는 의존성 g:a 에
    대해 (1) 새 부모 체인(재구성 뒤 effective pom, ``depmgmt-after-restructure.json``)이 관리하면 그대로 둔다
    → ``(None, "managed")``, (2) as-is 빌드가 쓴 버전(``logs/depmgmt.json``: 그 모듈의 direct → managed → 합친
    managed → 합친 direct) → ``(v, "asis")``, (3) 호출자가 허락할 때만(LLM 이 방금 추가한 의존성, 누락 버전
    케이스) 저장소의 최신 정식 릴리스(프리릴리스 제외) → ``(v, "repository")``, (4) 못 찾음 → ``(None,
    "unknown")``. LLM 에게 버전을 추측시키지 않으려는 것 — 사실(as-is·저장소)만 쓴다. 예외를 내지 않는다
    (저장소 실패 = 못 찾음). 실행마다 수정 루프가 하나 만들어 캐시한다.

    The v2.1l resolution order for a `<dependencies>` entry without `<version>` (one rule for the restructure and
    the fix loop): (1) the new parent chain manages it (the post-restructure capture; the declaring module's
    `by_module` entry decides when the capture has one, else the merged `managed`) → `(None, "managed")`; (2) the
    as-is build resolved it (`by_module[...].direct` → `.managed` → merged `managed` → merged `direct`) →
    `(v, "asis")`; (3) only with `allow_repository` (a dependency the LLM just added, a missing-version case): the
    newest release `RepositoryLookup.versions` lists that `is_prerelease` does not reject → `(v, "repository")`;
    (4) `(None, "unknown")`. `asis` / `after` are depmgmt dicts (`sbmig.build.effective.load_effective`);
    `repository` a zero-arg callable returning the run's `RepositoryLookup` (or None). Never raises."""

    def __init__(self, asis: dict | None = None, after: dict | None = None,
                 repository: Callable[[], Any] | None = None):
        """두 캡처(dict, 없으면 빈 것)와 저장소 조회기를 주는 콜러블을 받는다."""
        self.asis: dict = asis if isinstance(asis, dict) else {}
        self.after: dict = after if isinstance(after, dict) else {}
        self._repository = repository
        self._repo_cache: dict[str, str | None] = {}

    @classmethod
    def for_workspace(cls, ws: Any, repository: Callable[[], Any] | None = None) -> VersionResolver:
        """실행 디렉토리의 두 캡처(as-is ``depmgmt.json``, 재구성 뒤 ``depmgmt-after-restructure.json``)로 만든다.
        못 읽으면 빈 캡처 — 규칙은 저장소 단계만 남는다.

        Built from the run directory's two captures (missing / unreadable → empty)."""
        asis: dict | None = None
        after: dict | None = None
        try:
            from sbmig.build.effective import AFTER_RESTRUCTURE_FILE, load_effective
            asis = load_effective(ws)
            after = load_effective(ws, name=AFTER_RESTRUCTURE_FILE)
        except Exception as e:  # noqa: BLE001 - no captures: only the repository step is left
            log.debug("dependency version captures unavailable: %s: %s", type(e).__name__, e)
        return cls(asis, after, repository)

    @staticmethod
    def _has(data: dict) -> bool:
        return bool(data.get("managed") or data.get("direct") or data.get("by_module"))

    @property
    def has_asis(self) -> bool:
        """as-is 캡처가 있는가(비어 있지 않은가)."""
        return self._has(self.asis)

    @property
    def has_after(self) -> bool:
        """재구성 뒤 캡처(새 부모 체인)가 있는가. 없으면 '관리되는가' 를 알 수 없다."""
        return self._has(self.after)

    @staticmethod
    def _module(data: dict, module: str | None) -> dict | None:
        """캡처의 ``by_module`` 에서 모듈 항목(g:a 로, 없으면 artifactId 로). 없으면 None."""
        if not module:
            return None
        by = data.get("by_module") or {}
        own = by.get(module)
        if own is None:
            aid = module.split(":")[-1]
            own = next((v for k, v in by.items() if aid and str(k).split(":")[-1] == aid), None)
        return own if isinstance(own, dict) else None

    def managed_version(self, key: str, module: str | None = None) -> str | None:
        """새 부모 체인이 g:a 를 관리하는 버전(없으면 None). 선언한 모듈의 항목이 캡처에 있으면 그 모듈의
        effective dependencyManagement 가 정답이다 — 다른 모듈(예: boot 모듈의 Boot BOM)이 관리한다고 이
        모듈도 관리되는 것은 아니다. 모듈 항목이 없을 때만 합친 ``managed``.

        The version the new chain manages g:a at: the declaring module's effective dependencyManagement when the
        capture has the module (another module's chain does not manage this one), else the merged `managed`."""
        own = self._module(self.after, module)
        if own is not None:
            return (own.get("managed") or {}).get(key) or None
        return (self.after.get("managed") or {}).get(key) or None

    def asis_fact(self, key: str, module: str | None = None) -> tuple[str | None, str]:
        """as-is 빌드가 g:a 에 쓴 버전과 출처 문구: ``(v, "declared")`` / ``(v, "managed by g:a:v")`` /
        ``(None, "")``. 순서: 모듈 direct → 모듈 managed → 합친 managed → 합친 direct — 버전 없는 선언은 관리로
        풀렸으므로, 모듈 밖에서는 다른 모듈이 명시한 버전보다 부모가 관리한 버전이 맞다. 문구는 찾은 표를 그대로
        말한다(direct = declared, managed = managed by 부모; 섞지 않는다).

        (version, how) of the as-is build in the order: the module's `direct` → its `managed` → merged `managed` →
        merged `direct` (a version-less declaration was resolved by management: outside the module, the managed
        version beats another module's explicit one). `how` names the map the hit came from: "declared" for a
        `direct` map, "managed by <parent g:a:v>" for a `managed` one — never crossed."""
        own = self._module(self.asis, module)
        parent_of_module = (own.get("parent") if own else None) or self.asis.get("parent")
        maps: list[tuple[dict, str | None]] = []       # (map, the managing parent; None = a direct map)
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
        """저장소가 가진 g:a 의 최신 정식 릴리스(프리릴리스 제외). 조회기 없음·실패 → None. 키별 캐시.

        The newest release of g:a the repository lists (`RepositoryLookup.versions`, oldest → newest) that is not a
        prerelease; None without a lookup or on any failure. Cached per key."""
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
        except Exception as e:  # noqa: BLE001 - a repository failure counts as "unknown"
            log.debug("repository versions of %s unavailable: %s: %s", key, type(e).__name__, e)
            found = None
        self._repo_cache[key] = found
        return found

    def resolve(self, key: str, module: str | None = None, *, allow_repository: bool = False,
                check_managed: bool = True) -> tuple[str | None, str]:
        """규칙대로 ``(버전 | None, 출처)``: managed / asis / repository / unknown. ``check_managed=False`` 는
        빌드가 이미 '버전 없음' 이라고 말한 의존성(1단계를 건너뜀). 예외 없음.

        `(version | None, source)` by the resolution order; `check_managed=False` skips step 1 (the build already
        said the version is missing). Never raises."""
        try:
            if check_managed and self.managed_version(key, module):
                return None, "managed"
            v, _how = self.asis_fact(key, module)
            if v:
                return v, "asis"
            if allow_repository:
                v = self.repository_version(key)
                if v:
                    return v, "repository"
        except Exception as e:  # noqa: BLE001 - never raises: unknown
            log.debug("version of %s not resolved: %s: %s", key, type(e).__name__, e)
        return None, "unknown"


class BuildFixLoop:
    """v2.1 수정 루프. 오케스트레이터가 실행마다 하나 만들고(``budget`` / ``blocked`` / ``errors`` 공유),
    최종 빌드 실패 시 ``run`` 을 부른다. 케이스의 다음 사다리 단계는 트리아지(``sbmig.triage.scripts``)가,
    시도 실행·채점·되돌리기·KB 실적은 이 클래스가 맡는다. KB 가
    없으면 LLM 스크립트는 저장 없이 v0 로 돌고 재사용은 없다.

    v2.1 fix loop. `script_runner` = ScriptRunnerLike (the orchestrator passes a lazy `sbmig.script.ScriptRunner
    (cfg)`); `edit_applier` = `fn(ws, edits, *, cfg, errors=None) -> ScriptRunResult` (None = `sbmig.script.apply_edits`;
    `errors` = the case's errors, passed only when the signature takes it — A4, 2026-10-28); the KB's
    case store is `kb.cases` (None = no KB: LLM scripts run unstored as v0, nothing is reused); `errors` = the run's
    error list (`RunErrors`, set by the orchestrator; inactive by default)."""

    def __init__(self, cfg: Config, ws: Workspace, build_runner: Any, triager: Any, fixer: Any, kb: Any,
                 profile: ProjectProfile, plan_notes: Optional[list[str]] = None,
                 budget: Optional[LoopBudget] = None, *, dry_run: bool = False,
                 history: Optional[list[FixAttempt]] = None, blocked: Optional[BlockedRegistry] = None,
                 locator: Any = None, advisories: Optional[list[AdvisoryFinding]] = None,
                 script_runner: Any = None, edit_applier: Optional[Callable[..., ScriptRunResult]] = None,
                 errors: Optional[RunErrors] = None):
        """협력 객체와 실행 동안의 상태(시도 이력·차단 등록부·플래그·후속 조치·LLM 장애 상태·시도별 기록)를
        준비한다. ``history`` 는 같은 실행의 모든 루프가 공유할 수 있다 — 사다리와 한도는 이 이력을 본다."""
        self.cfg = cfg
        self.ws = ws
        self.build_runner = build_runner
        self.triager = triager
        self.fixer = fixer
        self.kb = kb
        self.script_runner = script_runner
        self.edit_applier = edit_applier
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
        self.tests_skipped = False               # round 8: only runtime test failures left → packaged with -DskipTests
        # round 10: per build target ("" = the whole project) — one MSA service's fallback must not change how the
        # other services' final builds run (`skips_for`)
        self.test_compile_skipped_targets: set[str] = set()
        self.tests_skipped_targets: set[str] = set()
        self._profile_summary: Optional[str] = None
        # run-wide blocked state (shared by every phase of the run through the orchestrator)
        self.blocked = blocked if blocked is not None else BlockedRegistry()
        self.locator = locator                   # owner class → artifact (sbmig.build.classpath); None = no attribution
        # library advisory findings of the run (round 6): matched per cluster → FixContext.advisory_hints
        self.advisories: list[AdvisoryFinding] = list(advisories or [])
        self.llm_unavailable: Optional[str] = None
        self.llm_config_error: Optional[str] = None   # round 10: the same HTTP 400 for different prompts (the short body)
        self._http400: dict[str, set[str]] = {}  # short 400 body → prompt hashes (cluster ids when a fixer sets none)
        self.aborted: Optional[str] = None       # set when the run must stop (configured parent not resolvable)
        self._llm_streak: list[tuple[ErrorCluster, str]] = []
        self._milestone = "idle"                 # last point the loop passed (watchdog, round 9c)
        self._milestone_at = time.monotonic()
        self._busy: Optional[str] = None         # a Maven build / OpenRewrite run of the loop in progress (watchdog)
        self._quiet_from = 0.0                   # the watchdog last saw such a call (or an LLM call) in progress
        self._streak_attempts: list[str] = []    # attempt ids of the current transport-failure streak
        self.llm_cooldown_used = False           # round 9 (L5): one cool-down per run
        self._cooled_attempts: set[str] = set()  # attempts of the streak that triggered the cool-down: not counted
        self._sleep: Optional[Callable[[float], None]] = None   # injectable (tests); None = module `sleep_seconds`
        # round 9: first-wins merge conflicts of the restructure step (set by the orchestrator) → fix-prompt hints
        self.merge_conflicts: list[MergeConflict] = []
        self._phase_items: list[str] = []
        self._cur_phase: Phase = Phase.JDK21
        # v2 fix scripts: scripts credited (attempt → outcome credited), attempts rolled back before the next try, the
        # errors the last fix round's rollbacks brought back (added to the next scoring's baseline)
        self._credited: dict[int, str] = {}
        self._rolled_back: set[int] = set()
        self._ref_paths: dict[int, set[str]] = {}   # attempt → merge-conflict reference labels shown to the LLM
        self._target: Optional[str] = None
        # v2.1: per try — the error list's (case id, try n, round); the applied diff; whether a stored script's run
        # decides its outcome (credit); the edits' apply result; how long the try took; scoring (gone, targeted)
        self._tries: dict[int, tuple[str, int, int]] = {}
        self._diffs: dict[int, str] = {}
        self._script_ran: dict[int, bool] = {}
        self._edit_results: dict[int, ScriptRunResult] = {}
        self._seconds: dict[int, float] = {}
        self._tally: dict[int, tuple[int, int]] = {}
        # 시도별 상태 사전은 FixAttempt 객체(id())가 키 — 시도 id 문자열은 MSA 서비스별 루프에서 겹칠 수 있다
        self._try_seq: dict[str, int] = {}       # case id → local try counter (an error list that returns no FixTry)
        self._answers: dict[int, Any] = {}       # v2.1b: attempt → the LLM answer it applied (its edit descriptions)
        self._notes: dict[int, list] = {}        # v2.1b: attempt → its 수정 notes (EditNote per change)
        self._match_hints: dict[str, list[str]] = {}   # cluster id → kb.match hint lines (the case's last solution)
        # v2.1d dependency version policy: the "Dependency versions" prompt lines per library (cached for the run),
        # the repository lookup (lazy; None + off after a failure), what the repository said per library
        self._library_hint_cache: dict[str, list[str]] = {}
        self._repo_lookup: Any = None
        self._repo_lookup_off = False
        self._repo_facts: dict[str, Any] = {}    # g:a → RepositoryVersions (the read-back's "same major offered")
        # v2.1l: 버전 결정기(처음 쓸 때 한 번), 누락 버전 케이스의 결정적 시도가 이미 본 (pom, g:a) 쌍(실행당 한 번)
        # the version resolver (lazy, one per loop); the (pom, g:a) pairs the zero-LLM missing-version try looked at
        self._version_resolver: VersionResolver | None = None
        # v2.1l / v2.1m: LLM 0회 사전 시도가 이미 본 키(출처, pom 또는 모듈, g:a | 확장자들)와 적용 실패 횟수
        # the zero-LLM pre-tries' keys already looked at — (source, pom or module, g:a | extensions) — and apply failures
        self._pre_tried: set[tuple[str, str, str]] = set()
        self._pre_failures: Counter = Counter()
        self._cases = CaseScripts.of(kb)
        # v2.1i: `fix.mode: edits` — LLM 직접 수정만(스크립트 생성·저장·재사용·실적 없음); script 모드는 그대로
        # (the same test as sbmig.triage.scripts.edits_mode — the loop imports triage lazily)
        self.edits_mode = str(getattr(getattr(cfg, "fix", None), "mode", "script") or "script") == "edits"
        attach = getattr(triager, "attach_run", None)
        if callable(attach):
            try:
                attach(blocked_registry=self.blocked, ws=ws, cases=self._cases if self._cases.active else None)
            except Exception as e:  # pragma: no cover - a foreign triager
                log.warning("triager.attach_run failed: %s", e)

    @property
    def cases(self) -> CaseScripts:
        """KB 케이스 저장소(``CaseScripts`` 어댑터, KB 가 없으면 비활성).

        The KB's case store (`kb.cases` through the `CaseScripts` adapter; inactive without a KB)."""
        return self._cases

    # ------------------------------------------------------------------ public
    def _mark(self, what: str) -> None:
        """감시견용 이정표: 루프가 지금 무엇을 하는지(sbmig.log 에 DEBUG 한 줄도).

        Milestone for the watchdog: what the loop is doing right now (also a DEBUG line in sbmig.log; the line's
        stage says where)."""
        self._milestone, self._milestone_at = what, time.monotonic()
        log.debug("→ %s", what)

    @contextmanager
    def _busy_with(self, what: str):
        """스스로 진행 신호를 남기는 호출(Maven 빌드 등) 동안
        감시견을 조용히 시키고, 끝나면 조용한 시간을 다시 잰다.

        A call that logs its own heartbeat (Maven build, OpenRewrite run): the watchdog stays silent meanwhile, and
        its quiet time starts again when the call returns."""
        prev, self._busy = self._busy, what
        try:
            yield
        finally:
            self._busy = prev
            self._quiet_from = time.monotonic()

    def _activity(self) -> Optional[str]:
        """지금 스스로 진행 신호를 내는 일이 도는가(이 루프의 빌드, LLM HTTP 호출, Maven 자식 프로세스) —
        없으면 None 이고 그때만 감시견이 말한다(긴 LLM 호출을 "멈춤" 으로 오인하지 않으려고).

        What is running right now that heartbeats by itself (round 10, A12): a build / rewrite of this loop, an LLM
        HTTP call (`sbmig.fix.client.llm_calls_in_flight`), a Maven child process (`sbmig.build.mvn.live_processes`,
        e.g. the classpath attribution) — None when nothing is (the watchdog then speaks)."""
        if self._busy:
            return self._busy
        try:
            from sbmig.fix.client import llm_calls_in_flight
            if llm_calls_in_flight():
                return "LLM call"
        except Exception:  # pragma: no cover - the fix package is ours; stay defensive
            pass
        try:
            from sbmig.build.mvn import live_processes
            if live_processes():
                return "Maven child process"
        except Exception:  # pragma: no cover
            pass
        return None

    def _start_watchdog(self, phase: Phase) -> threading.Event:
        """감시견 데몬 스레드: 루프가 ``WATCHDOG_QUIET_S`` 동안 조용하면 마지막 이정표를 WARNING 으로
        말하고, ``WATCHDOG_DUMP_S`` 가 지나면 스레드 덤프를 실행 디렉터리에 쓴다. 운영자가 "멈췄나?" 를
        sbmig.log 의 이 줄로 판단한다.

        A daemon thread that says, every WATCHDOG_S once the loop was quiet for WATCHDOG_QUIET_S, where the loop is
        (last milestone) — a slow LLM / build keeps logging through its own heartbeat, so a quiet watchdog line
        points at the code between those (hints, prompt packing, patch apply, git). Round 10: while such a call is in
        progress (`_activity`) the watchdog neither warns nor dumps; the quiet time counts from its end."""
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
                    except Exception as e:  # pragma: no cover - diagnostics never break the loop
                        log.warning("thread dump failed: %s", e)

        threading.Thread(target=contextvars.copy_context().run, args=(beat,), name="fixloop-watchdog",
                         daemon=True).start()
        return stop

    def run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None,
            first_build: Optional[BuildResult] = None) -> PhaseResult:
        """수정 루프 실행(감시견을 켠 채로). ``first_build`` = 루프를 시작하게 만든 실패한 최종
        ``clean package`` — 같은 트리를 다시 빌드하지 않고 빌드 1 로 쓴다(실패하는 프로젝트마다 Maven 빌드 한
        번 절약; 그 오류는 이미 목록에 있다).

        `first_build` (v2): the build that made the caller start the loop (the failed final `clean package`) —
        used as the loop's build 1 instead of building the same tree again (one Maven build less per failing
        project; the caller already put its errors on the error list)."""
        self._cur_phase = phase
        stop = self._start_watchdog(phase)
        try:
            return self._run(phase, goal, max_iterations, target, first_build)
        finally:
            stop.set()
            self._mark("idle")

    def _run(self, phase: Phase, goal: str, max_iterations: int, target: Optional[str] = None,
             first_build: Optional[BuildResult] = None) -> PhaseResult:
        """루프 본체. 바깥 반복 = 빌드 1번(채점 → 오류 목록 → 성공이면 끝 → 정체·진동 감지 → 종료 조건).
        안쪽 반복 = 수정 라운드; 아무것도 트리를 바꾸지 못했으면 같은 빌드로 각 케이스의 다음 사다리 단계를
        다시 묻는다(재라운드). 재라운드도 라운드 수(``max_iterations``)에
        들어가고, 루프는 반드시 빌드 뒤에 끝난다."""
        pr = PhaseResult(phase=phase)
        self._phase_items = []
        self._cur_phase = phase
        self._target = target
        self._mark(f"loop start ({goal})")
        self.ws.journal({"event": "loop_start", "phase": phase.value, "goal": goal, "max_iterations": max_iterations,
                         "target": target, "dry_run": self.dry_run, "blocked_known": len(self.blocked),
                         "first_build": first_build.log_path if first_build is not None else None})
        prev_build: Optional[BuildResult] = None
        prev_restored: list[BuildError] = []     # errors the previous round's rollbacks brought back (its baseline)
        prev_multiset: Optional[dict[str, int]] = None
        window: list[dict[str, int]] = []        # error multisets of the previous builds (oscillation detection)
        oscillations = 0
        stall_level = 0
        pending: list[_Pending] = []
        fix_rounds = 0
        build_no = 0
        last: Optional[BuildResult] = None
        while True:
            build_no += 1
            # the build opens the next round (`fix r<n>`); the one after the last allowed round closes it
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
                # 직전 라운드의 시도들을 이 빌드로 채점(악화는 여기서 되돌림 → 되돌린 트리를 다시 빌드)
                reverted = self._score(pr, pending, build, prev_build, prev_restored)
                scored = [p.attempt for p in pending]
                # 되돌린 뒤 재빌드: 되돌린 라운드에서 판정 불가였던 시도(pom 수정의 새 의존성 오류가 대상
                # 오류를 가렸던 것)는 다시 채점한다 — 그때까지 그 FixTry 는 대기 상태
                # a revert rebuild shows the tree minus the reverted commits against the same "before" build: tries
                # the reverted round left inconclusive (a pom edit's new dependency error hid their errors) are
                # scored again — their FixTry stays pending until then
                pending = [p for p in pending if p.attempt.outcome == "inconclusive"] if reverted else []
                again = {id(p.attempt) for p in pending}
                for p in pending:
                    p.attempt.notes.append("scored again on the rebuild after the revert")
                # "안 보임 ≠ 해결": 되돌린 라운드의 빌드, 오류를 파싱 못
                # 한 실패, 타임아웃은 아무것도 해결 처리하지 않는다(void)
                # a reverted round's build shows a tree that no longer exists; a failed build without parsed errors
                # (or a timeout) says nothing about which errors are gone → void: the list resolves nothing
                void = bool(reverted) or (not build.ok and not build.errors) or bool(build.timed_out)
                added = 0
                if not reused:     # the reused first build is on the list already (the caller's `build`)
                    # 판정보다 먼저 목록에 넣는다: 대기 중 시도가 걸린 항목은 열어 두고(resolve 가 판정), 다른
                    # 케이스의 수정으로 사라진 항목은 여기서 해결 + v2.1f
                    # ``resolved_by``(이 라운드에 그 파일·pom 을 바꾼 시도)
                    # BEFORE the verdicts: the list keeps this round's pending tries open for `resolve`
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
                # 사내 parent 가 해석되지 않으면 아무것도 빌드할 수 없다 → 실행 중단(코드 수정으로는 못 고침)
                if self._parent_abort(pr, phase, build, build_no):
                    break
                # 정체 = 직전 빌드와 오류 집합이 똑같음, 진동 = 더 이전 빌드와 같음(A↔B↔A). 정체가 쌓이면 더
                # 센 모델, 진동이 2번이면 관련 케이스를 차단(OSCILLATION)
                multiset = build.signature_multiset()
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
                if fix_rounds >= max_iterations:
                    log.info("build %d: %d error(s) in %d case(s) — max fix rounds (%d) used up; the loop ends here "
                             "and the final build carries the rest (reported as unresolved)", build_no,
                             build.error_count, len(clusters), max_iterations)
                if oscillating and oscillations >= 2:
                    self._block_oscillation(pr, phase, build_no, clusters)
                if self._only_blocked(pr, clusters, build_no):
                    break
                if fix_rounds >= max_iterations:
                    pr.notes.append(f"max iterations ({max_iterations}) reached with {build.error_count} error(s) "
                                    "left")
                    break
                if self.budget.time_exceeded():
                    pr.notes.append("run time budget exhausted: stopping the build-fix loop")
                    self._flag("time-budget-exhausted")
                    self.ws.journal({"event": "budget_exhausted", "phase": phase.value, "kind": "time"})
                    self._block_remaining(pr, phase, build_no, clusters, BlockReason.BUDGET_EXHAUSTED,
                                          f"run time budget ({self.cfg.loop.max_total_minutes} min) exhausted before "
                                          "this cluster was fixed")
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
                rec.decisions += decisions
                rec.cluster_count = len(rec.decisions)
                rec.attempt_ids = [a.id for a in pr.attempts if a.iteration == build_no]
                # 안쪽 반복을 끝내는 조건: 무엇이든 적용됨(→ 재빌드) /
                # 시도 자체가 없었음 / 라운드 소진 / 시간 초과 / 전부 차단
                if self.dry_run or applied_any or not tried or fix_rounds >= max_iterations \
                        or self.budget.time_exceeded() or all(self.blocked.covering(c) for c in clusters):
                    break
                # 재라운드: 트리가 그대로라 재빌드해도 같은 오류 →
                # 같은 빌드로 다음 단계를 묻는다(라운드 수는 센다)
                # nothing changed the tree, so a rebuild would show the same errors — the next rung of each case's
                # ladder on the same build (bounded by script.max_augments_per_case)
                self.ws.journal({"event": "reround", "phase": phase.value, "iteration": build_no,
                                 "fix_round": fix_rounds + 1})
            pending = new_pending
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
        # 끝까지 판정 불가로 남은 시도도 마지막에 한 번 실적으로 남긴다(KB 기록)
        self._credit_scripts(pr, [a for a in pr.attempts if a.outcome == "inconclusive"])
        if not pr.build_ok and not self.dry_run and not self.aborted:
            self._maybe_skip_test_compile(pr, phase, goal, target, last)
        if not pr.build_ok and not self.dry_run and not self.aborted:
            self._maybe_skip_tests(pr, phase, goal, target, last)
        if pr.build_ok and not self.test_compile_skipped_in(pr) and not self.tests_skipped_in(pr):
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

    # ------------------------------------------------------------------ v2.1: scoring → error list + log
    def _after_scoring(self, scored: list[FixAttempt], build_no: int) -> None:
        """직전 라운드의 채점된 시도들 마감: 오류 목록의 시도(FixTry) 결과·메모 동기화, 해결된 케이스 항목을
        resolve(개선·빌드 성공·부분 해결), 돌았던 저장 스크립트에 실적
        기록, 시도마다 결과 한 줄(``결과 (build N): resolved 3/4 → …``).

        The scored tries of the previous round: settle their FixTry (result, notes), resolve the error list's
        entries of a resolved case, credit the stored script that ran, one result line per try (in its case's
        stage)."""
        for a in scored:
            case_id, n, rnd = self._tries.get(id(a), (a.script_case_id, 0, 0))
            gone, targeted = self._tally.get(id(a), (0, len(a.cluster_errors)))
            self._sync_try(a)
            # 부분 해결도 사라진 항목은 이 시도로 해결 처리한다(남은 항목은 열린 채로 다음 시도에)
            if a.outcome in ("improved", "build_ok", "partial") and n:    # the gone entries of the case
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
        """빌드 요약 한 줄(오류 수, 새로 추가된 항목, 케이스별 해결 수, 남은 열린 항목).

        `build 2: 2 error(s); 새 에러 1건 추가; resolved 23 (case 7a52b1cea8 4/4, …), open 2` (§3.2)."""
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

    # ------------------------------------------------------------------ blocked bookkeeping
    def _touch_item(self, item_id: str) -> None:
        if item_id not in self._phase_items:
            self._phase_items.append(item_id)

    def _only_blocked(self, pr: PhaseResult, clusters: list[ErrorCluster], build_no: int) -> bool:
        """남은 클러스터가 전부 이미 차단된 것이면 True(단계 끝) — 고칠 수 없는 것에 라운드를 쓰지 않는다."""
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
        """BLOCKED 결정의 근거(BlockedCandidate): 트리아지가 가진 것, 없으면 결정 문구의 ``blocked: <사유>``
        에서 사유를 읽는다 (모르면 반복 실패)."""
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
        """``cand`` 가 덮는 오류들을 차단 항목으로 등록하고(javax / 외부 API 면 classpath 로 라이브러리
        귀속, 제안 문구 작성), 오류 목록의 케이스도 차단 처리한다.
        차단되지 *않은* 나머지 오류를 돌려준다 — 나머지는 계속 고친다.

        Record a BlockedItem for the errors `cand` covers; returns the cluster errors that are NOT blocked."""
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
        elif reason in (BlockReason.ARTIFACT_NOT_FOUND, BlockReason.PARENT_NOT_FOUND):
            artifact = next((e.artifact for e in errs if e.artifact is not None), None)
        elif reason == BlockReason.REACTOR_DUPLICATE:
            # v2.1k: 차단 표의 아티팩트 칸에 중복된 프로젝트 좌표(Maven 메시지에서)
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
        if getattr(cand, "suggestion", ""):          # round 10: a person's text (pinned `block` case action)
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

    def _classpath_hints(self, errs: list[BuildError], owner: Optional[str], required: Optional[str],
                         found: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """빈 칸(소유 클래스, 요구·실제 타입)을 classpath 도구로 채운다(도구가 그 기능을 줄 때만).

        Fill gaps with sbmig.build.classpath.owner_fqcn / type_mismatch (G1) when the locator provides them."""
        loc = self.locator
        if loc is None:
            return owner, required, found
        for e in errs[:3]:
            try:
                if owner is None and hasattr(loc, "owner_fqcn"):
                    o = loc.owner_fqcn(e)
                    if o and not o.startswith(("javax.", "jakarta.")):   # a bare mismatch names the type, not an owner
                        owner = o
                if (required is None or found is None) and hasattr(loc, "type_mismatch"):
                    r, f = loc.type_mismatch(e)
                    required, found = required or r, found or f
            except Exception as ex:  # pragma: no cover - attribution is best effort
                log.debug("classpath hints failed: %s", ex)
        return owner, required, found

    def _block_simple(self, phase: Phase, iteration: int, c: ErrorCluster, reason: BlockReason, why: str) -> None:
        """아직 차단 안 된 오류만 주어진 사유로 차단(예산 소진·LLM 불가·진동 등 루프가 직접 내리는 차단)."""
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
        """진동이 되풀이됨: 이 단계가 실제로 손댄 클러스터만 OSCILLATION 으로 차단한다(사람이 봐야 함)."""
        # round 10: the clusters this phase actually worked on — not every cluster that shares an L1 signature with one
        involved = [c for c in clusters if _same_cluster(c, pr.attempts, self.blocked.canonical_file)] or clusters
        why = "fixes keep toggling between the same error sets (A↔B↔A) within loop.oscillation_window"
        pr.notes.append(f"build {iteration}: oscillation repeated → blocking {len(involved)} cluster(s)")
        self._block_remaining(pr, phase, iteration, involved, BlockReason.OSCILLATION, why)

    def _parent_abort(self, pr: PhaseResult, phase: Phase, build: BuildResult, iteration: int) -> bool:
        """설정된 사내 parent(root / boot / comp)가 해석되지 않는다 → 아무것도 빌드할 수 없으니 실행을
        중단하고 분명한 차단 항목 (settings·Nexus·``artifacts.parent`` 를 고치라는)을 남긴다. 코드 수정으로는
        못 고치므로 LLM 을 부르지 않는다.

        The configured in-house parent is not resolvable → nothing can build: abort the run with a clear item."""
        keys = set(self.cfg.artifacts.parent_keys())           # root parent + boot_parent / comp_parent (round 9b)
        errs = [e for e in build.errors if e.kind == ErrorKind.PARENT_RESOLUTION
                and e.artifact is not None and e.artifact.key in keys]
        if not errs:
            return False
        from sbmig.triage.blocked import BlockedCandidate, repositories_in
        c = self.triager.cluster(errs, self.profile)[0]
        coords = errs[0].artifact.coordinates
        cand = BlockedCandidate(reason=BlockReason.PARENT_NOT_FOUND, errors=errs,
                                repositories=repositories_in("\n".join(f"{e.message}\n{e.raw}" for e in errs)),
                                evidence=[errs[0].message[:300]])
        self._register_blocked(phase, iteration, c, cand, f"blocked: parent_not_found — configured parent {coords} "
                                                          "is not resolvable; run aborted")
        self.aborted = (f"the configured in-house parent {coords} is not resolvable (settings: "
                        f"{self.cfg.build.settings_file or 'default'}) — fix the repository/settings or "
                        "artifacts.parent / boot_parent / comp_parent and rerun")
        self._flag("aborted:parent-not-found")
        self._followup(f"run aborted: {self.aborted}")
        pr.notes.append(f"build {iteration}: {self.aborted}")
        self.ws.journal({"event": "abort", "phase": phase.value, "iteration": iteration, "reason": self.aborted})
        return True

    def skips_for(self, target: Optional[str]) -> tuple[bool, bool]:
        """``target`` 빌드에 적용할 대체 결과(test-compile 건너뜀, 테스트 실행 건너뜀). 한 MSA 서비스의
        대체가 다른 서비스의 최종 빌드 방식을 바꾸면 안 된다: 서비스 빌드는 자기 것만, 프로젝트 전체 빌드는
        어느 서비스의 것이든 물려받는다.

        (test-compile skipped, tests skipped) by the L7 / L7b fallbacks for a build of `target` (round 10): a loop
        on the whole project ("") applies to every target; a whole-project build (target None) inherits any
        service's fallback (its reactor contains that service); a service build only its own."""
        t = _target_key(target)
        tc, ts = self.test_compile_skipped_targets, self.tests_skipped_targets
        if not t:
            return bool(tc), bool(ts)
        return (t in tc or "" in tc), (t in ts or "" in ts)

    @staticmethod
    def test_compile_skipped_in(pr: PhaseResult) -> bool:
        """이 단계 결과가 test-compile 건너뛰기 대체로 통과했는가(단계 메모로 판단)."""
        return any("test-compile-skipped" in n for n in pr.notes)

    @staticmethod
    def tests_skipped_in(pr: PhaseResult) -> bool:
        """이 단계 결과가 테스트 실행 건너뛰기(-DskipTests) 대체로 통과했는가."""
        return any("tests-skipped:" in n for n in pr.notes)

    # ------------------------------------------------------------------ build
    def _build(self, goal: str, target: Optional[str], label: str, phase: Phase, iteration: int,
               skip_test_compile: bool = False, skip_tests: bool = False) -> BuildResult:
        """루프의 빌드 한 번(v2 기본 ``-Dmaven.test.skip=true``). 빌드 부산물을 치우고, 케이스 키를
        다듬어(``refine_build``) 채점·오류 목록·클러스터·차단 등록부가 모두 같은 L1 / L2 를 보게 한다."""
        module, extra = build_target_args(self.profile, target)
        skip_test_compile = skip_test_compile or bool(getattr(self.cfg.build, "skip_test_compile", False))
        kw: dict[str, Any] = {"module": module, "label": label, "skip_test_compile": skip_test_compile}
        if extra:
            kw["extra_args"] = extra
        before = _status_paths(self.ws)
        with self._busy_with(f"mvn {goal} ({label})"):
            if skip_tests:
                try:
                    res = self.build_runner.run(goal, **kw, skip_tests=True)
                except TypeError:  # an older runner without the `skip_tests` option: pass -DskipTests as an argument
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

    # ------------------------------------------------------------------ scoring
    def _score(self, pr: PhaseResult, pending: list[_Pending], build: BuildResult,
               prev: Optional[BuildResult], restored: Optional[list[BuildError]] = None) -> bool:
        """직전 라운드의 시도들을 이 빌드로 채점한다. 되돌리기가 트리를 바꿨으면 True. 결과: build_ok;
        개선(improved) = 대상 오류가 전부 사라지고 *빌드가 그 단계까지 갔을 때만*("안 보임 ≠ 해결");
        부분(partial) = 일부만 사라지고 손댄 파일에 새 오류 없음; 변화 없음(no_change); 악화(regressed) = 손댄
        파일에 새 오류, 오류 수가 ×``regression_ratio`` 초과, 또는 pom 을 고친 뒤 새 구조적 오류·더 이른 단계
        오류가 대상 오류를 가림(→ 되돌림); 판정 불가(inconclusive) = 빌드가 말해 주지 못함(타임아웃, 파싱된
        오류 없음, 이 시도와 무관하게 이른 단계에서 멈춤) — KB 실적 없음, 되돌린 뒤 재빌드에서 다시 채점.
        ``restored`` = 롤백이 되살린 오류: 기준선에 넣어 "새 오류" 로 세지 않는다.

        Score the previous round's attempts against `build`. Returns True when a revert changed the tree.
        `restored` (v2) = the errors that round's rollbacks brought back: part of the baseline (`prev`), so they are
        never "new errors in a touched file" nor counted by the ×regression_ratio rule.

        Outcomes: `build_ok`; `improved` = every targeted error gone AND the build reached the stage that reports them;
        `partial` = some gone, the rest remain, nothing new in the touched files; `no_change`; `regressed` = new
        errors in a touched file (by (file, signature) occurrences), the ×regression_ratio rule, or — after a pom
        edit — a new structural error kind / new errors of an earlier stage that hide the targeted ones (reverted);
        `inconclusive` = the build cannot tell (timed out, failed without parsed errors, stopped at an earlier stage
        this attempt did not cause): no KB credit, scored again on a revert rebuild."""
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
        # 눈먼 빌드: 아무 시도에 대해서도 말해 주지 않는다 → 전원 판정 불가(KB 실적 없음)
        # a timed-out build, or a failed one without a single parsed error, says nothing about any attempt
        blind = not build.ok and (bool(build.timed_out) or not real)
        structural: set[str] = set()
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
            # True = gone and the build could have shown them; None = gone from a build that could not; False = present
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
            # pom 을 고친 시도 뒤에 새 구조적 오류 종류(pom 무효,
            # 의존성 해결 실패 등)가 나타나면 숫자와 상관없이 되돌린다
            if new_struct and pom_touched:
                a.outcome = "regressed"
                a.notes.append(f"regressed: structural error kind(s) {', '.join(new_struct)} appeared after this "
                               "attempt edited a pom.xml → reverted regardless of error counts")
                a.introduced_errors = sanitize_errors([e for e in real if e.kind.value in new_struct], 10)
                structural.add(a.id)
                continue
            left, targeted = left_of[id(a)]
            if a.cluster_resolved is None:
                # the targeted errors are not in this build, but the build never got to report them
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
            # 악화 판정: 손댄 파일의 새 오류는 (파일, 서명) 횟수로
            # 센다 — 다른 파일에 같은 L1 서명이 있어도 여기선 새 오류
            new_in_touched = _new_in_files(build.errors, touched, prev_count)
            before = max(a.errors_before, base_total) if restored else a.errors_before
            ratio_regress = (not any_resolved) and total > max(before, 1) * ratio
            if not a.cluster_resolved and (new_in_touched or ratio_regress):
                a.outcome = "regressed"
                a.introduced_errors = sanitize_errors(new_in_touched, 10)   # the augment prompt: why it regressed
                why = (f"{len(new_in_touched)} new error(s) in touched files" if new_in_touched
                       else f"errors {before} → {total} (> ×{ratio})")
                a.notes.append(f"regressed: {why}")
            elif a.cluster_resolved:
                a.outcome = "improved"
            elif 0 < left < targeted:
                a.outcome = "partial"
                a.notes.append(f"partial: {targeted - left} of {targeted} targeted error(s) gone, {left} remain")
            else:
                a.outcome = "no_change"
        # v2: no KB recipe credit (no KB recipes in the flow); the fix scripts are credited by the caller after the
        # revert below settled the outcomes (`_credit_scripts`)
        reverted = False
        # 되돌리기: HEAD 쪽부터 연속으로 악화된 커밋 묶음만 reset 한다(뒤의 개선된 커밋이 의존할 수 있으므로
        # 거기서 멈춤). 구조적 악화는 예외 — 뒤의 커밋이 있어도 git revert 로 되돌린다
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
            # structural regressions are reverted even when later commits of the round depend on them (git revert)
            for p in pending:
                a = p.attempt
                if a.outcome != "regressed" or a.id not in structural:
                    continue
                group = [q for q in pending if q.post_sha == p.post_sha]
                res = self.ws._git("revert", "--no-edit", p.post_sha, check=False)   # noqa: SLF001
                if res.returncode != 0:
                    self.ws._git("revert", "--abort", check=False)                      # noqa: SLF001
                    a.notes.append(f"structural revert of {p.post_sha[:10]} failed: {(res.stderr or '').strip()[:200]}")
                    continue
                reverted = True
                for q in group:
                    q.attempt.outcome = "reverted"
                    q.attempt.notes.append(f"reverted commit {p.post_sha[:10]} (git revert: structural regression)")
                self.ws.journal({"event": "revert", "phase": pr.phase.value, "commit": p.post_sha, "mode": "git-revert",
                                 "structural": new_struct, "attempts": [q.attempt.id for q in group]})
            for p in pending:
                if p.attempt.outcome == "regressed":
                    p.attempt.notes.append("not reverted: later commits that improved things depend on it")
        for p in pending:
            self.ws.journal({"event": "attempt_scored", "phase": pr.phase.value, "attempt": p.attempt.id,
                             "cluster": p.attempt.cluster_id, "outcome": p.attempt.outcome,
                             "cluster_resolved": p.attempt.cluster_resolved, "errors_after": total})
        return reverted

    # ------------------------------------------------------------------ fixing
    def _kb_match(self, cluster: ErrorCluster) -> KBMatch:
        """KB 조회(사람이 ``block`` 으로 고정한 케이스 + 케이스의
        마지막 해결 힌트). 실패해도 빈 결과로 계속 간다."""
        try:
            m = self.kb.match(cluster, self.ws, self.profile)
            return m if m is not None else KBMatch()
        except Exception as e:
            log.warning("kb.match failed for %s: %s", cluster.id, e)
            return KBMatch()

    def _plan(self, clusters: list[ErrorCluster]) -> list[tuple[ErrorCluster, Optional[TriageDecision], list[str]]]:
        """트리아지의 라운드 계획: 근본 원인 순으로 정렬된 (클러스터, 가려짐 결정, 가리는 클러스터들). 예:
        pom 문제가 있으면 그 모듈의 컴파일 오류는 가려 둔다(먼저 원인을 고친다)."""
        planner = getattr(self.triager, "plan_iteration", None)
        if not callable(planner):
            return [(c, None, []) for c in clusters]
        return [(it.cluster, it.skip, list(it.masked_by)) for it in planner(clusters)]

    def _decide(self, phase: Phase, iteration: int, stall_level: int, cluster: ErrorCluster,
                match: KBMatch) -> TriageDecision:
        """트리아지 결정(SCRIPT / BLOCKED / SKIP / ESCALATE). 차단 정책(``loop.blocked_policy``)·한도·고정된
        케이스는 트리아지가 판단한다. 트리아지가 깨지면 ESCALATE — 루프는 멈추지 않는다."""
        ctx = TriageContext(phase=phase, iteration=iteration, stall_level=stall_level,
                            previous_attempts=self._previous(cluster), llm_calls_used=self.budget.llm_calls,
                            llm_calls_budget=self.budget.max_llm_calls)
        try:
            return self.triager.decide(cluster, match, ctx, self.profile)
        except Exception as e:
            log.exception("triage.decide failed for %s", cluster.id)
            return TriageDecision(cluster_id=cluster.id, kind=DecisionKind.ESCALATE,
                                  rationale=f"triage failed: {type(e).__name__}: {e}")

    def _previous(self, cluster: ErrorCluster, *, ladder: bool = True) -> list[FixAttempt]:
        """이 클러스터에 대한 앞서 *채점된* 시도들 — 사다리와 한도의 근거. 같은 L1 서명의 다른 파일, 이번
        라운드의 대기 중 시도, LLM 쿨다운을 일으킨 전송 실패
        시도는 뺀다(쿨다운 라운드가 케이스 한도를 깎으면 안 된다). v2.1l / v2.1m: sbmig 의 사전 시도
        (``sbmig-version`` / ``sbmig-resources``, LLM 0회)는 사다리(``ladder``)에서 빠진다 — LLM 시도 한도를 깎으면 안 된다. 프롬프트의
        "앞 시도" 에는 들어간다(``ladder=False``).

        Earlier SCORED attempts on THIS cluster (round 10: not other files that share an L1 signature, not this
        round's pending ones), without the transport failures that triggered the LLM cool-down (L5): the cool-down
        round must not count against the case's limits. v2.1l / v2.1m: the zero-LLM pre-tries (`PRE_TRY_SOURCES`) are not
        ladder steps (`ladder=True`: left out, they never use up an LLM try); the prompt's history keeps them."""
        return [a for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)
                if a.id not in self._cooled_attempts and a.outcome != "pending"
                and not (ladder and a.source in PRE_TRY_SOURCES)]

    def _sibling_hints(self, cluster: ErrorCluster, limit: int = 3) -> list[str]:
        """이 실행의 *다른* 파일·범위에서 같은 오류가 어떻게
        고쳐졌는지(또는 무엇이 안 됐는지) — 프롬프트 힌트로만 쓴다.

        The same error in OTHER files of this run (shared signature): hint material for the prompt only (round 10)
        — how a sibling was fixed, or what did not work there. v2: a case already spans its files, so this is the
        same case in another scope (another MSA service's loop)."""
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
        """v2: 케이스(L2)마다 클러스터 하나(여러 파일에 걸침), 루프의 빌드 대상(MSA 서비스) 범위 안에서.

        v2: one cluster per case (L2) across files, scoped by the loop's build target (MSA services)."""
        return self.triager.cluster(errors, self.profile, scope=target)

    def _fix_round(self, pr: PhaseResult, phase: Phase, iteration: int, build: BuildResult, target: Optional[str],
                   stall_level: int, clusters: Optional[list[ErrorCluster]] = None, *, round_no: int = 1,
                   max_rounds: int = 0) -> tuple[list[_Pending], bool, list[TriageDecision], bool, list[BuildError]]:
        """수정 라운드 하나: 케이스마다 시도는 최대 1번, 라운드당 케이스는
        ``loop.max_clusters_per_iteration``(8)개까지(미뤄진 케이스·차단은 세지 않음). 가려진 케이스는, 가린
        쪽이 손댈 수 없는 것(차단·에스컬레이트)이면 가림을 풀고 다룬다.

        One fix round (log stage `fix r<round_no>`, set by the caller): at most one try per case,
        `loop.max_clusters_per_iteration` cases. Returns (pending tries, anything applied, decisions, a try was made,
        errors brought back by rollbacks)."""
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
        st = _Round(phase=phase, iteration=iteration, build=build, target=target, stall_level=stall_level,
                    others={c.id: _label(c) for c in clusters}, round=round_no,
                    n=sum(1 for a in pr.attempts if a.iteration == iteration))   # ids stay unique across re-rounds
        masked: list[tuple[ErrorCluster, TriageDecision, list[str]]] = []
        handled = 0
        before = sum(1 for a in pr.attempts if a.decision.kind == DecisionKind.SCRIPT)
        for c, skip, by in planned:
            c2 = self._unblocked_part(st, c)
            if c2 is None:
                continue
            if skip is not None:
                masked.append((c2, skip, by))
                continue
            if handled >= limit:
                continue
            if self._handle_cluster(pr, st, c2) not in ("blocked", "deferred"):     # deferred cases do not count
                handled += 1
        # 가림 풀기: 가린 쪽(예: pom 문제)이 차단·에스컬레이트되어
        # 손댈 수 없으면 고칠 수 있는 컴파일 오류를 숨기면 안 된다
        # unmask: a masker that cannot be acted upon must not hide compile errors we could fix
        for c, skip, by in masked:
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
        """이미 차단된 오류는 뺀다. 남는 것이 없으면 None(BLOCKED 결정을 기록하고 건너뜀).

        Drop the errors the run already blocked; None when nothing is left (decision BLOCKED is recorded)."""
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
        """트리아지의 마지막 SCRIPT 결정이 정한 사다리 단계(reuse / augment / new, 또는 한도 도달 = mode
        None). 트리아지가 안 주면 여기서 ``plan_script_step`` 으로 계산한다.

        The ladder step of the triager's last SCRIPT decision for `c` (computed here for a triager without it)."""
        getter = getattr(self.triager, "script_step", None)
        step = None
        if callable(getter):
            try:
                step = getter(c.id)
            except Exception:  # pragma: no cover - a foreign triager
                step = None
        if step is None:
            from sbmig.triage.scripts import plan_script_step
            step = plan_script_step(c, self._previous(c), self.cases if self.cases.active else None, self.cfg)
        return step

    def _deferral(self, st: "_Round", c: ErrorCluster, step: Any) -> tuple[list[str], str]:
        """``c`` 를 다음 빌드까지 미루게 하는 (케이스들, "files" | "pom"). ([], "") = 지금 시도. 근본 원인을
        먼저 다루므로, 원인이 고쳐지는 중인 결과 케이스는 재빌드에서 대개 사라진다 — 라운드당 원인 하나에 LLM
        1회가 맞는 비용이다. "files" = 이번 라운드에 수정(스크립트 또는 edits)이 이미 그 오류 파일 전부에서
        돌았음, "pom" = 컴파일 케이스의 모듈 pom(또는 루트 pom)을 이번 라운드 수정이 바꿨음. 재사용(LLM 0회,
        진짜 해결일 수 있음)·파일 없는 오류·라운드의 첫 케이스는 미루지 않는다.

        (case ids, "files" | "pom") that make `c` wait for the rebuild — ([], "") = attempt `c`. Root causes come
        first (`CAUSE_RANK`), so this defers the consequences of a cause being fixed — they mostly vanish on the
        rebuild, and one LLM call per root cause is what a round should cost.

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
        """파일이 속한 모듈의 pom(가장 가까운 위쪽 pom.xml). 알 수 없으면 None(미루기는 최적화일 뿐).

        The module pom of a project file (`sbmig.fix.context.nearest_pom`); None when it cannot be told."""
        try:
            from sbmig.fix.context import nearest_pom
            return nearest_pom(self.ws, rel)
        except Exception as e:  # noqa: BLE001 - deferral is an optimisation: never fail an attempt over it
            log.debug("nearest pom of %s unavailable: %s", rel, e)
            return None

    def _handle_cluster(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str = "") -> str:
        """결정 → 케이스의 시도(저장 스크립트 / LLM 답: edits 또는 스크립트) → 커밋. 모든 로그 줄이 케이스
        단계 태그 (``fix r<n> <case_id>``) 안에 찍힌다. 결과:
        blocked / escalated / skipped / deferred / attempted.

        Decide → the case's try (stored script / LLM answer: edits or script) → commit, every log line in the case's
        stage (`fix r<n> <case_id>`). Returns "blocked" (nothing left to do for this cluster), "escalated",
        "skipped", "deferred" (its files are being fixed by an earlier case of this round) or "attempted"."""
        with stage(case_stage(st.round, c)):
            return self._handle_case(pr, st, c, prefix)

    def _handle_case(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str) -> str:
        """한 케이스 처리. 사다리가 끝났으면(mode None) 반복 실패, LLM 이 필요한데 LLM 불가·예산 소진이면 그
        사유로 강제 차단. 일부만 차단되면 나머지 오류로 다시 결정한다(최대 4번). 미뤄진 케이스는 시도가
        아니다(한도·정체 감지에 안 보임)."""
        from sbmig.triage.blocked import BlockedCandidate
        log.info("%s", _case_header(c))
        self._mark(f"KB 탐색 for {c.id}")
        m = self._kb_match(c)            # pinned `block` case hits (a person's decision) + the case's last solution
        self._match_hints[c.id] = list(m.hints or [])
        for _ in range(4):
            self._mark(f"deciding {c.id}")
            d = self._decide(st.phase, st.iteration, st.stall_level, c, m)
            if prefix:
                d.rationale = prefix + d.rationale
            step = self._script_step(c) if d.kind == DecisionKind.SCRIPT else None
            seen_step = step
            forced = None
            # 강제 차단: 사다리 끝(저장본 1 + LLM 3회 소진 → 반복 실패), LLM 이 필요한데 LLM 불가 또는 예산
            # 소진. 저장본 재사용(LLM 0회)은 LLM 이 없어도 막지 않는다
            if step is not None and step.mode is None:
                forced = BlockedCandidate(reason=BlockReason.REPEATED_FAILURE, errors=list(c.errors),
                                          evidence=[(step.why or "fix-script limits reached") + "; needs a human"])
            elif step is not None and step.needs_llm and self.llm_unavailable:
                forced = BlockedCandidate(reason=BlockReason.LLM_UNAVAILABLE, errors=list(c.errors),
                                          evidence=[f"LLM unavailable ({self.llm_unavailable})"])
            elif step is not None and step.needs_llm and not self.budget.llm_allowed():
                forced = BlockedCandidate(reason=BlockReason.BUDGET_EXHAUSTED, errors=list(c.errors),
                                          evidence=[f"LLM budget exhausted ({self.budget.llm_text()})"])
            # v2.1l / v2.1m: 누락 버전·바이너리 리소스 필터링 케이스는 LLM 전에(사다리·한도·LLM 장애와 무관하게)
            # sbmig 가 한 번 고쳐 본다 — 단, 아래 미루기 검사 뒤에(같은 라운드에 다른 케이스가 바꾼 pom 이면 기다린다)
            # the zero-LLM pre-try (decided on the triage decision, run after the deferral check below)
            pre, pre_poms = self._pre_try_for(c, d)
            if forced is not None:
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                                   rationale=prefix + f"blocked: {forced.reason.value} — {forced.evidence[0]}")
                step = None
            # 같은 라운드에 앞 케이스가 이미 그 파일·모듈 pom
            # 을 고쳤으면 미룬다(대개 재빌드에서 함께 사라진다)
            by, why = self._deferral(st, c, step) if d.kind == DecisionKind.SCRIPT else ([], "")
            if not by and pre is not None:              # the pre-try edits poms: one changed this round waits
                by, why = self._pre_try_deferral(st, pre_poms)
            if by:
                # not an attempt: no FixAttempt, so neither the ladder, the limits, the blocked policy nor the stall
                # detection (error multisets) see it; the next round looks at the case again if it is still there
                names = ", ".join(x[len("case:"):] if x.startswith("case:") else x for x in by[:3])
                what = ("its module's pom was changed by case" if why == "pom" else "its files are being fixed by case")
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.SKIP,
                                   rationale=prefix + f"deferred: {what} {names} this round — waiting for the rebuild")
                st.decisions.append(d)
                self._journal_decision(st, d)
                log.info("deferred: %s %s → 다음 빌드까지 대기 (시도로 세지 않음)", what, names)
                return "deferred"
            # 적용되면 이것이 이번 라운드의 시도, 아니면 같은 라운드에 평소 사다리
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
                # only part of the case is blocked (e.g. one javax constructor mismatch): keep working on the rest
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
            return "attempted"      # one try per case and round
        return "attempted"

    def _kb_line(self, c: ErrorCluster, d: TriageDecision, step: Any) -> str:
        """로그의 "KB 탐색: …" 한 줄. SCRIPT 결정이면 사다리가 만든 줄(실행 / LLM 보강 요청 / 한도 도달),
        아니면 찾은 것과 결정(예: 저장된 스크립트 없음 → blocked (…)).

        The KB search result for the log (§3.2): the ladder's own line for a SCRIPT decision (or a ladder that ran
        out), else what the search found and the decision ("KB 탐색: 저장된 스크립트 없음 → blocked (…)")."""
        if step is None:
            getter = getattr(self.triager, "script_step", None)
            try:
                step = getter(c.id) if callable(getter) else None
            except Exception:  # pragma: no cover - a foreign triager
                step = None
        if step is None:
            return ""
        if d.kind == DecisionKind.SCRIPT or (step.mode is None and d.kind == DecisionKind.BLOCKED
                                             and "repeated_failure" in (d.rationale or "")):
            return step.kb_line
        m = _BLOCKED_RAT_RE.search(d.rationale or "")
        what = d.kind.value + (f" ({m.group('r')})" if m else "")
        return f"KB 탐색: {step.kb_found or '-'} → {what}"

    # ------------------------------------------------------------------ v2.1 tries
    def _edits_label(self, att: FixAttempt) -> str:
        """v2.1i edits 모드 시도의 꼬리표 ``edits:<case>#<n>``(n = 이 실행에서 케이스의 시도 번호, 오류 목록의 FixTry.n).

        The label of an edits-mode try, `edits:<case>#<n>` (n = the case's try number in this run, `FixTry.n`)."""
        case_id, n, _rnd = self._tries.get(id(att), (att.script_case_id, 0, 0))
        case_id = case_id or att.script_case_id or att.cluster_id
        if not n:                                # a try of another loop of the run (MSA service): number unknown
            return f"edits:{case_id}"
        from sbmig.report.errors import edits_label
        return edits_label(case_id, n)

    def _attempt_name(self, a: FixAttempt) -> str:
        """시도를 부르는 이름(힌트·로그): script 모드 ``script <case>:v<N> (<mode>)``, edits 모드 ``edits:<case>#<n>``.

        Who made an attempt (hints, logs): `_script_name` in script mode, `edits:<case>#<n>` in edits mode."""
        return self._edits_label(a) if self.edits_mode else _script_name(a)

    def _project(self) -> str:
        """KB 기록에 쓰는 프로젝트 이름(``sbmig.kb.common.project_name`` 이 있으면 그것, 없으면 프로필 이름).

        The project name the KB records use (`sbmig.kb.common.project_name` when it exists)."""
        try:
            from sbmig.kb.common import project_name
            return project_name(self.profile, self.ws)
        except Exception:  # noqa: BLE001 - the KB package may be gone (v2.1): the profile's name
            return (getattr(self.profile, "name", "") or "") or self.ws.root.name

    def _script_attempt(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, d: TriageDecision, step: Any) -> None:
        """케이스의 시도 한 번. 순서가 중요하다: (1) 보강의 기준(base)을 무엇이 움직이기 전에 만든다, (2) 그
        케이스의 직전 시도를 되돌린다(개선·성공이 아니면) — 그 시도가 지웠던 오류가 케이스로 돌아온다, (3)
        저장 스크립트 실행(``kb-script``) 또는 LLM 답 적용(``_llm_try``), 커밋. 새 시도가 아무것도 바꾸지
        못하면 되돌리기를 취소해 마지막 빌드가 본 트리로 둔다.

        One try of a case: build the augment base (before anything moves), roll back the case's previous try, then
        the stored script (`kb-script`) or the LLM's answer (`_llm_try`), commit. A try that changes nothing gives the
        rollback back (the tree stays as the last build saw it)."""
        t0 = time.monotonic()
        case_id = step.case_id
        base = self._script_base(c, step) if step.mode == "augment" else None
        head_before = self.ws.head()
        rolled = self._rollback_previous(c, st)
        restored = rolled[1] if rolled else []
        if restored:                                    # the case as it stands on the rolled-back tree
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
        # 미루기(deferral) 기록: 수정이 돈 오류 파일·바뀐 pom
        # → 같은 라운드의 뒤 케이스들은 다음 빌드까지 기다린다
        if self._ran_on_files(att):                     # a fix ran on its files: later cases there wait for the
            for e in c.errors:                          # rebuild (a static-check refusal never ran)
                if e.file:
                    st.covered.setdefault(_norm_path(e.file), c.id)
        if att.applied:                                 # a pom it changed: that module's compile cases wait too
            for ch in att.changes:
                for path in (ch.path, ch.new_path):
                    if path and PurePosixPath(_norm_path(path)).name == "pom.xml":
                        st.poms.setdefault(_norm_path(path), c.id)
        # 직전 시도를 되돌렸고 새 시도가 적용됐으면: 직전 것은 "rolled back"(최종 트리에 없음, 운영자
        # 확인에서도 빠짐). 새 시도가 아무것도 바꾸지 못했으면
        # 되돌리기를 취소한다(트리를 마지막 빌드가 본 그대로)
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

    def _llm_try(self, att: FixAttempt, c: ErrorCluster, st: "_Round", step: Any, base: Optional[ScriptBase]) -> None:
        """신규 / 보강: LLM 에 묻고(원인·해결·edits·스크립트) 적용한다. 보강 + 스크립트 → 새 버전
        저장·실행(``llm-augment``); 보강인데 스크립트 없이 edits(``no_script_reason``) → edits 직접
        적용(``llm-edit``); 신규 + edits → edits 를 바로 적용하고 스크립트는 unverified 로
        저장만(``llm-new``); 신규 + 스크립트만 → 저장·실행. 첫 선택이 적용되지 않으면(edits 실패·변화 없음,
        스크립트 실패) 같은 답의 나머지 반쪽을 대신 적용한다(시도에 메모).

        new / augment: ask the LLM (원인 · 해결 · edits · script), then apply (§3.1):

        * augment + script → the new version is stored and runs (`llm-augment`); augment without a script but with
          edits (`no_script_reason`) → the edits are applied directly (`llm-edit`);
        * new + edits → the edits are applied directly, the script (when there is one) is stored `unverified`
          (`llm-new`; without a script `llm-edit`); new + script only → it is stored and runs (`llm-new`);
        * the first choice did not apply (the edits failed / changed nothing, or the script failed) → the other half
          of the same answer is applied instead (noted on the try)."""
        t0 = time.monotonic()
        ans = self._ask_llm(att, c, st, step, base)
        secs = time.monotonic() - t0
        if self.edits_mode:                             # v2.1i: edits only — nothing stored, nothing run
            return self._edits_answer(att, c, st, ans, secs)
        requested = "llm-augment" if att.script_mode == "augment" else "llm-new"
        if ans is None:                                 # no usable answer: the failure is on the attempt
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
        # 보강했다는 스크립트가 기준 버전과 똑같으면 다시 돌리지
        # 않는다(같은 결과가 나올 뿐) — edits 가 있으면 그것으로
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
        # 신규는 edits 먼저(이 프로젝트를 바로 고치고, 스크립트는 다음 프로젝트용으로 unverified 저장), 보강은
        # 스크립트 먼저(저장본을 고쳐 쓰는 것이 목적) — 스크립트가 없을 때만 edits
        edits_first = bool(edits) and (att.script_mode == "new" or not has_code)
        if edits_first:
            self._begin_try(att, st)
            self._apply_edits(att, c, edits, st)
            # stored next to the fix (unverified); a duplicate only matters when it would run
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
            if self._script_ran.get(id(att)):           # the script ran and failed: credited as such right away
                self._credit_script(att)
                self._script_ran[id(att)] = False
            self._switch(att, f"script v{att.script_version} did not apply → applying the answer's edits instead")
            self._apply_edits(att, c, edits, st)

    def _edits_answer(self, att: FixAttempt, c: ErrorCluster, st: _Round, ans: Any, secs: float) -> None:
        """v2.1i edits 모드의 답 처리: 답의 edits 만 적용한다(출처 ``llm-edit``). 답에 스크립트가 있어도 저장·실행하지
        않고 메모만 남긴다. edits 가 없으면 실패한 시도(이유와 함께) — LLM 요청 한 번으로 센다.

        An edits-mode answer: only its edits are applied (source `llm-edit`); a script in it is neither stored nor
        run (a note says so); no edits → a failed try with the reason (it still counts as one LLM try)."""
        att.source = "llm-edit"
        if ans is None:                                 # no usable answer: the failure is on the attempt
            log.info("LLM 답변 없음 (%.0fs): %s", secs, _first_sentence("; ".join(att.apply_errors) or "-", 240))
            self._begin_try(att, st)
            return
        self._answers[id(att)] = ans
        edits = list(ans.edits or [])
        att.analysis = ans.analysis or ""
        att.solution = getattr(ans, "solution", "") or ""
        self._log_answer(secs, att, "", edits)
        if (ans.code or "").strip():
            att.notes.append(EDITS_SCRIPT_IGNORED)
            log.info("%s", EDITS_SCRIPT_IGNORED)
        self._begin_try(att, st)
        if not edits:
            # 다음 요청의 "앞 시도" 에 그대로 보이는 문장 — edits 모드 프롬프트에 script 라는 말이 새지 않게
            why = "LLM answered with no edits"
            if ans.error:
                why += f": {ans.error}"
            if ans.requested_files:
                why += f" (asked for: {', '.join(ans.requested_files[:5])})"
            return self._fail(att, why)
        self._apply_edits(att, c, edits, st)

    def _switch(self, att: FixAttempt, note: str) -> None:
        """답의 첫 선택이 적용되지 않았다: 그 실패는 메모로 바꾸고, 같은 시도에서 나머지 반쪽으로 계속한다.

        The answer's first choice did not apply: its failure becomes a note, the try goes on with the other half."""
        att.notes += [f"first choice failed: {e}" for e in att.apply_errors]
        att.apply_errors = []
        att.outcome = "pending"
        att.notes.append(note)
        log.info("%s", note)

    def _log_answer(self, secs: float, att: FixAttempt, code: str, edits: list) -> None:
        """LLM 답변 요약 한 줄(걸린 초, 원인·해결 첫 문장, edits 개수, 스크립트 유무와 없으면 이유).

        `LLM 답변 (21s): 원인 … / 해결 … / edits 1 / script 있음` (§3.2)."""
        parts = [f"원인 {_first_sentence(att.analysis, 140) or '-'}", f"해결 {_first_sentence(att.solution, 140) or '-'}",
                 f"edits {len(edits)}"]
        if self.edits_mode:                            # v2.1i: no script asked for — nothing to say about one
            pass
        elif code.strip():
            parts.append("script 있음")
        elif att.no_script_reason:
            parts.append(f"script 없음 (이유: {_first_sentence(att.no_script_reason, 140)})")
        else:
            parts.append("script 없음")
        log.info("LLM 답변 (%.0fs): %s", secs, " / ".join(parts))

    def _begin_try(self, att: FixAttempt, st: "_Round") -> None:
        """이 시도의 FixTry 를 오류 목록에 연다(출처 kb-script / llm-new / llm-augment / llm-edit 가 정해진
        뒤). 목록이 번호를 안 주면 케이스별 자체 번호를 쓴다.

        The error list's FixTry of this attempt (once its source is known)."""
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
        """FixAttempt → FixTry 동기화(적용 뒤, 채점 뒤, 롤백 뒤 매번 모든 필드). diff 는
        ``errors.diff_chars`` 에서 자르고(전체는 changes.patch), 수정
        기록·메이저 변경·운영자 확인 가이드는 옛 목록이면 빠진다.

        FixTry ← FixAttempt (after the apply, after the scoring, after a rollback): every field each time."""
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
        """수정(스크립트 또는 edits)이 실제로 파일 위에서
        돌았는가 — 정적 검사에서 거부된 것은 돌지 않은 것."""
        res = att.script_result
        if res is not None and res.status != "rejected":
            return True
        er = self._edit_results.get(id(att))
        return er is not None and er.status != "rejected"

    def _script_base(self, c: ErrorCluster, step: Any) -> Optional[ScriptBase]:
        """보강 프롬프트의 기준(ScriptBase): 이어 고칠 버전과 이 프로젝트에서 그것이 한 일(바꾼 파일, 오류,
        로그 끝 50줄), 아직 남은 케이스 오류, 새로 생긴 오류,
        버전 이력. 깨지면 None → 신규 요청(절대 죽지 않는다).

        The augment context: the version to extend and what it did in this project (or, for a stored version that
        did not run here, what the store knows of it), the case's remaining errors and the version history."""
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
                                  history=self._script_history(step.case_id, c))
            return ScriptBase(case_id=step.case_id, version=int(getattr(step.meta, "version", 0) or 0),
                              code=step.code, remaining_errors=[fmt_error(e) for e in c.errors][:30],
                              history=self._script_history(step.case_id, c))
        except Exception as e:  # noqa: BLE001 - a broken base means a new script, never a crash
            log.warning("script base for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return None

    def _script_history(self, case_id: str, c: ErrorCluster) -> list[str]:
        """저장된 버전마다 한 줄(상태, 성공 / 실패, 마지막 결과) + 이번 실행의 시도들 — 보강 프롬프트용 이력.

        'v1 [proven]: 2 ok / 1 failed — last: no_change' per stored version, then this run's tries."""
        out: list[str] = []
        info = self.cases.find(case_id) if self.cases.active else None
        for m in list(getattr(info, "scripts", None) or [])[-SCRIPT_HISTORY:]:
            out.append(f"v{m.version} [{m.status}]: {m.successes} ok / {m.failures} failed — last: "
                       f"{m.last_result or '-'}")
        for a in _same_cluster(c, self.history, self.blocked.canonical_file):
            if a.decision.kind == DecisionKind.SCRIPT:
                st = a.script_result.status if a.script_result is not None else "-"
                how = "edits" if a.edits_applied else f"script {st}"
                out.append(f"this run: {a.id} {a.source or a.script_mode} v{a.script_version} → {a.outcome} ({how})")
        return out[-2 * SCRIPT_HISTORY:]

    def _rollback_previous(self, c: ErrorCluster, st: "_Round"
                           ) -> Optional[tuple[FixAttempt, list[BuildError], str, bool]]:
        """케이스의 직전 시도를 되돌린다(회사 규칙: 개선·빌드 성공이 아니면 다음 시도 전에 되돌림; 이미
        되돌렸거나 적용 안 된 것은 제외). 그 시도가 HEAD 면 reset, 아니면 ``git revert``. 돌려주는 것: (그
        시도, 그 시도가 지웠던 — 이제 트리에 돌아온 — 오류들, 다음 시도용 메모, 되돌렸나). revert 가 충돌로
        실패하면 메모만 남기고 다음 버전이 그 위에서 돈다.

        Roll back the case's previous try unless it was improved / build_ok (or already reverted / never applied).
        Returns (that try, the errors it had removed — back in the tree now, a note for the next try, rolled back)
        or None. A rollback that fails (a later commit changed the same lines) is a note (rolled back False): the
        next version then runs on top of it."""
        prev = [a for a in _same_cluster(c, self.history, self.blocked.canonical_file)
                if a.decision.kind == DecisionKind.SCRIPT and a.applied and a.commit]
        if not prev:
            return None
        a = prev[-1]
        if a.outcome in NO_ROLLBACK or id(a) in self._rolled_back or _kept_partial(a):
            return None
        how = ""
        what = self._edits_label(a) if self.edits_mode else \
            f"v{a.script_version}" if a.script_version and not a.edits_applied else (a.source or "edits")
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
        except Exception as e:  # noqa: BLE001 - a git problem: keep going on top of it
            a.notes.append(f"rollback before the next attempt failed: {type(e).__name__}: {e}")
            return a, [], (f"the previous attempt {a.id} could not be rolled back ({type(e).__name__}): this "
                           "version ran on top of it"), False
        # 그 시도가 지웠던 오류 = 시도의 대상 오류 중 이번 빌드에 없는 것. 다음 채점의 기준선에 더한다
        restored = _gone(a.cluster_errors, st.build)
        st.restored += restored
        self.ws.journal({"event": "script_rollback", "phase": st.phase.value, "iteration": st.iteration,
                         "attempt": a.id, "case": a.script_case_id, "version": a.script_version,
                         "outcome": a.outcome, "commit": a.commit, "mode": how, "restored_errors": len(restored)})
        log.info("롤백: %s (%s, %s) 되돌림 (%s)%s", what, a.id, a.outcome, how,
                 f" — {len(restored)} error(s) it had removed are back" if restored else "")
        return a, restored, (f"the previous attempt {a.id} ({what}, {a.outcome}) was rolled back ({how}) before this "
                             "one"), True

    def _undo_rollback(self, st: "_Round", prev_att: FixAttempt, head_before: str, restored: list[BuildError],
                       att: FixAttempt) -> None:
        """다음 시도가 아무것도 바꾸지 못했다: 되돌렸던 시도를 되살린다(마지막 빌드가 본 트리 그대로).

        The next try changed nothing: put the rolled-back try back (the tree the last build saw)."""
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
        """신규 / 보강: FixContext(힌트: KB 마지막 해결·같은 실행의 다른 파일·권고·병합 충돌 참고본·v2.1d
        의존성 버전 사실)를 만들어 LLM 답(원인·해결·edits·스크립트 / 스크립트 없는 이유)을 받는다. 없으면
        None(전송 실패·설정 오류·propose_script 없는 fixer — 이유는 시도에). 정체가 쌓였거나 케이스의 두 번째
        보강부터는 더 센 모델(``llm.escalation_model``)을 청한다.

        new / augment: the LLM's answer (FixAnswer: 원인 · 해결 · edits · script / no_script_reason), or None when
        there is none (transport failure, configuration error, a fixer without propose_script — the reason is on the
        attempt)."""
        hints = [*self._match_hints.get(c.id, []), *self._sibling_hints(c)]
        other = "\n".join(v for k, v in st.others.items() if k != c.id)[:4000]
        self._mark(f"advisory hints for {c.id}")
        adv_hints, att.advisory_ids = self._advisory_hints(c)
        self._mark(f"merge-conflict hints for {c.id}")
        ref_notes, ref_files = self._merge_conflict_hints(att, c)
        if ref_files:
            self._ref_paths[id(att)] = {_norm_path(k) for k in ref_files}
        mode = step.mode if base is not None or step.mode == "new" else "new"
        if step.mode == "augment" and base is None:
            att.notes.append("augment without a usable base → asking for a new fix")
        att.script_mode = mode
        if mode == "augment":
            att.script_parent = int(base.version) or None
        # the stronger model on a stall, and from the second augment of a case on (v2.1i edits mode: from the third
        # ask on — the same rung as script mode's second augment)
        # edits 모드에는 보강이 없으므로 같은 자리(케이스의 세 번째 LLM 요청)부터 더 센 모델을 청한다
        escalate = st.stall_level >= self.cfg.loop.stall_limit or (mode == "augment" and step.augments_used >= 1) \
            or (self.edits_mode and int(getattr(step, "llm_used", 0) or 0) >= 2)
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        self._mark(f"library hints for {c.id}")
        lib_hints = self._library_hints(c)
        previous = self._previous(c, ladder=False)
        ctx = FixContext(phase=st.phase, iteration=st.iteration, stall_level=st.stall_level,
                         profile_summary=self._summary(), plan_notes=self.plan_notes, kb_hints=hints,
                         advisory_hints=adv_hints, previous_attempts=previous, other_errors_summary=other,
                         use_escalation_model=escalate, reference_notes=ref_notes, reference_files=ref_files,
                         script_mode=mode, case_id=step.case_id, signature_generic=sig,
                         script_base=base if mode == "augment" else None, library_hints=lib_hints)
        if self.edits_mode:                             # v2.1i: what the earlier tries edited instead of a base script
            ctx.fix_mode = "edits"
            ctx.previous_edits = self._previous_edits(previous)
        self._mark(f"fixer.propose_script for {c.id} ({mode}: prompt packing + LLM call)")
        try:
            proposal = self.fixer.propose_script(self.ws, c, ctx)
        except ScriptFixerMissing as e:
            self._fail(att, str(e))
            self._no_script_fixer(att, c, str(e))
            return None
        except Exception as e:
            # 실패한 호출도 부른 것이므로 예산에 센다
            self.budget.charge_llm(1)
            log.warning("fixer.propose_script failed for %s: %s", c.id, e)
            self._fail(att, f"LLM fixer failed: {type(e).__name__}: {e}")
            if is_llm_transport_error(f"llm-error: {type(e).__name__}: {e}"):
                self._llm_transport_failure(att, c, f"{type(e).__name__}: {e}")
            return None
        self.budget.charge_llm(max(1, proposal.llm_usage.calls))
        self._mark(f"LLM answered for {c.id}")
        att.model, att.prompt_hash, att.llm_usage = proposal.model, proposal.prompt_hash, proposal.llm_usage
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
                             f"escalation model requested (ask {int(step.llm_used) + 1} of the case)" if self.edits_mode
                             else f"escalation model requested (augment {step.augments_used + 1} of the case)")
        return proposal

    def _previous_edits(self, previous: list[FixAttempt]) -> dict[str, str]:
        """v2.1i edits 모드: 앞 시도마다(id → 글) 결과·실패 이유, 지금 트리에 남았는지(개선·성공이 아니면 다음
        시도 전에 되돌렸다), 적용한 diff 또는 적용 못 한 edits. 답이 없던 시도(전송 실패)는 뺀다. 보강의
        script_base 가 하던 일을 스크립트 없이 한다.

        v2.1i edits mode: one text per earlier try (attempt id → text) — its result and why it did not resolve the
        case, whether its changes are still in the tree (a try that was not improved / build_ok was rolled back
        before this ask — the loop's rule), the applied diff or, when nothing applied, the edits it asked for (both
        capped). Tries without an answer (a transport failure) are left out."""
        out: dict[str, str] = {}
        for a in previous:
            ans = self._answers.get(id(a))
            edits = list(getattr(ans, "edits", None) or []) if ans is not None else []
            diff = self._diffs.get(id(a), "") if a.applied else ""
            if not edits and not diff:
                continue
            # 이유 한 줄: 채점 결과 + 적용 오류·롤백·채점 메모(_run_reason; 개선·성공이면 결과 이름만)
            # (the loop's own "script ignored" note stays out: no script wording in an edits-mode prompt)
            why = "; ".join(x for x in self._run_reason(a).split("; ") if x != EDITS_SCRIPT_IGNORED)
            if not why.startswith(a.outcome):
                why = f"{a.outcome}: {why}" if why else a.outcome
            lines = [f"{self._edits_label(a)} (attempt {a.id}) → {why}"]
            if diff:
                failed_rollback = any(x.startswith("rollback before the next attempt failed") for x in a.notes)
                kept = a.outcome in SCRIPT_KEEP or failed_rollback or _kept_partial(a)
                lines.append("its changes are still in the tree" if kept else
                             "its changes were rolled back before this request (not in the tree now)")
                text = diff if len(diff) <= PREVIOUS_EDIT_CHARS else diff[:PREVIOUS_EDIT_CHARS] + _TRUNCATED_MARK
                lines += ["applied diff:", text]
            else:
                lines.append("edits it asked for (not applied):")
                lines += [_edit_line(e) for e in edits]
            body = "\n".join(lines)
            out[a.id] = body if len(body) <= 2 * PREVIOUS_EDIT_CHARS else body[:2 * PREVIOUS_EDIT_CHARS] + _TRUNCATED_MARK
        return out

    def _store_script(self, att: FixAttempt, c: ErrorCluster, step: Any, ans: Any, code: str, *,
                      check_duplicate: bool = True) -> bool:
        """LLM 스크립트를 KB 케이스에 저장한다. 실행할 스크립트는 *돌리기 전에* 저장한다(실패해도 계보가
        남도록), edits 답의 스크립트는 수정 옆에 unverified 로. KB 가 없으면 저장 없이 v0. 보강본은 v2.1e
        계보(``parent``, 만든 이유 ``reason`` = "v1: <부모의 실패 이유>")를 남긴다. False = 돌리지 말 것(같은
        코드의 버전이 이 실행에서 이미 돌았음).

        Store an LLM script in the KB case (`kb.cases.add_script`, source = the try's): a script run stores it
        BEFORE it runs (failures keep their lineage), an edits answer stores it `unverified` next to the fix. No KB →
        unstored (v0). False = do not run it (the same version already ran in this run). v2.1e: an augment's version
        records its `parent` and the `reason` it was made (`v1: <the parent's failure in words>`)."""
        if self.edits_mode:                             # v2.1i: no script is stored (nor run) in edits mode
            return False
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
            if not note and meta is None and att.script_parent:      # a stored version of an earlier run
                info = store.find(step.case_id)
                meta = next((x for x in (getattr(info, "scripts", None) or [])
                             if int(getattr(x, "version", 0) or 0) == att.script_parent), None)
            if not note and meta is not None:
                note = str(getattr(meta, "last_failure", "") or getattr(meta, "last_result", "") or "")
            note = note or "augment requested"
            lineage["reason"] = f"v{att.script_parent}: {note}" if att.script_parent else note
        # the adapter (sbmig.loop.entries.CaseScripts) drops the v2.1e keywords an older store does not take
        cs = store.add_script(step.case_id, sig, code, source=att.source or "llm-new", run_id=self.ws.run_id,
                              project=self._project(), analysis=getattr(ans, "analysis", "") or "",
                              solution=getattr(ans, "solution", "") or "", **lineage)
        if cs is None:
            att.notes.append("fix script not stored (kb.cases.add_script failed): v0")
            return True
        att.script_version = int(getattr(cs, "version", 0) or 0)
        # the store logs `스크립트 v1 저장 (unverified, llm-new) → cases/<id>/fix_v1.py` itself (stage kb)
        log.debug("stored as v%d (%s) → %s", att.script_version, getattr(cs, "status", "unverified"),
                  script_path(step.case_id, cs))
        ran = next((a for a in _same_cluster(c, self.history, self.blocked.canonical_file)
                    if a.decision.kind == DecisionKind.SCRIPT and a.script_version == att.script_version
                    and att.script_version and a.script_result is not None), None)
        self.ws.journal({"event": "script_stored", "case": step.case_id, "version": att.script_version,
                         "mode": att.script_mode, "source": att.source, "attempt": att.id,
                         "status": getattr(cs, "status", ""), "duplicate_of": ran.id if ran is not None else None})
        if ran is not None and check_duplicate:
            self._fail(att, f"the LLM answered with v{att.script_version} again (identical code), which already "
                            f"ran in this run ({ran.id}: {ran.outcome}) — not run again")
            return False
        return True

    def _run_script(self, att: FixAttempt, c: ErrorCluster, code: str, st: "_Round") -> None:
        """스크립트를 프로젝트에 돌린다(``sbmig.script``: 정적 검사, ``python -I`` 별도 프로세스, 프로젝트
        폴더 안만, 타임아웃). 거부·오류·타임아웃 → 아무것도 적용 안 됨(작업 트리 복원, 부분 적용 없음), 성공 +
        변경 → 보고한 파일만 커밋.

        Run the script on the project (`errors` = the case's errors); rejected / error / timeout → nothing applied
        (working tree restored); ok + changes → commit."""
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
            except Exception as e:  # noqa: BLE001 - the runner says it never raises; stay defensive
                res = ScriptRunResult(status="error", error=f"script runner failed: {type(e).__name__}: {e}")
        att.script_result = res
        name = f"script v{v}" if v else "script (unstored)"
        if res.status != "ok":
            why = f"script {res.status}: {(res.error or '').strip()[:400] or 'no detail'}"
            self._fail(att, why)                   # no partial application: the working tree is restored
            if res.problems:
                att.notes.append("static check: " + "; ".join(res.problems[:5]))
            log.info("적용 실패 (%s %s): %s — 변경 없음", name, res.status, _first_sentence(res.error or "-", 240))
            return
        self._drop_reference_writes(att, pre)
        self._fill_versions(att, c, res, pre, st)      # v2.1l: version-less dependencies get theirs (same commit)
        self._mark(f"git commit for {c.id}")
        sha = self._commit(f"fix(script:{case_id}:v{v} {att.script_mode}): {c.id}", pre, paths=res.changed_files)
        if sha is None:
            att.outcome = "apply_failed"
            att.apply_errors.append("no file changed")
            log.info("적용 실패 (%s): 실행은 ok, 바뀐 파일 없음", name)
            return
        self._applied(att, c, st, pre, sha, name, list(res.changed_files))

    def _apply_edits(self, att: FixAttempt, c: ErrorCluster, edits: list, st: "_Round",
                     subject: str | None = None, apply_fn: Callable[[], ScriptRunResult] | None = None) -> None:
        """답의 ``<edits>`` 를 ``sbmig.script.apply_edits`` 로 적용(스크립트와 같은 가드: 프로젝트 폴더
        안만, 같은 거부 규칙). 실패 → 아무것도 적용 안 됨(작업 트리 복원), 변경 → 커밋
        ``fix(edits:<case> <source>)``(v2.1l 의 sbmig 버전 채움은 ``subject`` 로 ``fix(sbmig-version): …``).

        The answer's `<edits>` through `sbmig.script.apply_edits` (the helper API's guards: project folder only,
        same refusals as a script); failure → nothing applied (working tree restored); changes → commit (`subject`
        overrides the commit subject: v2.1l's `fix(sbmig-version): <case>`). v2.1m: `apply_fn` (sbmig's own pom change
        that edits the files itself, e.g. `apply_non_filtered_extensions`) replaces the edit applier; the rest —
        failure handling, commit of the files it reports, `_applied` — is the same."""
        pre = self.ws.head()
        self._mark(f"applying {len(edits)} edit(s) for {c.id}")
        if apply_fn is None:
            try:                                   # traceability only: the record of what changed is the diff
                from sbmig.script import edits_script
                log.debug("edits as a script:\n%s", edits_script(edits))
            except Exception:  # noqa: BLE001 - the sbmig.script package may not offer it
                pass
        with self._busy_with(f"edits for {att.script_case_id}"):
            try:
                if apply_fn is not None:           # sbmig's own pom change (v2.1m): it edits the files itself
                    res = apply_fn()
                elif self.edit_applier is not None:
                    res = call_edit_applier(self.edit_applier, self.ws, edits, self.cfg, list(c.errors))
                else:                              # its own runner (no script.max_code_chars cap for a `write`)
                    from sbmig.script import apply_edits
                    res = apply_edits(self.ws, edits, cfg=self.cfg, errors=list(c.errors))
            except Exception as e:  # noqa: BLE001 - ImportError / a broken applier: nothing applied
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
        self._fill_versions(att, c, res, pre, st)      # v2.1l: version-less dependencies get theirs (same commit)
        self._mark(f"git commit for {c.id}")
        if subject is None:
            subject = (f"fix({self._edits_label(att)}): {c.id}" if self.edits_mode else
                       f"fix(edits:{att.script_case_id} {att.source or 'llm'}): {c.id}")
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
        """커밋된 변경 정리: 시도의 파일 목록, diff(DEBUG 로그엔 전체, FixTry 엔 잘라서), ``적용(…)`` 줄, 수정
        기록마다 ``수정: …`` INFO 줄(v2.1e: as-is / to-be 조각 포함), pom 을 바꿨으면 v2.1d 되읽기, 바뀐
        파일이 많으면 경고. 그리고 채점 대기에 넣는다.

        A committed change: the attempt's files, the diff (DEBUG, full; the FixTry, capped), the `적용(…)` line and
        one `수정: <path> <lines> — <change>` line per note (v2.1b: the LLM's descriptions of the edits it applied /
        the script it wrote, lines from the diff; a reused stored script has no answer → notes from the diff)."""
        att.applied, att.commit = True, sha
        try:
            files = self._changed_between(pre, sha)
        except Exception:  # pragma: no cover - git trouble: fall back to the fix's own account
            files = list(reported)
        att.changes = [FileChange(path=f, op="modify" if self.ws.exists(f) else "delete") for f in files]
        stats = self._numstat(pre, sha)
        try:
            diff = self.ws.diff(pre, sha)
        except Exception as e:  # noqa: BLE001 - the commit is there; only the text is missing
            diff = ""
            log.debug("diff %s..%s unavailable: %s", pre[:10], sha[:10], e)
        self._diffs[id(att)] = diff
        shown = [f"{p} (+{a} −{d})" for p, a, d in stats] or files
        log.info("적용(%s): %s, 커밋 %s", how, ", ".join(shown[:8]) + (f", … (+{len(shown) - 8})" if len(shown) > 8
                                                                       else ""), sha[:7])
        llm_notes = answer_notes(self._answers.get(id(att)), edits=how == "edits")
        try:                                           # v2.1e: as-is / to-be hunks, errors.snippet_lines per side
            notes = try_notes(llm_notes, diff,
                              max_lines=int(getattr(getattr(self.cfg, "errors", None), "snippet_lines", 0) or 0))
        except TypeError as e:                         # a try_notes without the keyword (before v2.1e)
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
        if pom_changed(files or reported):             # v2.1d: major upgrades / library replacements → 운영자 확인
            self._read_back(att, c, st, pre, sha, diff)
        warn = int(getattr(getattr(self.cfg, "script", None), "max_files_changed_warn", 0) or 0)
        if warn and len(files) > warn:
            note = (f"the fix changed {len(files)} files (more than script.max_files_changed_warn = {warn}) — "
                    "check its diff")
            att.notes.append(note)
            log.warning("%s", note)
        st.pending.append(_Pending(att, pre, sha))
        st.applied_any = True

    # ------------------------------------------------------------------ v2.1d dependency version policy
    def _read_back(self, att: FixAttempt, c: ErrorCluster, st: _Round, pre: str, sha: str, diff: str) -> None:
        """v2.1d 되읽기: 적용된 커밋의 pom 변경 중 메이저 업그레이드·라이브러리 교체를
        ``att.major_upgrades`` 에, 운영자 확인 가이드와 함께(답의 ``<verify>`` → 없으면 LLM 1회
        ``verify_guide`` → 그래도 없으면 sbmig 가 만든 것). 메이저가 바뀌어도 되돌리거나 차단하지 않는다 —
        빌드는 통과해야 하니까. 실패는 DEBUG 한 줄.

        The applied commit's pom changes (`sbmig.build.depchanges`): every major upgrade / library replacement
        becomes `att.major_upgrades` with the 운영자 확인 guide — the answer's `<verify>`, else ONE LLM call
        (`fixer.verify_guide`, `libraries.verify_guide_llm`), else sbmig's synthesized guide. Never reverts or
        blocks anything; a failure is a DEBUG line."""
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
        except Exception as e:  # noqa: BLE001 - the read-back is a report aid, never a reason to fail a try
            log.debug("dependency read-back for %s failed: %s: %s", att.id, type(e).__name__, e)

    def _verify_guide(self, att: FixAttempt, c: ErrorCluster, st: _Round, changes: list, diff: str
                      ) -> tuple[str, str]:
        """(가이드, "llm" | "sbmig"). 저장 스크립트 재사용(``kb-script``)은 LLM 을 부르지 않는다 — 재사용의
        장점(LLM 0회)을 지키려고 가이드는 sbmig 가 만든다.
        ``libraries.verify_guide_llm``·LLM 불가·예산도 본다.

        (guide, "llm" | "sbmig") for the try's major upgrades. A reused stored script (`kb-script`) costs no LLM
        call: its guide is synthesized (docs/INTERFACES.md v2.1d)."""
        ans = self._answers.get(id(att))
        text = str(getattr(ans, "verify", "") or "").strip() if ans is not None else ""
        if text:
            return text, "llm"
        fx = self._verify_fixer()
        if fx is not None and att.source != "kb-script" \
                and getattr(getattr(self.cfg, "libraries", None), "verify_guide_llm", True) \
                and not self.dry_run and not self.llm_unavailable and self.budget.llm_allowed():
            ctx = FixContext(phase=st.phase, iteration=st.iteration, profile_summary=self._summary(),
                             case_id=att.script_case_id)
            self._mark(f"fixer.verify_guide for {c.id}")
            got = ""
            try:
                try:
                    got = fx.verify_guide(self.ws, c, ctx, list(changes), diff, solution=att.solution or "")
                except TypeError as e:              # a VerifyGuideLike without the `solution` keyword
                    if "solution" not in str(e):
                        raise
                    got = fx.verify_guide(self.ws, c, ctx, list(changes), diff)
                got = str(got or "").strip()
            except Exception as e:  # noqa: BLE001 - the seam says it never raises; stay defensive
                log.debug("verify_guide failed for %s: %s: %s", c.id, type(e).__name__, e)
            self._charge_verify_usage(att, fx)
            if got:
                return got, "llm"
        return fallback_guide(changes, self._lang()), "sbmig"

    def _verify_fixer(self) -> Any:
        """``verify_guide`` 를 가진 fixer: fixer 자신, 또는 ``LazyFixer`` 뒤의 진짜 fixer(propose_script 가
        이미 만들었을 때만 — 가이드 하나 때문에 LLM 클라이언트를
        새로 만들지 않는다). None = sbmig 가 가이드를 만든다.

        The fixer that has `verify_guide` (VerifyGuideLike): the fixer itself, or the real one behind the
        orchestrator's `LazyFixer` proxy once `propose_script` created it; None = sbmig synthesizes the guide."""
        fx = self.fixer
        if callable(getattr(fx, "verify_guide", None)):
            return fx
        inner = getattr(fx, "_fixer", None) if isinstance(fx, LazyFixer) else None
        return inner if inner is not None and callable(getattr(inner, "verify_guide", None)) else None

    def _charge_verify_usage(self, att: FixAttempt, fx: Any) -> None:
        """verify_guide 호출의 사용량을 예산과 시도(FixTry 의 LLM 호출 수, 보고서 사용량)에 매긴다. 일찍
        실패했어도 요청은 했으므로 최소 1회.

        The verify_guide call's usage (`fixer.last_verify_usage`, an LLMUsage; a fixer without it = one call) on
        the budget and on the attempt (its try's `llm_calls`, the report's usage) — like `_ask_llm` does."""
        u = getattr(fx, "last_verify_usage", None)
        if not isinstance(u, LLMUsage):
            u = LLMUsage(calls=1)
        elif not u.calls:
            u = u.model_copy(update={"calls": 1})   # the call was asked for: charged once even when it failed early
        self.budget.charge_llm(max(1, int(u.calls or 0)))
        att.llm_usage = att.llm_usage.add(u)

    def _lang(self) -> str:
        """sbmig 가 만드는 가이드의 언어(``report.language``: ko | en, 기본 ko)."""
        lang = str(getattr(getattr(self.cfg, "report", None), "language", "") or "ko").lower()
        return lang if lang in ("en", "ko") else "ko"

    def _same_major_offer(self, ch: Any) -> str:
        """저장소 / 표가 제시했던 같은 메이저의 최신 jakarta 릴리스("" = 없음) — 경고 줄이 "정책상 메이저
        대신 이것이 있었다" 고 말하게 한다.

        The newest same-major jakarta release the repository / the table offered for `ch.key` ("" = none) — the
        WARNING line says so (the policy asked for it instead of the major upgrade)."""
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
        """v2.1d 프롬프트의 "Dependency versions" 절: 케이스 뒤의 라이브러리마다 표 항목과 저장소가 가진
        것(같은 메이저 jakarta ✓ / javax, 상위 메이저)과 마지막 줄의 판정(같은 메이저로 올려라 / 이 경우는
        메이저 허용). 규칙은 "메이저 금지, 같은 메이저의 jakarta 빌드로만" — LLM 이 추측하지 않고 사실을 보고
        고르게 한다. 라이브러리별로 실행 동안 캐시, 예외 없음. v2.1l: 맨 앞에 케이스 오류의 아티팩트마다 as-is
        빌드의 버전·새 부모 체인의 관리 버전 줄(``_version_facts``).

        FixContext.library_hints (the prompt's "Dependency versions" section): for the libraries behind the case
        (`_case_libraries`) the table entry and what the repository has (`RepositoryLookup.candidates` →
        `hint_lines`), cached per library for the run. v2.1l: the as-is / new-chain facts of every artifact of the
        case's errors come first (`_version_facts`). Never raises ([] / the facts on any problem)."""
        facts = self._version_facts(c) + self._resources_facts(c)   # v2.1l / v2.1m facts come first
        try:
            libs = self._case_libraries(c)
            out: list[str] = list(facts)
            for gav in libs:
                lines = self._library_hint_cache.get(gav.key)
                if lines is None:
                    lines = self._library_lines(gav.key, gav.version)
                    self._library_hint_cache[gav.key] = lines
                out += lines
            out = list(dict.fromkeys(x for x in out if x))
            if out:
                self.ws.journal({"event": "library_hints", "case": c.case_id or c.id,
                                 "keys": [g.key for g in libs], "lines": out})
            return out
        except Exception as e:  # noqa: BLE001 - hints never break a fix attempt
            log.debug("library hints for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return list(facts)

    def _case_libraries(self, c: ErrorCluster) -> list[GAV]:
        """케이스 뒤의 외부 라이브러리들(최대 ``libraries.max_artifacts_per_case``, g:a 당 하나): 오류의
        ``artifact``, 없으면 앞쪽 컴파일 오류의 소유 클래스·오류 줄에서 부른 클래스를 classpath 에서 찾아.
        사내 아티팩트와 프로젝트 클래스는 뺀다.

        The libraries behind the case (≤ `libraries.max_artifacts_per_case`, one per g:a): the errors' `artifact`,
        else the owner class of the first compile errors located on the module classpath (`ClassLocation.artifact`).
        In-house artifacts and project classes are left out."""
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
                names.append((loc.type_mismatch(e) or (None, None))[1] or "")     # the found type of a mismatch
            for fq in names:
                if not fq or fq in seen or fq.startswith(("java.", "javax.", "jakarta.")):
                    continue
                seen.add(fq)
                self._mark(f"locating {fq} for {c.id}")
                if take(loc.locate_class(self.ws, self.cfg, fq, c.module)):
                    return list(found.values())
            # the library class BEHIND the error (the class called on the error line — `LegacyRequestHelper` of a
            # `cannot access javax.servlet…` / `incompatible types` error): the same attribution the BLOCKED
            # registry uses (sbmig.build.classpath.locate_type_owner)
            if hasattr(loc, "locate_type_owner"):
                self._mark(f"locating the type owner of {e.file}:{e.line} for {c.id}")
                if take(loc.locate_type_owner(self.ws, self.cfg, e, c.module)):
                    return list(found.values())
        if not found:
            log.debug("library hints for %s: no external library located behind the first errors", c.id)
        return list(found.values())

    def _repository(self) -> Any:
        """실행의 저장소 조회기(Nexus·미러의 maven-metadata.xml). ``libraries.lookup`` 이 꺼졌거나 만들 수
        없으면 None — 그 뒤로는 실행 내내 끈다(WARNING 한 줄, 표만 사용).

        The run's `RepositoryLookup` (lazy; None when `libraries.lookup` is off or it cannot be built — then off
        for the rest of the run with one WARNING)."""
        if self._repo_lookup is not None or self._repo_lookup_off:
            return self._repo_lookup
        if not getattr(getattr(self.cfg, "libraries", None), "lookup", True):
            self._repo_lookup_off = True
            return None
        try:
            from sbmig.build.repo_versions import RepositoryLookup
            self._repo_lookup = RepositoryLookup(self.cfg)
        except Exception as e:  # noqa: BLE001 - no lookups: the prompt carries the table + the policy only
            self._repo_lookup_off = True
            log.warning("의존성 버전 조회 끔: 저장소 조회를 시작할 수 없음 (%s: %s) — 이 실행은 표만 사용",
                        type(e).__name__, str(e)[:200])
        return self._repo_lookup

    def _library_lines(self, key: str, current: str | None) -> list[str]:
        """라이브러리 하나의 프롬프트 줄들(표 항목 + 저장소 사실).
        찾은 것은 ``의존성 버전 조회: …`` INFO 줄로 남긴다.

        The prompt lines of one library (table entry + repository facts); logs what was found."""
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
            except Exception as e:  # noqa: BLE001 - the lookup says it never raises; stay defensive
                log.debug("repository lookup %s failed: %s: %s", key, type(e).__name__, e)
                rv = None
        if rv is not None:
            self._repo_facts[key] = rv
            log.info("%s", _versions_line(rv, key, current))
            try:
                from sbmig.build.repo_versions import hint_lines
                lines = [str(x) for x in (hint_lines(rv, entry) or []) if x]
                if lines:
                    return lines
            except Exception as e:  # noqa: BLE001
                log.debug("hint_lines for %s failed: %s: %s", key, type(e).__name__, e)
        if entry is not None:
            try:
                from sbmig.core.jakarta_libraries import library_hint_line
                line = str(library_hint_line(entry, current) or "")
                return [line] if line else []
            except Exception as e:  # noqa: BLE001
                log.debug("library_hint_line for %s failed: %s: %s", key, type(e).__name__, e)
        return []

    # ------------------------------------------------------------------ v2.1l dependency versions
    def _versions(self) -> VersionResolver:
        """이 루프의 버전 결정기(처음 쓸 때 두 캡처를 읽어 한 번 만든다; 저장소 조회기는 v2.1d 의 것을 같이 쓴다).

        The loop's `VersionResolver` (built once, lazily; the repository step reuses `_repository()`)."""
        if self._version_resolver is None:
            self._version_resolver = VersionResolver.for_workspace(self.ws, repository=self._repository)
        return self._version_resolver

    def _protected_version_keys(self) -> set[str]:
        """버전을 sbmig 가 채우지 않는 g:a: 사내 부모·역할 부모·필수·선택(별칭 포함)·``remove_when_present`` —
        재구성기가 건드리지 않는 것과 같은 목록(회사가 관리하는 좌표).

        The keys sbmig never fills a version for (the restructurer's protected set: in-house parent / role parents /
        mandatory / optional incl. aliases / `remove_when_present`)."""
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
        except Exception:  # noqa: BLE001 - a foreign config: the optional artifacts by their own keys
            keys |= {s.key for s in getattr(A, "optional", None) or []}
        keys |= {k for k in getattr(A, "remove_when_present", None) or [] if ":" in k}
        return keys

    def _pom_editor(self, rel: str) -> Any:
        """현재 트리의 pom 편집기(못 읽으면 None)."""
        try:
            from sbmig.restructure.pom_editor import PomEditor
            return PomEditor(self.ws.abs(rel))
        except Exception as e:  # noqa: BLE001 - an unparsable pom: nothing to fill there
            log.debug("pom %s not readable: %s: %s", rel, type(e).__name__, e)
            return None

    def _own_module_keys(self) -> set[str]:
        """프로젝트 자신의 모듈 좌표(g:a, ``${project.groupId}:a``) — 형제 모듈 의존성의 버전은 채우지 않는다."""
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
        """[pom, 프로젝트 안의 부모 pom, …] — ``<relativePath>``(없으면 ``../pom.xml``)를 따라 artifactId 가 맞는
        동안(10 단계까지). 빈 ``<relativePath/>``·프로젝트 밖·불일치에서 멈춘다.

        [pom, its in-project parent, …] following `<relativePath>` (default `../pom.xml`) while the parent's
        artifactId matches (≤ 10 levels)."""
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
                break                                # outside the project / a cycle / not here
            ped = self._pom_editor(target)
            if ped is None or ped.artifact_id() != parent.artifact_id:
                break
            out.append(target)
            cur = target
        return out

    @staticmethod
    def _managed_shape(ed: Any) -> tuple[str, tuple[str, ...]]:
        """pom 의 부모 좌표와 BOM import 들 — 시도가 sbmig 가 볼 수 없는 방식으로 버전 관리를 바꿨는지 비교하는
        데 쓴다(일반 dependencyManagement 항목은 직접 보이므로 여기 넣지 않는다).

        The pom's parent coordinates and BOM imports: what changes version management in a way sbmig cannot see
        (plain dependencyManagement entries are read directly by the local-chain check)."""
        p = ed.get_parent()
        parent = f"{p.group_id}:{p.artifact_id}:{p.version or ''}" if p is not None else ""
        boms = tuple(sorted(f"{g.key}:{g.version or ''}" for g in ed.managed_dependencies()
                            if (g.scope or "") == "import"))
        return parent, boms

    def _reported_missing(self, st: _Round | None, rel: str, aid: str | None, key: str) -> bool:
        """이 라운드의 빌드(또는 롤백으로 돌아온 오류)가 이 pom 의 g:a 를 '버전 없음' 으로 보고했는가.

        Did this round's build (or an error a rollback brought back) report g:a's version missing in this pom?"""
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
        """v2.1l: 적용된 스크립트 / edits 가 바꾼 pom 들의 버전 없는 ``<dependencies>`` 항목에 버전을 채운다
        (커밋 전, 그 시도의 커밋에 함께 들어간다). 시도가 *추가한* 의존성은 규칙 1–3(관리됨 → as-is → 저장소),
        원래 있던 것은 빌드가 '버전 없음' 이라고 보고한 경우만 규칙 2(as-is), 시도가 버전을 지운 것은 재구성 뒤
        캡처가 있을 때만 규칙 1–2(없으면 "부모가 관리" 라는 LLM 판단을 둔다). 규칙 1 은 프로젝트 안의
        dependencyManagement(그 pom·프로젝트 안 부모)도 본다 — 재구성 뒤 캡처가 없으면 그것뿐이다(비워 두면 다음
        빌드의 pom_invalid 로 시도 전체가 되돌려진다). 시도가 부모·BOM import 를 바꾼 pom(과 그 아래)은 건드리지
        않는다.
        사내·필수·선택·제거 대상·자기 모듈·system scope 는 제외. 예외 없음, 채운 것이 없으면 조용하다.

        Fill the versions of the `<dependencies>` entries without `<version>` in the poms the applied script / edits
        changed (before the commit; the pom joins the attempt's commit): a dependency the attempt ADDED (absent from
        the pom at `pre`) by steps 1–3; one that was already there only when the build reported its version missing,
        by step 2; one whose version the attempt dropped by steps 1–2, and only with the post-restructure capture
        (without it the LLM's "the parent manages it" stands — an as-is version could be an old one). Step 1 also counts a `<dependencyManagement>` entry of the pom or an in-project parent (the tree
        as the attempt left it); without the post-restructure capture that is all step 1 sees — an added dependency
        left empty would come back as pom_invalid and the structural-regression rule would revert the whole try.
        A pom whose parent or BOM imports the attempt changed (or one below such a pom) is left alone. Protected
        keys, own modules and `system` scope are skipped. Returns [(pom, key, version, source)]. Never raises."""
        try:
            return self._fill_versions_unsafe(att, c, res, pre, st)
        except Exception as e:  # noqa: BLE001 - a version fill never fails an attempt
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
                reshaped.add(p)          # a new pom, or the attempt changed how its versions are managed
        r = self._versions()
        protected = self._protected_version_keys()
        own: set[str] | None = None
        filled: list[tuple[str, str, str, str]] = []
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
            local: set[str] = set()                  # managed inside the project (this pom or an in-project parent)
            for q in chain:
                qed = ed if q == p else self._pom_editor(q)
                if qed is not None:
                    local |= {g.key for g in qed.managed_dependencies()}
            for el in ed.dependency_elements(False):
                g = ed.gav_of(el)
                key = g.key
                if g.version or not g.group_id or not g.artifact_id or "${" in key:
                    continue
                if own is None:
                    own = self._own_module_keys()
                if key in protected or key in own or (g.scope or "") == "system" or key in local:
                    continue
                if key not in pre_versions:          # added by this attempt: steps 1–3
                    # 재구성 뒤 캡처가 없으면 1단계는 프로젝트 안 관리(위 local)만 본다 — 비워 두면 다음 빌드의
                    # pom_invalid 가 구조적 악화로 시도 전체를 되돌리므로 as-is / 저장소 버전을 적는다
                    v, src = r.resolve(key, module, allow_repository=True)
                elif pre_versions[key]:              # the attempt dropped its version ("the parent manages it")
                    # 새 체인을 모르면 LLM 의 판단을 둔다 — as-is 버전(예: 옛 Boot 2 버전)을 되살리면 더 나쁘다
                    if not r.has_after:
                        continue
                    v, src = r.resolve(key, module)              # steps 1–2
                elif self._reported_missing(st, p, aid, key):   # already there, the build says missing: step 2
                    v, src = r.resolve(key, module, check_managed=False)
                else:                                # already there and resolved by the last build: managed
                    continue
                if not v:
                    continue
                ed.set_ref_version(el, v)
                filled.append((p, key, v, src))
            if ed.changed:
                ed.save()
        for p, key, v, src in filled:
            how = VERSION_SOURCE_TEXT.get(src, src)
            att.notes.append(f"version filled by sbmig: {key} {v} in {p} ({VERSION_SOURCE_NOTE.get(src, src)})")
            log.info("버전 채움: %s → %s (%s) — %s", key, v, how, p)
            self.ws.journal({"event": "version_filled", "case": case, "attempt": att.id, "key": key, "version": v,
                             "source": src, "pom": p})
        return filled

    def _declaring_pom(self, e: BuildError) -> str | None:
        """'버전 없음' 오류의 g:a 가 버전 없이 선언된 pom(오류의 pom → 같은 artifactId 의 모듈 pom, 각각의 프로젝트
        안 부모까지). 못 찾으면 None.

        The pom that declares the error's g:a without a version (the error's pom, else the module pom with that
        artifactId; each with its in-project parents); None when not found."""
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
        """누락 버전 케이스의 결정적 시도 계획: 오류마다 (pom, g:a) 쌍을 as-is 빌드 → 저장소 순으로 푼다(규칙 2–3;
        빌드가 '없음' 이라 했으니 규칙 1 은 건너뜀). 못 찾은 쌍은 바로 '본 것' 으로 기억하고(사다리를 두 번 늦추지
        않게), 찾은 쌍은 돌려주기만 한다 — 적용된 뒤에야 기억한다(``_version_attempt``). 돌려주는 것: ([(pom, g,
        a, 버전, 출처)], 못 찾은 것들, 새로 본 쌍이 있었나, 찾은 쌍들).

        The deterministic try's plan for a missing-version case: each (pom, g:a) of its errors not tried yet,
        resolved by steps 2–3. A pair nothing was found for is remembered at once (the ladder is not delayed twice);
        a resolved pair is only returned — `_version_attempt` remembers it once the edit applied. Returns
        ([(pom, g, a, version, source)], what stayed unknown, whether any pair was new, the resolved pairs)."""
        errs = [e for e in c.errors if e.signature_generic == MISSING_VERSION_SIG and e.artifact is not None
                and e.artifact.group_id and e.artifact.artifact_id]
        if not errs:
            return [], [], False, []
        # 쌍 = 오류가 가리키는 (pom 또는 모듈, g:a) — 이미 본 쌍은 다시 풀지 않는다(같은 답이 나올 뿐)
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
            if pair in seen:                         # the same pair twice in the case
                continue
            seen.add(pair)
            pom = self._declaring_pom(e)
            if key in protected or key in own:
                self._pre_tried.add(pair)            # nothing to do for it: remembered
                unknown.append(f"{key} (company / own module: not filled by sbmig)")
                continue
            if pom is None:
                self._pre_tried.add(pair)
                unknown.append(f"{key} (no pom declares it without a version)")
                continue
            v, src = r.resolve(key, self._coords(self._pom_editor(pom)), allow_repository=True, check_managed=False)
            if not v:
                self._pre_tried.add(pair)            # nothing found: the ladder goes on, once
                unknown.append(key)
                continue
            pairs.append(pair)
            if not any(x[0] == pom and f"{x[1]}:{x[2]}" == key for x in plan):
                plan.append((pom, e.artifact.group_id, e.artifact.artifact_id, v, src))
        return plan, unknown, bool(seen), pairs

    def _pre_tries(self) -> tuple[_PreTry, ...]:
        """LLM 0회 사전 시도 목록(순서대로 첫 번째로 해당하는 것 하나): v2.1l 누락 버전, v2.1m 리소스 필터링.

        The zero-LLM pre-tries, in order (the first that matches a case is the one): v2.1l versions, v2.1m resources."""
        return (_PreTry(SBMIG_VERSION_SOURCE, self._version_targets, self._version_attempt),
                _PreTry(SBMIG_RESOURCES_SOURCE, self._resources_targets, self._resources_attempt))

    def _pre_try_for(self, c: ErrorCluster, d: TriageDecision) -> tuple[_PreTry | None, list[str]]:
        """이 케이스에 LLM 0회 사전 시도를 먼저 해 볼까: 트리아지가 SCRIPT 를 정했거나 LLM 이 없어서(불가·예산
        소진) 막혔고, 사전 시도 하나가 해당할 때 → (그 시도, 손댈 pom). 사람이 고정한 차단·이미 차단된 오류는
        존중한다. 아니면 (None, []).

        The pre-try for this case and the poms it would edit, when the triager gave a SCRIPT decision or blocked it
        only for the LLM (unavailable / budget exhausted); else (None, [])."""
        if self.dry_run:
            return None, []
        if d.kind != DecisionKind.SCRIPT:
            m = _BLOCKED_RAT_RE.search(d.rationale or "") if d.kind == DecisionKind.BLOCKED else None
            if m is None or m.group("r") not in (BlockReason.LLM_UNAVAILABLE.value, BlockReason.BUDGET_EXHAUSTED.value):
                return None, []
        for pre in self._pre_tries():
            try:
                poms = pre.targets(c)
            except Exception as e:  # noqa: BLE001 - a broken matcher only means "no pre-try"
                log.debug("pre-try %s for %s: %s: %s", pre.source, c.id, type(e).__name__, e)
                poms = None
            if poms is not None:
                return pre, poms
        return None, []

    def _version_targets(self, c: ErrorCluster) -> list[str] | None:
        """누락 버전 케이스면 오류의 pom 들(없으면 빈 목록), 아니면 None."""
        if not any(e.signature_generic == MISSING_VERSION_SIG for e in c.errors):
            return None
        return list(dict.fromkeys(_norm_path(e.file) for e in c.errors
                                  if e.signature_generic == MISSING_VERSION_SIG and e.file))

    def _resources_targets(self, c: ErrorCluster) -> list[str] | None:
        """v2.1m: 리소스 / war 플러그인의 필터링 복사 실패 케이스면 고칠 pom 들(``reactive_targets``), 아니면 None.
        정규식으로 먼저 걸러 pom 을 읽는 일은 해당 케이스에서만 한다.

        The poms `reactive_targets` picks for a resources / war plugin filtering failure; None when it is not one
        (a regex pre-check keeps the pom parsing to such cases)."""
        from sbmig.restructure.resources_filtering import REACTIVE_RE, reactive_targets
        errs = list(c.errors)
        if not any(REACTIVE_RE.search(f"{e.message or ''}\n{e.raw or ''}") for e in errs):
            return None
        return list(reactive_targets(self.ws, errs)) or None

    def _pre_try_deferral(self, st: _Round, poms: list[str]) -> tuple[list[str], str]:
        """사전 시도를 다음 빌드까지 미루게 하는 (케이스들, "pom" | "files"): 이번 라운드에 다른 케이스의 수정이 손댈
        pom(또는 루트 pom — 모든 모듈의 부모 체인)을 바꿨거나 그 pom 위에서 돌았으면 기다린다. 한 트리에 두 시도가
        겹쳐 채점되거나, 앞 시도의 되돌리기(git revert)가 이 시도의 줄과 충돌하지 않게.

        (case ids, "pom" | "files") that make a pre-try wait for the rebuild: a fix of another case changed one of the
        poms it would edit (or the root pom — every module's chain) in this round, or ran on it. Two tries are never
        scored on one pom, and an earlier try's `git revert` never conflicts with this one's hunk."""
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
        """LLM 0회 사전 시도 한 번의 공통 순서: 결정·시도 기록 → ``_begin_try`` → ``_apply_edits``(edits, 또는 파일을
        직접 고치는 ``apply_fn`` — 같은 커밋·채점·롤백 길) → 키 정리(적용된 것만 '본 것', 적용 실패는
        ``PRE_TRY_APPLY_FAILURES`` 번까지 다시) → 미루기 기록 → 기록. 적용됐으면 True.

        The common sequence of one zero-LLM pre-try: decision + attempt (source, script_mode new, no stored script),
        `_begin_try`, `_apply_edits` (edits, or `apply_fn` that edits the files itself — same commit / scoring /
        rollback path, commit `fix(<source>): <cluster id>`), the keys settled (remembered once applied; an apply
        failure is retried up to `PRE_TRY_APPLY_FAILURES` times), the deferral bookkeeping, the record."""
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
        # 적용된 키만 '본 것' — 적용 실패(applier 예외, 커밋 실패, Windows 의 일시적 파일 잠금)는 다음 라운드에
        # 다시 해 본다(키마다 PRE_TRY_APPLY_FAILURES 번까지)
        for key in keys:
            if att.applied:
                self._pre_tried.add(key)
            else:
                self._pre_failures[key] += 1
                if self._pre_failures[key] >= PRE_TRY_APPLY_FAILURES:
                    self._pre_tried.add(key)
        if att.applied:                                 # its poms: the module's compile cases wait (deferral)
            for ch in att.changes:
                for path in (ch.path, ch.new_path):
                    if path and PurePosixPath(_norm_path(path)).name == "pom.xml":
                        st.poms.setdefault(_norm_path(path), c.id)
        self._seconds[id(att)] = time.monotonic() - t0
        self._record(pr, att)
        self._sync_try(att)
        return att.applied

    def _version_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """v2.1l 누락 버전 케이스의 결정적 시도(LLM 0회, 예산 그대로): 계획한 버전을 ``set_dependency_version``
        편집으로 ``_apply_edits`` 와 같은 길(가드·커밋·채점·롤백)로 적용한다. 출처 ``sbmig-version``, 모드 new,
        저장 스크립트 없음, 커밋 ``fix(sbmig-version): <case>``. 적용됐으면 True(이번 라운드의 케이스 시도는 이것),
        아니면 False — 같은 라운드에서 평소 사다리가 이어진다.

        The deterministic try of a missing-version case (no LLM call, the budget untouched): the planned versions as
        `set_dependency_version` edits through `_apply_edits` (same guards, commit, scoring, rollback); source
        `sbmig-version`, script_mode `new`, no stored script, commit `fix(sbmig-version): <case>`. True when it
        applied (the case's try of this round), False → the normal ladder goes on in the same round."""
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
            analysis=("the build reports dependency versions missing: the new parent chain does not manage "
                      + ", ".join(f"{g}:{a}" for _p, g, a, _v, _s in plan)),
            solution="sbmig writes the versions the as-is build used (else the newest release in the repository) — "
                     "no LLM call")

    def _resources_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """v2.1m 리소스 필터링 케이스의 결정적 시도(LLM 0회): ``binary_resource_plan(errors=…)`` 의 확장자를
        ``apply_non_filtered_extensions`` 로 maven-resources-plugin(필요하면 maven-war-plugin)의
        ``nonFilteredFileExtensions`` 에 적는다 — ``_apply_edits`` 와 같은 커밋·채점·롤백 길, 출처
        ``sbmig-resources``, 커밋 ``fix(sbmig-resources): <cluster id>``. (pom, 확장자 묶음)마다 실행당 한 번.
        추가할 것이 없거나 이미 해 본 것뿐이면 False(평소 사다리). 확장자 없는 바이너리 파일은 시도 메모와
        인수인계 후속 조치로.

        The deterministic try of a resources / war plugin filtering case (no LLM call): the extensions of
        `binary_resource_plan(ws, cfg, errors=…)` written by `apply_non_filtered_extensions` through the
        `_apply_edits` path (commit `fix(sbmig-resources): <cluster id>`, scoring, rollback), once per (pom, extension
        set) per run. Nothing to add / only tried ones → False (the normal ladder). `plan.followups` (binaries without
        an extension) → the attempt's notes and the run's Handover follow-ups."""
        from sbmig.restructure.resources_filtering import apply_non_filtered_extensions, binary_resource_plan
        plan = binary_resource_plan(self.ws, self.cfg, errors=list(c.errors))
        for fu in plan.followups:
            self._followup(fu)
        keys = {pom: (SBMIG_RESOURCES_SOURCE, pom, ",".join(exts))
                for pom, exts in plan.extensions_by_pom.items() if exts}
        fresh = [pom for pom, k in keys.items() if k not in self._pre_tried]
        seen = (SBMIG_RESOURCES_SOURCE, c.case_id or c.id, "")          # "nothing to add", journaled once
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
        """v2.1m 프롬프트 사실: 리소스 필터링 케이스면 계획이 찾은 것 — pom 마다 적을 확장자(``plan.lines()``)와
        필터링되는 바이너리 파일들(``RESOURCE_FACTS_FILES`` 개까지). 사전 시도가 적용되지 못했을 때 LLM 이 본다.
        예외 없음.

        The facts of a resources / war plugin filtering case: the plan's lines (extensions per pom) and the filtered
        binary files it found. Never raises."""
        try:
            if self._resources_targets(c) is None:
                return []
            from sbmig.restructure.resources_filtering import binary_resource_plan
            plan = binary_resource_plan(self.ws, self.cfg, errors=list(c.errors))
            out = [f"binary resources under filtering: {ln} (nonFilteredFileExtensions)" for ln in plan.lines()]
            out += [f"binary file under resource filtering: {f}" for f in plan.files[:RESOURCE_FACTS_FILES]]
            return out
        except Exception as e:  # noqa: BLE001 - facts never break a fix attempt
            log.debug("resource facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _version_facts(self, c: ErrorCluster) -> list[str]:
        """v2.1l 프롬프트 사실("Dependency versions" 절의 앞부분): 케이스 오류의 아티팩트마다(사내 것도) as-is
        빌드가 쓴 버전(``as-is build: g:a <v> (managed by …)`` / ``… not declared``)과 새 부모 체인이 관리하는
        버전(``new parent chain: manages g:a at <v>``). 캡처가 없으면 그 줄은 없다(모르는 것을 '없음' 이라고 하지
        않는다). 예외 없음.

        The as-is / new-chain facts for every artifact of the case's errors (in-house ones too); a capture that is
        missing gives no line (unknown is not "not declared"). Never raises."""
        try:
            mods: dict[str, str | None] = {}
            for e in c.errors:
                a = e.artifact
                if a is not None and a.group_id and a.artifact_id and a.key not in mods:
                    mods[a.key] = e.module
            if not mods:
                return []
            r = self._versions()
            if not (r.has_asis or r.has_after):
                return []
            out: list[str] = []
            for key, module in list(mods.items())[:VERSION_FACTS_MAX]:
                if r.has_asis:
                    v, how = r.asis_fact(key, module)
                    out.append(f"as-is build: {key} {v} ({how})" if v else f"as-is build: {key} not declared")
                if r.has_after:
                    mv = r.managed_version(key, module)
                    if mv:
                        out.append(f"new parent chain: manages {key} at {mv}")
            return out
        except Exception as e:  # noqa: BLE001 - facts never break a fix attempt
            log.debug("version facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _numstat(self, a: str, b: str) -> list[tuple[str, int, int]]:
        """두 커밋 사이 파일별 (경로, 추가 줄, 삭제 줄). 바이너리는 0 / 0, 이름 바꾸기는 새 경로.

        (path, added, removed) per file between two commits (`git diff --numstat -z`; binary files 0 / 0)."""
        try:
            out = self.ws._git("diff", "--numstat", "-z", a, b, check=False).stdout or ""   # noqa: SLF001
        except Exception:  # noqa: BLE001 - a stand-in workspace without git
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
            if not path and i + 2 < len(toks):             # a rename: "<add>\t<rem>\t" NUL old NUL new
                path = toks[i + 2]
                i += 3
            else:
                i += 1
            res.append((path, int(add) if add.isdigit() else 0, int(rem) if rem.isdigit() else 0))
        return res

    def _drop_reference_writes(self, att: FixAttempt, pre: str) -> None:
        """프롬프트가 보여 준 병합 충돌 참고본은 빌드 대상이 아니다(실행 디렉터리에 있음). 수정이 그 경로에
        파일을 썼다면 쓰레기를 프로젝트에 넣은 것 → 커밋 전에
        되돌린다("참고본은 절대 고칠 대상이 아니다", round 9).

        The merge-conflict reference copies the prompt showed are not in the build (they live under the run dir):
        a fix that wrote a file at such a path wrote junk into the project — undone before the commit (round 9:
        "never a patch target")."""
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
        except Exception as e:  # noqa: BLE001 - best effort; the diff shows what stayed
            log.warning("could not undo writes to reference copies for %s: %s", att.id, e)
            return
        if hit:
            att.notes.append(f"dropped {len(hit)} change(s) aimed at a read-only reference copy: {', '.join(hit[:3])}")

    def _credit_script(self, att: FixAttempt) -> None:
        """저장된 스크립트 버전의 실적 기록(``kb.cases.credit``): 시도마다 최종 결과로 한 번(판정 불가였던
        것은 되돌린 뒤 재채점되면 한 번 더). 그 버전의 *실행* 이 결과를 정한 경우만 — edits 옆에 저장만 된
        스크립트는 돌지 않았으니 실적 없음. v2.1e: 실행 기록(라운드·시도 번호·실패 이유·오류 수·파일)도 함께.
        저장 안 된 v0·드라이런·KB 없음 → 아무것도 안 함.

        `kb.cases.credit(case, version, result)` once per try and final outcome (an inconclusive one is credited
        again when a revert rebuild scores it) — only for a stored script whose RUN decided the outcome (a script
        stored next to applied edits never ran: no credit). Unstored (v0) scripts / dry run / no KB → nothing."""
        if self.dry_run or self.edits_mode or att.decision.kind != DecisionKind.SCRIPT or not att.script_version \
                or not att.script_case_id or att.outcome == "pending" or not self._script_ran.get(id(att)):
            return
        done = self._credited.get(id(att))
        if done is not None and done != "inconclusive":
            return
        if not self.cases.active:
            return
        # v2.1e: the run's record beside the counts — the try (round, n), why it failed, error counts, files
        _cid, n, rnd = self._tries.get(id(att), (att.script_case_id, 0, 0))
        reason = "" if att.outcome in ("improved", "build_ok") else self._run_reason(att)
        run = {"reason": reason, "round": int(rnd or 0), "n": int(n or 0), "errors_before": att.errors_before,
               "errors_left": att.errors_after, "errors_new": len(att.introduced_errors),
               "files": list(dict.fromkeys(ch.new_path or ch.path for ch in att.changes))}
        # the adapter (sbmig.loop.entries.CaseScripts) drops the v2.1e keywords an older store does not take
        if not self.cases.credit(att.script_case_id, att.script_version, att.outcome, project=self._project(),
                                 run_id=self.ws.run_id, **run):
            return
        self._credited[id(att)] = att.outcome
        self.ws.journal({"event": "script_credit", "case": att.script_case_id, "version": att.script_version,
                         "attempt": att.id, "outcome": att.outcome, "reason": reason})

    def _run_reason(self, att: FixAttempt) -> str:
        """채점된 시도가 왜 케이스를 못 풀었는지 한 줄(v2.1e ScriptRun.reason, 보강본의 CaseScript.reason).
        사람이 case.json 만 보고 "v1 은 어디서 깨졌고 왜 v2 가 생겼나" 를 알게 하려는 것. 개선·성공이면 "".

        Why a scored try did not resolve its case, one line (v2.1e ScriptRun.reason / an augment's
        CaseScript.reason; "" for improved / build_ok): `no_change: 2 of 2 targeted error(s) remained — <the first
        one>`, else the try's notes (regressed / partial / inconclusive / script error / timeout / rejected …) and
        the first new error of a regression."""
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
        except Exception:  # noqa: BLE001 - the KB package is part of sbmig; stay defensive
            return "; ".join(x for x in (head, note) if x) or att.outcome

    def _credit_scripts(self, pr: PhaseResult, attempts: list[FixAttempt]) -> None:
        """여러 시도의 실적 기록(``_credit_script`` 를 차례로)."""
        for a in attempts:
            self._credit_script(a)

    def _no_script_fixer(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """``propose_script`` 없는 tier-2 fixer: 이 실행에서는 LLM 스크립트를 쓸 수 없다(저장본은 재사용).
        기반 시설 문제가 아니므로 ``infra:llm`` 은 아니고, ``fixer-without-scripts`` 플래그 + 후속 조치 한 줄,
        해당 케이스는 llm_unavailable 차단.

        A tier-2 fixer without `propose_script`: no LLM script can be written in this run (stored versions are
        still reused). Not an infrastructure problem (no `llm-unavailable` / `infra:llm`): flag
        `fixer-without-scripts` + one follow-up."""
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
        """새 FixAttempt(id ``<phase>-<build>-<n>``). 대상 오류를 함께 담고(채점·오류 목록용), 지금 HEAD 를
        ``base_commit`` 으로 기록한다 — ``base_commit..commit``
        이 이 시도가 적용한 정확한 코드(롤백의 근거)."""
        att = FixAttempt(id=f"{phase.value}-{iteration}-{n}", phase=phase, iteration=iteration, cluster_id=c.id,
                         decision=d, errors_before=build.error_count, cluster_signatures=list(c.signatures),
                         cluster_errors=list(c.errors))   # scoring and the error list need the errors
        try:   # `git diff base_commit..commit` = the exact code the attempt applied (rollback)
            att.base_commit = self.ws.head()
        except Exception as e:  # noqa: BLE001 - a workspace without git: no diff, nothing else changes
            log.debug("base commit for %s unavailable: %s", att.id, e)
        return att

    def _record(self, pr: PhaseResult, att: FixAttempt) -> None:
        """시도를 단계 결과와 실행 이력(사다리·한도의 근거)에 넣고 저널에 한 줄."""
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
        """시도를 커밋한다: 스크립트 / edits 가 보고한 파일만(v2.1b) — 직전 빌드가 lib/ 에 복사한 jar 는
        수정이 아니다. 바뀐 것이 없으면 None.

        Commit an attempt: only the files its script / edits reported (`paths`, v2.1b) — a jar the last build
        copied into `lib/` is not part of the fix; None when nothing changed."""
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
        """적용 실패로 표시하고 커밋 안 된 변경을 버린다(부분 적용을 남기지 않는다)."""
        att.outcome = "apply_failed"
        att.apply_errors.append(why)
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
        except Exception:  # pragma: no cover
            pass

    def _summary(self) -> str:
        """프롬프트용 프로젝트 요약(실행 동안 한 번만 만든다)."""
        if self._profile_summary is None:
            self._profile_summary = profile_summary(self.profile)
        return self._profile_summary

    def _advisory_hints(self, c: ErrorCluster) -> tuple[list[str], list[str]]:
        """이 클러스터에 맞는 라이브러리 권고의 (힌트, 권고 id). 권고는 차단하지 않고 프롬프트 힌트로만 간다.

        (hints, advisory ids) of the run's library advisories that match this cluster (sbmig.advisory.hints_for)."""
        if not self.advisories:
            return [], []
        try:
            from sbmig.advisory.hints import hints_for
            root = getattr(self.cfg, "repo_root", None)
            root = Path(root) if isinstance(root, (str, Path)) else None
            hints, ids = hints_for(c.errors, self.advisories, self.profile, notes_root=root)
        except Exception as e:  # pragma: no cover - hints_for never raises; stay defensive about imports
            log.warning("advisory hints unavailable: %s", e)
            return [], []
        if ids:
            log.debug("advisory hints for %s (%s):\n%s", c.id, ", ".join(ids), "\n".join(f"- {h}" for h in hints))
        return list(hints), list(ids)

    def _llm_transport_failure(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """전송·설정 실패가 연속 2번(L5): 실행에서 처음이면 ``llm.cooldown_seconds`` 쉬고 다시 시도한다(그
        시도들은 케이스의 LLM 한도에 세지 않음). 두 번째 연속 실패이거나 쿨다운을 쓸 수 없으면 실행 끝까지 LLM
        불가 — 필요한 케이스는 llm_unavailable 로 차단되고 저장된 스크립트 재사용만 계속된다.

        Two transport/config failures in a row (L5): the first such streak of the run pauses the loop for
        `llm.cooldown_seconds` and retries (the streak's attempts do not count against the per-cluster LLM limit);
        a second streak — or a streak with no cool-down configured / left in the time budget — marks the LLM
        unavailable for the rest of the run."""
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
        """게이트웨이가 서로 다른 프롬프트 ``LLM_CONFIG_ERROR_PROMPTS``(2)개에 같은 HTTP 400 본문을 돌려줬다
        → 프롬프트 탓이 아니라 설정(지원 안 되는 모델 이름, 페이로드 필드) 문제: 그 실행의 LLM 호출을
        멈춘다(``llm-config-error``). True = 이 시도는 여기서 끝.

        Round 10: the gateway answered the same HTTP 400 body to LLM_CONFIG_ERROR_PROMPTS different prompts → not
        the prompts' fault but the configuration (an unsupported model name, a payload field): stop calling the LLM
        for the run (flag `llm-config-error`, one follow-up with the short body). True = this attempt ends here."""
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
        """LLM 을 포기하기 전에 실행당 한 번 쉰다(게이트웨이 503 같은 일시 장애 대비). False = 쿨다운
        없음(이미 씀, 꺼짐, 남은 실행 시간보다 김) → 호출한 쪽이 LLM 불가로 표시한다.

        Pause once per run instead of giving up on the LLM (round 9). False = no cool-down (used up, disabled,
        or it would run past the run's time budget) → the caller marks the LLM unavailable."""
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

    # ------------------------------------------------------------------ merge-conflict hints (round 9)
    def _merge_conflict_hints(self, att: FixAttempt, c: ErrorCluster) -> tuple[list[str], dict[str, str]]:
        """``c`` 와 관련된 병합 충돌(first-wins)의 (참고 메모, 참고 파일). 남긴 파일이 오류 파일이거나
        오류가 그 클래스 이름을 말할 때. 버려진 사본은 읽기 전용 참고로만 보여 준다 — 그 멤버를 옮겨 오라고,
        두 번째 모듈을 다시 만들지 말라고.

        (reference_notes, reference_files) for the first-wins merge conflicts relevant to `c`: by file (the kept
        file is the cluster's / an error's file) or by symbol (an error names the kept class's simple name)."""
        if not self.merge_conflicts:
            return [], {}
        try:
            return self._merge_conflict_hints_unsafe(att, c)
        except Exception as e:  # pragma: no cover - hints never break a fix attempt
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
        """두 커밋 사이 바뀐 파일들(NUL 구분이라 한글·공백 이름도 그대로)."""
        changed = getattr(self.ws, "changed_between", None)          # NUL-separated: Korean / spaced names verbatim
        if callable(changed):
            return changed(a, b)
        out = self.ws._git("diff", "--name-only", a, b).stdout   # noqa: SLF001 - a stand-in workspace without it
        return [l.strip() for l in out.splitlines() if l.strip()]

    # ------------------------------------------------------------------ fallback
    def _maybe_skip_test_compile(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                                 last: BuildResult) -> None:
        """대체 빌드(L7): 남은 오류가 전부 테스트 컴파일 오류면 ``-Dmaven.test.skip=true`` 로 다시 빌드한다.
        통과하면 단계는 통과, 플래그 ``test-compile-skipped``, 테스트 오류는 미해결·후속 조치로
        남는다(테스트는 담당 팀 몫). v2 기본은 처음부터 테스트를 컴파일하지
        않으므로(``build.skip_test_compile``) 보통은 해당 없다."""
        if not self.cfg.build.allow_skip_test_compile_fallback:
            return
        if not any(g in goal.split() for g in TEST_COMPILING_GOALS):
            return
        if not last.errors or any(e.kind != ErrorKind.TEST_COMPILE for e in last.errors):
            return
        self.ws.journal({"event": "test_compile_fallback", "phase": phase.value, "test_errors": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests", phase, pr.iterations + 1, skip_test_compile=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("test-compile fallback (-Dmaven.test.skip=true) also failed")
            return
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

    def _maybe_skip_tests(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                          last: BuildResult) -> None:
        """대체 빌드(L7b): 메인 코드는 빌드되고 테스트 *실행* 실패만 남았으면(대개 외부 설정·빌드 때 닿지
        않는 인프라) ``-DskipTests`` 로 다시 빌드한다. 통과하면 플래그 ``tests-skipped``, 실패한 테스트마다
        후속 조치(마무리에서 미해결).

        Round 8: main code compiles and packages, only runtime test failures (TEST_FAILURE) remain — usually an
        externalized setting or infrastructure not reachable at build time. Rebuild with -DskipTests; on success the
        phase passes with flag `tests-skipped` and every failing test as a follow-up (and unresolved at finalize)."""
        if not getattr(self.cfg.build, "allow_skip_tests_fallback", False):
            return
        if not any(g in goal.split() for g in TEST_RUNNING_GOALS):
            return
        if not last.errors or any(e.kind != ErrorKind.TEST_FAILURE for e in last.errors):
            return
        self.ws.journal({"event": "tests_fallback", "phase": phase.value, "failing": len(last.errors)})
        res = self._build(goal, target, f"{phase.value}-skip-tests-run", phase, pr.iterations + 1, skip_tests=True)
        pr.iterations += 1
        pr.iteration_records.append(IterationRecord(phase=phase, iteration=pr.iterations, build_ok=res.ok,
                                                    error_count=res.error_count,
                                                    signatures=res.signature_multiset(), build_log=res.log_path))
        if not res.ok:
            pr.notes.append("tests fallback (-DskipTests) also failed")
            return
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

    def _flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def _followup(self, text: str) -> None:
        if text not in self.followups:
            self.followups.append(text)


VERSIONS_LOGGED = 6                  # versions per list in the `의존성 버전 조회:` line (the newest ones)


def _versions_text(cands: list) -> str:
    """후보 버전 목록 글(최신 ``VERSIONS_LOGGED`` 개, pom 을 확인한 것은 jakarta ✓ / javax 표시).

    '2.5.0, 2.6.0 (jakarta ✓)' — the newest VERSIONS_LOGGED candidates, each tagged when its pom was checked."""
    items = []
    for v in cands:
        tag = " (jakarta ✓)" if getattr(v, "jakarta", None) is True else (
            " (javax)" if getattr(v, "jakarta", None) is False else "")
        items.append(f"{v.version}{tag}")
    if len(items) > VERSIONS_LOGGED:
        items = ["…"] + items[-VERSIONS_LOGGED:]
    return ", ".join(items)


def _versions_line(rv: Any, key: str, current: str | None) -> str:
    """저장소 조회 결과 로그 한 줄(현재 버전 → 같은 메이저 후보 / 상위 메이저 후보, 또는 조회 실패).

    `의존성 버전 조회: opensymphony:sitemesh 2.4.2 → 같은 메이저 2.5.0, 2.6.0 (jakarta ✓) / 상위 3.3.0` or
    `의존성 버전 조회 실패: <key> — <error>`."""
    k = getattr(rv, "key", "") or key
    if getattr(rv, "error", "") and not (rv.same_major or rv.higher):
        return f"의존성 버전 조회 실패: {k} — {' '.join(str(rv.error).split())[:200]}"
    cur = getattr(rv, "current", None) or current or "?"
    line = f"의존성 버전 조회: {k} {cur} → 같은 메이저 {_versions_text(rv.same_major) or '없음'}"
    if rv.higher:
        line += f" / 상위 {_versions_text(rv.higher)}"
    return line


def _edit_line(e: Any) -> str:
    """v2.1i: 적용 못 한 edit 한 줄(앞 시도 글): ``- replace <path> — <desc>: old '…' → new '…'``(텍스트는 잘라서).

    One edit that did not apply, for `FixContext.previous_edits`: op, path, desc and its texts (capped)."""
    def cut(text: Any) -> str:
        t = str(text or "")
        return repr(t if len(t) <= PREVIOUS_EDIT_TEXT else t[:PREVIOUS_EDIT_TEXT] + "…")
    op, path = str(getattr(e, "op", "") or "?"), str(getattr(e, "path", "") or "?")
    line = f"- {op} {path}" + (f" — {getattr(e, 'desc', '')}" if getattr(e, "desc", "") else "")
    if op == "replace":
        line += f": old {cut(getattr(e, 'old', ''))} → new {cut(getattr(e, 'new', ''))}"
    elif op in ("write", "create"):
        line += f": content {cut(getattr(e, 'content', ''))}"
    elif op == "move":
        line += f" → {getattr(e, 'to', '')}"
    elif getattr(e, "pom", None):
        line += f": pom {cut(e.pom)}"
    return line


def _try_note(att: FixAttempt) -> str:
    """FixTry 메모: 왜 실패했는지(적용 오류), 롤백, 채점 메모 — 중복 제거, ``TRY_NOTE_CHARS`` 자 캡.

    FixTry.note: why the try failed (apply errors), rollbacks, scoring notes — deduplicated, capped."""
    parts = list(dict.fromkeys([*att.apply_errors, *att.notes]))
    text = "; ".join(" ".join(p.split()) for p in parts if p)
    return text if len(text) <= TRY_NOTE_CHARS else text[:TRY_NOTE_CHARS - 1] + "…"


def _kept_partial(a: FixAttempt) -> bool:
    """v2.1l / v2.1m: 부분 해결된 sbmig 사전 시도(``sbmig-version`` / ``sbmig-resources``)는 다음 시도 전에
    되돌리지 않는다 — 적은 것은 추측이 아니라 사실(as-is 빌드·저장소, 파일 스캔)이고, 남은 오류는 sbmig 가 못 찾은
    것뿐이다.

    A partial zero-LLM pre-try stays in the tree (no rollback before the case's next try): what it wrote are facts
    (the as-is build / the repository / the file scan), what is left is what it could not find."""
    return a.source in PRE_TRY_SOURCES and a.outcome == "partial"


def _synthetic(e: BuildError) -> bool:
    """Maven 이 죽거나 시간 초과일 때 실행기가 넣는 대역 오류 — 빌드가 어디까지 갔는지 아무것도 말하지 않는다.

    The runner's stand-in error for a crashed / timed-out Maven: says nothing about how far the build got."""
    return e.kind == ErrorKind.UNKNOWN and not e.file and (e.message or "").startswith(_SYNTHETIC_PREFIXES)


def _pair(e: BuildError) -> tuple[str, str]:
    """채점의 단위 (파일, 서명). L1 서명에는 파일이 없어서(다른 두
    파일의 같은 오류 = 같은 서명) 파일과 짝지어 횟수로 비교한다.

    (file, signature) of an error — file '' for module / artifact errors. L1 signatures carry no file: the same
    renamed method in two files is one signature, so scoring compares these pairs (with their occurrence counts)."""
    return (_norm_path(e.file) if e.file else ""), (e.signature or f"{e.kind.value}|{e.message}")


def _touched(a: FixAttempt) -> set[str]:
    """시도가 바꾼 파일 경로들(이름 바꾸기는 옛 / 새 경로 모두)."""
    return {_norm_path(p) for c in a.changes for p in (c.path, c.new_path) if p}


def _targeted_left(a: FixAttempt, cur: Counter) -> tuple[int, int]:
    """(빌드에 아직 남은 대상 오류 수, 대상 오류 수) — (파일, 서명)
    횟수로. 오류 기록이 없는 시도는 서명으로 전부 / 전무 판정.

    (targeted errors still in the build, targeted errors) — by (file, signature) occurrence; attempts without
    recorded errors fall back to the cluster signatures (all or nothing)."""
    if not a.cluster_errors:
        sigs = set(a.cluster_signatures)
        return (1 if any(s in sigs for _f, s in cur) else 0), 1
    targeted = Counter(_pair(e) for e in a.cluster_errors)
    return sum(min(n, cur[k]) for k, n in targeted.items()), sum(targeted.values())


def _stage_of(errors: list[BuildError]) -> int:
    """이 오류들이 보고되는 가장 이른 빌드 단계 번호(없으면 99).

    The earliest build stage (`STAGE` / `error_stage`) at which these errors are reported; 99 when none has one."""
    return min((error_stage(e) for e in errors if error_stage(e) is not None), default=99)


def _visible_in(errors: list[BuildError], build: BuildResult, real: list[BuildError]) -> bool:
    """이 빌드가 (지금은 없는) 이 오류들을 보고할 *수* 있었나? 그 모듈이 빌드됐거나 빌드가 그 단계까지
    갔으면 예. 아니면 사라진 것은 "해결" 이 아니라 "안 보임" 이다(round
    10: 안 보임 ≠ 해결) — 그때 시도는 개선이 아니라 판정 불가.

    Could `build` have reported these (now absent) errors? Their module was built, or the build got at least as far
    as their stage — the same notion as the error list's visibility (round 10: "not visible" ≠ "resolved")."""
    if build.ok:
        return True
    if not real:
        return False
    built = set(build.modules_built or [])
    failed = {build.failed_module} | {x.module for x in real if x.module}
    # how far the build got: an UNKNOWN error without a file (a crashed JVM, `mvn` itself failing, a generic BUILD
    # FAILURE line) proves no stage
    reached = max((error_stage(x) if error_stage(x) is not None else -1
                   for x in real if x.kind != ErrorKind.UNKNOWN or x.file), default=-1)
    for e in errors:
        if _synthetic(e):
            return False                         # a stand-in error is never "fixed" by disappearing
        m = e.module
        if m and m in built:
            continue
        if m and build.failed_module and m not in failed:
            return False                         # its module never ran (skipped after another one failed)
        st = error_stage(e)
        if st is None or reached < st:
            return False
    return True


def _new_errors(errors: list[BuildError], prev: Counter) -> list[BuildError]:
    """직전 빌드보다 많아진 (파일, 서명) 발생분 = 새 오류.

    The occurrences of (file, signature) beyond what the previous build had."""
    seen: Counter = Counter()
    out: list[BuildError] = []
    for e in errors:
        k = _pair(e)
        seen[k] += 1
        if seen[k] > prev[k]:
            out.append(e)
    return out


def _new_in_files(errors: list[BuildError], files: set[str], prev: Counter) -> list[BuildError]:
    """``files`` 안의 새 오류들((파일, 서명) 횟수로) — 다른
    파일이 이미 가진 L1 서명이라도 이 파일에선 새 오류다.

    New errors (by (file, signature) occurrence) in `files` — round 10: a new error whose L1 signature another file
    already had is still new here."""
    return [e for e in _new_errors([e for e in errors if e.file and _norm_path(e.file) in files], prev)]


def _target_key(target: Optional[str]) -> str:
    """빌드 대상의 정규화 키('' = 프로젝트 전체, 끝의 pom.xml
    제거) — 대체 빌드 결과를 대상별로 기억할 때 쓴다."""
    t = _norm_path(target or "").strip("/")
    if t.endswith("pom.xml"):
        t = t[:-len("pom.xml")].rstrip("/")
    return t


def _gone(errors: list[BuildError], build: BuildResult) -> list[BuildError]:
    """``errors`` 중 이 빌드에 더는 없는 발생분 — 시도가 지운 것(롤백하면 돌아오는 것).

    The occurrences of `errors` (by (file, signature)) that `build` no longer has — what an attempt removed."""
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
    """시도가 바꾼 것 한 줄(같은 실행의 형제 힌트용): 바꾼 파일들, 없으면 원인 분석 첫 문장.

    One line of what an attempt changed (sibling hints): the files its script changed, else its analysis."""
    if a.changes:
        files = [c.new_path or c.path for c in a.changes]
        return f"changed {len(files)} file(s): {', '.join(files[:4])}" + (" …" if len(files) > 4 else "")
    return _first_sentence(a.analysis or "", 160) or "-"


def _norm_path(p: str) -> str:
    p = (p or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def refine_build(build: Optional[BuildResult]) -> Optional[BuildResult]:
    """빌드 오류들의 케이스 키를 다듬는다(제자리, 거친 ``build`` / ``other`` 키를 javac 줄로 세분).
    채점·오류 목록·클러스터·차단 등록부가 모두 같은 L1 / L2 를 보게 하려고 빌드 직후 한 번만 한다.

    The refined case keys of a build's errors (in place, `sbmig.triage.cluster.refine_signatures`): every consumer
    of the build — scoring, the error list, the clusters, the blocked registry — sees the same L1 / L2."""
    if build is None or not build.errors:
        return build
    try:
        from sbmig.triage.cluster import refine_signatures
        refine_signatures(build.errors)
    except Exception as e:  # pragma: no cover - triage is ours; a broken refinement keeps the parser's keys
        log.warning("signature refinement failed: %s: %s", type(e).__name__, e)
    return build
