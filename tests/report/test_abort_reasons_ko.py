"""v2.1z-aj: every abort reason a person can hit at intake or at the start of the remediation flow has Korean
(`tr_line("ko", …)`) — the web status shows it. The bare reason (journal `abort`) and the stored follow-up form
`run aborted: <reason>` both translate; the Ant notice (already Korean, from the config) passes through unchanged."""
from __future__ import annotations

import re

import pytest

from sbmig.analyze.build_tool import GRADLE_REASON, NONE_REASON
from sbmig.core.workspace import STOP_REQUEST_REASON
from sbmig.report.i18n import tr_line

DETACHED = re.compile(r"[A-Za-z0-9`)\]] (은|는|이|가|을|를|과|와|로|으로|의|에|에서|도|만|이라|라)(?=[\s,.)]|$)")

# (reason as Orchestrator._abort receives it, the Korean it must become)
CASES = [
    ("vulnerability remediation not started (not ready): IVAS_BASE_URL is not set; JDK 21 not found",
     "취약점 조치를 시작하지 못했습니다. 실행 환경이 갖춰지지 않았습니다: IVAS_BASE_URL is not set; JDK 21 not found"),
    (GRADLE_REASON,
     "Gradle 빌드 파일(build.gradle / settings.gradle)만 있고 pom.xml은 없습니다. Maven 프로젝트만 받습니다. Maven으로 변환한 뒤 "
     "다시 신청해 주세요."),
    (NONE_REASON, "pom.xml이 없어 지원하는 빌드 도구를 찾지 못했습니다. Maven 프로젝트만 받습니다."),
    ("'mvn' is not runnable (install Maven or set build.mvn)",
     "Maven(mvn)을 실행할 수 없습니다. Maven을 설치하거나 build.mvn을 설정해 주세요."),
    ("'mvn' cannot start: The JAVA_HOME environment variable is not defined correctly — set build.java_home (or JAVA_HOME) "
     "to a JDK 21 home",
     "Maven(mvn)이 시작되지 않습니다(The JAVA_HOME environment variable is not defined correctly). build.java_home(또는 "
     "JAVA_HOME)을 JDK 21 경로로 설정해 주세요."),
    ("Maven runs on Java 17.0.9 (runtime /opt/jdk-17), not Java 21: set build.java_home (or JAVA_HOME) to a JDK 21 home "
     "and rerun",
     "Maven이 Java 17.0.9에서 실행됩니다(runtime /opt/jdk-17). Java 21이 필요합니다. build.java_home(또는 JAVA_HOME)을 "
     "JDK 21 경로로 설정한 뒤 다시 실행해 주세요."),
    ("build.setup_command failed (rc=1): connection refused",
     "실행 준비 명령(build.setup_command)이 실패했습니다(rc=1). connection refused"),
    (STOP_REQUEST_REASON, "사용자 요청으로 멈췄습니다. 최종 빌드(mvn clean package)는 실행하지 않았습니다."),
    ("stopped by user (web UI) — the final `mvn clean package` did not run",
     "화면에서 멈춤을 요청해 멈췄습니다. 최종 빌드(mvn clean package)는 실행하지 않았습니다."),
    ("internal error: KeyError: 'x'", "내부 오류로 멈췄습니다: KeyError: 'x'"),
    ("missing component: sbmig.analyze is not installed", "필요한 구성 요소가 없어 멈췄습니다: sbmig.analyze is not installed"),
    ("pom.xml is an EAR (portal-ear, packaging ear) that packages modules which are not in this directory: portal-web "
     "(war). An EAR holds no code of its own — run sbmig on the directory that contains every module (e.g. the folder "
     "above APP/); with sibling poms and no root pom, sbmig generates the aggregator pom itself "
     "(layout.generate_aggregator)",
     "루트 pom.xml이 EAR(portal-ear)이고, EAR이 묶는 모듈(portal-web (war))이 이 폴더에 없습니다. EAR에는 코드가 없으니 모든 "
     "모듈이 들어 있는 상위 폴더로 다시 신청해 주세요."),
    ("Ant build.xml detected and no pom.xml: the pom synthesis (sbmig.analyze.ant) is not available in this installation "
     "(ModuleNotFoundError: x) — convert the build to Maven first",
     "Ant 프로젝트의 pom.xml을 만드는 기능을 이 설치본에서 쓸 수 없습니다(ModuleNotFoundError: x). Maven으로 변환한 뒤 신청해 주세요."),
    ("pom.xml could not be synthesized from build.xml: no source root (Ant project rejected: convert the build to Maven "
     "first)",
     "build.xml에서 pom.xml을 만들지 못했습니다(no source root). Maven으로 변환한 뒤 신청해 주세요."),
]
ANT_NOTICE = "Maven 변환 후 신청 부탁드립니다."


@pytest.mark.parametrize("reason,korean", CASES, ids=[c[0][:40] for c in CASES])
def test_abort_reason_has_korean_in_both_forms(reason, korean):
    assert tr_line("ko", reason) == korean
    assert tr_line("ko", f"run aborted: {reason}") == f"실행 중단: {korean}"
    assert tr_line("en", reason) == reason and tr_line("en", f"run aborted: {reason}") == f"run aborted: {reason}"
    assert not DETACHED.search(korean.replace("(war)", "")), korean


def test_the_ant_notice_passes_through():
    assert tr_line("ko", ANT_NOTICE) == ANT_NOTICE
    assert tr_line("ko", f"run aborted: {ANT_NOTICE}") == f"실행 중단: {ANT_NOTICE}"


def test_the_orchestrator_texts_are_the_ones_in_the_table(cfg, tmp_path):
    """The f-string reasons above are built the way the orchestrator builds them (guards against wording drift)."""
    import inspect

    from sbmig.loop import orchestrator
    src = inspect.getsource(orchestrator)
    for piece in ("is not runnable (install Maven or set build.mvn)", "cannot start: ", "— set ",
                  "build.setup_command failed (rc=", "vulnerability remediation not started (not ready): ",
                  "internal error: ", "missing component: ", "and rerun"):
        assert piece in src, piece
