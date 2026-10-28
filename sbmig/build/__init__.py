"""sbmig.build — Maven 실행기, 빌드 로그 파서, 오류 서명(L1 / L2), 그리고 빌드 주변의 사실 조회를 모은 패키지.
흐름상 위치: 변환 후 컴파일·검증 빌드(``mvn clean package -Dmaven.test.skip=true``)·수정 루프의 재빌드·최종
빌드가 전부 ``MavenRunner`` 를 거치고, 그 로그를 ``parse_maven_log`` 가 BuildError 목록(L1 / L2 서명 포함)으로
바꾼다 → 케이스 = L2 서명 하나. 핵심 진입점: ``MavenRunner``, ``parse_maven_log``, ``signature_for``,
``normalize_message``, ``maven_available``(전제 점검). classpath / deptree / effective / repo_versions(v2.1d
저장소 버전 조회) / depchanges(v2.1d pom diff 되읽기)는 여기서 다시 내보내지 않으므로 모듈 경로로 import 한다.
불변조건: 빌드는 실패해도 예외가 아니라 BuildResult 다(빌드 게이트); 테스트는 항상
건너뛴다(``build.skip_test_compile``).

sbmig.build — Maven runner, log parser and error signatures (workstream C).

Public API (docs/INTERFACES.md):
    MavenRunner(ws, cfg).run(goal, *, module=None, extra_args=(), label="", skip_test_compile=False,
                             skip_tests=False) -> BuildResult   # skip_tests → -DskipTests (tests-skipped fallback)
    parse_maven_log(text, project_root) -> list[BuildError]          (signatures filled)
    signature_for(error) -> (L1, L2)
    normalize_message(msg) -> str
    maven_available(cfg) -> (ok, version string)                     (preflight)


"""
from .log_parser import parse_maven_log, parse_reactor_summary, strip_ansi
from .mvn import (base_command, local_repo_path, maven_available, mvn_env, repository_notes, run_logged,
                  settings_catch_all_mirror)
from .runner import MavenRunner, build_summary
from .signatures import register_external_prefixes, external_prefixes_from_config, classify_javac, normalize_message, signature_for

__all__ = [
    "repository_notes", "settings_catch_all_mirror",
    "MavenRunner",
    "base_command",
    "build_summary",
    "classify_javac",
    "local_repo_path",
    "maven_available",
    "mvn_env",
    "normalize_message",
    "parse_maven_log",
    "parse_reactor_summary",
    "run_logged",
    "signature_for",
    "strip_ansi",
]
