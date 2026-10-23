"""`sbmig inventory <dir|zip> [--out] [--json]` — read only, exit 0 (3 for a missing path)."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from sbmig.cli import app

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "webapps" / "legacy-exploded"
runner = CliRunner()


def _files(root: Path) -> dict[str, float]:
    return {p.relative_to(root).as_posix(): p.stat().st_mtime for p in root.rglob("*") if p.is_file()}


def test_inventory_directory(tmp_path):
    before = _files(FIXTURE)
    r = runner.invoke(app, ["inventory", str(FIXTURE), "--out", str(tmp_path / "inv")])
    assert r.exit_code == 0, r.output
    lines = r.output.strip().splitlines()
    assert len(lines) <= 12
    assert lines[0] == f"inventory: {FIXTURE}"
    assert "admin (2 jsp, servlet 2.5, spring-mvc), portal (3 jsp, servlet 2.3, plain-servlet)" in r.output
    assert "portal/WEB-INF/src (3 java, cp949)" in r.output and "java from classes: 1.6" in r.output
    assert "jars: 5 known / 1 unknown / 2 ignored (unknown: acme-util.jar)" in r.output
    assert "classes without source: 1   duplicate classes: 1" in r.output
    assert "proposals: admin (war, java 1.8), portal (war, java 1.6)" in r.output
    assert (tmp_path / "inv" / "inventory.md").is_file() and (tmp_path / "inv" / "inventory.json").is_file()
    assert _files(FIXTURE) == before                                   # read only


def test_inventory_zip_and_json(tmp_path):
    z = tmp_path / "legacy.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in FIXTURE.rglob("*"):
            if p.is_file():
                zf.write(p, "legacy-exploded/" + p.relative_to(FIXTURE).as_posix())
    r = runner.invoke(app, ["inventory", str(z), "--out", str(tmp_path / "o"), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["root"] == f"{z.resolve()}!/legacy-exploded"
    assert [p["name"] for p in data["proposals"]] == ["admin", "portal"]
    assert data["outputs"]["inventory_json"].endswith("inventory.json")
    assert json.loads((tmp_path / "o" / "inventory.json").read_text(encoding="utf-8"))["root"] == data["root"]


def test_inventory_flat_zip_uses_zip_name(tmp_path):
    z = tmp_path / "shop.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("WEB-INF/web.xml", "<web-app version=\"2.4\"/>")
        zf.writestr("a.jsp", "x")
        zf.writestr("other/b.jsp", "y")
    r = runner.invoke(app, ["inventory", str(z), "--out", str(tmp_path / "o"), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["root"] == str(z.resolve()) and data["proposals"][0]["name"] == "shop"


def test_inventory_missing_path_exits_3(tmp_path):
    r = runner.invoke(app, ["inventory", str(tmp_path / "nope")])
    assert r.exit_code == 3
