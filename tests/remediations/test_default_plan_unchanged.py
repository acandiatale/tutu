"""The approval step was removed (docs/INTERFACES.md "v2.1z-ba"): a plan made with today's default configuration must be
exactly the plan the code with the approval step made under its defaults (`require_approval: false`, `always_approve:
[]`). The values below were produced by the planner of the commit before the removal on the same inputs; the only
differences allowed are the `requires_approval` field (gone) and the policy hash (the two settings left it).

Pinned here: every proposal (id, g:a, from → to, kind, risk labels, decision, the edit and the proposal hash) and every
residual, and that every proposal is applied (`to_apply`). The risk labels stay facts of the plan; they never changed
what is planned, only (with the old non-default settings) whether a proposal waited.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

from sbmig.core.config import RemediationConfig
from sbmig.remediations import models as M
from sbmig.remediations import planner as P

ROOT = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>root</artifactId>
  <version>1.0.0</version>
  <packaging>pom</packaging>
  <modules>
    <module>core</module>
    <module>other</module>
  </modules>
  <properties>
    <core.version>2.14.0</core.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>lib</artifactId>
      <version>1.2.3</version>
    </dependency>
    <dependency>
      <groupId>example</groupId>
      <artifactId>core-lib</artifactId>
      <version>${core.version}</version>
    </dependency>
  </dependencies>
</project>
"""
L_LIB, L_PROP = 18, 12  # line of <version>1.2.3</version> and of <core.version>


def _module(name: str, parent: str) -> str:
    return (
        f"<project>\n  <parent>\n    <groupId>example</groupId>\n    <artifactId>{parent}</artifactId>\n"
        f"    <version>1.0.0</version>\n  </parent>\n  <artifactId>{name}</artifactId>\n"
        f"  <dependencies>\n  </dependencies>\n</project>\n"
    )


class Source:
    """Repository versions (the evidence host is a placeholder)."""

    DATA = {
        "example:lib": ["1.2.3", "1.2.4", "2.0.0"],
        "example:core-lib": ["2.14.0", "2.14.3"],
        "example:t": ["1.0", "1.1"],
        "example:u": ["1.0", "2.0"],
        "example:m": ["1.0", "1.1"],
        "example:n": ["1.0", "1.1"],
    }

    def versions(self, g: str, a: str) -> list[str]:
        return list(self.DATA.get(f"{g}:{a}", []))

    def evidence(self, g: str, a: str, v: str) -> str:
        return "repository:nexus.invalid" if v in self.DATA.get(f"{g}:{a}", []) else ""


def scenario(tmp: Path, planner: Any, models: Any, cfg: Any) -> Any:
    """One round over a root pom (direct literal, property), a module inheriting the root and one inheriting an
    external parent: a same-major direct change, a major direct change, a property change, transitive overrides in
    the root and in the module pom, a major-crossing override, a parent-managed override and a residual."""
    (tmp / "pom.xml").write_text(ROOT, encoding="utf-8")
    for name, parent in (("core", "root"), ("other", "external-parent")):
        (tmp / name).mkdir(exist_ok=True)
        (tmp / name / "pom.xml").write_text(_module(name, parent), encoding="utf-8")
    item, loc = models.InventoryItem, models.PomLocation
    items = [
        item(
            ga="example:lib",
            version="1.2.3",
            origin="direct_literal",
            finding_ids=["CVE-L1", "CVE-L2"],
            patched_versions=["2.0.0"],
            modules=["core", "other"],
            location=loc("pom.xml", L_LIB, "1.2.3"),
        ),
        item(
            ga="example:core-lib",
            version="2.14.0",
            origin="direct_property",
            finding_ids=["CVE-C"],
            patched_versions=["2.14.3"],
            modules=["core", "other"],
            location=loc("pom.xml", L_PROP, "2.14.0", property="core.version", element="property"),
        ),
        item(
            ga="example:t",
            version="1.0",
            origin="transitive",
            finding_ids=["CVE-T"],
            patched_versions=["1.1"],
            modules=["core", "other"],
            via=["example:lib:1.2.3"],
        ),
        item(
            ga="example:u",
            version="1.0",
            origin="transitive",
            finding_ids=["CVE-U"],
            patched_versions=["2.0"],
            modules=["core"],
            via=["example:lib:1.2.3"],
        ),
        item(
            ga="example:m",
            version="1.0",
            origin="parent_managed",
            finding_ids=["CVE-M"],
            patched_versions=["1.1"],
            modules=["core"],
            manager="example:root:1.0.0",
        ),
        item(
            ga="example:n",
            version="1.0",
            origin="transitive",
            finding_ids=["CVE-N"],
            patched_versions=["1.5"],
            modules=["core"],
            via=["example:lib:1.2.3"],
        ),
    ]
    return planner.plan_round(tmp, models.Inventory(items=items), cfg, Source(), round=1, source_tree_hash="t" * 64)


def digest(plan: Any) -> dict[str, Any]:
    """What a run applies and leaves: proposals with their edits and hashes, residuals."""
    return {
        "proposals": [
            (
                p.id,
                p.ga,
                p.from_version,
                p.to_version,
                p.kind,
                sorted(p.risks),
                p.decision,
                [(e.op, e.pom, e.line, e.old, e.new) for e in p.edits],
                p.proposal_hash,
            )
            for p in plan.proposals
        ],
        "to_apply": [p.id for p in plan.to_apply()],
        "residuals": [(r.ga, r.version, r.reason) for r in plan.residuals],
    }


# produced by the planner before the removal (require_approval: false, always_approve: [])
PINNED: dict[str, Any] = {
    "proposals": [
        (
            "p-1-1",
            "example:t",
            "1.0",
            "1.1",
            "managed_override",
            ["managed_override"],
            "auto",
            [
                (
                    "insert",
                    "other/pom.xml",
                    8,
                    "",
                    "  <dependencyManagement>\n"
                    "    <dependencies>\n"
                    "      <dependency>\n"
                    "        <groupId>example</groupId>\n"
                    "        <artifactId>t</artifactId>\n"
                    "        <version>1.1</version>\n"
                    "      </dependency>\n"
                    "    </dependencies>\n"
                    "  </dependencyManagement>\n",
                )
            ],
            "eb178c839b5c1fb483381c48da764900cbd87f8f298d60abf664f8bf2a5b9d49",
        ),
        (
            "p-1-2",
            "example:m",
            "1.0",
            "1.1",
            "managed_override",
            ["managed_override"],
            "auto",
            [
                (
                    "insert",
                    "pom.xml",
                    14,
                    "",
                    "  <dependencyManagement>\n"
                    "    <dependencies>\n"
                    "      <dependency>\n"
                    "        <groupId>example</groupId>\n"
                    "        <artifactId>m</artifactId>\n"
                    "        <version>1.1</version>\n"
                    "      </dependency>\n"
                    "    </dependencies>\n"
                    "  </dependencyManagement>\n",
                )
            ],
            "69d4f355507e4d9b7a80f9b3397a7bb15c88ebb56142b9c4b9a75f4c61c6c0ac",
        ),
        (
            "p-1-3",
            "example:t",
            "1.0",
            "1.1",
            "managed_override",
            ["managed_override"],
            "auto",
            [
                (
                    "insert",
                    "pom.xml",
                    14,
                    "",
                    "  <dependencyManagement>\n"
                    "    <dependencies>\n"
                    "      <dependency>\n"
                    "        <groupId>example</groupId>\n"
                    "        <artifactId>t</artifactId>\n"
                    "        <version>1.1</version>\n"
                    "      </dependency>\n"
                    "    </dependencies>\n"
                    "  </dependencyManagement>\n",
                )
            ],
            "ce6e73d4420f4386e39ccb9e0eaf663974a58ad0c459def1d6bdb952a8cd9ab7",
        ),
        (
            "p-1-4",
            "example:u",
            "1.0",
            "2.0",
            "managed_override",
            ["major", "managed_override"],
            "auto",
            [
                (
                    "insert",
                    "pom.xml",
                    14,
                    "",
                    "  <dependencyManagement>\n"
                    "    <dependencies>\n"
                    "      <dependency>\n"
                    "        <groupId>example</groupId>\n"
                    "        <artifactId>u</artifactId>\n"
                    "        <version>2.0</version>\n"
                    "      </dependency>\n"
                    "    </dependencies>\n"
                    "  </dependencyManagement>\n",
                )
            ],
            "87c24760b919ef5a32314cbadc0e1cc04bbe8b640fa05ce90080bfb7b2f7cd4c",
        ),
        (
            "p-1-5",
            "example:core-lib",
            "2.14.0",
            "2.14.3",
            "property_version",
            [],
            "auto",
            [("replace", "pom.xml", 12, "2.14.0", "2.14.3")],
            "bdbba39403360b864b03a470b204892fab52e72bd59ed575c3cbff077e24b767",
        ),
        (
            "p-1-6",
            "example:lib",
            "1.2.3",
            "2.0.0",
            "direct_version",
            ["major"],
            "auto",
            [("replace", "pom.xml", 18, "1.2.3", "2.0.0")],
            "53640a9c111106f8acf6273ea0eec6eac59c08de47f00b37af0aea655ec4a1a1",
        ),
    ],
    "residuals": [("example:n", "1.0", "no_candidate_version")],
    "to_apply": ["p-1-1", "p-1-2", "p-1-3", "p-1-4", "p-1-5", "p-1-6"],
}


def test_default_plan_is_the_plan_made_before_the_approval_step_was_removed(tmp_path):
    plan = scenario(tmp_path, P, M, types.SimpleNamespace(remediation=RemediationConfig()))
    assert digest(plan) == PINNED
    assert all(p.decision == M.D_AUTO for p in plan.proposals)
    assert plan.to_apply() == plan.proposals and not plan.notes
    assert all("requires_approval" not in p for p in plan.to_dict()["proposals"])
