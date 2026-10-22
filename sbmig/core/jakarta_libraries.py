"""Third-party libraries with a known jakarta.* release (knowledge/libraries/jakarta_libraries.yml).

Used by the handler `jakarta-library-floor` (raise the dependency to `version` after the javax → jakarta migration),
by `compose_suggestion` for X1 blocked items (the library behind a javax API jar) and by the report's javax API
inventory. Company additions go into the yml (knowledge, not code).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

JAKARTA_LIBRARIES_FILE = Path(__file__).resolve().parents[2] / "knowledge" / "libraries" / "jakarta_libraries.yml"


class JakartaLibrary(BaseModel):
    key: str                                  # groupId:artifactId (current coordinates)
    old_keys: list[str] = Field(default_factory=list)   # earlier coordinates (e.g. ch.simas.qlrm:qlrm) → relocated to key
    jakarta_since: str                        # first jakarta.* release
    version: str                              # release the handler sets (>= jakarta_since)
    classifier: Optional[str] = None          # jakarta flavour published as a classifier jar (Ehcache 3: `jakarta`);
                                              # the handler sets it on the bumped / relocated declaration
    requires_java: Optional[int] = None
    note: str = ""
    docs: list[str] = Field(default_factory=list)

    @property
    def group_id(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def artifact_id(self) -> str:
        return self.key.split(":", 1)[1]

    @property
    def all_keys(self) -> list[str]:
        """Current + earlier coordinates (`groupId:artifactId`)."""
        return [self.key, *self.old_keys]


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[JakartaLibrary, ...]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError:
        return ()
    return tuple(JakartaLibrary.model_validate(e) for e in (doc.get("libraries") or []))


def jakarta_libraries(path: Optional[str | Path] = None) -> tuple[JakartaLibrary, ...]:
    return _load(str(path or JAKARTA_LIBRARIES_FILE))


def jakarta_library_for(key: Optional[str], path: Optional[str | Path] = None) -> Optional[JakartaLibrary]:
    """The table entry whose current or earlier coordinates equal `key` (None when the library is unknown)."""
    if not key:
        return None
    return next((lib for lib in jakarta_libraries(path) if key in lib.all_keys), None)
