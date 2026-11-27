"""이번 실행에 쓸 ``rewrite.yml``을 만드는 모듈. 실행할 레시피 id를 합성 레시피 하나로 묶는다.

Placeholders: ``{{SPRING_BOOT_VERSION}}``, ``{{JAVA_VERSION}}``, ``{{SPRING_FRAMEWORK_VERSION}}``
(``template_values``). No KB recipe files.

``OpenRewriteRunner.compose`` calls it; the result is saved as ``logs/rewrite-<n>.yml`` and becomes the
plugin's ``-Drewrite.configLocation``. The ``recipeList`` of the composite ``com.company.migrate.Run`` holds
the requested ids in the requested order, without duplicates. Only the composite is activated, so the order
of application never changes. Declarative recipes from ``extra_files`` go first, placeholders filled. A recipe
whose catalog entry names a ``file`` (the Hystrix conversion) reaches this module through ``extra_files``,
passed by the runner.
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
    """자기 파일을 지정한 레시피가 합성한 어느 파일에도 정의되어 있지 않을 때 던지는 예외.
    지금 코드는 던지지 않는다. 공개 API 호환 때문에 남겨 둔다."""

    def __init__(self, missing: Sequence[str], searched: Sequence[str]):
        self.missing = list(missing)
        self.searched = list(searched)
        super().__init__(
            "recipe id(s) not defined in the composed rewrite config: " + ", ".join(self.missing)
            + (" (searched: " + ", ".join(self.searched) + ")" if self.searched else ""))


def template_values(target: TargetConfig | None) -> dict[str, str]:
    """레시피 파일의 ``{{…}}`` 플레이스홀더에 넣을 값을 반환한다.
    목표 Spring Boot, Java, Spring Framework 버전이다."""
    t = target or TargetConfig()
    return {
        "SPRING_BOOT_VERSION": t.spring_boot_version,
        "JAVA_VERSION": t.java_version,
        "SPRING_FRAMEWORK_VERSION": t.spring_framework_version,
    }


def render_template(text: str, values: dict[str, str], *, source: str = "") -> str:
    """``{{NAME}}``을 값으로 바꾼다. 모르는 플레이스홀더는 경고 한 줄을 남기고 그대로 둔다."""
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        log.warning("unknown placeholder {{%s}} in %s left as-is", key, source or "recipe file")
        return m.group(0)
    return _PLACEHOLDER_RE.sub(repl, text)


def split_documents(text: str) -> list[str]:
    """여러 문서로 된 YAML을 ``---`` 줄로 나눈다. 주석만 있거나 빈 조각은 버린다."""
    out = []
    for chunk in _DOC_SEP_RE.split(text):
        body = "\n".join(ln for ln in chunk.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))
        if body.strip():
            out.append(chunk.strip("\n"))
    return out


def recipe_documents(text: str) -> dict[str, dict]:
    """텍스트 안의 ``specs.openrewrite.org/v1beta/recipe`` 문서를 모두 파싱해 이름별로 반환한다."""
    out: dict[str, dict] = {}
    for doc in yaml.safe_load_all(text):
        if isinstance(doc, dict) and doc.get("type") == RECIPE_TYPE and doc.get("name"):
            out[str(doc["name"])] = doc
    return out


def recipe_list_ids(doc: dict) -> list[str]:
    """선언형 레시피의 ``recipeList``가 가리키는 레시피 id 목록을 반환한다.
    항목은 문자열이거나 키가 하나인 mapping이다."""
    ids: list[str] = []
    for item in doc.get("recipeList") or []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item:
            ids.append(str(next(iter(item))))
    return ids


def _source_files(extra_files: Iterable[Path], recipes: Sequence[RecipeRef]) -> list[Path]:
    """합성 파일에 넣을 선언형 레시피 파일을 중복 없이 반환한다.
    호출자가 준 ``extra_files``만 담는다. 계획은 레시피 id만 쓰기 때문에 ``recipes``는 보지 않는다."""
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
    """rewrite.yml 전체 텍스트를 만든다.
    머리 주석, ``extra_files``의 레시피 문서, 합성 레시피 순서로 쓴다.
    합성 id와 같은 이름의 문서는 무시한다. 같은 이름이 두 번 나오면 경고를 남기고 뒤의 것을 쓴다.
    ``extra_files``의 파일이 없으면 FileNotFoundError, YAML이 깨졌으면 ValueError를 던진다."""
    values = template_values(target)
    files = _source_files([Path(p) for p in extra_files], recipes)
    by_name: dict[str, tuple[str, str]] = {}           # 이름별 (문서 텍스트, 출처 파일)
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
