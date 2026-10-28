"""빌드 도구 판별(카탈로그 S4): Maven 프로젝트만 마이그레이션한다.
흐름상 위치: 접수(intake)의 ``_check_build_tool`` 이 Maven 호출 전에 가장 먼저 부르고, 분석기가 같은 결과를
``ProjectProfile.build_tool`` 에 다시 쓴다(대비책). Maven 이 아니면 실행은 exit 3 으로 중단된다. 핵심 진입점:
:func:`detect_build_tool` → :class:`BuildToolDetection` (``ok`` = maven). 결정: Gradle / Ant 거부는 사용자
결정(2026-10-20), pom 이 하나도 없는 트리 거부는 회사 결정 (2026-10-23, pom 합성 없음; ``sbmig inventory
<dir>`` 로 트리를 설명해 담당 팀에 거부 사유를 보낸다). pom
이 어딘가에 하나라도 있으면 옆의 Gradle / Ant 파일은 무시한다.

Build-tool detection (catalog S4): only Maven projects are migrated.

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
    """빌드 도구 판별 결과: ``tool`` (maven / gradle / ant / none) 과 사람에게 보여 줄 설명 ``reason``."""
    tool: BuildTool
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Maven 이면 True — 이것만 마이그레이션 대상이다."""
        return self.tool == "maven"


def _has_any(ws: Any, names: tuple[str, ...]) -> bool:
    """루트에 먼저 묻고(싸다), 없으면 하위 전체를 glob — ``.git`` / ``target`` 등 건너뛸 경로는 제외."""
    exists = getattr(ws, "exists", None)
    if callable(exists) and any(exists(n) for n in names):
        return True
    return any(not is_skipped_path(p) for n in names for p in ws.glob(n))


def detect_build_tool(ws: Any) -> BuildToolDetection:
    """pom.xml 이 루트든 하위든 하나라도 있으면 ``maven``, 아니면 gradle / ant / none 과 거부 설명을 돌려준다.

    `maven` when any pom.xml exists (root or below); else gradle / ant / none with an explanation."""
    gradle = _has_any(ws, GRADLE_FILES)
    ant = _has_any(ws, ANT_FILES)
    # pom 이 있으면 Maven 이 이긴다: 옆에 남은 Gradle / Ant 파일은 무시하고 경고(reason 에 "ignored")만 남긴다
    if _has_any(ws, ("pom.xml",)):
        ignored = []
        if gradle:
            ignored.append("Gradle build files")
        if ant:
            ignored.append("Ant build.xml")
        reason = (f"{' and '.join(ignored)} present next to Maven poms: ignored (Maven build used)"
                  if ignored else "pom.xml found")
        return BuildToolDetection("maven", reason)
    # 여기부터는 pom 이 전혀 없는 경우 — 전부 접수에서 거부(exit 3), reason 이 보고서의 중단 사유가 된다
    if gradle:
        return BuildToolDetection("gradle", GRADLE_REASON)
    if ant:
        return BuildToolDetection("ant", ANT_REASON)
    return BuildToolDetection("none", NONE_REASON)
