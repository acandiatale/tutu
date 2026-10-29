"""v2.1j report side (docs/INTERFACES.md "v2.1j"): `report.pom_synthesis` → the Handover block "Ant project: pom.xml
synthesized" / "Ant 프로젝트: pom.xml 합성" right after the operator checks (coordinates, layout, jar counts, one
`mvn install:install-file` line per unknown jar, the system-scope runtime warning, dropped jars, build.xml notes),
the "What we changed" line, the flag help of `pom-synthesized` / `jar-coordinates-unknown` and the attention code.
The orchestrator side is tests/loop/test_v21j_ant_intake.py."""
from __future__ import annotations

from sbmig.core.models import DependencyChange, JarCoordinate, OperatorCheck, PomSynthesis, RunReport
from sbmig.report import build_handover, render_markdown
from sbmig.report.handover import install_file_command, jar_stem_version, unknown_jar_commands
from sbmig.report.markdown import FLAG_HELP, POM_SYNTH_LIST_MAX, pom_synthesis_lines
from sbmig.report.result_line import report_result_line

from .test_handover import EFFECTIVE, as_is_profile, final_tree, make_report

UNKNOWN = "WebContent/WEB-INF/lib/acme-util-1.2.jar"
UNKNOWN_BARE = "lib/inhouse-crypto.jar"
CMD_1 = ("mvn install:install-file -Dfile=WebContent/WEB-INF/lib/acme-util-1.2.jar -DgroupId=com.company.migrated "
         "-DartifactId=acme-util -Dversion=1.2 -Dpackaging=jar")
CMD_2 = ("mvn install:install-file -Dfile=lib/inhouse-crypto.jar -DgroupId=com.company.migrated "
         "-DartifactId=inhouse-crypto -Dversion=0 -Dpackaging=jar")
FLAGS = ["tests-not-compiled", "pom-synthesized", "jar-coordinates-unknown"]


def synthesis(**kw) -> PomSynthesis:
    ps = PomSynthesis(
        ok=True, group_id="com.company.migrated", artifact_id="legacy-web", version="1.0.0-SNAPSHOT",
        packaging="war", source_dirs=["src", "src-gen"], web_root="WebContent", resource_dirs=["conf"],
        lib_dirs=["lib", "WebContent/WEB-INF/lib"], java_source="1.8", encoding="MS949",
        jars=[JarCoordinate(path="lib/commons-lang-2.6.jar", group_id="commons-lang", artifact_id="commons-lang",
                            version="2.6", source="pom_properties"),
              JarCoordinate(path="lib/ojdbc6.jar", group_id="com.oracle.database.jdbc", artifact_id="ojdbc6",
                            version="11.2.0.4", source="name_table"),
              JarCoordinate(path="lib/x-1.0.jar", group_id="org.x", artifact_id="x", version="1.0",
                            source="sha1_lookup"),
              JarCoordinate(path=UNKNOWN, source="unknown", scope="system", used=True),
              JarCoordinate(path=UNKNOWN_BARE, source="manifest", scope="system", used=None,
                            note="Implementation-Title: crypto"),
              JarCoordinate(path="lib/old-unused-0.9.jar", source="unknown", used=False)],
        unknown_jars=[UNKNOWN, UNKNOWN_BARE], dropped_jars=["lib/old-unused-0.9.jar"],
        build_xml_notes=["target `gen-stubs`: <exec executable=\"wsdl2java\"> not translated"],
        warnings=["2 source roots: src-gen added with build-helper-maven-plugin"])
    return ps.model_copy(update=kw)


CHECK = OperatorCheck(change=DependencyChange(path="pom.xml", key="org.sitemesh:sitemesh", before="2.4.2",
                                              after="3.2.1", kind="major"))


def ant_report(tmp_path, cfg, **kw) -> RunReport:
    report = make_report(pom_synthesis=kw.pop("pom_synthesis", synthesis()), flags=list(FLAGS),
                         operator_checks=[CHECK], **kw)
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    return report


def _section(md: str, start: str, end: str) -> str:
    i = md.index(start)
    return md[i:md.index(end, i)]


def test_english_handover_block(tmp_path, cfg):
    md = render_markdown(ant_report(tmp_path, cfg), language="en", cfg=cfg)
    assert md.index("## Handover") < md.index("### Operator checks (1 major upgrades)") \
        < md.index("### Ant project: pom.xml synthesized") < md.index("### Versions replaced")
    block = _section(md, "### Ant project: pom.xml synthesized", "### Versions replaced")
    assert ("The project had an Ant `build.xml` and no pom.xml: sbmig wrote `pom.xml` at intake from build.xml and "
            "the directories (commit `intake: pom.xml synthesized from build.xml`); build.xml itself is left as it "
            "was.") in block
    assert "- pom: `com.company.migrated:legacy-web:1.0.0-SNAPSHOT` (war), Java 1.8, encoding MS949" in block
    assert ("- layout: sources `src`, `src-gen`, web root `WebContent`, resources `conf`, lib `lib`, "
            "`WebContent/WEB-INF/lib`") in block
    assert "- jars: 6 — 1 pom.properties, 1 name table, 1 repository lookup, 3 unknown" in block
    assert ("**2 jar(s) without Maven coordinates** — declared with `system` scope (`<systemPath>`): the build "
            "passes, but system-scope jars are not packaged into the WAR / boot jar, so the application fails at "
            "runtime until each one is installed into Nexus and its pom entry is replaced by a normal dependency "
            "(drop `<scope>system</scope>` and `<systemPath>`):") in block
    assert f"```\n{CMD_1}\n{CMD_2}\n```" in block
    assert "`mvn deploy:deploy-file`" in block
    assert ("Left out (no coordinates and no source imports their packages): `lib/old-unused-0.9.jar` — add one "
            "back if the build or the runtime needs it.") in block
    assert ("build.xml steps not translated to Maven — check whether the build still needs them:\n"
            "- target `gen-stubs`: <exec executable=\"wsdl2java\"> not translated") in block
    assert "Synthesis warnings:\n- 2 source roots: src-gen added with build-helper-maven-plugin" in block
    changed = md[md.index("## What we changed"):]
    assert changed.split("\n")[2] == ("pom.xml synthesized from build.xml (6 jars: 1 pom.properties, 1 table, "
                                      "1 lookup, 3 unknown)")
    assert f"- `pom-synthesized`: {FLAG_HELP['pom-synthesized']}" in md
    assert f"- `jar-coordinates-unknown`: {FLAG_HELP['jar-coordinates-unknown']}" in md


def test_korean_handover_block(tmp_path, cfg):
    md = render_markdown(ant_report(tmp_path, cfg), language="ko", cfg=cfg)
    assert "### Ant project" not in md and "Left out" not in md and "pom.xml synthesized from build.xml (" not in md
    assert md.index("### 운영자 확인 필요") < md.index("### Ant 프로젝트: pom.xml 합성") < md.index("### 교체된 버전")
    block = _section(md, "### Ant 프로젝트: pom.xml 합성", "### 교체된 버전")
    assert ("Ant `build.xml` 만 있고 pom.xml 이 없는 프로젝트라 sbmig 가 접수에서 build.xml 과 폴더 구조로 `pom.xml` 을 "
            "만들었다(커밋 `intake: pom.xml synthesized from build.xml`). build.xml 은 그대로 두었다.") in block
    assert "- pom: `com.company.migrated:legacy-web:1.0.0-SNAPSHOT` (war), Java 1.8, 인코딩 MS949" in block
    assert "- 구조: 소스 `src`, `src-gen`, 웹 루트 `WebContent`, 리소스 `conf`, lib `lib`" in block
    assert "- jar: 6개 — pom.properties 1 · 이름표 1 · 저장소 조회 1 · 미상 3" in block
    assert "**Maven 좌표를 찾지 못한 jar 2개** — `system` scope(`<systemPath>`)로 선언했다" in block
    assert "WAR / boot jar 에 들어가지 않아 실행 시 실패한다" in block
    assert f"```\n{CMD_1}\n{CMD_2}\n```" in block                 # commands are never translated
    assert "뺀 jar(좌표도 없고 어떤 소스도 그 패키지를 import 하지 않음): `lib/old-unused-0.9.jar`" in block
    assert "Maven 으로 옮기지 못한 build.xml 단계 — 빌드에 아직 필요한지 확인할 것:" in block
    assert "합성 경고:" in block
    assert "build.xml 에서 pom.xml 합성 (jar 6개: pom.properties 1 · 이름표 1 · 조회 1 · 미상 3)" in md
    assert "- `pom-synthesized`: Ant build.xml 만 있고 pom.xml 이 없는 프로젝트라" in md
    assert "- `jar-coordinates-unknown`: Ant 프로젝트의 jar 중 Maven 좌표를 찾지 못한 것이 있다" in md


def test_block_without_operator_checks_or_versions(cfg):
    """No handover data at all (the early "no version changes" return): the block still comes first."""
    r = make_report(pom_synthesis=synthesis(), flags=list(FLAGS), advisories=[], manual_followups=[])
    md = render_markdown(r, language="en", cfg=cfg)
    assert md.index("## Handover") < md.index("### Ant project: pom.xml synthesized") \
        < md.index("No version changes and no library advisories recorded for this run.")


def test_skip_policy_wording(tmp_path, cfg):
    skip = cfg.model_copy(deep=True)
    skip.intake.unknown_jar = "skip"
    ps = synthesis(jars=[JarCoordinate(path=UNKNOWN, source="unknown", used=True)], unknown_jars=[UNKNOWN],
                   dropped_jars=[], build_xml_notes=[], warnings=[])
    block = "\n".join(pom_synthesis_lines(make_report(pom_synthesis=ps), "en", skip))
    assert ("**1 jar(s) without Maven coordinates** — not declared (`intake.unknown_jar: skip`): the build shows "
            "which ones the code needs; install each into Nexus and add it to the pom as a dependency:") in block
    assert CMD_1 in block and "system" not in block.split("```")[0]
    assert "Left out" not in block and "build.xml steps" not in block and "Synthesis warnings" not in block


def test_install_file_command_rules(cfg):
    assert jar_stem_version("a/b/acme-util-1.2.jar") == ("acme-util", "1.2")
    assert jar_stem_version("a\\b\\ojdbc6.jar") == ("ojdbc6", "")
    assert jar_stem_version("x_2.0.1-beta.jar") == ("x", "2.0.1-beta")
    jar = JarCoordinate(path="lib/foo-bar.jar", artifact_id="foo", version="3.1")
    assert install_file_command("lib/foo-bar.jar", "g.x", jar) == (
        "mvn install:install-file -Dfile=lib/foo-bar.jar -DgroupId=g.x -DartifactId=foo -Dversion=3.1 "
        "-Dpackaging=jar")
    assert install_file_command("Web Content\\lib\\a-1.0.jar", "g.x") == (
        "mvn install:install-file -Dfile=\"Web Content/lib/a-1.0.jar\" -DgroupId=g.x -DartifactId=a -Dversion=1.0 "
        "-Dpackaging=jar")
    company = cfg.model_copy(deep=True)
    company.intake.synth_group_id = "com.acme.legacy"
    cmds = unknown_jar_commands(synthesis(), company)
    assert cmds[0].startswith("mvn install:install-file -Dfile=" + UNKNOWN + " -DgroupId=com.acme.legacy ")
    assert unknown_jar_commands(synthesis()) == [CMD_1, CMD_2]          # no cfg: the pom's own groupId


def test_long_lists_are_capped(cfg):
    many = [f"lib/u{i}.jar" for i in range(POM_SYNTH_LIST_MAX + 10)]
    ps = synthesis(jars=[], unknown_jars=many)
    lines = pom_synthesis_lines(make_report(pom_synthesis=ps), "en", cfg)
    assert sum(1 for x in lines if x.startswith("mvn install:install-file")) == POM_SYNTH_LIST_MAX
    assert "… 10 more (report.json)" in lines


def test_nothing_without_a_synthesis(tmp_path, cfg):
    for ps in (None, PomSynthesis(ok=False, error="no source root")):
        md = render_markdown(make_report(pom_synthesis=ps), language="en", cfg=cfg)
        assert "Ant project" not in md and "synthesized from build.xml" not in md


def test_attention_code_and_json_round_trip(tmp_path, cfg):
    r = ant_report(tmp_path, cfg)
    line = report_result_line(r, 0)
    attn = line.split("attn=")[1].split()[0].split(",")
    assert "jar-coordinates-unknown" in attn and "pom-synthesized" not in attn
    back = RunReport.model_validate_json(r.model_dump_json())
    assert back.pom_synthesis is not None and back.pom_synthesis.unknown_jars == [UNKNOWN, UNKNOWN_BARE]
    assert back.pom_synthesis.counts == {"pom_properties": 1, "name_table": 1, "sha1_lookup": 1, "unknown": 2,
                                         "manifest": 1}
