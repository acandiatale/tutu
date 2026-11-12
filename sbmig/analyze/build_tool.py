"""프로젝트의 빌드 도구를 판별하는 모듈. Maven 프로젝트만 이관하고, 아니면 exit 3으로 중단한다.
접수의 ``_check_build_tool``이 가장 먼저 호출하고, 분석기가 결과를 ``ProjectProfile.build_tool``에 넣는다.
- Gradle 프로젝트와 pom이 하나도 없는 트리는 거부한다.
  pom이 없는 트리는 ``sbmig inventory <dir>``로 구조를 설명한다
- Ant 프로젝트는 접수가 ``intake.ant_message``로 거부한다.
  ``intake.ant_to_maven: true``일 때만 pom.xml을 만든다
- pom이 하나라도 있으면 함께 있는 Gradle / Ant 파일은 무시한다

Build-tool detection (catalog S4): only Maven projects are migrated. An Ant project is refused at intake with
`intake.ant_message` (`ANT_REASON` is the technical description); with `intake.ant_to_maven: true` the intake
synthesizes a pom.xml from `ant_build_file` instead.

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
    """빌드 도구 판별 결과를 담는 데이터 클래스.
    ``tool``(maven / gradle / ant / none)과 설명 ``reason``을 담는다.
    ant면 ``ant_build_file``에 사용할 build.xml의 상대 경로가 들어간다."""
    tool: BuildTool
    reason: str = ""
    ant_build_file: str = ""                 # v2.1j: repo-relative build.xml when tool is ant (else "")

    @property
    def ok(self) -> bool:
        """Maven 프로젝트인지 확인한다. Maven만 이관 대상이다."""
        return self.tool == "maven"


def _has_any(ws: Any, names: tuple[str, ...]) -> bool:
    """이름 중 하나인 파일이 트리에 있는지 확인한다.
    확인 비용이 적은 루트를 먼저 보고, 없으면 하위 전체를 찾는다(``.git`` / ``target`` 등은 제외)."""
    exists = getattr(ws, "exists", None)
    if callable(exists) and any(exists(n) for n in names):
        return True
    return any(not is_skipped_path(p) for n in names for p in ws.glob(n))


def find_ant_build_file(ws: Any) -> str:
    """Ant 빌드 파일 경로를 찾는다. 루트의 ``build.xml``이 없으면 가장 얕은 ``build.xml``(같은 깊이면
    이름순)을 고르고, 없으면 빈 문자열을 반환한다.

    The Ant build file: the root `build.xml`, else the shallowest one outside skipped paths; "" when none."""
    exists = getattr(ws, "exists", None)
    if callable(exists) and exists("build.xml"):
        return "build.xml"
    cands = sorted((p for p in ws.glob("build.xml") if not is_skipped_path(p)), key=lambda p: (p.count("/"), p))
    return cands[0] if cands else ""


def detect_build_tool(ws: Any) -> BuildToolDetection:
    """빌드 도구를 판별한다.
    pom.xml이 어디든 하나라도 있으면 ``maven``을, 아니면 gradle / ant / none과 거부 사유를 반환한다.

    `maven` when any pom.xml exists (root or below); else gradle / ant / none with an explanation."""
    gradle = _has_any(ws, GRADLE_FILES)
    ant = _has_any(ws, ANT_FILES)
    # pom이 있으면 Maven으로 본다. 함께 있는 Gradle / Ant 파일은 무시하고 reason에 "ignored"만 남긴다
    if _has_any(ws, ("pom.xml",)):
        ignored = []
        if gradle:
            ignored.append("Gradle build files")
        if ant:
            ignored.append("Ant build.xml")
        reason = (f"{' and '.join(ignored)} present next to Maven poms: ignored (Maven build used)"
                  if ignored else "pom.xml found")
        return BuildToolDetection("maven", reason)
    # 여기부터는 pom이 없는 경우이므로 모두 접수에서 거부한다. reason이 보고서의 중단 사유가 된다
    if gradle:
        return BuildToolDetection("gradle", GRADLE_REASON)
    if ant:
        # 접수는 intake.ant_message로 거부하고, intake.ant_to_maven이 켜졌을 때만 이 파일로 pom을 만든다
        return BuildToolDetection("ant", ANT_REASON, ant_build_file=find_ant_build_file(ws))
    return BuildToolDetection("none", NONE_REASON)
