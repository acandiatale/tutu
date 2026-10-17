"""Edge-case handlers (encoding, BOM, enforcer, -Werror, carry-over managed versions …).

Implemented in workstream G1; registered by `default_handlers()` in pom_handlers.py BEFORE the generic handlers.
"""
from __future__ import annotations

from typing import Optional

from sbmig.core.config import Config

from .base import Handler


def edge_handlers(cfg: Optional[Config] = None) -> list[Handler]:
    return []
