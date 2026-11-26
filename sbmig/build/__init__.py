"""Maven 빌드를 실행하고, 빌드 로그에서 오류를 뽑아 오류 서명(L1, L2)을 붙이는 패키지.

The verify build, the fix loop's rebuilds and the final build all run through ``MavenRunner``;
``parse_maven_log`` turns the log into a list of BuildError. One L2 signature is one case. classpath,
deptree, effective, repo_versions and depchanges are not re-exported: import them by module path. A failed
build is a BuildResult, not an exception. Tests are always skipped (``build.skip_test_compile``).

Public API (docs/INTERFACES.md):
    MavenRunner(ws, cfg).run(goal, *, module=None, extra_args=(), label="", skip_test_compile=False,
                             skip_tests=False) -> BuildResult   # skip_tests adds -DskipTests
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
