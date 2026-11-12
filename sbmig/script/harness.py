"""별도 프로세스에서 수정 스크립트를 실행하는 모듈. 부모 프로세스가 import하지 않고 파일로 실행한다.
실행 명령은 ``<python> -I -X utf8 harness.py <payload.json>``이다.
- ``-I``는 PYTHON* 환경 변수, 사용자 site, 현재 디렉토리를 무시하므로 의도하지 않은 import를 막는다
- sbmig 소스 루트는 payload로 받아 sys.path에 넣는다.
  허용된 표준 모듈을 먼저 로드해 두므로 루트에 같은 이름의 파일이 있어도 가려지지 않는다
- LLM 코드는 최소한의 builtins와 검사를 거치는 ``__import__``로 실행하며, sbmig 자체는 import할 수 없다
- 결과 JSON은 원자적으로 쓰고, 로그와 바뀐 파일은 저널에 줄마다 flush하므로 시간 초과로 종료돼도 남는다

The fix-script process. Run as a FILE, never imported by the parent:

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
    """스크립트에 넘길 builtins를 만든다.
    안전한 이름, 로그로 보내는 ``print``, 검사를 거치는 ``__import__``만 넣는다."""
    out = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES if hasattr(builtins, name)}
    out["print"] = print_fn
    out["__import__"] = import_fn
    return out


def guarded_import(allowed, extra_imports):
    """스크립트에서 쓸 ``__import__``를 만든다.
    ``allowed(name, fromlist, extra)``가 허용한 모듈만 import하고 상대 import는 막는다.

    `__import__` for the script: only what `allowed(name, fromlist, extra)` accepts, no relative imports."""
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            raise ImportError("relative imports are not allowed in a fix script")
        if not allowed(name, tuple(fromlist or ()), extra_imports):
            raise ImportError(f"import of {name!r} is not allowed in a fix script")
        return real_import(name, globals, locals, fromlist, level)

    return _import


def describe(exc: BaseException, code: str) -> str:
    """예외를 한 줄 요약과 스크립트 자신의 프레임으로 설명한다. 도우미와 하네스의 프레임은 뺀다.

    'KeyError: 'x' (line 7)' + the script's own frames (helper / harness frames are left out)."""
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
    """결과 JSON을 임시 파일에 쓴 뒤 ``os.replace``로 바꾼다.
    부모 프로세스가 쓰다 만 결과를 읽지 않게 하기 위해서다."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    os.replace(tmp, path)


def run(payload: dict) -> dict:
    """payload 하나를 실행해 결과 dict를 반환한다. 스크립트에서 난 예외는 모두 status error로 반환한다."""
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
    # 거부된 경로는 다음 시도가 알아야 할 스크립트 버그이므로 스크립트가 PathRefused를 잡았더라도 실패로 처리한다
    if result["status"] == "ok" and helper._refusals:
        result.update(status="error", error=f"PathRefused: {helper._refusals[0]} (caught by the script)")
    result.update(changed_files=list(helper._changed), log=list(helper._log), log_dropped=helper._dropped)
    helper._close()
    return result


def main(argv: list[str]) -> int:
    """하네스의 진입점으로, payload를 읽어 ``run``을 실행하고 결과를 파일에 쓴다.
    하네스 자체가 실패하면 traceback을 stderr로 출력하고 종료 코드 3을 반환한다."""
    # Windows 콘솔 코드페이지와 상관없이 한글 로그와 경로가 깨지지 않도록 UTF-8로 맞춘다
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
