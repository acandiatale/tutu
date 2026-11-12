"""한국어 주석 규약(docs/v2/CODE-COMMENTS-KO.md) 검사.
- ``sbmig/``의 모든 모듈 docstring과 공개 최상위 클래스·함수 docstring에 한국어가 있어야 한다
- 한국어 주석 줄에는 줄표, 화살표, 굵게, 버전 태그, 날짜, 띄어 쓴 조사가 없어야 한다

Every module under ``sbmig/`` carries a Korean handover docstring (Korean first, then the English original) and every
public top-level class / function has Korean in its docstring (docs/v2/CODE-COMMENTS-KO.md, 2026-10-28).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

HANGUL = re.compile(r"[가-힣]")
ROOT = Path(__file__).resolve().parents[2]
MODULES = sorted(p for p in (ROOT / "sbmig").rglob("*.py") if "__pycache__" not in p.parts)


def _module_doc(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_module_docstring_starts_in_korean(path: Path):
    doc = _module_doc(path)
    assert doc.strip(), "module docstring missing"
    first = next(line for line in doc.splitlines() if line.strip())
    assert HANGUL.search(first), "module docstring must start with the Korean block (English original after it)"


def test_every_public_definition_has_korean_docstring():
    missing: list[str] = []
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith("_"):
                continue
            doc = ast.get_docstring(node) or ""
            if not HANGUL.search(doc):
                missing.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {node.name}")
    assert not missing, "public definitions without a Korean docstring:\n" + "\n".join(missing)


# 문체 검사: 한국어 주석은 개발자가 직접 쓴 주석처럼 쓴다(docs/v2/CODE-COMMENTS-KO.md 3절)
# The Korean parts of docstrings and comments carry no em dash, arrow, bold, version tag or date, and no particle written
# apart from the word before it (the business team read the first Korean comments as generated text, 2026-11-12).
# English parts are not checked: they are the working record and keep their history.

_CODE_SPAN = re.compile(r"``[^`\n]*``|`[^`\n]*`")
_QUOTED = re.compile(r'"[^"\n]*(?:\n[^"\n]*)?"|“[^”\n]*(?:\n[^”\n]*)?”')
_ASIS = re.compile(r"\b(as-is|to-be)\b", re.I)
_EN_WORD = re.compile(r"(?<![A-Za-z_./-])(?:[A-Z][a-z]{2,}|[a-z]{3,})(?![A-Za-z_(]|[./-][A-Za-z])")
_PARTICLE = re.compile(r"[A-Za-z0-9_`\]>] (은|는|가|을|를|의|에|로|으로|와|과|도|만|에서)(?=[\s.,)]|$)")
_TELLS = {
    "줄표(—)": re.compile("—|–"),
    "화살표(→)": re.compile("→|⇒|←"),
    "굵게(**)": re.compile(r"\*\*"),
    "버전 태그": re.compile(r"\bv2\.\d"),
    "날짜": re.compile(r"20\d\d-\d\d-\d\d"),
}


def _blank(m: "re.Match[str]") -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def _korean_parts(lines: list[str]) -> list[list[tuple[str, str]]]:
    """줄 묶음(docstring 문단, 이어진 주석 줄)에서 줄마다 한국어로 쓴 부분을 (원문, 코드 표기를 지운 글)로 반환한다.

    Double-quoted spans are blanked first (a quote may run over one line break), code spans for the second text. A line is
    split at ` / ` ("한국어. / English."); a part is Korean when it has more Korean words than English words. A
    Korean-looking line between two English lines of the block is a Korean term inside an English paragraph: nothing."""
    quoted = _QUOTED.sub(_blank, "\n".join(lines))
    bare = _ASIS.sub(_blank, _CODE_SPAN.sub(_blank, quoted))
    found: list[list[tuple[str, str]]] = []
    english: list[int] = []
    for q, b in zip(quoted.split("\n"), bare.split("\n")):
        parts, en_line, pos = [], 0, 0
        for start, end in [m.span() for m in re.finditer(r" / ", b)] + [(len(b), len(b))]:
            seg = b[pos:start]
            ko = sum(1 for w in seg.split() if HANGUL.search(w))
            en = len(_EN_WORD.findall(seg))
            en_line += en
            if ko >= 1 and ko > en:
                parts.append((q[pos:start], seg))
            pos = end
        found.append(parts)
        english.append(en_line)
    out = []
    for i, parts in enumerate(found):
        if parts:
            has_prev = i > 0 and bool(lines[i - 1].strip())
            has_next = i + 1 < len(lines) and bool(lines[i + 1].strip())
            prev_en = has_prev and not found[i - 1]
            next_en = has_next and not found[i + 1]
            if (prev_en and next_en) or (english[i] >= 2 and ((prev_en and not has_next) or (next_en and not has_prev))):
                parts = []
        out.append(parts)
    return out


def _korean_comment_parts(path: Path):
    """파일의 docstring과 주석에서 (줄 번호, 원문, 코드 표기를 지운 글)을 차례로 낸다."""
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            first, block, start = body[0].value.lineno, [], 0
            for n, line in enumerate(body[0].value.value.split("\n") + [""]):
                if line.strip():
                    if not block:
                        start = n
                    block.append(line)
                    continue
                for k, parts in enumerate(_korean_parts(block)):
                    for text, bare in parts:
                        yield first + start + k, text.strip(), bare
                block = []
    comments = [(t.start[0], t.start[1], t.string, not t.line[:t.start[1]].strip())
                for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.COMMENT]
    i = 0
    while i < len(comments):
        j = i
        while comments[i][3] and j + 1 < len(comments) and comments[j + 1][3] \
                and comments[j + 1][0] == comments[j][0] + 1 and comments[j + 1][1] == comments[i][1]:
            j += 1
        block = [re.sub(r"^#+:? ?", "", c[2]) for c in comments[i:j + 1]]
        for k, parts in enumerate(_korean_parts(block)):
            for text, bare in parts:
                yield comments[i + k][0], text.strip(), bare
        i = j + 1


def test_korean_comment_lines_read_like_hand_written_comments():
    bad: list[str] = []
    for path in MODULES:
        for lineno, text, bare in _korean_comment_parts(path):
            hits = [name for name, rx in _TELLS.items() if rx.search(bare)]
            if _PARTICLE.search(text):
                hits.append("띄어 쓴 조사")
            if hits:
                bad.append(f"{path.relative_to(ROOT).as_posix()}:{lineno} [{', '.join(hits)}] {text[:90]}")
    assert not bad, (f"{len(bad)} Korean comment line(s) break docs/v2/CODE-COMMENTS-KO.md section 3:\n"
                     + "\n".join(bad[:60]))


def test_korean_parts_leave_english_alone():
    # an English sentence that quotes Korean, and a Korean term inside an English paragraph, are not Korean parts
    assert _korean_parts(['the log line says "KB 탐색: 저장된', '스크립트 없음 → LLM 에 새 수정 요청" and goes on']) == [[], []]
    assert _korean_parts(["The fixer's answer.", "v2.1 (2026-10-27): 원인 `analysis` · 해결 방법", "stored for reuse."])[1] == []
    korean, english = _korean_parts(["시도 번호를 반환한다. / The attempt number."])[0], None
    assert [t.strip() for t, _ in korean] == ["시도 번호를 반환한다."]
    assert _korean_parts(["KB 에 기록한다"])[0][0][0] == "KB 에 기록한다"
