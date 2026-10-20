"""Round 8: externalized-config sentinel placeholders written by the restructure (layout + msa_pom)."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.config import Config
from sbmig.core.models import CaseType, PropertyFinding, PropertyInventory, PropertyRef
from sbmig.restructure import Restructurer

from .helpers import HEAD, make_ws, profile
from .test_restructurer import (boot2_files, boot2_profile, kept_boot_files, msa_files, msa_profile, multi_files,
                                multi_profile)

BEGIN = "# --- sbmig externalized-config: keys the code reads that no packaged resource defined."
END = "# --- end sbmig externalized-config"
PH = "module/boot-jar/src/main/resources/application.properties"


def ref(key: str, source: str = "value-annotation", file: str = "src/main/java/com/x/MailService.java",
        line: int | None = 12, default: str | None = None, scope: str = "main") -> PropertyRef:
    return PropertyRef(key=key, source=source, file=file, line=line, default=default, scope=scope)  # type: ignore[arg-type]


def ext(key: str, *refs: PropertyRef, boot_keys: list[str] | None = None, note: str = "") -> PropertyFinding:
    return PropertyFinding(key=key, status="external", refs=list(refs) or [ref(key)],
                           boot_keys=[key] if boot_keys is None else boot_keys, note=note)


def inventory() -> PropertyInventory:
    return PropertyInventory(findings=[
        ext("mail.host"),
        ext("java:comp/env/jdbc/orderDS", ref("java:comp/env/jdbc/orderDS", "jndi", "src/main/resources/ctx.xml", 4),
            boot_keys=["spring.datasource.url", "spring.datasource.username", "spring.datasource.password"],
            note="JNDI DataSource → spring.datasource.*"),
        ext("app.mode", ref("app.mode", "context-param", "src/main/webapp/WEB-INF/web.xml", 9, default="batch"),
            boot_keys=["server.servlet.context-parameters.app.mode"]),
        ext("skipped.key", boot_keys=[]),                                      # boot default / rule skip
        ext("test.only", ref("test.only", file="src/test/java/com/x/T.java", line=3, scope="test")),
        ext("mail.host", ref("mail.host", file="src/main/java/com/x/Other.java", line=99)),   # duplicate key
        PropertyFinding(key="app.name", status="defined", refs=[ref("app.name")], defined_in=["application.yml"]),
        PropertyFinding(key="app.timeout", status="defaulted", refs=[ref("app.timeout", default="30")]),
    ])


def block(text: str) -> str:
    i = text.index(BEGIN)
    return text[i:text.index(END) + len(END)]


def single(tmp_path: Path, cfg: Config, name: str = "ph", inv: PropertyInventory | None = None, files=None):
    ws = make_ws(tmp_path, files or boot2_files(), name)
    prof = boot2_profile(ws)
    prof.properties = inventory() if inv is None else inv
    return ws, prof, Restructurer().apply(ws, prof, cfg, mode="layout")


def test_single_creates_file_next_to_yml(tmp_path: Path, cfg: Config) -> None:
    ws, _, res = single(tmp_path, cfg)
    assert res.ok, res.error
    text = (ws.root / PH).read_text()
    assert text.startswith(BEGIN)
    assert "# --- application.yml exists; placeholders go to application.properties (Boot loads both)" in text
    assert (ws.root / "module/boot-jar/src/main/resources/application.yml").exists()
    keys = ["mail.host", "server.servlet.context-parameters.app.mode", "spring.datasource.password",
            "spring.datasource.url", "spring.datasource.username", "test.only"]
    assert res.placeholders_written == keys
    assert res.placeholder_files == [PH]
    lines = text.splitlines()
    assert [ln.split("=", 1)[0] for ln in lines if not ln.startswith("#")] == keys       # sorted, deduped
    assert "mail.host=__SET_ME__" in lines
    assert "# from @Value src/main/java/com/x/MailService.java:12" in lines                # first finding wins
    assert "Other.java" not in text
    assert "server.servlet.context-parameters.app.mode=batch" in lines                    # as-is value kept
    assert "spring.datasource.url=__SET_ME__" in lines
    assert "# from JNDI java:comp/env/jdbc/orderDS (JNDI DataSource → spring.datasource.*)" in lines
    assert "skipped.key" not in text and "app.name" not in text and "app.timeout" not in text
    assert lines[-1] == END
    assert any("6 externalized-config placeholder(s) written to " + PH in n for n in res.notes)
    fu = [f for f in res.manual_followups if "externalized key(s)" in f]
    assert fu == [f"6 externalized key(s) have placeholder values (__SET_ME__) in {PH} — set them before "
                  "running (see Handover 'Properties to set')"]
    assert PH in res.changed_files and PH in res.generated_files
    assert not ws.is_dirty()                                                   # committed with the step


def test_appends_to_existing_file_and_is_idempotent(tmp_path: Path, cfg: Config) -> None:
    files = boot2_files()
    files["src/main/resources/application.properties"] = "server.port=8080\r\nspring.application.name=shop\r\n"
    ws, prof, res = single(tmp_path, cfg, "append", files=files)
    assert res.ok, res.error
    raw = (ws.root / PH).read_bytes()
    assert raw.startswith(b"server.port=8080\r\nspring.application.name=shop\r\n\r\n" + BEGIN.encode())
    assert b"\n" not in raw.replace(b"\r\n", b"")                              # CRLF kept throughout
    assert "application.yml exists" not in raw.decode()                       # the file existed: no note
    assert PH not in res.generated_files
    # second pass (already restructured): identical bytes
    res2 = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res2.ok and "already restructured" in res2.notes
    assert (ws.root / PH).read_bytes() == raw
    assert res2.placeholder_files == [PH] and res2.placeholders_written == res.placeholders_written
    # a changed inventory replaces the block, everything else stays byte-for-byte
    prof.properties = PropertyInventory(findings=[ext("only.one")])
    Restructurer().apply(ws, prof, cfg, mode="layout")
    new = (ws.root / PH).read_bytes().decode()
    assert new.startswith("server.port=8080\r\nspring.application.name=shop\r\n\r\n" + BEGIN)
    assert "only.one=__SET_ME__" in new and "mail.host" not in new and new.count(BEGIN) == 1


def test_already_restructured_project_gets_placeholders(tmp_path: Path, cfg: Config) -> None:
    root = (HEAD + "    <groupId>com.acme</groupId>\n    <artifactId>x</artifactId>\n    <version>1</version>\n"
            "    <packaging>pom</packaging>\n    <modules>\n        <module>module/comp-jar</module>\n"
            "        <module>module/boot-jar</module>\n    </modules>\n</project>\n")
    existing = "# own\nfoo=bar\n"
    ws = make_ws(tmp_path, {"pom.xml": root, "module/boot-jar/src/main/resources/application.properties": existing},
                 "done")
    prof = profile(ws, CaseType.SPRING_BOOT, artifact_id="x", version="1")
    prof.properties = PropertyInventory(findings=[ext("mail.host")])
    res = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res.ok and "already restructured" in res.notes
    text = (ws.root / PH).read_text()
    assert text.startswith(existing + "\n" + BEGIN) and "mail.host=__SET_ME__\n" + END + "\n" in text
    assert res.placeholder_files == [PH]


def test_merge_with_kept_boot_module(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, kept_boot_files(), "kept")
    prof = multi_profile(ws)
    prof.properties = PropertyInventory(findings=[ext("mail.host", ref("mail.host", file="core/src/main/java/R.java"))])
    res = Restructurer().apply(ws, prof, cfg, mode="layout")
    assert res.ok and res.kept_boot_module == "module/boot-jar"
    text = (ws.root / PH).read_text()
    assert "mail.host=__SET_ME__" in text and "application.yml exists" in text
    assert (ws.root / "module/boot-jar/src/main/resources/application.yml").read_text() == "server:\n  port: 8081\n"


def test_merge_generated_boot_and_wrap(tmp_path: Path, cfg: Config) -> None:
    for policy in ("merge", "wrap"):
        ws = make_ws(tmp_path, multi_files(), f"multi-{policy}")
        prof = multi_profile(ws)
        prof.properties = PropertyInventory(findings=[ext("mail.host")])
        res = Restructurer().apply(ws, prof, cfg, mode="layout", params={"multi_module_policy": policy})
        assert res.ok, res.error
        assert res.placeholder_files == [PH], policy
        assert "mail.host=__SET_ME__" in (ws.root / PH).read_text()
        if policy == "merge":                                     # web's application.properties moved to boot-jar
            assert (ws.root / PH).read_text().startswith("a=b\n\n" + BEGIN)
        else:                                                     # the WAR module keeps its own file untouched
            assert (ws.root / "module/web/src/main/resources/application.properties").read_text() == "a=b\n"


def test_test_only_refs_follow_config(tmp_path: Path, cfg: Config) -> None:
    cfg.externalized.include_test_refs = False
    ws, _, res = single(tmp_path, cfg, "notest")
    assert "test.only" not in res.placeholders_written and "test.only" not in (ws.root / PH).read_text()
    assert "mail.host" in res.placeholders_written


def test_disabled_or_empty_writes_nothing(tmp_path: Path, cfg: Config) -> None:
    cfg.externalized.write_placeholders = False
    ws, _, res = single(tmp_path, cfg, "off")
    assert not (ws.root / PH).exists() and res.placeholder_files == [] and res.placeholders_written == []
    cfg.externalized.write_placeholders = True
    cfg.externalized.enabled = False
    ws, _, res = single(tmp_path, cfg, "disabled")
    assert not (ws.root / PH).exists() and res.placeholder_files == []
    cfg.externalized.enabled = True
    for name, inv in (("empty", PropertyInventory()),
                      ("nokeys", PropertyInventory(findings=[ext("x", boot_keys=[])]))):
        ws, _, res = single(tmp_path, cfg, name, inv=inv)
        assert not (ws.root / PH).exists() and res.placeholder_files == []
        assert not any("externalized" in f for f in res.manual_followups)
        assert not any("placeholder" in n for n in res.notes)


def test_msa_each_service_gets_its_own_keys(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, msa_files(), "msa")
    prof = msa_profile(ws)
    prof.properties = PropertyInventory(findings=[
        ext("order.url", ref("order.url", file="order-service/src/main/java/com/acme/order/OrderApp.java")),
        ext("user.url", ref("user.url", file="user-service/src/main/java/U.java")),
        ext("shared.key", ref("shared.key", file="common/src/main/java/C.java")),
    ])
    res = Restructurer().apply(ws, prof, cfg, mode="msa_pom")
    assert res.ok, res.error
    order = ws.root / "order-service/src/main/resources/application.properties"
    text = order.read_text()
    assert "order.url=__SET_ME__" in text and "shared.key=__SET_ME__" in text and "user.url" not in text
    # user-service has no src/main/java → no resources directory is created for it
    assert not (ws.root / "user-service/src/main/resources").exists()
    assert res.placeholder_files == ["order-service/src/main/resources/application.properties"]
    assert res.placeholders_written == ["order.url", "shared.key"]
    assert sum("externalized key(s)" in f for f in res.manual_followups) == 1
    res2 = Restructurer().apply(ws, prof, cfg, mode="msa_pom")
    assert res2.ok and order.read_text() == text                               # idempotent


def test_optional_bump_writes_nothing(tmp_path: Path, cfg: Config) -> None:
    ws = make_ws(tmp_path, boot2_files(), "bump")
    prof = boot2_profile(ws)
    prof.properties = inventory()
    res = Restructurer().apply(ws, prof, cfg, mode="optional_bump")
    assert res.ok and res.placeholder_files == []
    assert not any(p.name == "application.properties" for p in ws.root.rglob("*") if ".git" not in p.parts)
