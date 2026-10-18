"""Fleet manifests: which projects to migrate, in which order, with which per-project config.

Accepted forms (``load_manifest(path)``):

* a **directory**: every ``*.zip`` and every subdirectory containing a ``pom.xml`` (sorted by name)
* a ``.txt`` file: one path per line, ``#`` comments, blank lines ignored
* a ``.yml``/``.yaml`` file: ``projects: [{name, input, configs: [...], overrides: {...}, tags: [...]}]``
* a ``.csv`` file: header ``name,input,tags`` (tags separated by ``;`` or spaces)

Relative paths are resolved against the manifest file's directory. Missing inputs do not raise: the project is
returned with ``error`` set and the runner marks it ``skipped``.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class FleetProject:
    name: str
    input: str  # resolved path (absolute) of the project dir / zip
    configs: list[str] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    error: Optional[str] = None  # set when the input does not exist / is not usable

    @property
    def ok(self) -> bool:
        return self.error is None


class ManifestError(ValueError):
    pass


_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    """A name usable in run ids / directory names."""
    n = _NAME_RE.sub("-", name).strip("-.")
    return n or "project"


def _name_for(p: Path) -> str:
    return p.stem if p.suffix.lower() == ".zip" else p.name


def _resolve(base: Path, p: str) -> Path:
    q = Path(str(p)).expanduser()
    return q if q.is_absolute() else (base / q)


def _validate(proj: FleetProject) -> FleetProject:
    p = Path(proj.input)
    if not p.exists():
        proj.error = f"input not found: {proj.input}"
    elif p.is_dir():
        if not (p / "pom.xml").exists() and not any(p.glob("*/pom.xml")):
            proj.error = f"no pom.xml in {proj.input}"
    elif p.suffix.lower() != ".zip":
        proj.error = f"input must be a directory or a .zip: {proj.input}"
    for c in proj.configs:
        if not Path(c).exists():
            proj.error = (proj.error + "; " if proj.error else "") + f"config not found: {c}"
    return proj


def _dedupe_names(projects: list[FleetProject]) -> list[FleetProject]:
    seen: dict[str, int] = {}
    for p in projects:
        base = safe_name(p.name)
        n = seen.get(base, 0)
        seen[base] = n + 1
        p.name = base if n == 0 else f"{base}-{n + 1}"
    return projects


def _from_dir(d: Path) -> list[FleetProject]:
    out = []
    for p in sorted(d.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue
        if (p.is_file() and p.suffix.lower() == ".zip") or (p.is_dir() and (p / "pom.xml").exists()):
            out.append(FleetProject(name=_name_for(p), input=str(p.resolve())))
    return out


def _from_txt(f: Path) -> list[FleetProject]:
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            p = _resolve(f.parent, line)
            out.append(FleetProject(name=_name_for(p), input=str(p.resolve())))
    return out


def _tags(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [t for t in re.split(r"[;,\s]+", v) if t]
    return [str(t) for t in v]


def _from_yml(f: Path) -> list[FleetProject]:
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    items = data.get("projects") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ManifestError(f"{f}: expected 'projects: [...]'")
    out = []
    for i, it in enumerate(items):
        if isinstance(it, str):
            it = {"input": it}
        if not isinstance(it, dict) or not it.get("input"):
            raise ManifestError(f"{f}: projects[{i}] needs an 'input'")
        p = _resolve(f.parent, str(it["input"]))
        configs = it.get("configs") or []
        if isinstance(configs, str):
            configs = [configs]
        overrides = it.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ManifestError(f"{f}: projects[{i}].overrides must be a mapping")
        out.append(
            FleetProject(
                name=str(it.get("name") or _name_for(p)),
                input=str(p.resolve()),
                configs=[str(_resolve(f.parent, str(c)).resolve()) for c in configs],
                overrides=overrides,
                tags=_tags(it.get("tags")),
            )
        )
    return out


def _from_csv(f: Path) -> list[FleetProject]:
    out = []
    with open(f, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for i, row in enumerate(rows):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not row.get("input"):
            if any(row.values()):
                raise ManifestError(f"{f}: row {i + 2} needs an 'input'")
            continue
        p = _resolve(f.parent, row["input"])
        out.append(
            FleetProject(
                name=row.get("name") or _name_for(p), input=str(p.resolve()), tags=_tags(row.get("tags"))
            )
        )
    return out


def load_manifest(path: str | Path) -> list[FleetProject]:
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"manifest not found: {path}")
    if path.is_dir():
        projects = _from_dir(path)
    else:
        suffix = path.suffix.lower()
        if suffix == ".txt":
            projects = _from_txt(path)
        elif suffix in (".yml", ".yaml"):
            projects = _from_yml(path)
        elif suffix == ".csv":
            projects = _from_csv(path)
        else:
            raise ManifestError(
                f"unsupported manifest type {suffix or '(none)'}: use a directory, .txt, .yml or .csv"
            )
    return [_validate(p) for p in _dedupe_names(projects)]
