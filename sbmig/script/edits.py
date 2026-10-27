"""Direct edits (v2.1, docs/v2/PLAN-v2.1.md §3.6): the LLM's `<edits>` JSON list applied by sbmig — "LLM 이 직접 수정".

The gateway has no tool calling, so the LLM states its changes as data (`Edit`: replace / write / create / delete /
move / pom) and sbmig applies them through the SAME helper API and guard as a fix script: `edits_script(edits)`
synthesizes a `def fix(p)` with every text embedded as a literal (`repr`), and `apply_edits` runs it through
`ScriptRunner.run` (static check, separate `python -I` process, path guard, encoding / line-end keeping) → the same
`ScriptRunResult` (changed_files, log, error).

Failure rules (the loop rolls back and asks again):
- invalid edits (unknown op, a path outside the project, a missing field, a pom call the editor does not have) are
  refused before anything runs: status `rejected`, `problems` = one line per finding;
- a `replace` whose `old` text is not in the file fails the run: `error` = "edit 2: text not found in a/B.java" (an
  `old` that is gone while its `new` text is already there counts as applied); create over a different existing file,
  move of a missing source / onto an existing target and a missing pom fail the same way;
- the run is NOT atomic: edits before the failing one are already on disk (`changed_files` lists them). The caller
  restores the tree (git) on any status other than `ok` — exactly as for a failed fix script.

Pom edits name the editor calls of `p.pom(path)` (`POM_CALLS`, a whitelist): `{"op": "pom", "path": "pom.xml",
"add_dependency": {"group_id": "g", "artifact_id": "a", "version": null}}`; argument names may be the Maven spelling
(`groupId`), the snake spelling (`group_id`) or the editor's (`g`); a `"g:a[:v]"` string or a positional list works too;
several calls of one name go in a list of objects. A call name outside the whitelist is refused.
"""
from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Iterable, Sequence
from typing import Any, Optional

from sbmig.core.models import Edit, ScriptRunResult

log = logging.getLogger("sbmig.script.edits")

EDIT_OPS: tuple[str, ...] = ("replace", "write", "create", "delete", "move", "pom")

# p.pom(path) mutators an edit may call: name → ((editor parameter, required), …). Read-only calls (text,
# dependencies, has_dependency, get_property) and save() (implicit) are not edits.
POM_CALLS: dict[str, tuple[tuple[str, bool], ...]] = {
    "add_dependency": (("g", True), ("a", True), ("version", False), ("scope", False), ("type", False),
                       ("classifier", False)),
    "add_managed_dependency": (("g", True), ("a", True), ("version", False), ("scope", False), ("type", False),
                               ("classifier", False)),
    "remove_dependency": (("g", True), ("a", True)),
    "remove_managed_dependency": (("g", True), ("a", True)),
    "set_dependency_version": (("g", True), ("a", True), ("version", False)),
    "add_exclusion": (("g", True), ("a", True), ("ex_g", True), ("ex_a", True)),
    "set_property": (("key", True), ("value", True)),
    "remove_property": (("key", True),),
    "set_plugin_version": (("g", True), ("a", True), ("version", True)),
}
# the names the prompt teaches (`group_id`, …) → the editor's parameter
_ARG_ALIASES: dict[str, str] = {
    "g": "g", "groupid": "g", "group": "g",
    "a": "a", "artifactid": "a", "artifact": "a",
    "version": "version", "v": "version",
    "scope": "scope", "type": "type", "classifier": "classifier",
    "exg": "ex_g", "exgroupid": "ex_g", "exclusiongroupid": "ex_g", "excludegroupid": "ex_g",
    "exclusiongroup": "ex_g",
    "exa": "ex_a", "exartifactid": "ex_a", "exclusionartifactid": "ex_a", "excludeartifactid": "ex_a",
    "exclusionartifact": "ex_a",
    "key": "key", "name": "key", "property": "key", "prop": "key",
    "value": "value",
}
# the editor's parameter → the name shown to the LLM in problems
_ARG_SHOWN = {"g": "group_id", "a": "artifact_id", "ex_g": "exclusion_group_id", "ex_a": "exclusion_artifact_id",
              "key": "name"}
# keys of an edit object that are not pom calls (everything else on a pom edit is a call name)
_ITEM_KEYS = {"op", "action", "path", "file", "pom", "comment", "note", "why", "reason", "description", "explanation",
              "old", "new", "count", "content", "text", "to"}
_OLD_KEYS = ("old", "search", "old_text", "find")
_NEW_KEYS = ("new", "replace", "new_text", "replacement")
_CONTENT_KEYS = ("content", "text", "new_content")
_TO_KEYS = ("to", "dst", "dest", "destination", "new_path", "target")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_EDIT_FAILED_RE = re.compile(r"^EditFailed: (.*?)(?: \(line \d+\))?$")
MAX_PROBLEMS = 20


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _norm_path(raw: Any) -> tuple[str, str]:
    """(posix path relative to the project, problem)."""
    if not isinstance(raw, str):
        return "", f"path must be a string, got {type(raw).__name__}"
    s = raw.strip().strip("`'\"").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if not s:
        return "", "empty path"
    if any(ord(c) < 32 for c in s):
        return "", f"path {raw.strip()!r} contains a control character"
    if s.startswith(("/", "~")) or _DRIVE_RE.match(s):
        return "", f"absolute path {raw.strip()!r}: use a path relative to the project root"
    rel = posixpath.normpath(s)
    if rel == "." or rel == ".." or rel.startswith("../"):
        return "", f"path {raw.strip()!r} leaves the project folder"
    if any(part.lower() == ".git" for part in rel.split("/")):
        return "", f"path {raw.strip()!r} is under .git/"
    return rel, ""


def _arg_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", str(name).lower())


def _scalar(v: Any) -> tuple[Optional[str], bool]:
    """(value as the editor takes it, ok). None stays None (null = managed by the parent / no value)."""
    if v is None:
        return None, True
    if isinstance(v, bool):
        return ("true" if v else "false"), True
    if isinstance(v, (str, int, float)):
        return str(v), True
    return None, False


def pom_call_args(call: str, spec: Any) -> tuple[list[dict[str, Optional[str]]], list[str]]:
    """The keyword arguments of one (or several) `p.pom(...)` calls of `call` from the LLM's spec: an object
    (`{"group_id": …}`), a list of objects (several calls), a positional list (`["g", "a", "1.0"]`) or a
    `"g:a[:v]"` string. Returns ([kwargs …], problems)."""
    params = POM_CALLS.get(call)
    if params is None:
        return [], [f"unknown pom call {call!r} (allowed: {', '.join(POM_CALLS)})"]
    names = [p for p, _ in params]
    if isinstance(spec, list) and spec and all(isinstance(x, dict) for x in spec):
        out: list[dict[str, Optional[str]]] = []
        problems: list[str] = []
        for item in spec:
            got, probs = pom_call_args(call, item)
            out += got
            problems += probs
        return out, problems
    raw: dict[str, Any] = {}
    if isinstance(spec, dict):
        excl = spec.get("exclusion")
        for k, v in spec.items():
            if k == "exclusion" and isinstance(excl, dict) and call == "add_exclusion":
                for ek, ev in excl.items():
                    canon = _ARG_ALIASES.get(_arg_key(ek))
                    raw[{"g": "ex_g", "a": "ex_a"}.get(canon or "", f"?{ek}")] = ev
                continue
            canon = _ARG_ALIASES.get(_arg_key(k))
            raw[canon if canon in names else f"?{k}"] = v
    elif isinstance(spec, str) and names[:2] == ["g", "a"]:
        parts = spec.strip().split(":")
        if len(parts) < 2 or len(parts) > len(names):
            return [], [f"{call}: {spec!r} is not 'groupId:artifactId[:version]'"]
        raw = dict(zip(names, parts))
    elif isinstance(spec, list):
        if len(spec) > len(names):
            return [], [f"{call}: {len(spec)} arguments, it takes at most {len(names)} ({', '.join(names)})"]
        raw = dict(zip(names, spec))
    elif isinstance(spec, str) and names == ["key"]:
        raw = {"key": spec}
    else:
        return [], [f"{call}: arguments must be an object like "
                    f"{{{', '.join(repr(_ARG_SHOWN.get(n, n)) + ': …' for n in names)}}}"]
    problems = []
    kwargs: dict[str, Optional[str]] = {}
    for k, v in raw.items():
        if k.startswith("?"):
            problems.append(f"{call}: unknown argument {k[1:]!r} (allowed: "
                            f"{', '.join(_ARG_SHOWN.get(n, n) for n in names)})")
            continue
        val, ok = _scalar(v)
        if not ok:
            problems.append(f"{call}: argument {_ARG_SHOWN.get(k, k)!r} must be a string or null")
            continue
        kwargs[k] = val
    if call == "set_dependency_version":
        kwargs.setdefault("version", None)                # null / absent = drop <version> (the parent manages it)
    for name, required in params:
        if not required:
            continue
        v = kwargs.get(name)
        empty_ok = name == "value" or (call == "set_plugin_version" and name == "g")   # "" = org.apache.maven.plugins
        if name not in kwargs or v is None or (v == "" and not empty_ok):
            problems.append(f"{call}: {_ARG_SHOWN.get(name, name)!r} is missing")
    return ([kwargs] if not problems else []), problems


def pom_calls(spec: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Optional[str]]]], list[str]]:
    """Ordered (call, kwargs) of a pom edit's `pom` dict (`{"add_dependency": {…}, "set_property": {…}}`; also
    `{"call": "add_dependency", "args": {…}}`). Unknown call names are refused (whitelist `POM_CALLS`)."""
    if not isinstance(spec, dict):
        return [], ["pom: the editor calls must be an object"]
    items = list(spec.items())
    call_name = spec.get("call") or spec.get("method")
    if isinstance(call_name, str) and ("args" in spec or "arguments" in spec):
        items = [(call_name, spec.get("args", spec.get("arguments")))]
    out: list[tuple[str, dict[str, Optional[str]]]] = []
    problems: list[str] = []
    for call, args in items:
        name = str(call).strip()
        if name == "save":
            continue                                      # implicit
        got, probs = pom_call_args(name, args)
        problems += probs
        out += [(name, kw) for kw in got]
    if not out and not problems:
        problems.append("pom: no editor call (e.g. \"add_dependency\": {\"group_id\": …, \"artifact_id\": …})")
    return out, problems


def edit_problems(edits: Sequence[Edit]) -> list[str]:
    """Why these edits cannot be applied, one line each ("edit 2: unknown pom call 'add_plugin' …"). [] = fine."""
    problems: list[str] = []
    for n, e in enumerate(edits, 1):
        for p in _one_edit_problems(e):
            problems.append(f"edit {n}: {p}")
    if len(problems) > MAX_PROBLEMS:
        problems = problems[:MAX_PROBLEMS] + [f"… {len(problems) - MAX_PROBLEMS} more problem(s)"]
    return problems


def _one_edit_problems(e: Edit) -> list[str]:
    if e.op not in EDIT_OPS:                              # a model_construct()ed Edit can carry anything
        return [f"unknown op {e.op!r} (allowed: {', '.join(EDIT_OPS)})"]
    _, why = _norm_path(e.path)
    out = [why] if why else []
    if e.op == "replace":
        if not e.old:
            out.append("replace needs the exact current text in 'old'")
        elif e.old.replace("\r\n", "\n") == e.new.replace("\r\n", "\n"):
            out.append("replace with 'old' equal to 'new' changes nothing")
    elif e.op == "move":
        if not e.to:
            out.append("move needs the destination in 'to'")
        else:
            _, why = _norm_path(e.to)
            if why:
                out.append(f"to: {why}")
    elif e.op == "pom":
        if posixpath.basename(e.path.replace("\\", "/").strip()) != "pom.xml":
            out.append(f"pom edits take a pom.xml path, got {e.path!r}")
        out += pom_calls(e.pom)[1]
    return out


def _first(item: dict[str, Any], keys: Iterable[str]) -> tuple[bool, Any]:
    for k in keys:
        if k in item:
            return True, item[k]
    return False, None


def edit_from_item(item: Any) -> tuple[Optional[Edit], list[str]]:
    """One edit object of the LLM's JSON → (`Edit`, problems). Lenient about key spellings (`file` for `path`,
    `search` / `replace` for `old` / `new`, `dst` for `to`); a pom edit's extra keys are its editor calls."""
    if not isinstance(item, dict):
        return None, [f"an edit must be an object, got {type(item).__name__}"]
    op = str(item.get("op") or item.get("action") or "").strip().lower()
    if not op:
        return None, ["no 'op' (one of: " + ", ".join(EDIT_OPS) + ")"]
    if op not in EDIT_OPS:
        return None, [f"unknown op {op!r} (allowed: {', '.join(EDIT_OPS)})"]
    has_path, path = _first(item, ("path", "file"))
    if not has_path:
        return None, [f"{op}: no 'path'"]
    rel, why = _norm_path(path)
    if why:
        return None, [why]
    data: dict[str, Any] = {"op": op, "path": rel}
    problems: list[str] = []
    if op == "replace":
        has_old, old = _first(item, _OLD_KEYS)
        has_new, new = _first(item, _NEW_KEYS)
        if not has_old or not isinstance(old, str) or not old:
            problems.append("replace needs the exact current text in 'old'")
        if not has_new or not isinstance(new, str):
            problems.append("replace needs the replacement text in 'new' (\"\" removes the text)")
        count = item.get("count", -1)
        if count is None:
            count = -1
        if isinstance(count, bool) or not isinstance(count, int):
            try:
                count = int(str(count))
            except ValueError:
                problems.append(f"replace: count must be a number, got {count!r}")
                count = -1
        data.update(old=old if isinstance(old, str) else "", new=new if isinstance(new, str) else "",
                    count=count if count != 0 else -1)
    elif op in ("write", "create"):
        has_content, content = _first(item, _CONTENT_KEYS)
        if not has_content and op == "write":
            problems.append("write needs the complete new text in 'content'")
        if has_content and not isinstance(content, str):
            problems.append(f"{op}: 'content' must be a string")
        data["content"] = content if isinstance(content, str) else ""
    elif op == "move":
        has_to, to = _first(item, _TO_KEYS)
        if not has_to:
            problems.append("move needs the destination in 'to'")
        else:
            to_rel, why = _norm_path(to)
            if why:
                problems.append(f"to: {why}")
            data["to"] = to_rel
    elif op == "pom":
        spec: dict[str, Any] = dict(item["pom"]) if isinstance(item.get("pom"), dict) else {}
        for k, v in item.items():
            if k not in _ITEM_KEYS:
                spec[k] = v
        data["pom"] = spec
    if problems:
        return None, problems
    try:
        e = Edit(**data)
    except Exception as exc:  # pydantic ValidationError
        return None, [f"invalid edit: {exc}"]
    more = _one_edit_problems(e)
    return (None, more) if more else (e, [])


def edits_from_json(obj: Any) -> tuple[list[Edit], list[str]]:
    """The parsed `<edits>` JSON (a list; also `{"edits": [...]}` or one edit object) → ([Edit …], problems).
    Any problem refuses the whole list (edits are applied all together or not at all)."""
    if isinstance(obj, dict):
        obj = obj["edits"] if isinstance(obj.get("edits"), list) else [obj]
    if obj is None:
        return [], []
    if not isinstance(obj, list):
        return [], [f"<edits> must be a JSON list of edit objects, got {type(obj).__name__}"]
    edits: list[Edit] = []
    problems: list[str] = []
    for n, item in enumerate(obj, 1):
        e, probs = edit_from_item(item)
        problems += [f"edit {n}: {p}" for p in probs]
        if e is not None:
            edits.append(e)
    if len(problems) > MAX_PROBLEMS:
        problems = problems[:MAX_PROBLEMS] + [f"… {len(problems) - MAX_PROBLEMS} more problem(s)"]
    return (edits if not problems else []), problems


# ---------------------------------------------------------------------------
# synthesis
# ---------------------------------------------------------------------------
_PREAMBLE = '''\
# sbmig v2.1: the LLM's <edits> applied directly (generated by sbmig.script.edits — {n} edit(s))


class EditFailed(Exception):
    pass


def fail(n, msg):
    raise EditFailed(f"edit {{n}}: {{msg}}")


def fix(p):
'''

_GUARD = '''\
    except EditFailed:
        raise
    except Exception as exc:
        fail({n}, {what} + f": {{exc}}")
'''


def _lit(s: str) -> str:
    return repr(s.replace("\r\n", "\n"))


def _edit_body(n: int, e: Edit) -> list[str]:
    path = _norm_path(e.path)[0]
    L = [f"        path = {_lit(path)}"]
    if e.op == "replace":
        count = e.count if e.count and e.count > 0 else -1
        L += ["        if not p.exists(path):",
              f'            fail({n}, f"file not found: {{path}}")',
              f"        old = {_lit(e.old)}",
              f"        new = {_lit(e.new)}",
              f"        k = p.replace(path, old, new, {count})",
              "        if k:",
              f'            p.log(f"edit {n}: {{path}}: replaced {{k}} occurrence(s)")',
              "        elif new and new in p.read(path):",
              f'            p.log(f"edit {n}: {{path}}: already applied (new text present)")',
              "        else:",
              f'            fail({n}, f"text not found in {{path}}")']
    elif e.op == "write":
        L += [f"        content = {_lit(e.content)}",
              "        if p.write(path, content):",
              f'            p.log(f"edit {n}: {{path}}: written ({{len(content)}} chars)")',
              "        else:",
              f'            p.log(f"edit {n}: {{path}}: unchanged")']
    elif e.op == "create":
        L += [f"        content = {_lit(e.content)}",
              "        if p.exists(path):",
              "            if p.read(path) != content:",
              f'                fail({n}, f"{{path}} already exists (use op write to replace it)")',
              f'            p.log(f"edit {n}: {{path}}: already there")',
              "        else:",
              "            p.create(path, content)",
              f'            p.log(f"edit {n}: {{path}}: created ({{len(content)}} chars)")']
    elif e.op == "delete":
        L += ["        if p.exists(path):",
              "            p.delete(path)",
              f'            p.log(f"edit {n}: {{path}}: deleted")',
              "        else:",
              f'            p.log(f"edit {n}: {{path}}: already gone")']
    elif e.op == "move":
        L += [f"        to = {_lit(_norm_path(e.to)[0])}",
              "        if p.exists(path):",
              "            if p.exists(to):",
              f'                fail({n}, f"cannot move {{path}}: {{to}} already exists")',
              "            p.move(path, to)",
              f'            p.log(f"edit {n}: {{path}} -> {{to}}")',
              "        elif p.exists(to):",
              f'            p.log(f"edit {n}: {{path}}: already moved to {{to}}")',
              "        else:",
              f'            fail({n}, f"nothing to move at {{path}}")']
    elif e.op == "pom":
        calls, _ = pom_calls(e.pom)
        L += ["        if not p.exists(path):",
              f'            fail({n}, f"pom not found: {{path}}")',
              "        pom = p.pom(path)",
              "        done = []"]
        for call, kwargs in calls:
            args = ", ".join(f"{k}={_lit(v) if v is not None else 'None'}" for k, v in kwargs.items())
            label = call + " " + ":".join(str(kwargs[k]) for k in ("g", "a", "key") if kwargs.get(k) is not None)
            if kwargs.get("value") is not None:
                label += "=" + str(kwargs["value"])
            elif kwargs.get("version") is not None and call != "remove_dependency":
                label += ":" + str(kwargs["version"])
            L += [f"        if pom.{call}({args}):",
                  f"            done.append({_lit(label.strip())})"]
        L += ["        if done:",
              "            pom.save()",
              f'            p.log(f"edit {n}: {{path}}: " + ", ".join(done))',
              "        else:",
              f'            p.log(f"edit {n}: {{path}}: nothing to change (already as asked)")']
    return L


def edits_script(edits: Sequence[Edit]) -> str:
    """The `def fix(p)` that applies `edits` in order through the helper API (texts embedded with `repr`). Raises
    ValueError with the problems when an edit is invalid (`edit_problems`). The code passes `check_script`; it is
    what `apply_edits` runs and what the loop may keep with the try for traceability."""
    edits = list(edits)
    problems = edit_problems(edits)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    lines = [_PREAMBLE.format(n=len(edits)).rstrip("\n")]
    if not edits:
        lines.append('    p.log("no edits")')
    for n, e in enumerate(edits, 1):
        what = f"{e.op} {_norm_path(e.path)[0]}"
        lines.append(f"    # edit {n}: {what}")
        lines.append("    try:")
        lines += _edit_body(n, e)
        lines.append(_GUARD.format(n=n, what=_lit(what)).rstrip("\n"))
    return "\n".join(lines) + "\n"


def edit_error(error: str) -> str:
    """The runner's error of a failed edits run → its first line without the exception name / script traceback:
    "EditFailed: edit 2: text not found in pom.xml (line 23)\\nscript traceback: …" → "edit 2: text not found in
    pom.xml". Other errors (timeout, the process failed) are kept as they are."""
    first = (error or "").strip().splitlines()[0] if (error or "").strip() else ""
    m = _EDIT_FAILED_RE.match(first)
    return m.group(1) if m else (error or "")


def summarize_edits(edits: Sequence[Edit]) -> str:
    """One line per edit for logs and notes: "replace pom.xml", "pom a/pom.xml add_dependency", …"""
    out = []
    for e in edits:
        if e.op == "pom":
            calls = [c for c, _ in pom_calls(e.pom)[0]]
            out.append(f"pom {e.path} " + ", ".join(calls))
        elif e.op == "move":
            out.append(f"move {e.path} -> {e.to}")
        else:
            out.append(f"{e.op} {e.path}")
    return "; ".join(out)


def apply_edits(ws: Any, edits: Sequence[Edit], *, cfg: Any, errors: Iterable[Any] = (), runner: Any = None,
                timeout_s: Optional[int] = None) -> ScriptRunResult:
    """Apply the LLM's edits to the workspace: validate (`edit_problems` → status `rejected`), synthesize the script
    (`edits_script`) and run it through `runner` (default: `ScriptRunner(cfg)` without the `script.max_code_chars`
    cap — the code is sbmig's own, a `write` may carry a whole file). Same `ScriptRunResult` as a fix script;
    `error` of a failed edit is "edit N: …" (`edit_error`). Changes stay uncommitted; NOT atomic — on any status but
    `ok` the caller restores the tree. Never raises."""
    edits = list(edits or ())
    try:
        problems = edit_problems(edits)
        if problems:
            more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
            log.info("edits refused before running: %s%s", problems[0], more)
            return ScriptRunResult(status="rejected", problems=problems, error=f"edits refused: {problems[0]}{more}")
        if not edits:
            return ScriptRunResult(status="ok", log=["no edits"])
        code = edits_script(edits)
        if runner is None:
            from .runner import ScriptRunner
            script_cfg = cfg.script.model_copy(update={"max_code_chars": 0})
            runner = ScriptRunner(cfg.model_copy(update={"script": script_cfg}))
            runner.label = "edits"
        res = runner.run(ws, code, errors=list(errors or ()), timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - never raises: an internal failure is the result
        log.debug("apply_edits failed", exc_info=True)
        return ScriptRunResult(status="error", error=f"sbmig could not apply the edits: {type(exc).__name__}: {exc}")
    if res.status != "ok" and res.error:
        res = res.model_copy(update={"error": edit_error(res.error)})
    return res


__all__ = [
    "EDIT_OPS",
    "POM_CALLS",
    "apply_edits",
    "edit_error",
    "edit_from_item",
    "edit_problems",
    "edits_from_json",
    "edits_script",
    "pom_call_args",
    "pom_calls",
    "summarize_edits",
]
