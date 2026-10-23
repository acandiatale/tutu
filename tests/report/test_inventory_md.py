"""inventory.md (en | ko) + inventory.json from `write_inventory`."""
from __future__ import annotations

import json
from pathlib import Path

from sbmig.analyze import scan_webapp_tree
from sbmig.core.models import WebappInventory
from sbmig.report.inventory import render_inventory, write_inventory

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "webapps" / "legacy-exploded"


def test_english_markdown_and_json(tmp_path):
    inv = scan_webapp_tree(FIXTURE)
    paths = write_inventory(inv, tmp_path / "out")
    assert set(paths) == {"inventory_md", "inventory_json"} and inv.outputs == paths
    md = Path(paths["inventory_md"]).read_text(encoding="utf-8")
    for s in ("# Webapp inventory — legacy-exploded", "| Webapp roots | 2 |", "| Jars known / unknown / ignored | 5 / 1 / 2 |",
              "| Class files (Java level) | 1 (1.6) |", "## Maven module proposal", "## What to do next",
              "`knowledge/libraries/jar_names.yml`", "| `portal` | war | `portal` | `portal/WEB-INF/src` | 1.6 | 3 / 1 |",
              "| Frameworks | spring-mvc |", "`jdbc/PortalDS`", "`com.acme.common.Util`"):
        assert s in md, s
    # unknown jars first, backups last
    rows = [line for line in md.splitlines() if line.startswith("| `") and ".jar" in line]
    assert "acme-util.jar" in rows[0] and "unknown coordinates" in rows[0] and "commons-io-2.4.jar.old" in rows[-1]
    data = json.loads(Path(paths["inventory_json"]).read_text(encoding="utf-8"))
    back = WebappInventory.model_validate(data)
    assert back.proposals[1].name == "portal" and back.outputs == paths


def test_korean_markdown_keeps_identifiers(tmp_path, cfg):
    inv = scan_webapp_tree(FIXTURE)
    ko_cfg = cfg.model_copy(update={"report": cfg.report.model_copy(update={"language": "ko"})})
    paths = write_inventory(inv, tmp_path, ko_cfg)
    ko = Path(paths["inventory_md"]).read_text(encoding="utf-8")
    for s in ("# 웹앱 인벤토리 — legacy-exploded", "## Maven 모듈 제안", "## 다음 할 일", "| 웹앱 루트 | 2 |",
              "좌표 미확인", "`portal/WEB-INF/lib/acme-util.jar`", "`commons-lang:commons-lang`", "spring-mvc"):
        assert s in ko, s
    for s in ("## Maven module proposal", "## What to do next", "| Webapp roots |", "unknown coordinates"):
        assert s not in ko, s
    # the language argument wins over the config; json is language independent
    assert "# Webapp inventory" in render_inventory(inv, "en")
    assert write_inventory(inv, tmp_path / "en", ko_cfg, language="en")
    assert "# Webapp inventory" in (tmp_path / "en" / "inventory.md").read_text(encoding="utf-8")


def test_empty_inventory_renders(tmp_path):
    md = render_inventory(WebappInventory(root=str(tmp_path)), "ko")
    assert "`WEB-INF`가 있는 디렉터리를 찾지 못했다." in md and "제안할 것이 없다" in md
