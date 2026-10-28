"""이번 실행용 ``rewrite.yml`` 합성 — 변환(OpenRewrite 1회)에서 돌릴 레시피 id 들을 합성 레시피 하나로 묶는다.
흐름상 위치: ``OpenRewriteRunner.compose`` 가 부르고, 결과는 ``logs/rewrite-<n>.yml`` 로 저장돼 플러그인의
``-Drewrite.configLocation`` 이 된다. 결정: 합성 레시피 ``com.company.migrate.Run`` 의 ``recipeList`` = 요청한
id 를 요청 순서대로(중복 제거), 활성화는 이것 하나뿐 → 적용 순서가 결정적이다. v2 계획은 OpenRewrite 레시피만
쓰므로 보통 합성 레시피 하나뿐이다(프로젝트 / KB 레시피 파일 없음). 호출자가 준 ``extra_files`` 의 선언형
레시피는 ``{{SPRING_BOOT_VERSION}}`` 등을 채워 앞에 붙인다.

Compose the run-specific ``rewrite.yml``.

One composite recipe ``com.company.migrate.Run`` whose ``recipeList`` is the requested recipe ids in order; only that
composite is activated, so ordering is deterministic. Declarative recipe files a caller passes (``extra_files``, a
``RecipeRef.file``) are concatenated in front of it after templating ``{{SPRING_BOOT_VERSION}}``,
``{{JAVA_VERSION}}``, ``{{SPRING_FRAMEWORK_VERSION}}``. v2 plans OpenRewrite recipes only, so a planned run is the
composite alone (no project or KB recipe files).


"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml

from sbmig.core.config import TargetConfig
from sbmig.core.models import RecipeRef

log = logging.getLogger("sbmig.rewrite")

COMPOSITE_RECIPE_ID = "com.company.migrate.Run"
RECIPE_TYPE = "specs.openrewrite.org/v1beta/recipe"
_DOC_SEP_RE = re.compile(r"^---[ \t]*(?:#.*)?$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")


class RecipeCompositionError(ValueError):
    """자기 파일을 가리키는 레시피가 합성된 어느 파일에도 정의돼 있지 않을 때의 오류(프로그래밍 / 설정 오류).
    v2 의 RecipeRef 에는 파일 필드가 없어 지금 코드는 이 예외를 내지 않는다 — 공개 API 호환용으로 남아 있다.

    A requested recipe that names its own file (`RecipeRef.file`) is not defined in any composed file (a
    programming / config error)."""

    def __init__(self, missing: Sequence[str], searched: Sequence[str]):
        self.missing = list(missing)
        self.searched = list(searched)
        super().__init__(
            "recipe id(s) not defined in the composed rewrite config: " + ", ".join(self.missing)
            + (" (searched: " + ", ".join(self.searched) + ")" if self.searched else ""))


def template_values(target: TargetConfig | None) -> dict[str, str]:
    """레시피 파일의 ``{{…}}`` 자리표시자 값: 목표 Spring Boot / Java / Spring
    Framework 버전(``target.*``)."""
    t = target or TargetConfig()
    return {
        "SPRING_BOOT_VERSION": t.spring_boot_version,
        "JAVA_VERSION": t.java_version,
        "SPRING_FRAMEWORK_VERSION": t.spring_framework_version,
    }


def render_template(text: str, values: dict[str, str], *, source: str = "") -> str:
    """``{{NAME}}`` 을 값으로 바꾼다. 모르는 자리표시자는 경고 한 줄과 함께 그대로 둔다."""
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        log.warning("unknown placeholder {{%s}} in %s left as-is", key, source or "recipe file")
        return m.group(0)
    return _PLACEHOLDER_RE.sub(repl, text)


def split_documents(text: str) -> list[str]:
    """여러 문서짜리 YAML 을 ``---`` 줄로 나눈다(주석만 있거나 빈 조각은 버림).

    Split a multi-document YAML text on ``---`` lines, dropping comment-only/empty chunks."""
    out = []
    for chunk in _DOC_SEP_RE.split(text):
        body = "\n".join(ln for ln in chunk.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))
        if body.strip():
            out.append(chunk.strip("\n"))
    return out


def recipe_documents(text: str) -> dict[str, dict]:
    """텍스트 안의 모든 ``specs.openrewrite.org/v1beta/recipe`` 문서를 이름 → 파싱된 문서로.

    name → parsed document for every ``specs.openrewrite.org/v1beta/recipe`` document in ``text``."""
    out: dict[str, dict] = {}
    for doc in yaml.safe_load_all(text):
        if isinstance(doc, dict) and doc.get("type") == RECIPE_TYPE and doc.get("name"):
            out[str(doc["name"])] = doc
    return out


def recipe_list_ids(doc: dict) -> list[str]:
    """선언형 레시피의 ``recipeList`` 가 가리키는 레시피 id 들(문자열 항목 또는 키 하나짜리 매핑).

    Recipe ids referenced by a declarative recipe's ``recipeList`` (string items or single-key mappings)."""
    ids: list[str] = []
    for item in doc.get("recipeList") or []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item:
            ids.append(str(next(iter(item))))
    return ids


def _source_files(extra_files: Iterable[Path], recipes: Sequence[RecipeRef]) -> list[Path]:
    """합성 파일에 넣을 선언형 레시피 파일들(중복 제거). v2 계획은 OpenRewrite 레시피 id 만 쓰므로 호출자가 준
    ``extra_files`` 뿐이고 ``recipes`` 는 보지 않는다.

    Declarative recipe files to inline (v2 plans name OpenRewrite recipes only; `extra_files` stays for callers)."""
    files: list[Path] = [Path(p) for p in extra_files]
    seen: set[str] = set()
    out = []
    for f in files:
        key = str(f.resolve()) if f.exists() else str(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def compose_rewrite_yaml(recipes: Sequence[RecipeRef], *, extra_files: Iterable[Path | str] = (),
                         target: TargetConfig | None = None,
                         composite_id: str = COMPOSITE_RECIPE_ID) -> str:
    """rewrite.yml 전체 텍스트를 만든다: 머리 주석(출처 파일), ``extra_files`` 의 레시피 문서들, 마지막에 합성
    레시피. 합성 id 와 같은 이름의 문서는 무시, 같은 이름이 두 번이면 뒤의 것이 이긴다(경고). 없는
    ``extra_files`` 파일은 FileNotFoundError, 깨진 YAML 은 ValueError.

    Return the full rewrite.yml text. Raises ``RecipeCompositionError`` when a recipe with its own `file` is not
    defined there (nor in another composed file) and ``FileNotFoundError`` for a given (extra / RecipeRef.file) file
    that does not exist."""
    values = template_values(target)
    files = _source_files([Path(p) for p in extra_files], recipes)
    by_name: dict[str, tuple[str, str]] = {}           # name → (doc text, source)
    other_docs: list[tuple[str, str]] = []
    for f in files:
        if not f.exists():
            raise FileNotFoundError(f"recipe file not found: {f}")
        text = render_template(f.read_text(encoding="utf-8"), values, source=str(f))
        for chunk in split_documents(text):
            try:
                doc = yaml.safe_load(chunk)
            except yaml.YAMLError as exc:
                raise ValueError(f"invalid YAML in recipe file {f}: {exc}") from exc
            if isinstance(doc, dict) and doc.get("name"):
                name = str(doc["name"])
                if name == composite_id:
                    log.warning("ignoring recipe %s from %s: reserved composite id", name, f)
                    continue
                if name in by_name:
                    log.warning("recipe %s defined in %s overrides the definition from %s", name, f, by_name[name][1])
                by_name[name] = (chunk, str(f))
            elif doc is not None:
                other_docs.append((chunk, str(f)))

    ids: list[str] = []
    for r in recipes:
        if r.id not in ids:
            ids.append(r.id)

    parts = [f"# generated by sbmig — composite {composite_id}; sources:"]
    parts += [f"#   {f}" for f in files]
    body = [chunk for chunk, _src in by_name.values()] + [chunk for chunk, _src in other_docs]
    composite = {
        "type": RECIPE_TYPE,
        "name": composite_id,
        "displayName": "sbmig migration run",
        "description": "Generated by sbmig: runs the requested recipes in order.",
        "recipeList": ids,
    }
    body.append(yaml.safe_dump(composite, sort_keys=False, default_flow_style=False).rstrip("\n"))
    return "\n".join(parts) + "\n" + "".join(f"---\n{chunk}\n" for chunk in body)
