"""대상 Boot 버전에 맞는 Spring Cloud 릴리스 트레인을 찾는 모듈.
재구성(``sbmig.restructure.restructurer._cloud_bom``)이 ``spring-cloud-dependencies`` BOM을 import해야
하는데 부모 체인에 ``spring-cloud.version`` 같은 속성이 없을 때 쓴다.
- Boot와 Cloud 버전의 짝은 ``knowledge/config/spring_cloud_bom.yml``의 ``pairs``에서만 바꾼다
- 파일이 없거나 깨졌으면 None을 반환한다. 그때 재구성은 인수인계 후속 작업만 남긴다. 예외를 던지지 않는다

Rows look like `{boot: "4.1", cloud: "2025.1.4"}`, matched by the first two segments of
`target.spring_boot_version`. Contract: docs/INTERFACES.md "v2.1t"."""
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
        except Exception as e:  # noqa: BLE001 - 설정이 깨졌으면 패키지 안의 파일을 쓴다
            log.debug("spring_cloud_bom path: %s", e)
    from sbmig.core.config import PACKAGE_ROOT
    return PACKAGE_ROOT / CLOUD_BOM_FILE


def cloud_pairs(cfg: Any = None) -> dict[str, str]:
    """Boot 버전 앞 두 자리별 Cloud 트레인 표를 반환한다.
    먼저 나온 항목이 우선한다. 읽지 못하면 빈 dict를 반환한다.

    Shape: `{"4.1": "2025.1.4", …}`."""
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
    """Boot 버전의 앞 두 자리에 맞는 Spring Cloud 트레인을 반환한다. 짝이 없으면 None을 반환한다.
    ``boot_version``이 없으면 ``target.spring_boot_version``을 쓴다. 예외를 던지지 않는다."""
    try:
        v = boot_version or str(getattr(getattr(cfg, "target", None), "spring_boot_version", "") or "")
        line = ".".join(v.strip().split(".")[:2])
        return cloud_pairs(cfg).get(line) if line else None
    except Exception as e:  # noqa: BLE001 - 짝이 없는 것으로 보고 재구성을 실패시키지 않는다
        log.debug("cloud pairing failed: %s: %s", type(e).__name__, e)
        return None


__all__ = ["CLOUD_BOM", "CLOUD_BOM_FILE", "CLOUD_GROUP", "cloud_pairs", "cloud_release_for"]
