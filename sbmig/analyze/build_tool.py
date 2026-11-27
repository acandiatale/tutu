"""프로젝트가 어떤 빌드 도구를 쓰는지 판별하는 모듈. Maven 프로젝트만 이관한다.

The intake's `_check_build_tool` calls it first; a tool other than Maven stops the run with exit 3. The
analyzer puts the result into `ProjectProfile.build_tool`. Gradle trees and trees without any pom are
refused (a pom-less tree is described with `sbmig inventory <dir>`). Gradle / Ant files next to a pom are
ignored. `ANT_REASON` is the technical description; the operator-facing refusal is `intake.ant_message`.
With `intake.ant_to_maven: true` the intake synthesizes a pom.xml from `ant_build_file` instead.
`detect_build_tool(ws)` is cheap (root checks first, then recursive globs that skip `.git` / `target`) so it
runs at intake before the effective-pom capture; the analyzer reuses it (+ `warnings[0]` when the tool is not
Maven).
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
    """빌드 도구 판별 결과를 담는 클래스.
    ``tool``이 maven이 아니면 ``reason``이 거부 사유다."""
    tool: BuildTool
    reason: str = ""
    ant_build_file: str = ""                 # ant일 때만 build.xml의 상대 경로가 들어간다

    @property
    def ok(self) -> bool:
        """Maven 프로젝트면 True다."""
        return self.tool == "maven"


def _has_any(ws: Any, names: tuple[str, ...]) -> bool:
    """``names`` 중 하나라도 프로젝트 안에 있으면 True를 반환한다.
    루트부터 보고, 없으면 하위 폴더까지 찾는다."""
    exists = getattr(ws, "exists", None)
    if callable(exists) and any(exists(n) for n in names):
        return True
    return any(not is_skipped_path(p) for n in names for p in ws.glob(n))


def find_ant_build_file(ws: Any) -> str:
    """Ant 빌드 파일(``build.xml``)의 경로를 찾는다.
    루트에 없으면 가장 얕은 폴더의 것을 고른다. 하나도 없으면 빈 문자열을 반환한다."""
    exists = getattr(ws, "exists", None)
    if callable(exists) and exists("build.xml"):
        return "build.xml"
    cands = sorted((p for p in ws.glob("build.xml") if not is_skipped_path(p)), key=lambda p: (p.count("/"), p))
    return cands[0] if cands else ""


def detect_build_tool(ws: Any) -> BuildToolDetection:
    """프로젝트의 빌드 도구를 판별한다.
    pom.xml이 하나라도 있으면 maven이다. 없으면 gradle, ant, none 중 하나와 거부 사유를 반환한다."""
    gradle = _has_any(ws, GRADLE_FILES)
    ant = _has_any(ws, ANT_FILES)
    # pom이 있으면 Maven으로 본다. 함께 있는 Gradle, Ant 파일은 무시하고 reason에 "ignored"만 남긴다
    if _has_any(ws, ("pom.xml",)):
        ignored = []
        if gradle:
            ignored.append("Gradle build files")
        if ant:
            ignored.append("Ant build.xml")
        reason = (f"{' and '.join(ignored)} present next to Maven poms: ignored (Maven build used)"
                  if ignored else "pom.xml found")
        return BuildToolDetection("maven", reason)
    # 여기부터는 pom이 없는 경우다. 모두 접수에서 거부하고, reason이 보고서의 중단 사유가 된다
    if gradle:
        return BuildToolDetection("gradle", GRADLE_REASON)
    if ant:
        # Ant도 거부한다. intake.ant_to_maven을 켰을 때만 이 build.xml로 pom을 만든다
        return BuildToolDetection("ant", ANT_REASON, ant_build_file=find_ant_build_file(ws))
    return BuildToolDetection("none", NONE_REASON)
