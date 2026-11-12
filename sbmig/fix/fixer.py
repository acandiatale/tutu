"""LLM에 빌드 오류 수정 스크립트를 요청하는 LLMFixer 모듈.
수정 단계 플러그인 인터페이스(`ScriptFixerLike.propose_script`)의 기본 구현이다.
처리 순서: 문맥 구성 -> LLM 호출 -> 답변 파싱 -> 정적 검사와 피드백 1회 -> FixAnswer.
수정 루프가 보강이나 신규 단계에서 LLM이 필요할 때 `propose_script`를 호출하고, 메이저 업그레이드가
`<verify>` 없이 적용되면 `verify_guide`를 호출한다.
- 답변은 `<analysis>`, `<solution>`, `<edits>`, `<script>` 또는 `<no_script>`, `<changes>`, `<verify>` 블록으로 이뤄진다
- propose_script 한 번에 LLM을 첫 요청, 문맥 라운드 최대 `llm.max_context_rounds`회, 피드백 1회까지
  호출한다. 케이스당 요청 횟수는 수정 루프가 정한다
- fixer는 작업 트리를 고치지 않는다. 적용, 커밋, 재빌드로 하는 시도 결과 평가, 되돌리기는 수정 루프가 한다
- 예외를 던지지 않는다. 전송 오류는 수정 루프가 분류하는 `llm-error: …` 문구로 바꿔 돌려준다

LLMFixer: the default fixer seam (`propose_script`, ScriptFixerLike): context packing → LLM → answer parsing →
static pre-check / one feedback round → FixAnswer (= ScriptProposal).

`propose_script` asks for ONE case (L2) across files: model choice, call tags, usage accounting, prompt hash, record
dir and <need_files>/<need_grep> context rounds. v2.1 (docs/v2/PLAN-v2.1.md §3.6): the answer has four parts —
`<analysis>` 원인, `<solution>` 해결 방법, `<edits>` 수정 (JSON list of `Edit`, applied by the loop through
`sbmig.script.apply_edits`), `<script>` 스크립트 (stored for the same case in other projects) or `<no_script>` with the
reason. Mode `new` asks for both edits and a script; `augment` for a better script, or no_script + edits. The answer
is parsed by `sbmig.fix.script_protocol`, the script statically pre-checked (`sbmig.script.check_script`); an
unusable script or unreadable edits get ONE feedback round. The fixer never edits the tree.

v2.1b (2026-10-27): every modification is described for the business team — each edit's `desc` and, with a script,
the `<changes>` list (`FixAnswer.changes`, EditNote); the language follows `report.language` (`describe_language`).
A script without a `<changes>` list is not an error: `analysis` gets "[sbmig] the answer did not describe its
changes" and the loop builds the notes from the diff (`sbmig.script.notes.merge_notes`).

v2.1d (2026-10-27, dependency version policy): the answer may carry `<verify>` 운영자 확인 사항 (REQUIRED after a major
version upgrade / a library replaced, rule 14 of the script prompt) → `ScriptProposal.verify`. `verify_guide`
(`VerifyGuideLike`) is the ONE extra call the loop makes when such a change was applied without a `<verify>`."""
from __future__ import annotations

import inspect
import json
import logging
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

import jinja2

from sbmig.core.config import Config
from sbmig.core.interfaces import ChatMessage, FixContext, LLMClientLike, LLMResponse
from sbmig.core.models import (
    DependencyChange,
    ErrorCluster,
    LLMUsage,
    ProjectProfile,
    ScriptBase,
    ScriptProposal,
)
from sbmig.core.workspace import Workspace

from .client import LLMError, RecordingClient, llm_call_in_flight, messages_hash, usage_from
from .context import (
    ASIS_LABEL,
    TARGET_MARK,
    ContextPacker,
    PackedContext,
    asis_line,
    dedupe_errors,
    fence_for,
    format_error_line,
    lang_for,
    target_text,
    with_target,
)
from .script_protocol import (
    NO_SCRIPT,
    PROTOCOL_WARNING,
    ParsedScript,
    normalize_path,
    parse_script_response,
)

log = logging.getLogger("sbmig.fix.fixer")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MAX_SERVED_FILES = 6
MAX_SERVED_LINES = 300
SERVED_HEAD_LINES = 200
SERVED_TAIL_LINES = 80
MAX_GREP_LINES = 40
MAX_GREP_FILES = 3000             # a <need_grep> scans at most this many project files …
MAX_GREP_FILE_BYTES = 512_000     # … skipping files bigger than this (generated sources, bundles)
GREP_BUDGET_S = 45.0              # … and stops after this long (Windows + on-access antivirus made it crawl)
# the contract's import whitelist (docs/INTERFACES.md "Script contract (S)"); sbmig.script.ALLOWED_IMPORTS wins when present
DEFAULT_SCRIPT_IMPORTS = ("re", "json", "textwrap", "fnmatch", "posixpath", "itertools", "collections", "functools",
                          "string", "math")
# v2.1d verify_guide (VerifyGuideLike): the 운영자 확인 사항 after a major upgrade the answer did not describe
VERIFY_ROLE = "verify_guide"
VERIFY_MAX_ERRORS = 5             # the case's errors shown (one line each)
VERIFY_MAX_CHANGES = 20           # dependency changes listed
VERIFY_MAX_DIFF_CHARS = 6000      # the applied diff, capped
VERIFY_MAX_SOLUTION_CHARS = 1500
VERIFY_MAX_CHARS = 4000           # the guide returned
_VERIFY_OPEN_RE = re.compile(r"<\s*verify\s*>", re.IGNORECASE)
_VERIFY_CLOSE_RE = re.compile(r"<\s*/\s*verify\s*>", re.IGNORECASE)
# a fix / review answer (a replayed fix answer, the "no fix" answer) is not a guide
_FIX_TAG_RE = re.compile(r"<\s*(?:analysis|solution|edits|changes|no_script|blocked|script|need_files?|need_grep|review)"
                         r"\s*>", re.IGNORECASE)
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)


def _script_module_attr(name: str) -> Any:
    """`sbmig.script` 모듈의 속성을 읽는다.
    모듈이 없거나 깨졌으면 None을 반환하고, 그때는 fixer 안에 둔 복사본을 쓴다.

    An attribute of agent S's `sbmig.script` (HELPER_API_DOC, ALLOWED_IMPORTS, check_script), or None when the
    module is missing or broken — the fixer then falls back to its own copy of the contract."""
    try:
        import sbmig.script as script_mod
    except Exception:            # ImportError, or a half-written module during development
        return None
    return getattr(script_mod, name, None)


def script_allowed_imports(extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    """스크립트가 import할 수 있는 모듈 목록을 만든다.
    `sbmig.script.ALLOWED_IMPORTS`(없으면 fixer 안의 복사본)에 `script.extra_allowed_imports`를 더한다."""
    base = _script_module_attr("ALLOWED_IMPORTS") or DEFAULT_SCRIPT_IMPORTS
    out = [str(m) for m in base]
    out += [str(m) for m in extra if str(m) not in out]
    return out


def script_helper_api_doc(extra_imports: tuple[str, ...] = (), timeout_s: Optional[int] = None) -> str:
    """스크립트 프롬프트에 넣을 헬퍼 API 표를 만든다.
    만들지 못하면 빈 문자열을 반환하고, 그때는 프롬프트 파일에 있는 표를 쓴다.

    The helper API block of the script prompt from agent S's `sbmig.script` (`helper_api_doc(...)`, else
    `HELPER_API_DOC`); "" when unavailable — the prompt file then uses its own copy of the table."""
    fn = _script_module_attr("helper_api_doc")
    if callable(fn):
        try:
            return str(fn(extra_imports=list(extra_imports), timeout_s=timeout_s) or "").strip()
        except Exception:
            log.debug("sbmig.script.helper_api_doc failed; trying HELPER_API_DOC", exc_info=True)
    doc = _script_module_attr("HELPER_API_DOC")
    return str(doc).strip() if doc else ""


@lru_cache(maxsize=8)
def load_script_prompts(version: str = "v1", extra_imports: tuple[str, ...] = (),
                        timeout_s: Optional[int] = None, target: str = "") -> tuple[str, jinja2.Template]:
    """수정 스크립트 요청의 (system 프롬프트, user 템플릿)을 읽고 헬퍼 API 표와 허용 import를 채운다.
    목표 버전(`{{ target }}`)은 프롬프트 파일에 직접 쓰지 않고 `target` 인자로 채운다.

    (system prompt, user template) of a fix-script request. The helper API block comes from `sbmig.script`
    (`helper_api_doc`) when that module provides it, else from the table kept in `script_system_<version>.md` (both
    mirror the INTERFACES.md "Script contract (S)" table). v2.1u: `{{ target }}` is filled from `target`
    (`target_text(cfg)` from the caller; "" = the TargetConfig defaults) — no version is written in the prompt file."""
    sys_path = PROMPTS_DIR / f"script_system_{version}.md"
    user_path = PROMPTS_DIR / f"script_user_{version}.md.j2"
    if not sys_path.exists() or not user_path.exists():
        raise FileNotFoundError(f"script prompt version {version!r} not found in {PROMPTS_DIR}")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, autoescape=False)
    system = env.from_string(sys_path.read_text(encoding="utf-8")).render(
        helper_api=script_helper_api_doc(extra_imports, timeout_s),
        allowed_imports=", ".join(f"`{m}`" for m in script_allowed_imports(extra_imports)),
        target=target or target_text(None))
    user_t = env.from_string(user_path.read_text(encoding="utf-8"))
    return system.strip() + "\n", user_t


def default_script_checker(cfg: Config) -> Optional[Callable[[str], list[str]]]:
    """설정(추가 import, 최대 코드 크기)을 적용한 `sbmig.script.check_script` 검사 함수를 만든다.
    검사 모듈이 없으면 None을 반환한다.
    이 검사에서 문제가 나오면 피드백 1회 뒤에도 남은 스크립트를 수정 루프가 저장하거나 실행하기 전에 거절한다.

    `sbmig.script.check_script` bound to the config (extra imports, max code size), or None when unavailable.
    Its findings reject a script before the loop stores / runs it (one feedback round first)."""
    check = _script_module_attr("check_script")
    if not callable(check):
        return None
    try:
        params = inspect.signature(check).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, Any] = {}
    extra = list(cfg.script.extra_allowed_imports)
    for name, value in (("extra_imports", extra), ("extra_allowed_imports", extra),
                        ("max_chars", cfg.script.max_code_chars), ("max_code_chars", cfg.script.max_code_chars),
                        ("cfg", cfg), ("config", cfg)):
        if name in params:
            kwargs[name] = value

    def _check(code: str) -> list[str]:
        res = check(code, **kwargs)
        if res is None or res is True:
            return []
        if res is False:
            return ["refused by the static check"]
        if isinstance(res, str):
            return [res] if res else []
        problems = getattr(res, "problems", res)        # a result object or a plain list
        return [str(x) for x in problems or []]

    return _check


@lru_cache(maxsize=1)
def advisory_markers() -> frozenset[str]:
    """라이브러리가 아니라 권고 표시로 쓰는 탐지 이름 목록을 반환한다.
    `LIBRARY_RULES`에서 좌표 패턴이 없는 규칙이 여기에 해당한다.
    이 이름은 프롬프트의 `libs=`에서 빼고 권고는 힌트로 따로 보낸다.
    이름은 코드에 두지 않고 규칙 표에서 읽으며, 읽지 못하면 빈 집합을 반환한다.

    Detection names that are advisory markers, not libraries: the `LIBRARY_RULES` entries without any coordinate
    pattern (import-only, e.g. the JDK-internal APIs marker). Kept out of `libs=` (the advisories reach the prompt as
    hints); read from the rules table, never listed here; empty when the table cannot be read."""
    try:
        from sbmig.analyze.libraries import LIBRARY_RULES
    except Exception:  # noqa: BLE001 - analyzer missing / half-built: show every library
        return frozenset()
    return frozenset(r.name for r in LIBRARY_RULES if not (r.deps or r.managed or r.parents))


def profile_summary(profile: ProjectProfile, cfg: Config | None = None) -> str:
    """프롬프트에 넣을 프로젝트 한 줄 설명을 만든다.
    분석 시점의 프로젝트 정보이므로 앞에 `as-is (before sbmig): …`를 붙인다.
    - `case=`가 항상 첫 항목이다. ContextPacker가 이 값으로 노트를 고른다
    - jakarta 변환이 이미 끝났으므로 `javax-imports=`는 넣지 않고, 권고 표시는 `libs=`에서 뺀다
    - 목표 버전은 ``cfg.target``에서 만든다

    One-line project description for prompts (and for FixContext.profile_summary), labelled as the AS-IS project
    (`as-is (before sbmig): …`, v2.1u): `case=` first (the packer selects notes by it), no `javax-imports=` (the jakarta
    rewrite already ran), advisory markers kept out of `libs=`; the ` | target: …` tail from `cfg.target`
    (TargetConfig defaults without a config) — the packer retargets a summary built without one."""
    parts = [f"case={profile.case_type.value}"]
    parts.append(f"java={profile.java_version or '?'}")
    if profile.spring_boot_version:
        parts.append(f"boot={profile.spring_boot_version}")
    if profile.spring_version:
        parts.append(f"spring={profile.spring_version}")
    if profile.packaging:
        parts.append(f"packaging={profile.packaging}")
    parts.append(f"modules={len(profile.poms)}")
    if profile.services:
        parts.append(f"services={len(profile.services)}")
    markers = advisory_markers()
    shown = [l for l in profile.libraries if l.name not in markers]
    if shown:
        libs = [f"{l.name}:{l.version}" if l.version else l.name for l in shown[:20]]
        parts.append("libs=" + ",".join(libs))
    src = profile.source
    if src.has_web_xml:
        parts.append("web.xml")
    if src.jsp_count:
        parts.append(f"jsp={src.jsp_count}")
    return ASIS_LABEL + " ".join(parts) + TARGET_MARK + target_text(cfg)


def _unset(v: Optional[str]) -> bool:
    return not v or "${" in v or v == "default"



def describe_language(cfg: Optional[Config]) -> str:
    """프롬프트가 요구할 변경 설명의 언어를 정한다.
    `report.language`를 따르고, `sbmig.script`를 불러오지 못하면 한국어를 쓴다.

    The language of the change descriptions for the prompts (`sbmig.script.notes.describe_language`)."""
    try:
        from sbmig.script.notes import describe_language as _describe
    except Exception:  # noqa: BLE001 - sbmig.script missing / half-built
        return "Korean (한국어)"
    return _describe(cfg)


@lru_cache(maxsize=4)
def load_verify_prompt(version: str = "v1", target: str = "") -> str:
    """verify_guide 요청의 system 프롬프트를 읽는다.
    `{{ target }}`은 `target`으로 채우고 `{{ language }}`는 호출하는 쪽이 채운다.

    The system prompt of a `verify_guide` request (`verify_system_<version>.md`, v1 when the version has none);
    `{{ target }}` is filled from `target` (v2.1u; "" = the TargetConfig defaults), `{{ language }}` by the caller."""
    path = PROMPTS_DIR / f"verify_system_{version}.md"
    if not path.exists():
        path = PROMPTS_DIR / "verify_system_v1.md"
    return path.read_text(encoding="utf-8").replace("{{ target }}", target or target_text(None)).strip() + "\n"


@dataclass
class VerifyAnswer:
    """verify_guide 답의 파싱 결과를 담는 클래스.
    가이드 텍스트(빈 문자열이면 쓸 내용 없음)와 버린 이유를 가진다.

    A parsed `verify_guide` answer: the guide text ("" = nothing usable) and why it was dropped."""
    text: str = ""
    warnings: list[str] = field(default_factory=list)


def _cap_lines(text: str, limit: int) -> str:
    """`text` cut at a line end within `limit` characters (with a "…" line), or as it is."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 2]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.rstrip() + "\n…"


def parse_verify_answer(text: str) -> VerifyAnswer:
    """verify_guide 답에서 가이드 텍스트를 꺼낸다.
    `<verify>` 블록이 있으면 그 내용을, 없으면 답 전체를 쓴다.
    수정 답이 왔으면 빈 문자열이다. 바깥 펜스와 markdown 제목 표시를 지우고 VERIFY_MAX_CHARS자로 자른다.

    The guide of a `verify_guide` answer: the `<verify>` block when there is one (closing tag optional), else the
    whole answer — unless it is a fix answer (`<analysis>` / `<script>` / … without `<verify>`, e.g. a replayed fix
    answer or the "no fix" answer): then "". Outer code fence and markdown heading markers dropped, blank-line runs
    collapsed, capped at VERIFY_MAX_CHARS."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = VerifyAnswer()
    m = _VERIFY_OPEN_RE.search(t)
    if m:
        rest = t[m.end():]
        close = _VERIFY_CLOSE_RE.search(rest)
        body = rest[:close.start()] if close else rest
    elif _FIX_TAG_RE.search(t):
        out.warnings.append("verify guide: the answer is a fix answer, not a guide")
        return out
    else:
        body = t
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.match(r"^\s*(`{3,}|~{3,})[\w+.-]*\s*$", lines[0]):
        lines.pop(0)
        if lines and re.match(r"^\s*(`{3,}|~{3,})\s*$", lines[-1]):
            lines.pop()
    body = _HEADING_RE.sub("", "\n".join(ln.rstrip() for ln in lines))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        out.warnings.append("verify guide: empty answer")
    out.text = _cap_lines(body, VERIFY_MAX_CHARS)
    return out


def _change_line(c: DependencyChange) -> str:
    """One dependency change for the verify prompt: `- [major] g:a 2.4.2 → 3.2.1 — dependencies in demo-back/pom.xml`."""
    line = f"- [{c.kind}] {c.text} — {c.section} in {c.path}"
    if c.approved:
        line += " — approved by a person (libraries.yml)" + (f": {c.note}" if c.note else "")
    elif c.note:
        line += f" — note: {c.note}"
    return line


def _same_code(a: str, b: str) -> bool:
    """공백만 다른 두 스크립트를 같은 코드로 보고 비교한다. 원본과 같은 보강 답을 거절할 때 쓴다.

    Two scripts are the same code when they differ in whitespace only."""
    def norm(t: str) -> list[str]:
        return [ln.rstrip() for ln in t.replace("\r\n", "\n").strip().split("\n") if ln.strip()]
    return norm(a) == norm(b)


class LLMFixer:
    """기본 fixer(ScriptFixerLike) 클래스.
    채팅 완성 요청으로 케이스마다 파이썬 수정 스크립트 하나와 이 프로젝트용 편집을 받는다.
    사내 fix.plugin은 propose_script를 직접 구현하면서 같은 문맥(`ContextPacker.pack_script`)을 재사용할 수 있다.

    Default fixer (ScriptFixerLike): one Python fix script per case over a chat completion.

    A fix.plugin (in-house coding agent, docs/ADAPTERS.md) implements `propose_script` itself and can reuse the same
    packed context we give our LLM: `ContextPacker(cfg).pack_script(ws, cluster, ctx, ctx.profile_summary)` →
    PackedContext (sections with the case's errors, source windows, poms, hints, previous attempts; `.render()`
    gives one markdown string)."""

    def __init__(self, cfg: Config, client: LLMClientLike, notes_dir: Optional[str | Path] = None,
                 *, packer: Optional[ContextPacker] = None,
                 script_checker: Optional[Callable[[str], list[str]]] = None):
        self.cfg = cfg
        self.client = client
        self.packer = packer or ContextPacker(cfg, notes_dir)
        # the static pre-check of a proposed script (default: sbmig.script.check_script, looked up lazily; a
        # ScriptRunnerLike's `check` fits as well)
        self.script_checker = script_checker
        self._script_prompts: Optional[tuple[str, jinja2.Template]] = None
        # verify_guide는 텍스트만 반환하므로 사용량을 여기 남겨 수정 루프가 예산에 반영하게 한다
        # v2.1d verify_guide: usage of the last call (the loop charges it: the method returns only the text) and of
        # every call of this fixer
        self.last_verify_usage = LLMUsage()
        self.verify_usage = LLMUsage()

    # ------------------------------------------------------------------ ScriptFixerLike
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal:
        """케이스 하나에 대한 수정 답(FixAnswer)을 LLM에서 받는다.
        `ctx.script_mode`는 new | augment이고, `ctx.script_base`는 보강할 버전이다.
        - `code`나 `edits`가 있으면 쓸 수 있는 답이고, 둘 다 없을 때만 `error`를 채운다
        - 피드백 뒤에도 쓸 수 없는 스크립트와 읽을 수 없는 편집은 버리고 그 사실을 "[sbmig] …"로 남긴다
        - 예외를 던지지 않는다. 전송, 설정 실패는 `llm-error: …` 분석 문구로 돌려준다

        The answer for ONE case (`ctx.script_mode` new | augment, `ctx.script_base` = the version to extend) as a
        FixAnswer: `analysis` 원인, `solution` 해결 방법, `edits` (validated `Edit`s; the loop applies them with
        `sbmig.script.apply_edits`), `code` (the script; "" = none) and `no_script_reason`. Usable = `code` or
        `edits`; `error` is set only when neither is (then `code=""`, `edits=[]`). A script dropped after the
        feedback round next to valid edits → `no_script_reason` "[sbmig] the script could not be used: …"; edits
        dropped next to a valid script → an "[sbmig] edits dropped: …" line in `analysis`. Never raises; transport and
        configuration failures carry the analysis texts the loop classifies with `is_llm_transport_error` /
        `llm_400_body` (`llm-error: …`, `llm-error: response did not follow the protocol: …`)."""
        parse = parse_script_response
        model = self._model(ctx)
        usage = LLMUsage()
        requested: list[str] = []
        transcript: list[dict] = []
        rounds = 0
        messages: list[ChatMessage] = []
        last: Optional[LLMResponse] = None
        base = ctx.script_base
        # 보강 답은 base를 대체하는 완전한 스크립트이므로 base가 없으면 신규로 요청한다
        mode = "augment" if base is not None and ctx.script_mode in ("augment", None) else "new"
        if ctx.script_mode == "augment" and base is None:
            log.warning("script request for %s: augment without a script base — asking for a new script", cluster.id)
        role, meta = self.call_tags(ctx, cluster.id, ws)
        case_id = ctx.case_id or cluster.case_id or ""
        meta["script_mode"] = mode
        if case_id:
            meta["case_id"] = case_id
        if mode == "augment" and base is not None:
            meta["script_base_version"] = base.version
        adv_in_prompt = 0
        packed: Optional[PackedContext] = None
        try:
            system, user_t = self.script_prompts()
            log.info("script prompt for %s (%s%s): packing %d error(s) (%d advisory hint(s), %d KB hint(s), %d "
                     "reference file(s)%s)", cluster.id, mode, f" of v{base.version}" if mode == "augment" and base
                     else "", len(cluster.errors), len(ctx.advisory_hints), len(ctx.kb_hints), len(ctx.reference_files),
                     f", {len(ctx.library_hints)} dependency version line(s)" if ctx.library_hints else "")
            packed = self.packer.pack_script(ws, cluster, ctx, ctx.profile_summary)
            log.info("script prompt for %s packed: %d chars, %d file(s) shown, %d listed by name, sections [%s]%s → "
                     "calling the LLM", cluster.id, packed.chars, len(packed.files_included),
                     len(packed.files_listed), ", ".join(t[:28] for t, _ in packed.sections),
                     f", truncated {', '.join(packed.truncated)}" if packed.truncated else "")
            guidance = next((b for t, b in packed.sections if t == ContextPacker.ADVISORY_TITLE), "")
            if guidance:
                adv_in_prompt = sum(1 for ln in guidance.splitlines() if ln.startswith("- "))
            messages = self.build_script_messages(packed, mode, base, system=system, user_template=user_t)
            # v2.1z-p: the case thread — an augment continues the gateway session of the base attempt with the delta only
            continuing = (self._thread_on() and mode == "augment" and base is not None
                          and bool(getattr(base, "session_id", None)))
            # v2.1z-x (user, 2026-11-05): a gateway session cannot change its model — when this ask goes to another model
            # (the escalation model from the case's third ask / after a stall, or back), the full prompt opens a NEW session
            if continuing and (getattr(base, "session_model", "") or "") != (model or ""):
                log.info("case thread for %s: the model changes (%s → %s) and a gateway session keeps its model — the full "
                         "prompt opens a new session", cluster.id, base.session_model or "(gateway default)",
                         model or "(gateway default)")
                continuing = False
            if continuing:
                full_chars = sum(len(m.content) for m in messages)
                messages = self.build_thread_turn(packed, base)
                meta["session_id"] = str(base.session_id)
                log.info("script prompt for %s: continuing the case's gateway session %s with the delta (%d chars; the full "
                         "prompt would be %d)", cluster.id, base.session_id, sum(len(m.content) for m in messages), full_chars)
            else:
                meta.pop("session_id", None)
            try:
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta, parse=parse)
            except LLMError as e:
                if continuing and getattr(self.cfg.llm, "thread_fallback_full", True) and e.status is not None \
                        and 400 <= int(e.status) < 500:
                    log.warning("thread continuation for %s rejected (HTTP %s): the full prompt goes out in a new session",
                                cluster.id, e.status)
                    meta.pop("session_id", None)
                    messages = self.build_script_messages(packed, mode, base, system=system, user_template=user_t)
                    last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta, parse=parse)
                else:
                    raise
            verify = parsed.verify                  # v2.1d: the latest <verify> of any round

            # 1) 문맥 라운드: LLM이 파일이나 grep을 요청하면 제공하고, 쓸 수 있는 답이 오거나 한도에 이르면 멈춘다
            # 1) context rounds
            while parsed.wants_context and not parsed.usable and not parsed.blocked_reason \
                    and rounds < self.cfg.llm.max_context_rounds:
                rounds += 1
                requested.extend(p for p in parsed.need_files if p not in requested)
                remaining = self.cfg.llm.max_context_rounds - rounds
                log.info("script prompt for %s: the LLM asked for more context (round %d/%d): %d file(s) %s, %d "
                         "grep(s) %s", cluster.id, rounds, self.cfg.llm.max_context_rounds, len(parsed.need_files),
                         parsed.need_files[:4], len(parsed.need_grep), parsed.need_grep[:3])
                served = self.serve_context(ws, parsed.need_files, parsed.need_grep, remaining)
                messages = self._follow_up(messages, last, served, meta)
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta, parse=parse)
                verify = parsed.verify or verify
            if parsed.wants_context and not parsed.usable and not parsed.blocked_reason:
                requested.extend(p for p in parsed.need_files if p not in requested)

            # 2) 정적 검사와 피드백 1회: 스크립트를 그대로 쓸 수 없거나, patches 형식이거나, 편집 JSON을 읽을 수 없을 때
            # 2) static pre-check + one feedback round for a script that cannot be used as written / edits that
            #    cannot be read
            notes: list[str] = []
            sbase = base if mode == "augment" else None
            problems = self._script_problems(parsed, sbase)
            if self._wants_feedback(parsed, problems):
                rounds += 1
                fb = self._feedback_problems(parsed, problems)
                log.info("answer for %s cannot be used (%s) → one feedback round", cluster.id, fb[0][:160])
                fb_text = self.script_feedback_message(parsed, fb, mode)
                messages = self._follow_up(messages, last, fb_text, meta)
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta, parse=parse)
                verify = parsed.verify or verify
                problems = self._script_problems(parsed, sbase)
                if not (parsed.code and not problems) and not (parsed.edits and not parsed.edits_problem):
                    notes.append("feedback round did not give a usable script or edits (first answer: "
                                 f"{fb[0][:200]})")

            code = parsed.code if not problems else ""
            edits = list(parsed.edits) if not parsed.edits_problem else []
            analysis = parsed.analysis
            no_script = parsed.no_script_reason
            error = ""
            if not code and not edits:
                error = self._answer_error(parsed, problems)
                if not parsed.candidate and not parsed.wants_context and PROTOCOL_WARNING in parsed.warnings:
                    analysis = "llm-error: response did not follow the protocol: " + parsed.raw.strip()[:300]
                elif parsed.wants_context and not parsed.candidate and not parsed.blocked_reason:
                    analysis = (analysis + "\n" if analysis else "") + \
                        f"(still requesting context after {self.cfg.llm.max_context_rounds} round(s): " + \
                        ", ".join(parsed.need_files + parsed.need_grep) + ")"
            else:
                if parsed.candidate and not code and problems:       # edits kept, the broken script dropped
                    notes.append(f"script dropped: {problems[0][:200]}")
                    no_script = no_script or f"[sbmig] the script could not be used: {problems[0][:200]}"
                if parsed.edits_problem:                             # the script kept, the broken edits dropped
                    notes.append(f"edits dropped: {parsed.edits_problem[:200]}")
            if code and not parsed.described:                       # not an error: the loop describes it from the diff
                notes.append(parsed.changes_note())
            if last and last.finish_reason in ("length", "max_tokens"):
                notes.append(f"output truncated (finish_reason={last.finish_reason})")
            if notes:
                analysis = (analysis + "\n" if analysis else "") + "\n".join(f"[sbmig] {n}" for n in notes)
            proposal = ScriptProposal(
                code=code,
                analysis=analysis,
                solution=parsed.solution,
                edits=edits,
                changes=list(parsed.changes) if (code or edits) else [],
                no_script_reason=no_script if not code else "",
                verify=verify if (code or edits) else "",
                model=last.model if last else (model or ""),
                prompt_hash=messages_hash(messages),
                llm_usage=usage,
                session_id=getattr(last, "session_id", None) if last else None,   # v2.1z-o
                session_model=(model or "") if last else "",                      # v2.1z-x: the model the session runs on
                requested_files=requested,
                raw_response=last.content if last else "",
                error=error,
                # declined / needs_owner 분류는 수정 루프가 하므로 <blocked>는 편집이 있어도 그대로 넘긴다
                blocked_reason=parsed.blocked_reason,
            )
            log.debug("answer for %s: %d edit(s), %d change note(s), script %s, solution %d chars%s%s", cluster.id,
                      len(proposal.edits), len(proposal.changes),
                      "yes" if proposal.code else ("no: " + (proposal.no_script_reason or proposal.error or "-"))[:120],
                      len(proposal.solution), f", 운영자 확인 {len(proposal.verify)} chars" if proposal.verify else "",
                      f", error {proposal.error[:120]}" if proposal.error else "")
            if proposal.blocked_reason:
                log.info("answer for %s: blocked — %s", cluster.id, proposal.blocked_reason[:200])
        except LLMError as exc:
            proposal = self._error_script(f"llm-error: {exc}", f"LLM call failed (status={exc.status})", model,
                                          messages, usage, requested)
        except Exception as exc:  # never raise into the orchestrator
            log.exception("LLM script request failed for cluster %s", cluster.id)
            proposal = self._error_script(f"llm-error: {type(exc).__name__}: {exc}",
                                          f"LLM call failed ({type(exc).__name__})", model, messages, usage, requested)

        self._journal_script(ws, cluster, ctx, mode, proposal, rounds, adv_in_prompt, packed)
        self._record(cluster, transcript, proposal, prefix="script")
        return proposal

    # ------------------------------------------------------------------ VerifyGuideLike (v2.1d)
    def verify_guide(self, ws: Workspace, cluster: ErrorCluster | None, ctx: FixContext | None,
                     changes: list[DependencyChange], diff: str, *, solution: str = "") -> str:
        """운영자가 확인할 사항을 LLM으로 만든다.
        적용된 수정이 메이저 버전을 올렸거나 라이브러리를 바꿨는데 답에 `<verify>`가 없을 때 호출한다.
        LLM을 1회 호출한다(role `verify_guide`). 변경이 없거나, `libraries.verify_guide_llm`이 꺼졌거나,
        호출이 실패하면 빈 문자열을 반환하고 수정 루프가 sbmig 기본 가이드를 쓴다.
        예외를 던지지 않는다. 사용량은 `self.last_verify_usage`에 남는다.

        The 운영자 확인 사항 for an applied fix that raised a dependency's major version / replaced a library and
        whose answer carried no `<verify>` (`VerifyGuideLike`): ONE LLM call (role `verify_guide`) with the project,
        the case's first errors, the changes (approved ones marked), the diff (capped) and the answer's `<solution>`
        when the loop passes it. Plain lines in the report language, at most VERIFY_MAX_CHARS. "" when `changes` is
        empty, `libraries.verify_guide_llm` is off, or on any failure; never raises. The call's usage is
        `self.last_verify_usage` (added to `self.verify_usage`) — the loop charges it to the budget."""
        self.last_verify_usage = LLMUsage()
        if not changes:
            return ""
        libs = getattr(self.cfg, "libraries", None)
        if libs is not None and not getattr(libs, "verify_guide_llm", True):
            return ""
        usage = LLMUsage()
        transcript: list[dict] = []
        messages: list[ChatMessage] = []
        model: str | None = None
        text, error = "", ""
        cid = cluster.id if cluster is not None else "review"
        case_id = (ctx.case_id if ctx is not None else "") or (cluster.case_id if cluster is not None else "") or ""
        try:
            model = self._model(ctx) if ctx is not None else (None if _unset(self.cfg.llm.model) else self.cfg.llm.model)
            _, meta = self.call_tags(ctx, cid, ws)
            meta["purpose"] = VERIFY_ROLE
            if case_id:
                meta["case_id"] = case_id
            messages = self.build_verify_messages(cluster, ctx, changes, diff, solution=solution)
            log.info("운영자 확인 가이드 요청 (%s): %d change(s) %s", case_id or cid, len(changes),
                     ", ".join(c.text for c in changes[:3])[:200])
            _, ans = self._call(messages, model, usage, transcript, role=VERIFY_ROLE, metadata=meta,
                                parse=parse_verify_answer)
            text = ans.text
            if not text:
                error = "; ".join(ans.warnings) or "empty answer"
        except LLMError as exc:
            error = f"llm-error: {exc}"
        except Exception as exc:  # never raise into the loop
            log.debug("verify guide request failed for %s", cid, exc_info=True)
            error = f"llm-error: {type(exc).__name__}: {exc}"
        if error:
            log.warning("운영자 확인 가이드: LLM 응답 없음 (%s) — %s", case_id or cid, error[:300])
        else:
            log.info("운영자 확인 가이드 (%s): %d line(s), %d chars", case_id or cid, text.count("\n") + 1, len(text))
        self.last_verify_usage = usage
        self.verify_usage = self.verify_usage.add(usage)
        try:
            ws.journal({"event": "llm_verify_guide", "cluster_id": cid, "case_id": case_id or None,
                        "model": usage.model or model or "", "calls": usage.calls,
                        "tokens": {"input": usage.input_tokens, "output": usage.output_tokens},
                        "prompt_hash": messages_hash(messages) if messages else "",
                        "changes": [c.text for c in changes[:VERIFY_MAX_CHANGES]], "chars": len(text),
                        "error": error or None})
        except Exception:  # pragma: no cover
            log.debug("journal failed", exc_info=True)
        self._record_json("verify", cid, messages_hash(messages) if messages else "",
                          {"cluster_id": cid, "case_id": case_id, "calls": transcript, "guide": text,
                           "error": error, "changes": [c.model_dump(mode="json") for c in changes]})
        return text

    def build_verify_messages(self, cluster: ErrorCluster | None, ctx: FixContext | None,
                              changes: list[DependencyChange], diff: str, *, solution: str = "") -> list[ChatMessage]:
        """verify_guide 요청의 (system, user) 메시지를 만든다.
        프로젝트, 케이스 오류, 의존성 변경, 해결 방법, 적용된 diff를 넣고 `<verify>`로 답하게 한다.

        (system, user) of a `verify_guide` request."""
        lang = describe_language(self.cfg)
        system = load_verify_prompt(self.cfg.llm.prompt_version, target_text(self.cfg)).replace("{{ language }}", lang)
        # 이 안내는 수정을 적용한 직후 재빌드 전에 만들어지므로 빌드가 통과했다고 쓰면 안 된다
        parts = [("A fix applied to this project raised a dependency's major version or replaced a library. The "
                  "build has not been re-run yet; the runtime is unverified. Write the 운영자 확인 사항 for the "
                  "operator.")]
        if ctx is not None and ctx.profile_summary:
            parts.append(f"## Project\n{asis_line(with_target(ctx.profile_summary, self.cfg))}")
        if cluster is not None:
            errors = dedupe_errors(cluster.errors)
            case_id = (ctx.case_id if ctx is not None else "") or cluster.case_id or cluster.id
            head = f"## Case {case_id} ({cluster.kind.value}, {len(errors)} error(s))"
            lines = [format_error_line(e, 240) for e in errors[:VERIFY_MAX_ERRORS]]
            if len(errors) > VERIFY_MAX_ERRORS:
                lines.append(f"… {len(errors) - VERIFY_MAX_ERRORS} more error(s)")
            parts.append(head + ("\n" + "\n".join(lines) if lines else ""))
        ch = [_change_line(c) for c in changes[:VERIFY_MAX_CHANGES]]
        if len(changes) > VERIFY_MAX_CHANGES:
            ch.append(f"… {len(changes) - VERIFY_MAX_CHANGES} more change(s)")
        parts.append("## Dependency changes (read from the applied pom diff)\n" + "\n".join(ch))
        if solution.strip():
            sol = solution.strip()
            if len(sol) > VERIFY_MAX_SOLUTION_CHARS:
                sol = sol[:VERIFY_MAX_SOLUTION_CHARS].rstrip() + " …"
            parts.append(f"## What the fix says it does (해결 방법)\n{sol}")
        d = (diff or "").replace("\r\n", "\n").strip("\n")
        if d:
            if len(d) > VERIFY_MAX_DIFF_CHARS:
                d = d[:VERIFY_MAX_DIFF_CHARS].rstrip("\n") + f"\n… diff cut ({len(d) - VERIFY_MAX_DIFF_CHARS} more chars)"
            f = fence_for(d)
            parts.append(f"## Applied diff\n{f}diff\n{d}\n{f}")
        parts.append(f"Answer with the 운영자 확인 lines only, inside <verify>…</verify>, written in {lang}.")
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content="\n\n".join(parts) + "\n")]

    def script_prompts(self) -> tuple[str, jinja2.Template]:
        """스크립트 요청의 (system, user 템플릿)을 반환한다.
        처음 쓸 때 `llm.prompt_version`으로 읽고, 그 버전이 없으면 v1을 쓴다.

        (system, user template) of the script requests — loaded on first use (`llm.prompt_version`, falling back
        to v1 when the version has no script prompts)."""
        if self._script_prompts is None:
            extra = tuple(self.cfg.script.extra_allowed_imports)
            timeout = self.cfg.script.timeout_seconds
            try:
                self._script_prompts = load_script_prompts(self.cfg.llm.prompt_version, extra, timeout,
                                                           target_text(self.cfg))
            except FileNotFoundError:
                log.warning("no script prompts for llm.prompt_version=%s; using v1", self.cfg.llm.prompt_version)
                self._script_prompts = load_script_prompts("v1", extra, timeout, target_text(self.cfg))
        return self._script_prompts

    # 게이트웨이 세션이 system 프롬프트와 이전 턴을 기억하므로 보강 턴에는 바뀐 것(기준 스크립트의 결과,
    # 남은 오류와 소스, pom과 버전 정보)만 보낸다
    # 기준 코드, 규칙, 힌트, 노트는 이미 세션에 있다
    THREAD_TURN_KEYS = ("base_result", "errors", "file_index", "files", "related_files", "pom", "library_hints")

    def _thread_on(self) -> bool:
        """``llm.case_thread``가 켜져 있는지 확인한다.
        켜져 있으면 한 케이스의 보강 요청들이 게이트웨이 세션 하나를 이어서 쓴다."""
        return bool(getattr(self.cfg.llm, "case_thread", False))

    def build_thread_turn(self, packed: PackedContext, base: ScriptBase) -> list[ChatMessage]:
        """케이스 대화를 이어 가는 보강 요청(user 메시지 하나)을 만든다.
        같은 대화임을 밝히고 ``THREAD_TURN_KEYS`` 섹션만 붙여 완전한 새 스크립트를 요청한다.
        system 프롬프트, 케이스 머리말, 규칙, 힌트, 노트, 기준 코드는 세션이 기억하므로 보내지 않는다.

        The augment turn of a case thread: one user message — "same conversation", then only what changed (the base
        script's result, the errors now, their source windows, the poms, the version facts) and the request for the
        COMPLETE new script in the same output format. No system prompt, no case header / rules / hints / base code."""
        keys = list(getattr(packed, "keys", None) or [])
        pairs = ([(t, b) for k, (t, b) in zip(keys, packed.sections) if k in self.THREAD_TURN_KEYS] if keys
                 else list(packed.sections))
        label = f"version {base.version}" if base.version else "this run's script"
        kept = base.outcome in ("improved", "build_ok")
        parts = [f"Same conversation, next turn for this case: your {label} ran here and "
                 + ("its changes are STILL in the tree (it fixed what it targeted; the errors below appeared after it)."
                    if kept else "was rolled back (the tree is as it was before it)."),
                 "Only what changed follows — the case facts, the company rules, the hints and your earlier scripts are in "
                 "this conversation already.", ""]
        for t, b in pairs:
            parts += [f"## {t}", b, ""]
        if packed.truncated:
            parts.append(f"(Context was shortened to fit the budget: {', '.join(packed.truncated)}. Ask with <need_files> / "
                         "<need_grep> if you need more.)")
        parts.append("Answer now with the COMPLETE new script in the same output format as before (<analysis>, <solution>, "
                     "<verify> only after a major upgrade, <changes>, then <script> defining `def fix(p)`): it replaces "
                     + label + (" and must also be a no-op for what is already applied" if kept
                                else " and runs on a tree WITHOUT its changes") + ". Keep what worked, fix what the result "
                     "shows. If no reusable script can fix this case, answer <no_script> with the reason and <edits> that fix "
                     "this project directly.")
        return [ChatMessage(role="user", content="\n".join(parts).strip() + "\n")]

    def _follow_up(self, messages: list[ChatMessage], last: LLMResponse, text: str, meta: dict) -> list[ChatMessage]:
        """문맥 라운드나 피드백 라운드에 보낼 다음 메시지 목록을 만든다.
        케이스 대화가 켜져 있고 직전 답에 세션 id가 있으면 새 user 메시지 하나만 보낸다.
        그렇지 않으면 전체 대화를 다시 보낸다.

        The next round's messages: in a session (thread on, the last answer carried a session id) only the new user
        message, continuing that session; otherwise the stateless chain (the assistant's answer + the user message)."""
        sid = getattr(last, "session_id", None)
        if self._thread_on() and sid:
            meta["session_id"] = str(sid)
            return [ChatMessage(role="user", content=text)]
        meta.pop("session_id", None)
        return messages + [ChatMessage(role="assistant", content=last.content), ChatMessage(role="user", content=text)]

    def build_script_messages(self, packed: PackedContext, mode: str = "new", base: Optional[ScriptBase] = None,
                              *, system: Optional[str] = None,
                              user_template: Optional[jinja2.Template] = None) -> list[ChatMessage]:
        """스크립트 요청 메시지 (system, user)를 만든다.
        섹션, 줄인 섹션, 모드, 보강할 base 정보, 설명 언어로 user 템플릿을 채운다."""
        if system is None or user_template is None:
            system, user_template = self.script_prompts()
        label = (f"version {base.version}" if base.version else "this run's script") if base is not None else ""
        # the loop rolls a base back before the next try unless it was improved / build_ok (then it stays in the tree
        # and the case's errors are ones javac showed after it)
        kept = base is not None and base.outcome in ("improved", "build_ok")
        user = user_template.render(sections=packed.sections, truncated=packed.truncated, mode=mode, base_label=label,
                                    base_kept=kept, desc_language=describe_language(self.cfg))
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user.strip() + "\n")]

    def _static_problems(self, code: str) -> list[str]:
        """정적 검사로 스크립트의 문제를 찾는다.
        실행기가 실행 전에 다시 검사하므로 검사기가 고장 나도 수정을 막지 않는다."""
        checker = self.script_checker if self.script_checker is not None else default_script_checker(self.cfg)
        if checker is None:
            return []
        try:
            return [str(p) for p in (checker(code) or [])]
        except Exception as exc:          # a broken checker must not block fixing; the runner checks again
            log.warning("static script check failed (%s: %s); leaving the check to the runner", type(exc).__name__, exc)
            return []

    def _script_problems(self, parsed: ParsedScript, base: Optional[ScriptBase]) -> list[str]:
        """답의 스크립트를 쓸 수 없는 이유 목록을 반환한다.
        빈 목록이면 쓸 수 있거나, 편집이나 <no_script> 이유가 있어 스크립트가 없는 것이 의도된 경우다.
        보강 답이 원본과 같은 코드여도 문제로 본다.

        Why the parsed answer's script is not usable ([] = usable, or no script at all next to valid edits /
        a <no_script> reason)."""
        if parsed.code:
            problems = self._static_problems(parsed.code)
            if not problems and base is not None and _same_code(parsed.code, base.code):
                label = f"version {base.version}" if base.version else "this run's script"
                problems = [(f"the script is identical to the version it should extend ({label}), which did not "
                             "fix the case — change what its result shows")]
            return problems
        if parsed.candidate:
            return [parsed.problem]
        if parsed.used_patches:
            return ["the answer used SEARCH/REPLACE <patches>; this loop takes <edits> (a JSON list) and ONE Python "
                    "fix script in <script>"]
        if parsed.edits or parsed.no_script_reason:
            return []                                     # no script, on purpose or with edits: not a script problem
        return [parsed.problem or NO_SCRIPT]

    @staticmethod
    def _feedback_problems(parsed: ParsedScript, problems: list[str]) -> list[str]:
        """피드백에 넣을 문제(읽을 수 없는 편집과 스크립트의 문제)를 모은다.

        What the feedback round names: the unreadable edits and / or the script's problems."""
        out = [parsed.edits_problem] if parsed.edits_problem else []
        if parsed.candidate or parsed.used_patches:
            out += problems
        return out or problems or [NO_SCRIPT]

    @staticmethod
    def _wants_feedback(parsed: ParsedScript, problems: list[str]) -> bool:
        """피드백을 한 번 보낼지 정한다.
        스크립트를 쓸 수 없거나, patches 형식 답이거나, 편집 블록을 읽을 수 없을 때 보낸다.
        수정이 아예 없는 답은 모델이 판단한 결과이므로 피드백하지 않는다.

        One feedback round when a script block is there but unusable, the answer used v1 patches, or the edits
        block cannot be read. An answer without any fix (only an analysis / a <no_script> without edits) is the
        model's decision: no feedback round."""
        return bool(parsed.edits_problem) or bool(problems and (parsed.candidate or parsed.used_patches))

    def _answer_error(self, parsed: ParsedScript, problems: list[str]) -> str:
        """쓸 수 있는 스크립트도 유효한 편집도 없는 답에 대해 짧은 이유를 만든다(ScriptProposal.error).

        The short reason of an answer with neither a usable script nor valid edits (ScriptProposal.error)."""
        if parsed.blocked_reason and not parsed.candidate and not parsed.edits_problem:
            return f"blocked: {parsed.blocked_reason[:200]}"
        err = self._script_error(parsed, problems)
        if parsed.edits_problem:
            return parsed.edits_problem if err == NO_SCRIPT and not parsed.candidate else \
                f"{err}; {parsed.edits_problem}"
        if parsed.no_script_reason and not parsed.candidate and err == NO_SCRIPT:
            return f"no script and no edits: {parsed.no_script_reason[:200]}"
        return err

    def _script_error(self, parsed: ParsedScript, problems: list[str]) -> str:
        """스크립트가 없는 제안에 대해 짧은 이유를 만든다.

        The short reason of a proposal without a script."""
        if parsed.code and problems:
            if problems[0].startswith("the script is identical"):
                return problems[0]
            return "script rejected by the static check: " + "; ".join(problems[:5])
        if parsed.candidate:
            return parsed.problem
        if parsed.wants_context:
            return f"still requesting context after {self.cfg.llm.max_context_rounds} round(s)"
        if parsed.used_patches:
            return NO_SCRIPT + " (the answer used the v1 <patches> format)"
        return NO_SCRIPT

    def script_feedback_message(self, parsed: ParsedScript, problems: list[str], mode: str = "new") -> str:
        """피드백 user 메시지를 만든다.
        문제 목록(최대 10개), 형식 안내, 모드에 맞는 완전한 답을 다시 보내라는 요청을 담는다."""
        parts = ["Your answer cannot be used. Problems:"]
        parts += [f"- {p}" for p in problems[:10]]
        if len(problems) > 10:
            parts.append(f"- … and {len(problems) - 10} more problem(s)")
        if parsed.used_patches and not parsed.candidate:
            parts.append("This loop does not take SEARCH/REPLACE patches: give the changes as <edits> (a JSON list of "
                         "{\"op\": \"replace\", \"path\": …, \"old\": …, \"new\": …} objects) and ONE Python fix script "
                         "that makes the same edits through the helper API `p` (p.replace / p.sub / p.pom …) for "
                         "every error in p.errors.")
        if parsed.edits_problem:
            parts.append("<edits> must be ONE valid JSON list: double-quoted keys and strings, `\\n` and `\\\"` "
                         "escaped inside strings, no comments, no trailing commas; `op` one of replace / write / "
                         "create / delete / move / pom; paths relative to the project root.")
        what = ("<analysis>, <solution>, <edits> (each with its \"desc\"), <changes> and <script> with a module-level "
                "`def fix(p)` (or <no_script> with the reason)") if mode != "augment" else \
            ("<analysis>, <solution>, <changes> and the COMPLETE new <script> with a module-level `def fix(p)` — or "
             "<no_script> with the reason and <edits> (each with its \"desc\")")
        parts.append(f"Re-send the COMPLETE corrected answer: {what}, plus <verify> after a major version upgrade "
                     "(rule 14). Use only the helper API `p` and the allowed imports in the script (no open / os / sys / "
                     "subprocess / eval / exec / __dunder__ access).")
        return "\n\n".join(parts) + "\n"

    def _error_script(self, analysis: str, error: str, model: Optional[str], messages: list[ChatMessage],
                      usage: LLMUsage, requested: list[str]) -> ScriptProposal:
        """실패한 요청에 대한 빈 제안을 만든다.
        analysis가 `llm-error: …`로 시작하므로 수정 루프가 전송 장애로 분류한다."""
        log.warning("%s", analysis[:300])
        return ScriptProposal(code="", analysis=analysis[:1000], model=model or "",
                              prompt_hash=messages_hash(messages) if messages else "", llm_usage=usage,
                              requested_files=requested, error=error[:300])

    def _journal_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, mode: str, p: ScriptProposal,
                        rounds: int, advisory_hints: int, packed: Optional[PackedContext]) -> None:
        """`llm_script` 저널 이벤트를 남긴다. 저널 기록이 실패해도 무시한다."""
        try:
            ws.journal({"event": "llm_script", "cluster_id": cluster.id,
                        "case_id": ctx.case_id or cluster.case_id or None, "mode": mode,
                        "base_version": ctx.script_base.version if mode == "augment" and ctx.script_base else None,
                        "model": p.model, "rounds": rounds, "calls": p.llm_usage.calls,
                        "tokens": {"input": p.llm_usage.input_tokens, "output": p.llm_usage.output_tokens},
                        "prompt_hash": p.prompt_hash, "code_chars": len(p.code), "edits": len(p.edits),
                        "solution_chars": len(p.solution), "no_script": bool(p.no_script_reason),
                        "verify_chars": len(p.verify),
                        "files_shown": len(packed.files_included) if packed else 0,
                        "files_listed": len(packed.files_listed) if packed else 0,
                        "error": p.error or None,
                        "blocked_reason": p.blocked_reason[:300] or None,
                        "llm_error": p.analysis if p.analysis.startswith("llm-error") else None,
                        "advisory_hints": advisory_hints})
        except Exception:  # pragma: no cover
            log.debug("journal failed", exc_info=True)

    # ------------------------------------------------------------------ building blocks
    def _model(self, ctx: FixContext) -> Optional[str]:
        """이번 호출에 쓸 모델을 정한다. `ctx.use_escalation_model`이 켜지고 `llm.escalation_model`이
        설정됐으면 그 모델을, 아니면 `llm.model`을 쓴다.
        `llm.model`도 없으면 None을 반환하고 게이트웨이 기본 모델을 쓴다."""
        llm = self.cfg.llm
        if ctx.use_escalation_model and not _unset(llm.escalation_model):
            return llm.escalation_model
        return None if _unset(llm.model) else llm.model

    @staticmethod
    def call_tags(ctx: Optional[FixContext], cluster_id: Optional[str] = None,
                  ws: Optional[Workspace] = None) -> tuple[str, dict]:
        """propose_script 한 번의 모든 LLM 호출에 붙일 (role, metadata)를 만든다.
        게이트웨이가 목적별로 라우팅하고 예산을 관리할 수 있게 하기 위해서다.
        role은 fix 또는 fix_escalation이고, metadata는 run_id, project,
        phase, iteration, cluster_id이며 비밀 값은 넣지 않는다.

        (role, metadata) sent with every LLM call of one propose_script() — lets a gateway route/budget by purpose.
        Round 10: `run_id` and `project` (the workspace's run id / project directory name) as the LLMRequest contract
        says (`sbmig.core.interfaces.LLMRequest.metadata`); never secrets."""
        meta: dict = {}
        if ws is not None:
            run_id = getattr(ws, "run_id", None)
            root = getattr(ws, "root", None)
            if run_id:
                meta["run_id"] = str(run_id)
            if root is not None and getattr(root, "name", ""):
                meta["project"] = root.name
        if ctx is None:
            if cluster_id:
                meta["cluster_id"] = cluster_id
            return "fix", meta
        meta.update({"phase": ctx.phase.value, "iteration": ctx.iteration})
        if cluster_id:
            meta["cluster_id"] = cluster_id
        return ("fix_escalation" if ctx.use_escalation_model else "fix"), meta

    def _call(self, messages: list[ChatMessage], model: Optional[str], usage: LLMUsage,
              transcript: list[dict], *, role: str = "fix", metadata: Optional[dict] = None,
              parse: Callable[[str], Any] = parse_script_response) -> tuple[LLMResponse, Any]:
        """LLM을 한 번 호출하고 답을 파싱한다. watchdog 표시, 사용량 누적, transcript 추가도 함께 한다.
        LLMError 같은 예외는 호출한 쪽으로 그대로 던진다."""
        with llm_call_in_flight():          # the loop's watchdog stays silent while the call heartbeats (round 10)
            resp = self.client.complete(messages, model=model, max_tokens=self.cfg.llm.max_output_tokens,
                                        temperature=self.cfg.llm.temperature, role=role,
                                        metadata=dict(metadata) if metadata else None)
        u = usage_from(resp)
        usage.model = usage.model or u.model
        usage.calls += 1
        usage.input_tokens += u.input_tokens
        usage.output_tokens += u.output_tokens
        parsed = parse(resp.content)
        sent = sum(len(m.content) for m in messages)
        in_session = bool(metadata and metadata.get("session_id"))
        if in_session:                       # v2.1z-p: the measurement the company lacks a document for (history re-billed?)
            log.info("thread turn (session %s): sent %d chars in %d message(s) → prompt_tokens=%d", metadata["session_id"],
                     sent, len(messages), u.input_tokens)
        transcript.append({"prompt_hash": messages_hash(messages), "response": resp.content,
                           "usage": u.model_dump(), "warnings": parsed.warnings,
                           "session_id": getattr(resp, "session_id", None),      # v2.1z-o: the gateway's session
                           "sent_chars": sent, "in_session": in_session})          # v2.1z-p: the thread measurement
        if parsed.warnings:
            log.debug("protocol warnings: %s", parsed.warnings)
        return resp, parsed

    # ------------------------------------------------------------------ context serving
    def resolve_path(self, ws: Workspace, want: str) -> tuple[Optional[str], list[str]]:
        """LLM이 요청한 경로(부분 경로나 FQCN일 수 있음)를 워크스페이스 파일 경로로 바꾼다.
        (경로, 후보 목록)을 반환하고, 하나로 정해지지 않으면 (None, 최대 8개의 후보)를 반환한다.

        Map a requested path (maybe partial, or a FQCN) to a workspace file. Returns (path, candidates)."""
        if ws.exists(want) and ws.abs(want).is_file():
            return want.replace("\\", "/"), []
        want = want.replace("\\", "/")
        cands: list[str] = []
        suffixes = [want]
        if re.fullmatch(r"[\w$]+(\.[\w$]+)+", want) and not re.search(r"\.(java|xml|properties|ya?ml|kt|jsp)$", want):
            suffixes.append(want.replace(".", "/") + ".java")
        # posix forms: on Windows the workspace globs may come back with backslashes (round 10)
        files = [f.replace("\\", "/") for f in ws.java_files() + ws.pom_files()] + self._resource_files(ws)
        for s in suffixes:
            cands = [f for f in files if f == s or f.endswith("/" + s)]
            if cands:
                break
        if not cands:
            base = PurePosixPath(want).name
            cands = [f for f in files if PurePosixPath(f).name == base]
        if len(cands) == 1:
            return cands[0], []
        return None, cands[:8]

    @staticmethod
    def _resource_files(ws: Workspace) -> list[str]:
        """src/ 아래 리소스 파일(properties / yml / yaml / xml / factories / imports, pom 제외) 목록을 반환한다."""
        out: list[str] = []
        for ext in ("properties", "yml", "yaml", "xml", "factories", "imports"):
            for f in ws.glob(f"*.{ext}"):
                f = f.replace("\\", "/")
                if f.startswith("src/") or "/src/" in f:
                    if PurePosixPath(f).name != "pom.xml":
                        out.append(f)
        return sorted(set(out))

    def serve_context(self, ws: Workspace, need_files: list[str], need_grep: list[str], remaining_rounds: int) -> str:
        """`<need_files>` / `<need_grep>` 요청에 답하는 user 메시지를 만든다.
        파일은 라운드당 MAX_SERVED_FILES개까지 보내고 길면 앞뒤만 보낸다.
        grep 결과와 남은 라운드 안내를 붙이고, 마지막 라운드면 지금 답하라고 쓴다."""
        parts: list[str] = ["Here is the requested context."]
        for want in need_files[:MAX_SERVED_FILES]:
            path, cands = self.resolve_path(ws, want)
            if not path:
                if cands:
                    parts.append(f"// {want}: not found exactly; candidates: {', '.join(cands)}")
                else:
                    parts.append(f"// {want}: file not found in the project")
                continue
            text = ws.read(path).replace("\r\n", "\n")
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]
            lang = lang_for(path)
            if len(lines) <= MAX_SERVED_LINES:
                body = "\n".join(lines)
                f = fence_for(body)
                parts.append(f"// {path} lines 1-{len(lines)} (whole file)\n{f}{lang}\n{body}\n{f}")
            else:
                head = "\n".join(lines[:SERVED_HEAD_LINES])
                tail_start = len(lines) - SERVED_TAIL_LINES + 1
                tail = "\n".join(lines[tail_start - 1:])
                f = fence_for(head + tail)
                parts.append(
                    f"// {path} lines 1-{SERVED_HEAD_LINES} (of {len(lines)})\n{f}{lang}\n{head}\n{f}\n"
                    f"// … lines {SERVED_HEAD_LINES + 1}-{tail_start - 1} omitted (use <need_grep> to locate code there)\n"
                    f"// {path} lines {tail_start}-{len(lines)}\n{f}{lang}\n{tail}\n{f}")
        if len(need_files) > MAX_SERVED_FILES:
            parts.append(f"(only the first {MAX_SERVED_FILES} files are served per round; skipped: "
                         + ", ".join(need_files[MAX_SERVED_FILES:]) + ")")
        if need_grep:
            parts.append(self.grep(ws, need_grep))
        if remaining_rounds <= 0:
            parts.append("This was the LAST context round. Answer now with <analysis>, <solution>, <edits>, <changes> "
                         "and <script> (or <no_script> with the reason and <edits>; or <analysis> and <blocked> when "
                         "the blocker is outside the project).")
        else:
            parts.append(f"You may ask for more context {remaining_rounds} more time(s), but prefer answering now "
                         "(<analysis>, <solution>, <edits>, <changes>, <script>).")
        return "\n\n".join(parts) + "\n"

    def grep(self, ws: Workspace, patterns: list[str]) -> str:
        """LLM의 `<need_grep>` 요청을 처리한다.
        java / pom / 리소스 파일을 한 번씩 읽으며 최대 5개 패턴을 찾는다.
        Windows 실시간 백신 환경에서는 느리므로 큰 파일은 건너뛰고 MAX_GREP_FILES개 또는 GREP_BUDGET_S초에서
        멈춘다. 잘못된 정규식은 문자 그대로 찾는다.

        `<need_grep>` from the LLM: one pass over the project's java / pom / resource files (each read once, big
        files skipped, bounded by MAX_GREP_FILES and GREP_BUDGET_S) for up to 5 patterns."""
        t0 = time.monotonic()
        files = [f.replace("\\", "/") for f in ws.java_files() + ws.pom_files()] + self._resource_files(ws)
        skipped_count = max(0, len(files) - MAX_GREP_FILES)
        files = files[:MAX_GREP_FILES]
        rxs: list[tuple[str, re.Pattern[str]]] = []
        for pat in patterns[:5]:
            try:
                rxs.append((pat, re.compile(pat)))
            except re.error:
                rxs.append((pat, re.compile(re.escape(pat))))
        log.info("grep for the LLM: %d pattern(s) over %d file(s)%s", len(rxs), len(files),
                 f" ({skipped_count} more not scanned)" if skipped_count else "")
        hits: dict[str, list[str]] = {pat: [] for pat, _ in rxs}
        counts: dict[str, int] = {pat: 0 for pat, _ in rxs}
        total = 0
        scanned = 0
        budget_hit = False
        for f in files:
            if time.monotonic() - t0 > GREP_BUDGET_S:
                budget_hit = True
                break
            try:
                if ws.abs(f).stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                text = ws.read(f)
            except OSError:
                continue
            scanned += 1
            lines: Optional[list[str]] = None
            for pat, rx in rxs:
                if not rx.search(text):
                    continue
                if lines is None:
                    lines = text.splitlines()
                for i, line in enumerate(lines, 1):
                    if rx.search(line):
                        counts[pat] += 1
                        if total < MAX_GREP_LINES:
                            hits[pat].append(f"{f}:{i}: {line.strip()[:200]}")
                            total += 1
        out: list[str] = []
        for pat, _ in rxs:
            head = f"grep /{pat}/ → {counts[pat]} matching line(s)" + \
                (f" (showing {len(hits[pat])})" if counts[pat] > len(hits[pat]) else "")
            out.append(head + ("\n" + "\n".join(hits[pat]) if hits[pat] else ""))
        if budget_hit or skipped_count:
            out.append(f"(grep stopped after {scanned} of {len(files) + skipped_count} file(s)"
                       f"{' — time budget' if budget_hit else ''}; ask for specific files instead)")
        log.info("grep for the LLM done: %d file(s) scanned in %.1fs, %d line(s) returned%s", scanned,
                 time.monotonic() - t0, total, " (budget hit)" if budget_hit else "")
        return "\n\n".join(out)

    # ------------------------------------------------------------------ bookkeeping
    def _record(self, cluster: ErrorCluster, transcript: list[dict], p: ScriptProposal,
                prefix: str = "script") -> None:
        """스크립트 요청 하나를 기록 파일(`script-<cluster>-<hash>.json`)로 남긴다.
        호출 transcript와 제안을 담는다."""
        self._record_json(prefix, cluster.id, p.prompt_hash, {"cluster_id": cluster.id, "calls": transcript,
                                                              "proposal": p.model_dump(mode="json")})

    def _record_json(self, prefix: str, cluster_id: str, prompt_hash: str, payload: dict) -> None:
        """`<llm.record_dir>/<prefix>-<cluster>-<hash>.json` 파일을 쓴다.
        record dir이 없거나 클라이언트가 RecordingClient면 아무것도 하지 않는다.
        실패해도 수정은 계속한다.

        `<llm.record_dir>/<prefix>-<cluster>-<hash>.json` (nothing when no record dir or the client records)."""
        rd = self.cfg.llm.record_dir
        if not rd or isinstance(self.client, RecordingClient):
            return
        try:
            d = self.cfg.resolve_path(rd)
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", cluster_id)[:80]
            path = d / f"{prefix}-{safe}-{(prompt_hash or 'none')[:12]}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, TypeError, ValueError):  # pragma: no cover
            log.debug("record failed", exc_info=True)


__all__ = ["LLMFixer", "profile_summary", "load_script_prompts", "default_script_checker", "describe_language",
           "script_allowed_imports", "script_helper_api_doc", "normalize_path", "load_verify_prompt",
           "parse_verify_answer", "VerifyAnswer", "VERIFY_ROLE"]
