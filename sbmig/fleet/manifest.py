"""일괄 실행 매니페스트 — 어떤 프로젝트를, 어떤 순서로, 프로젝트별로 어떤 설정으로 이관할지 읽는다.
흐름상 위치: ``FleetRunner.run`` 이 맨 먼저 ``load_manifest`` 를 부른다(재개할 때도 매번 다시 읽는다). 형식:
디렉토리(``*.zip`` 과 ``pom.xml`` 이 있는 하위 폴더), ``.txt`` (한 줄에 경로 하나), ``.yml`` (configs /
overrides / tags), ``.csv`` (``name,input,tags``). 상대 경로는 매니페스트 파일 위치 기준. 입력이 없거나 쓸 수
없어도 예외를 내지 않고 ``error`` 를 채워 돌려준다 → 러너가 ``skipped`` 로 둔다. 윈도우 운영자 대비(round 10):
인코딩은 UTF-8(BOM 유무)·UTF-16·cp949 무엇이든 읽는다. 이름은 실행 ID·폴더명이 되므로 ASCII 로 만든다(한글
이름은 slug + 입력 경로 해시). 일괄 실행 상태는 이름이 아니라 입력 경로(:func:`input_key`)로 프로젝트를 찾는다
— 이름이 바뀌어도 남의 성공을 물려받지 않게.

Fleet manifests: which projects to migrate, in which order, with which per-project config.

Accepted forms (``load_manifest(path)``):

* a **directory**: every ``*.zip`` and every subdirectory containing a ``pom.xml`` (sorted by name)
* a ``.txt`` file: one path per line, ``#`` comments, blank lines ignored
* a ``.yml``/``.yaml`` file: ``projects: [{name, input, configs: [...], overrides: {...}, tags: [...]}]``
* a ``.csv`` file: header ``name,input,tags`` (tags separated by ``;`` or spaces)

Relative paths are resolved against the manifest file's directory. Missing inputs do not raise: the project is
returned with ``error`` set and the runner marks it ``skipped``.

Round 10 (Windows operators): manifest files are read whatever the editor saved — UTF-8 with or without BOM (Excel
"CSV UTF-8"), UTF-16 (PowerShell 5.1 ``>``), else cp949. Project names are ASCII (run ids, directory names): a name
with non-ASCII characters (``주문관리-v2``) becomes its ASCII slug plus a short hash of the input path
(``v2-1a2b3c``, ``project-4d5e6f``), and a name that collides with an earlier one gets the hash too, so two Korean
folder names never share a name. The fleet state is keyed by the input path (:func:`input_key`), not by the name.


"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class FleetProject:
    """매니페스트 한 줄 = 프로젝트 하나: 이름(ASCII), 입력 경로(절대), 프로젝트별 설정·오버라이드·태그,
    쓸 수 없는 입력이면 ``error`` (그때 러너는 실행하지 않고 ``skipped``)."""
    name: str
    input: str  # resolved path (absolute) of the project dir / zip
    configs: list[str] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    error: Optional[str] = None  # set when the input does not exist / is not usable

    @property
    def ok(self) -> bool:
        """입력이 쓸 만한가(``error`` 없음)."""
        return self.error is None


class ManifestError(ValueError):
    """매니페스트 자체가 틀림(형식·필수 키 누락).
    입력 하나가 없는 것은 이 예외가 아니라 ``FleetProject.error`` 다."""


_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
HASH_CHARS = 6


def safe_name(name: str) -> str:
    """실행 ID·폴더명에 쓸 수 있는 이름(ASCII 영문·숫자·``._-``). 남는 것이 없으면 ``project``.

    A name usable in run ids / directory names (ASCII letters, digits, `._-`)."""
    n = _NAME_RE.sub("-", name).strip("-.")
    return n or "project"


def input_key(path: str | Path) -> str:
    """일괄 실행 상태에서 프로젝트를 가리키는 열쇠: 입력 경로를 절대·정규화한 것(윈도우는 대소문자도 접는다).
    예전 상태 파일도 같은 절대 경로를 적었으므로 그대로 맞는다.

    The identity of a project in the fleet state: its input path, absolute and normalized (case-folded where the
    file system is, i.e. on Windows). Older state files stored the same resolved path, so they match unchanged."""
    p = str(path or "").strip()
    if not p:
        return ""
    try:
        p = str(Path(p).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return os.path.normcase(os.path.normpath(p))


def path_hash(path: str | Path, n: int = HASH_CHARS) -> str:
    """입력 경로(:func:`input_key`)의 sha1 앞 ``n`` 자리 — 매니페스트 순서가 바뀌어도 같은 값."""
    return hashlib.sha1(input_key(path).encode("utf-8")).hexdigest()[:n]


def name_with_hash(name: str, path: str | Path) -> str:
    """``<slug>-<경로 해시>``: 한글 이름이나 겹치는 이름을 서로 다르게 만든다."""
    return f"{safe_name(name)}-{path_hash(path)}"


def read_text_any(path: str | Path) -> str:
    """편집기가 무엇으로 저장했든 읽는다: UTF-8(BOM 유무), UTF-16(BOM, 또는 BOM 없는 UTF-16LE 의 NUL 패턴),
    그 밖은 cp949(한국어 윈도우). 매니페스트 때문에 UnicodeDecodeError 가 나는 일은 없다.

    A text file whatever the editor saved: UTF-8 (with or without BOM), UTF-16 (BOM, or the NUL pattern of BOM-less
    UTF-16LE), else cp949 (Korean Windows) — never a UnicodeDecodeError for a manifest."""
    data = Path(path).read_bytes()
    # PowerShell 5.1 의 ``>`` 는 UTF-16LE 로 저장한다: BOM 이 있으면 그대로, 없으면 둘째·넷째 바이트가 NUL(첫
    # 바이트는 아님)인 것으로 알아본다
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00" and data[0:1] != b"\x00":
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    try:
        # utf-8-sig: 엑셀 "CSV UTF-8" 의 BOM 을 떼어 첫 열 이름(``name``)이 깨지지 않게
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def _name_for(p: Path) -> str:
    return p.stem if p.suffix.lower() == ".zip" else p.name


def _resolve(base: Path, p: str) -> Path:
    q = Path(str(p)).expanduser()
    return q if q.is_absolute() else (base / q)


def _validate(proj: FleetProject) -> FleetProject:
    """입력이 쓸 만한지 보고 아니면 ``error`` 를 채운다(예외 없음): 입력 없음,
    pom.xml 이 맨 위에도 한 단계 아래에도 없음, 디렉토리도 zip 도 아님, 프로젝트별 설정 파일 없음."""
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
    """이름을 ASCII 로, 겹치지 않게: slug 로 글자를 잃는 이름(한글 등)이나 앞 이름과 겹치는 이름에는
    입력 경로 해시를 붙인다(순서가 바뀌거나 폴더가 늘어도 같은 이름). 같은 입력이 두 번이면 번호 접미사.

    ASCII names, unique: a name that loses characters to the slug (non-ASCII) or collides with an earlier one gets
    the hash of its input path (stable when the manifest is reordered or a folder is added); the same input listed
    twice keeps a numbered suffix."""
    taken: set[str] = set()
    for p in projects:
        base = safe_name(p.name)
        lossy = not p.name.isascii() or base == "project" and p.name.strip() != "project"
        name = name_with_hash(p.name, p.input) if lossy else base
        if name in taken and not lossy:
            name = name_with_hash(p.name, p.input)
        n = 1
        candidate = name
        while candidate in taken:
            n += 1
            candidate = f"{name}-{n}"
        taken.add(candidate)
        p.name = candidate
    return projects


def _from_dir(d: Path) -> list[FleetProject]:
    """디렉토리 매니페스트: 이름순으로 ``*.zip`` 과 ``pom.xml`` 이 바로 아래 있는 하위 폴더
    (점으로 시작하는 것 제외)."""
    out = []
    for p in sorted(d.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue
        if (p.is_file() and p.suffix.lower() == ".zip") or (p.is_dir() and (p / "pom.xml").exists()):
            out.append(FleetProject(name=_name_for(p), input=str(p.resolve())))
    return out


def _from_txt(f: Path) -> list[FleetProject]:
    out = []
    for line in read_text_any(f).splitlines():
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
    data = yaml.safe_load(read_text_any(f)) or {}
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
    rows = list(csv.DictReader(io.StringIO(read_text_any(f), newline="")))
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
    """매니페스트(디렉토리 / .txt / .yml / .csv)를 읽어 프로젝트 목록을 돌려준다(이름 정리 + 입력 검사까지).
    매니페스트가 없거나 형식이 틀리면 :class:`ManifestError`, 입력 하나가 없으면 그 프로젝트의 ``error``."""
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
