"""The fix-script process. Run as a FILE, never imported by the parent:

    <python> -I -X utf8 .../sbmig/script/harness.py <payload.json>

`-I` (isolated) ignores PYTHON* variables, the user site and the current directory, so nothing in the temp cwd or
the environment is imported by accident; the sbmig source root the parent passes in the payload is put on sys.path
explicitly (after the whitelisted stdlib modules are loaded, so the root can never shadow them). The LLM code then
runs with a small builtins dict and a guarded `__import__` (the same rule as the static check — sbmig itself is not
importable from the script) against the helper `p` (sbmig.script.api.FixHelper).

Payload (JSON, UTF-8): root, code_path, result_path, journal_path, errors, encoding_candidates, log_lines,
extra_imports, preload, sbmig_root. Result (JSON, UTF-8, written atomically): status ok | error, error, changed_files,
log, log_dropped. The journal (one JSON line per log line / changed file, flushed at once) is what a killed process
still leaves behind. A guard against mistakes, not a sandbox (see sbmig.script.check).
"""
from __future__ import annotations

import builtins
import json
import os
import sys
import traceback
import types

SCRIPT_FILENAME = "fix_script.py"         # the code's filename in tracebacks (only its frames are reported)
MAX_TRACE_FRAMES = 8
EXIT_HARNESS_FAILED = 3

SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "abs", "all", "any", "bool", "bytes", "chr", "dict", "divmod", "enumerate", "filter", "float", "frozenset",
    "hash", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next", "ord", "range",
    "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    "object", "staticmethod", "classmethod", "property", "__build_class__",          # small helper classes
    "True", "False", "None",
    "Exception", "ArithmeticError", "AssertionError", "AttributeError", "FileExistsError", "FileNotFoundError",
    "ImportError", "IndexError", "IsADirectoryError", "KeyError", "LookupError", "NameError", "NotImplementedError",
    "OSError", "RuntimeError", "StopIteration", "TypeError", "UnicodeDecodeError", "UnicodeEncodeError",
    "UnicodeError", "ValueError", "ZeroDivisionError",
)


def safe_builtins(print_fn, import_fn) -> dict:
    out = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES if hasattr(builtins, name)}
    out["print"] = print_fn
    out["__import__"] = import_fn
    return out


def guarded_import(allowed, extra_imports):
    """`__import__` for the script: only what `allowed(name, fromlist, extra)` accepts, no relative imports."""
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            raise ImportError("relative imports are not allowed in a fix script")
        if not allowed(name, tuple(fromlist or ()), extra_imports):
            raise ImportError(f"import of {name!r} is not allowed in a fix script")
        return real_import(name, globals, locals, fromlist, level)

    return _import


def describe(exc: BaseException, code: str) -> str:
    """'KeyError: 'x' (line 7)' + the script's own frames (helper / harness frames are left out)."""
    lines = code.splitlines()
    msg = str(exc)
    head = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == SCRIPT_FILENAME]
    if isinstance(exc, SyntaxError) and exc.filename == SCRIPT_FILENAME and exc.lineno:
        return f"{head} (line {exc.lineno})"
    if not frames:
        return head
    out = [f"{head} (line {frames[-1].lineno})", "script traceback:"]
    for f in frames[-MAX_TRACE_FRAMES:]:
        src = lines[f.lineno - 1].strip() if f.lineno and 0 < f.lineno <= len(lines) else ""
        out.append(f"  line {f.lineno}, in {f.name}: {src}")
    return "\n".join(out)


def write_result(path: str, result: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    os.replace(tmp, path)


def run(payload: dict) -> dict:
    for name in payload.get("preload") or ():            # whitelisted stdlib first: the root cannot shadow them
        try:
            __import__(name)
        except ImportError:
            pass
    root = payload.get("sbmig_root") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from sbmig.script.api import FixHelper
    from sbmig.script.check import import_allowed

    helper = FixHelper(payload["root"], errors=payload.get("errors") or (),
                       encoding_candidates=payload.get("encoding_candidates") or None,
                       log_lines=int(payload.get("log_lines") or 200), journal_path=payload.get("journal_path"))
    with open(payload["code_path"], encoding="utf-8") as fh:
        code = fh.read()
    result: dict = {"status": "ok", "error": ""}
    try:
        compiled = compile(code, SCRIPT_FILENAME, "exec", dont_inherit=True)
        importer = guarded_import(import_allowed, tuple(payload.get("extra_imports") or ()))
        scope = {"__builtins__": safe_builtins(helper._print, importer), "__name__": "__sbmig_fix__"}
        exec(compiled, scope)                            # noqa: S102  # checked by sbmig.script.check first
        fix = scope.get("fix")
        if not callable(fix):
            raise TypeError("the script defines no callable 'fix'")
        out = fix(helper)
        if isinstance(out, types.GeneratorType):         # `yield` inside fix: run it to the end
            for _ in out:
                pass
    except BaseException as exc:                         # noqa: BLE001  # every failure is the script's result
        result.update(status="error", error=describe(exc, code))
    if result["status"] == "ok" and helper._refusals:
        result.update(status="error", error=f"PathRefused: {helper._refusals[0]} (caught by the script)")
    result.update(changed_files=list(helper._changed), log=list(helper._log), log_dropped=helper._dropped)
    helper._close()
    return result


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):             # pragma: no cover - replaced / detached streams
            pass
    if len(argv) != 2:
        print("usage: python -I harness.py <payload.json>", file=sys.stderr)
        return EXIT_HARNESS_FAILED
    try:
        with open(argv[1], encoding="utf-8") as fh:
            payload = json.load(fh)
        write_result(payload["result_path"], run(payload))
    except BaseException:                                # noqa: BLE001  # the parent reports the stderr tail
        traceback.print_exc(file=sys.stderr)
        return EXIT_HARNESS_FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
