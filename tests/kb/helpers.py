"""Plain helpers (builders) for KB tests."""
from __future__ import annotations

from typing import Optional

from sbmig.core.models import BuildError, ErrorCluster, ErrorEntry, ErrorKind, FixTry
from sbmig.errors import case_id_for

L2 = "compile|cannot_find_symbol|WebSecurityConfigurerAdapter"
CASE = case_id_for(L2)
FILE = "src/main/java/com/example/config/SecurityConfig.java"


def make_error(l2: str = L2, file: Optional[str] = FILE, line: int = 5, message: str = "cannot find symbol"
               ) -> BuildError:
    return BuildError(kind=ErrorKind.COMPILE, message=message, file=file, line=line,
                      symbol="class WebSecurityConfigurerAdapter", signature=f"{l2}|L1", signature_generic=l2)


def make_cluster(*errors: BuildError) -> ErrorCluster:
    errs = list(errors) or [make_error()]
    return ErrorCluster(id=f"case:{case_id_for(errs[0].signature_generic)}", kind=errs[0].kind, errors=errs,
                        case_id=case_id_for(errs[0].signature_generic),
                        signatures=sorted({e.signature for e in errs}))


def entry(eid: str = "e1", *, run_id: str = "r1", project: str = "demo", status: str = "resolved",
          l2: str = L2, tries=None, **kw) -> ErrorEntry:
    data = dict(id=eid, run_id=run_id, project=project, case_id=case_id_for(l2), l2=l2, kind=ErrorKind.COMPILE,
                file=FILE, line=5, message="cannot find symbol", seen="compile", status=status,
                resolved="r1 try 1" if status == "resolved" else "",
                tries=tries if tries is not None else [FixTry(n=1, round=1, source="llm-new", analysis="Boot 3 removed it",
                                                              solution="use a SecurityFilterChain bean",
                                                              applied="edits", diff="-a\n+b", result="improved")])
    data.update(kw)
    return ErrorEntry(**data)


def with_kb(cfg, **kw):
    """Copy of cfg with kb.* fields overridden."""
    return cfg.model_copy(update={"kb": cfg.kb.model_copy(update=kw)})
