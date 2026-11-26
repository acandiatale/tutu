"""고치는 중인 빌드 오류와 관련된 권고를 찾아, 수정 프롬프트에 넣을 짧은 안내 줄로 만드는 모듈.

The fix loop calls ``hints_for`` per case while it builds the LLM prompt (``FixContext.advisory_hints``).
Only the note files are read and nothing raises; a problem drops that advisory's hint only.

    hints_for(errors, findings, profile=None, *, notes_root=None) -> (hints, advisory_ids)

A finding matches a cluster when any error's message / symbol / location / raw text / artifact mentions
  (a) an import package of the finding's library (`LibraryRule.imports` in `sbmig.analyze.libraries`, e.g.
      `com.netflix.hystrix`), matched on a package-segment boundary,
  (b) one of the finding's as-is artifacts (`g:a`), or
  (c) the replacement artifact (`g:a` or its artifactId, e.g. a missing `spring-cloud-starter-circuitbreaker-resilience4j`).

One hint per matched finding (order of `findings`):
    "<library> (<status>): replace with <replacement>[ (<replacement_version>)]. <followup>"
(≤ HINT_MAX chars; the title stands in when there is no follow-up) plus, when the advisory references a note
(`notes: knowledge/notes/<file>.md`, carried on the finding as "see <path>"), an excerpt of that note's section:
a `## fix-loop: <library>` heading wins (convention for loop guidance written for this purpose), otherwise the
first heading that mentions the library (≤ NOTE_EXCERPT_MAX chars).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any, Optional

from sbmig.core.models import AdvisoryFinding, BuildError

log = logging.getLogger("sbmig.advisory.hints")

HINT_MAX = 500
NOTE_EXCERPT_MAX = 600
RAW_MAX = 4000                      # 오류 원문의 길이 상한. javac 오류 블록은 다 담고 긴 의존성 출력은 자른다
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


# ---------------------------------------------------------------------------
# 라이브러리 규칙
# ---------------------------------------------------------------------------
@cache
def library_packages(library: str) -> tuple[str, ...]:
    """라이브러리의 import 패키지 접두어(``LibraryRule.imports``)를 반환한다.
    모르는 이름이면 빈 튜플을 반환한다."""
    try:
        from sbmig.analyze.libraries import LIBRARY_RULES
    except Exception as e:  # noqa: BLE001  # pragma: no cover - analyze is ours
        log.debug("LIBRARY_RULES unavailable: %s", e)
        return ()
    for rule in LIBRARY_RULES:
        if rule.name == library:
            return tuple(rule.imports)
    return ()


def _pkg_re(prefix: str) -> re.Pattern:
    # 패키지 경계에서만 맞춘다. 이름이 비슷한 다른 패키지에 힌트가 붙으면 안 된다
    # segment boundary on both sides: `com.netflix.hystrix` matches `com.netflix.hystrix.HystrixCommand` and
    # "package com.netflix.hystrix does not exist", not `com.netflix.hystrixfoo` or `x.com.netflix.hystrix`
    return re.compile(r"(?<![\w.])" + re.escape(prefix) + r"(?![\w])")


def _error_text(e: BuildError) -> str:
    """권고와 맞춰 볼 오류 텍스트를 만든다. 메시지, 심볼, 위치, 원문, 아티팩트를 이어 붙인다.
    원문은 오류당 RAW_MAX자까지만 쓴다."""
    parts = [e.message or "", e.symbol or "", e.location or "", (e.raw or "")[:RAW_MAX]]
    if e.artifact is not None:
        parts.append(f"{e.artifact.group_id}:{e.artifact.artifact_id}")
    return "\n".join(p for p in parts if p)


def _replacement_keys(replacement: Optional[str]) -> list[str]:
    """대체 아티팩트로 맞춰 볼 키를 만든다. ``g:a``와 artifactId를 쓴다.
    artifactId는 대시가 들어 있어 다른 단어와 헷갈리지 않을 때만 쓴다."""
    if not replacement:
        return []
    r = replacement.strip()
    out = [r] if ":" in r else []
    aid = r.split(":")[1] if ":" in r else r
    if aid and "-" in aid:
        out.append(aid)
    return out


def _matches(f: AdvisoryFinding, text: str) -> bool:
    """오류 텍스트가 이 권고와 관련 있는지 확인한다.
    라이브러리의 import 패키지, as-is 아티팩트, 대체 아티팩트 중 하나라도 나오면 관련 있다고 본다."""
    for prefix in library_packages(f.library):
        if prefix and _pkg_re(prefix).search(text):
            return True
    for ga in f.artifacts:
        if ga and ga in text:
            return True
    for key in _replacement_keys(f.replacement):
        if re.search(r"(?<![\w.\-])" + re.escape(key) + r"(?![\w\-])", text):
            return True
    return False


# ---------------------------------------------------------------------------
# 힌트 문구
# ---------------------------------------------------------------------------
def _cut(text: str, limit: int) -> str:
    """공백을 정리하고 ``limit``자로 자른다. 되도록 문장 끝(". ")에서 자르고, 그럴 수 없으면 끝에 …을 붙인다."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    dot = head.rfind(". ")
    if dot >= limit // 2:
        return head[: dot + 1]
    return head.rstrip() + "…"


def _note_paths(f: AdvisoryFinding) -> list[str]:
    """권고 notes에 있는 ``see <파일>.md`` 경로를 반환한다."""
    out = []
    for n in f.notes:
        m = re.match(r"\s*see\s+(\S+\.md)\b", n or "")
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _resolve(path: str, notes_root: Optional[Path]) -> Optional[Path]:
    """노트 파일을 찾는다. 상대 경로면 ``notes_root``, 이 저장소 루트, 현재 디렉토리 순서로 찾는다.
    못 찾으면 None을 반환한다."""
    p = Path(path)
    if p.is_absolute():
        cands = [p]
    else:
        cands = ([Path(notes_root) / p] if notes_root else []) + [_REPO_ROOT / p, p]
    return next((c for c in cands if c.is_file()), None)


def note_section(text: str, library: str) -> tuple[str, str]:
    """노트에서 ``library``에 관한 절을 찾아 (제목, 본문)으로 반환한다.
    ``## fix-loop: <library>`` 절이 있으면 그 절이고, 없으면 라이브러리 이름이 들어간 첫 소제목이다.
    둘 다 없으면 ("", "")를 반환한다.

    The name must appear as a whole word; a level-1 heading is never picked by name."""
    lines = text.replace("\r\n", "\n").split("\n")
    heads = [(i, len(m.group(1)), m.group(2)) for i, ln in enumerate(lines) if (m := _HEADING_RE.match(ln))]
    lib = re.escape(library)
    loop_re = re.compile(rf"(?i)^fix-loop:\s*{lib}\s*$")
    word_re = re.compile(rf"(?i)(?<![\w\-]){lib}(?![\w\-])")
    pick = next((h for h in heads if loop_re.match(h[2])), None) or next(
        (h for h in heads if h[1] > 1 and word_re.search(h[2])), None)
    if pick is None:
        return "", ""
    i, level, title = pick
    end = next((j for j, lv, _ in heads if j > i and lv <= level), len(lines))
    return title, "\n".join(lines[i + 1:end]).strip()


def _note_excerpt(f: AdvisoryFinding, notes_root: Optional[Path]) -> str:
    """힌트 뒤에 붙일 노트 발췌(`` Note (<파일> § <제목>): …``)를 만든다. 읽지 못하면 발췌 없이 반환한다."""
    for rel in _note_paths(f):
        try:
            p = _resolve(rel, notes_root)
            if p is None:
                continue
            from sbmig.fix.context import strip_front_matter
            title, body = note_section(strip_front_matter(p.read_text(encoding="utf-8")), f.library)
        except Exception as e:  # noqa: BLE001 노트를 읽지 못하면 발췌 없이 힌트만 쓴다
            log.debug("advisory %s: note %s unreadable: %s", f.advisory_id, rel, e)
            continue
        if body:
            return f" Note ({Path(rel).name} § {title}): {_cut(body, NOTE_EXCERPT_MAX)}"
    return ""


def hint_text(f: AdvisoryFinding, notes_root: Optional[Path] = None) -> str:
    """권고 하나의 힌트 줄을 만든다. 본문은 HINT_MAX자에서 자르고, 뒤에 노트 발췌를 붙인다.
    형식은 ``<라이브러리> (<상태>): replace with <대체품> (<버전>). <후속 작업>``이다."""
    head = f"{f.library} ({f.status})"
    if f.replacement:
        head += f": replace with {f.replacement}"
        if f.replacement_version:
            head += f" ({f.replacement_version})"
    head += "."
    tail = f.followup.strip() or f.title.strip()
    main = _cut(f"{head} {tail}".strip(), HINT_MAX)
    return main + _note_excerpt(f, notes_root)


# ---------------------------------------------------------------------------
# 공개 함수
# ---------------------------------------------------------------------------
def hints_for(errors: Iterable[BuildError], findings: Optional[Iterable[AdvisoryFinding]], profile: Any = None, *,
              notes_root: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """이 오류들과 관련된 권고를 찾아 (힌트 줄 목록, 권고 id 목록)을 반환한다. 순서는 ``findings`` 순서다.
    예외를 던지지 않는다. 권고 하나가 실패하면 그 권고만 빠지고, 전체가 실패하면 빈 목록이다.

    `profile` is accepted for the contract and unused; `notes_root` = base directory of the advisories'
    `notes` paths (the config's repo root; defaults to this checkout)."""
    hints: list[str] = []
    ids: list[str] = []
    try:
        texts = [_error_text(e) for e in (errors or [])]
        if not texts:
            return [], []
        blob = "\n".join(texts)
        for f in findings or []:
            if f.advisory_id in ids:
                continue
            try:
                if not _matches(f, blob):
                    continue
                hints.append(hint_text(f, notes_root))
                ids.append(f.advisory_id)
            except Exception as e:  # noqa: BLE001 권고 하나가 깨져도 나머지 힌트는 그대로 만든다
                log.debug("advisory hint for %s failed: %s", getattr(f, "advisory_id", "?"), e)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - 힌트 때문에 수정 루프가 멈추지 않게 한다
        log.warning("advisory hints failed: %s", e)
        return [], []
    return hints, ids
