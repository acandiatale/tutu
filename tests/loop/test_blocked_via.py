"""X1 via (2026-10-22, fourth company project): the javax API jar is only the *type's* jar — the dependency tree
says which library drags it in, and the blocked suggestion names that library (ladder (1)–(5))."""
from __future__ import annotations

from sbmig.core.config import ArtifactSpec
from sbmig.core.models import (GAV, BlockedItem, BlockReason, BuildError, ErrorCluster, ErrorKind, JavaxApiJar,
                               Phase)
from sbmig.loop.blocked_registry import (compose_suggestion, fill_via, is_javax_api_artifact,
                                         match_javax_jar)

from .fakes import FakeBuildRunner, bad_build, compile_error, journal_events
from .test_orchestrator import make_orch

API = "javax.persistence:javax.persistence-api:2.2"
QRY = "src/main/java/com/x/ReportDao.java"
MSG = "incompatible types: jakarta.persistence.Query cannot be converted to javax.persistence.Query"


def x1(artifact: str | None = API, module: str = "shop-back", **kw) -> BlockedItem:
    err = BuildError(kind=ErrorKind.COMPILE, message=MSG, file=QRY, line=42, module=module,
                     signature=f"compile|incompatible|{QRY}")
    return BlockedItem(id="blk-q", reason=kw.pop("reason", BlockReason.EXTERNAL_JAVAX_API), phase=Phase.SPRING,
                       cluster_id="c", errors=[err], artifact=GAV.parse(artifact) if artifact else None,
                       owner_class="javax.persistence.Query", required_type="javax.persistence.Query",
                       found_type="jakarta.persistence.Query", **kw)


def jar(via: str | None = "org.qlrm:qlrm:2.1.1", module: str | None = "shop-back", artifact: str = API,
        declared: bool = False) -> JavaxApiJar:
    root = f"com.acme:{module or 'shop'}:jar:1.0.0"
    path = [root] + ([via] if via else []) + [artifact]
    return JavaxApiJar(artifact=GAV.parse(artifact), module=module, scope="compile", path=path,
                       via=GAV.parse(via) if via else None, declared=declared)


def company(cfg):
    c = cfg.model_copy(deep=True)
    c.artifacts.parent = ArtifactSpec(group_id="com.company", artifact_id="base-parent", version="4.0.0")
    return c


def test_javax_api_artifact_predicate():
    assert is_javax_api_artifact(GAV.parse(API))
    assert is_javax_api_artifact(GAV.parse("org.hibernate.javax.persistence:hibernate-jpa-2.1-api:1.0.2"))
    assert is_javax_api_artifact(GAV.parse("jakarta.persistence:jakarta.persistence-api:2.2.3"))
    assert is_javax_api_artifact(GAV.parse("jakarta.servlet:jakarta.servlet-api:4.0.4"))
    assert not is_javax_api_artifact(GAV.parse("jakarta.persistence:jakarta.persistence-api:3.2.0"))
    assert not is_javax_api_artifact(GAV.parse("org.qlrm:qlrm:2.1.1"))
    assert not is_javax_api_artifact(None)


def test_qlrm_known_jakarta_library(cfg):
    item = x1()
    assert "run `mvn dependency:tree -Dincludes=javax.persistence:javax.persistence-api`" in \
        compose_suggestion(item, cfg)                                         # (5) before the inventory
    assert fill_via([item], [jar()], cfg) == 1
    assert item.via_direct.coordinates == "org.qlrm:qlrm:2.1.1"
    assert item.via == ["com.acme:shop-back:jar:1.0.0", "org.qlrm:qlrm:2.1.1", API]
    assert "via: com.acme:shop-back:jar:1.0.0 → org.qlrm:qlrm:2.1.1 → " + API in item.notes
    s = item.suggestion
    assert s.startswith(f"`{API}` comes through `org.qlrm:qlrm:2.1.1` (direct dependency of module `shop-back`)")
    assert "moved to jakarta.* in 4.0.1 — raise it to 4.2.1 (requires Java 17)" in s
    assert "handler" not in s                                    # v2: no deterministic handler does it
    assert "upgrade `javax.persistence:javax.persistence-api` to a Jakarta build" not in s


def test_qlrm_old_coordinates_relocate(cfg):
    item = x1()
    fill_via([item], [jar(via="ch.simas.qlrm:qlrm:1.7.1")], cfg)
    s = item.suggestion
    assert s.startswith("`ch.simas.qlrm:qlrm:1.7.1` is `org.qlrm:qlrm` under its earlier coordinates → "
                        "`org.qlrm:qlrm:4.2.1` (jakarta.* since 4.0.1, requires Java 17)")
    assert "relocate the declaration (it drags in" in s and "handler" not in s


def test_in_house_library(cfg):
    item = x1()
    fill_via([item], [jar(via="com.company.web:legacy-dao:1.4.0")], company(cfg))
    assert item.suggestion == (
        "`com.company.web:legacy-dao:1.4.0` is an in-house artifact still compiled against javax (it drags in "
        f"`{API}`): it needs a Jakarta build on the 4.0.0 line; add it to `artifacts.optional` with that version "
        "so the optional bump moves it")


def test_unknown_library_and_module_preference(cfg):
    item = x1(module="shop-boot")
    inv = [jar(via="org.other:lib:1.0", module="shop-back"), jar(via="com.vendor:reports:2.3", module="shop-boot")]
    assert match_javax_jar(item, inv).via.key == "com.vendor:reports"
    fill_via([item], inv, cfg)
    assert item.suggestion == (f"`{API}` comes through `com.vendor:reports:2.3` (direct dependency of module "
                               "`shop-boot`), which is still compiled against javax: upgrade it to a Jakarta release "
                               "(check its changelog) or replace it; left unchanged on purpose")


def test_known_library_preferred_over_unknown(cfg):
    item = x1(module="elsewhere")
    inv = [jar(via="org.other:lib:1.0"), jar(via="org.qlrm:qlrm:2.1.1")]
    assert match_javax_jar(item, inv).via.key == "org.qlrm:qlrm"


def test_declared_api_jar(cfg):
    item = x1()
    fill_via([item], [jar(via=None, declared=True)], cfg)
    assert item.via_direct is None and len(item.via) == 2
    assert not any(n.startswith("via: ") for n in item.notes)
    assert item.suggestion == (f"`{API}` is declared in `shop-back/pom.xml` — remove it (its jakarta.* successor "
                               "comes with the Spring Boot starters)")


def test_no_artifact_type_package_lookup(cfg):
    item = x1(artifact=None)
    other = jar(artifact="org.hibernate.javax.persistence:hibernate-jpa-2.1-api:1.0.2", via="org.qlrm:qlrm:2.1.1")
    fill_via([item], [other], cfg)                     # same API family, different coordinates
    assert item.artifact.key == "org.hibernate.javax.persistence:hibernate-jpa-2.1-api"
    assert item.via_direct.key == "org.qlrm:qlrm"


def test_no_via_found(cfg):
    item = x1()
    fill_via([item], [jar(artifact="javax.servlet:javax.servlet-api:4.0.1", via="x:y:1")], cfg)
    assert item.via == [] and item.via_direct is None
    assert "the javac error only names the javax type; run `mvn dependency:tree " \
           "-Dincludes=javax.persistence:javax.persistence-api` in the module" in item.suggestion


def test_non_javax_artifact_and_other_reasons_untouched(cfg):
    lib = x1(artifact="com.company:legacy-web:1.4", suggestion="keep me")
    rf = x1(reason=BlockReason.REPEATED_FAILURE, suggestion="needs a human")
    fill_via([lib, rf], [jar()], cfg)
    assert lib.suggestion == "keep me" and lib.via == [] and rf.suggestion == "needs a human" and rf.via == []


# ---------------------------------------------------------------- orchestrator _finalize
def _seed(orch) -> None:
    err = compile_error(QRY, msg=MSG, sig="compile|incompatible|q")
    cluster = ErrorCluster(id="compile:q", kind=ErrorKind.COMPILE, file=QRY, errors=[err])
    item, _ = orch.blocked.block(cluster, BlockReason.EXTERNAL_JAVAX_API, Phase.SPRING,
                                 required_type="javax.persistence.Query", found_type="jakarta.persistence.Query",
                                 owner_class="javax.persistence.Query", artifact=GAV.parse(API))
    item.suggestion = compose_suggestion(item, orch.cfg)


def _q_build(ws, call):
    return bad_build(compile_error(QRY, msg=MSG, sig="compile|incompatible|q"), goal=call.get("goal", "compile"))


def test_finalize_inventory_fills_report(cfg, ws):
    calls: list = []

    def fake_inv(ws_, cfg_, profile):
        calls.append(profile)
        return [jar(module=None), jar(artifact="javax.inject:javax.inject:1", via="com.querydsl:querydsl-apt:5.1.0")]
    orch = make_orch(cfg, ws, build=FakeBuildRunner(ws, fn=_q_build), javax_inventory=fake_inv)
    _seed(orch)
    report = orch.run()
    assert len(calls) == 1
    assert [j.artifact.key for j in report.javax_api_jars] == ["javax.persistence:javax.persistence-api",
                                                              "javax.inject:javax.inject"]
    assert "javax-api-on-classpath" in report.flags
    fu = next(f for f in report.manual_followups if f.startswith("2 javax API jar(s) still on the classpath"))
    assert fu.endswith("need Jakarta builds: `org.qlrm:qlrm:2.1.1`, `com.querydsl:querydsl-apt:5.1.0`")
    b = next(b for b in report.blocked if b.id.startswith("blk-"))
    assert b.via_direct.coordinates == "org.qlrm:qlrm:2.1.1" and "raise it to 4.2.1" in b.suggestion
    ev = [e for e in journal_events(ws) if e["event"] == "javax_api_inventory"]
    assert ev and ev[0]["jars"] == [API, "javax.inject:javax.inject:1"]
    assert ev[0]["via"] == ["org.qlrm:qlrm:2.1.1", "com.querydsl:querydsl-apt:5.1.0"]


def test_finalize_inventory_empty(cfg, ws):
    orch = make_orch(cfg, ws, javax_inventory=lambda w, c, p: [])
    report = orch.run()
    assert report.javax_api_jars == [] and "javax-api-on-classpath" not in report.flags
    assert [e for e in journal_events(ws) if e["event"] == "javax_api_inventory"][0]["jars"] == []


def test_finalize_inventory_disabled_by_config(cfg, ws):
    cfg.report.javax_api_inventory = False
    called: list = []
    orch = make_orch(cfg, ws, javax_inventory=lambda w, c, p: called.append(1) or [jar()])
    report = orch.run()
    assert not called and report.javax_api_jars == []


def test_finalize_inventory_exception_never_fails_the_run(cfg, ws):
    def boom(w, c, p):
        raise RuntimeError("mvn exploded")
    report = make_orch(cfg, ws, javax_inventory=boom).run()
    assert report.success and report.javax_api_jars == []


def test_no_inventory_with_a_fake_build_runner_by_default(cfg, ws, monkeypatch):
    import sbmig.build.deptree as dt
    monkeypatch.setattr(dt, "javax_api_inventory", lambda *a, **k: (_ for _ in ()).throw(AssertionError("mvn")))
    report = make_orch(cfg, ws).run()
    assert report.javax_api_jars == []
