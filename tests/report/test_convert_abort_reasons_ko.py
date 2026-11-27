"""The abort reasons of the conversion step at intake (`Orchestrator._intake_convert`) have Korean, in the bare form
(journal `abort`, the web status) and in the stored follow-up form `run aborted: <reason>`. The reasons are the
exact sentences the orchestrator writes (tests/loop/test_intake_convert.py checks them on real runs)."""
from __future__ import annotations

import pytest

from sbmig.report.i18n import tr_line

CASES = [
    ("framework conversion is not available: convert.enabled is false — choose the way 'reduced' or switch "
     "convert.enabled on",
     "사내 규정 전환이 꺼져 있습니다(convert.enabled). 다른 처리 방법으로 다시 신청하거나 설정을 켜 주세요."),
    ("framework conversion is not available: the framework has no name (framework.name and framework.artifacts are "
     "empty) — set framework.name or framework.artifacts",
     "사내 규정 전환에 쓸 프레임워크 이름이 없습니다. framework.name이나 framework.artifacts를 설정해 주세요."),
    ("framework conversion is not available: the project has no root pom to take the package from and "
     "convert.package_root is not set — set convert.package_root",
     "패키지 이름을 가져올 루트 pom.xml이 프로젝트에 없고 convert.package_root도 설정되어 있지 않습니다. "
     "convert.package_root를 설정해 주세요."),
    ("framework conversion is not available: no version for org.mybatis.spring.boot:mybatis-spring-boot-starter "
     "(convert.versions has no value for it and the repository set has no release of it) — set "
     "convert.versions[\"org.mybatis.spring.boot:mybatis-spring-boot-starter\"]",
     "org.mybatis.spring.boot:mybatis-spring-boot-starter의 버전을 저장소에서 찾지 못했습니다. convert.versions에 버전을 "
     "지정해 주세요."),
    ("framework conversion is not available: the conversion rules could not be loaded (RulesError: bad file) — check "
     "knowledge/fwconvert/rules.yml and convert.rules",
     "사내 규정 전환 규칙을 읽지 못했습니다(RulesError: bad file). knowledge/fwconvert/rules.yml과 convert.rules를 확인해 "
     "주세요."),
    ("framework conversion failed: RuntimeError: converter broke — the project was left as uploaded",
     "사내 규정 전환 중 오류가 나서 멈췄습니다(RuntimeError: converter broke). 프로젝트는 올린 그대로입니다."),
    ("framework conversion failed: OSError: disk full — the project was put back as uploaded",
     "사내 규정 전환 중 오류가 나서 멈췄습니다(OSError: disk full). 바꾸던 프로젝트는 올린 상태로 모두 되돌렸습니다."),
    ("framework conversion failed: OSError: disk full — the as-is files could not all be put back; what is missing "
     "is in /data/out/r1/convert/asis (local, docs-ro)",
     "사내 규정 전환 중 오류가 나서 멈췄습니다(OSError: disk full). 올린 파일을 모두 되돌리지 못했습니다. 빠진 "
     "파일(local, docs-ro)은 /data/out/r1/convert/asis에 있습니다."),
    ("framework conversion failed: OSError: disk full — the as-is files were put back, but not everything was cleaned "
     "up (shopApp-boot: PermissionError: denied)",
     "사내 규정 전환 중 오류가 나서 멈췄습니다(OSError: disk full). 올린 파일은 모두 되돌렸지만 정리하지 못한 것이 "
     "있습니다: shopApp-boot: PermissionError: denied"),
    ("framework conversion found 3 files that look like framework files by name, but none could be read as one: the "
     "project was handled without the conversion, as with the way 'reduced'",
     "이름으로는 프레임워크 파일로 보이는 파일이 3개 있었지만 하나도 프레임워크 파일로 읽지 못했습니다. 그래서 사내 규정 "
     "전환 없이 JDK 21, Jakarta만 적용했습니다."),
    ("framework conversion is not available: the root pom cannot be read (not well-formed XML) and "
     "convert.package_root is not set — set convert.package_root",
     "루트 pom.xml을 읽지 못했고(not well-formed XML) convert.package_root도 설정되어 있지 않습니다. pom을 고치거나 "
     "convert.package_root를 설정해 주세요."),
    ("framework conversion is not available: the root pom has no groupId and convert.package_root is not set — set "
     "convert.package_root",
     "루트 pom.xml에 groupId가 없고 convert.package_root도 설정되어 있지 않습니다. groupId를 넣거나 "
     "convert.package_root를 설정해 주세요."),
    ("framework conversion is not available: the root pom's groupId 'kr.co.my-corp' cannot be used as a package and "
     "convert.package_root is not set — set convert.package_root",
     "루트 pom.xml의 groupId(kr.co.my-corp)는 자바 패키지 이름으로 쓸 수 없고 convert.package_root도 설정되어 있지 "
     "않습니다. convert.package_root를 설정해 주세요."),
    ("framework conversion is not available: the root pom has no artifactId and convert.package_root is not set — "
     "set convert.package_root",
     "루트 pom.xml에 artifactId가 없고 convert.package_root도 설정되어 있지 않습니다. artifactId를 넣거나 "
     "convert.package_root를 설정해 주세요."),
    ("framework conversion is not available: the root pom's artifactId '${app}' cannot be used as a project name and "
     "convert.package_root is not set — set convert.package_root",
     "루트 pom.xml의 artifactId(${app})는 프로젝트 이름으로 쓸 수 없고 convert.package_root도 설정되어 있지 않습니다. "
     "convert.package_root를 설정해 주세요."),
]


@pytest.mark.parametrize("reason,korean", CASES, ids=[c[0][38:80] for c in CASES])
def test_convert_abort_reason_has_korean_in_both_forms(reason, korean):
    assert tr_line("ko", reason) == korean
    assert tr_line("ko", f"run aborted: {reason}") == f"실행 중단: {korean}"
    assert tr_line("en", reason) == reason
