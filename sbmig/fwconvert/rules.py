"""전환 규칙 파일을 읽어 프레임워크 이름을 채워 넣는다. 회사 값은 덧씌울 값으로 받는다.

Contract: docs/FW-CONVERT.md section 1. The vocabulary is ``knowledge/fwconvert/rules.yml``; ``{Fw}`` / ``{fw}`` in
any key or string value become the framework's name / its lower-case form. The overlay has the same shape as the
file: a dict is merged key by key, a list is extended (no duplicates), anything else replaces the value. Modules of
this package read rules only through ``ConvertRules`` and never open the file or the config themselves, so the place
company values come from can change without touching them."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

RULES_FILE = "knowledge/fwconvert/rules.yml"


class RulesError(ValueError):
    """규칙을 만들 수 없을 때 던지는 예외. 프레임워크 이름이 없거나 규칙 파일을 읽지 못한 경우다."""


@dataclass
class ConvertRules:
    """프레임워크 이름을 채운 전환 규칙. ``get("activities.kinds")``처럼 점으로 이은 경로로 읽는다."""
    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, path: str, default: Any = None) -> Any:
        """경로의 값을 반환한다. 없으면 ``default``를 반환한다. 반환한 값을 고쳐도 규칙은 바뀌지 않는다."""
        cur: Any = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return copy.deepcopy(cur)

    def strs(self, path: str) -> list[str]:
        """경로의 값을 문자열 목록으로 반환한다. 없으면 빈 목록이다."""
        v = self.get(path, [])
        return [str(x) for x in v] if isinstance(v, (list, tuple)) else []

    def text(self, path: str, default: str = "") -> str:
        """경로의 값을 문자열로 반환한다. 없으면 ``default``다."""
        v = self.get(path, default)
        return default if v is None else str(v)


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(over, list):
        return base + [x for x in over if x not in base]
    return copy.deepcopy(over)


def _expand(value: Any, name: str) -> Any:
    if isinstance(value, str):
        return value.replace("{Fw}", name).replace("{fw}", name.lower())
    if isinstance(value, dict):
        return {_expand(k, name): _expand(v, name) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, name) for v in value]
    return value


def rules_path(cfg: Any = None) -> Path:
    """규칙 파일의 경로를 반환한다. 설정이 있으면 그 저장소 루트를 기준으로 한다."""
    if cfg is not None and hasattr(cfg, "resolve_path"):
        return Path(cfg.resolve_path(RULES_FILE))
    from sbmig.core.config import PACKAGE_ROOT
    return PACKAGE_ROOT / RULES_FILE


def load_rules(name: str, overlay: Optional[dict[str, Any]] = None, path: Optional[str | Path] = None) -> ConvertRules:
    """규칙 파일에 ``overlay``를 덧씌우고 프레임워크 이름을 채워 반환한다.
    이름이 비어 있거나 파일을 읽지 못하면 ``RulesError``를 던진다."""
    fw = str(name or "").strip()
    if not fw:
        raise RulesError("the framework's name is empty: set framework.name or framework.artifacts")
    p = Path(path) if path else rules_path()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise RulesError(f"cannot read the conversion rules {p}: {e}") from e
    if not isinstance(data, dict):
        raise RulesError(f"the conversion rules {p} are not a mapping")
    if overlay:
        data = _merge(data, overlay)
    return ConvertRules(name=fw, data=_expand(data, fw))
