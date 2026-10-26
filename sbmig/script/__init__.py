"""sbmig.script — v2 fix scripts: the LLM answers a build error with a Python script `def fix(p)`; sbmig checks it
statically (`check_script`) and runs it in a separate `python -I` process (`ScriptRunner`) against the helper API `p`
(`sbmig.script.api`), which only touches files inside the project folder. A guard against mistakes, not a sandbox
against a hostile author (docs/v2/PLAN-v2.md §3.2, docs/INTERFACES.md "Script contract (S)")."""
from .api import HELPER_API_DOC, helper_api_doc
from .check import ALLOWED_IMPORTS, REFUSED_MODULES, REFUSED_NAMES, check_script
from .runner import ScriptRunner

__all__ = [
    "ALLOWED_IMPORTS",
    "HELPER_API_DOC",
    "REFUSED_MODULES",
    "REFUSED_NAMES",
    "ScriptRunner",
    "check_script",
    "helper_api_doc",
]
