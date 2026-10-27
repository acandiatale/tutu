"""Test helpers for triage: a BuildError factory."""
from __future__ import annotations

from typing import Optional

from sbmig.core.models import GAV, BuildError, ErrorKind


def err(kind: ErrorKind = ErrorKind.COMPILE, message: str = "cannot find symbol", file: Optional[str] = None,
        sig: Optional[str] = None, module: Optional[str] = None, artifact: Optional[str] = None,
        symbol: Optional[str] = None, line: Optional[int] = None, raw: str = "") -> BuildError:
    return BuildError(kind=kind, message=message, file=file, module=module, symbol=symbol, line=line, raw=raw,
                      artifact=GAV.parse(artifact) if artifact else None,
                      signature=sig or f"{kind.value}|{message}|{symbol or ''}",
                      signature_generic=f"{kind.value}|{message.split()[0] if message else ''}")
