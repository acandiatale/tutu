"""한국어 주석 규약(docs/v2/CODE-COMMENTS-KO.md)이 지켜지는지: ``sbmig/`` 의 모든 모듈 docstring 과 공개 최상위
클래스·함수 docstring 에 한글이 있어야 한다. 새 모듈 / 새 공개 함수를 만들면 한국어 한 줄은 꼭 달아야 이 테스트가 통과한다.

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
