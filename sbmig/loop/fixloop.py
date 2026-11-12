"""Maven 빌드 오류를 LLM이 작성한 수정 스크립트로 고치는 수정 루프 모듈.
검증 빌드가 실패하면 오케스트레이터가 호출한다.
한 라운드는 빌드 -> 직전 시도 결과 평가 -> 새 오류 기록 -> 오류 서명(L2)이 같은 오류를 케이스로 묶기 ->
케이스마다 수정 시도 -> 커밋 순서로 진행한다.
- 케이스마다 저장된 스크립트 재사용(LLM 호출 없음) -> LLM 보강 -> LLM 신규 순서로 시도한다
- 케이스당 LLM 요청은 ``script.max_llm_tries_per_case``번까지이고 그중 보강은
  ``script.max_augments_per_case``번까지다. 다 쓰면 그 케이스는 차단(BLOCKED)된다
- 시도 결과는 재빌드로만 평가한다(``_score``).
  대상 오류가 모두 사라지고 빌드가 그 단계까지 진행돼야 개선으로 본다
- 개선되지 않은 시도는 같은 케이스의 다음 시도 전에 되돌린다
- 근본 원인 케이스를 먼저 고치고, 이번 라운드에 이미 수정한 파일이나 모듈 pom에 걸린 케이스는 다음 빌드까지 미룬다
- 루프는 항상 빌드 뒤에 끝난다. 종료 조건은 BUILD SUCCESS, 라운드
  소진(``loop.phases.verify.max_iterations``), 남은 케이스 없음, 차단된 케이스만 남음, 시간 초과다
- LLM에게 의존성 버전을 추측시키지 않는다. 버전이 없는 의존성은 커밋 전에 ``_fill_versions``가 채운다
- 누락된 버전이나 리소스 필터링처럼 확인된 정보만으로 고칠 수 있는
  케이스는 LLM을 호출하기 전에 sbmig가 직접 고친다(사전 시도)

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
case and run (v2.1z): 1 stored + `script.max_llm_tries_per_case` (20) LLM asks, of them at most
`script.max_augments_per_case` (15) augments (used up → the next ask is a fresh `new`), then BLOCKED
(repeated_failure). When an
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
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

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
from sbmig.restructure.system_jars import WiredJar, bundled_jar_followup, wire_bundled_jar

from .blocked_registry import BlockedRegistry, attribute, compose_suggestion, is_in_house, sanitize_errors
from .budget import LoopBudget
from .components import LazyFixer, ScriptFixerMissing, profile_summary
from .entries import CaseScripts, RunErrors, script_path
from .notes import (answer_notes, fallback_guide, log_major_upgrades, note_line, operator_changes, pom_changed,
                    table_entry, try_notes)

log = logging.getLogger("sbmig.loop")

TEST_COMPILING_GOALS = ("test-compile", "package", "verify", "install", "deploy", "test")
TEST_RUNNING_GOALS = ("test", "package", "verify", "install", "deploy")
RESOLUTION_PREFIXES = ("parent_resolution:", "pom_invalid:", "dependency_resolution:", "plugin_resolution:")
# 게이트웨이, 네트워크, 키 문제 같은 전송이나 설정 실패를 모델이 형식을 어긴 응답과 구분한다
# 쿨다운을 하고 "LLM 사용 불가"로 세는 것은 전송이나 설정 실패뿐이다
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
# 게이트웨이가 요청 내용(너무 긴 요청, 형식 오류, 토큰 한도) 때문에 거절한 경우다.
# 전송이 아니라 프롬프트의 문제이므로 LLM 사용 불가로 세지 않는다
# the gateway rejected *this request* for its content (too long, malformed, cut by the token limit): the prompt's
# fault, not the wire's — such a failure must not count towards "LLM unavailable for the run"
_LLM_REQUEST_REJECTED_RE = re.compile(r"status=4(?:00|13|22)\b|content[ _-]?filter|"
                                      r"finish_reason=(?:length|max_tokens)\b")
# a 400 that says the prompt is too big: different prompts may well get the same text — never a configuration error
_LLM_PROMPT_TOO_BIG_RE = re.compile(r"(?i)context[ _-]?length|too (?:long|large|many tokens)|"
                                    r"max(?:imum)?[ _-]?(?:context|tokens?|input)|token limit|payload too large|"
                                    r"finish_reason=(?:length|max_tokens)")
_LLM_400_BODY_RE = re.compile(r"status=400\)?:?\s*(?P<body>.*)", re.S)
# 서로 다른 프롬프트 2개가 같은 HTTP 400 본문을 받으면 설정 오류로 보고 그 실행의 LLM 호출을 멈춘다
LLM_CONFIG_ERROR_PROMPTS = 2         # the same HTTP 400 body for this many different prompts → configuration error
_BLOCKED_RAT_RE = re.compile(r"blocked:\s*(?P<r>[a-z_]+)")
REFERENCE_MAX_CHARS = 12_000         # one dropped merge copy in the fix prompt (round 9)
# Windows에는 SIGUSR1이 없으므로 루프가 오래 조용하면 감시 스레드가 스스로 경고하고 스레드 덤프를 남긴다
WATCHDOG_S = 30.0                    # the fix loop's watchdog wakes up this often …
WATCHDOG_QUIET_S = 60.0              # … and warns when no milestone was reached for this long (operators: stuck?)
WATCHDOG_DUMP_S = 300.0              # … and writes a thread dump into the run dir by itself after this long (Windows has no SIGUSR1)
REFERENCE_MAX_FILES = 3              # dropped copies per cluster
_TRUNCATED_MARK = "\n… (truncated)"
# 개선되지 않은 시도는 되돌리고, 개선되었거나 빌드에 성공한 시도만 트리에 남긴다
# NO_ROLLBACK의 나머지 상태는 이미 트리에 없거나 아직 결과가 나오지 않아 되돌릴 것이 없다
SCRIPT_KEEP = frozenset({"improved", "build_ok"})   # a case's previous script attempt is kept (not rolled back)
NO_ROLLBACK = frozenset({"improved", "build_ok", "reverted", "pending", "apply_failed", "blocked"})
SCRIPT_BASE_LOG = 50                 # ScriptBase.log lines (augment prompt)
SCRIPT_HISTORY = 10                  # ScriptBase.history lines
FIXER_WITHOUT_SCRIPTS_FLAG = "fixer-without-scripts"
# 이 메모가 붙은 시도는 최종 트리에 없으므로 운영자 확인 목록에서도 뺀다
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
        """오류 종류별로 Maven이 그 오류를 보고하는 빌드 단계 번호를 반환한다. 번호가 작을수록 이른 단계다.
        공용 정의를 가져오지 못할 때 쓰는 대체 정의다."""
        if e.kind == ErrorKind.PLUGIN_EXECUTION:
            return 2 if "resources" in (e.goal or "").lower() else 5
        return STAGE.get(e.kind)
TRY_NOTE_CHARS = 600                 # FixTry.note: the try's failure / rollback / scoring notes, joined
NOTES_LOGGED = 20                    # `수정:` INFO lines per applied change (the rest: errors.md / errors.jsonl)


def fmt_error(e: BuildError) -> str:
    """오류 하나를 ``파일:줄 메시지`` 형식의 한 줄로 만든다. 보강 프롬프트의 오류 목록에 쓴다.

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
    """빌드가 target/ 밖에 만든 파일(lib/에 복사된 jar 등)을 빌드 직후 치운다.
    시도의 커밋과 파일 목록에 실제로 수정한 내용만 남기기 위해서다.

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
    """LLM 쿨다운 동안 주어진 초만큼 기다린다. 테스트가 한 번에 교체할 수 있도록 모듈 함수로 둔다.

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


def cut_lines(text: str, limit: int) -> str:
    """텍스트를 ``limit``자 안에서 줄 단위로 자르고, 잘린 줄 수를 ``(… N more lines)``로 덧붙인다.

    `text` cut at a line boundary within `limit` characters, with `(… N more lines)` naming what was left out
    (v2.1v E-L9: the other cases' summary used to be cut mid-line without a marker). A first line longer than
    `limit` is cut itself (with "…")."""
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
    """라운드 안에서 케이스 하나를 처리할 때 쓰는 로그 단계 태그(``fix r3 7a52b1cea8``)를 만든다.

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
    """``llm-error: …`` 분석 문구가 전송이나 설정 실패인지 판단한다.
    이런 실패만 쿨다운과 "LLM 사용 불가"로 센다.
    형식을 어긴 응답이나 내용 때문에 거절된 요청은 프롬프트의 문제이므로 해당하지 않는다.

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
    """HTTP 400 ``llm-error``의 짧은 본문(200자 이하)을 반환한다. 해당하지 않으면 None을 반환한다.
    프롬프트가 너무 크다는 400도 None이다.
    큰 프롬프트 둘이 같은 본문을 받는 것은 정상이므로 설정 오류의 근거로 삼지 않는다.

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
    """``target``을 빌드할 때 쓸 (빌드 디렉토리 모듈, 추가 Maven 인자)를 정한다.
    루트 aggregator가 그 모듈을 포함하면 루트에서 ``-pl <target> -am``으로 빌드한다.
    저장소 안의 공유 라이브러리도 함께 빌드되고 오류 경로가 루트 기준으로 유지되기 때문이다.
    그렇지 않으면 그 모듈 디렉토리 안에서 빌드한다.

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
    ``covered``와 ``poms``는 이번 라운드에 수정한 오류 파일과 pom을 케이스 ID와 연결한다.
    같은 파일이나 모듈의 다른 케이스를 다음 빌드까지 미룰 때 쓴다.
    ``restored``는 되돌리기로 다시 나타난 오류이며, 다음 시도 결과 평가의 기준선이 된다."""
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
    """적용되어 결과 평가를 기다리는 시도와 그 커밋 범위를 담는 클래스. 되돌릴 때 pre / post sha를 쓴다."""
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
    """클러스터 id가 같거나 서명을 하나라도 공유하는 시도를 다른 파일까지 포함해 찾는다.
    프롬프트 힌트로만 쓰고 시도 횟수 한도에는 반영하지 않는다.

    Same cluster id or any shared signature (other files too): hint material, never a limit (round 10)."""
    sigs = set(cluster.signatures)
    return [a for a in attempts if a.cluster_id == cluster.id or (sigs and sigs & set(a.cluster_signatures))]


def _same_cluster(cluster: ErrorCluster, attempts: list[FixAttempt],
                  canon: Optional[Callable[[Optional[str]], str]] = None) -> list[FixAttempt]:
    """이 클러스터에 대한 시도를 찾는다. id가 같거나, 파일이 같고 서명이 겹치는 시도다.
    시도 순서, 시도 횟수 한도, 되돌리기는 이 목록을 기준으로 한다.

    The attempts made on THIS cluster (same id, or same — possibly moved — file with an overlapping signature)."""
    from sbmig.triage.decide import same_cluster_attempts
    return same_cluster_attempts(cluster, attempts, canon)


def call_edit_applier(fn: Callable[..., Any], ws: Any, edits: list, cfg: Any, errors: list) -> Any:
    """플러그인 edit applier를 ``fn(ws, edits, *, cfg, errors=None)`` 형태로 호출한다.
    ``errors``는 함수가 그 인자를 받을 때만 넘기므로, ``errors``를 받지 않는 회사 플러그인도 TypeError 없이
    실행된다. 시그니처를 확인할 수 없으면 그대로 넘긴다.

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
SBMIG_JAKARTA_SOURCE = "sbmig-jakarta"               # v2.1z-e: … of the jakarta-build swap of a javax-era dependency
SBMIG_BUNDLED_SOURCE = "sbmig-bundled"               # v2.1z-i: … of the wiring of a jar bundled in the tree
SBMIG_PARENT_SOURCE = "sbmig-parent"                 # v2.1z-ag: … of the alignment of an as-is company parent version
RESOURCE_FACTS_FILES = 10                             # binary files named in the v2.1m prompt facts
PRE_TRY_SOURCES = frozenset({SBMIG_VERSION_SOURCE, SBMIG_RESOURCES_SOURCE, SBMIG_JAKARTA_SOURCE,
                             SBMIG_BUNDLED_SOURCE, SBMIG_PARENT_SOURCE})   # zero-LLM pre-tries
JAKARTA_SEARCH_PREFIX = "repository search for a jakarta build of"   # BlockedItem.suggestion / notes evidence (v2.1z-e)
UNMAPPED_PREFIX = "the library behind"                                # … "<owner class> could not be matched to a dependency"
# 소유 클래스의 패키지를 선언된 의존성과 맞춰 볼 때 아무것도 구별하지 못하므로 무시하는 토큰 / tokens that
# identify nothing when matching a class
# package against declared dependencies
_GENERIC_TOKENS = frozenset({"com", "org", "net", "io", "kr", "co", "java", "javax", "jakarta", "api", "core", "lib",
                             "libs", "common", "commons", "util", "utils", "web", "app", "impl", "internal", "base"})
VERSION_FACTS_MAX = 12                                # artifacts per case in the "as-is build: …" prompt facts
VERSION_TRY_APPLY_FAILURES = 3                        # zero-LLM tries of a (pom, g:a) that may fail to apply
PRE_TRY_APPLY_FAILURES = VERSION_TRY_APPLY_FAILURES   # … the same cap for every zero-LLM pre-try (v2.1m)


@dataclass(frozen=True)
class _PreTry:
    """LLM을 호출하지 않는 사전 시도 한 종류(``sbmig-version``, ``sbmig-resources`` 등)를 담는 데이터 클래스.
    케이스가 이 종류에 해당하는지와 수정할 pom 목록(해당하지 않으면
    None)을 알려 주고, 실행해서 적용되면 True를 반환한다.

    One zero-LLM pre-try kind: `targets(c)` → the poms it would edit (the deferral check; None = not this kind),
    `run(pr, st, c)` → True when it applied (the case's try of the round)."""
    source: str
    targets: Callable[[ErrorCluster], list[str] | None]
    run: Callable[..., bool]
VERSION_SOURCE_TEXT = {"asis": "as-is 빌드의 버전", "repository": "저장소의 최신 릴리스",          # log / edit desc
                       "boot-bom": "목표 Boot BOM 의 버전 (부모 체인이 관리하지 않음)",
                       "chain-property": "부모 체인의 버전 속성"}
VERSION_SOURCE_NOTE = {"asis": "the as-is build", "repository": "newest release in the repository",   # att.notes
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
CHAIN_PROPERTY_FACTS_MAX = 20                         # "parent chain property: …" lines per prompt
# v2.1v (B-M4): "package <p> does not exist" → the managed artifactIds of the longest groupId prefixing <p>
PACKAGE_FACTS_ARTIFACTS = 25                          # artifactIds per `managed artifacts under <group>` line
PACKAGE_FACTS_LINES = 5                               # such lines per prompt (one per missing package)
_PACKAGE_MISSING_RE = re.compile(r"package\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)\s+does not exist")
# v2.1z-i: `cannot find symbol … symbol: class X` → X (its package from the error file's `import <p>.X;`)
_SYMBOL_CLASS_RE = re.compile(r"symbol:\s+class\s+([A-Za-z_]\w*)")
BUNDLED_READ_CHARS = 200_000                          # chars of an error file read for its imports (bundled-jar check)
BUNDLED_TRUSTED_SOURCES = ("pom_properties", "sha1_lookup", "name_table")   # jar coordinates checked in the repository
# v2.1v (B-H1): the line for a library the new chain / the target Boot BOM manages — no repository list, no verdict
API_CHANGED_TAIL = " — the API changed, fix the code and leave the version"
OTHER_ERRORS_CHARS = 4000                             # FixContext.other_errors_summary, cut at a line boundary (E-L9)
# v2.1y (E-M1): an answer with neither edits nor a script (or with <blocked>) is "declined" — a blocker outside the
# project; after script.max_declines_per_case of them the case is BLOCKED needs_owner, its same-cause siblings too
DECLINED_NOTE = "declined: "                          # FixAttempt.notes prefix of a declined answer
NEEDS_OWNER_KO = "소유팀 결정 필요"                    # the Korean suggestion's tail (BlockedItem.suggestion)
# ScriptProposal.error values that mean "the answer simply had no script / no edits" (not an unparsable answer)
_DECLINE_ERRORS = ("no <script> block", "no <edits> block", "no script and no edits", "no edits:")
NON_IDEMPOTENT_NOTE = "non-idempotent: "              # FixAttempt.notes prefix (v2.1y E-H3)
# PM 2026-10-31: a declined case keeps the triage classifier's specific reason (operators, the handbook and the attn
# codes key on them); needs_owner only for a cluster without one
DECLINE_KEEPS_REASONS = frozenset({BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE,
                                   BlockReason.ARTIFACT_NOT_FOUND, BlockReason.PLUGIN_INCOMPATIBLE})
_STEM_SUFFIXES = ("-starter", "-bom", "-parent")


def artifact_stem(artifact_id: str) -> str:
    """artifactId 끝의 ``-starter`` / ``-bom`` / ``-parent``를 떼어 기본 이름을 반환한다.

    The artifactId without a trailing `-starter` / `-bom` / `-parent`."""
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
    """부모 체인의 ``*.version`` / ``*-version`` 속성 중 이 아티팩트에 해당하는 것을 구체적인 것부터 찾아 반환한다.
    - 이름이 artifactId나 그 기본 이름으로 시작하는 속성, 또는 그룹 키워드(``spring-boot``,
      ``spring-cloud``, ``spring-data``, ``spring-framework``)인 속성
    - 사내 아티팩트는 기본 이름이 속성 이름 + ``-``로 시작하는 속성도
      포함한다(``xxx-reuse-web``이면 ``xxx-reuse.version``)

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
    """회사 부모 체인의 pom이 정의한 속성과, 각 속성을 정의한 pom을 반환한다.
    정의한 pom의 groupId가 사내 부모의 그룹이나 그 하위 그룹이거나 ``is_in_house``에 해당할 때만 포함한다.
    Spring / Spring Boot pom과 sbmig가 추가한 Boot BOM의 속성은 회사 체인에서 참조할 수 없으므로 뺀다.
    정보에 ``company_properties`` / ``company_property_sources``가 이미 있으면 그것을 쓴다.

    The properties DEFINED BY THE COMPANY'S OWN chain poms and their defining pom: the defining pom's groupId is a
    root's group (or under it) or in-house (`is_in_house`); never a Spring / Spring Boot pom (`org.springframework*`)
    nor the Boot add-on — a `${lombok.version}` taken from the Boot BOM of a `spring-boot-starter-parent` chain would
    not resolve on the company chain. `facts.company_properties` / `company_property_sources` when the facts carry
    them (non-empty); else the same rule applied to `properties` / `property_sources` here."""
    own = getattr(facts, "company_properties", None)
    if isinstance(own, dict) and own:                 # the loader's own split (v2.1t); empty → the same rule here
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
            continue                                  # unknown origin, or a Spring / Boot pom (the add-on too)
        if any(g == x or g.startswith(x + ".") for x in roots) or \
                is_in_house(GAV(group_id=g, artifact_id=parts[1]), cfg):
            keep[name], keep_src[name] = value, src
    return keep, keep_src


def chain_own_managed(facts: Any, boot: Callable[[], dict] | dict | None = None) -> dict[str, str]:
    """사내 부모 체인이 직접 관리하는 버전을 반환한다.
    sbmig가 추가한 Boot BOM(``boot_bom_added``)의 항목은 실제 빌드에서 관리되지 않으므로 뺀다.
    정보에 ``managed_by_chain``이 있으면 그것을 쓰고, 없으면 ``managed``에서 Boot BOM과 값이 같은 항목을 뺀다.

    The parent chain's OWN managed versions, without the Boot add-on (`boot_bom_added`: sbmig added the Boot BOM
    because the chain does not import it — the real build does not manage those). `facts.managed_by_chain` when the
    facts have it, else `managed` minus the entries equal to the target Boot BOM's (an entry the chain declares itself
    at another version stays)."""
    # 로더가 Boot 항목을 추가했을 때만 채우는 `ParentChainFacts.chain_managed`를 먼저 쓰고, 이전 이름인
    # `managed_by_chain`도 받는다
    # 둘 다 없으면 아래에서 Boot BOM과 값이 같은 항목을 뺀다
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


class VersionResolver:
    """버전이 없는 의존성의 버전을 정하는 클래스. 재구성과 수정 루프가 같은 규칙을 쓴다.
    순서: 새 부모 체인이 관리 ``(None, "managed")`` -> 목표 Boot BOM이 관리 ``(v, "boot-bom")``, 단 as-is
    버전이 더 새것이면 as-is -> as-is 빌드의 버전 ``(v, "asis")`` -> 호출한 쪽이 허용하면 저장소의 최신
    릴리스 ``(v, "repository")`` -> ``(None, "unknown")``
    LLM에게 버전을 추측시키지 않는다. 예외를 던지지 않고, 저장소 조회에 실패하면 찾지 못한 것으로 본다.
    수정 루프가 실행마다 하나 만들어 재사용한다.

    The v2.1l resolution order for a `<dependencies>` entry without `<version>` (one rule for the restructure and
    the fix loop): (1) the new parent chain manages it (the post-restructure capture; the declaring module's
    `by_module` entry decides when the capture has one, else the merged `managed`; without the capture the parent-chain
    facts WITHOUT their Boot add-on) → `(None, "managed")`; (1b, v2.1s) the TARGET Boot BOM manages it → that version,
    `(v, "boot-bom")`, unless the as-is version is newer (then `(as-is, "asis")` — never a downgrade); (2) the
    as-is build resolved it (`by_module[...].direct` → `.managed` → merged `managed` → merged `direct`) →
    `(v, "asis")`; (3) only with `allow_repository` (a dependency the LLM just added, a missing-version case): the
    newest release `RepositoryLookup.versions` lists that `is_prerelease` does not reject → `(v, "repository")`;
    (4) `(None, "unknown")`. `asis` / `after` are depmgmt dicts (`sbmig.build.effective.load_effective`);
    `repository` a zero-arg callable returning the run's `RepositoryLookup` (or None). Never raises."""

    def __init__(self, asis: dict | None = None, after: dict | None = None,
                 repository: Callable[[], Any] | None = None, chain: Any = None, boot: Any = None):
        """as-is 캡처와 재구성 뒤 캡처(없으면 빈 dict), 저장소 조회기를 반환하는 함수, 재구성 뒤 캡처가 없을
        때 쓸 사내 부모 체인 정보를 받는다.
        ``chain``과 ``boot``는 dict이거나 dict를 반환하는 함수이며, 함수는 처음 필요할 때 한 번만 호출한다.

        `chain`: the parent-chain facts used when the post-restructure capture is missing (a capture-shaped dict, or a
        zero-arg callable returning one, called once when first needed). `boot` (v2.1s): the target Boot BOM's
        managed versions (g:a → version, or a zero-arg callable returning them, called once)."""
        self.asis: dict = asis if isinstance(asis, dict) else {}
        self.after: dict = after if isinstance(after, dict) else {}
        self._repository = repository
        self._repo_cache: dict[str, str | None] = {}
        self._chain_src = chain
        self._chain: dict | None = None
        self._boot_src = boot
        self._boot: dict | None = None

    @classmethod
    def for_workspace(cls, ws: Any, repository: Callable[[], Any] | None = None,
                      cfg: Any = None) -> VersionResolver:
        """실행 디렉토리의 두 캡처(as-is ``depmgmt.json``, 재구성 뒤 ``depmgmt-after-restructure.json``)를
        읽어 생성한다. 읽지 못한 캡처는 빈 캡처로 본다.
        ``cfg``가 있으면 재구성 뒤 캡처가 없을 때 사내 부모 체인 정보(``sbmig.build.parent_chain``)로 1단계를 판단한다.

        Built from the run directory's two captures (missing / unreadable → empty); with `cfg`, step 1 falls back on
        the parent-chain facts (`load_parent_chain_facts(cfg).as_capture()`, the restructure's fallback) when the
        post-restructure capture is missing."""
        asis: dict | None = None
        after: dict | None = None
        try:
            from sbmig.build.effective import AFTER_RESTRUCTURE_FILE, load_effective
            asis = load_effective(ws)
            after = load_effective(ws, name=AFTER_RESTRUCTURE_FILE)
        except Exception as e:  # noqa: BLE001 - no captures: only the repository step is left
            log.debug("dependency version captures unavailable: %s: %s", type(e).__name__, e)

        def boot() -> dict:
            # 모듈 속성으로 호출해야 테스트의 가짜 구현(tests/conftest.py)이 적용된다
            from sbmig.build import boot_bom
            return dict(boot_bom.load_boot_bom_facts(cfg).managed)

        def chain() -> dict | None:
            from sbmig.build import parent_chain
            facts = parent_chain.load_parent_chain_facts(cfg)
            data = facts.as_capture()
            data["managed"] = chain_own_managed(facts, boot)
            # Spring / Boot pom의 속성은 회사 체인에서 참조할 수 없으므로 회사 체인 pom이 정의한 속성만 쓴다
            data["company_properties"], data["company_property_sources"] = chain_company_properties(facts, cfg)
            return data

        return cls(asis, after, repository, chain=chain if cfg is not None else None,
                   boot=boot if cfg is not None else None)

    @staticmethod
    def _has(data: dict) -> bool:
        return bool(data.get("managed") or data.get("direct") or data.get("by_module"))

    @property
    def has_asis(self) -> bool:
        """as-is 캡처에 내용이 있는지 확인한다."""
        return self._has(self.asis)

    @property
    def has_after(self) -> bool:
        """재구성 뒤 캡처(새 부모 체인 정보)가 있는지 확인한다."""
        return self._has(self.after)

    @property
    def chain(self) -> dict:
        """재구성 뒤 캡처 대신 쓸 사내 부모 체인 정보. 처음 쓸 때 한 번 읽고, 없거나 읽지 못하면 빈 dict다."""
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
        """목표 Boot BOM이 관리하는 g:a별 버전. 처음 쓸 때 한 번 읽고, 없거나 읽지 못하면 빈 dict다."""
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
        체인이 관리하거나 BOM에 없으면 None을 반환한다.

        The target Boot BOM's version of g:a when the new chain does not manage it (else None)."""
        if self.managed_version(key, module):
            return None
        return self.boot.get(key) or None

    @property
    def has_chain(self) -> bool:
        """새 부모 체인이 관리하는 버전을 알고 있는지 여부.
        모르면 1단계에서 아무것도 관리되지 않는 것으로 본다."""
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

        The version the new chain manages g:a at: the declaring module's effective dependencyManagement when the
        capture has the module (another module's chain does not manage this one), else the merged `managed`; without
        the post-restructure capture, the parent-chain facts' `managed`."""
        if not self.has_after:
            return (self.chain.get("managed") or {}).get(key) or None
        own = self._module(self.after, module)
        if own is not None:
            return (own.get("managed") or {}).get(key) or None
        return (self.after.get("managed") or {}).get(key) or None

    def asis_fact(self, key: str, module: str | None = None) -> tuple[str | None, str]:
        """as-is 빌드가 g:a에 쓴 버전과 그 출처를 반환한다: ``(v, "declared")`` /
        ``(v, "managed by g:a:v")`` / ``(None, "")``.
        모듈의 직접 선언 -> 모듈의 관리 버전 -> 합친 관리 버전 -> 합친 직접 선언 순서로 찾는다.
        모듈 밖에서는 다른 모듈이 명시한 버전보다 부모가 관리한 버전이 맞기 때문이다.

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
        """저장소에 있는 g:a의 최신 정식 릴리스(프리릴리스 제외)를 반환한다.
        조회기가 없거나 조회에 실패하면 None을 반환하며, 결과는 키별로 캐시한다.

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
        """규칙에 따라 ``(버전 또는 None, 출처)``를 반환한다.
        출처는 managed / boot-bom / asis / repository / unknown 중 하나다.
        ``check_managed=False``는 빌드가 이미 버전이 없다고 알린 의존성이므로 1단계를 건너뛴다.
        예외를 던지지 않는다.

        `(version | None, source)` by the resolution order (managed / boot-bom / asis / repository / unknown);
        `check_managed=False` skips step 1 (the build already said the version is missing). Never raises."""
        try:
            if check_managed and self.managed_version(key, module):
                return None, "managed"
            v, _how = self.asis_fact(key, module)
            # 목표 Boot BOM의 버전을 as-is 버전보다 먼저 따르되, as-is 버전이 더 새것이면 버전을 내리지 않는다
            bv = self.boot.get(key) or None          # not the chain's (step 1, or the build said missing)
            if bv:
                from sbmig.plan.versions import compare_versions
                if v and compare_versions(v, bv) == 1:
                    return v, "asis"
                return bv, "boot-bom"
            if v:
                return v, "asis"
            if allow_repository and key.startswith("org.springframework"):
                # v2.1z-n (company run: `spring-dao` lost its version, the loop tried to pin it): a Spring artifact that no chain /
                # BOM manages has no release on the target line — the repository's newest is an obsolete one (spring-dao 2.0.8,
                # 2007) and would drag Spring 2 next to Spring 7. Unknown on purpose: the LLM removes / replaces the artifact
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
    """Maven 빌드 오류를 케이스별 수정 시도로 고치는 수정 루프 클래스. 오케스트레이터가 실행마다 하나
    만들고(``budget`` / ``blocked`` / ``errors``는 공유) 검증 빌드가 실패하면 ``run``을 호출한다.
    케이스의 다음 시도 방식은 트리아지(``sbmig.triage.scripts``)가 정하고, 시도 실행, 결과 평가, 되돌리기,
    KB 실적 기록은 이 클래스가 맡는다.
    KB가 없으면 LLM 스크립트는 저장하지 않고 v0로 실행하며 재사용도 하지 않는다.

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
        """협력 객체와 실행 중 상태(시도 이력, 차단 목록, 플래그, 후속 조치, LLM 장애 상태, 시도별 기록)를 준비한다.
        ``history``는 같은 실행의 여러 루프가 공유할 수 있으며, 시도 순서와 횟수 한도는 이 이력을 기준으로 한다."""
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
        # 재구성 결과와 모드는 오케스트레이터가 넣어 주며, 프롬프트의 "Project after the restructure" 절을 만들 때 쓴다
        # the run's RestructureResults and their modes (set by the orchestrator) → FixContext.layout_facts
        self.restructure_results: list[Any] = []
        self.restructure_modes: list[Optional[str]] = []
        self._layout_cache: Optional[tuple[int, list[str]]] = None
        # v2.1v (E-L9): (file, signature) of an error that first appeared in the files of an applied try → that try
        self._introduced: dict[tuple[str, str], FixAttempt] = {}
        # v2.1y (E-M1): attempt → the blocker sentence of its declined answer; the cases BLOCKED needs_owner
        # (case id, files, artifact keys, sentence) — their same-cause siblings are blocked without an LLM call
        self._declines: dict[int, str] = {}
        self._owner_blocked: list[tuple[str, frozenset[str], frozenset[str], str]] = []
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
        # 시도 id 문자열은 MSA 서비스별 루프에서 겹칠 수 있으므로 시도별 상태는 FixAttempt 객체의 id()를 키로 쓴다
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
        # 버전 결정기(처음 쓸 때 생성)와, 누락 버전 사전 시도가 이미 확인한 (pom, g:a) 쌍을 기억한다
        # the version resolver (lazy, one per loop); the (pom, g:a) pairs the zero-LLM missing-version try looked at
        self._version_resolver: VersionResolver | None = None
        # 같은 사전 시도를 반복하지 않도록 이미 확인한 키(출처, pom 또는
        # 모듈, g:a 또는 확장자)와 적용 실패 횟수를 기억한다
        # the zero-LLM pre-tries' keys already looked at — (source, pom or module, g:a | extensions) — and apply failures
        self._pre_tried: set[tuple[str, str, str]] = set()
        self._pre_failures: Counter = Counter()
        self._jakarta_plans: dict[str, Any] = {}     # v2.1z-e: cluster id → the sbmig-jakarta plan (computed once a round)
        self._javax_owner_libs: dict[str, list[GAV]] = {}   # v2.1z-e: cluster id → libraries mapped without the classpath
        self._javax_owner: dict[str, Optional[str]] = {}    # v2.1z-e: cluster id → the owner class the fallback worked from
        self._cases = CaseScripts.of(kb)
        attach = getattr(triager, "attach_run", None)
        if callable(attach):
            try:
                attach(blocked_registry=self.blocked, ws=ws, cases=self._cases if self._cases.active else None)
            except Exception as e:  # pragma: no cover - a foreign triager
                log.warning("triager.attach_run failed: %s", e)
        # 지식 노트(knowledge/notes)를 트리아지에도 전달해
        # `replacement: true` 노트에 맞는 라이브러리는 차단하지 않게 한다
        self._notes_index: Optional[list[Any]] = None
        if callable(attach):
            try:
                attach(notes=self._knowledge_notes())
            except Exception as e:  # pragma: no cover - a foreign triager without the keyword
                log.debug("triager.attach_run(notes=…) not accepted: %s", e)

    @property
    def cases(self) -> CaseScripts:
        """KB 케이스 저장소(``CaseScripts`` 어댑터)를 반환한다. KB가 없으면 비활성 상태의 저장소다.

        The KB's case store (`kb.cases` through the `CaseScripts` adapter; inactive without a KB)."""
        return self._cases

    # ------------------------------------------------------------------ public
    def _mark(self, what: str) -> None:
        """감시 스레드가 볼 수 있도록 루프가 지금 하는 일을 기록하고, sbmig.log에 DEBUG 한 줄을 남긴다.

        Milestone for the watchdog: what the loop is doing right now (also a DEBUG line in sbmig.log; the line's
        stage says where)."""
        self._milestone, self._milestone_at = what, time.monotonic()
        log.debug("→ %s", what)

    @contextmanager
    def _busy_with(self, what: str):
        """스스로 진행 로그를 남기는 호출(Maven 빌드 등)이 실행되는 동안 감시 스레드가 경고하지 않게 하고,
        끝나면 무응답 시간을 처음부터 다시 잰다.

        A call that logs its own heartbeat (Maven build, OpenRewrite run): the watchdog stays silent meanwhile, and
        its quiet time starts again when the call returns."""
        prev, self._busy = self._busy, what
        try:
            yield
        finally:
            self._busy = prev
            self._quiet_from = time.monotonic()

    def _activity(self) -> Optional[str]:
        """스스로 진행 로그를 남기는 작업(이 루프의 빌드, LLM HTTP 호출, Maven 자식 프로세스)이 실행 중인지
        확인해 그 이름을 반환한다.
        실행 중인 작업이 없으면 None을 반환하고, 이때만 감시 스레드가 경고한다.
        긴 LLM 호출을 멈춤으로 오인하지 않기 위해서다.

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
        """감시 데몬 스레드를 시작한다. 루프가 ``WATCHDOG_QUIET_S`` 동안 조용하면 마지막 진행 내용을
        WARNING으로 남기고, ``WATCHDOG_DUMP_S``가 지나면 스레드 덤프를 실행 디렉토리에 기록한다.
        운영자는 sbmig.log의 이 줄을 보고 멈췄는지 판단한다.

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
        """감시 스레드를 켜고 수정 루프를 실행한다.
        ``first_build``는 루프를 시작하게 한 실패한 ``clean package`` 결과다.
        같은 트리를 다시 빌드하지 않고 이 결과를 첫 빌드로 쓰며, 그 오류는 이미 오류 목록에 있다.

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
        """수정 루프의 본체를 실행한다. 바깥 반복 한 번이 빌드 한 번이다(시도 결과 평가 -> 오류 목록 갱신 ->
        성공이면 종료 -> 정체와 진동 감지 -> 종료 조건 확인).
        안쪽 반복은 수정 라운드다.
        트리가 바뀌지 않았으면 재빌드하지 않고 같은 빌드 결과로 각 케이스의 다음 시도를 실행한다.
        이런 라운드도 ``max_iterations``에 포함되며, 루프는 항상 빌드 뒤에 끝난다."""
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
                # 직전 라운드의 시도를 이 빌드로 평가한다.
                # 악화된 시도는 여기서 되돌리고 되돌린 트리를 다시 빌드한다
                reverted = self._score(pr, pending, build, prev_build, prev_restored)
                scored = [p.attempt for p in pending]
                # 되돌린 뒤 재빌드에서는 되돌린 라운드에서 판단할 수 없었던 시도를 다시 평가한다
                # pom 수정이 만든 새 의존성 오류가 대상 오류를 가렸던 경우이며, 그때까지 FixTry는 대기 상태다
                # a revert rebuild shows the tree minus the reverted commits against the same "before" build: tries
                # the reverted round left inconclusive (a pom edit's new dependency error hid their errors) are
                # scored again — their FixTry stays pending until then
                pending = [p for p in pending if p.attempt.outcome == "inconclusive"] if reverted else []
                again = {id(p.attempt) for p in pending}
                for p in pending:
                    p.attempt.notes.append("scored again on the rebuild after the revert")
                # 보이지 않는다고 해결된 것은 아니므로 되돌린 라운드의 빌드, 오류를 파싱하지 못한 실패, 타임아웃은
                # 어떤 오류도 해결된 것으로 처리하지 않는다(void)
                # a reverted round's build shows a tree that no longer exists; a failed build without parsed errors
                # (or a timeout) says nothing about which errors are gone → void: the list resolves nothing
                void = bool(reverted) or (not build.ok and not build.errors) or bool(build.timed_out)
                added = 0
                if not reused:     # the reused first build is on the list already (the caller's `build`)
                    # 결과를 정하기 전에 먼저 오류 목록에 넣고, 대기 중인 시도의
                    # 항목은 열어 두어 resolve가 처리하게 한다
                    # 다른 케이스의 수정으로 사라진 항목은 여기서 해결 처리하고, 이번 라운드에 그 파일이나
                    # pom을 바꾼 시도를 ``resolved_by``에 남긴다
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
                # 노드 경계까지 기다리면 웹이 Job을 지워 보고서가 사라지므로 웹의
                # 멈춤 요청(stop.request)은 빌드 사이에 확인한다
                # 여기서 빠져나가면 다음 노드나 finalize가 aborted로 마무리한다
                if getattr(self.ws, "stop_requested", lambda: False)():
                    pr.notes.append(f"build {build_no}: stop requested (stop.request) — leaving the fix loop before the next build")
                    log.warning("stop requested (stop.request) — leaving the fix loop after build %d (no further fix)", build_no)
                    self.ws.journal({"event": "stop_request", "phase": phase.value, "iteration": build_no, "where": "fix loop"})
                    break
                # Maven의 HTTP 차단 미러에 막혔으면 부모 버전 맞추기, 사전 시도, LLM
                # 어느 것으로도 해결할 수 없으므로 바로 중단한다
                if self.http_blocked_abort(pr, phase, build, build_no):
                    break
                # 사내 parent를 찾지 못하면 아무것도 빌드할 수 없고 코드 수정으로도 해결되지 않으므로 실행을 중단한다
                if self._parent_abort(pr, phase, build, build_no):
                    break
                # 정체는 오류 집합이 직전 빌드와 같은 경우, 진동은 그보다 이전 빌드와 같은 경우다
                # 정체가 쌓이면 상위 모델로 바꾸고, 진동이 2번 일어나면 관련 케이스를 OSCILLATION으로 차단한다
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
                if oscillating and oscillations >= max(1, int(getattr(self.cfg.loop, "oscillation_limit", 2) or 2)):
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
                # 무엇이든 적용됐거나(재빌드 필요), 시도가 없거나, 라운드를 다 썼거나, 시간이 초과됐거나,
                # 모두 차단됐으면 안쪽 반복을 끝낸다
                if self.dry_run or applied_any or not tried or fix_rounds >= max_iterations \
                        or self.budget.time_exceeded() or all(self.blocked.covering(c) for c in clusters):
                    break
                # 트리가 그대로라 재빌드해도 오류가 같으므로 같은 빌드 결과로 다음 시도를 실행한다.
                # 이 라운드도 라운드 수에 포함된다
                # nothing changed the tree, so a rebuild would show the same errors — the next rung of each case's
                # ladder on the same build (bounded by script.max_llm_tries_per_case)
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
        # 끝까지 결과를 판단할 수 없었던 시도도 마지막에 한 번 KB 실적으로 기록한다
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
        """직전 라운드에서 평가된 시도의 후속 처리를 한다.
        - 오류 목록의 FixTry 결과와 메모를 맞추고, 해결된 케이스 항목(개선, 빌드 성공, 부분 해결)을 해결 처리한다
        - 실행된 저장 스크립트에 실적을 기록하고, 시도마다 결과를 로그 한 줄로 남긴다

        The scored tries of the previous round: settle their FixTry (result, notes), resolve the error list's
        entries of a resolved case, credit the stored script that ran, one result line per try (in its case's
        stage)."""
        for a in scored:
            case_id, n, rnd = self._tries.get(id(a), (a.script_case_id, 0, 0))
            gone, targeted = self._tally.get(id(a), (0, len(a.cluster_errors)))
            self._sync_try(a)
            # 부분 해결이어도 사라진 항목은 이 시도로 해결 처리하고, 남은 항목은 열어 둔 채 다음 시도에 넘긴다
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
        """빌드 결과를 요약한 로그 한 줄(오류 수, 새로 추가된 항목, 케이스별 해결 수, 남은 항목)을 남긴다.

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

    # ------------------------------------------------------------------ blocked bookkeeping
    def _touch_item(self, item_id: str) -> None:
        if item_id not in self._phase_items:
            self._phase_items.append(item_id)

    def _only_blocked(self, pr: PhaseResult, clusters: list[ErrorCluster], build_no: int) -> bool:
        """남은 클러스터가 모두 이미 차단됐으면 True를 반환해 단계를 끝낸다.
        고칠 수 없는 오류에 라운드를 쓰지 않기 위해서다."""
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
        """BLOCKED 결정의 근거(BlockedCandidate)를 반환한다. 트리아지가 주지 않았으면 결정 문구의
        ``blocked: <사유>``에서 읽고, 사유를 알 수 없으면 반복 실패로 본다."""
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
        """``cand``에 포함된 오류를 차단 항목으로 등록하고, 오류 목록에서도 그 케이스를 차단 처리한다.
        javax나 외부 API 오류면 classpath에서 라이브러리를 찾아 조치 방법 문구를 만든다.
        차단되지 않은 나머지 오류를 반환하며, 이 오류는 계속 고친다.

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
            if reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.EXTERNAL_API_INCOMPATIBLE, BlockReason.NEEDS_OWNER):
                item.suggestion = self._with_eol_advisory(item.suggestion, artifact)   # v2.1z-k: "no jakarta build" + why
        if reason in (BlockReason.EXTERNAL_JAVAX_API, BlockReason.NEEDS_OWNER) and _mentions_javax(errs):
            # 어디를 찾아봤는지 사람이 볼 수 있도록 탐색 기록을 조치 방법 뒤에 붙이고 메모에도 남긴다.
            # 의존성을 찾지 못한 소유 클래스는 찾지 못했다고 함께 기록한다 / what sbmig searched (and an owner class it could not match) on the item
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

    def _classpath_hints(self, errs: list[BuildError], owner: Optional[str], required: Optional[str],
                         found: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """비어 있는 값(소유 클래스, 요구 타입, 실제 타입)을 classpath 도구로 채운다.
        도구가 그 기능을 제공할 때만 쓴다.

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
        """아직 차단되지 않은 오류만 주어진 사유로 차단한다.
        예산 소진, LLM 사용 불가, 진동처럼 루프가 직접 내리는 차단에 쓴다."""
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
        """진동이 반복되면 이 단계에서 실제로 수정을 시도한 클러스터만 OSCILLATION으로 차단해 사람이 확인하게 한다."""
        # round 10: the clusters this phase actually worked on — not every cluster that shares an L1 signature with one
        involved = [c for c in clusters if _same_cluster(c, pr.attempts, self.blocked.canonical_file)] or clusters
        why = (f"fixes kept toggling between the same error sets (A↔B↔A) {int(getattr(self.cfg.loop, 'oscillation_limit', 2) or 2)} "
               "time(s) within loop.oscillation_window (loop.oscillation_limit)")
        pr.notes.append(f"build {iteration}: oscillation repeated → blocking {len(involved)} cluster(s)")
        self._block_remaining(pr, phase, iteration, involved, BlockReason.OSCILLATION, why)

    @staticmethod
    def build_text(build: Any) -> str:
        """실패한 빌드나 OpenRewrite 실행의 오류 메시지, 원문, 요약, 로그 파일 내용을 합친 텍스트를 반환한다.

        The text of a failed build (or OpenRewrite run): the errors' message / raw, the summary / error text, the log file."""
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
        """실패한 Maven 실행에 HTTP 차단의 증거(``Blocked mirror for repositories`` 문구나
        ``http://0.0.0.0``)가 있으면 실행을 중단한다. 미러 id만으로는 판단하지 않는다.
        중단하면 플래그 ``aborted:http-blocked``, 저널 ``maven_http_blocked``, ERROR 한 줄, 인수인계 한 줄을
        남긴다. 이번에 또는 이미 중단했으면 True, 증거가 없으면 False를 반환한다.

        Maven's HTTP repository blocker in a failed build / OpenRewrite run → abort the run once: flag `aborted:http-blocked`,
        journal `maven_http_blocked` (repositories, no credentials) + `abort`, one ERROR line, one Handover line. No parent
        alignment, zero-LLM pre-try or LLM call can change it. False when the text shows no blocker."""
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
        """설정된 사내 parent(root / boot / comp)를 찾지 못하면 아무것도 빌드할 수 없으므로 실행을 중단하고,
        settings, Nexus, ``artifacts.parent``를 확인하라는 차단 항목을 남긴다.
        코드 수정으로 해결되지 않으므로 LLM을 호출하지 않는다.

        The configured in-house parent is not resolvable → nothing can build: abort the run with a clear item."""
        keys = set(self.cfg.artifacts.parent_keys())           # root parent + boot_parent / comp_parent (round 9b)
        # 사내 부모 g:a라도 설정 버전이 아닌 as-is 버전을 찾지 못한 경우는
        # 사전 시도 sbmig-parent가 맞추므로 중단하지 않는다
        # 설정 버전 자체이거나 버전을 모를 때, 또는 openrewrite.unresolvable_asis_parent: abort일 때만 중단한다
        errs = [e for e in build.errors if e.kind == ErrorKind.PARENT_RESOLUTION
                and e.artifact is not None and e.artifact.key in keys and self._parent_miss_aborts(e)]
        # 사내 부모가 <build><extensions>에 선언한 확장(wagon-webdav 등)을 Maven이 받지 못한 경우다
        # 상속된 확장은 프로젝트에서 뺄 수 없고 어떤 goal도 실행되지
        # 않으므로, 부모를 찾지 못한 경우처럼 LLM 없이 중단한다
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
            else:                                           # an as-is version under `unresolvable_asis_parent: abort`
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

    def _parent_miss_aborts(self, e: BuildError) -> bool:
        """사내 부모 g:a를 찾지 못한 오류가 실행을 중단시키는지 판단한다. 설정 버전 자체이거나 버전을 모르면
        중단하고, as-is 버전이면 ``openrewrite.unresolvable_asis_parent: abort``일 때만 중단한다.

        Does this company-parent resolution failure abort the run? The configured version (or an unknown one): yes; an as-is
        version: only under `openrewrite.unresolvable_asis_parent: abort`."""
        try:
            from sbmig.restructure.asis_parent import classify_gav
            kind = classify_gav(e.artifact, self.cfg, self.ws).kind
        except Exception as ex:  # noqa: BLE001 - unknown → the old behaviour (abort)
            log.debug("parent miss classification failed: %s", ex)
            return True
        if kind == "asis-version":
            return getattr(self.cfg.openrewrite, "unresolvable_asis_parent", "align") == "abort"
        return kind == "configured"

    def _chain_extensions(self) -> dict[str, str]:
        """사내 부모 체인이 선언한 빌드 확장을 {g:a: g:a:v} 형태로 반환한다.
        선언한 pom은 ``_extension_declarer``에 기억하고, 읽지 못하면 빈 dict를 반환한다."""
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
        """이 빌드의 `Unresolveable build extension` 오류 중 좌표(g:a)가 사내 부모 체인의 확장과 같은 것을 찾는다."""
        cands = [e for e in build.errors if e.kind == ErrorKind.POM_INVALID and e.artifact is not None
                 and "build extension" in (e.message or "").lower()]
        if not cands:
            return []
        chain = self._chain_extensions()
        # the full g:a:v: a project pom that redeclares the same g:a at another version owns that declaration (Maven merges
        # extensions by g:a, child wins) — that one stays an LLM case
        return [e for e in cands if chain.get(e.artifact.key) == e.artifact.coordinates] if chain else []

    def skips_for(self, target: Optional[str]) -> tuple[bool, bool]:
        """``target`` 빌드에 적용할 대체 빌드 결과(test-compile 건너뜀, 테스트 실행 건너뜀)를 반환한다.
        MSA 서비스 하나의 대체 빌드가 다른 서비스의 최종 빌드를 바꾸면 안 되므로, 서비스 빌드는 자기 결과만,
        프로젝트 전체 빌드는 모든 서비스의 결과를 물려받는다.

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
        """이 단계가 test-compile을 건너뛴 대체 빌드로 통과했는지 단계 메모로 판단한다."""
        return any("test-compile-skipped" in n for n in pr.notes)

    @staticmethod
    def tests_skipped_in(pr: PhaseResult) -> bool:
        """이 단계가 테스트 실행을 건너뛴(-DskipTests) 대체 빌드로 통과했는지 판단한다."""
        return any("tests-skipped:" in n for n in pr.notes)

    # ------------------------------------------------------------------ build
    def _build(self, goal: str, target: Optional[str], label: str, phase: Phase, iteration: int,
               skip_test_compile: bool = False, skip_tests: bool = False) -> BuildResult:
        """루프의 빌드를 한 번 실행한다. 기본으로 ``-Dmaven.test.skip=true``를 붙인다.
        빌드 부산물을 치우고 케이스 키를 다듬어(``refine_build``) 시도 결과 평가, 오류 목록, 클러스터, 차단
        목록이 같은 L1 / L2 서명을 보게 한다."""
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
        """직전 라운드의 시도를 이 빌드 결과로 평가한다. 되돌리기로 트리가 바뀌었으면 True를 반환한다.
        - improved는 대상 오류가 모두 사라지고 빌드가 그 단계까지 진행된 경우, partial은 일부만 사라지고
          수정한 파일에 새 오류가 없는 경우다
        - regressed는 수정한 파일에 새 오류가 생겼거나, 오류 수가 ``regression_ratio``배를 넘었거나, pom
          수정 뒤 새 구조적 오류나 더 이른 단계의 오류가 대상을 가린 경우이며 되돌린다
        - inconclusive는 빌드로 판단할 수 없는 경우(타임아웃, 파싱된 오류 없음)이며 KB 실적 없이 되돌린 뒤
          재빌드에서 다시 평가한다. 되돌리기로 다시 나타난 오류(``restored``)는 새 오류로 세지 않는다

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
        # 타임아웃이거나 파싱된 오류가 없는 실패 빌드로는 어떤 시도도 판단할
        # 수 없으므로 모두 판단 불가로 둔다(KB 실적 없음)
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
            # pom을 고친 시도 뒤에 새 구조적 오류(pom 무효, 의존성 해결 실패 등)가 나타나면 오류 수와 상관없이 되돌린다
            uncovered = self._uncovered_stage(a, left_of[id(a)], real, new_struct) if new_struct and pom_touched else []
            if uncovered:
                # pom 문제를 고치면 빌드가 다음 단계로 나아가 가려져 있던
                # 오류("Could not find artifact" 등)가 처음 보일 수 있다
                # 대상 오류가 모두 사라졌고 새 구조적 오류가 모두 더 뒤 단계에
                # 있으면 이 시도가 악화시킨 것으로 보지 않는다
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
            # 다른 파일에 같은 L1 서명이 있어도 수정한 파일에 생긴 오류는 새 오류이므로 (파일, 서명) 횟수로 센다
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
        # 남은 시도가 수정한 파일에 새로 생긴 오류는 그 시도와 연결해,
        # 그 오류 케이스의 프롬프트에 "introduced by script:<case>:v<N>"으로 알린다
        if not build.ok:
            for p in pending:
                a = p.attempt
                if a.outcome == "reverted" or not a.applied:
                    continue
                for e in _new_in_files(build.errors, _touched(a), prev_count):
                    self._introduced.setdefault(_pair(e), a)
        for p in pending:
            self.ws.journal({"event": "attempt_scored", "phase": pr.phase.value, "attempt": p.attempt.id,
                             "cluster": p.attempt.cluster_id, "outcome": p.attempt.outcome,
                             "cluster_resolved": p.attempt.cluster_resolved, "errors_after": total})
        return reverted

    @staticmethod
    def _uncovered_stage(a: FixAttempt, left_targeted: tuple[int, int], real: list[BuildError],
                         new_struct: list[str]) -> list[BuildError]:
        """pom을 고친 시도가 대상 오류를 모두 없앴고 새 구조적 오류가 모두 대상보다 뒤 단계면 그 오류 목록을
        반환한다. 아니면 빈 목록을 반환하고 평소대로 되돌린다.
        이런 오류는 시도가 만든 악화가 아니라 pom 문제에 가려져 있던 다음 단계의 오류다.

        The new structural errors when a pom-editing attempt removed EVERY targeted error and all of them are of a LATER
        stage than the targets (a stage-0 pom problem fixed → stage-1 dependency resolution now visible): uncovered, not
        caused. [] otherwise (a new structural error of the same or an earlier stage is the regression it always was)."""
        left, _targeted = left_targeted
        if left:
            return []
        stage = _stage_of(a.cluster_errors)
        new = [e for e in real if e.kind.value in new_struct]
        if not new or any((error_stage(e) if error_stage(e) is not None else 99) <= stage for e in new):
            return []
        return new

    # ------------------------------------------------------------------ fixing
    def _kb_match(self, cluster: ErrorCluster) -> KBMatch:
        """KB에서 이 클러스터를 조회한다(사람이 ``block``으로 고정한 케이스, 케이스의 마지막 해결 힌트).
        조회에 실패해도 빈 결과로 계속한다."""
        try:
            m = self.kb.match(cluster, self.ws, self.profile)
            return m if m is not None else KBMatch()
        except Exception as e:
            log.warning("kb.match failed for %s: %s", cluster.id, e)
            return KBMatch()

    def _plan(self, clusters: list[ErrorCluster]) -> list[tuple[ErrorCluster, Optional[TriageDecision], list[str]]]:
        """트리아지로 이번 라운드의 처리 계획을 세운다.
        근본 원인 순으로 정렬한 (클러스터, 가려짐 결정, 가리는 클러스터 목록)을 반환한다.
        pom 문제가 있으면 그 모듈의 컴파일 오류는 가려 두고 원인을 먼저 고친다."""
        planner = getattr(self.triager, "plan_iteration", None)
        if not callable(planner):
            return [(c, None, []) for c in clusters]
        return [(it.cluster, it.skip, list(it.masked_by)) for it in planner(clusters)]

    def _decide(self, phase: Phase, iteration: int, stall_level: int, cluster: ErrorCluster,
                match: KBMatch) -> TriageDecision:
        """트리아지에게 결정(SCRIPT / BLOCKED / SKIP / ESCALATE)을 받는다.
        차단 정책(``loop.blocked_policy``), 시도 한도, 고정된 케이스는 트리아지가 판단한다.
        트리아지에서 예외가 나도 루프가 멈추지 않도록 ESCALATE를 반환한다."""
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
        """이 클러스터에 대해 앞서 결과가 평가된 시도를 반환한다.
        시도 순서와 횟수 한도는 이 목록을 기준으로 한다.
        같은 L1 서명의 다른 파일, 이번 라운드에 대기 중인 시도, LLM 쿨다운을 일으킨 전송 실패는 뺀다.
        쿨다운 때문에 케이스의 시도 한도가 줄면 안 되기 때문이다.
        sbmig 사전 시도는 ``ladder=True``일 때 빠져 LLM 시도 한도를 쓰지 않고, 프롬프트에 보여 줄 이전
        시도(``ladder=False``)에는 들어간다.

        Earlier SCORED attempts on THIS cluster (round 10: not other files that share an L1 signature, not this
        round's pending ones), without the transport failures that triggered the LLM cool-down (L5): the cool-down
        round must not count against the case's limits. v2.1l / v2.1m: the zero-LLM pre-tries (`PRE_TRY_SOURCES`) are not
        ladder steps (`ladder=True`: left out, they never use up an LLM try); the prompt's history keeps them."""
        return [a for a in _same_cluster(cluster, self.history, self.blocked.canonical_file)
                if a.id not in self._cooled_attempts and a.outcome != "pending"
                and not (ladder and a.source in PRE_TRY_SOURCES)]

    def _sibling_hints(self, cluster: ErrorCluster, limit: int = 3) -> list[str]:
        """이 실행에서 다른 파일이나 범위의 같은 오류를 어떻게 고쳤는지, 또는 무엇이 실패했는지 찾는다.
        프롬프트 힌트로만 쓴다.

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
        """오류를 케이스(L2 서명)마다 클러스터 하나로 묶는다.
        한 클러스터는 여러 파일에 걸칠 수 있고, 루프의 빌드 대상(MSA 서비스) 안의 오류만 담는다.

        v2: one cluster per case (L2) across files, scoped by the loop's build target (MSA services)."""
        return self.triager.cluster(errors, self.profile, scope=target)

    def _fix_round(self, pr: PhaseResult, phase: Phase, iteration: int, build: BuildResult, target: Optional[str],
                   stall_level: int, clusters: Optional[list[ErrorCluster]] = None, *, round_no: int = 1,
                   max_rounds: int = 0) -> tuple[list[_Pending], bool, list[TriageDecision], bool, list[BuildError]]:
        """수정 라운드 하나를 실행한다. 케이스마다 최대 1번 시도하고, 라운드당 ``loop.max_clusters_per_iteration``개
        케이스까지 다룬다(미뤄진 케이스와 차단된 케이스는 세지 않는다).
        가리고 있는 쪽이 차단되거나 에스컬레이트되어 손댈 수 없으면 가려진 케이스도 함께 다룬다.

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
        # 가리고 있는 쪽(pom 문제 등)이 차단되거나 에스컬레이트되어 손댈 수
        # 없으면 고칠 수 있는 컴파일 오류를 더 숨기지 않는다
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
        """이미 차단된 오류를 뺀 클러스터를 반환한다.
        남는 오류가 없으면 BLOCKED 결정을 기록하고 None을 반환한다.

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
        """트리아지의 마지막 SCRIPT 결정이 정한 시도 방식(reuse / augment / new, 한도에 도달하면 mode
        None)을 반환한다. 트리아지가 주지 않으면 ``plan_script_step``으로 계산한다.

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
        """``c``를 다음 빌드까지 미뤄야 하는지 판단해 (원인 케이스 목록, "files" 또는 "pom")을 반환한다.
        ([], "")이면 지금 시도한다.
        - "files"는 이번 라운드의 수정이 그 오류 파일 전부에 이미 실행된 경우, "pom"은 이번 라운드의 수정이
          컴파일 케이스의 모듈 pom이나 루트 pom을 바꾼 경우다
        - 근본 원인을 먼저 고치면 뒤따르는 케이스는 재빌드에서 대개 사라지므로, 원인 하나에 LLM 호출 1번으로 끝난다
        - 재사용, 파일이 없는 오류, 라운드의 첫 케이스는 미루지 않는다

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
        """파일이 속한 모듈의 pom(가장 가까운 상위 pom.xml)을 찾는다.
        알 수 없으면 None을 반환하며, 이 경우 미루지 않을 뿐이다.

        The module pom of a project file (`sbmig.fix.context.nearest_pom`); None when it cannot be told."""
        try:
            from sbmig.fix.context import nearest_pom
            return nearest_pom(self.ws, rel)
        except Exception as e:  # noqa: BLE001 - deferral is an optimisation: never fail an attempt over it
            log.debug("nearest pom of %s unavailable: %s", rel, e)
            return None

    def _handle_cluster(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str = "") -> str:
        """케이스 하나에 대해 결정, 시도(저장 스크립트, 또는 LLM 답의 edits나 스크립트), 커밋을 차례로 실행한다.
        로그는 모두 케이스 단계 태그(``fix r<n> <case_id>``) 아래에 남는다. 반환값:
        blocked / escalated / skipped / deferred / attempted.

        Decide → the case's try (stored script / LLM answer: edits or script) → commit, every log line in the case's
        stage (`fix r<n> <case_id>`). Returns "blocked" (nothing left to do for this cluster), "escalated",
        "skipped", "deferred" (its files are being fixed by an earlier case of this round) or "attempted"."""
        with stage(case_stage(st.round, c)):
            return self._handle_case(pr, st, c, prefix)

    def _handle_case(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, prefix: str) -> str:
        """케이스 하나를 처리한다. 시도 순서를 다 썼으면(mode None) 반복 실패로, LLM이 필요한데 LLM을 쓸 수
        없거나 예산이 없으면 그 사유로 차단한다.
        일부 오류만 차단되면 나머지 오류로 다시 결정한다(최대 4번). 미뤄진 케이스는 시도로 세지 않는다."""
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
            # 소유팀의 결정이 필요해 차단된 케이스와 같은 파일이나 같은 아티팩트의
            # 케이스는 LLM을 호출하지 않고 같은 사유로 차단한다
            if step is not None and step.needs_llm:
                owner = self._owner_cause(c)
                if owner is not None:
                    return self._block_same_cause(st, c, owner, prefix)
            forced = None
            # 시도 순서를 다 썼거나(script.max_llm_tries_per_case 소진, 반복 실패), LLM이 필요한데 LLM을 쓸
            # 수 없거나 예산이 없으면 강제로 차단한다
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
            # 누락 버전과 바이너리 리소스 필터링 케이스는 시도 순서, 한도, LLM
            # 장애와 상관없이 LLM보다 먼저 sbmig가 한 번 고쳐 본다
            # 단, 같은 라운드에 다른 케이스가 바꾼 pom이면 기다려야 하므로 아래 미루기 검사 뒤에 실행한다
            # the zero-LLM pre-try (decided on the triage decision, run after the deferral check below)
            pre, pre_poms = self._pre_try_for(c, d)
            if forced is not None:
                d = TriageDecision(cluster_id=c.id, kind=DecisionKind.BLOCKED,
                                   rationale=prefix + f"blocked: {forced.reason.value} — {forced.evidence[0]}")
                step = None
            # 같은 라운드에 앞 케이스가 이미 그 파일이나 모듈 pom을 고쳤으면 재빌드에서 대개 함께 사라지므로 미룬다
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
        """로그에 남길 "KB 탐색: …" 한 줄을 만든다.
        SCRIPT 결정이면 시도 방식(실행, LLM 보강 요청, 한도 도달)을, 아니면 찾은 내용과 결정을 쓴다.

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
    def _attempt_name(self, a: FixAttempt) -> str:
        """힌트와 로그에 쓰는 시도 이름(``script <case>:v<N> (<mode>)``)을 만든다.

        Who made an attempt (hints, logs): `_script_name`."""
        return _script_name(a)

    def _project(self) -> str:
        """KB 기록에 쓰는 프로젝트 이름을 반환한다.
        ``sbmig.kb.common.project_name``이 있으면 그것을, 없으면 프로필 이름을 쓴다.

        The project name the KB records use (`sbmig.kb.common.project_name` when it exists)."""
        try:
            from sbmig.kb.common import project_name
            return project_name(self.profile, self.ws)
        except Exception:  # noqa: BLE001 - the KB package may be gone (v2.1): the profile's name
            return (getattr(self.profile, "name", "") or "") or self.ws.root.name

    def _script_attempt(self, pr: PhaseResult, st: "_Round", c: ErrorCluster, d: TriageDecision, step: Any) -> None:
        """케이스를 한 번 시도한다. 아래 순서를 지켜야 한다.
        1. 보강의 기준(base)을 트리가 바뀌기 전에 만든다
        2. 직전 시도가 개선이나 빌드 성공이 아니면 되돌린다. 그 시도가 없앴던 오류가 케이스로 돌아온다
        3. 저장 스크립트(``kb-script``)를 실행하거나 LLM 답을 적용(``_llm_try``)하고 커밋한다.
          새 시도가 아무것도 바꾸지 못하면 되돌리기를 취소해 트리를 마지막 빌드 때 상태로 둔다

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
        # 수정이 실행된 오류 파일과 바뀐 pom을 기록해 같은 라운드의 뒤 케이스가 다음 빌드까지 기다리게 한다
        if self._ran_on_files(att):                     # a fix ran on its files: later cases there wait for the
            for e in c.errors:                          # rebuild (a static-check refusal never ran)
                if e.file:
                    st.covered.setdefault(_norm_path(e.file), c.id)
        if att.applied:                                 # a pom it changed: that module's compile cases wait too
            for ch in att.changes:
                for path in (ch.path, ch.new_path):
                    if path and PurePosixPath(_norm_path(path)).name == "pom.xml":
                        st.poms.setdefault(_norm_path(path), c.id)
        # 직전 시도를 되돌렸고 새 시도가 적용됐으면 직전 시도는 최종 트리에 없으므로 "rolled back"으로
        # 표시해 운영자 확인에서 뺀다
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
        self._after_decline(st, c, att)                 # v2.1y: the declined case's BLOCKED needs_owner (recorded first)

    def _llm_try(self, att: FixAttempt, c: ErrorCluster, st: "_Round", step: Any, base: Optional[ScriptBase]) -> None:
        """신규나 보강 시도에서 LLM에 원인, 해결 방법, edits, 스크립트를 요청하고 그 답을 적용한다.
        - 보강 답에 스크립트가 있으면 새 버전으로 저장하고 실행한다(``llm-augment``).
          스크립트 없이 edits만 있으면(``no_script_reason``) edits를 직접 적용한다(``llm-edit``)
        - 신규 답에 edits가 있으면 edits를 바로 적용하고 스크립트는 unverified로 저장만 한다(``llm-new``).
          스크립트만 있으면 저장하고 실행한다
        - 먼저 고른 쪽이 적용되지 않으면(edits 실패나 변화 없음, 스크립트 실패) 같은 답의 나머지 쪽을
          적용하고 시도에 메모를 남긴다

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
        # <blocked>나 분석만 있는 답은 프로젝트 밖의 일이라는 거절로 보고 같은 질문을 반복하지 않는다
        declined = self._declined_reason(ans, code, edits)
        if declined:
            fact = self._decline_contradiction(c, declined)
            carries = bool(code.strip() or edits)
            if fact:
                # sbmig가 찾은 정보(jakarta 빌드, 번들 jar, 대체 방법 노트)와 모순되는 거절은 받지 않고,
                # 거절 횟수에 넣지 않은 채 다음 라운드에 그 정보와 함께 다시 요청한다
                # 받지 않은 거절에 edits나 script가 있으면 버리지 않고 보통 답처럼 적용한다
                # a decline is rejected when a fact contradicts it (a jakarta build found, a bundled jar, a replacement note):
                # not a decline (no count). Without edits / a script: an ordinary failed try, asked again with the fact;
                # with them (v2.1z-aa): the answer is applied below like any other
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
        # 신규는 이 프로젝트를 바로 고치기 위해 edits를 먼저 적용하고,
        # 스크립트는 다음 프로젝트용으로 unverified 저장한다
        # 보강은 저장된 스크립트를 고치는 것이 목적이므로 스크립트가 먼저이고, 스크립트가 없을 때만 edits를 쓴다
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

    def _declines_allowed(self) -> int:
        """케이스당 허용하는 거절 횟수(``script.max_declines_per_case``)를 반환한다.
        0 이하면 거절로 분류하지 않고 적용 실패로 처리한다."""
        try:
            return int(getattr(getattr(self.cfg, "script", None), "max_declines_per_case", 1))
        except (TypeError, ValueError):
            return 1

    def _declined_reason(self, ans: Any, code: str, edits: list) -> str:
        """LLM 답이 거절이면 그 차단 사유 문장을, 아니면 빈 문자열을 반환한다.
        거절은 ``<blocked>``(``blocked_reason``)가 있거나 edits도 스크립트도 없이 분석만 있는 답이다.
        전송 실패, 형식을 어긴 답, 파일을 더 요청하는 답, 쓸 수 없는 edits는 거절이 아니라 적용 실패다.
        ``script.max_declines_per_case``가 0 이하면 항상 빈 문자열을 반환한다.

        The blocker sentence of a declined answer, "" otherwise: `ScriptProposal.blocked_reason`, or an answer with
        neither edits nor a script and only `<analysis>`. Transport failures (`llm-error:`), protocol garbage, an
        answer still asking for context or with unusable edits are not declined. Off when
        `script.max_declines_per_case` ≤ 0."""
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
            return ""                                   # unparsable / unusable edits / still asking for files
        why = str(getattr(ans, "no_script_reason", "") or "").strip()
        if why and why != "(no reason given)":
            return " ".join(why.split())
        return _first_sentence(analysis, 300)

    def _decline_count(self, case_id: str) -> int:
        """이 실행에서 케이스가 거절된 횟수를 실행 이력의 시도 메모 ``declined: …``로 센다."""
        if not case_id:
            return 0
        return sum(1 for a in self.history if a.script_case_id == case_id
                   and any(n.startswith(DECLINED_NOTE) for n in a.notes))

    def _decline(self, att: FixAttempt, c: ErrorCluster, ans: Any, reason: str, *, ignored: bool = False) -> None:
        """거절된 답을 기록한다.
        시도 결과는 ``blocked``, 메모는 ``declined: <사유>``, 저널은 ``declined``이며 적용한 것은 없다.
        차단(needs_owner)은 시도가 이력에 들어간 뒤 ``_after_decline``이 한다.

        A declined answer (`<blocked>` or analysis only): outcome `blocked`, note `declined: <reason>`, journal
        `declined`; BLOCKED needs_owner follows in `_after_decline` once the attempt is recorded. Nothing applied."""
        case = att.script_case_id or c.case_id or c.id
        n = self._decline_count(case) + 1
        limit = max(1, self._declines_allowed())
        att.outcome = "blocked"
        att.notes.append(DECLINED_NOTE + _first_sentence(reason, 400))
        if ignored:
            att.notes.append("the answer named a blocker (<blocked>): its edits / script were not applied")
        self._declines[id(att)] = reason
        try:
            if self.ws.is_dirty():
                self.ws.discard_uncommitted()
        except Exception:  # pragma: no cover - nothing was applied; stay defensive
            pass
        log.info("LLM 이 거절함 (declined %d/%d): %s — 프로젝트 밖의 일, 다시 묻지 않음", n, limit,
                 _first_sentence(reason, 240))
        self.ws.journal({"event": "declined", "case": case, "attempt": att.id, "reason": reason[:500],
                         "explicit": bool(str(getattr(ans, "blocked_reason", "") or "").strip()),
                         "declines": n, "limit": limit, "mode": att.script_mode, "ignored": ignored})

    def _after_decline(self, st: "_Round", c: ErrorCluster, att: FixAttempt) -> None:
        """거절 횟수가 ``script.max_declines_per_case``에 도달하면 케이스를
        BLOCKED needs_owner로 차단하고 LLM을 더 호출하지 않는다.

        After a declined attempt is recorded: BLOCKED needs_owner once the case reached
        `script.max_declines_per_case` declined answers (no further LLM call)."""
        reason = self._declines.pop(id(att), None)
        if reason is None:
            return
        case = att.script_case_id or c.case_id or c.id
        if self._decline_count(case) < max(1, self._declines_allowed()):
            return
        self._block_needs_owner(st, c, reason)

    def _block_needs_owner(self, st: "_Round", c: ErrorCluster, reason: str, same_as: str = "") -> None:
        """거절된 케이스나 원인이 같은 다른 케이스를 LLM 호출 없이 차단한다.
        - 트리아지 분류기가 구체적인 사유(``DECLINE_KEEPS_REASONS``: javax 라이브러리, 호환되지 않는 외부 API, 없는
          아티팩트, 플러그인)를 주면 운영자와 attn 코드가 볼 수 있도록 그 사유와 조치 방법 문구를 쓴다
        - 분류되지 않은 오류만 needs_owner로 차단하고, 조치 방법에는 차단
          사유 문장에 소유팀의 결정이 필요하다는 말을 붙인다
        - 처음 거절한 케이스는 원인이 같은 케이스를 알아보기 위해 (파일, 아티팩트)를 기억해 둔다

        Block a declined case (or a same-cause sibling) without another LLM call. When the triage classifier has a
        specific reason for it (`DECLINE_KEEPS_REASONS`: external_javax_api, external_api_incompatible,
        artifact_not_found, plugin_incompatible) that reason and its composed suggestion are kept (PM, 2026-10-31:
        operators, the handbook and the attn codes key on them; the LLM's sentence goes into the rationale); only an
        unclassified cluster — or the errors the classification leaves out — is BLOCKED `needs_owner` with the
        suggestion "<blocker sentence> — 소유팀 결정 필요" ("same cause as <case>: …" for a sibling)."""
        from sbmig.triage.blocked import BlockedCandidate, classify_blocked
        sentence = _first_sentence(reason, 300)
        text = f"same cause as {same_as}: {sentence}" if same_as else sentence
        rest_c: Optional[ErrorCluster] = c
        try:
            pol = classify_blocked(c, self.profile, self.cfg, ws=self.ws)
        except Exception as e:  # noqa: BLE001 - a classifier bug: the plain needs_owner block below
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
        """``c``가 소유팀의 결정으로 차단된 케이스와 원인이 같으면 (그 케이스, 사유 문장)을, 아니면 None을 반환한다.
        ``c``의 오류 파일이 모두 그 케이스의 오류 파일에 포함되거나 같은
        아티팩트(``e.artifact``)를 가리키면 원인이 같다고 본다.

        (case, sentence) of the needs_owner case `c` shares its cause with: every error file of `c` is among that
        case's error files, or an error names the same artifact. None otherwise."""
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
        """원인이 같은 케이스를 LLM 호출 없이 BLOCKED needs_owner로 차단한다.
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
        """LLM 답에서 먼저 고른 쪽이 적용되지 않았을 때 그 실패를 메모로 남기고, 같은 시도에서 나머지 쪽으로 계속한다.

        The answer's first choice did not apply: its failure becomes a note, the try goes on with the other half."""
        att.notes += [f"first choice failed: {e}" for e in att.apply_errors]
        att.apply_errors = []
        att.outcome = "pending"
        att.notes.append(note)
        log.info("%s", note)

    def _log_answer(self, secs: float, att: FixAttempt, code: str, edits: list) -> None:
        """LLM 답을 요약한 로그 한 줄(걸린 시간, 원인과 해결 방법의 첫 문장,
        edits 개수, 스크립트 유무와 없는 이유)을 남긴다.

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
        """시도의 출처(kb-script / llm-new / llm-augment / llm-edit)가 정해진 뒤 오류 목록에 이 시도의
        FixTry를 연다. 오류 목록이 번호를 주지 않으면 케이스별로 직접 번호를 매긴다.

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
        """FixAttempt의 내용을 FixTry에 반영한다. 적용, 결과 평가, 되돌리기 뒤마다 모든 필드를 맞춘다.
        diff는 ``errors.diff_chars``까지만 남기고(전체는 changes.patch), 오류 목록이 이전 형식이면 수정
        기록, 메이저 변경, 운영자 확인 가이드는 넣지 않는다.

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
        """수정(스크립트 또는 edits)이 실제로 파일에 실행됐는지 판단한다.
        정적 검사에서 거부된 것은 실행되지 않은 것으로 본다."""
        res = att.script_result
        if res is not None and res.status != "rejected":
            return True
        er = self._edit_results.get(id(att))
        return er is not None and er.status != "rejected"

    def _script_base(self, c: ErrorCluster, step: Any) -> Optional[ScriptBase]:
        """보강 프롬프트에 넣을 기준(ScriptBase)을 만든다. 이어서 고칠 버전, 이 프로젝트에서 그 버전이 한
        일(바꾼 파일, 오류, 로그 마지막 50줄), 남은 케이스 오류, 새로 생긴 오류, 버전 이력을 담는다.
        만들 수 없으면 None을 반환하고 신규 요청으로 넘어간다. 예외를 던지지 않는다.

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
                                  history=self._script_history(step.case_id, c),
                                  session_id=getattr(a, "llm_session_id", None),   # v2.1z-p: the case thread
                                  session_model=getattr(a, "llm_session_model", "") or "")   # v2.1z-x: its model
            return ScriptBase(case_id=step.case_id, version=int(getattr(step.meta, "version", 0) or 0),
                              code=step.code, remaining_errors=[fmt_error(e) for e in c.errors][:30],
                              history=self._script_history(step.case_id, c))
        except Exception as e:  # noqa: BLE001 - a broken base means a new script, never a crash
            log.warning("script base for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return None

    def _script_history(self, case_id: str, c: ErrorCluster) -> list[str]:
        """보강 프롬프트에 넣을 이력을 만든다.
        저장된 버전마다 한 줄(상태, 성공 / 실패 수, 마지막 결과)을 쓰고 이번 실행의 시도를 덧붙인다.

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
        """케이스의 직전 시도가 개선이나 빌드 성공이 아니면 되돌린다.
        이미 되돌렸거나 적용되지 않은 시도는 건너뛴다.
        그 시도가 HEAD면 reset, 아니면 ``git revert``로 되돌리고 (시도, 그 시도가 없앴다가 다시 나타난 오류,
        다음 시도용 메모, 되돌렸는지 여부)를 반환한다.
        revert가 충돌로 실패하면 메모만 남기고, 다음 버전은 그 상태에서 실행된다.

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
        except Exception as e:  # noqa: BLE001 - a git problem: keep going on top of it
            a.notes.append(f"rollback before the next attempt failed: {type(e).__name__}: {e}")
            return a, [], (f"the previous attempt {a.id} could not be rolled back ({type(e).__name__}): this "
                           "version ran on top of it"), False
        # 그 시도가 없앴던 오류는 시도의 대상 오류 중 이번 빌드에 없는 것이며, 다음 시도 결과 평가의 기준선에 더한다
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
        """다음 시도가 아무것도 바꾸지 못했을 때 되돌렸던 시도를 되살려 트리를 마지막 빌드 때 상태로 둔다.

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
        """FixContext를 만들어 LLM에게 답(원인, 해결 방법, edits, 스크립트 또는 스크립트가 없는 이유)을
        받는다(신규 또는 보강). 힌트로 KB의 마지막 해결, 같은 실행의 다른 파일, 권고, 병합 충돌 참고 파일,
        의존성 버전 정보를 넣는다.
        답을 받지 못하면(전송 실패, 설정 오류, propose_script가 없는 fixer) None을 반환하고 이유를 시도에 남긴다.
        정체가 쌓였거나 케이스의 세 번째 LLM 요청부터는 상위 모델(``llm.escalation_model``)을 요청한다.

        new / augment: the LLM's answer (FixAnswer: 원인 · 해결 · edits · script / no_script_reason), or None when
        there is none (transport failure, configuration error, a fixer without propose_script — the reason is on the
        attempt)."""
        # 앞 시도가 만든 오류라면 새 스크립트보다 그 스크립트를 보강하는 편이 낫기 때문에 그 스크립트를 먼저 알린다
        hints = [*self._match_hints.get(c.id, []), *self._introduced_hints(c), *self._sibling_hints(c)]
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
        elif mode == "augment" and self._decline_count(step.case_id):
            # 거절한 케이스에는 보강을 요청하지 않는다.
            # script.max_declines_per_case가 1보다 클 때만 여기에 온다
            mode, base = "new", None
            att.notes.append("the case was declined before → asking for a new fix, not an augment")
        att.script_mode = mode
        if mode == "augment":
            att.script_parent = int(base.version) or None
        # 정체일 때와 케이스의 세 번째 LLM 요청부터는 상위 모델을 쓴다.
        # 보강을 다 쓴 뒤의 신규 요청도 포함한다
        # the stronger model on a stall, and from the case's THIRD LLM ask on, whatever the rung (the fresh `new` asks after
        # the augments are used up included) — the same rung as "from the second augment" before v2.1z
        escalate = st.stall_level >= self.cfg.loop.stall_limit or int(getattr(step, "llm_used", 0) or 0) >= 2
        sig = next((e.signature_generic for e in c.errors if e.signature_generic), "") or ""
        self._mark(f"library hints for {c.id}")
        lib_hints = self._library_hints(c)
        previous = self._previous(c, ladder=False)
        ctx = FixContext(phase=st.phase, iteration=st.iteration, stall_level=st.stall_level,
                         profile_summary=self._summary(), plan_notes=self.plan_notes, kb_hints=hints,
                         advisory_hints=adv_hints, previous_attempts=previous, other_errors_summary=other,
                         use_escalation_model=escalate, reference_notes=ref_notes, reference_files=ref_files,
                         script_mode=mode, case_id=step.case_id, signature_generic=sig,
                         script_base=base if mode == "augment" else None, library_hints=lib_hints,
                         layout_facts=self._layout_facts())
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
        self.budget.charge_llm(max(1, proposal.llm_usage.calls))
        self._mark(f"LLM answered for {c.id}")
        att.model, att.prompt_hash, att.llm_usage = proposal.model, proposal.prompt_hash, proposal.llm_usage
        att.llm_session_id = getattr(proposal, "session_id", None)      # v2.1z-o: the gateway session (continuation)
        att.llm_session_model = getattr(proposal, "session_model", "") or ""   # v2.1z-x: the model that session runs on
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
        """프롬프트의 "Project after the restructure" 절을 만든다.
        오케스트레이터가 넘긴 재구성 결과와 설정을 쓴다(``sbmig.loop.layout_facts``).
        재구성 결과 수가 바뀔 때만 다시 만들고, 재구성이 없었으면 빈 목록을 반환한다. 예외를 던지지 않는다.

        `FixContext.layout_facts` from the restructure results the orchestrator handed over (`restructure_results` /
        `restructure_modes`) and `cfg.artifacts` / `cfg.layout`; rebuilt only when the number of results changes."""
        results = list(self.restructure_results or [])
        if not results:
            return []
        if self._layout_cache is not None and self._layout_cache[0] == len(results):
            return list(self._layout_cache[1])
        try:
            from .layout_facts import layout_fact_lines
            case = getattr(getattr(self.profile, "case_type", None), "value", None) or \
                str(getattr(self.profile, "case_type", "") or "")
            lines = layout_fact_lines(results, self.cfg, modes=list(self.restructure_modes or []), case=case,
                                      read_artifact_id=self._artifact_id_of)
        except Exception as e:  # noqa: BLE001 - a fact section never breaks a fix attempt
            log.debug("layout facts unavailable: %s: %s", type(e).__name__, e)
            lines = []
        self._layout_cache = (len(results), list(lines))
        if lines:
            self.ws.journal({"event": "layout_facts", "lines": lines})
        return list(lines)

    def _artifact_id_of(self, rel: str) -> str:
        """모듈 pom의 artifactId를 반환한다. 읽을 수 없으면 빈 문자열을 반환한다.

        The artifactId of a project pom ("" when it does not read)."""
        ed = self._pom_editor(rel)
        try:
            return str(ed.artifact_id() or "") if ed is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    def _introduced_hints(self, c: ErrorCluster) -> list[str]:
        """이 케이스의 오류가 앞 라운드에 적용된 시도가 수정한 파일에서 처음 나타났으면, 그 시도를 알려 주는
        힌트 한 줄을 만든다. 해당하지 않으면 빈 목록을 반환한다.
        힌트는 ``introduced by script:<case>:v<N>``으로 시작하고, edits 시도면 그 시도 이름만 쓴다.

        The kb hint naming the earlier applied try whose touched files first showed this case's errors (`_score`
        records them): `introduced by script:<case>:v<N> (attempt <id>) — prefer augmenting that script`."""
        seen: list[FixAttempt] = []
        for e in c.errors:
            a = self._introduced.get(_pair(e))
            if a is not None and a not in seen and (a.script_case_id or a.cluster_id) != (c.case_id or c.id):
                seen.append(a)
        out: list[str] = []
        for a in seen[:2]:
            if a.script_version and a.script_case_id and not a.edits_applied:
                out.append(f"introduced by script:{a.script_case_id}:v{a.script_version} (attempt {a.id}) — prefer "
                           "augmenting that script")
            else:
                out.append(f"introduced by {self._attempt_name(a)} "
                           f"(attempt {a.id}) — its change is in the tree; fix what it left, do not undo it")
        return out

    def _store_script(self, att: FixAttempt, c: ErrorCluster, step: Any, ans: Any, code: str, *,
                      check_duplicate: bool = True) -> bool:
        """LLM이 작성한 스크립트를 KB 케이스에 저장한다.
        - 실행할 스크립트는 실패해도 이력이 남도록 실행 전에 저장한다.
          edits 답에 딸린 스크립트는 unverified로 저장한다
        - 보강한 버전에는 부모 버전(``parent``)과 만든 이유(``reason``, 부모 버전이 실패한 이유)를 남긴다.
          KB가 없으면 저장하지 않고 v0로 둔다
        False를 반환하면 같은 코드의 버전이 이 실행에서 이미 실행됐다는 뜻이므로 실행하지 않는다.

        Store an LLM script in the KB case (`kb.cases.add_script`, source = the try's): a script run stores it
        BEFORE it runs (failures keep their lineage), an edits answer stores it `unverified` next to the fix. No KB →
        unstored (v0). False = do not run it (the same version already ran in this run). v2.1e: an augment's version
        records its `parent` and the `reason` it was made (`v1: <the parent's failure in words>`)."""
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
        # 저장 로그는 저장소가 직접 남기므로(stage kb) 여기서는 DEBUG 한 줄만 남긴다
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
        """스크립트를 프로젝트에 실행한다.
        ``sbmig.script``가 정적 검사, ``python -I`` 별도 프로세스, 프로젝트 폴더 한정, 타임아웃을 적용한다.
        거부, 오류, 타임아웃이면 아무것도 적용하지 않고 작업 트리를 복원한다.
        성공해서 변경이 있으면 스크립트가 보고한 파일만 커밋한다.

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
        # sbmig의 버전 채우기를 스크립트의 두 번째 변경으로 오인하지 않도록 버전 채우기보다 먼저 실행한다
        self._idempotency_check(att, c, code, res)
        self._fill_versions(att, c, res, pre, st)      # v2.1l: version-less dependencies get theirs (same commit)
        self._mark(f"git commit for {c.id}")
        sha = self._commit(f"fix(script:{case_id}:v{v} {att.script_mode}): {c.id}", pre, paths=res.changed_files)
        if sha is None:
            att.outcome = "apply_failed"
            att.apply_errors.append("no file changed")
            log.info("적용 실패 (%s): 실행은 ok, 바뀐 파일 없음", name)
            return
        self._applied(att, c, st, pre, sha, name, list(res.changed_files))

    def _idempotency_check(self, att: FixAttempt, c: ErrorCluster, code: str, res: ScriptRunResult) -> None:
        """``ok``로 끝나며 파일을 바꾼 스크립트를 첫 실행 결과 위에서 같은 오류로 한 번 더 실행해 멱등성을
        확인한다. 설정 ``script.idempotency_check``로 켠다.
        - 두 번째 실행이 또 파일을 바꾸면 멱등이 아니다. 두 번째 변경만 되돌리고 저장된 버전을
          ``non_idempotent``로 표시해 재사용하지 않으며, 시도 메모와 저널 ``script_non_idempotent``를 남긴다
        - 시도의 결과는 재빌드로 정한다. 버전 채우기 전에 실행하므로 sbmig의 pom 수정과 섞이지 않는다.
          예외를 던지지 않는다

        Run an `ok` script that changed files once more on its own result (same errors). A second change = not
        idempotent (rule 2): only the second run's changes are undone (the first run's result is committed as
        usual), the stored version is marked `non_idempotent` (never reused), a note goes on the attempt and the
        journal gets `script_non_idempotent`; the attempt's outcome stays whatever the rebuild scores. Runs before the
        version fill so sbmig's own pom edits are never mistaken for the script's. Never raises."""
        try:
            if att.source in PRE_TRY_SOURCES or self.script_runner is None \
                    or not getattr(getattr(self.cfg, "script", None), "idempotency_check", True) \
                    or not res.changed_files or not self.ws.is_dirty():
                return
            snap: dict[str, Optional[bytes]] = {}
            for f in dict.fromkeys(_norm_path(x) for x in res.changed_files if x):
                full = self.ws.abs(f)
                snap[f] = full.read_bytes() if full.is_file() else None
        except Exception as e:  # noqa: BLE001 - the check is a guard, never a reason to fail the attempt
            log.debug("idempotency check of %s skipped: %s: %s", att.id, type(e).__name__, e)
            return
        v = int(att.script_version or 0)
        self._mark(f"idempotency check of {att.script_case_id}:v{v}")
        with self._busy_with(f"fix script {att.script_case_id}:v{v} (second run)"):
            try:
                res2 = self.script_runner.run(self.ws, code, errors=list(c.errors))
            except Exception as e:  # noqa: BLE001 - the runner says it never raises; stay defensive
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
        첫 실행이 바꾼 파일은 기억해 둔 내용으로 복원하고(없던 파일은 삭제),
        나머지는 HEAD 내용으로 복원하며 HEAD에 없으면 삭제한다.

        Put every file the second run changed back as the first run left it: the remembered bytes (None = the file did
        not exist), else HEAD's content (the attempt has not committed yet), else delete it."""
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
            except Exception as e:  # noqa: BLE001 - best effort; the commit takes only the first run's files
                log.warning("could not undo the second run's change of %s: %s: %s", f, type(e).__name__, e)

    def _apply_edits(self, att: FixAttempt, c: ErrorCluster, edits: list, st: "_Round",
                     subject: str | None = None, apply_fn: Callable[[], ScriptRunResult] | None = None) -> None:
        """LLM 답의 ``<edits>``를 ``sbmig.script.apply_edits``로 적용한다.
        스크립트와 같은 안전장치(프로젝트 폴더 한정, 같은 거부 규칙)를 쓴다.
        실패하면 아무것도 적용하지 않고 작업 트리를 복원한다. 변경이 있으면 ``fix(edits:<case> <source>)``로
        커밋하고, sbmig의 버전 채우기는 ``subject``로 ``fix(sbmig-version): …``를 쓴다.

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
        - 시도의 파일 목록과 diff(DEBUG 로그에는 전체, FixTry에는 일부)를 남기고, ``적용(…)`` 줄과 수정
          기록마다 ``수정: …`` INFO 줄을 남긴다
        - pom을 바꿨으면 변경 내용을 다시 읽어 확인하고, 바뀐 파일이 많으면 경고한다

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

    # ------------------------------------------------------------------ v2.1d dependency version policy
    def _read_back(self, att: FixAttempt, c: ErrorCluster, st: _Round, pre: str, sha: str, diff: str) -> None:
        """적용된 커밋의 pom 변경 중 메이저 업그레이드와 라이브러리 교체를 찾아 운영자 확인 가이드와 함께
        ``att.major_upgrades``에 넣는다.
        가이드는 답의 ``<verify>``를 쓰고, 없으면 LLM에 한 번 요청하고(``verify_guide``), 그것도 없으면 sbmig가 만든다.
        빌드 통과가 목표이므로 메이저 버전이 바뀌어도 되돌리거나 차단하지 않는다.
        실패는 DEBUG 한 줄로만 남긴다.

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
        """운영자 확인 가이드를 만들어 (가이드, "llm" 또는 "sbmig")를 반환한다. 저장 스크립트 재사용(``kb-script``)은
        LLM 호출이 없다는 장점을 지키기 위해 LLM을 호출하지 않고 sbmig가 가이드를 만든다.
        ``libraries.verify_guide_llm`` 설정, LLM 사용 가능 여부, 예산도 확인한다.

        (guide, "llm" | "sbmig") for the try's major upgrades. A reused stored script (`kb-script`) costs no LLM
        call: its guide is synthesized (docs/INTERFACES.md v2.1d)."""
        ans = self._answers.get(id(att))
        text = str(getattr(ans, "verify", "") or "").strip() if ans is not None else ""
        if text:
            return text, "llm"
        fx = self._verify_fixer()
        if fx is not None and att.source != "kb-script" and att.source not in PRE_TRY_SOURCES \
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
        """``verify_guide``를 가진 fixer를 반환한다. fixer 자신이거나 ``LazyFixer`` 뒤의 실제 fixer다.
        가이드 하나 때문에 LLM 클라이언트를 새로 만들지 않도록, 실제 fixer는 propose_script가 이미 만들어 둔
        경우에만 쓴다. None이면 sbmig가 가이드를 만든다.

        The fixer that has `verify_guide` (VerifyGuideLike): the fixer itself, or the real one behind the
        orchestrator's `LazyFixer` proxy once `propose_script` created it; None = sbmig synthesizes the guide."""
        fx = self.fixer
        if callable(getattr(fx, "verify_guide", None)):
            return fx
        inner = getattr(fx, "_fixer", None) if isinstance(fx, LazyFixer) else None
        return inner if inner is not None and callable(getattr(inner, "verify_guide", None)) else None

    def _charge_verify_usage(self, att: FixAttempt, fx: Any) -> None:
        """verify_guide 호출의 사용량을 예산과 시도(FixTry의 LLM 호출 수, 보고서 사용량)에 반영한다.
        일찍 실패했더라도 요청은 했으므로 최소 1회로 센다.

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
        """sbmig가 만드는 가이드의 언어(``report.language``: ko 또는 en, 기본값 ko)를 반환한다."""
        lang = str(getattr(getattr(self.cfg, "report", None), "language", "") or "ko").lower()
        return lang if lang in ("en", "ko") else "ko"

    def _same_major_offer(self, ch: Any) -> str:
        """저장소나 표가 제시했던 같은 메이저의 최신 jakarta 릴리스를 반환한다. 없으면 빈 문자열을 반환한다.
        경고 줄에 메이저 업그레이드 대신 쓸 수 있었던 버전을 함께 보여 주는 데 쓴다.

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
        """프롬프트의 "Dependency versions" 절을 만든다.
        - 맨 앞은 정보 줄이다. 케이스 오류의 아티팩트와 classpath에서 찾은 라이브러리의 as-is 버전, 새 부모
          체인 / 목표 Boot BOM의 관리 버전, 체인 속성, ``_package_facts``, 리소스 정보를 쓴다
        - 그다음 라이브러리마다 저장소 정보(같은 메이저의 jakarta / javax, 상위 메이저)와 판단을 붙인다.
          오류가 ``javax.``를 언급하거나 javax 표에 있는 라이브러리인 javax 케이스에만 붙인다
        - 새 체인이나 Boot BOM이 관리하는 라이브러리에는 "the API changed, fix the code and leave the version" 정보 줄
          하나만 붙인다. API가 제거된 경우에 버전을 올리라는 안내가 나가지 않도록 하기 위해서다
        라이브러리별 결과는 실행 중에 캐시하고, 예외를 던지지 않는다.

        FixContext.library_hints (the prompt's "Dependency versions" section): the facts first — `_version_facts` (the
        errors' artifacts and, since v2.1v, the libraries `_case_libraries` locates on the classpath), the chain's
        version properties, `_package_facts`, the resource facts — then, per library, the repository facts and the
        verdict (`RepositoryLookup.candidates` → `hint_lines`, cached per library). v2.1v: the repository lines only
        for a javax case (an error names `javax.` or the library is in the javax table); a library the new chain /
        the target Boot BOM manages gets its one fact line and nothing else. Never raises."""
        libs: list[GAV] = []
        try:
            libs = self._case_libraries(c) or self._javax_libraries(c)   # v2.1z-e: the owner-class fallback
        except Exception as e:  # noqa: BLE001 - hints never break a fix attempt
            log.debug("case libraries for %s unavailable: %s: %s", c.id, type(e).__name__, e)
        facts = self._version_facts(c, libs) + self._chain_property_facts(c, libs) + self._package_facts(c) + \
            self._resources_facts(c)                                                    # facts first
        try:
            out: list[str] = list(facts)
            javax_case = _mentions_javax(c.errors)
            listed: list[str] = []
            for gav in libs:
                if self._known_version(gav.key, self._lib_module(c)):
                    continue                            # its fact line says it all: no listing, no verdict (v2.1v)
                if not (javax_case or table_entry(gav.key, self.cfg) is not None):
                    continue                            # v2.1d's scope: javax libraries only
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
        except Exception as e:  # noqa: BLE001 - hints never break a fix attempt
            log.debug("library hints for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return list(facts)

    def _lib_module(self, c: ErrorCluster) -> Optional[str]:
        """classpath에서 찾은 라이브러리를 조회할 모듈을 반환한다.
        케이스의 첫 컴파일 오류의 모듈이고, 없으면 클러스터의 모듈이다."""
        return next((e.module for e in c.errors if e.module and e.kind in (ErrorKind.COMPILE, ErrorKind.TEST_COMPILE)),
                    None) or c.module

    def _known_version(self, key: str, module: Optional[str]) -> Optional[tuple[str, str]]:
        """새 부모 체인이나 목표 Boot BOM이 이 g:a를 관리하면 ("new parent
        chain" 또는 "target Boot BOM", 버전)을 반환한다.

        (who, version) when the new parent chain or the target Boot BOM manages `key`; None otherwise."""
        try:
            r = self._versions()
            mv = r.managed_version(key, module) if r.has_chain else None
            if mv:
                return "new parent chain", mv
            bv = r.boot_version(key, module)
            if bv:
                return "target Boot BOM", bv
        except Exception as e:  # noqa: BLE001
            log.debug("managed version of %s unavailable: %s: %s", key, type(e).__name__, e)
        return None

    def _case_libraries(self, c: ErrorCluster) -> list[GAV]:
        """케이스와 관련된 외부 라이브러리를 g:a당 하나씩, 최대 ``libraries.max_artifacts_per_case``개까지 찾는다.
        오류의 ``artifact``를 쓰고, 없으면 앞쪽 컴파일 오류의 소유 클래스나 오류 줄에서 호출한 클래스를
        classpath에서 찾는다. 사내 아티팩트와 프로젝트 클래스는 뺀다.

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
        """이 실행의 저장소 조회기(Nexus, 미러의 maven-metadata.xml)를 반환한다.
        ``libraries.lookup``이 꺼졌거나 만들 수 없으면 None을 반환하고, 그 뒤로는 실행 내내 조회하지
        않는다(WARNING 한 줄, 표만 사용).

        The run's `RepositoryLookup` (lazy; None when `libraries.lookup` is off or it cannot be built — then off
        for the rest of the run with one WARNING)."""
        if self._repo_lookup is not None or self._repo_lookup_off:
            return self._repo_lookup
        if not getattr(getattr(self.cfg, "libraries", None), "lookup", True):
            self._repo_lookup_off = True
            return None
        try:
            from sbmig.build.repo_versions import RepositoryLookup
            try:
                self._repo_lookup = RepositoryLookup(self.cfg, project_root=self.ws.root)   # v2.1z-e: the poms' repositories too
            except TypeError:                           # a lookup double without the keyword (tests, plugins)
                self._repo_lookup = RepositoryLookup(self.cfg)
        except Exception as e:  # noqa: BLE001 - no lookups: the prompt carries the table + the policy only
            self._repo_lookup_off = True
            log.warning("의존성 버전 조회 끔: 저장소 조회를 시작할 수 없음 (%s: %s) — 이 실행은 표만 사용",
                        type(e).__name__, str(e)[:200])
        return self._repo_lookup

    def _library_lines(self, key: str, current: str | None, javax: bool = False) -> list[str]:
        """라이브러리 하나에 대한 프롬프트 줄(표 항목, 저장소 정보, 탐색 줄)을 만든다.
        ``javax``이면 javax 케이스이므로 비슷한 이름의 좌표 탐색 결과도 넣는다.
        찾은 내용은 ``의존성 버전 조회: …`` INFO 줄로 남긴다.

        The prompt lines of one library (table entry + repository facts + the v2.1z-e discovery lines; `javax` = a
        javax case, so the sibling search's outcome is said too); logs what was found."""
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

    # ------------------------------------------------------------------ v2.1z-e jakarta build discovery
    def _discover(self, key: str, current: Optional[str], rv: Any = None) -> Any:
        """라이브러리 ``key``의 jakarta 빌드를 탐색해 결과를 반환한다(``sbmig.build.jakarta_discovery``,
        조회기에 캐시). 조회기가 없으면 None을 반환한다.
        ``rv``는 이미 받아 둔 RepositoryVersions이며, 없으면 ``_repo_facts``의 것을 쓴다.
        후보(``candidates``)는 라이브러리당 한 번만 조회한다.

        The `JakartaDiscovery` of `key` (cached on the lookup); None without a repository lookup."""
        lookup = self._repository()
        if lookup is None:
            return None
        try:
            from sbmig.build.jakarta_discovery import discover_jakarta_builds
            self._mark(f"jakarta discovery {key}")
            g, _, a = key.partition(":")
            return discover_jakarta_builds(lookup, g, a, current, table_entry=table_entry(key, self.cfg), cfg=self.cfg,
                                           rv=rv if rv is not None else self._repo_facts.get(key))
        except Exception as e:  # noqa: BLE001 - discovery is an aid
            log.debug("jakarta discovery %s failed: %s: %s", key, type(e).__name__, e)
            return None

    def _discovery_lines(self, key: str, current: Optional[str], entry: Any, rv: Any, javax: bool) -> list[str]:
        """프롬프트에 붙일 탐색 줄을 만들고 저널 ``library_lookup``을 남긴다.
        ``hint_lines``가 이미 보여 준 내용은 반복하지 않고, sbmig의 확인(버전 한정어, jar 스캔)으로 새로
        알게 된 것, 다른 좌표의 빌드, javax 케이스에서 비슷한 이름까지 찾았지만 없었다는 한 줄만 쓴다.

        The discovery lines that ADD to `hint_lines`: same-coordinate builds only when sbmig's verification (qualifier /
        jar scan) said more than the pom check, sibling builds, and — for a javax case whose sibling search ran — the
        one "none found (searched: …)" line. Nothing when the repository could not be read."""
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
        """journal.jsonl만 보고도 그 라이브러리를 저장소에서 찾았는지, 결과가 무엇인지 알 수 있도록 저널
        ``library_lookup``을 남긴다."""
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
        """케이스가 ``external_javax_api``로 분류되면 그 BlockedCandidate(소유 클래스 포함)를, 아니면 None을 반환한다."""
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
        직접 선언과 dependencyManagement 항목을 포함하고 프로젝트 자신의 모듈은 뺀다."""
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
        """classpath로 소유 클래스의 라이브러리를 찾지 못할 때, 선언된 의존성에서 대신 찾는다.
        소유 클래스의 패키지 토큰(``com.vendor8.grid.GridExporter``면 vendor8, vendor, grid)을 의존성의 groupId,
        artifactId 토큰과 비교해 가장 많이 겹치는 것을 고르고, 동점이면 groupId가 패키지 접두어인 것을 고른다.
        그래도 없으면 로컬 저장소에 있는 그 의존성 jar 안에 해당 클래스 파일이 있는지 확인한다.

        The fallback when the classpath cannot attribute the owner class: the class package's tokens against the
        declared dependencies' groupId / artifactId tokens (stems without trailing digits count too), best overlap
        wins (a groupId that prefixes the package breaks ties); then the local repository's jars of the declared
        dependencies for the class file. None when nothing matches."""
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
        # the local repository's jars (bounded): the class file inside one of the declared dependencies' jars
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
                except Exception:  # noqa: BLE001 - a broken jar: not this one
                    continue
        return None

    def _javax_libraries(self, c: ErrorCluster) -> list[GAV]:
        """javax 케이스와 관련된 라이브러리를 classpath 없이 찾는다(``_owner_dependency``).
        결과는 클러스터별로 캐시한다.
        찾지 못하면 소유 클래스와 함께 저널 ``library_unmapped``를 남긴다.
        프롬프트에 저장소 줄이 없는 이유를 journal.jsonl만으로 알 수 있게 하기 위해서다.

        The libraries behind a javax case when the classpath attribution found none: the owner class → declared
        dependency fallback (`_owner_dependency`); journal `library_unmapped` when even that fails."""
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
                owner = self._owner_from_source(c)      # `incompatible types` names no owner: the error line's import
            self._javax_owner[c.id] = owner
            deps = self._declared_dependencies()
            g = self._owner_dependency(owner, deps)
            # 첫 소유 클래스는 어느 라이브러리에나 있는 타입일 수 있으므로, 맞지
            # 않으면 다음 순위의 소유 클래스로 다시 찾아본다
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
        """메시지가 소유 클래스를 알려 주지 않는 오류(``incompatible types``)에서, 오류 줄의 식별자 중 그
        파일의 import가 가리키는 외부 클래스의 FQCN을 반환한다.
        JDK, javax, jakarta, 프로젝트 클래스는 제외하고, 찾지 못하면 None을 반환한다. 파일은 읽기만 한다.

        The library class on the error line when the message names none: an identifier of that line whose import in the
        file is neither JDK / javax / jakarta nor a project class; None otherwise."""
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

            # the error line's own identifiers first (`new GridExporter().export(…)`), then — the call goes through a
            # field or a local (`exporter.export(req, resp)`) — the file's first external import
            for ident in re.findall(r"\b([A-Z]\w*)\b", window):
                fq = by_simple.get(ident)
                if fq and external(fq):
                    return fq
            for fq in imports:
                if external(fq):
                    return fq
        return None

    def _jakarta_evidence(self, c: ErrorCluster, owner: Optional[str], artifact: Optional[GAV]) -> str:
        """차단 항목에 붙일 한 문장을 만든다. 탐색 기록(``JAKARTA_SEARCH_PREFIX`` …)이나, 의존성을 찾지 못한
        소유 클래스(``UNMAPPED_PREFIX`` …)를 쓴다."""
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
        """거절 사유가 jakarta / javax 빌드에 관한 것인데 sbmig가 확인된 jakarta 빌드를 찾았으면 그 정보를,
        아니면 빈 문자열을 반환한다.

        The fact that contradicts a "no jakarta build" decline — a verified build discovery found for a library of the
        case — or ""."""
        noted = self._note_contradiction(c)                  # v2.1z-aa: a replacement note knows the fix (project code)
        if noted:
            return noted
        bundled = self._bundled_contradiction(c, reason)     # v2.1z-i: the "missing" library is bundled
        if bundled:
            return bundled
        if not self._jakarta_enabled() or not re.search(r"(?i)jakarta|javax", reason or ""):
            return ""
        try:
            libs = self._case_libraries(c) or self._javax_libraries(c)
            for lib in libs:
                d = self._discover(lib.key, lib.version)
                b = d.best() if d is not None else None
                if b is not None:
                    return f"the repository has {b.coordinates} ({b.how}; found by {b.source}) — a jakarta build of {lib.key}"
        except Exception as e:  # noqa: BLE001
            log.debug("decline contradiction for %s failed: %s: %s", c.id, type(e).__name__, e)
        return ""

    def _knowledge_notes(self) -> list[Any]:
        """``knowledge/notes``의 노트 목록을 반환한다(``sbmig.fix.context.load_notes_index``).
        한 번만 읽고, 읽지 못하면 빈 목록을 반환한다.
        트리아지에도 같은 목록을 넘긴다(``attach_run(notes=…)``). 테스트는 ``_notes_index``에 직접 넣는다."""
        if self._notes_index is None:
            try:
                from sbmig.fix.context import load_notes_index
                self._notes_index = load_notes_index(self.cfg.resolve_path("knowledge/notes"))
            except Exception as e:  # noqa: BLE001 - notes are hints; a missing folder must not stop the loop
                log.debug("notes index unavailable: %s: %s", type(e).__name__, e)
                self._notes_index = []
        return self._notes_index

    def _note_contradiction(self, c: ErrorCluster) -> str:
        """거절된 케이스의 오류 텍스트나 오류 파일의 import 줄에 ``replacement: true`` 노트가 맞으면 그
        정보(노트 제목, 파일)를, 아니면 빈 문자열을 반환한다.
        노트가 대체 방법을 알고 있는 라이브러리라면 거절을 받지 않기 위해 쓴다.

        The fact that contradicts a decline on a library a `replacement: true` note knows how to replace — or ""."""
        try:
            from sbmig.fix.context import replacement_notes
            from sbmig.triage.blocked import note_text
            case = getattr(getattr(self.profile, "case_type", None), "value", None)
            hits = replacement_notes(self._knowledge_notes(), note_text(self.ws, c.errors), case=case, phase=None)
        except Exception as e:  # noqa: BLE001 - a note problem must never stop the loop
            log.debug("note contradiction for %s failed: %s: %s", c.id, type(e).__name__, e)
            return ""
        if not hits:
            return ""
        n = hits[0]
        return (f"the migration note '{n.title}' ({n.file}) gives the replacement as project code — the fix is inside the "
                f"project, not a blocker")

    def _bundled_contradiction(self, c: ErrorCluster, reason: str) -> str:
        """좌표, 라이브러리, jar가 없다는 거절인데 케이스에 없는 패키지를 프로젝트에 포함된 jar(번들 jar)가
        제공하면 그 정보를, 아니면 빈 문자열을 반환한다.

        The fact that contradicts a "no coordinates / library missing" decline — a bundled jar provides one of the
        case's missing packages — or ""."""
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
        javax 케이스의 라이브러리마다 탐색 -> 확인된 가장 좋은 빌드 선택 -> 선언한 pom 찾기 순서로 (pom,
        이전 g:a, 새 빌드, 쓸 버전 또는 None)을 만든다.
        새 좌표를 새 부모 체인이나 Boot BOM이 관리하면 버전 없이 쓰고, 보호
        좌표(사내 parent, 필수, 선택)는 바꾸지 않는다.

        The `sbmig-jakarta` plan: per javax library of the case, the best verified build, the poms declaring the old
        coordinates, the version to write (None when the new chain / Boot BOM manages the new key)."""
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
        """jakarta 빌드 탐색 설정(``libraries.discover_jakarta_builds``)이 켜져 있는지 여부.
        꺼져 있으면 사전 시도, 거절 반박, 탐색 없이 릴리스 목록만 보여 준다."""
        return bool(getattr(getattr(self.cfg, "libraries", None), "discover_jakarta_builds", True))

    def _jakarta_targets(self, c: ErrorCluster) -> list[str] | None:
        """javax 라이브러리 케이스이고 적용할 jakarta 빌드가 있으면 수정할 pom 목록을, 아니면 None을 반환한다."""
        if not self._jakarta_enabled() or not _mentions_javax(c.errors):
            return None
        plan = self._jakarta_plan(c)
        return list(dict.fromkeys(i["pom"] for i in plan["items"])) or None

    def _jakarta_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """LLM 호출 없이, 확인된 jakarta 빌드로 의존성 선언을 바꾸는 사전 시도를 실행한다.
        좌표가 다르면 ``replace_dependency``를 쓰고, 같으면 버전(관리되면 버전 삭제)과 classifier를 바꾼다. javax에서
        jakarta로 가는 상위 메이저 변경은 유일하게 허용된 메이저 변경이므로 적용하고, 운영자 확인 줄을 남긴다.
        커밋은 ``fix(sbmig-jakarta): <cluster id>``이고, (pom, 이전 좌표, 새 좌표)마다 실행당 한 번만 시도한다.

        The deterministic swap of a javax-era dependency for the verified jakarta build (`replace_dependency` for other
        coordinates, version / classifier for the same): commit `fix(sbmig-jakarta): <cluster id>`, scored and rolled
        back like any try; a higher major is the one major change v2.1d allows (javax → jakarta) and the read-back
        lists it for the operator with sbmig's synthesized guide."""
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
        self._jakarta_plans.pop(c.id, None)             # the next round plans again (the applied keys are remembered)
        return applied

    # ------------------------------------------------------------------ v2.1z-i bundled jars
    def _bundled_enabled(self) -> bool:
        """``intake.bundled_jars``가 켜져 있고 분석 단계가 프로젝트에 포함된 jar(번들 jar)를 찾았는지 확인한다."""
        if not bool(getattr(getattr(self.cfg, "intake", None), "bundled_jars", True)):
            return False
        return bool(getattr(self.profile, "bundled_jars", None) or [])

    def _bundled_managed_keys(self) -> set[str]:
        """목표 부모 체인이나 Boot BOM이 관리하는 g:a 집합(``_managed_keys``)을 반환한다.
        읽을 수 없으면 빈 집합을 반환하며, 이때는 어떤 jar도 대체된 것으로 보지 않는다."""
        try:
            return set(self._managed_keys())
        except Exception as e:  # noqa: BLE001 - no captures / chain facts: nothing is known to be managed
            log.debug("managed keys unavailable for the bundled-jar check: %s: %s", type(e).__name__, e)
            return set()

    def _bundled_superseded(self) -> list[JarCoordinate]:
        """좌표를 알고 그 g:a를 목표 부모 체인이나 Boot BOM이 관리하는 번들 jar(WEB-INF/lib의 오래된
        spring-webmvc 4.x 등)를 반환한다. 이런 jar는 관리 버전으로 대체된다.
        이전 버전을 고정하거나 system scope로 연결하면 두 Spring 버전이 섞이므로 절대 연결하지 않는다.
        LLM에는 이전 jar를 다시 넣지 말고 코드를 고치라는 정보만 전달한다.

        Bundled jars whose known g:a the target parent chain / Boot BOM manages (an old `spring-webmvc-4.x.jar` in
        WEB-INF/lib): superseded by the managed version — never wired, never pinned; the LLM only gets the fact."""
        managed = self._bundled_managed_keys()
        if not managed:
            return []
        return [j for j in getattr(self.profile, "bundled_jars", None) or []
                if j.group_id and j.artifact_id and f"{j.group_id}:{j.artifact_id}" in managed]

    def _bundled_wireable(self) -> list[JarCoordinate]:
        """연결할 수 있는 번들 jar를 반환한다. 컨테이너 API jar(``scope == "provided"``: servlet / jsp / el,
        Tomcat)와 목표 체인이 관리하는 좌표의 jar(``_bundled_superseded``)는 뺀다.

        The bundled jars that may be wired: a container API copy (`scope == "provided"`) never is, nor a jar whose
        coordinates the target parent chain manages (`_bundled_superseded`)."""
        skip = {j.path for j in self._bundled_superseded()}
        return [j for j in getattr(self.profile, "bundled_jars", None) or []
                if (j.scope or "compile") != "provided" and j.path not in skip]

    def _bundled_missing_packages(self, c: ErrorCluster) -> dict[str, list[str]]:
        """케이스에서 찾지 못한 패키지와 그 오류 파일을 반환한다.
        ``package <p> does not exist``의 ``<p>``와, ``cannot find symbol … symbol: class X`` 오류면 오류
        파일의 ``import <p>.X;``에서 ``<p>``를 가져온다(파일은 ``BUNDLED_READ_CHARS``자까지 읽는다).
        ``javax.*`` / ``jakarta.*``는 jakarta 사전 시도가 다루므로 뺀다. 예외를 던지지 않는다.

        The packages the case lacks → the error files: `package <p> does not exist`, and for `cannot find symbol …
        class X` the `<p>` of `import <p>.X;` in the error's file (read up to `BUNDLED_READ_CHARS`). `javax.*` /
        `jakarta.*` are left to the jakarta pre-try. Never raises."""
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
                    except Exception:  # noqa: BLE001 - an unreadable file: no import to read
                        texts[f] = ""
                im = re.search(r"(?m)^\s*import\s+([A-Za-z_][\w.]*)\." + re.escape(s.group(1)) + r"\s*;", texts[f])
                if im:
                    add(im.group(1), f)
        except Exception as e:  # noqa: BLE001 - facts never break the loop
            log.debug("bundled packages for %s: %s: %s", c.id, type(e).__name__, e)
        return out

    def _bundled_providers(self, c: ErrorCluster) -> list[tuple[str, JarCoordinate, list[str]]]:
        """(없는 패키지, 그 패키지를 제공하는 번들 jar, 오류 파일) 목록을 반환한다.
        패키지마다 ``providers``의 첫 jar를 쓰고, 정확히 일치하는 jar를 우선한다.

        (missing package, the bundled jar providing it, error files): the first `providers` hit per package."""
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
        오류 파일의 모듈은 재구성 뒤의 현재 트리에 있는 pom으로 정한다."""
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
                if self._bundled_declared(pom, jar):             # v2.1z-j: the restructure (or a person) already wired it
                    continue
                if not any(p == pom and j.path == jar.path for p, j, _ in plan):
                    plan.append((pom, jar, pkg))
        return plan

    def _with_eol_advisory(self, suggestion: str, artifact: Any) -> str:
        """차단된 아티팩트가 권고표(``knowledge/libraries/advisories.yml``)의 EOL / 미관리 항목이면 조치
        방법 뒤에 EOL 제목과 대체 라이브러리를 붙인다.
        분석 결과(findings)를 먼저 보고, 없으면 카탈로그의 ``artifacts`` glob으로 찾는다.
        jakarta 빌드가 없는 이유와 다음에 할 일을 소유팀이 한 줄에서 읽을 수 있게 하기 위해서다.
        권고가 없으면 문구를 그대로 반환하며 예외를 던지지 않는다.

        Append the EOL advisory (title + replacement) of the blocked artifact to the suggestion when the advisory
        table knows it (the profile's findings first, else the catalog's `artifacts` globs); unchanged otherwise."""
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
                    except Exception:  # noqa: BLE001 - no table, no advisory text
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
        except Exception as e:  # noqa: BLE001 - a decoration never breaks the block
            log.debug("eol advisory lookup for %s: %s: %s", artifact, type(e).__name__, e)
            return suggestion

    def _bundled_declared(self, pom: str, jar: JarCoordinate) -> bool:
        """그 pom이 jar의 g:a를 이미 선언했는지 판단한다(재구성이 연결했거나 사람이 넣은 경우).
        좌표를 모르면 False를 반환한다.

        Does `pom` already declare the jar's known g:a (the restructure wired it, or a person did)? Unknown coordinates → False."""
        if not (jar.group_id and jar.artifact_id):
            return False
        ed = self._pom_editor(pom)
        try:
            return bool(ed is not None and ed.has_dependency(jar.group_id, jar.artifact_id))
        except Exception:  # noqa: BLE001 - an unreadable pom declares nothing we can see
            return False

    def _bundled_targets(self, c: ErrorCluster) -> list[str] | None:
        """케이스에 없는 패키지를 번들 jar가 제공하면 그 jar를 연결할 모듈 pom 목록(오류 파일의
        ``owning_pom``, 없으면 루트 pom)을 반환한다.
        설정이 꺼졌거나, 번들 jar가 없거나, 제공하는 jar가 없거나, 모두 시도했으면 None을 반환한다.

        The module poms (`owning_pom` of the error files, else the root pom) to wire a bundled jar into, when a bundled
        jar provides a package the case lacks; None otherwise (config off, no bundled jars, no provider, all tried)."""
        if self.dry_run or not self._bundled_enabled():
            return None
        plan = self._bundled_plan(c)
        return list(dict.fromkeys(p for p, _j, _k in plan)) or None

    def _bundled_in_repository(self, jar: JarCoordinate) -> bool:
        """jar 자신의 좌표(pom.properties, sha1 조회, 이름표 중 버전이 있는 것)가 저장소에 실제로 있는지
        판단한다. 조회기가 없거나 읽을 수 없으면 False를 반환하며, 이때는 system scope로 연결한다.

        True only when the jar's own coordinates (with a version) are found in the repository set (the v2.1z-e
        `RepositoryLookup`: the pom of exact `g:a:v`, else the version in the metadata); no / unreadable lookup → False."""
        g, a, v = jar.group_id, jar.artifact_id, jar.version
        if jar.source not in BUNDLED_TRUSTED_SOURCES or not (g and a and v):
            return False
        lookup = self._repository()
        if lookup is None:
            return False
        try:
            self._mark(f"repository lookup {g}:{a}:{v}")
            if lookup.pom_text(g, a, v):
                return True
            return v in (lookup.versions(g, a) or [])
        except Exception as e:  # noqa: BLE001 - an unreadable repository counts as "not there"
            log.debug("repository check %s:%s:%s: %s: %s", g, a, v, type(e).__name__, e)
            return False

    def _bundled_boot_poms(self, module_pom: str = "") -> list[str]:
        """현재 트리에서 spring-boot-maven-plugin을 가진 pom과 분석 단계의
        boot 모듈(``<dir>/pom.xml``이 있을 때)을 반환한다.
        ``module_pom``이 주어지면 그 모듈에 의존하거나 그 모듈 자체인 boot pom만 남긴다.
        MSA처럼 boot 모듈이 여럿일 때 관계없는 서비스에 system 의존성을 넣지 않기 위해서다.
        맞는 것이 없으면 전부 반환한다.

        The current poms carrying the Boot plugin plus the profile's boot modules; with `module_pom`, only the
        boot poms that depend on that module (or are it) — several boot modules (MSA) must not all carry the
        system dependency; none matching → all of them (safe default)."""
        from sbmig.restructure.restructurer import BOOT_PLUGIN
        out: list[str] = []
        for p in self.ws.pom_files():
            ed = self._pom_editor(p)
            try:
                if ed is not None and ed.has_plugin(*BOOT_PLUGIN, where="build"):
                    out.append(_norm_path(p))
            except Exception as e:  # noqa: BLE001 - an unreadable pom is no boot pom
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
        except Exception:  # noqa: BLE001 - an unreadable module pom: keep every boot pom
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
            except Exception as e:  # noqa: BLE001 - an unreadable boot pom is kept (safe default below)
                log.debug("boot pom dependency check %s: %s: %s", b, type(e).__name__, e)
        return kept or out

    def _bundled_attempt(self, pr: PhaseResult, st: _Round, c: ErrorCluster) -> bool:
        """사전 시도 ``sbmig-bundled``를 실행한다.
        케이스에 없는 패키지를 제공하는 번들 jar를 오류 파일의 모듈 pom에 연결한다(``wire_bundled_jar``).
        저장소에 jar 좌표가 있으면 일반 의존성으로, 없으면 ``<module>/lib/<name>``에 복사해 system scope로
        연결한다. 좌표는 추측하지 않고 인수인계 항목을 남긴다.
        ``intake.unknown_jar: skip``이면 저장소에 없는 jar는 연결하지 않는다.
        (pom, jar)마다 실행당 한 번만 시도한다.

        The zero-LLM wiring of a bundled jar that provides a package the case lacks (`wire_bundled_jar` per (module
        pom, jar)): commit `fix(sbmig-bundled): <cluster id>`, scored and rolled back like any try; journal
        `bundled_jar_wired` + one Handover line per wiring. `intake.unknown_jar: skip` → a jar the repository lacks is
        not wired (facts + Handover only). Once per (pom, jar) per run."""
        plan = self._bundled_plan(c)
        if not plan:
            return False
        policy = str(getattr(getattr(self.cfg, "intake", None), "unknown_jar", "system") or "system")
        items: list[tuple[str, JarCoordinate, str, bool]] = []
        for pom, jar, pkg in plan:
            has = self._bundled_in_repository(jar)
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
                except Exception as e:  # noqa: BLE001 - the Handover line is an aid
                    log.debug("bundled_jar_followup: %s: %s", type(e).__name__, e)
        return applied

    def _bundled_facts(self, c: ErrorCluster) -> list[str]:
        """프로젝트에 포함된 jar가 제공하는 패키지를 프롬프트에 알리는 줄을 만든다.
        (jar, 패키지)마다 "the project bundles `<jar>` which provides package `<p>` …" 한 줄을 쓰고,
        ``PACKAGE_FACTS_ARTIFACTS``개까지만 쓴다. 예외를 던지지 않는다.

        One `the project bundles <jar> which provides package <p> …` line per (jar, package), capped. Never raises."""
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
            # superseded jars: the fact says the opposite — do NOT re-add the old jar, the chain manages that g:a
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
        except Exception as e:  # noqa: BLE001 - facts never break a fix attempt
            log.debug("bundled facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
        return out

    # ------------------------------------------------------------------ v2.1l dependency versions
    def _versions(self) -> VersionResolver:
        """이 루프의 버전 결정기를 반환한다.
        처음 쓸 때 두 캡처를 읽어 한 번 만들고, 저장소 조회기는 ``_repository()``의 것을 함께 쓴다.

        The loop's `VersionResolver` (built once, lazily; the repository step reuses `_repository()`)."""
        if self._version_resolver is None:
            self._version_resolver = VersionResolver.for_workspace(self.ws, repository=self._repository,
                                                                   cfg=self.cfg)
        return self._version_resolver

    def _protected_version_keys(self) -> set[str]:
        """sbmig가 버전을 채우지 않는 g:a 집합을 반환한다.
        사내 부모, 역할 부모, 필수, 선택 아티팩트(별칭 포함), ``remove_when_present``가 해당한다.
        회사가 관리하는 좌표이므로 재구성 단계에서 건드리지 않는 목록과 같다.

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
        """현재 트리의 pom 편집기를 반환한다. 읽지 못하면 None을 반환한다."""
        try:
            from sbmig.restructure.pom_editor import PomEditor
            return PomEditor(self.ws.abs(rel))
        except Exception as e:  # noqa: BLE001 - an unparsable pom: nothing to fill there
            log.debug("pom %s not readable: %s: %s", rel, type(e).__name__, e)
            return None

    def _own_module_keys(self) -> set[str]:
        """프로젝트 자신의 모듈 좌표(g:a, ``${project.groupId}:a``)를 반환한다.
        형제 모듈 의존성의 버전은 채우지 않기 위해 쓴다."""
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
        """pom에서 시작해 프로젝트 안의 부모 pom을 차례로 찾아 [pom, 부모 pom, …] 목록을 반환한다.
        ``<relativePath>``(없으면 ``../pom.xml``)를 따라 artifactId가 맞는 동안 최대 10단계까지 올라간다.
        빈 ``<relativePath/>``, 프로젝트 밖의 경로, artifactId 불일치에서 멈춘다.

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
        """pom의 부모 좌표와 BOM import 목록을 반환한다.
        시도가 sbmig가 추적할 수 없는 방식으로 버전 관리를 바꿨는지 비교하는 데 쓴다.
        일반 dependencyManagement 항목은 직접 읽으므로 넣지 않는다.

        The pom's parent coordinates and BOM imports: what changes version management in a way sbmig cannot see
        (plain dependencyManagement entries are read directly by the local-chain check)."""
        p = ed.get_parent()
        parent = f"{p.group_id}:{p.artifact_id}:{p.version or ''}" if p is not None else ""
        boms = tuple(sorted(f"{g.key}:{g.version or ''}" for g in ed.managed_dependencies()
                            if (g.scope or "") == "import"))
        return parent, boms

    def _reported_missing(self, st: _Round | None, rel: str, aid: str | None, key: str) -> bool:
        """이 라운드의 빌드(또는 되돌리기로 돌아온 오류)가 이 pom의 g:a에 버전이 없다고 보고했는지 판단한다.

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
        """적용된 스크립트나 edits가 바꾼 pom에서 버전 없는 ``<dependencies>`` 항목에 버전을 채운다.
        시도의 커밋에 함께 들어가도록 커밋 전에 실행한다.
        - 시도가 추가한 의존성은 1~3단계(관리 버전, as-is, 저장소)로, 원래 있던 것은 빌드가 버전 없음을
          보고했을 때만 2단계로, 시도가 버전을 지운 것은 재구성 뒤 캡처가 있을 때만 1~2단계로 채운다.
          1단계에서는 프로젝트 안의 dependencyManagement도 보는데, 비워 두면 다음 빌드의 pom_invalid로 시도
          전체가 되돌려지기 때문이다
        - 시도가 버전을 명시해 추가한 의존성을 새 부모 체인이 관리하면 ``<version>``을 지운다.
          사내 의존성에는 체인의 버전 속성 ``${prop}``을 쓴다
        - 원래 있던 명시 버전, 부모나 BOM import를 바꾼 pom, 사내, 필수, 선택, 제거 대상, 자기 모듈, system
          scope 의존성은 건드리지 않는다. 예외를 던지지 않는다

        Fill the versions of the `<dependencies>` entries without `<version>` in the poms the applied script / edits
        changed (before the commit; the pom joins the attempt's commit): a dependency the attempt ADDED (absent from
        the pom at `pre`) by steps 1–3; one that was already there only when the build reported its version missing,
        by step 2; one whose version the attempt dropped by steps 1–2, and only with the post-restructure capture
        (without it the LLM's "the parent manages it" stands — an as-is version could be an old one). Step 1 also counts a `<dependencyManagement>` entry of the pom or an in-project parent (the tree
        as the attempt left it); without the post-restructure capture that is all step 1 sees — an added dependency
        left empty would come back as pom_invalid and the structural-regression rule would revert the whole try.
        A dependency the attempt ADDED with an explicit version the new chain manages (step 1: the capture, else the
        parent-chain facts) loses that `<version>` — managed wins (the as-is build never had it, no owner's pin to
        respect), with the `${prop}` the attempt added only for it; journal `version_filled` source `managed`,
        `removed` = the explicit text. v2.1t: an added IN-HOUSE dependency (version-less or literal) the chain does not
        manage gets the chain's family version property `${prop}` (`_chain_property_for`; source `chain-property`,
        `property`), protected in-house coordinates included. Explicit versions that were already there are never touched (the
        restructure's rules own those). A pom whose parent or BOM imports the attempt changed (or one below such a
        pom) is left alone. Protected keys, own modules and `system` scope are skipped. Returns
        [(pom, key, version, source)] of the versions written. Never raises."""
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
        removed: list[tuple[str, str, str, str, Any, Any]] = []   # (pom, key, explicit, managed, pom at pre, editor)
        replaced: dict[tuple[str, str], str] = {}     # v2.1t: (pom, key) → the literal a chain property replaced
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
                if not g.group_id or not g.artifact_id or "${" in key:
                    continue
                if g.version and key in pre_versions:   # an explicit version that was there: the restructure's
                    continue
                if own is None:
                    own = self._own_module_keys()
                if key in own or (g.scope or "") == "system":
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
                if g.version:                        # added WITH a version: the new chain's management wins
                    if (g.type or "jar") == "jar" and not g.classifier:   # managed per type:classifier
                        mv = r.managed_version(key, module)
                        if mv:
                            ed.remove_version(el)
                            removed.append((p, key, g.version, mv, before[p], ed))
                    continue
                if key in local:
                    continue
                if key not in pre_versions:          # added by this attempt: steps 1–3
                    # 재구성 뒤 캡처가 없으면 1단계는 프로젝트 안의 관리 버전(위 local)만 본다
                    # 버전을 비워 두면 다음 빌드의 pom_invalid가 구조적 악화로 시도 전체를 되돌리므로
                    # as-is나 저장소 버전을 쓴다
                    v, src = r.resolve(key, module, allow_repository=True)
                elif pre_versions[key]:              # the attempt dropped its version ("the parent manages it")
                    # 새 체인을 모르면 as-is 버전(이전 Boot 2 버전 등)을 되살리는
                    # 것이 더 나쁘므로 LLM의 판단을 그대로 둔다
                    if not r.has_chain:
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
            for _p, _k, explicit, _mv, ed0, _ed in [x for x in removed if x[0] == p]:
                self._drop_added_property(ed, ed0, explicit, p)
            if ed.changed:
                ed.save()
        for p, key, explicit, mv, _ed0, _ed in removed:
            att.notes.append(f"version removed by sbmig: {key} {explicit} in {p} (the new parent chain manages it at "
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
        """사내 의존성 g에 쓸 회사 체인의 버전 속성 이름을 ``chain_company_properties``에서 찾아 반환한다.
        필수, 선택 아티팩트 설정의 ``property_name``이 체인에 있으면 그것을 쓰고(재구성과 같은 순서), 아니면
        ``version_property_matches``의 가장 구체적인 것을 쓴다. 없으면 None을 반환한다.

        The company chain's version property for in-house `g` (company-defined properties only): the mandatory /
        optional spec's `property_name` when the chain defines it (the restructure's order), else the most specific
        `version_property_matches`; None when none."""
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
        """케이스 오류의 아티팩트와 classpath에서 찾은 라이브러리(``libs``)마다 관련된 회사 체인 버전 속성을
        프롬프트 정보 줄로 만든다.
        회사 pom이 정의한 속성(``chain_company_properties``)만
        ``parent chain property: <name>=<value> (<정의한 pom>)`` 형식으로, 중복 없이
        ``CHAIN_PROPERTY_FACTS_MAX``줄까지 쓴다. 예외를 던지지 않는다.

        The parent chain's version properties relevant to the case's artifacts and (v2.1v) the libraries located on
        the classpath (`libs`; compile errors carry no `e.artifact`), one `parent chain property: <name>=<value>
        (<defining pom>)` line each, ≤ `CHAIN_PROPERTY_FACTS_MAX`. Never raises."""
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
        except Exception as e:  # noqa: BLE001 - facts never break a fix attempt
            log.debug("chain property facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _drop_added_property(self, ed: Any, ed0: Any, version: str, rel: str) -> None:
        """지운 ``<version>``이 ``${속성}``이고, 그 속성을 이 시도가 이 pom에 추가했으며 이제 어느 pom도
        참조하지 않으면 속성도 지운다. 원래 있던 속성이나 다른 곳에서 쓰는 속성은 그대로 둔다.

        Drop the property a removed `${prop}` version used when this attempt added it to this pom and no project pom
        references it any more (an owner's property, or one still used, stays)."""
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
                except Exception:  # noqa: BLE001 - an unreadable pom: keep the property
                    return
        ed.remove_property(prop)

    def _declaring_pom(self, e: BuildError) -> str | None:
        """버전 없음 오류의 g:a를 버전 없이 선언한 pom을 찾는다.
        오류의 pom, 그다음 같은 artifactId의 모듈 pom 순서로 찾고, 각각 프로젝트 안의 부모까지 본다.
        찾지 못하면 None을 반환한다.

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
        """누락 버전 케이스를 LLM 없이 고칠 계획을 세운다.
        오류마다 (pom, g:a) 쌍의 버전을 as-is 빌드 -> 저장소 순서로 찾는다.
        빌드가 이미 버전 없음을 알렸으므로 1단계는 건너뛴다.
        찾지 못한 쌍은 바로 확인한 것으로 기억해 다음 시도가 두 번 늦어지지 않게 한다.
        찾은 쌍은 반환만 하고, 적용된 뒤 ``_version_attempt``가 기억한다.
        반환값은 ([(pom, g, a, 버전, 출처)], 찾지 못한 것, 새로 확인한 쌍이 있는지, 찾은 쌍)이다.

        The deterministic try's plan for a missing-version case: each (pom, g:a) of its errors not tried yet,
        resolved by steps 2–3. A pair nothing was found for is remembered at once (the ladder is not delayed twice);
        a resolved pair is only returned — `_version_attempt` remembers it once the edit applied. Returns
        ([(pom, g, a, version, source)], what stayed unknown, whether any pair was new, the resolved pairs)."""
        errs = [e for e in c.errors if e.signature_generic == MISSING_VERSION_SIG and e.artifact is not None
                and e.artifact.group_id and e.artifact.artifact_id]
        if not errs:
            return [], [], False, []
        # 쌍은 오류가 가리키는 (pom 또는 모듈, g:a)다.
        # 이미 확인한 쌍은 같은 결과가 나오므로 다시 찾지 않는다
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
        """LLM을 호출하지 않는 사전 시도 목록을 반환한다. 아래 순서대로 처음 해당하는 하나만 쓴다: 사내 부모
        버전 맞추기, 누락 버전, 리소스 필터링, 번들 jar, jakarta 빌드.

        The zero-LLM pre-tries, in order (the first that matches a case is the one): v2.1l versions, v2.1m resources,
        v2.1z-i bundled jars, v2.1z-e jakarta builds."""
        return (_PreTry(SBMIG_PARENT_SOURCE, self._parent_targets, self._parent_attempt),        # v2.1z-ag, first
                _PreTry(SBMIG_VERSION_SOURCE, self._version_targets, self._version_attempt),
                _PreTry(SBMIG_RESOURCES_SOURCE, self._resources_targets, self._resources_attempt),
                _PreTry(SBMIG_BUNDLED_SOURCE, self._bundled_targets, self._bundled_attempt),     # v2.1z-i, before jakarta
                _PreTry(SBMIG_JAKARTA_SOURCE, self._jakarta_targets, self._jakarta_attempt))

    def _pre_try_for(self, c: ErrorCluster, d: TriageDecision) -> tuple[_PreTry | None, list[str]]:
        """이 케이스에 먼저 실행할 사전 시도와 수정할 pom을 반환한다. 트리아지가 SCRIPT로 정했거나, LLM을 쓸
        수 없거나 예산이 없어서만 막혔고, 사전 시도 하나가 해당할 때 반환한다.
        사람이 고정한 차단과 이미 차단된 오류는 그대로 두며, 해당하지 않으면 (None, [])을 반환한다.

        The pre-try for this case and the poms it would edit, when the triager gave a SCRIPT decision or blocked it
        only for the LLM (unavailable / budget exhausted); else (None, [])."""
        if self.dry_run:
            return None, []
        tries = self._pre_tries()
        if d.kind != DecisionKind.SCRIPT:
            m = _BLOCKED_RAT_RE.search(d.rationale or "") if d.kind == DecisionKind.BLOCKED else None
            # 부모를 찾지 못해 트리아지가 parent_not_found로 차단한 케이스도 sbmig-parent 사전 시도는 먼저 실행한다
            if m is not None and m.group("r") == BlockReason.PARENT_NOT_FOUND.value:
                tries = tuple(t for t in tries if t.source == SBMIG_PARENT_SOURCE)
            elif m is None or m.group("r") not in (BlockReason.LLM_UNAVAILABLE.value, BlockReason.BUDGET_EXHAUSTED.value):
                return None, []
        for pre in tries:
            try:
                poms = pre.targets(c)
            except Exception as e:  # noqa: BLE001 - a broken matcher only means "no pre-try"
                log.debug("pre-try %s for %s: %s: %s", pre.source, c.id, type(e).__name__, e)
                poms = None
            if poms is not None:
                return pre, poms
        return None, []

    def _parent_misses(self, c: ErrorCluster) -> list[Any]:
        """이 케이스의 부모를 찾지 못한 오류 중 사전 시도가 맞출 대상을 반환한다.
        사내 부모 g:a의 as-is 버전(설정 버전이 아님)이고 이 실행에서 아직 시도하지 않은 것이다.
        ``openrewrite.unresolvable_asis_parent: align``일 때만 반환한다.

        The as-is company-parent versions of this case's parent-resolution errors the pre-try would align (switch
        `align` only, not tried in this run yet)."""
        if getattr(self.cfg.openrewrite, "unresolvable_asis_parent", "align") != "align":
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
        """사전 시도 ``sbmig-parent``를 실행한다. 사내 부모의 as-is 버전이 저장소에 없으면 그 부모를 선언한
        모든 pom에 설정 버전을 쓴다(``align_asis_parent``).
        커밋 ``fix(sbmig-parent): <cluster id>``, 저널 ``asis_parent_aligned``, 플래그 ``asis-parent-aligned``를 남긴다.
        부모 좌표마다 실행당 한 번만 시도하고, 맞출 것이 없으면 False를 반환한다.

        The zero-LLM `sbmig-parent` pre-try: set the configured version in every pom whose parent is the missing as-is
        version of a company parent (commit `fix(sbmig-parent): <cluster id>`, scored and rolled back like any try), once
        per parent coordinates per run. False when there is nothing to align."""
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
        """누락 버전 케이스면 오류의 pom 목록(없으면 빈 목록)을, 아니면 None을 반환한다."""
        if not any(e.signature_generic == MISSING_VERSION_SIG for e in c.errors):
            return None
        return list(dict.fromkeys(_norm_path(e.file) for e in c.errors
                                  if e.signature_generic == MISSING_VERSION_SIG and e.file))

    def _resources_targets(self, c: ErrorCluster) -> list[str] | None:
        """리소스 / war 플러그인이 필터링 복사에 실패한 케이스면 고칠 pom
        목록(``reactive_targets``)을, 아니면 None을 반환한다.
        해당 케이스에서만 pom을 읽도록 정규식으로 먼저 거른다.

        The poms `reactive_targets` picks for a resources / war plugin filtering failure; None when it is not one
        (a regex pre-check keeps the pom parsing to such cases)."""
        from sbmig.restructure.resources_filtering import REACTIVE_RE, reactive_targets
        errs = list(c.errors)
        if not any(REACTIVE_RE.search(f"{e.message or ''}\n{e.raw or ''}") for e in errs):
            return None
        return list(reactive_targets(self.ws, errs)) or None

    def _pre_try_deferral(self, st: _Round, poms: list[str]) -> tuple[list[str], str]:
        """사전 시도를 다음 빌드까지 미뤄야 하는지 판단해 (원인 케이스 목록, "pom" 또는 "files")를 반환한다.
        이번 라운드에 다른 케이스의 수정이 대상 pom이나 루트 pom을 바꿨거나 그 pom에서 실행됐으면 기다린다. 한 pom의 두
        시도가 함께 평가되거나, 앞 시도의 git revert가 이 시도의 변경과 충돌하지 않게 하기 위해서다.

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
        """사전 시도 한 번을 공통 순서로 실행하고, 적용됐으면 True를 반환한다.
        결정과 시도 기록 -> ``_begin_try`` -> ``_apply_edits``(edits 또는 파일을 직접 고치는 ``apply_fn``)
        -> 키 정리 -> 미루기 기록 -> 이력 기록
        적용된 키만 확인한 것으로 기억하고, 적용에 실패한 키는 ``PRE_TRY_APPLY_FAILURES``번까지 다시 시도한다.

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
        # 적용된 키만 확인한 것으로 기억한다.
        # 적용 실패(applier 예외, 커밋 실패, Windows의 일시적 파일 잠금)는
        # 다음 라운드에 키마다 PRE_TRY_APPLY_FAILURES번까지 다시 시도한다
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
        """누락 버전 케이스를 LLM 없이 고친다. 계획한 버전을 ``set_dependency_version`` 편집으로 만들어
        ``_apply_edits``로 적용하며, LLM과 예산은 쓰지 않는다.
        커밋은 ``fix(sbmig-version): <case>``이다.
        적용됐으면 True를 반환하고, 아니면 False를 반환해 같은 라운드에서 평소 시도 순서로 이어진다.

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
        """리소스 필터링 케이스를 LLM 없이 고친다. ``binary_resource_plan``이 찾은 확장자를
        ``apply_non_filtered_extensions``로 ``nonFilteredFileExtensions``에 추가한다.
        커밋은 ``fix(sbmig-resources): <cluster id>``이고 (pom, 확장자 묶음)마다 실행당 한 번만 시도한다.
        추가할 것이 없으면 False를 반환한다. 확장자가 없는 바이너리 파일은 인수인계 후속 조치로 남긴다.

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
        """리소스 필터링 케이스의 프롬프트 정보 줄을 만든다. 계획이 찾은 pom별 확장자(``plan.lines()``)와
        필터링되는 바이너리 파일(``RESOURCE_FACTS_FILES``개까지)이다.
        사전 시도가 적용되지 못했을 때 LLM이 참고한다. 예외를 던지지 않는다.

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

    def _version_facts(self, c: ErrorCluster, libs: Optional[list[GAV]] = None) -> list[str]:
        """프롬프트 "Dependency versions" 절의 앞부분에 들어갈 정보 줄을 만든다.
        - 케이스 오류의 아티팩트(사내 포함)와 classpath에서 찾은 라이브러리(``libs``)마다 as-is 빌드 버전(``as-is build:
          …``)과 새 부모 체인 / 목표 Boot BOM의 관리 버전(``new parent chain: manages g:a at <v>``)을 쓴다
        - 메이저 버전이 다르면 메이저 변화를 덧붙이고, classpath 라이브러리의 관리 줄 끝에는 ``API_CHANGED_TAIL``을 붙인다
        - 버전 정보를 캡처하지 못했으면 그 줄을 쓰지 않는다. 모르는 것을 선언되지 않았다고 말하지 않기 위해서다
        ``VERSION_FACTS_MAX``개까지 쓰고 예외를 던지지 않는다.

        The as-is / new-chain / target Boot BOM facts for every artifact of the case's errors (in-house ones too) and,
        since v2.1v, for the libraries located on the module classpath (`libs`, de-duplicated, ≤ `VERSION_FACTS_MAX`);
        `(major X → Y)` when the as-is and the managed majors differ; a located library's managed line ends with
        `API_CHANGED_TAIL`. A capture that is missing gives no line (unknown is not "not declared"). Never raises."""
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
                    out.append(f"new parent chain: manages {key} at {mv}{_major_delta(asis_v, mv)}{tail}")
                bv = r.boot_version(key, module)
                if bv:                               # v2.1s: the to-be line when the chain does not manage it
                    out.append(f"target Boot BOM: manages {key} at {bv}{_major_delta(asis_v, bv)}{tail}")
            return out
        except Exception as e:  # noqa: BLE001 - facts never break a fix attempt
            log.debug("version facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _package_facts(self, c: ErrorCluster) -> list[str]:
        """javax가 아닌 ``package <p> does not exist`` 케이스의 정보 줄을 만든다. 새 부모 체인 / 목표 Boot
        BOM이 관리하는 키 중 ``<p>``와 가장 길게 겹치는 groupId(``org.springframework.batch.item``이면
        ``org.springframework.batch``)의
        artifactId 들(정렬, ``PACKAGE_FACTS_ARTIFACTS`` 개까지): ``managed artifacts under <group> (add the one that holds
        <p>, version-less): a, b, …``.
        의존성을 추가해야 하는 케이스에서 LLM이 좌표를 짐작하지 않게 하기 위해서다.
        맞는 그룹이 없으면 쓰지 않고, 좌표는 관리 목록에서만 가져온다. 예외를 던지지 않는다.

        For `package <p> does not exist` errors (not `javax.*`): the artifactIds managed under the longest groupId that
        prefixes `<p>` (the new chain's capture / parent-chain facts and the target Boot BOM): one `managed artifacts
        under <group> (add the one that holds <p>, version-less): …` line per package (≤ `PACKAGE_FACTS_LINES`).
        Nothing when no managed group matches. Never raises."""
        try:
            pkgs: list[str] = []
            for e in c.errors:
                for text in (e.message, e.raw):
                    m = _PACKAGE_MISSING_RE.search(text or "")
                    if m and not m.group(1).startswith("javax.") and m.group(1) not in pkgs:
                        pkgs.append(m.group(1))
                        break
            bundled = self._bundled_facts(c)            # v2.1z-i: a jar in the project provides it
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
        except Exception as e:  # noqa: BLE001 - facts never break a fix attempt
            log.debug("package facts for %s unavailable: %s: %s", c.id, type(e).__name__, e)
            return []

    def _managed_keys(self) -> set[str]:
        """새 부모 체인(재구성 뒤 캡처의 전체와 모듈별 항목, 없으면 부모 체인 정보)과 목표 Boot BOM이
        관리하는 g:a 집합을 반환한다.

        Every g:a the new parent chain (the post-restructure capture, merged and per module; else the parent-chain
        facts) or the target Boot BOM manages."""
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
        """두 커밋 사이에 바뀐 파일별로 (경로, 추가 줄 수, 삭제 줄 수)를 반환한다.
        바이너리 파일은 0 / 0이고, 이름이 바뀐 파일은 새 경로로 쓴다.

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
        """수정이 병합 충돌 참고 파일의 경로에 파일을 썼으면 커밋 전에 되돌린다.
        참고 파일은 프롬프트에 보여 주려고 실행 디렉토리에 둔 것이며 빌드나 수정의 대상이 아니므로, 그
        경로에 쓴 파일은 프로젝트에 불필요한 파일이다.

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
        """저장된 스크립트 버전의 실적을 기록한다(``kb.cases.credit``).
        시도마다 최종 결과로 한 번 기록하고, 판단할 수 없었다가 다시 평가되면 한 번 더 기록한다.
        그 버전을 실행한 결과가 시도 결과를 정한 경우만 기록한다.
        edits와 함께 저장만 된 스크립트는 실행되지 않았으므로 기록하지 않는다.
        실행 정보(라운드, 시도 번호, 실패 이유, 오류 수, 파일)도 함께 남긴다.
        저장되지 않은 v0, 드라이런, KB 없음이면 아무것도 하지 않는다.

        `kb.cases.credit(case, version, result)` once per try and final outcome (an inconclusive one is credited
        again when a revert rebuild scores it) — only for a stored script whose RUN decided the outcome (a script
        stored next to applied edits never ran: no credit). Unstored (v0) scripts / dry run / no KB → nothing."""
        if self.dry_run or att.decision.kind != DecisionKind.SCRIPT or not att.script_version \
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
        """평가된 시도가 케이스를 해결하지 못한 이유 한 줄(ScriptRun.reason, 보강 버전의
        CaseScript.reason)을 반환한다. 개선이나 빌드 성공이면 빈 문자열을 반환한다.
        case.json만 보고도 어느 버전이 어디서 실패했고 왜 다음 버전이 생겼는지 알 수 있게 하기 위해서다.

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
        """여러 시도의 실적을 ``_credit_script``로 차례로 기록한다."""
        for a in attempts:
            self._credit_script(a)

    def _no_script_fixer(self, att: FixAttempt, c: ErrorCluster, why: str) -> None:
        """``propose_script``가 없는 tier-2 fixer일 때 처리한다.
        이 실행에서는 LLM 스크립트를 쓸 수 없고 저장된 스크립트 재사용만 한다.
        인프라 문제가 아니므로 ``infra:llm`` 대신 ``fixer-without-scripts`` 플래그와 후속 조치 한 줄을
        남기고, 해당 케이스는 llm_unavailable로 차단한다.

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
        """새 FixAttempt(id ``<phase>-<build>-<n>``)를 만든다.
        대상 오류를 함께 담고 현재 HEAD를 ``base_commit``으로 기록한다.
        ``base_commit..commit``이 이 시도가 적용한 정확한 변경이며 되돌리기의 기준이 된다."""
        att = FixAttempt(id=f"{phase.value}-{iteration}-{n}", phase=phase, iteration=iteration, cluster_id=c.id,
                         decision=d, errors_before=build.error_count, cluster_signatures=list(c.signatures),
                         cluster_errors=list(c.errors))   # scoring and the error list need the errors
        try:   # `git diff base_commit..commit` = the exact code the attempt applied (rollback)
            att.base_commit = self.ws.head()
        except Exception as e:  # noqa: BLE001 - a workspace without git: no diff, nothing else changes
            log.debug("base commit for %s unavailable: %s", att.id, e)
        return att

    def _record(self, pr: PhaseResult, att: FixAttempt) -> None:
        """시도를 단계 결과와 실행 이력(시도 순서와 횟수 한도의 기준)에 넣고 저널에 한 줄 남긴다."""
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
        """시도를 커밋한다.
        직전 빌드가 lib/에 복사한 jar 같은 파일은 수정이 아니므로 스크립트나 edits가 보고한 파일만 넣는다.
        바뀐 것이 없으면 None을 반환한다.

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
        """LLM 전송이나 설정 실패를 기록하고, 연속 2번이 되면 대응한다.
        실행에서 처음이면 ``llm.cooldown_seconds``만큼 쉬고 다시 시도하며, 이 시도들은 케이스의 LLM 한도에 세지 않는다.
        두 번째 연속 실패이거나 쿨다운을 쓸 수 없으면 실행 끝까지 LLM 사용 불가로 둔다.
        LLM이 필요한 케이스는 llm_unavailable로 차단되고 저장된 스크립트 재사용만 계속한다.

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
        """게이트웨이가 서로 다른 프롬프트 ``LLM_CONFIG_ERROR_PROMPTS``개에 같은 HTTP 400 본문을 반환했는지 확인한다.
        그렇다면 프롬프트가 아니라 설정(지원하지 않는 모델 이름, 페이로드 필드) 문제이므로 그 실행의 LLM
        호출을 멈춘다(``llm-config-error``). True를 반환하면 이 시도는 여기서 끝난다.

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
        """게이트웨이 503 같은 일시 장애에 대비해, LLM을 포기하기 전에 실행당 한 번 쉰다.
        쿨다운을 쓸 수 없으면(이미 씀, 꺼짐, 남은 실행 시간보다 김) False를
        반환하고, 호출한 쪽이 LLM 사용 불가로 표시한다.

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
        """``c``와 관련된 병합 충돌(first-wins)의 (참고 메모, 참고 파일)을 반환한다.
        남긴 파일이 오류 파일이거나 오류가 그 클래스 이름을 언급하면 관련된 것으로 본다.
        버려진 쪽 파일은 읽기 전용 참고로만 보여 준다.
        필요한 멤버를 옮겨 오게 하고, 두 번째 모듈을 다시 만들지 않게 하기 위해서다.

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
        """두 커밋 사이에 바뀐 파일 목록을 반환한다.
        NUL로 구분하므로 한글이나 공백이 있는 파일 이름도 그대로 받는다."""
        changed = getattr(self.ws, "changed_between", None)          # NUL-separated: Korean / spaced names verbatim
        if callable(changed):
            return changed(a, b)
        out = self.ws._git("diff", "--name-only", a, b).stdout   # noqa: SLF001 - a stand-in workspace without it
        return [l.strip() for l in out.splitlines() if l.strip()]

    # ------------------------------------------------------------------ fallback
    def _maybe_skip_test_compile(self, pr: PhaseResult, phase: Phase, goal: str, target: Optional[str],
                                 last: BuildResult) -> None:
        """남은 오류가 모두 테스트 컴파일 오류면 ``-Dmaven.test.skip=true``로 다시 빌드하는 대체 빌드를 실행한다.
        통과하면 단계를 통과로 보고 플래그 ``test-compile-skipped``를 남기며,
        테스트 오류는 담당 팀이 처리할 후속 조치로 남는다.
        기본 설정(``build.skip_test_compile``)은 처음부터 테스트를 컴파일하지 않으므로 보통은 해당하지 않는다."""
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
        """메인 코드는 빌드되고 테스트 실행 실패만 남았으면 ``-DskipTests``로 다시 빌드하는 대체 빌드를
        실행한다. 이런 실패는 대개 외부 설정이나 빌드 시점에 접근할 수 없는 인프라 때문이다.
        통과하면 플래그 ``tests-skipped``를 남기고, 실패한 테스트마다 미해결 후속 조치를 남긴다.

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
    """후보 버전 목록을 텍스트로 만든다.
    최신 ``VERSIONS_LOGGED``개를 쓰고, pom을 확인한 버전에는 jakarta / javax 표시를 붙인다.

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
    """저장소 조회 결과를 로그 한 줄(현재 버전, 같은 메이저 후보, 상위 메이저 후보)로 만든다.
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
    실패 이유(적용 오류), 되돌리기, 결과 평가 메모를 중복 없이 합치고 ``TRY_NOTE_CHARS``자에서 자른다.

    FixTry.note: why the try failed (apply errors), rollbacks, scoring notes — deduplicated, capped."""
    parts = list(dict.fromkeys([*att.apply_errors, *att.notes]))
    text = "; ".join(" ".join(p.split()) for p in parts if p)
    return text if len(text) <= TRY_NOTE_CHARS else text[:TRY_NOTE_CHARS - 1] + "…"


def _kept_partial(a: FixAttempt) -> bool:
    """부분 해결된 사전 시도(``sbmig-version`` / ``sbmig-resources``)인지 판단한다.
    이런 시도는 다음 시도 전에 되돌리지 않는다.
    추측이 아니라 확인된 정보(as-is 빌드, 저장소, 파일 스캔)로 쓴
    내용이고, 남은 오류는 sbmig가 찾지 못한 것뿐이기 때문이다.

    A partial zero-LLM pre-try stays in the tree (no rollback before the case's next try): what it wrote are facts
    (the as-is build / the repository / the file scan), what is left is what it could not find."""
    return a.source in PRE_TRY_SOURCES and a.outcome == "partial"


def _major_delta(before: Optional[str], after: Optional[str]) -> str:
    """as-is 버전과 관리 버전의 메이저가 다르면 메이저 변화 표시를 만든다.
    둘 중 하나를 모르거나 메이저가 같으면 빈 문자열을 반환한다.

    `" (major X → Y)"` when both majors are known and differ, else ""."""
    from sbmig.build.depchanges import major_of
    a, b = (major_of(before) if before else None), (major_of(after) if after else None)
    return f" (major {a} → {b})" if a is not None and b is not None and a != b else ""


def _mentions_javax(errors: Iterable[BuildError]) -> bool:
    """케이스 오류의 메시지, 심볼, 위치, 원문 중에 ``javax.``가 있는지 확인한다.
    저장소 목록과 판단은 이런 케이스에만 붙인다.

    Does any error of the case name `javax.` (message / symbol / location / raw)? v2.1d's repository listing and
    verdict exist for those cases only."""
    return any("javax." in (t or "") for e in errors for t in (e.message, e.symbol, e.location, e.raw))


def _synthetic(e: BuildError) -> bool:
    """Maven이 비정상 종료되거나 타임아웃일 때 실행기가 대신 넣는 오류인지 판단한다.
    이런 오류로는 빌드가 어디까지 진행됐는지 알 수 없다.

    The runner's stand-in error for a crashed / timed-out Maven: says nothing about how far the build got."""
    return e.kind == ErrorKind.UNKNOWN and not e.file and (e.message or "").startswith(_SYNTHETIC_PREFIXES)


def _pair(e: BuildError) -> tuple[str, str]:
    """시도 결과 평가에 쓰는 (파일, 서명) 쌍을 반환한다.
    L1 서명에는 파일이 없어 두 파일의 같은 오류가 같은 서명이 되므로, 파일과 짝지어 횟수로 비교한다.

    (file, signature) of an error — file '' for module / artifact errors. L1 signatures carry no file: the same
    renamed method in two files is one signature, so scoring compares these pairs (with their occurrence counts)."""
    return (_norm_path(e.file) if e.file else ""), (e.signature or f"{e.kind.value}|{e.message}")


def _touched(a: FixAttempt) -> set[str]:
    """시도가 바꾼 파일 경로를 반환한다. 이름이 바뀐 파일은 이전 경로와 새 경로를 모두 넣는다."""
    return {_norm_path(p) for c in a.changes for p in (c.path, c.new_path) if p}


def _targeted_left(a: FixAttempt, cur: Counter) -> tuple[int, int]:
    """(빌드에 아직 남은 대상 오류 수, 전체 대상 오류 수)를 (파일, 서명) 횟수로 세어 반환한다.
    오류 기록이 없는 시도는 서명만으로 전부 남았는지 전부 사라졌는지 판단한다.

    (targeted errors still in the build, targeted errors) — by (file, signature) occurrence; attempts without
    recorded errors fall back to the cluster signatures (all or nothing)."""
    if not a.cluster_errors:
        sigs = set(a.cluster_signatures)
        return (1 if any(s in sigs for _f, s in cur) else 0), 1
    targeted = Counter(_pair(e) for e in a.cluster_errors)
    return sum(min(n, cur[k]) for k, n in targeted.items()), sum(targeted.values())


def _stage_of(errors: list[BuildError]) -> int:
    """이 오류들이 보고되는 가장 이른 빌드 단계 번호를 반환한다. 오류가 없으면 99를 반환한다.

    The earliest build stage (`STAGE` / `error_stage`) at which these errors are reported; 99 when none has one."""
    return min((error_stage(e) for e in errors if error_stage(e) is not None), default=99)


def _visible_in(errors: list[BuildError], build: BuildResult, real: list[BuildError]) -> bool:
    """지금은 없는 이 오류들을 이 빌드가 보고할 수 있었는지 판단한다.
    그 모듈이 빌드됐거나 빌드가 그 단계까지 진행됐으면 True를 반환한다.
    아니면 오류가 해결된 것이 아니라 보이지 않을 뿐이므로, 시도는 개선이 아니라 판단 불가가 된다.

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
    """직전 빌드보다 늘어난 (파일, 서명) 오류를 새 오류로 반환한다.

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
    """``files`` 안에서 새로 생긴 오류를 (파일, 서명) 횟수로 찾는다.
    다른 파일에 이미 있던 L1 서명이어도 이 파일에서는 새 오류다.

    New errors (by (file, signature) occurrence) in `files` — round 10: a new error whose L1 signature another file
    already had is still new here."""
    return [e for e in _new_errors([e for e in errors if e.file and _norm_path(e.file) in files], prev)]


def _target_key(target: Optional[str]) -> str:
    """빌드 대상을 정규화한 키를 반환한다. 빈 문자열은 프로젝트 전체이고, 끝의 pom.xml은 뗀다.
    대체 빌드 결과를 대상별로 기억할 때 쓴다."""
    t = _norm_path(target or "").strip("/")
    if t.endswith("pom.xml"):
        t = t[:-len("pom.xml")].rstrip("/")
    return t


def _gone(errors: list[BuildError], build: BuildResult) -> list[BuildError]:
    """``errors`` 중 이 빌드에 더 이상 없는 오류를 반환한다. 시도가 없앤 오류이며, 되돌리면 다시 나타난다.

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
    """다른 파일의 같은 오류에 대한 힌트에 쓰도록, 시도가 바꾼 내용을 한 줄로 만든다.
    바꾼 파일 목록을 쓰고, 없으면 원인 분석의 첫 문장을 쓴다.

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
    """빌드 오류의 케이스 키를 제자리에서 다듬는다.
    대략적인 ``build`` / ``other`` 키를 javac 줄로 더 세분한다.
    시도 결과 평가, 오류 목록, 클러스터, 차단 목록이 같은 L1 / L2 서명을 보도록 빌드 직후 한 번만 실행한다.

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
