"""구조 검토 단계(`restructure.review`)를 실행하는 모듈.
LLM이 pom, 의존성, 모듈 구조를 회사 규칙에 맞는지 보고, critical 문제는 LLM이 준 `<edits>`로 고친다.

Spec: docs/v2/PLAN-v2.md §3.3, docs/v2/PLAN-v2.1.md §2.
* Answer: the `<review>` JSON + `<analysis>`, `<solution>`, `<edits>` (JSON list of `Edit`) and an
  optional `<script>` (`StructureReview.answer`; the loop stores the script for reuse).
* Critical issues: edits go through `sbmig.script.apply_edits` (same helper API / guard as a fix
  script); an answer with a script and no edits runs the script through the `ScriptRunnerLike`.
* Guards: edits / changes that touch sources are refused, every changed pom must still parse.
* Warnings: report section "Structure review" + Handover; the loop records the review in the
  error list (no KB write).
* LLM calls, at most 3: the review; once more when the answer is unreadable (no review JSON, or
  unreadable edits next to a critical issue); once more when the edits could not be applied
  (error + "restored" go back to the LLM).
* No LLM / `review.enabled` false = `skipped`; transport error, unreadable answer, anything
  unexpected = `failed` with a note. Every log line is tagged `[review]` (`sbmig.core.stage`).

StructureReviewer implements `StructureReviewerLike`. The orchestrator calls `review(ws, profile, …)` right
after the restructure is committed, before the first `verify.build`. A successful fix stays uncommitted and
the orchestrator commits it as `review: …`; a failed one is rolled back. Nothing is raised, and the build
always goes ahead.
"""
from __future__ import annotations

import logging
import posixpath
import re
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from sbmig.core.config import Config
from sbmig.core.interfaces import ChatMessage, LLMResponse
from sbmig.core.models import (CaseType, LLMUsage, asked_model_name, served_beside, ProjectProfile, ReviewIssue, ScriptProposal, ScriptRunResult,
                               StructureReview)
from sbmig.core.stage import stage

from .inputs import (ReviewInput, collect_review_input, invariant_violations, java_charset, raw_poms,
                     source_encodings)
from .parse import CUT_REASONS, ParsedReview, changes_note, mark_unsupported, parse_review_answer

log = logging.getLogger("sbmig.review")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ROLE = "structure_review"
STEP_ID = "restructure.review"
RETRY_MESSAGE = ("Your answer could not be read. Answer again with exactly one <review>{…}</review> block holding the "
                 "JSON object described in the system prompt (verdict, issues, analysis) and — only for critical "
                 "issues you can fix in the build files — <analysis>, <solution>, <edits> (a JSON list, every edit "
                 "with its \"desc\"). No other text.")
EDITS_RETRY_MESSAGE = ("Your <edits> could not be read: {problem}. Answer again with the COMPLETE answer: the <review> "
                       "block, <analysis>, <solution> and <edits> as ONE valid JSON list (double quotes, `\\n` and "
                       "`\\\"` escaped inside strings, no comments, no trailing commas).")
APPLY_RETRY_MESSAGE = ("Your edits could not be applied: {error}. The project was restored to the state before your "
                       "edits. Answer again with the COMPLETE answer (<review>, <analysis>, <solution>, corrected "
                       "<edits> each with its \"desc\"): copy every `old` text exactly from the poms shown (indentation "
                       "and line breaks included), prefer `op: pom` editor calls, change build files only.")
# 토큰 한도에서 잘린 답변을 다시 요청할 때 덧붙이는 문장. 더 짧게 답하라고 한다
CUT_RETRY_TAIL = (" Your previous answer was cut off at the output token limit: keep this one shorter (a short analysis, "
                  "only the issues that matter, edits only for the critical ones).")
# review.ask_script가 켜져 있으면 다시 요청하는 문장 끝에 붙이는 스크립트 안내
SCRIPT_RETRY_TAIL = " Optionally add <script> with <changes>."
# review_v1.md에서 스크립트용 부분을 감싼 표시. review.ask_script가 꺼져 있으면 이 부분을 뺀다
_SCRIPT_BLOCK_RE = re.compile(r"<!-- script:begin -->.*?<!-- script:end -->\n?", re.S)
_SCRIPT_MARK_RE = re.compile(r"<!-- script:(?:begin|end) -->\n?")
SOURCE_SUFFIXES = (".java", ".kt", ".groovy", ".scala", ".jsp", ".jspx", ".tag")
_ENCODING_WORDS = ("encoding", "인코딩", "ms949", "cp949", "euc-kr", "euc_kr", "charset")
SBMIG_CHECK = "(sbmig check) "
REVIEW_INPUT_FILE = "review-input.md"           # <run dir>/logs/review-input.md
_ENCODING_ISSUE = " Java sources in `"          # sbmig가 직접 만드는 인코딩 이슈 문장의 일부(_encoding_issue 참고)

# HELPER_API_DOC을 import할 수 없을 때 쓰는 도우미 API 표. docs/INTERFACES.md "Script contract"와 같다
FALLBACK_HELPER_API = """| API | |
|---|---|
| `p.errors` | list of dicts (empty for the structure review) |
| `p.files(glob="**/*")` | sorted relative posix paths (no `.git/`, `target/`, `node_modules/`) |
| `p.read(path)` / `p.exists(path)` | text decoded with the file's detected encoding |
| `p.write(path, text)` | same encoding and newline style as the original (a new file: UTF-8, `\\n`) |
| `p.replace(path, old, new, count=-1)` / `p.sub(path, pattern, repl, count=0, flags=0)` | number of replacements |
| `p.create(path, text)` / `p.delete(path)` / `p.move(src, dst)` | create refuses an existing file |
| `p.pom(path="pom.xml")` | pom editor: `add_dependency(g, a, version=None, scope=None, type=None, classifier=None)`, `remove_dependency(g, a)`, `set_dependency_version(g, a, version)`, `add_exclusion(g, a, ex_g, ex_a)`, `add_managed_dependency(g, a, version, scope=None, type=None)`, `set_property(name, value)`, `remove_property(name)`, `set_plugin_version(g, a, version)`, `text()`, `save()` — mutators return True when the pom changed; call `save()` once at the end |
| `p.log(msg)` | a log line (recorded); `print` goes to the log too |
"""


@lru_cache(maxsize=4)
def _system_template(version: str = "v1") -> str:
    path = PROMPTS_DIR / f"review_{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"review prompt {version!r} not found in {PROMPTS_DIR}")
    return path.read_text(encoding="utf-8")


def helper_api_doc() -> str:
    """프롬프트에 넣을 도우미 API 표를 반환한다.
    `sbmig.script.HELPER_API_DOC`에서 첫 표 줄부터 쓴다. import할 수 없으면 FALLBACK_HELPER_API를 쓴다.

    The text before the first table line is the fix-loop answer format, which does not apply to the review."""
    try:
        from sbmig.script import HELPER_API_DOC
        if isinstance(HELPER_API_DOC, str) and HELPER_API_DOC.strip():
            lines = HELPER_API_DOC.strip().splitlines()
            start = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("|")), 0)
            return "\n".join(lines[start:]).strip() + "\n"
    except Exception:  # noqa: BLE001 - 표 하나 때문에 검토가 실패하면 안 된다
        log.debug("sbmig.script.HELPER_API_DOC unavailable: using the built-in helper table")
    return FALLBACK_HELPER_API


def ask_script(cfg: Any) -> bool:
    """검토에서 `<script>`도 받을지 반환한다(`review.ask_script`). 기본값인 false면 edits만 받는다."""
    return bool(getattr(getattr(cfg, "review", None), "ask_script", False))


def _retry_text(msg: str, cfg: Any) -> str:
    """LLM에 다시 요청할 문장을 만든다.
    `review.ask_script`가 켜져 있으면 끝에 스크립트도 보낼 수 있다는 안내를 붙인다."""
    return msg + SCRIPT_RETRY_TAIL if ask_script(cfg) else msg


def system_prompt(cfg: Config, version: str = "v1") -> str:
    """구조 검토 system 프롬프트(`prompts/review_<version>.md`)를 채워 반환한다.
    `review.ask_script`가 false면 `<!-- script:begin -->`부터 `<!-- script:end -->`까지 뺀다.

    The marked parts: script paragraph, `<changes>` / `<script>` example, script rules, helper API table."""
    extra = [m for m in (getattr(cfg.script, "extra_allowed_imports", None) or []) if m]
    from sbmig.script.notes import describe_language    # report.language에 맞춰 변경 설명의 언어를 정한다

    text = _system_template(version)
    text = _SCRIPT_MARK_RE.sub("", text) if ask_script(cfg) else _SCRIPT_BLOCK_RE.sub("", text)
    if "{{ helper_api }}" in text:
        text = text.replace("{{ helper_api }}", helper_api_doc().strip())
    return (text.replace("{{ extra_imports }}", "".join(f", {m}" for m in extra))
            .replace("{{ desc_language }}", describe_language(cfg))).strip() + "\n"


def _unset(v: Optional[str]) -> bool:
    return not v or "${" in v or v == "default"


def _in_flight() -> Any:
    try:
        from sbmig.fix.client import llm_call_in_flight  # LLM 호출 중에는 루프 감시가 경고하지 않게 한다
        return llm_call_in_flight()
    except Exception:  # noqa: BLE001
        return nullcontext()


def _short(text: object, limit: int = 300) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _diff_stats(diff: str) -> dict[str, tuple[int, int]]:
    """unified `git diff`에서 파일별 (추가 줄 수, 삭제 줄 수)를 센다."""
    out: dict[str, list[int]] = {}
    cur: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            cur = m.group(2) if m else None
            if cur is not None:
                out.setdefault(cur, [0, 0])
        elif cur is None or line.startswith(("+++", "---")):
            continue
        elif line.startswith("+"):
            out[cur][0] += 1
        elif line.startswith("-"):
            out[cur][1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def _file_desc(path: str, notes: list[Any], changed: list[str]) -> str:
    """바뀐 파일 `path`에 대한 LLM의 설명을 찾는다.
    파일 이름만 쓴 설명은 바뀐 파일 중 그 이름이 하나뿐일 때만 인정한다.
    설명은 "where: change" 형식이다. 여러 개면 "; "로 잇는다."""
    from sbmig.script.notes import same_path

    base = PurePosixPath(path).name.lower()
    out: list[str] = []
    for n in notes:
        npath = str(getattr(n, "path", "") or "")
        if not npath:
            continue
        hit = same_path(npath, path)
        if not hit and PurePosixPath(npath.replace("\\", "/")).name.lower() == base:
            hit = sum(1 for c in changed if PurePosixPath(c).name.lower() == base) == 1
        if not hit:
            continue
        where, change = str(getattr(n, "where", "") or ""), str(getattr(n, "change", "") or "")
        text = f"{where}: {change}" if where and change and where not in change else (change or where)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


class StructureReviewer:
    """구조 검토를 실행하는 클래스.
    `llm`은 사용량 한도가 걸린 LLMClientLike이고 None일 수 있다.
    `runner`는 스크립트 답변을 실행하는 ScriptRunnerLike다. None이면 스크립트는 실행하지 않는다.
    `kb`는 쓰지 않는다.

    `edits_runner`: the runner `apply_edits` uses (None = its own `ScriptRunner`)."""

    def __init__(self, cfg: Config, llm: Any = None, runner: Any = None, kb: Any = None, *,
                 prompt_version: str = "v1", edits_runner: Any = None):
        self.cfg = cfg
        self.llm = llm
        self.runner = runner
        self.kb = kb                                   # 쓰지 않는다. 팩토리 시그니처 때문에 남겨 둔다
        self.prompt_version = prompt_version
        self.edits_runner = edits_runner
        self._unsupported: list[str] = []             # 마지막 답변에서 자료에 없던 단어 목록

    # ------------------------------------------------------------------ 공개 메서드
    def review(self, ws: Any, profile: Optional[ProjectProfile], cfg: Optional[Config] = None, *, run_id: str = "",
               restructure_notes: Iterable[str] = (), restructure_results: Iterable[Any] = ()) -> StructureReview:
        """검토를 한 번 실행하고 항상 StructureReview를 반환한다. 예외를 던지지 않는다.
        내부 오류가 나면 verdict를 `failed`로 하고 이미 적용한 수정은 되돌린다."""
        with stage("review"):
            cfg = cfg or self.cfg
            run_id = run_id or str(getattr(ws, "run_id", "") or "")
            # RunReport.project_name과 같은 규칙
            project = (profile.name if profile is not None and profile.name else
                       str(getattr(getattr(ws, "root", None), "name", "") or ""))
            rv = StructureReview(id=f"{run_id}:review", run_id=run_id, project=project,
                                 case_type=profile.case_type if profile is not None else CaseType.UNKNOWN)
            try:
                self._review(rv, ws, profile, cfg, list(restructure_notes or ()), list(restructure_results or ()))
            # 검토가 실패해도 실행은 계속한다. 최종 결과는 빌드가 정한다
            except Exception as e:  # noqa: BLE001
                log.warning("structure review failed: %s: %s", type(e).__name__, e, exc_info=True)
                rv.verdict = "failed"
                rv.note = _short(f"review error: {type(e).__name__}: {e}", 500)
                if rv.script_result is not None and not rv.reverted:
                    self._restore(ws, rv, "the review failed after its fix ran")
            if rv.answer is not None:
                rv.answer = rv.answer.model_copy(update={"llm_usage": rv.llm_usage, "model": rv.model})
            self._log_summary(rv)
            return rv

    # ------------------------------------------------------------------ 검토 단계
    def _review(self, rv: StructureReview, ws: Any, profile: Optional[ProjectProfile], cfg: Config,
                notes: list[str], results: list[Any]) -> None:
        """검토 본체를 실행한다.
        순서는 입력 수집 -> 질문(_ask) -> 답변 반영(_take) -> critical 수정(_fix) -> 재시도(_apply_retry)다.
        재시도는 편집 적용이 실패했을 때만 한다."""
        if not cfg.review.enabled:
            rv.verdict, rv.note = "skipped", "review.enabled is off"
            return
        if self.llm is None:
            rv.verdict, rv.note = "skipped", "no LLM client for this run (dry run or LLM off)"
            return
        inp = collect_review_input(ws, profile, cfg, notes=notes, results=results)
        if not inp.poms:
            rv.verdict, rv.note = "skipped", "no pom.xml in the workspace"
            return
        user = inp.render()
        if getattr(cfg.review, "save_input", False):
            self._save_input(ws, user)
        messages = [ChatMessage(role="system", content=system_prompt(cfg, self.prompt_version)),
                    ChatMessage(role="user", content=user)]
        modules = len({str(PurePosixPath(p.path).parent) for p in inp.poms})
        log.info("structure review: asking the LLM (%d pom(s), %d module(s), %d pom characters%s, %d module encoding "
                 "line(s), prompt %d characters)", len(inp.poms), modules, inp.pom_chars,
                 f", {sum(1 for p in inp.poms if p.cut)} cut" if any(p.cut for p in inp.poms) else "",
                 len(inp.encodings), len(user))
        parsed, error, messages = self._ask(rv, cfg, messages)
        if parsed is None:
            rv.verdict, rv.note = "failed", error
            log.warning("structure review: no usable answer — %s (the build goes ahead)", error)
            return
        self._take(rv, parsed, inp)
        log.info("structure review: %d pom, %d module; LLM 판정: %s (critical %d, warning %d)%s", len(inp.poms),
                 modules, rv.verdict, len(rv.critical), len(rv.issues) - len(rv.critical),
                 f"; edits {len(parsed.edits)}" if parsed.edits else "")
        if parsed.warnings:
            log.debug("structure review answer warnings: %s", "; ".join(parsed.warnings))
        if self._fix(rv, ws, cfg, parsed, inp) == "retry":
            self._apply_retry(rv, ws, cfg, inp, messages)

    def _take(self, rv: StructureReview, parsed: ParsedReview, inp: ReviewInput) -> None:
        """읽은 답변의 이슈, 분석, 스크립트, 편집을 검토 결과에 옮긴다.
        LLM이 놓친 인코딩 불일치는 sbmig가 critical 이슈로 추가한다."""
        # 프롬프트 자료에 없는 버전이나 속성을 언급한 이슈는 표시만 한다. 고치지도, 인수인계하지도 않는다
        rv.issues, self._unsupported = mark_unsupported(list(parsed.issues), inp.material_text(), inp.state_text())
        if self._unsupported:
            log.info("structure review: %d issue(s) name something not in the material (%s) — kept in the report, "
                     "not applied, not handed over", sum(1 for i in rv.issues if i.unsupported),
                     ", ".join(self._unsupported[:8]))
        rv.analysis = parsed.analysis
        rv.script = parsed.script
        analysis = parsed.fix_analysis
        missing = changes_note(parsed)
        if missing:
            analysis = (analysis + "\n" if analysis else "") + f"[sbmig] {missing}"
        rv.answer = ScriptProposal(code=parsed.script, analysis=analysis, solution=parsed.solution,
                                   edits=list(parsed.edits), changes=list(parsed.changes),
                                   no_script_reason=parsed.no_script_reason, verify=parsed.verify, model=rv.model,
                                   raw_response=parsed.raw, error=parsed.edits_problem)
        self._encoding_check(rv, inp)
        rv.verdict = "issues" if rv.issues else "ok"

    def _ask(self, rv: StructureReview, cfg: Config, messages: list[ChatMessage]
             ) -> tuple[Optional[ParsedReview], str, list[ChatMessage]]:
        """LLM에 검토를 요청하고 (읽은 답변, 오류, 대화)를 반환한다.
        첫 답변에 검토 JSON이 없거나 critical 이슈의 edits를 읽지 못하면 한 번 더 요청한다. 최대 2번이다.
        두 번째 요청이 실패해도 첫 답변의 검토 JSON을 읽었으면 편집 없이 그것을 쓴다.

        Retry text: RETRY_MESSAGE for no review JSON, EDITS_RETRY_MESSAGE for unreadable edits."""
        resp = self._call(rv, cfg, messages)
        if isinstance(resp, str):
            return None, resp, messages
        parsed = parse_review_answer(resp.content, getattr(resp, "finish_reason", None))
        convo = messages + [ChatMessage(role="assistant", content=resp.content[:8000] or "(empty)")]
        if parsed.ok and not (parsed.edits_problem and parsed.critical):
            return parsed, "", convo
        if parsed.ok:
            log.info("structure review: edits unreadable (%s) — asking once more", _short(parsed.edits_problem, 200))
            ask = _retry_text(EDITS_RETRY_MESSAGE.format(problem=_short(parsed.edits_problem, 400)), cfg)
        else:
            log.info("structure review: answer unreadable (%s%s) — asking once more for the format", parsed.error,
                     ", cut at the token limit" if (resp.finish_reason or "") == "length" else "")
            ask = _retry_text(RETRY_MESSAGE, cfg)
            if (getattr(resp, "finish_reason", None) or "") in CUT_REASONS:
                ask += CUT_RETRY_TAIL
        first, first_error = parsed, parsed.error
        retry = convo + [ChatMessage(role="user", content=ask)]
        resp2 = self._call(rv, cfg, retry)
        if isinstance(resp2, str):
            if first.ok:                                  # 검토 JSON은 읽었다. 편집 없이 그대로 쓴다
                return first, "", convo
            return None, f"unreadable answer ({first_error}), then {resp2}", retry
        parsed = parse_review_answer(resp2.content, getattr(resp2, "finish_reason", None))
        convo2 = retry + [ChatMessage(role="assistant", content=resp2.content[:8000] or "(empty)")]
        if parsed.ok:
            return parsed, "", convo2
        if first.ok:
            return first, "", convo
        return None, _short(f"unreadable answer twice ({parsed.error}): {resp2.content}", 400), convo2

    def _call(self, rv: StructureReview, cfg: Config, messages: list[ChatMessage]) -> LLMResponse | str:
        """LLM을 한 번 호출한다(role `structure_review`).
        실패하면 예외 대신 `llm-error: …` 문자열을 반환한다. 사용량은 `rv.llm_usage`에 더한다."""
        model = None if _unset(cfg.llm.model) else cfg.llm.model
        meta = {"run_id": rv.run_id, "project": rv.project, "phase": "restructure", "step": STEP_ID}
        try:
            with _in_flight():
                resp = self.llm.complete(messages, model=model, max_tokens=cfg.llm.max_output_tokens,
                                         temperature=cfg.llm.temperature, role=ROLE, metadata=meta)
        except Exception as e:  # noqa: BLE001 - LLM 오류, 한도 초과, 전송 오류는 검토만 건너뛴다
            return _short(f"llm-error: {type(e).__name__}: {e}", 400)
        # 이름은 요청한 모델이다. 응답이 알려 준 모델은 served로 남긴다
        name = asked_model_name(getattr(resp, "model", ""), model, getattr(self.llm, "model", ""))
        u = LLMUsage(model=name, calls=1,
                     input_tokens=int(getattr(resp, "input_tokens", 0) or 0),
                     output_tokens=int(getattr(resp, "output_tokens", 0) or 0),
                     served=served_beside(name, list(getattr(resp, "served_models", None) or [])))
        rv.llm_usage = rv.llm_usage.add(u)
        rv.model = rv.model or u.model
        if not isinstance(getattr(resp, "content", None), str):
            return "llm-error: the client returned no text"
        return resp

    def _encoding_check(self, rv: StructureReview, inp: ReviewInput) -> None:
        """UTF-8이 아닌 소스가 있는데 맞는 인코딩 선언이 없는 모듈을 critical로 본다.
        이 문제는 빌드 오류로 드러나지 않기 때문이다.
        LLM이 그 모듈의 인코딩 이슈를 지적하지 않았으면 sbmig가 `(sbmig check)` 이슈로 추가한다.
        LLM의 이슈는 그 모듈의 pom이나 폴더, 또는 모듈이 상속하는 프로젝트 안 pom을 가리킬 때만 인정한다.

        With role parents a module does not inherit the root aggregator (an issue on it does not count)."""
        from .inputs import _chain

        by_path = {p.path: p for p in inp.pom_infos}
        for me in inp.encoding_mismatches:
            pom = me.pom
            info = by_path.get(pom)
            chain = {q.path for q in _chain(info, inp.pom_infos)} if info is not None else {pom}
            chain |= {posixpath.dirname(c) for c in chain if posixpath.dirname(c)}
            named = any(
                any(w in f"{i.problem} {i.fix_hint}".lower() for w in _ENCODING_WORDS)
                and not i.unsupported
                and ((i.file or "").rstrip("/") in chain or (not i.file and len(chain) == 1 and pom == "pom.xml"))
                for i in rv.issues)
            if named:
                continue
            rv.issues.append(self._encoding_issue(me))

    @staticmethod
    def _encoding_issue(me: Any) -> ReviewIssue:
        names = ", ".join(java_charset(e) for e in me.mismatch)
        declared = f"`{me.declared}`" if me.declared else "UTF-8 (not declared in the project poms)"
        return ReviewIssue(
            severity="critical", file=me.pom,
            problem=f"{SBMIG_CHECK}{names}{_ENCODING_ISSUE}{me.module or '(root)'}` build with {declared}: javac "
                    "only warns and garbles the Korean string literals",
            fix_hint=f"set `project.build.sourceEncoding` to {names.split(', ')[0]} in `{me.pom}` (or convert the "
                     "sources to UTF-8)" + (" — the module mixes encodings, convert one side" if me.mixed else ""))

    def _encoding_recheck(self, rv: StructureReview, ws: Any, cfg: Config) -> None:
        """편집 뒤 인코딩 불일치를 다시 계산한다.
        고쳐진 모듈의 sbmig 이슈는 뺀다. 아직 맞지 않는 모듈은 LLM 답변과 상관없이 critical로 남긴다.
        예외를 던지지 않는다."""
        try:
            from sbmig.analyze.pom_parser import parse_poms

            poms, _w = parse_poms(ws)
            now = [m for m in source_encodings(ws, poms, getattr(cfg.build, "encoding_candidates", None),
                                               skip_tests=bool(getattr(cfg.build, "skip_test_compile", True)))
                   if m.mismatch]
        except Exception:  # 다시 계산하지 못했다. 처음 검사한 이슈를 그대로 둔다
            log.debug("encoding re-check after the review edits failed", exc_info=True)
            return
        broken = {m.pom: m for m in now}
        kept = [i for i in rv.issues if not (i.problem.startswith(SBMIG_CHECK) and _ENCODING_ISSUE in i.problem
                                             and i.file not in broken)]
        have = {i.file for i in kept if i.problem.startswith(SBMIG_CHECK) and _ENCODING_ISSUE in i.problem}
        kept += [self._encoding_issue(m) for p, m in broken.items() if p not in have]
        if len(kept) != len(rv.issues) or any(a is not b for a, b in zip(kept, rv.issues)):
            log.info("structure review: source encodings re-checked after the edits — %d module(s) still mismatched",
                     len(broken))
        rv.issues = kept

    def _fix(self, rv: StructureReview, ws: Any, cfg: Config, parsed: ParsedReview,
             inp: Optional[ReviewInput] = None) -> str:
        """critical 이슈에 대한 수정을 적용한다. 편집이 있으면 편집을, 없으면 스크립트를 쓴다.
        빈 문자열은 끝났거나 할 일이 없다는 뜻이다.
        "retry"는 편집이 실패해서 되돌렸다는 뜻이다. 호출한 쪽은 LLM에 한 번 더 요청한다.
        커밋되지 않은 변경이 있으면 적용하지 않는다."""
        # 프롬프트 자료에 없는 내용을 언급한 critical 이슈는 고치지 않는다
        critical = [i for i in rv.critical if not i.unsupported]
        edits = list(parsed.edits)
        if not edits and not rv.script:
            if critical:
                rv.note = ("edits unreadable, no script for the critical issue(s): they are handed over"
                           f" ({_short(parsed.edits_problem, 200)})" if parsed.edits_problem else
                           "no edits or script for the critical issue(s): they are handed over")
            return ""
        what = "edits" if edits else "script"
        if not critical:
            rv.note = (f"{what} not applied: the review reported no critical issue" if not rv.critical else
                       f"{what} not applied: every critical issue names something not in the material")
            return ""
        if not cfg.review.fix_critical:
            rv.note = f"{what} not applied: review.fix_critical is off"
            return ""
        if not edits and self.runner is None:
            rv.note = "script not run: no script runner in this run"
            return ""
        # 커밋되지 않은 변경이 있으면 적용하지 않는다. 실패해서 되돌릴 때 재구성 결과까지 지워진다
        if self._dirty(ws):
            rv.note = (f"{what} not applied: the working tree has uncommitted changes (the restructure must be "
                       "committed before the review — restoring after a failed fix would lose them)")
            log.warning("structure review: %s", rv.note)
            return ""
        if edits:
            return self._apply(rv, ws, cfg, parsed, inp)
        self._run_script(rv, ws)
        return ""

    def _apply(self, rv: StructureReview, ws: Any, cfg: Config, parsed: ParsedReview,
               inp: Optional[ReviewInput] = None) -> str:
        """검토의 편집을 apply_edits로 적용한다. 커밋은 하지 않는다.
        소스를 고치는 편집은 거부한다.
        적용이 실패하거나, pom을 파싱할 수 없거나, 재구성 규칙을 깨면 되돌리고 "retry"를 반환한다."""
        from sbmig.script.edits import apply_edits, summarize_edits

        edits = self._supported_edits(list(parsed.edits))
        if not edits:
            rv.note = "edits not applied: every edit carries a version / property that is not in the material"
            log.warning("structure review: %s", rv.note)
            return ""
        sources = sorted({p for e in edits for p in (e.path, e.to) if p and p.lower().endswith(SOURCE_SUFFIXES)})
        if sources:
            rv.note = ("edits not applied: they change sources (" + ", ".join(sources[:5]) + ") — only build files "
                       "may change")
            log.warning("structure review: %s", rv.note)
            return ""
        log.info("structure review: applying %d edit(s) for %d critical issue(s): %s", len(edits), len(rv.critical),
                 _short(summarize_edits(edits), 300))
        res = apply_edits(ws, edits, cfg=cfg, runner=self.edits_runner)
        rv.script_result = res
        if res.status != "ok":
            what = res.error or "; ".join(res.problems[:3]) or "-"
            self._restore(ws, rv, f"review edits failed ({res.status}): {_short(what, 300)}")
            return "retry"
        # 편집기의 보고 대신 git이 확인한 변경을 기준으로 한다. 소스 수정 검사와 pom 파싱 검사도 이 목록을 쓴다
        changed = self._changed_files(ws, res.changed_files)
        if changed != sorted(res.changed_files):
            rv.script_result = res = res.model_copy(update={"changed_files": changed})
        if not changed:
            rv.note = f"review edits ({len(edits)}) changed nothing (already as asked)"
            return ""
        touched = [f for f in changed if f.lower().endswith(SOURCE_SUFFIXES)]
        if touched:
            self._restore(ws, rv, "review edits touched sources (" + ", ".join(touched[:5])
                          + ") — only build files may change: restored")
            return ""
        broken = self._unparsable_poms(ws, changed)
        if broken:
            self._restore(ws, rv, "review edits left unparsable pom(s): " + "; ".join(broken[:3]) + " — restored")
            return "retry"
        # 검토 편집이 재구성의 결정을 뒤집었으면 되돌린다. 이유를 알려 주고 다시 요청한다
        violations = self._invariants(ws, cfg, changed, inp) if inp is not None and \
            getattr(cfg.review, "invariant_check", True) else []
        if violations:
            self._restore(ws, rv, "review edits broke a restructure rule: " + "; ".join(violations[:4])
                          + (f" (+{len(violations) - 4})" if len(violations) > 4 else "") + " — restored")
            return "retry"
        if inp is not None and inp.encoding_mismatches:
            self._encoding_recheck(rv, ws, cfg)
        self._log_diff(ws, changed, rv.answer.solution if rv.answer is not None else "",
                       rv.answer.changes if rv.answer is not None else ())
        rv.note = (f"review edits changed {len(changed)} file(s): {', '.join(changed[:8])}"
                   + (f" (+{len(changed) - 8})" if len(changed) > 8 else "")
                   + f" ({len(edits)} edit(s)) — not committed yet")
        return ""

    def _apply_retry(self, rv: StructureReview, ws: Any, cfg: Config, inp: ReviewInput,
                     convo: list[ChatMessage]) -> None:
        """실패 이유를 LLM에 알리고, 고쳐 온 편집을 한 번만 다시 적용한다.
        그래도 실패하면 첫 결과와 되돌린 상태를 남긴다."""
        error = rv.note
        log.info("structure review: asking the LLM once more with the failure (%s)", _short(error, 200))
        ask = convo + [ChatMessage(role="user", content=_retry_text(APPLY_RETRY_MESSAGE.format(
            error=_short(error, 600)), cfg))]
        resp = self._call(rv, cfg, ask)
        if isinstance(resp, str):
            rv.note = f"{error}; asking again failed: {resp}"
            return
        parsed = parse_review_answer(resp.content, getattr(resp, "finish_reason", None))
        if not parsed.ok or not parsed.edits:
            why = parsed.error or parsed.edits_problem or "no edits in the answer"
            rv.note = f"{error}; the corrected answer had no usable edits ({_short(why, 200)})"
            return
        first_result = rv.script_result
        self._take(rv, parsed, inp)
        rv.script_result, rv.reverted = None, False
        self._fix(rv, ws, cfg, parsed, inp)               # 세 번째 시도는 없다. 다시 실패하면 인수인계한다
        if rv.script_result is None:                      # 고쳐 온 답변을 적용하지 않았다. 프로젝트는 되돌린 상태다
            rv.script_result, rv.reverted = first_result, True
        rv.note = f"{rv.note} (second try; first: {_short(error, 200)})"

    def _run_script(self, rv: StructureReview, ws: Any) -> None:
        """편집 없이 스크립트만 온 답변을 ScriptRunnerLike로 실행한다.
        실행이 실패하거나, 소스를 고쳤거나, pom이 깨졌으면 되돌린다."""
        log.info("structure review: running the review script for %d critical issue(s)", len(rv.critical))
        try:
            res = self.runner.run(ws, rv.script, errors=[])
        except Exception as e:  # noqa: BLE001 - run은 예외를 던지지 않는다. 혹시 몰라 막아 둔다
            res = ScriptRunResult(status="error", error=f"{type(e).__name__}: {e}")
        rv.script_result = res
        if res.status != "ok":
            what = res.error or "; ".join(res.problems[:3]) or "-"
            self._restore(ws, rv, f"review script failed ({res.status}): {_short(what, 300)}")
            return
        changed = self._changed_files(ws, res.changed_files)
        if changed != sorted(res.changed_files):          # 스크립트가 보고한 것보다 git이 더 많이 찾았다
            rv.script_result = res = res.model_copy(update={"changed_files": changed})
        if not changed:
            rv.note = "review script ran but changed nothing"
            return
        sources = [f for f in changed if f.lower().endswith(SOURCE_SUFFIXES)]
        if sources:
            self._restore(ws, rv, "review script touched sources (" + ", ".join(sources[:5])
                          + ") — only build files may change: restored")
            return
        broken = self._unparsable_poms(ws, changed)
        if broken:
            self._restore(ws, rv, "review script left unparsable pom(s): " + "; ".join(broken[:3]) + " — restored")
            return
        self._log_diff(ws, changed, "", rv.answer.changes if rv.answer is not None else ())
        rv.note = (f"review script changed {len(changed)} file(s): {', '.join(changed[:8])}"
                   + (f" (+{len(changed) - 8})" if len(changed) > 8 else "") + " — not committed yet")

    # ------------------------------------------------------------------ 보조 함수
    def _supported_edits(self, edits: list[Any]) -> list[Any]:
        """프롬프트 자료에 없는 버전이나 속성을 담은 편집을 뺀 목록을 반환한다."""
        if not self._unsupported:
            return edits
        kept: list[Any] = []
        for e in edits:
            try:
                text = e.model_dump_json()
            except Exception:  # noqa: BLE001 - 모델 객체가 아니면 그대로 둔다
                kept.append(e)
                continue
            hit = [t for t in self._unsupported if re.search(rf"(?<![\w.-]){re.escape(t)}(?![\w-])", text)]
            if hit:
                log.info("structure review: edit on %s left out — it carries %s (not in the material)",
                         getattr(e, "path", "?"), ", ".join(hit[:3]))
                continue
            kept.append(e)
        return kept

    @staticmethod
    def _invariants(ws: Any, cfg: Config, changed: list[str], inp: ReviewInput) -> list[str]:
        """바뀐 pom을 다시 읽어 재구성의 결정을 뒤집었는지 `invariant_violations`로 검사한다.
        검사 자체가 실패하면 위반이 없는 것으로 본다."""
        try:
            from sbmig.analyze.pom_parser import parse_poms

            after_poms, _w = parse_poms(ws)
            after = raw_poms(ws, after_poms)
        except Exception:  # 다시 읽지 못하면 비교할 것이 없다. pom 파싱 검사는 이미 했다
            log.debug("review invariant check: re-reading the poms failed", exc_info=True)
            return []
        out = invariant_violations(cfg, changed, inp.pom_infos, inp.raw, after_poms, after, inp.chain, inp.results)
        if out:
            log.warning("structure review: the edits break %d restructure rule(s): %s", len(out), "; ".join(out[:4]))
        return out

    @staticmethod
    def _save_input(ws: Any, text: str) -> None:
        """검토에 쓴 user 프롬프트를 `<run dir>/logs/review-input.md`에 저장한다.
        실행 디렉토리가 없으면 건너뛴다."""
        try:
            from sbmig.core.workspace import logs_dir

            d = logs_dir(ws)
            d.mkdir(parents=True, exist_ok=True)
            (d / REVIEW_INPUT_FILE).write_text(text, encoding="utf-8")
            log.debug("structure review input saved: %s (%d characters)", d / REVIEW_INPUT_FILE, len(text))
        except Exception as e:  # noqa: BLE001 - 테스트처럼 실행 디렉토리가 없는 경우
            log.debug("structure review input not saved: %s: %s", type(e).__name__, e)

    @staticmethod
    def _dirty(ws: Any) -> bool:
        try:
            return bool(ws.is_dirty())
        except Exception:  # noqa: BLE001 - 테스트처럼 git 저장소가 없는 경우
            return False

    @staticmethod
    def _changed_files(ws: Any, reported: Iterable[str]) -> list[str]:
        """수정이 보고한 파일과 git이 확인한 변경 파일을 합쳐 반환한다.
        git을 쓸 수 없으면 보고된 파일만 반환한다."""
        out = {str(f).replace("\\", "/") for f in (reported or []) if f}
        try:
            out |= set(ws.changed_files_since(ws.head()))
        except Exception as e:  # noqa: BLE001 - git이 없으면 수정이 보고한 목록만 쓴다
            log.debug("git status for the review fix unavailable: %s", e)
        return sorted(out)

    @staticmethod
    def _log_diff(ws: Any, changed: list[str], solution: str, notes: Iterable[Any] = ()) -> None:
        """바뀐 파일마다 INFO 로그를 한 줄 남긴다. diff 본문은 DEBUG 로그로 남긴다.
        설명은 그 파일에 대한 LLM의 설명을 쓴다. 없으면 solution을 쓴다."""
        try:
            diff = ws.diff(ws.head())
        except Exception as e:  # noqa: BLE001 - git이 없으면 파일 목록만 남긴다
            log.debug("diff of the review fix unavailable: %s", e)
            diff = ""
        stats = _diff_stats(diff)
        lines = len([ln for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))])
        notes = list(notes or ())
        for f in changed:
            add, rem = stats.get(f, (0, 0))
            desc = _file_desc(f, notes, changed)
            what = _short(desc, 240) if desc else (_short(solution, 160) if solution else "")
            log.info("structure review 수정: %s%s%s", f, f" (+{add} −{rem})" if f in stats else " (new / deleted)",
                     (f" — {what}" if what else "") + (f" … diff {lines} lines" if lines else ""))
        if diff:
            log.debug("structure review diff:\n%s", diff[:6000])

    @staticmethod
    def _unparsable_poms(ws: Any, changed: list[str]) -> list[str]:
        """바뀐 pom.xml 중 다시 파싱되지 않는 파일을 반환한다. 결과가 있으면 호출한 쪽이 되돌린다."""
        from sbmig.analyze.pom_parser import parse_pom_text

        out: list[str] = []
        for f in changed:
            if PurePosixPath(f).name != "pom.xml":
                continue
            try:
                if not ws.exists(f):
                    continue
                parse_pom_text(ws.read(f), f)
            except Exception as e:  # noqa: BLE001
                out.append(f"{f}: {_short(e, 160)}")
        return out

    @staticmethod
    def _restore(ws: Any, rv: StructureReview, note: str) -> None:
        """커밋되지 않은 변경을 모두 버린다. `rv.reverted=True`와 메모를 남긴다.
        되돌리기가 실패하면 그 사실을 메모에 덧붙인다."""
        try:
            ws.discard_uncommitted()
            rv.reverted = True
        except Exception as e:  # noqa: BLE001
            note += f" (restoring the working tree failed: {type(e).__name__}: {e})"
            log.warning("structure review: could not restore the working tree: %s", e)
        rv.note = note
        log.warning("structure review: %s", note)

    @staticmethod
    def _log_summary(rv: StructureReview) -> None:
        """검토 결과를 로그로 남긴다. 요약과 critical 이슈는 INFO, warning은 DEBUG다."""
        crit, warn = len(rv.critical), len(rv.issues) - len(rv.critical)
        edits = len(rv.answer.edits) if rv.answer is not None else 0
        kind = "edits" if edits else "script"
        if rv.script_result is not None:
            r = rv.script_result
            fix = f"{kind} {r.status}" + (", restored" if rv.reverted else f", {len(r.changed_files)} file(s) changed")
        elif edits or rv.script:
            fix = f"{kind} not applied"
        else:
            fix = "no fix"
        log.info("structure review: verdict=%s, %d critical, %d warning(s), %s, %d LLM call(s)%s", rv.verdict, crit,
                 warn, fix, rv.llm_usage.calls, f" — {rv.note}" if rv.note else "")
        for i in rv.critical:
            log.info("structure review critical: %s: %s", i.file or "(project)", _short(i.problem, 300))
        for i in rv.issues:
            if i.severity != "critical":
                log.debug("structure review warning: %s: %s", i.file or "(project)", _short(i.problem, 300))
