from __future__ import annotations

from pathlib import Path

from rich.console import Console

from sbmig.core.models import GAV, BlockedItem, BlockReason, BuildError, ErrorKind, Phase, RunReport
from sbmig.core.workspace import Workspace
from sbmig.report import BLOCKED_TITLE, console_summary, flag_help, render_markdown, write_report

from .test_report import sample_report


def blocked_items() -> list[BlockedItem]:
    err = BuildError(kind=ErrorKind.COMPILE, file="src/main/java/com/x/Foo.java", line=12,
                     message="constructor LegacyRequestHelper in class com.company.legacy.web.LegacyRequestHelper "
                             "cannot be applied to given types;", signature="compile|cannot_be_applied|x")
    x1 = BlockedItem(id="blk-1", reason=BlockReason.EXTERNAL_JAVAX_API, phase=Phase.SPRING, cluster_id="compile:1",
                     signatures=["compile|cannot_be_applied|x"],
                     files=[f"src/main/java/com/x/F{i}.java:{i}" for i in range(7)], errors=[err],
                     artifact=GAV.parse("com.company:legacy-web:1.4"),
                     owner_class="com.company.legacy.web.LegacyRequestHelper",
                     required_type="javax.servlet.http.HttpServletRequest",
                     found_type="jakarta.servlet.http.HttpServletRequest",
                     suggestion="upgrade `com.company:legacy-web` to a Jakarta build | or shim", attempts=0)
    x3 = BlockedItem(id="blk-2", reason=BlockReason.ARTIFACT_NOT_FOUND, phase=Phase.RESTRUCTURE,
                     cluster_id="dependency_resolution:2", files=["com.x:gone:1.0"],
                     artifact=GAV.parse("com.x:gone:1.0"), suggestion="proxy it in Nexus or fix the coordinates",
                     errors=[BuildError(kind=ErrorKind.DEPENDENCY_RESOLUTION, message="Could not find artifact",
                                        artifact=GAV.parse("com.x:gone:1.0"))])
    rf = BlockedItem(id="blk-3", reason=BlockReason.REPEATED_FAILURE, phase=Phase.SPRING, cluster_id="compile:3",
                     owner_class=None, suggestion="needs a human", attempts=3)
    return [x1, x3, rf]


def test_blocked_section_rendering():
    r = sample_report()
    r.blocked = blocked_items()
    md = render_markdown(r)
    assert f"## {BLOCKED_TITLE}" in md
    # right after the summary header, before "Unresolved"
    assert md.index("| Run id |") < md.index(f"## {BLOCKED_TITLE}") < md.index("## Unresolved errors")
    assert "| **Blocked** | 3 item(s) — external_javax_api 1, artifact_not_found 1, repeated_failure 1 (see below) |" in md
    assert ("| `external_javax_api` (spring) | `com.company:legacy-web:1.4` (class "
            "`com.company.legacy.web.LegacyRequestHelper`) | `javax.servlet.http.HttpServletRequest` → "
            "`jakarta.servlet.http.HttpServletRequest` |") in md
    assert "`src/main/java/com/x/F4.java:4` (+2)" in md and "F5.java" not in md       # ≤5 files
    assert "a Jakarta build \\| or shim" in md                                          # cells are escaped
    assert "| `artifact_not_found` (restructure) | `com.x:gone:1.0` | - |" in md
    assert "| `repeated_failure` (spring) | - | - | - | 3 | needs a human |" in md
    assert "<details><summary><code>blk-1</code> — 1 error(s)</summary>" in md
    assert "`src/main/java/com/x/Foo.java:12` constructor LegacyRequestHelper" in md
    assert "(blocked clusters excluded)" in md
    assert BLOCKED_TITLE not in render_markdown(sample_report())


def test_blocked_json_roundtrip(tmp_path: Path):
    ws = Workspace(tmp_path / "p", tmp_path / "run", "run-1")
    r = sample_report()
    r.blocked = blocked_items()
    r.phases[0].blocked = r.blocked[:1]
    paths = write_report(r, ws)
    back = RunReport.model_validate_json(Path(paths["report_json"]).read_text())
    assert back.blocked == r.blocked and back.phases[0].blocked == r.blocked[:1]
    assert back.model_dump(mode="json") == r.model_dump(mode="json")


def test_console_summary_counts_blocked():
    r = sample_report()
    r.blocked = blocked_items()
    console = Console(record=True, width=200)
    console_summary(r, console)
    text = console.export_text()
    assert "blocked: 3 item(s) - external_javax_api 1, artifact_not_found 1, repeated_failure 1" in text
    assert BLOCKED_TITLE in text and "com.company:legacy-web:1.4" in text


def test_flag_help_prefixes():
    assert flag_help("aborted:parent-not-found") and flag_help("llm-unavailable") and flag_help("infra:llm")
    assert flag_help("stopped-after-spring") and flag_help("nope") is None
    # flags only the removed v1 handlers raised have no help text any more
    for gone in ("source-encoding-set:cp949", "werror-removed", "enforcer-relaxed", "jakarta-leftovers-renamed",
                 "managed-versions-carried-over"):
        assert flag_help(gone) is None, gone
