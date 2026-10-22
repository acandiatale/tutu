"""Extract reference tables for the handbook workbook straight from the code (run with the repo venv)."""
import json, re, sys, typing, inspect
from pathlib import Path
import yaml
REPO = Path.cwd()
out: dict = {}

def _j(lst):
    return ", ".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in (lst or []))

# ---------------------------------------------------------------- 1. config keys
from pydantic import BaseModel
from sbmig.core.config import Config, load_config

def yml_comments(path: Path) -> dict:
    """path 'a.b.c' -> (value text, comment) from default.yml (indent-based)."""
    res, stack, pending = {}, [], []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            pending = []
            continue
        if raw.strip().startswith("#"):
            pending.append(raw.strip().lstrip("#").strip()); continue
        m = re.match(r"^(\s*)([A-Za-z_][\w-]*):(.*)$", raw)
        if not m:
            continue
        ind, key, rest = len(m.group(1)), m.group(2), m.group(3)
        while stack and stack[-1][0] >= ind:
            stack.pop()
        stack.append((ind, key))
        p = ".".join(k for _, k in stack)
        val, _, com = rest.partition("#")
        comment = " ".join([*pending, com.strip()]).strip()
        pending = []
        res[p] = (val.strip().strip('"'), comment)
    return res
YC = yml_comments(REPO / "config/default.yml")

def tname(ann) -> str:
    s = repr(ann) if not isinstance(ann, type) else ann.__name__
    s = s.replace("typing.", "").replace("<class '", "").replace("'>", "")
    return re.sub(r"sbmig\.core\.config\.", "", s)

def is_model(ann) -> bool:
    return isinstance(ann, type) and issubclass(ann, BaseModel)

def inner_model(ann):
    if is_model(ann):
        return ann
    for a in typing.get_args(ann):
        if is_model(a):
            return a
    return None

def default_of(fi):
    from pydantic_core import PydanticUndefined
    if fi.default is not PydanticUndefined:
        return fi.default
    if fi.default_factory is not None:
        try:
            return fi.default_factory()
        except Exception:
            return "(factory)"
    return "(required)"

rows = []
def walk(model, prefix):
    for name, fi in model.model_fields.items():
        path = f"{prefix}{name}"
        sub = inner_model(fi.annotation)
        d = default_of(fi)
        y = YC.get(path, ("", ""))
        if sub is not None and not isinstance(d, (list, dict)) and typing.get_origin(fi.annotation) not in (dict, list):
            rows.append({"key": path, "type": tname(fi.annotation), "default": "(하위 섹션)", "yml": y[0], "comment": y[1] or (fi.description or "")})
            walk(sub, path + ".")
            continue
        if isinstance(d, BaseModel):
            d = d.model_dump()
        rows.append({"key": path, "type": tname(fi.annotation), "default": json.dumps(d, ensure_ascii=False, default=str) if not isinstance(d, str) else d,
                     "yml": y[0], "comment": y[1] or (fi.description or "")})
walk(Config, "")
out["config"] = rows

# ---------------------------------------------------------------- 2. handlers
from sbmig.triage.handlers import HandlerRegistry
from sbmig.plan.pre_handlers import HANDLER_IDS
try:
    from sbmig.loop.orchestrator import ALWAYS_PREEMPTIVE
except Exception:
    ALWAYS_PREEMPTIVE = ()
hs = []
for h in HandlerRegistry.default():
    hs.append({"id": h.id, "description": h.description, "kinds": ", ".join(sorted(k.value for k in h.kinds)) if len(h.kinds) < 11 else "(all)",
               "triggers": " | ".join(h.triggers), "preemptive": ("항상" if h.id in ALWAYS_PREEMPTIVE else "프로필 기반" if h.id in HANDLER_IDS else "반응형만"),
               "phase": getattr(h, "preemptive_phase", None).value if getattr(h, "preemptive_phase", None) else "", "class": type(h).__module__ + "." + type(h).__name__})
out["handlers"] = hs

# ---------------------------------------------------------------- 3. error catalog
tables = []
section = ""
header = None
for line in (REPO / "docs/ERROR-HANDLING.md").read_text(encoding="utf-8").splitlines():
    if line.startswith("## "):
        section, header = line[3:].strip(), None; continue
    if line.startswith("|"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells; continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        tables.append({"section": section, **{header[i] if i < len(header) else f"col{i}": c for i, c in enumerate(cells)}})
    else:
        if header is not None and not line.startswith("|"):
            header = None
out["errors"] = tables

# ---------------------------------------------------------------- 4. CLI
import typer, click
from sbmig.cli import app
root = typer.main.get_command(app)
cli = []
def walk_cmd(cmd, path):
    if hasattr(cmd, "commands"):
        for name, sub in sorted(cmd.commands.items()):
            walk_cmd(sub, path + [name])
        return
    full = " ".join(["sbmig", *path])
    help_ = (cmd.help or "").strip().splitlines()[0] if cmd.help else ""
    if not cmd.params:
        cli.append({"command": full, "help": help_, "param": "", "opts": "", "type": "", "required": "", "default": "", "param_help": ""})
    for p in cmd.params:
        if p.name in ("help",):
            continue
        cli.append({"command": full, "help": help_, "param": p.name, "opts": " / ".join([*p.opts, *p.secondary_opts]) if getattr(p, "param_type_name", "") == "option" else "(argument)",
                    "type": getattr(p.type, "name", str(p.type)), "required": "예" if p.required else "", "default": "" if p.default in (None, ()) else str(p.default),
                    "param_help": (getattr(p, "help", "") or "").strip()})
walk_cmd(root, [])
out["cli"] = cli

# ---------------------------------------------------------------- 5. REST
import tempfile
from sbmig.web.app import create_app
wapp = create_app(Path(tempfile.mkdtemp(prefix="sbmig-doc-")))
rest = []
for r in wapp.routes:
    methods = getattr(r, "methods", None)
    if not methods:
        continue
    doc = (getattr(r.endpoint, "__doc__", "") or "").strip().splitlines()
    rest.append({"method": ",".join(sorted(m for m in methods if m != "HEAD")), "path": r.path, "name": r.name,
                 "summary": getattr(r, "summary", None) or (doc[0] if doc else ""), "status": str(getattr(r, "status_code", "") or "")})
out["rest"] = rest

# ---------------------------------------------------------------- 6. fixtures
fx = []
for d in sorted((REPO / "fixtures/projects").iterdir()):
    if not d.is_dir():
        continue
    e = json.loads((d / "expected.json").read_text(encoding="utf-8")) if (d / "expected.json").exists() else {}
    readme = (d / "README.md").read_text(encoding="utf-8").splitlines() if (d / "README.md").exists() else []
    title = next((l[2:].strip() for l in readme if l.startswith("# ")), "")
    ex = e.get("expect", {})
    fx.append({"name": d.name, "title": title, "case": e.get("case_type", ""), "java": e.get("java_version", ""), "boot": e.get("spring_boot_version") or "",
               "spring": e.get("spring_version") or "", "packaging": e.get("packaging", ""), "multi": "예" if e.get("is_multi_module") else "",
               "libraries": _j(e.get("libraries")), "success": str(ex.get("success", "")), "exit": str(ex.get("exit_code", "")),
               "max_llm": str(ex.get("max_llm_calls", "")), "blocked": _j(ex.get("blocked")), "catalog": _j(e.get("catalog"))})
out["fixtures"] = fx

# ---------------------------------------------------------------- 7. journal events / 8. flags
ev: dict = {}
flags: dict = {}
for f in sorted((REPO / "sbmig").rglob("*.py")):
    t = f.read_text(encoding="utf-8")
    rel = f.relative_to(REPO).as_posix()
    for m in re.finditer(r'"event":\s*"([a-z_]+)"', t):
        ev.setdefault(m.group(1), set()).add(rel)
    for m in re.finditer(r'(?:_flag|\.flag|flags\.append)\(\s*f?"([^"]+)"', t):
        flags.setdefault(m.group(1), set()).add(rel)
out["events"] = [{"event": k, "files": ", ".join(sorted(v))} for k, v in sorted(ev.items())]
out["flags"] = [{"flag": k, "files": ", ".join(sorted(v))} for k, v in sorted(flags.items())]

# ---------------------------------------------------------------- 9. recipes / 10. advisories / 11. notes
cat = yaml.safe_load((REPO / "knowledge/recipes/catalog.yml").read_text(encoding="utf-8"))
out["recipes"] = [{"id": r.get("id"), "origin": r.get("origin"), "artifact": r.get("artifact") or "", "phase": r.get("phase") or "",
                   "advisor": "예" if r.get("advisor") else "", "gate": r.get("gate") or "", "sets": ", ".join(r.get("sets") or []),
                   "rules": ", ".join(r.get("rules") or []), "verified": str(r.get("verified") or ""), "description": r.get("description") or ""}
                  for r in cat.get("recipes", [])]
adv = yaml.safe_load((REPO / "knowledge/libraries/advisories.yml").read_text(encoding="utf-8"))
out["advisories"] = [{"id": a.get("id"), "library": a.get("library") or "", "artifacts": ", ".join(a.get("artifacts") or []), "status": a.get("status"),
                      "policy": a.get("policy"), "title": a.get("title") or "", "replacement": a.get("replacement") or "",
                      "replacement_version": str(a.get("replacement_version") or ""), "recipes": ", ".join(a.get("recipes") or []),
                      "applies_to": ", ".join(a.get("applies_to") or []), "summary": " ".join(str(a.get("summary") or "").split())}
                     for a in adv.get("advisories", [])]
# 10b. libraries with a known jakarta release (handler jakarta-library-floor) / 10c. coordinate relocations
jl = yaml.safe_load((REPO / "knowledge/libraries/jakarta_libraries.yml").read_text(encoding="utf-8"))
out["jakarta_libraries"] = [{"key": l.get("key"), "old_keys": ", ".join(l.get("old_keys") or []),
                             "jakarta_since": str(l.get("jakarta_since") or ""), "version": str(l.get("version") or ""),
                             "requires_java": str(l.get("requires_java") or ""), "note": " ".join(str(l.get("note") or "").split()),
                             "docs": " | ".join(l.get("docs") or [])} for l in jl.get("libraries", [])]
rl = yaml.safe_load((REPO / "sbmig/triage/handlers/relocations.yml").read_text(encoding="utf-8"))
out["relocations"] = [{"from": r.get("from"), "to": r.get("to"), "version": str(r.get("version") or ""), "scope": r.get("scope") or "",
                       "packages": ", ".join(r.get("packages") or []), "min_target_boot": str(r.get("min_target_boot") or "")}
                      for r in rl.get("relocations", [])]
notes = yaml.safe_load((REPO / "knowledge/notes/index.yml").read_text(encoding="utf-8"))
out["notes"] = [{"file": n.get("file"), "title": n.get("title"), "keywords": " | ".join(n.get("keywords") or []), "cases": ", ".join(n.get("cases") or []),
                 "phases": ", ".join(n.get("phases") or []), "always": "예" if n.get("always") else ""} for n in notes.get("notes", [])]

# ---------------------------------------------------------------- 12. enums
from sbmig.core import models as M
enums = []
for name in ("CaseType", "Phase", "StepKind", "ErrorKind", "DecisionKind", "PropertyStatus"):
    obj = getattr(M, name, None)
    if obj is None:
        continue
    if isinstance(obj, type):
        for mem in obj:
            enums.append({"enum": name, "value": mem.value, "note": ""})
    else:
        for v in typing.get_args(obj):
            enums.append({"enum": name, "value": v, "note": "(Literal)"})
out["enums"] = enums
json.dump(out, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print({k: len(v) for k, v in out.items()})
