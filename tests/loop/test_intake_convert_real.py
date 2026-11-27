"""The real conversion pipeline through the orchestrator: a run in flow `full` with the framework mode `convert` on a
small framework project replaces the working tree by what `sbmig.fwconvert.pipeline.convert_project` generates, and
the one analysis of the run then sees an ordinary multi-module Spring Boot project.

`tests/loop/test_intake_convert.py` checks the intake step with a fake `convert_project`; this file runs the real one.
No Maven, no network: the build runner is a fake that answers every call with success (so the as-is effective-pom
capture captures nothing), `convert.versions` is pinned, the analyzer and the planner are the real ones."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.analyze import analyze_project
from sbmig.core.models import CaseType, Phase
from sbmig.core.workspace import Workspace
from sbmig.loop.convert import CONVERT_COMMIT, FLAG_CONVERTED
from sbmig.plan import build_plan

from .fakes import FakeBuildRunner, journal_events, ok_build
from .test_orchestrator import make_orch

MYBATIS = "org.mybatis.spring.boot:mybatis-spring-boot-starter"
PROJECT = "shopApp"
PACKAGE = "com.example.shop.shopApp"
MODULES = [f"{PROJECT}-boot", f"{PROJECT}-domain", f"{PROJECT}-service", f"{PROJECT}-store"]

ROOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example.shop</groupId>
  <artifactId>shop-app</artifactId>
  <version>1.0.0</version>
  <packaging>war</packaging>
  <dependencies>
    <dependency>
      <groupId>com.example.fw</groupId>
      <artifactId>example-fw-core</artifactId>
      <version>1.0.0</version>
    </dependency>
  </dependencies>
</project>
"""

ORDER_SERVICE = """<?xml version="1.0" encoding="UTF-8"?>
<service name="order-service">
  <activity name="list" class="com.example.fw.activity.ExampleMyBatisSearch">
    <property name="sql-key" value="order.getList"/>
    <property name="result-key" value="rows"/>
  </activity>
  <activity name="save" class="com.example.fw.activity.ExampleMyBatisUpdate">
    <property name="sql-key" value="order.save"/>
  </activity>
  <activity name="router" class="com.example.fw.activity.ExampleDefaultRouter">
    <transition name="find" value="list"/>
    <transition name="save" value="save"/>
  </activity>
</service>
"""

ORDER_SQL = """<?xml version="1.0" encoding="UTF-8"?>
<queryMap desc="orders">
  <query id="order.getList" isNamed="true"><![CDATA[ SELECT ORDER_ID, AMOUNT FROM TB_ORDER WHERE CUST = :cust ]]></query>
  <query id="order.save" isNamed="false"><![CDATA[ INSERT INTO TB_ORDER (CUST, AMOUNT) VALUES (?, ?) ]]></query>
</queryMap>
"""

ORDER_JSP = """<%@ page contentType="text/html; charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jsp" %>
<html><body>
<a href="${pageContext.request.contextPath}/SH1/SH1A10/order-list.mvc">orders</a>
<script src="${pageContext.request.contextPath}/js/order.js"></script>
</body></html>
"""

ORDER_JS = """function load(tran) {
  var url = "/shop/commonAjax.json?ServiceName=order-service&" + tran + "=1&cust=7";
  return url;
}
"""

APP_CONTEXT = """<?xml version="1.0" encoding="UTF-8"?>
<beans xmlns="http://www.springframework.org/schema/beans">
  <bean id="dataSource" class="org.apache.commons.dbcp2.BasicDataSource">
    <property name="driverClassName" value="org.h2.Driver"/>
    <property name="url" value="jdbc:h2:mem:shop"/>
    <property name="username" value="sa"/>
  </bean>
</beans>
"""

TREE = {
    "pom.xml": ROOT_POM,
    "src/main/resources/service/order-service.xml": ORDER_SERVICE,
    "src/main/resources/sql/order.example_sql": ORDER_SQL,
    "src/main/resources/applicationContext.xml": APP_CONTEXT,
    "WebContent/WEB-INF/jsp/shop/order-list.jsp": ORDER_JSP,
    "WebContent/WEB-INF/jsp/common/header.jsp": "<div>head</div>\n",
    "WebContent/js/order.js": ORDER_JS,
    "WebContent/WEB-INF/web.xml": "<web-app/>\n",
}


def _cfg(cfg):
    c = cfg.model_copy(deep=True)
    c.framework.artifacts = ["example-fw-core"]
    c.framework.mode = "convert"
    c.convert.versions = {MYBATIS: "9.9.9"}           # pinned: nothing is built, no lookup
    # the company layout with role parents (the local e2e config pins the older layout)
    c.layout.module_dir = ""
    c.layout.boot_module = "{name}-boot"
    c.layout.comp_module = "{name}-back"
    c.layout.front_module = "{name}-front"
    c.layout.boot_as_is = "in-place"
    return c


def _ws(tmp_path: Path) -> Workspace:
    src = tmp_path / "src" / "shop-app"
    for rel, text in TREE.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return Workspace.from_input(src, tmp_path / "out", run_id="t-conv-real")


def _run(cfg, ws, stop_after: Phase):
    orch = make_orch(cfg, ws, analyzer=analyze_project, planner=build_plan,
                     build=FakeBuildRunner(ws, fn=lambda w, call: ok_build(call.get("goal") or "compile")))
    return orch, orch.run(stop_after=stop_after)


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(root).parts)


def test_the_tree_becomes_the_generated_project_and_is_the_second_commit(cfg, tmp_path):
    ws = _ws(tmp_path)
    _orch, report = _run(_cfg(cfg), ws, Phase.INTAKE)
    assert "aborted" not in report.flags, report.manual_followups
    assert FLAG_CONVERTED in report.flags
    files = _files(ws.root)
    # nothing of the as-is tree is left, the generated tree is there
    assert not [f for f in files if f.startswith(("src/", "WebContent/"))], files
    assert "pom.xml" in files
    for m in MODULES:
        assert f"{m}/pom.xml" in files, m
    pkg = PACKAGE.replace(".", "/")
    assert f"{PROJECT}-boot/src/main/java/{pkg}/ShopAppApplication.java" in files or any(
        f.startswith(f"{PROJECT}-boot/src/main/java/{pkg}/") and f.endswith("Application.java") for f in files), files
    assert any(f.startswith(f"{PROJECT}-store/src/main/java/{pkg}/store/") and f.endswith("MybatisStore.java")
               for f in files), files
    assert any(f.endswith("order-mapper.xml") for f in files), files
    assert any(f.endswith("/shop/order-list.jsp") for f in files), files
    root_pom = (ws.root / "pom.xml").read_text(encoding="utf-8")
    assert "<groupId>com.example.shop</groupId>" in root_pom and f"<artifactId>{PROJECT}</artifactId>" in root_pom
    assert "spring-boot-starter-parent" in root_pom and "example-fw-core" not in root_pom
    for m in MODULES:
        assert f"<module>{m}</module>" in root_pom
    yml = (ws.root / f"{PROJECT}-boot/src/main/resources/application.yml").read_text(encoding="utf-8")
    assert "jdbc:h2:mem:shop" in yml
    subjects = ws._git("log", "--reverse", "--format=%s").stdout.splitlines()
    assert subjects[:2] == ["sbmig: as-is snapshot", CONVERT_COMMIT], subjects
    assert not ws.is_dirty()

    conv = report.conversion
    assert conv is not None and conv.converted
    assert conv.project == PROJECT and conv.package == PACKAGE and conv.modules == MODULES
    assert conv.commit == ws.head() and conv.datasource == "configured:h2"
    assert conv.counts["services"] == 1 and conv.counts["mappers"] >= 1 and conv.counts["pages"] >= 1
    assert conv.errors == []
    [ev] = [e for e in journal_events(ws) if e.get("event") == "framework_converted"]
    assert ev["project"] == PROJECT and ev["package"] == PACKAGE and ev["modules"] == MODULES
    assert ev["framework"] == "Example" and ev["commit"] == conv.commit and ev["errors"] == 0
    summary = json.loads((ws.run_dir / "convert" / "summary.json").read_text(encoding="utf-8"))
    assert summary["project"] == PROJECT
    assert json.loads((ws.run_dir / "convert" / "queue.json").read_text(encoding="utf-8")) is not None


def test_the_analysis_and_the_plan_see_a_multi_module_spring_boot_project(cfg, tmp_path):
    ws = _ws(tmp_path)
    _orch, report = _run(_cfg(cfg), ws, Phase.PLAN)
    assert "aborted" not in report.flags, report.manual_followups
    prof = report.profile
    assert prof is not None
    assert prof.case_type == CaseType.SPRING_BOOT
    assert prof.is_multi_module
    assert sorted(p.path for p in prof.poms) == sorted(["pom.xml"] + [f"{m}/pom.xml" for m in MODULES])
    assert prof.boot_modules == [f"{PROJECT}-boot"]
    # the framework is gone from the tree: the check finds nothing, the scope stays full
    assert prof.framework is not None and not prof.framework.evidence and prof.framework.scope == "full"
    assert not prof.reduced_scope
    assert "framework-project" not in report.flags
    plan = report.plan
    assert plan is not None and (plan.scope or "full") == "full"
    steps = [s.id for s in plan.steps]
    assert steps[0] == "transform.rewrite"
    assert "restructure.boot_pom" in steps and "restructure.review" in steps and steps[-1] == "verify.build", steps
