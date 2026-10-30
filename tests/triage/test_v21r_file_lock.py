"""v2.1r: a cluster whose errors show a Windows file lock under target/ is BLOCKED `file_lock` at once (blocked_policy
0: no LLM, no script) with the antivirus / process suggestion (Korean in the report)."""
from __future__ import annotations

from sbmig.build import parse_maven_log
from sbmig.core.interfaces import TriageContext
from sbmig.core.models import BlockedItem, BlockReason, DecisionKind, Phase
from sbmig.errors.entries import error_stage
from sbmig.loop.blocked_registry import FILE_LOCK_SUGGESTION, compose_suggestion
from sbmig.report.i18n import tr_line
from sbmig.report.markdown import blocked_rows
from sbmig.triage import Triager, classify_blocked, cluster_cases, cluster_errors

from .conftest import make_profile

JAR = r"C:\work\proj\app\target\app-1.0\WEB-INF\lib\wagon-http-shared-1.0-beta-2.jar"
LOCK_LOG = ("[INFO] --- war:3.4.0:war (default-war) @ app ---\n"
            "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-war-plugin:3.4.0:war (default-war) on "
            "project app: Error assembling WAR: Problem creating war: Execution exception: "
            f"java.nio.file.FileSystemException: {JAR}: 다른 프로세스가 파일을 사용 중이기 때문에 프로세스가 파일에 "
            "액세스할 수 없습니다 -> [Help 1]\n[INFO] BUILD FAILURE\n")
KO_SUGGESTION = ("빌드 산출물(target/)을 다른 프로세스가 잠갔습니다 — 백신 실시간 검사에서 프로젝트의 target/ 를 예외 "
                 "처리하고, 열려 있는 IDE/탐색기/이전 java 프로세스를 닫은 뒤 다시 실행하세요")


def _cluster():
    clusters = cluster_errors(parse_maven_log(LOCK_LOG, "/work/proj"))
    assert len(clusters) == 1
    return clusters[0]


def test_a_file_lock_is_classified_with_the_path():
    c = _cluster()
    b = classify_blocked(c, make_profile(), None)
    assert b is not None and b.reason == BlockReason.FILE_LOCK and b.errors == c.errors
    assert b.detail().startswith(f"the build output ({JAR}) is locked by another process")


def test_the_default_policy_blocks_at_once_without_the_llm(cfg, profile):
    assert cfg.loop.blocked_policy["file_lock"] == 0
    case = cluster_cases(parse_maven_log(LOCK_LOG, "/work/proj"))[0]
    d = Triager(cfg).decide(case, None, TriageContext(phase=Phase.VERIFY, iteration=1), profile)
    assert d.kind == DecisionKind.BLOCKED and d.rationale.startswith("blocked: file_lock — the build output")


def test_the_suggestion_in_english_and_korean():
    c = _cluster()
    item = BlockedItem(id="b1", reason=BlockReason.FILE_LOCK, phase=Phase.VERIFY, cluster_id=c.id,
                       errors=list(c.errors))
    s = compose_suggestion(item)
    assert s == FILE_LOCK_SUGGESTION and s.endswith("the LLM is not asked")
    assert tr_line("ko", s) == KO_SUGGESTION
    item.suggestion = s
    assert any(KO_SUGGESTION in row for row in blocked_rows([item], "ko"))


def test_a_file_lock_proves_no_stage():
    """The loop's scoring never treats a lock as "a new earlier-stage error" hiding the targeted ones."""
    lock = _cluster().errors[0]
    assert error_stage(lock) is None
    compile_err = parse_maven_log("[ERROR] /w/src/main/java/A.java:[3,8] cannot find symbol\n", "/w")[0]
    assert error_stage(compile_err) == 3
