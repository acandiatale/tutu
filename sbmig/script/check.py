"""Static check of an LLM fix script before it runs (docs/v2/PLAN-v2.md §3.2, docs/INTERFACES.md "Script contract").

The script must define a module-level `def fix(p)`; it may import only a small whitelist of pure-text stdlib modules
and must not name the builtins that reach files, processes or other code (`open`, `exec`, `eval`, …). Any attribute
whose name starts with `_` is refused: that covers dunders (`__class__`, `__globals__`, …) and the helper's private
state (`p._root`). The same import rule guards `__import__` at run time (`sbmig.script.harness`).

This is a guard against a script that reaches outside the project by mistake, not a sandbox against a hostile author:
Python-level checks can be worked around; the account running sbmig is the real limit.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable

ALLOWED_IMPORTS: tuple[str, ...] = ("re", "json", "textwrap", "fnmatch", "posixpath", "itertools", "collections",
                                    "functools", "string", "math")
# refused even when a company config lists them in script.extra_allowed_imports
REFUSED_MODULES: tuple[str, ...] = ("os", "sys", "subprocess", "socket", "pathlib", "importlib", "shutil", "ctypes",
                                    "threading", "multiprocessing", "builtins", "io")
REFUSED_NAMES: tuple[str, ...] = ("open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
                                  "getattr", "setattr", "delattr", "input", "breakpoint", "help", "memoryview")
# attribute names refused besides `_…`: an allowed module's own imports (`posixpath.os`, `json.codecs`) and `.open`
REFUSED_ATTRIBUTES: frozenset[str] = frozenset(REFUSED_MODULES) | {"codecs", "open"}
DEFAULT_MAX_CODE_CHARS = 30000
MAX_PROBLEMS = 20                       # findings listed per script; the rest is counted

_HINTS = {"open": " (use p.read / p.write / p.create)", "exec": "", "eval": "", "getattr": " (use plain attributes)"}


def module_allowed(name: str, extra_imports: Iterable[str] = ()) -> bool:
    """May a fix script import `name` (dotted)? Built-in whitelist: by top-level module (`collections.abc` is fine).
    `script.extra_allowed_imports` entries allow themselves and their submodules (`xml.etree.ElementTree` does not
    open all of `xml`). A refused module stays refused whatever the config says."""
    if not name or name.startswith("."):
        return False
    top = name.split(".", 1)[0]
    if top in REFUSED_MODULES:
        return False
    if top in ALLOWED_IMPORTS:
        return True
    return any(name == e or name.startswith(e + ".") for e in (x.strip() for x in extra_imports) if e)


def import_allowed(name: str, fromlist: Iterable[str] = (), extra_imports: Iterable[str] = ()) -> bool:
    """`import name` / `from name import a, b`: the module itself is allowed, or every `name.a` is (an extra entry
    `xml.etree.ElementTree` allows `from xml.etree import ElementTree`)."""
    if module_allowed(name, extra_imports):
        return True
    names = [f for f in (fromlist or ()) if f and f != "*"]
    return bool(names) and all(module_allowed(f"{name}.{f}", extra_imports) for f in names)


def _allowed_list(extra_imports: Iterable[str]) -> str:
    extra = [e for e in extra_imports if e and e not in ALLOWED_IMPORTS]
    return ", ".join([*ALLOWED_IMPORTS, *extra])


def _fix_signature_problem(fn: ast.FunctionDef) -> str | None:
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    if a.vararg is not None:
        return f"line {fn.lineno}: 'fix' must not take *{a.vararg.arg}; define it as 'def fix(p)'"
    if len(positional) != 1:
        return (f"line {fn.lineno}: 'fix' must take exactly one positional parameter (the helper p), "
                f"found {len(positional)}")
    missing = [k.arg for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    if missing:
        return f"line {fn.lineno}: keyword-only parameter '{missing[0]}' of 'fix' needs a default"
    return None


def check_script(code: str, *, extra_imports: Iterable[str] = (), max_chars: int = DEFAULT_MAX_CODE_CHARS
                 ) -> list[str]:
    """Problems that keep `code` from running, one line each ("line 3: import of 'os' is not allowed").
    Empty list = allowed. Never raises."""
    if not isinstance(code, str):
        return [f"the script is not text ({type(code).__name__})"]
    if not code.strip():
        return ["the script is empty; define 'def fix(p)'"]
    if max_chars and len(code) > max_chars:
        return [f"the script is {len(code)} characters long (limit {max_chars}); keep it shorter"]
    try:
        tree = ast.parse(code, filename="fix_script.py")
        compile(tree, "fix_script.py", "exec", dont_inherit=True)    # compiler-stage errors ('return' outside…)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: syntax error: {exc.msg}" if exc.lineno else f"syntax error: {exc.msg}"]
    except (ValueError, RecursionError, MemoryError) as exc:     # NUL bytes / absurd nesting
        return [f"the script cannot be parsed: {type(exc).__name__}: {exc}"]
    extra = tuple(extra_imports or ())
    problems: list[str] = []

    def add(msg: str) -> None:
        if msg not in problems:
            problems.append(msg)

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not module_allowed(alias.name, extra):
                    add(_import_problem(line, alias.name, extra))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                add(f"line {line}: relative import 'from {'.' * node.level}{mod} import …' is not allowed")
                continue
            names = [a.name for a in node.names]
            if not import_allowed(mod, names, extra):
                add(_import_problem(line, mod, extra))
            for n in names:
                if n.startswith("_"):
                    add(f"line {line}: import of private name '{n}' from '{mod}' is not allowed")
        elif isinstance(node, ast.Name):
            if node.id in REFUSED_NAMES:
                add(f"line {line}: '{node.id}' is not allowed{_HINTS.get(node.id, '')}")
            elif node.id.startswith("__") and node.id != "__name__":
                add(f"line {line}: name '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                add(f"line {line}: attribute '{node.attr}' is not allowed (names starting with '_' are refused)")
            elif node.attr in REFUSED_ATTRIBUTES:
                add(f"line {line}: attribute '.{node.attr}' is not allowed")
        elif isinstance(node, ast.alias) and node.asname and node.asname in REFUSED_NAMES:
            add(f"line {line}: import alias '{node.asname}' is not allowed")
    fixes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "fix"]
    if not fixes:
        add("no module-level 'def fix(p)' found")
    else:
        fn = fixes[-1]                                   # the last definition is the one that runs
        if isinstance(fn, ast.AsyncFunctionDef):
            add(f"line {fn.lineno}: 'fix' must be a plain function, not 'async def'")
        else:
            sig = _fix_signature_problem(fn)
            if sig:
                add(sig)
    if len(problems) > MAX_PROBLEMS:
        more = len(problems) - MAX_PROBLEMS
        problems = problems[:MAX_PROBLEMS] + [f"… {more} more problem(s)"]
    return problems


def _import_problem(line: int, name: str, extra: Iterable[str]) -> str:
    top = name.split(".", 1)[0]
    if top in REFUSED_MODULES:
        return f"line {line}: import of '{name}' is not allowed"
    return f"line {line}: import of '{name}' is not allowed (allowed: {_allowed_list(extra)})"
