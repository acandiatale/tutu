from __future__ import annotations

from pathlib import Path

import pytest

from sbmig.core.models import BuildError, ErrorCluster, ErrorKind
from sbmig.core.workspace import Workspace


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    return Workspace(root, tmp_path / "run", "t-run")


def write(ws: Workspace, rel: str, text: str) -> None:
    ws.write(rel, text)


@pytest.fixture()
def put(ws):
    def _put(rel: str, text: str, raw: bool = False) -> str:
        p = ws.abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
        return rel
    return _put


def make_cluster(file: str = "src/main/java/a/A.java", lines=(3,), kind: ErrorKind = ErrorKind.COMPILE,
                 message: str = "cannot find symbol", symbol: str | None = "class Foo", cid: str = "c1") -> ErrorCluster:
    errs = [BuildError(kind=kind, message=message, file=file, line=l, column=5, symbol=symbol,
                       location="class a.A", signature=f"sig{l}") for l in lines]
    return ErrorCluster(id=cid, kind=kind, file=file, errors=errs, signatures=[e.signature for e in errs])
