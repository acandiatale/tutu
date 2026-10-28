"""LLMFixer — 2단 접점(`ScriptFixerLike.propose_script`)의 기본 구현: 문맥 꾸리기 → LLM → 답변 파싱 → 정적
사전 검사 / 피드백 1회 → FixAnswer(= ScriptProposal). 흐름상 위치: 수정 루프가 케이스마다 사다리에서 LLM 이
필요할 때(보강 / 신규) `propose_script` 를 부르고, 메이저 업그레이드·라이브러리 교체가 `<verify>` 없이
적용됐을 때 `verify_guide` 를 부른다. 답변(v2.1): `<analysis>` 원인, `<solution>` 해결 방법, `<edits>`
수정(루프가 `sbmig.script.apply_edits` 로 바로 적용), `<script>` 재사용 스크립트(같은 케이스의 다른
프로젝트용으로 KB 에 저장) 또는 `<no_script>` + 이유, `<changes>` 현업이 읽는 변경 설명, v2.1d `<verify>`
운영자 확인 사항(메이저 업그레이드 뒤 필수, 규칙 14). 신규는 편집 + 스크립트를, 보강은 더 나은 완전한
스크립트(또는 no_script + 편집)를 요청한다. LLM 호출 한도: propose_script 한 번 = 첫 질문 1회 + LLM 이
파일·grep 을 더 달라면(`<need_files>` / `<need_grep>`) 최대 `llm.max_context_rounds`(기본 2)회 + 쓸 수 없는
스크립트·못 읽는 편집이면 피드백 1회. 케이스당 몇 번 부르는지는 루프가 정한다(한 실행에서 저장본 1회 + LLM
시도 3회, `script.max_augments_per_case`). verify_guide 는 그 시도당 1회. 지킬 것: fixer 는 작업 트리를 고치지
않는다 — 적용·커밋·재빌드 채점·되돌리기는 루프 몫이다(예전 사내 에이전트가 빌드를 돌리지 않고 "끝" 이라고 한
실패 때문에 모든 판정은 재빌드로 한다). 절대 예외를 내지
않는다(전송 오류는 루프가 분류하는 `llm-error: …` 분석 문구가 된다).

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
from .context import ContextPacker, PackedContext, dedupe_errors, fence_for, format_error_line, lang_for
from .script_protocol import NO_SCRIPT, PROTOCOL_WARNING, ParsedScript, normalize_path, parse_script_response

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
_FIX_TAG_RE = re.compile(r"<\s*(?:analysis|solution|edits|changes|no_script|script|need_files?|need_grep|review)\s*>",
                         re.IGNORECASE)
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)


def _script_module_attr(name: str) -> Any:
    """`sbmig.script` 의 속성(HELPER_API_DOC, ALLOWED_IMPORTS, check_script). 모듈이 없거나 깨졌으면 None —
    그러면 fixer 가 가진 계약 사본을 쓴다.

    An attribute of agent S's `sbmig.script` (HELPER_API_DOC, ALLOWED_IMPORTS, check_script), or None when the
    module is missing or broken — the fixer then falls back to its own copy of the contract."""
    try:
        import sbmig.script as script_mod
    except Exception:            # ImportError, or a half-written module during development
        return None
    return getattr(script_mod, name, None)


def script_allowed_imports(extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    """스크립트의 import 허용 목록: `sbmig.script.ALLOWED_IMPORTS`(없으면 계약 사본) +
    `extra`(`script.extra_allowed_imports`)."""
    base = _script_module_attr("ALLOWED_IMPORTS") or DEFAULT_SCRIPT_IMPORTS
    out = [str(m) for m in base]
    out += [str(m) for m in extra if str(m) not in out]
    return out


def script_helper_api_doc(extra_imports: tuple[str, ...] = (), timeout_s: Optional[int] = None) -> str:
    """스크립트 프롬프트의 헬퍼 API 표(`sbmig.script.helper_api_doc(...)`, 없으면 HELPER_API_DOC). 못 구하면
    "" — 프롬프트 파일에 든 자체 표가 쓰인다.

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
                        timeout_s: Optional[int] = None) -> tuple[str, jinja2.Template]:
    """수정 스크립트 요청의 (system 프롬프트, user 템플릿): `prompts/script_system_<version>.md` +
    `script_user_<version>.md.j2`. 헬퍼 API 표와 허용 import 를
    채운다(둘 다 INTERFACES.md "Script contract (S)" 표를 따른다).

    (system prompt, user template) of a fix-script request. The helper API block comes from `sbmig.script`
    (`helper_api_doc`) when that module provides it, else from the table kept in `script_system_<version>.md` (both
    mirror the INTERFACES.md "Script contract (S)" table)."""
    sys_path = PROMPTS_DIR / f"script_system_{version}.md"
    user_path = PROMPTS_DIR / f"script_user_{version}.md.j2"
    if not sys_path.exists() or not user_path.exists():
        raise FileNotFoundError(f"script prompt version {version!r} not found in {PROMPTS_DIR}")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True, autoescape=False)
    system = env.from_string(sys_path.read_text(encoding="utf-8")).render(
        helper_api=script_helper_api_doc(extra_imports, timeout_s),
        allowed_imports=", ".join(f"`{m}`" for m in script_allowed_imports(extra_imports)))
    user_t = env.from_string(user_path.read_text(encoding="utf-8"))
    return system.strip() + "\n", user_t


def default_script_checker(cfg: Config) -> Optional[Callable[[str], list[str]]]:
    """config(추가 import, 최대 코드 크기)에 묶은 `sbmig.script.check_script`(없으면 None). 이 검사가 문제를
    찾으면 루프가 저장·실행하기 전에 스크립트가 거절된다(그 전에 피드백 1회).

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


def profile_summary(profile: ProjectProfile) -> str:
    """프롬프트용 프로젝트 한 줄 설명(FixContext.profile_summary): `case=… java=… boot=… libs=… | target: …`.
    ContextPacker 는 여기의 `case=` 로 노트를 고른다.

    One-line project description for prompts (and for FixContext.profile_summary)."""
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
    if profile.libraries:
        libs = [f"{l.name}:{l.version}" if l.version else l.name for l in profile.libraries[:20]]
        parts.append("libs=" + ",".join(libs))
    src = profile.source
    if src.has_web_xml:
        parts.append("web.xml")
    if src.jsp_count:
        parts.append(f"jsp={src.jsp_count}")
    if any(k.startswith("javax.") for k in src.javax_imports):
        parts.append("javax-imports=" + ",".join(sorted(src.javax_imports)[:8]))
    return " ".join(parts) + " | target: JDK 21, Spring Boot 4.1"


def _unset(v: Optional[str]) -> bool:
    return not v or "${" in v or v == "default"



def describe_language(cfg: Optional[Config]) -> str:
    """프롬프트가 요구할 변경 설명 언어(`report.language` 기준; `sbmig.script` 를 못 불러오면 한국어).

    The language of the change descriptions for the prompts (`sbmig.script.notes.describe_language`)."""
    try:
        from sbmig.script.notes import describe_language as _describe
    except Exception:  # noqa: BLE001 - sbmig.script missing / half-built
        return "Korean (한국어)"
    return _describe(cfg)


@lru_cache(maxsize=4)
def load_verify_prompt(version: str = "v1") -> str:
    """verify_guide 요청의 system 프롬프트(`verify_system_<version>.md`,
    없으면 v1). `{{ language }}` 는 호출한 쪽이 채운다.

    The system prompt of a `verify_guide` request (`verify_system_<version>.md`, v1 when the version has none);
    `{{ language }}` is filled by the caller."""
    path = PROMPTS_DIR / f"verify_system_{version}.md"
    if not path.exists():
        path = PROMPTS_DIR / "verify_system_v1.md"
    return path.read_text(encoding="utf-8").strip() + "\n"


@dataclass
class VerifyAnswer:
    """verify_guide 답을 읽은 결과: 가이드 텍스트("" = 쓸 게 없음)와 버린 이유.

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
    """verify_guide 답의 가이드: `<verify>` 블록(닫는 태그 없어도 됨), 없으면 답 전체 — 단 수정
    답(`<analysis>` / `<script>` 등, 예: 재생된 수정 답이나 "no fix")이면 "". 바깥 펜스·markdown 제목 표시를
    지우고 VERIFY_MAX_CHARS 로 자른다.

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
    """공백만 다른 두 스크립트는 같은 코드로 본다 — 보강 답이 원본과 같으면 거절하기 위해.

    Two scripts are the same code when they differ in whitespace only."""
    def norm(t: str) -> list[str]:
        return [ln.rstrip() for ln in t.replace("\r\n", "\n").strip().split("\n") if ln.strip()]
    return norm(a) == norm(b)


class LLMFixer:
    """기본 fixer(ScriptFixerLike): 채팅 완성 하나로 케이스마다 파이썬 수정 스크립트 하나(+ 이 프로젝트용
    편집). 사내 fix.plugin 은 propose_script 를 직접 구현하되
    같은 문맥(`ContextPacker.pack_script`)을 재사용할 수 있다.

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
        # verify_guide 는 텍스트만 돌려주므로 사용량은 여기 남기고, 루프가 예산에 청구한다
        # v2.1d verify_guide: usage of the last call (the loop charges it: the method returns only the text) and of
        # every call of this fixer
        self.last_verify_usage = LLMUsage()
        self.verify_usage = LLMUsage()

    # ------------------------------------------------------------------ ScriptFixerLike
    def propose_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext) -> ScriptProposal:
        """케이스 하나의 답(FixAnswer). `ctx.script_mode` 는 new | augment, `ctx.script_base` 는 보강할 버전.
        순서: 문맥 꾸리기 → 질문 → 문맥 라운드(최대 `llm.max_context_rounds`) → 정적 검사 + 피드백 1회 → 결과
        조립 → 저널·기록. 쓸 수 있음 = `code` 또는 `edits`; 둘 다 없을 때만 `error`. 피드백 뒤에도 못 쓰는
        스크립트는 버리고(편집이 있으면 `no_script_reason` 에 "[sbmig] …"), 못 읽는 편집은 버린다(스크립트가
        있으면 analysis 에 "[sbmig] edits dropped"). 예외를 내지 않는다 — 전송·설정 실패는 루프가 분류하는
        `llm-error: …` 분석 문구가 된다.

        The answer for ONE case (`ctx.script_mode` new | augment, `ctx.script_base` = the version to extend) as a
        FixAnswer: `analysis` 원인, `solution` 해결 방법, `edits` (validated `Edit`s; the loop applies them with
        `sbmig.script.apply_edits`), `code` (the script; "" = none) and `no_script_reason`. Usable = `code` or
        `edits`; `error` is set only when neither is (then `code=""`, `edits=[]`). A script dropped after the
        feedback round next to valid edits → `no_script_reason` "[sbmig] the script could not be used: …"; edits
        dropped next to a valid script → an "[sbmig] edits dropped: …" line in `analysis`. Never raises; transport and
        configuration failures carry the analysis texts the loop classifies with `is_llm_transport_error` /
        `llm_400_body` (`llm-error: …`, `llm-error: response did not follow the protocol: …`)."""
        model = self._model(ctx)
        usage = LLMUsage()
        requested: list[str] = []
        transcript: list[dict] = []
        rounds = 0
        messages: list[ChatMessage] = []
        last: Optional[LLMResponse] = None
        base = ctx.script_base
        # base 가 없으면 신규로 묻는다 — 보강 답은 base 를 대체하는 완전한 스크립트라 base 없이는 뜻이 없다
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
            log.info("script prompt for %s (%s%s): packing %d error(s) (%d advisory hint(s), %d KB hint(s), %d reference "
                     "file(s)%s)", cluster.id, mode, f" of v{base.version}" if mode == "augment" and base else "",
                     len(cluster.errors), len(ctx.advisory_hints), len(ctx.kb_hints), len(ctx.reference_files),
                     f", {len(ctx.library_hints)} dependency version line(s)" if ctx.library_hints else "")
            packed = self.packer.pack_script(ws, cluster, ctx, ctx.profile_summary)
            log.info("script prompt for %s packed: %d chars, %d file(s) shown, %d listed by name, sections [%s]%s → "
                     "calling the LLM", cluster.id, packed.chars, len(packed.files_included), len(packed.files_listed),
                     ", ".join(t[:28] for t, _ in packed.sections),
                     f", truncated {', '.join(packed.truncated)}" if packed.truncated else "")
            guidance = next((b for t, b in packed.sections if t == ContextPacker.ADVISORY_TITLE), "")
            if guidance:
                adv_in_prompt = sum(1 for ln in guidance.splitlines() if ln.startswith("- "))
            messages = self.build_script_messages(packed, mode, base, system=system, user_template=user_t)
            last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta,
                                      parse=parse_script_response)
            verify = parsed.verify                  # v2.1d: the latest <verify> of any round

            # 1) 문맥 라운드: LLM 이 파일·grep 을 더 달라면 내준다(쓸 수 있는 답이 오거나 한도에 닿으면 멈춤)
            # 1) context rounds
            while parsed.wants_context and not parsed.usable and rounds < self.cfg.llm.max_context_rounds:
                rounds += 1
                requested.extend(p for p in parsed.need_files if p not in requested)
                remaining = self.cfg.llm.max_context_rounds - rounds
                log.info("script prompt for %s: the LLM asked for more context (round %d/%d): %d file(s) %s, %d grep(s) "
                         "%s", cluster.id, rounds, self.cfg.llm.max_context_rounds, len(parsed.need_files),
                         parsed.need_files[:4], len(parsed.need_grep), parsed.need_grep[:3])
                served = self.serve_context(ws, parsed.need_files, parsed.need_grep, remaining)
                messages = messages + [ChatMessage(role="assistant", content=last.content),
                                       ChatMessage(role="user", content=served)]
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta,
                                          parse=parse_script_response)
                verify = parsed.verify or verify
            if parsed.wants_context and not parsed.usable:
                requested.extend(p for p in parsed.need_files if p not in requested)

            # 2) 정적 검사 + 피드백 1회: 스크립트를 못 쓰거나 v1 patches 형식이거나 편집 JSON 이 안 읽힐 때
            # 2) static pre-check + one feedback round for a script that cannot be used as written / edits that
            #    cannot be read
            notes: list[str] = []
            sbase = base if mode == "augment" else None
            problems = self._script_problems(parsed, sbase)
            if self._wants_feedback(parsed, problems):
                rounds += 1
                fb = self._feedback_problems(parsed, problems)
                log.info("answer for %s cannot be used (%s) → one feedback round", cluster.id, fb[0][:160])
                messages = messages + [ChatMessage(role="assistant", content=last.content),
                                       ChatMessage(role="user", content=self.script_feedback_message(parsed, fb, mode))]
                last, parsed = self._call(messages, model, usage, transcript, role=role, metadata=meta,
                                          parse=parse_script_response)
                verify = parsed.verify or verify
                problems = self._script_problems(parsed, sbase)
                if not (parsed.code and not problems) and not (parsed.edits and not parsed.edits_problem):
                    notes.append(f"feedback round did not give a usable script or edits (first answer: {fb[0][:200]})")

            code = parsed.code if not problems else ""
            edits = list(parsed.edits) if not parsed.edits_problem else []
            analysis = parsed.analysis
            no_script = parsed.no_script_reason
            error = ""
            if not code and not edits:
                error = self._answer_error(parsed, problems)
                if not parsed.candidate and not parsed.wants_context and PROTOCOL_WARNING in parsed.warnings:
                    analysis = "llm-error: response did not follow the protocol: " + parsed.raw.strip()[:300]
                elif parsed.wants_context and not parsed.candidate:
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
                requested_files=requested,
                raw_response=last.content if last else "",
                error=error,
            )
            log.debug("answer for %s: %d edit(s), %d change note(s), script %s, solution %d chars%s%s", cluster.id,
                      len(proposal.edits), len(proposal.changes),
                      "yes" if proposal.code else ("no: " + (proposal.no_script_reason or proposal.error or "-"))[:120],
                      len(proposal.solution), f", 운영자 확인 {len(proposal.verify)} chars" if proposal.verify else "",
                      f", error {proposal.error[:120]}" if proposal.error else "")
        except LLMError as exc:
            proposal = self._error_script(f"llm-error: {exc}", f"LLM call failed (status={exc.status})", model,
                                          messages, usage, requested)
        except Exception as exc:  # never raise into the orchestrator
            log.exception("LLM script request failed for cluster %s", cluster.id)
            proposal = self._error_script(f"llm-error: {type(exc).__name__}: {exc}",
                                          f"LLM call failed ({type(exc).__name__})", model, messages, usage, requested)

        self._journal_script(ws, cluster, ctx, mode, proposal, rounds, adv_in_prompt, packed)
        self._record(cluster, transcript, proposal)
        return proposal

    # ------------------------------------------------------------------ VerifyGuideLike (v2.1d)
    def verify_guide(self, ws: Workspace, cluster: ErrorCluster | None, ctx: FixContext | None,
                     changes: list[DependencyChange], diff: str, *, solution: str = "") -> str:
        """운영자 확인 사항: 적용된 수정이 의존성 메이저를 올렸거나 라이브러리를 바꿨는데 답에 `<verify>` 가
        없을 때(VerifyGuideLike). LLM 1회(role `verify_guide`): 프로젝트, 케이스의 첫 오류들, 변경 목록(사람이
        승인한 것 표시), diff(잘림), 답의 `<solution>`. 보고서 언어의 평문, 최대 VERIFY_MAX_CHARS. 변경이
        없거나 `libraries.verify_guide_llm` 이 꺼졌거나 실패하면 "" — 그때는 루프가 sbmig 가 만든 가이드를
        쓴다. 예외를 내지 않는다. 사용량은 `self.last_verify_usage`(루프가 예산에 청구).

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
        """verify_guide 요청의 (system, user) 메시지: 프로젝트 → 케이스 오류(최대 VERIFY_MAX_ERRORS) → 의존성
        변경 → 해결 방법 → 적용된 diff(최대 VERIFY_MAX_DIFF_CHARS) → `<verify>` 로 답하라는 요청.

        (system, user) of a `verify_guide` request."""
        lang = describe_language(self.cfg)
        system = load_verify_prompt(self.cfg.llm.prompt_version).replace("{{ language }}", lang)
        parts = [("A fix applied to this project raised a dependency's major version or replaced a library. The "
                  "build passes; the runtime is unverified. Write the 운영자 확인 사항 for the operator.")]
        if ctx is not None and ctx.profile_summary:
            parts.append(f"## Project\n{ctx.profile_summary}")
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
        """스크립트 요청의 (system, user 템플릿) — 처음 쓸 때
        로드(`llm.prompt_version`, 그 버전에 스크립트 프롬프트가 없으면 v1).

        (system, user template) of the script requests — loaded on first use (`llm.prompt_version`, falling back
        to v1 when the version has no script prompts)."""
        if self._script_prompts is None:
            extra = tuple(self.cfg.script.extra_allowed_imports)
            timeout = self.cfg.script.timeout_seconds
            try:
                self._script_prompts = load_script_prompts(self.cfg.llm.prompt_version, extra, timeout)
            except FileNotFoundError:
                log.warning("no script prompts for llm.prompt_version=%s; using v1", self.cfg.llm.prompt_version)
                self._script_prompts = load_script_prompts("v1", extra, timeout)
        return self._script_prompts

    def build_script_messages(self, packed: PackedContext, mode: str = "new", base: Optional[ScriptBase] = None,
                              *, system: Optional[str] = None,
                              user_template: Optional[jinja2.Template] = None) -> list[ChatMessage]:
        """스크립트 요청 메시지 (system, user): 꾸린 섹션·줄인 섹션·모드·보강 base 이름·base 가 트리에 남아
        있는지·설명 언어로 user 템플릿을 채운다."""
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
        """정적 검사(script_checker 또는 기본 check_script)가 찾은 문제들. 검사기가 깨져도 수정은 막지 않는다
        — 러너가 실행 전에 다시 검사한다."""
        checker = self.script_checker if self.script_checker is not None else default_script_checker(self.cfg)
        if checker is None:
            return []
        try:
            return [str(p) for p in (checker(code) or [])]
        except Exception as exc:          # a broken checker must not block fixing; the runner checks again
            log.warning("static script check failed (%s: %s); leaving the check to the runner", type(exc).__name__, exc)
            return []

    def _script_problems(self, parsed: ParsedScript, base: Optional[ScriptBase]) -> list[str]:
        """답의 스크립트를 쓸 수 없는 이유([] = 쓸 수 있음, 또는 편집·<no_script> 이유가 있어 스크립트가 없는
        것이 의도된 경우). 보강 답이 원본과 같은 코드면 그것도 문제다.

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
        """피드백에 적을 것: 못 읽는 편집과 / 또는 스크립트의 문제.

        What the feedback round names: the unreadable edits and / or the script's problems."""
        out = [parsed.edits_problem] if parsed.edits_problem else []
        if parsed.candidate or parsed.used_patches:
            out += problems
        return out or problems or [NO_SCRIPT]

    @staticmethod
    def _wants_feedback(parsed: ParsedScript, problems: list[str]) -> bool:
        """피드백 1회를 할지: 스크립트 블록은 있는데 못 씀, v1 patches 답, 편집 블록을 못 읽음. 수정이 아예
        없는 답(분석만 / 편집 없는 <no_script>)은 모델의 판단이라 피드백하지 않는다.

        One feedback round when a script block is there but unusable, the answer used v1 patches, or the edits
        block cannot be read. An answer without any fix (only an analysis / a <no_script> without edits) is the
        model's decision: no feedback round."""
        return bool(parsed.edits_problem) or bool(problems and (parsed.candidate or parsed.used_patches))

    def _answer_error(self, parsed: ParsedScript, problems: list[str]) -> str:
        """쓸 스크립트도 유효한 편집도 없는 답의 짧은 이유(ScriptProposal.error).

        The short reason of an answer with neither a usable script nor valid edits (ScriptProposal.error)."""
        err = self._script_error(parsed, problems)
        if parsed.edits_problem:
            return parsed.edits_problem if err == NO_SCRIPT and not parsed.candidate else \
                f"{err}; {parsed.edits_problem}"
        if parsed.no_script_reason and not parsed.candidate and err == NO_SCRIPT:
            return f"no script and no edits: {parsed.no_script_reason[:200]}"
        return err

    def _script_error(self, parsed: ParsedScript, problems: list[str]) -> str:
        """스크립트가 없는 제안의 짧은 이유.

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
        """피드백 user 메시지: 문제 목록(최대 10개) + 형식 안내(patches 금지, 편집 JSON 규칙) + 모드에 맞는
        "완전한 답을 다시 보내라" 요청."""
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
        """실패한 요청의 빈 제안(analysis 가 `llm-error: …` 로 시작 — 루프가 전송 장애로 분류한다)."""
        log.warning("%s", analysis[:300])
        return ScriptProposal(code="", analysis=analysis[:1000], model=model or "",
                              prompt_hash=messages_hash(messages) if messages else "", llm_usage=usage,
                              requested_files=requested, error=error[:300])

    def _journal_script(self, ws: Workspace, cluster: ErrorCluster, ctx: FixContext, mode: str, p: ScriptProposal,
                        rounds: int, advisory_hints: int, packed: Optional[PackedContext]) -> None:
        """`llm_script` 저널 이벤트(모드·라운드·호출·토큰·프롬프트
        해시·코드 / 편집 크기·오류). 저널 실패는 무시한다."""
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
                        "llm_error": p.analysis if p.analysis.startswith("llm-error") else None,
                        "advisory_hints": advisory_hints})
        except Exception:  # pragma: no cover
            log.debug("journal failed", exc_info=True)

    # ------------------------------------------------------------------ building blocks
    def _model(self, ctx: FixContext) -> Optional[str]:
        """이번 호출의 모델: `ctx.use_escalation_model`(루프가 정체 뒤나 케이스의 두 번째 보강부터 켠다)이면
        `llm.escalation_model`(설정됐을 때), 아니면 `llm.model`(미설정이면 None = 게이트웨이 기본)."""
        llm = self.cfg.llm
        if ctx.use_escalation_model and not _unset(llm.escalation_model):
            return llm.escalation_model
        return None if _unset(llm.model) else llm.model

    @staticmethod
    def call_tags(ctx: Optional[FixContext], cluster_id: Optional[str] = None,
                  ws: Optional[Workspace] = None) -> tuple[str, dict]:
        """propose_script 한 번의 모든 LLM 호출에 붙는 (role, metadata) — 게이트웨이가 목적별로 라우팅·예산을
        할 수 있게. role 은 fix(에스컬레이션이면 fix_escalation), metadata 는
        run_id·project·phase·iteration·cluster_id(비밀 금지).

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
        """LLM 한 번: watchdog 침묵 표시, 사용량 누적, 답 파싱, transcript(기록용)에 추가. 예외(LLMError 등)는
        호출한 쪽으로 그대로 올라간다."""
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
        transcript.append({"prompt_hash": messages_hash(messages), "response": resp.content,
                           "usage": u.model_dump(), "warnings": parsed.warnings})
        if parsed.warnings:
            log.debug("protocol warnings: %s", parsed.warnings)
        return resp, parsed

    # ------------------------------------------------------------------ context serving
    def resolve_path(self, ws: Workspace, want: str) -> tuple[Optional[str], list[str]]:
        """LLM 이 달라는 경로(부분 경로나 FQCN 일 수도)를 워크스페이스 파일로. (경로, 후보들); 하나로 정해지지
        않으면 (None, 후보 최대 8개).

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
        """src/ 아래 리소스 파일(properties / yml / yaml / xml / factories / imports, pom 제외)."""
        out: list[str] = []
        for ext in ("properties", "yml", "yaml", "xml", "factories", "imports"):
            for f in ws.glob(f"*.{ext}"):
                f = f.replace("\\", "/")
                if f.startswith("src/") or "/src/" in f:
                    if PurePosixPath(f).name != "pom.xml":
                        out.append(f)
        return sorted(set(out))

    def serve_context(self, ws: Workspace, need_files: list[str], need_grep: list[str], remaining_rounds: int) -> str:
        """`<need_files>` / `<need_grep>` 에 답하는 user 메시지: 파일은 라운드당 MAX_SERVED_FILES
        개(MAX_SERVED_LINES 줄을 넘으면 앞 SERVED_HEAD_LINES + 뒤 SERVED_TAIL_LINES 줄), grep 결과, 남은
        라운드 안내(마지막이면 "지금 답하라")."""
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
                         "and <script> (or <no_script> with the reason and <edits>; or explain in <analysis> why this "
                         "case cannot be fixed safely).")
        else:
            parts.append(f"You may ask for more context {remaining_rounds} more time(s), but prefer answering now "
                         "(<analysis>, <solution>, <edits>, <changes>, <script>).")
        return "\n\n".join(parts) + "\n"

    def grep(self, ws: Workspace, patterns: list[str]) -> str:
        """LLM 의 `<need_grep>`: java / pom / 리소스 파일을 한 번씩만 읽어 패턴 최대 5개를 찾는다. 큰
        파일(MAX_GREP_FILE_BYTES 초과)은 건너뛰고, MAX_GREP_FILES 개·GREP_BUDGET_S 초에서 멈춘다(Windows +
        실시간 백신에서 너무 느렸던 경험). 잘못된 정규식은 글자 그대로 찾는다.

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
        """스크립트 요청 하나의 기록(`script-<cluster>-<hash>.json`: 호출 transcript + 제안)."""
        self._record_json(prefix, cluster.id, p.prompt_hash, {"cluster_id": cluster.id, "calls": transcript,
                                                              "proposal": p.model_dump(mode="json")})

    def _record_json(self, prefix: str, cluster_id: str, prompt_hash: str, payload: dict) -> None:
        """`<llm.record_dir>/<prefix>-<cluster>-<hash>.json` 을 쓴다(record dir 이 없거나 클라이언트가
        RecordingClient 면 아무것도 안 함). 실패해도 수정은 계속.

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
