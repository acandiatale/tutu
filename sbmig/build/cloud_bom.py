"""대상 Boot 버전에 맞는 Spring Cloud 릴리스 트레인을 찾는 모듈.
재구성(``sbmig.restructure.restructurer._cloud_bom``)이 ``spring-cloud-dependencies`` BOM을 import해야
하는데 부모 체인에 ``spring-cloud.version`` 같은 속성이 없을 때 쓴다.
- Boot와 Cloud 버전의 짝은 ``knowledge/config/spring_cloud_bom.yml``의 ``pairs``에서만 바꾼다
- 파일이 없거나 깨졌으면 None을 반환하고, 재구성은 인수인계 후속 작업만 남긴다. 예외를 던지지 않는다

The Spring Cloud release train paired with the target Spring Boot line (v2.1t): `pairs` of
`knowledge/config/spring_cloud_bom.yml` (`{boot: "4.1", cloud: "2025.1.4"}` …), matched by the first two segments of
`target.spring_boot_version`. None when the file is missing / broken or names no pairing. Never raises."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("sbmig.build.cloud_bom")

CLOUD_BOM_FILE = "knowledge/config/spring_cloud_bom.yml"
CLOUD_GROUP = "org.springframework.cloud"
CLOUD_BOM = "spring-cloud-dependencies"


def _path(cfg: Any) -> Path:
    if cfg is not None and hasattr(cfg, "resolve_path"):
        try:
            return Path(cfg.resolve_path(CLOUD_BOM_FILE))
        except Exception as e:  # noqa: BLE001 - a broken config falls back to the package copy
            log.debug("spring_cloud_bom path: %s", e)
    from sbmig.core.config import PACKAGE_ROOT
    return PACKAGE_ROOT / CLOUD_BOM_FILE


def cloud_pairs(cfg: Any = None) -> dict[str, str]:
    """Boot 버전 앞 두 자리별 Cloud 트레인 표를 반환한다.
    먼저 나온 항목이 우선하고, 읽지 못하면 빈 dict를 반환한다.

    `{"4.1": "2025.1.4", …}` from the YAML (first entry wins); {} when unreadable."""
    path = _path(cfg)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("spring_cloud_bom not loaded from %s: %s", path, e)
        return {}
    out: dict[str, str] = {}
    for row in (data.get("pairs") if isinstance(data, dict) else None) or []:
        if isinstance(row, dict) and row.get("boot") and row.get("cloud"):
            out.setdefault(str(row["boot"]).strip(), str(row["cloud"]).strip())
    return out


def cloud_release_for(cfg: Any = None, boot_version: str | None = None) -> str | None:
    """대상 Boot 버전의 앞 두 자리에 맞는 Spring Cloud 트레인을 반환한다. 없으면 None을 반환한다.

    The Spring Cloud train paired with `boot_version` (default `cfg.target.spring_boot_version`), by its first two
    segments; None when no pairing. Never raises."""
    try:
        v = boot_version or str(getattr(getattr(cfg, "target", None), "spring_boot_version", "") or "")
        line = ".".join(v.strip().split(".")[:2])
        return cloud_pairs(cfg).get(line) if line else None
    except Exception as e:  # noqa: BLE001 - no pairing, never a failed restructure
        log.debug("cloud pairing failed: %s: %s", type(e).__name__, e)
        return None


__all__ = ["CLOUD_BOM", "CLOUD_BOM_FILE", "CLOUD_GROUP", "cloud_pairs", "cloud_release_for"]
