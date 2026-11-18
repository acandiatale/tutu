"""W3 (review 2026-11-18): the migration intake refuses a ZIP whose unpacked size, largest entry or compression ratio is over
`intake.max_unpacked_mb` / `max_entry_mb` / `max_compression_ratio` (0 = no limit) BEFORE writing anything, judged from the
ZIP's directory, and stops while unpacking when the bytes really written pass a limit (the directory can lie). Same meaning
of each number as the remediation intake: MiB of the whole tree, MiB of one entry, unpacked / packed size of one entry
(checked from 1 MiB unpacked on). The run ends like the other intake refusals: exit 3, a reason sentence."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sbmig.core.workspace import Workspace, ZipLimits

MIB = 1024 * 1024


def _zip(path: Path, files: dict[str, bytes], method=zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression=method) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _written(run_dir: Path) -> list[Path]:
    return [p for p in run_dir.rglob("*") if p.is_file() and p.name != "journal.jsonl"]


def test_a_small_zip_within_the_limits_is_unpacked(tmp_path):
    src = _zip(tmp_path / "p.zip", {"p/pom.xml": b"<project/>", "p/src/A.java": b"class A {}"})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(1, 1, 200))
    assert ws.intake_refusal is None and (ws.root / "pom.xml").is_file()


def test_over_the_total_is_refused_before_anything_is_written(tmp_path):
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", "a.bin": b"\0" * (2 * MIB), "b.bin": b"\1" * (2 * MIB)})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(3, 0, 0))
    assert ws.intake_refusal == ("ZIP refused: it unpacks to 4 MiB, over the limit of 3 MiB (intake.max_unpacked_mb) "
                                 "— nothing was unpacked")
    assert _written(ws.run_dir) == [] and list(ws.root.iterdir()) == []
    events = (ws.run_dir / "journal.jsonl").read_text()
    assert '"event": "intake_refused"' in events and "max_unpacked_mb" in events


def test_one_entry_over_the_limit_is_refused(tmp_path):
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", "lib/big.jar": b"\0" * (3 * MIB)})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(0, 2, 0))
    assert ws.intake_refusal == ("ZIP refused: the entry lib/big.jar unpacks to 3 MiB, over the limit of 2 MiB "
                                 "(intake.max_entry_mb) — nothing was unpacked")
    assert _written(ws.run_dir) == []


def test_a_compression_ratio_over_the_limit_is_refused(tmp_path):
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", "zeros.txt": b"\0" * (8 * MIB)})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(0, 0, 100))
    assert ws.intake_refusal.startswith("ZIP refused: the entry zeros.txt is compressed ")
    assert ws.intake_refusal.endswith(":1, over the limit of 100:1 (intake.max_compression_ratio) — nothing was unpacked")
    assert _written(ws.run_dir) == []
    # small entries are not judged by their ratio (same rule as the remediation intake: from 1 MiB unpacked on)
    ok = _zip(tmp_path / "q.zip", {"pom.xml": b"<project/>" + b" " * 50000})
    assert Workspace.from_input(ok, tmp_path / "out", run_id="r2", limits=ZipLimits.from_mb(0, 0, 2)).intake_refusal is None


def test_zero_means_no_limit(tmp_path):
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", "zeros.txt": b"\0" * (3 * MIB)})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(0, 0, 0))
    assert ws.intake_refusal is None and (ws.root / "zeros.txt").stat().st_size == 3 * MIB


def test_limits_come_from_the_intake_config():
    from sbmig.core.config import IntakeConfig
    lim = ZipLimits.from_config(IntakeConfig(max_unpacked_mb=10, max_entry_mb=2, max_compression_ratio=50))
    assert (lim.max_unpacked_bytes, lim.max_entry_bytes, lim.max_compression_ratio) == (10 * MIB, 2 * MIB, 50)
    default = ZipLimits.from_config(IntakeConfig())
    assert (default.max_unpacked_bytes, default.max_entry_bytes, default.max_compression_ratio) == (4096 * MIB, 1024 * MIB, 200)


def _lying_open(monkeypatch, sizes: dict[str, int]):
    """Entries whose data stream gives more bytes than the ZIP's directory says (Python's zipfile itself stops at the
    declared size; another reader, or a future one, may not — the count of bytes really written is the wall)."""
    import io

    import sbmig.core.workspace as wsmod
    real_open = zipfile.ZipFile.open

    def lying_open(self, member, *a, **kw):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if name in sizes:
            return io.BytesIO(b"\7" * sizes[name])
        return real_open(self, member, *a, **kw)
    monkeypatch.setattr(wsmod.zipfile.ZipFile, "open", lying_open)
    monkeypatch.setattr(wsmod, "_UNPACK_CHUNK", 64 * 1024)


def test_while_unpacking_the_written_bytes_are_counted(tmp_path, monkeypatch):
    """The directory can lie: the bytes really written pass the entry limit → stop, remove what was written."""
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", "big.bin": b"\7" * 10}, zipfile.ZIP_STORED)
    _lying_open(monkeypatch, {"big.bin": 3 * MIB})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(0, 2, 0))
    assert ws.intake_refusal == ("ZIP refused while unpacking: the entry big.bin wrote more than 2 MiB "
                                 "(intake.max_entry_mb) — the ZIP's directory understates its sizes")
    assert _written(ws.run_dir) == [] and list(ws.root.iterdir()) == []


def test_while_unpacking_the_total_and_the_ratio_are_counted(tmp_path, monkeypatch):
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", "a.bin": b"\7" * 10, "b.bin": b"\5" * 10},
               zipfile.ZIP_STORED)
    _lying_open(monkeypatch, {"a.bin": 2 * MIB, "b.bin": 2 * MIB})
    ws = Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(3, 0, 0))
    assert ws.intake_refusal == ("ZIP refused while unpacking: more than 3 MiB written (intake.max_unpacked_mb) "
                                 "— the ZIP's directory understates its sizes")
    assert _written(ws.run_dir) == []
    ws2 = Workspace.from_input(src, tmp_path / "out", run_id="r2", limits=ZipLimits.from_mb(0, 0, 100))
    assert ws2.intake_refusal == ("ZIP refused while unpacking: the entry a.bin expanded more than 100:1 "
                                  "(intake.max_compression_ratio) — the ZIP's directory understates its sizes")


def test_cli_run_ends_like_an_intake_refusal(tmp_path, monkeypatch):
    """Exit 3, report.json carries `run aborted: <reason>` (the sentence the page shows), SBMIG-RESULT says aborted."""
    import json

    from typer.testing import CliRunner

    from sbmig.cli import app
    import os
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", "a.bin": os.urandom(2 * MIB)})
    monkeypatch.setenv("SBMIG_INTAKE__MAX_UNPACKED_MB", "1")
    out = tmp_path / "out"
    r = CliRunner().invoke(app, ["run", str(src), "--config", "config/artifacts.local-e2e.yml", "--out", str(out),
                                 "--run-id", "zb-1"])
    assert r.exit_code == 3, r.output
    rep = json.loads((out / "zb-1" / "report.json").read_text(encoding="utf-8"))
    reason = "ZIP refused: it unpacks to 2 MiB, over the limit of 1 MiB (intake.max_unpacked_mb) — nothing was unpacked"
    assert f"run aborted: {reason}" in rep["manual_followups"]
    assert "SBMIG-RESULT" in r.output and "aborted" in r.output
    assert not (out / "zb-1" / "project" / "a.bin").exists()


@pytest.mark.parametrize("name", ["../evil.txt"])
def test_zip_slip_is_still_blocked(tmp_path, name):
    src = _zip(tmp_path / "p.zip", {"pom.xml": b"<project/>", name: b"x"})
    with pytest.raises(ValueError, match="zip slip"):
        Workspace.from_input(src, tmp_path / "out", run_id="r1", limits=ZipLimits.from_mb(0, 0, 0))
