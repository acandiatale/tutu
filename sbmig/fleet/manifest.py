"""일괄 실행 매니페스트를 읽어 어떤 프로젝트를 어떤 순서와 설정으로 이관할지 정하는 모듈.

``FleetRunner.run`` calls ``load_manifest`` first and reads the manifest again on every resume. Relative
paths are relative to the manifest file. A missing or unusable input never raises: the project gets an
``error`` and the runner marks it ``skipped``. Names become run ids and folder names, so they are ASCII. The
fleet state finds a project by its input path (:func:`input_key`), so a renamed project never inherits
another project's success.

Accepted forms (``load_manifest(path)``):

* a directory: every ``*.zip`` and every subdirectory with a ``pom.xml`` at its top or one level down (sorted
  by name; the same :func:`has_pom` rule :func:`_validate` applies); a subdirectory without one is left out
  with a warning naming it
* a ``.txt`` file: one path per line, ``#`` comments, blank lines ignored
* a ``.yml``/``.yaml`` file: ``projects: [{name, input, configs: [...], overrides: {...}, tags: [...]}]``
* a ``.csv`` file: header ``name,input,tags`` (tags separated by ``;`` or spaces)

Encodings: UTF-8 with or without BOM (Excel "CSV UTF-8"), UTF-16 (PowerShell 5.1 ``>``), else cp949.
A non-ASCII name (``주문관리-v2``) becomes its ASCII slug plus a short hash of the input path (``v2-1a2b3c``,
``project-4d5e6f``); a name that collides with an earlier one gets the hash too.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("sbmig.fleet")


@dataclass
class FleetProject:
    """매니페스트의 프로젝트 하나를 담는 데이터 클래스.
    이름(ASCII), 절대 입력 경로, 프로젝트별 설정, 덮어쓴 값, 태그를 담는다.
    입력을 쓸 수 없으면 ``error``를 채운다. 러너는 이 프로젝트를 실행하지 않고 ``skipped``로 둔다."""
    name: str
    input: str  # 프로젝트 폴더나 zip의 절대 경로
    configs: list[str] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    error: Optional[str] = None  # 입력이 없거나 쓸 수 없을 때 채운다

    @property
    def ok(self) -> bool:
        """입력을 쓸 수 있는지(``error``가 없는지) 반환한다."""
        return self.error is None


class ManifestError(ValueError):
    """매니페스트 자체가 잘못되었을 때(형식 오류, 필수 키 누락) 던지는 예외.
    입력 하나가 없는 것은 예외가 아니라 ``FleetProject.error``로 알린다."""


_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
HASH_CHARS = 6


def safe_name(name: str) -> str:
    """실행 ID와 폴더명에 쓸 수 있는 이름(ASCII 영문, 숫자, ``._-``)으로 바꾼다.
    남는 문자가 없으면 ``project``를 반환한다."""
    n = _NAME_RE.sub("-", name).strip("-.")
    return n or "project"


def input_key(path: str | Path) -> str:
    """입력 경로를 절대 경로로 정규화해서 반환한다. 일괄 실행 상태에서 프로젝트를 구분하는 키다.
    Windows에서는 대소문자도 통일한다.

    Older state files stored the same resolved path, so they match unchanged."""
    p = str(path or "").strip()
    if not p:
        return ""
    try:
        p = str(Path(p).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return os.path.normcase(os.path.normpath(p))


def path_hash(path: str | Path, n: int = HASH_CHARS) -> str:
    """입력 경로(:func:`input_key`)의 sha1 앞 ``n``자리를 반환한다. 매니페스트 순서가 바뀌어도 값은 같다."""
    return hashlib.sha1(input_key(path).encode("utf-8")).hexdigest()[:n]


def name_with_hash(name: str, path: str | Path) -> str:
    """``<slug>-<경로 해시>`` 형태의 이름을 만든다. 한글 이름이나 겹치는 이름을 구분할 때 쓴다."""
    return f"{safe_name(name)}-{path_hash(path)}"


def read_text_any(path: str | Path) -> str:
    """텍스트 파일을 편집기가 저장한 인코딩과 상관없이 읽는다.
    UTF-8(BOM 유무), UTF-16, 그 밖에는 cp949로 읽는다.
    UnicodeDecodeError를 던지지 않는다."""
    data = Path(path).read_bytes()
    # PowerShell 5.1의 ``>``는 UTF-16LE로 저장한다. BOM이 있거나, BOM이
    # 없어도 둘째와 넷째 바이트가 NUL이면 UTF-16으로 읽는다
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00" and data[0:1] != b"\x00":
        try:
            return data.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    try:
        # utf-8-sig로 읽는다. 엑셀 "CSV UTF-8"의 BOM 때문에 첫 열 이름(``name``)이 깨지지 않게 한다
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def _name_for(p: Path) -> str:
    return p.stem if p.suffix.lower() == ".zip" else p.name


def _resolve(base: Path, p: str) -> Path:
    q = Path(str(p)).expanduser()
    return q if q.is_absolute() else (base / q)


def has_pom(d: Path) -> bool:
    """맨 위나 한 단계 아래(``*/pom.xml``)에 ``pom.xml``이 있으면 Maven 프로젝트로 보고 True를 반환한다.
    디렉토리 매니페스트(:func:`_from_dir`)와 입력 검사(:func:`_validate`)가 같은 기준으로 쓴다.

    One level down is enough because intake turns sibling poms into an aggregator."""
    return (d / "pom.xml").is_file() or any(q.is_file() for q in d.glob("*/pom.xml"))


def _validate(proj: FleetProject) -> FleetProject:
    """입력을 쓸 수 있는지 확인한다. 쓸 수 없으면 ``error``를 채운다. 예외를 던지지 않는다.
    입력이 없거나, pom.xml이 없거나(:func:`has_pom`), 디렉토리도 zip도 아니면 오류다.
    프로젝트별 설정 파일이 없어도 오류다."""
    p = Path(proj.input)
    if not p.exists():
        proj.error = f"input not found: {proj.input}"
    elif p.is_dir():
        if not has_pom(p):
            proj.error = f"no pom.xml in {proj.input}"
    elif p.suffix.lower() != ".zip":
        proj.error = f"input must be a directory or a .zip: {proj.input}"
    for c in proj.configs:
        if not Path(c).exists():
            proj.error = (proj.error + "; " if proj.error else "") + f"config not found: {c}"
    return proj


def _dedupe_names(projects: list[FleetProject]) -> list[FleetProject]:
    """이름을 겹치지 않는 ASCII로 정리한다.
    slug로 바꾸면서 문자를 잃거나 앞 이름과 겹치면 입력 경로 해시를 붙인다.
    그래도 겹치면(같은 입력이 두 번 나온 경우) 번호를 붙인다.

    The path hash stays the same when the manifest is reordered or a folder is added."""
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
    """디렉토리 매니페스트를 읽는다.
    ``*.zip``과 pom이 있는 하위 폴더를 이름순으로 모은다. 점으로 시작하는 폴더는 뺀다.
    pom이 없는 폴더도 뺀다. 조용히 사라지지 않게 폴더 이름을 경고로 남긴다."""
    out = []
    for p in sorted(d.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue
        if (p.is_file() and p.suffix.lower() == ".zip") or (p.is_dir() and has_pom(p)):
            out.append(FleetProject(name=_name_for(p), input=str(p.resolve())))
        elif p.is_dir():
            # pom이 없는 폴더는 이관하지 않는다. 운영자가 알 수 있게 이름을 경고로 남긴다
            log.warning("fleet manifest: %s left out - no pom.xml at its top or one level down", p)
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
    """매니페스트(디렉토리, .txt, .yml, .csv)를 읽어 프로젝트 목록을 반환한다. 이름을 정리하고 입력도 검사한다.
    매니페스트가 없거나 형식이 틀리면 :class:`ManifestError`를 던진다.
    입력 하나가 없으면 그 프로젝트의 ``error``를 채운다."""
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
