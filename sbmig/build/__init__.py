"""sbmig.build — Maven runner, log parser and error signatures (workstream C).

Public API (docs/INTERFACES.md):
    MavenRunner(ws, cfg).run(goal, *, module=None, extra_args=(), label="", skip_test_compile=False,
                             skip_tests=False) -> BuildResult   # skip_tests → -DskipTests (tests-skipped fallback)
    parse_maven_log(text, project_root) -> list[BuildError]          (signatures filled)
    signature_for(error) -> (L1, L2)
    normalize_message(msg) -> str
    maven_available(cfg) -> (ok, version string)                     (preflight)
"""
from .log_parser import parse_maven_log, parse_reactor_summary, strip_ansi
from .mvn import base_command, local_repo_path, maven_available, mvn_env, run_logged
from .runner import MavenRunner, build_summary
from .signatures import register_external_prefixes, external_prefixes_from_config, classify_javac, normalize_message, signature_for

__all__ = [
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
