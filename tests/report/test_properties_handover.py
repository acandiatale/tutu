"""Round 8: Handover "Properties to set before running" (externalized-config rows, guide, from_here, console)."""
from __future__ import annotations

import io

from sbmig.core.models import PropertyFinding, PropertyInventory, PropertyRef
from sbmig.report import build_handover, render_markdown
from sbmig.report.handover import env_name, property_rows, split_property_row
from sbmig.report.writer import console_summary, properties_console_line

from .test_handover import EFFECTIVE, as_is_profile, final_tree, make_report

PH = "module/boot-jar/src/main/resources/application.properties"
DS_KEYS = ["spring.datasource.url", "spring.datasource.username", "spring.datasource.password",
           "spring.datasource.driver-class-name"]


def ref(key: str, source: str = "value-annotation", file: str = "src/main/java/com/x/MailService.java",
        line: int | None = 12, default: str | None = None, scope: str = "main") -> PropertyRef:
    return PropertyRef(key=key, source=source, file=file, line=line, default=default, scope=scope)  # type: ignore[arg-type]


def inventory(placeholders: bool = True) -> PropertyInventory:
    findings = [
        PropertyFinding(key="mail.host", status="external", boot_keys=["mail.host"],
                        refs=[ref("mail.host", line=i) for i in (1, 2, 3, 4, 5)]),
        PropertyFinding(key="java:comp/env/jdbc/catalogDS", status="external", boot_keys=DS_KEYS, supplied_by="jndi",
                        refs=[ref("java:comp/env/jdbc/catalogDS", "jndi", "src/main/webapp/WEB-INF/app.xml", 9)],
                        note="JNDI DataSource → spring.datasource.*"),
        PropertyFinding(key="catalog.region", status="external", supplied_by="context-param",
                        boot_keys=["server.servlet.context-parameters.catalog.region"],
                        refs=[ref("catalog.region", "context-param", "src/main/webapp/WEB-INF/web.xml", 7,
                                  default="kr")]),
        PropertyFinding(key="catalog.home", status="external", supplied_by="system-property", boot_keys=["catalog.home"],
                        refs=[ref("catalog.home", "system-property", "src/main/java/com/x/Home.java", 20)],
                        note="read via System.getProperty: pass -Dcatalog.home=… on the java command line"),
        PropertyFinding(key="HOME_DIR", status="external", boot_keys=[],
                        refs=[ref("HOME_DIR", "system-env", "src/main/java/com/x/Home.java", 30)],
                        note="environment variable (System.getenv / ${env.*}): set it in the runtime environment"),
        PropertyFinding(key="contextConfigLocation", status="external", supplied_by="context-param", boot_keys=[],
                        refs=[ref("contextConfigLocation", "context-param", "src/main/webapp/WEB-INF/web.xml", 3,
                                  default="/WEB-INF/applicationContext.xml")],
                        note="Spring XML config is imported via @ImportResource (legacy-config/)"),
        PropertyFinding(key="catalog.title", status="defined", refs=[ref("catalog.title")],
                        defined_in=["src/main/resources/catalog.properties"], boot_keys=["catalog.title"]),
        PropertyFinding(key="catalog.page-size", status="defaulted", refs=[ref("catalog.page-size", default="10")],
                        boot_keys=["catalog.page-size"]),
    ]
    inv = PropertyInventory(findings=findings)
    if placeholders:
        inv.placeholder_files = [PH]
        inv.placeholders_written = ["catalog.home", "mail.host", "server.servlet.context-parameters.catalog.region",
                                    *DS_KEYS]
    return inv


FOLLOWUP = (f"7 externalized setting(s) have placeholder values (__SET_ME__) in {PH} — set them before running "
            "(see Handover 'Properties to set')")


def rows_by_key(rows: list[str]) -> dict[str, list[str]]:
    return {split_property_row(r)[0]: split_property_row(r) for r in rows}


def test_rows_external_only_and_cells(cfg) -> None:
    rows = property_rows(inventory(), cfg)
    by = rows_by_key(rows)
    assert list(by) == ["mail.host", "java:comp/env/jdbc/catalogDS", "catalog.region", "catalog.home", "HOME_DIR",
                        "contextConfigLocation"]
    assert "catalog.title" not in by and "catalog.page-size" not in by           # defined / defaulted excluded
    assert all(len(split_property_row(r)) == 5 for r in rows)
    mail = by["mail.host"]
    assert mail[1] == "unknown"
    assert mail[2] == ("src/main/java/com/x/MailService.java:1, src/main/java/com/x/MailService.java:2, "
                       "src/main/java/com/x/MailService.java:3 (+2 more)")
    assert mail[3] == "mail.host=__SET_ME__" and mail[4] == "MAIL_HOST"
    ds = by["java:comp/env/jdbc/catalogDS"]
    assert ds[1] == "jndi" and ds[2] == "src/main/webapp/WEB-INF/app.xml:9"
    assert ds[3].startswith("spring.datasource.url=__SET_ME__, spring.datasource.username=__SET_ME__")
    assert ds[4] == "SPRING_DATASOURCE_URL"
    region = by["catalog.region"]
    assert region[3] == "server.servlet.context-parameters.catalog.region=kr"          # as-is value kept
    assert region[4] == "SERVER_SERVLET_CONTEXT_PARAMETERS_CATALOG_REGION"
    assert by["catalog.home"][4] == "-Dcatalog.home"                                 # System.getProperty only
    env = by["HOME_DIR"]                                                              # boot_keys == [] → note
    assert env[3].startswith("environment variable (System.getenv") and env[4] == "environment variable HOME_DIR"
    skipped = by["contextConfigLocation"]                                             # rule skip: note, nothing to set
    assert skipped[3].startswith("Spring XML config is imported") and skipped[4] == "-"


def test_only_skipped_context_params_give_no_rows(cfg) -> None:
    inv = PropertyInventory(findings=[f for f in inventory().findings if f.key in ("contextConfigLocation",
                                                                                  "catalog.title")])
    assert property_rows(inv, cfg) == []
    boot_default = PropertyFinding(key="server.port", status="external", boot_keys=[], refs=[ref("server.port")],
                                   note="Boot default")
    assert property_rows(PropertyInventory(findings=[boot_default]), cfg) == []


def test_rows_refs_cap_from_config_and_nothing_written(cfg) -> None:
    cfg.externalized.max_refs_per_key = 1
    by = rows_by_key(property_rows(inventory(placeholders=False), cfg))
    assert by["mail.host"][2] == "src/main/java/com/x/MailService.java:1 (+4 more)"
    assert by["mail.host"][3] == "-"                                                 # placeholders not written


def test_env_name() -> None:
    assert env_name("spring.datasource.driver-class-name") == "SPRING_DATASOURCE_DRIVER_CLASS_NAME"
    assert env_name("db.url") == "DB_URL"
    assert env_name("app.list[0]") == "APP_LIST_0"


def test_cells_never_contain_pipe(cfg) -> None:
    inv = PropertyInventory(findings=[PropertyFinding(key="a|b", status="external", boot_keys=[],
                                                      refs=[ref("a|b")], note="x | y")])
    cells = split_property_row(property_rows(inv, cfg)[0])
    assert cells[0] == "a/b" and cells[3] == "x / y"


def test_handover_guide_and_from_here(tmp_path, cfg) -> None:
    report = make_report(properties=inventory())
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    assert len(h.properties_to_set) == 6
    assert h.properties_guide and any("__SET_ME__" in g for g in h.properties_guide)
    item = [x for x in h.from_here if "externalized setting(s)" in x]
    assert item == [f"5 externalized setting(s) need values before the first start — placeholders (__SET_ME__) in "
                    f"{PH}; see 'Properties to set'"]


def test_from_here_keeps_restructure_followup(tmp_path, cfg) -> None:
    report = make_report(properties=inventory())
    report.manual_followups.append(FOLLOWUP)
    h = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    item = [x for x in h.from_here if "externalized setting(s)" in x]
    assert item == [FOLLOWUP]


def test_profile_fallback_and_no_cfg(tmp_path) -> None:
    prof = as_is_profile()
    prof.properties = inventory(placeholders=False)
    report = make_report(profile=prof)
    assert report.properties is None
    h = build_handover(prof, final_tree(tmp_path), report, effective=EFFECTIVE, cfg=None)
    assert len(h.properties_to_set) == 6
    assert h.properties_guide == []                                                  # cfg None → no guide
    assert any("no placeholders were written" in x for x in h.from_here)


def test_markdown_block_before_from_here(tmp_path, cfg) -> None:
    report = make_report(properties=inventory())
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    md = render_markdown(report)
    i = md.index("### Properties to set before running")
    assert md.index("**Merge conflicts**") < i if "**Merge conflicts**" in md else True
    assert i < md.index("### From here on, your team")
    assert "| key | before migration | referenced at | written as | set via |" in md
    assert "| `mail.host` | unknown |" in md
    assert "How to supply them:" in md
    assert f"Packaged placeholders: `{PH}` (grep `__SET_ME__`)" in md
    line = properties_console_line(report)
    assert line == f"properties to set: 5 (placeholders in {PH})"            # skipped context-param not counted
    buf = io.StringIO()
    from rich.console import Console
    console_summary(report, Console(file=buf, width=200))
    assert line in buf.getvalue()


def test_markdown_absent_without_external(tmp_path, cfg) -> None:
    inv = PropertyInventory(findings=[f for f in inventory().findings if f.status != "external"])
    report = make_report(properties=inv)
    report.handover = build_handover(as_is_profile(), final_tree(tmp_path), report, effective=EFFECTIVE, cfg=cfg)
    assert report.handover.properties_to_set == [] and report.handover.properties_guide == []
    assert not any("externalized setting(s)" in x for x in report.handover.from_here)
    md = render_markdown(report)
    assert "Properties to set" not in md
    assert properties_console_line(report) is None


def test_nothing_recorded_return_considers_properties(cfg) -> None:
    from sbmig.core.models import HandoverSummary
    from sbmig.report.handover import property_rows as rows
    report = make_report(advisories=[], properties=inventory())
    report.handover = HandoverSummary(properties_to_set=rows(inventory(), cfg))
    md = render_markdown(report)
    assert "### Properties to set before running" in md
    assert "No version changes and no library advisories recorded" not in md


def test_report_json_carries_properties(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from sbmig.report import write_report
    report = make_report(properties=inventory())
    write_report(report, SimpleNamespace(run_dir=tmp_path / "run"))
    data = json.loads((tmp_path / "run" / "report.json").read_text())
    assert data["properties"]["placeholder_files"] == [PH]
    assert any(f["key"] == "mail.host" for f in data["properties"]["findings"])
