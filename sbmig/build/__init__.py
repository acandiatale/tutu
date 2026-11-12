"""Maven 빌드 실행, 빌드 로그 파싱, 오류 서명(L1 / L2) 계산, 빌드 관련 정보 조회를 모은 패키지.
검증 빌드, 수정 루프의 재빌드, 최종 빌드는 모두 ``MavenRunner``로 실행하고, ``parse_maven_log``가 로그를
BuildError 목록으로 바꾼다. L2 서명 하나가 케이스 하나다.
- 진입점: ``MavenRunner``, ``parse_maven_log``, ``signature_for``, ``normalize_message``, ``maven_available``
- classpath, deptree, effective, repo_versions, depchanges는 패키지에서 다시 내보내지 않으므로 모듈 경로로 import한다
- 빌드 실패는 예외가 아니라 BuildResult로 반환한다
- 테스트는 항상 건너뛴다(``build.skip_test_compile``)

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
