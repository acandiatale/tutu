"""Build-tool detection (catalog S4): only Maven projects are migrated.

`detect_build_tool(ws)` is cheap (root checks first, then recursive globs that skip `.git` / `target`) so the
orchestrator can run it at intake, before the effective-pom capture, and the analyzer reuses it for
`ProjectProfile.build_tool` (+ `warnings[0]` when the tool is not Maven).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .pom_parser import is_skipped_path

BuildTool = Literal["maven", "gradle", "ant", "none"]

GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
ANT_FILES = ("build.xml",)

GRADLE_REASON = ("Gradle build detected (build.gradle / settings.gradle) and no pom.xml: only Maven projects are "
                 "supported (convert the build to Maven first)")
ANT_REASON = ("Ant build.xml detected and no pom.xml: only Maven projects are supported "
              "(convert the build to Maven first)")
NONE_REASON = ("no pom.xml found (no supported build tool): only Maven projects are supported — company decision "
               "2026-10-23: pom-less trees are rejected; `sbmig inventory <dir>` describes the tree for the rejection note")


@dataclass(frozen=True)
class BuildToolDetection:
    tool: BuildTool
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.tool == "maven"


def _has_any(ws: Any, names: tuple[str, ...]) -> bool:
    exists = getattr(ws, "exists", None)
    if callable(exists) and any(exists(n) for n in names):
        return True
    return any(not is_skipped_path(p) for n in names for p in ws.glob(n))


def detect_build_tool(ws: Any) -> BuildToolDetection:
    """`maven` when any pom.xml exists (root or below); else gradle / ant / none with an explanation."""
    gradle = _has_any(ws, GRADLE_FILES)
    ant = _has_any(ws, ANT_FILES)
    if _has_any(ws, ("pom.xml",)):
        ignored = []
        if gradle:
            ignored.append("Gradle build files")
        if ant:
            ignored.append("Ant build.xml")
        reason = (f"{' and '.join(ignored)} present next to Maven poms: ignored (Maven build used)"
                  if ignored else "pom.xml found")
        return BuildToolDetection("maven", reason)
    if gradle:
        return BuildToolDetection("gradle", GRADLE_REASON)
    if ant:
        return BuildToolDetection("ant", ANT_REASON)
    return BuildToolDetection("none", NONE_REASON)
