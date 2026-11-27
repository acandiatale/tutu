"""사내 프레임워크 프로젝트를 Spring Boot 프로젝트로 전환하는 접수 단계의 도우미 함수를 모은 모듈.

Used by `Orchestrator._intake_convert` (docs/INTERFACES.md "v2.1z-cl"; out/scratch/fwconvert/ROUND2.md section E).
Pure helpers: the run value `framework_mode`, the names read from the as-is root pom, the working-tree swap and the
summary records. Nothing here calls Maven or the network; the repository lookup is passed in.

Working-tree swap (`TreeSwap`): nothing of the as-is tree is deleted before the conversion commit exists. Every entry
of the project root except `.git` and `.sbmig` is renamed into `<run dir>/convert/asis/` (one `os.rename` per
top-level entry: same file system, no copy, ignored and untracked files included), the generated files are moved in
(`shutil.move`: a rename, the mode bits stay), the caller commits, and only then the aside folder is removed. On any
failure in between `rollback` removes what the generated tree put in place and renames every entry back; a read-only
directory gets the owner's write bit for its rename and its mode back right after. Symlinks are renamed or removed as
links, never followed. A generated path that is absolute, has `..`, or starts with a kept name is refused with
ValueError before anything is moved.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sbmig.core.models import FRAMEWORK_MODE_REDUCED, FRAMEWORK_MODES, ConversionInfo

CONVERT_COMMIT = "convert: generated Spring Boot project"
FLAG_CONVERTED = "framework-converted"           # 주의 코드가 아니다
FLAG_UNAVAILABLE = "aborted:convert-unavailable"
FLAG_FAILED = "aborted:convert-failed"
QUEUE_CAP = 200                                  # report.json에 넣는 확인 항목 수 상한
CONVERT_DIR = "convert"                          # 실행 폴더 안에서 전환 결과를 두는 폴더
KEEP_NAMES = (".git", ".sbmig")                  # 작업 트리를 바꿀 때 남기는 이름
UNAVAILABLE_PREFIX = "framework conversion is not available: "
FAILED_PREFIX = "framework conversion failed: "
SKIPPED_LINE = ("framework conversion found nothing to convert (no service XML and no SQL file of the framework): the "
                "project was left as uploaded and migrated as with the way 'reduced'")
UNREADABLE_LINE_HEAD = "framework conversion found "


def unreadable_line(candidates: int) -> str:
    """이름은 프레임워크 파일 같지만 하나도 읽지 못했을 때의 인수인계 문장을 반환한다."""
    return (f"{UNREADABLE_LINE_HEAD}{candidates} files that look like framework files by name, but none could be read "
            "as one: the project was handled without the conversion, as with the way 'reduced'")


DUPLICATE_LINE_HEAD = "framework conversion: duplicate request mappings in the generated controllers"

_JAVA_PACKAGE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def parse_framework_mode(value: Any, default: Any = FRAMEWORK_MODE_REDUCED) -> str:
    """처리 방법 값(``--framework-mode``)을 ``FRAMEWORK_MODES`` 중 하나로 바꾼다. 대소문자와 앞뒤 공백은 무시한다.
    None이나 빈 문자열이면 ``default``를 쓴다. 모르는 값이면 ValueError를 던진다."""
    text = "" if value is None else str(value).strip().lower()
    if not text:
        text = "" if default is None else str(default).strip().lower()
    if not text:
        return FRAMEWORK_MODE_REDUCED
    if text not in FRAMEWORK_MODES:
        raise ValueError(f"framework_mode must be one of: {', '.join(FRAMEWORK_MODES)}")
    return text


@dataclass(frozen=True)
class PomNames:
    """루트 pom에서 읽은 groupId와 artifactId를 담는 클래스."""
    group_id: str
    artifact_id: str


def _legal_package(name: str) -> bool:
    if not _JAVA_PACKAGE_RE.match(name):
        return False
    from sbmig.fwconvert.naming import JAVA_KEYWORDS
    return not any(seg in JAVA_KEYWORDS for seg in name.split("."))


def read_root_pom(root: Path) -> tuple[PomNames | None, str]:
    """프로젝트 루트 pom.xml의 groupId와 artifactId를 읽는다. Maven은 호출하지 않는다.
    groupId가 없으면 ``<parent>``의 groupId를 쓴다. 속성은 그 pom의 ``<properties>``로만 푼다.
    쓸 수 있으면 (이름, 빈 문자열)을, 쓸 수 없으면 (None, 그 이유)를 반환한다.

    Reasons (English, they end up in the refusal sentence): "the project has no root pom to take the package from",
    "the root pom cannot be read (<why>)", "the root pom has no groupId", "the root pom's groupId '<g>' cannot be used
    as a package", "the root pom has no artifactId", "the root pom's artifactId '<a>' cannot be used as a project
    name". A Java keyword segment (`new`) is not a legal package."""
    from sbmig.analyze.pom_parser import interpolate, parse_pom_text
    p = Path(root) / "pom.xml"
    try:
        if p.is_symlink() or not p.is_file():
            return None, "the project has no root pom to take the package from"
        data = p.read_bytes()
    except OSError as e:
        return None, f"the root pom cannot be read ({type(e).__name__})"
    try:
        raw = parse_pom_text(data, "pom.xml")
    except ValueError as e:
        if "missing <artifactId>" in str(e):
            return None, "the root pom has no artifactId"
        return None, "the root pom cannot be read (not well-formed XML)"
    props = dict(raw.properties or {})
    group = (interpolate(raw.eff_group, props) or "").strip()
    artifact = (interpolate(raw.artifact_id, props) or "").strip()
    if not group:
        return None, "the root pom has no groupId"
    if not _legal_package(group):
        return None, f"the root pom's groupId '{group[:80]}' cannot be used as a package"
    if not _ARTIFACT_RE.match(artifact):
        return None, f"the root pom's artifactId '{artifact[:80]}' cannot be used as a project name"
    return PomNames(group_id=group, artifact_id=artifact), ""


def root_pom_names(root: Path) -> PomNames | None:
    """루트 pom에서 쓸 수 있는 groupId와 artifactId를 반환한다. 쓸 수 없으면 None을 반환한다."""
    return read_root_pom(root)[0]


def project_domain(name: str, domains: Iterable[str]) -> str:
    """프로젝트 이름이 ``<코드>-``로 시작하고 그 코드가 ``domains``에 있으면 설정에 적힌 코드를 반환한다.
    대소문자는 가리지 않는다. 맞는 코드가 없으면 빈 문자열을 반환한다."""
    head = re.split(r"[-_]", str(name or "").strip(), maxsplit=1)[0].lower()
    if not head or head == str(name or "").strip().lower():
        return ""
    for d in domains or []:
        if str(d).strip().lower() == head:
            return str(d).strip()
    return ""


def is_duplicate_mapping(line: str) -> bool:
    """전환 오류 문장이 생성한 컨트롤러의 주소 중복(``duplicate_mappings``의 줄)이면 True를 반환한다."""
    t = str(line or "")
    return ": claimed by " in t or " differ only in case: " in t or t.lower().startswith("duplicate mapping")


def duplicate_followup(errors: Iterable[str], limit: int = 5) -> str:
    """주소 중복 오류를 인수인계 한 줄로 만든다. 중복이 없으면 빈 문자열을 반환한다."""
    dups = [str(e) for e in errors or [] if is_duplicate_mapping(e)]
    if not dups:
        return ""
    shown = "; ".join(dups[:limit]) + (f"; (+{len(dups) - limit} more)" if len(dups) > limit else "")
    return (f"{DUPLICATE_LINE_HEAD} ({len(dups)}): two handlers answer the same address and the application does not "
            f"start until the owners keep one of them — {shown}")


def newest_release(lookup: Any, key: str) -> str:
    """저장소 조회기에서 ``g:a``의 최신 정식 릴리스를 찾아 반환한다. 프리릴리스는 뺀다.
    조회기가 없거나 찾지 못하면 빈 문자열을 반환한다. 예외를 던지지 않는다."""
    g, _, a = str(key).partition(":")
    if lookup is None or not g or not a:
        return ""
    try:
        from sbmig.build.repo_versions import is_prerelease
        for v in reversed(list(lookup.versions(g, a) or [])):
            if v and not is_prerelease(v):
                return str(v)
    except Exception:  # noqa: BLE001 - 조회 실패는 찾지 못한 것으로 본다
        return ""
    return ""


def _owner_writable(path: Path) -> int | None:
    # 쓰기 권한이 없는 폴더는 주인 쓰기 권한을 잠시 주고 원래 권한을 반환한다. 이미 쓸 수 있으면 None이다
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_mode & stat.S_IWUSR:
        return None
    os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IWUSR)
    return stat.S_IMODE(st.st_mode)


def _chmod_twice(path: Path, mode: int) -> None:
    # 권한을 돌려놓는 일은 한 번 더 시도한다. 두 번째도 실패하면 OSError를 던진다
    try:
        os.chmod(path, mode)
    except OSError:
        os.chmod(path, mode)


def rename_entry(src: Path, dst: Path) -> None:
    """항목 하나를 같은 파일 시스템 안에서 이름만 바꿔 옮긴다. 링크는 따라가지 않는다.
    읽기 전용 폴더는 옮기는 동안만 쓰기 권한을 준다. 옮기지 못해도 원래 권한으로 돌린다.
    실패하면 OSError를 던진다. 옮긴 뒤 권한을 돌리다 실패해도 던진다. 그때 항목은 이미 ``dst``에 있다."""
    src, dst = Path(src), Path(dst)
    restore: list[tuple[Path, int]] = []
    own: int | None = None
    renamed = False
    try:
        for d in {src.parent, dst.parent}:
            m = _owner_writable(d)
            if m is not None:
                restore.append((d, m))
        own = _owner_writable(src)                # 폴더를 다른 폴더로 옮기려면 그 폴더에도 쓰기 권한이 있어야 한다
        os.rename(src, dst)
        renamed = True
    finally:
        errors: list[OSError] = []
        if own is not None:
            try:
                _chmod_twice(dst if renamed else src, own)
            except OSError as e:
                errors.append(e)
        for d, m in restore:
            try:
                _chmod_twice(d, m)
            except OSError:
                pass
        if errors and renamed:
            raise errors[0]


def _force_remove(path: Path) -> None:
    # 읽기 전용 폴더 안의 항목도 지운다. 지우기 전에 그 부모 폴더에 쓰기 권한을 준다
    def again(func: Any, p: str, _exc: Any) -> None:
        for d in (os.path.dirname(p), p):
            try:
                if os.path.isdir(d) and not os.path.islink(d):
                    os.chmod(d, stat.S_IMODE(os.lstat(d).st_mode) | stat.S_IRWXU)
            except OSError:
                pass
        func(p)
    p = Path(path)
    if p.is_symlink() or not p.is_dir():
        try:
            p.unlink()
        except FileNotFoundError:
            return
        except PermissionError:
            again(os.unlink, str(p), None)
        return
    shutil.rmtree(p, onerror=again)


SWAP_MARKER = "swap.json"                         # 작업 트리를 바꾸는 중임을 알리는 표시 파일(실행 폴더의 convert/ 안)


def write_marker(path: Path, data: dict[str, Any]) -> None:
    """표시 파일을 원자적으로 쓴다. 실패하면 OSError를 던진다."""
    import json
    tmp = Path(path).with_name(Path(path).name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class TreeSwap:
    """작업 트리를 생성한 프로젝트로 바꾸는 클래스. 전환 커밋이 생기기 전에는 as-is 파일을 지우지 않는다.
    as-is 항목은 실행 폴더의 ``convert/asis``로 옮겨 두고, 실패하면 그대로 되돌린다.

    An entry counts as moved the moment `os.rename` returns (checked by its presence in the aside folder, so a
    failure after the rename, such as the chmod giving a read-only folder its mode back, never strands it). Each
    entry's own mode is recorded before the move and given back after the move back. `rollback` also sweeps
    whatever is still in the aside folder, and reports it as put back only when that folder is listed empty."""

    def __init__(self, root: Path, aside: Path, keep: Iterable[str] = KEEP_NAMES):
        self.root, self.aside, self.keep = Path(root), Path(aside), set(keep)
        self.pending: list[str] = []     # 아직 옮기지 않은 as-is 항목
        self.moved: list[str] = []       # 옮겨 둔 as-is 항목
        self.modes: dict[str, int] = {}  # 옮기기 전 항목의 권한
        self.asis_names: set[str] = set()  # 옮기려던 as-is 항목 전체
        self.started = False

    def move_aside(self) -> int:
        """``keep``을 뺀 최상위 항목을 모두 옮겨 두고 옮긴 개수를 반환한다.
        옮겨 둘 폴더가 이미 있고 비어 있지 않으면 아무것도 옮기기 전에 ValueError를 던진다."""
        if self.aside.exists() or self.aside.is_symlink():
            if self.aside.is_symlink() or not self.aside.is_dir() or any(self.aside.iterdir()):
                raise ValueError(f"{self.aside} exists and is not empty: the as-is tree was not moved")
        else:
            self.aside.mkdir(parents=True)
        self.pending = sorted(e.name for e in self.root.iterdir() if e.name not in self.keep)
        self.asis_names = set(self.pending)
        self.started = True
        while self.pending:
            name = self.pending[0]
            st = os.lstat(self.root / name)
            if not stat.S_ISLNK(st.st_mode):
                self.modes[name] = stat.S_IMODE(st.st_mode)
            try:
                rename_entry(self.root / name, self.aside / name)
            finally:
                # 이름이 바뀌었으면 그 뒤에 무엇이 실패했든 옮긴 항목이다
                there = self.aside / name
                if there.exists() or there.is_symlink():
                    self.moved.append(self.pending.pop(0))
        return len(self.moved)

    def move_in(self, src: Path, files: Iterable[str]) -> int:
        """생성 폴더의 파일을 프로젝트 루트로 옮긴다(``move_tree``)."""
        return move_tree(src, self.root, files)

    def _back(self, name: str) -> None:
        target = self.root / name
        try:
            rename_entry(self.aside / name, target)
        except OSError:
            # 이름이 바뀐 뒤의 실패면 권한만 아래에서 돌린다. 옮기지 못했으면 한 번 더 시도한다
            if not (target.exists() or target.is_symlink()):
                rename_entry(self.aside / name, target)
        mode = self.modes.get(name)
        if mode is not None and not target.is_symlink() and stat.S_IMODE(os.lstat(target).st_mode) != mode:
            _chmod_twice(target, mode)

    def _listing(self, folder: Path) -> list[str]:
        # 목록을 읽지 못하면 한 번 더 시도한다. 두 번째도 실패하면 OSError를 던진다
        try:
            return sorted(e.name for e in folder.iterdir())
        except OSError:
            return sorted(e.name for e in folder.iterdir())

    def rollback(self) -> tuple[str, list[str]]:
        """생성한 파일을 치우고 옮겨 둔 as-is 항목을 모두 제자리로 되돌린다. 예외를 던지지 않는다.
        (실패 내용, 옮겨 둔 폴더에 남은 항목)을 반환한다. 둘 다 비어 있어야 모두 되돌린 것이다.
        단계마다 따로 보호한다. 옮겨 둔 폴더는 마지막에 다시 읽어서 남은 항목을 반환한다."""
        if not self.started:
            return "", []
        problems: list[str] = []
        still = set(self.pending)
        try:
            names = self._listing(self.root)
        except OSError as e:
            problems.append(f"the project folder could not be listed: {type(e).__name__}: {e}")
            names = []
        for name in names:
            if name in self.keep or name in still:
                continue
            try:
                _force_remove(self.root / name)      # 생성한 트리가 이미 놓은 항목이다
            except OSError as e:
                problems.append(f"{name}: {type(e).__name__}: {e}")
        for name in reversed(list(self.moved)):
            try:
                self._back(name)
                self.moved.remove(name)
            except OSError as e:
                problems.append(f"{name}: {type(e).__name__}: {e}")
        # 목록만 믿지 않는다. 옮겨 둔 폴더에 남은 항목도 되돌린다
        try:
            rest = self._listing(self.aside) if self.aside.is_dir() else []
        except OSError as e:
            problems.append(f"{self.aside}: {type(e).__name__}: {e}")
            rest = []
        for name in rest:
            target = self.root / name
            if target.exists() or target.is_symlink():
                if name not in self.asis_names:
                    problems.append(f"{name}: the project has an entry of that name again")
                    continue
                try:
                    _force_remove(target)            # 같은 이름을 생성한 트리가 놓았다
                except OSError as e:
                    problems.append(f"{name}: {type(e).__name__}: {e}")
                    continue
            try:
                self._back(name)
            except OSError as e:
                problems.append(f"{name}: {type(e).__name__}: {e}")
        left: list[str] = []
        try:
            left = self._listing(self.aside) if self.aside.exists() else []
        except OSError as e:
            problems.append(f"{self.aside}: {type(e).__name__}: {e}")
            left = ["(the folder could not be listed)"]
        if not left and self.aside.is_dir():
            try:
                self.aside.rmdir()
            except OSError as e:
                problems.append(f"{self.aside}: {type(e).__name__}: {e}")
        return "; ".join(dict.fromkeys(problems))[:500], left

    def finish(self) -> None:
        """전환 커밋이 생긴 뒤 옮겨 둔 as-is 항목을 지운다. 실패하면 OSError를 던진다."""
        if self.aside.exists():
            _force_remove(self.aside)


def generated_paths(src: Path, keep: Iterable[str] = KEEP_NAMES) -> tuple[list[str], list[str]]:
    """생성 폴더의 일반 파일 경로 목록과 건너뛸 심볼릭 링크 목록을 정렬해서 반환한다. 링크는 따라가지 않는다.
    경로가 폴더 밖을 가리키거나 남기는 이름(``.git`` 등)으로 시작하면 ValueError를 던진다."""
    kept = set(keep)
    files: list[str] = []
    links: list[str] = []
    base = Path(src)
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = PurePosixPath(Path(dirpath).relative_to(base).as_posix())
        for d in list(dirnames):
            if (Path(dirpath) / d).is_symlink():
                links.append(str(rel_dir / d) if str(rel_dir) != "." else d)
                dirnames.remove(d)
        for f in filenames:
            rel = (rel_dir / f).as_posix() if str(rel_dir) != "." else f
            if (Path(dirpath) / f).is_symlink():
                links.append(rel)
                continue
            parts = PurePosixPath(rel).parts
            if not parts or ".." in parts or PurePosixPath(rel).is_absolute() or parts[0] in kept:
                raise ValueError(f"generated path not allowed in the project: {rel}")
            files.append(rel)
    return sorted(files), sorted(links)


def move_tree(src: Path, dst: Path, files: Iterable[str]) -> int:
    """생성 폴더의 파일을 프로젝트 루트의 같은 경로로 옮기고 옮긴 파일 수를 반환한다. 권한 비트는 유지한다.
    옮길 자리에 이미 무엇이 있으면 ValueError를 던진다."""
    dst_root = Path(dst).resolve()
    n = 0
    for rel in files:
        target = dst_root / rel
        # 프로젝트 밖으로 나가는 경로는 옮기지 않는다
        if dst_root not in target.parent.resolve().parents and target.parent.resolve() != dst_root:
            raise ValueError(f"generated path leaves the project: {rel}")
        if target.exists() or target.is_symlink():
            raise ValueError(f"generated path already exists in the project: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(Path(src) / rel), str(target))
        n += 1
    return n


def remove_empty_dirs(root: Path) -> None:
    """폴더 안의 빈 폴더를 아래에서부터 지우고, 비었으면 그 폴더도 지운다. 실패해도 계속 진행한다."""
    base = Path(root)
    if not base.is_dir() or base.is_symlink():
        return
    for dirpath, _dirs, _files in os.walk(base, topdown=False, followlinks=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def queue_dicts(result: Any, with_excerpt: bool) -> list[dict[str, str]]:
    """전환 결과의 확인 항목을 dict 목록으로 반환한다. ``with_excerpt``가 False면 원본 발췌와 오류 문장을 뺀다."""
    out: list[dict[str, str]] = []
    for q in list(getattr(result, "queue", None) or []):
        try:
            d = asdict(q)
        except TypeError:
            d = {k: getattr(q, k, "") for k in ("component", "reason", "source_path", "last_error", "excerpt")}
        d = {k: str(v if v is not None else "") for k, v in d.items()}
        if not with_excerpt:
            d = {k: d.get(k, "") for k in ("component", "reason", "source_path")}
        out.append(d)
    return out


def scrub_text(text: str, cfg: Any = None) -> str:
    """원본에서 가져온 문자열의 비밀 값을 가려서 반환한다. 예외를 던지지 않는다.
    비밀번호 같은 키에 붙은 값을 가리고(``sbmig.kb.redact``), 실행 환경의 비밀 값도 가린다(``sbmig.core.scrub``).
    가리다가 실패하면 문자열 대신 정해진 문구를 반환한다."""
    from sbmig.core import scrub
    try:
        from sbmig.kb.redact import redact
        out = redact(str(text or ""), cfg)
        out = scrub.scrub(out if out is not None else "")
        return out
    except Exception:  # noqa: BLE001 - 가리지 못한 원문은 내보내지 않는다
        return scrub.WITHHELD


# 발췌에서만 쓰는 넓은 가리기. 이 단어가 있는 줄의 문자열과 XML 텍스트를 모두 가린다
_SECRET_WORD_RE = re.compile(r"(?i)pass|pwd|secret|credential|token|apikey|api_key|_key|비밀번호|암호|인증키|패스워드")
_QUOTED_RE = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'")
_XML_TEXT_RE = re.compile(r">([^<>\n]*\S[^<>\n]*)<")
_NAME_ATTR_RE = re.compile(r"(?i)\bname\s*=\s*([\"'])([^\"'\n]*)\1")
_VALUE_EL_RE = re.compile(r"(?i)(<value\b[^>]*>)([^<]*)(</value>)")
_TEXT_BLOCK = '"""'
_REACH = 2                                       # 단어가 있는 줄 다음으로 가리는 줄 수


def _mask_line(line: str) -> str:
    out = _QUOTED_RE.sub(lambda m: m.group(0)[0] + "***" + m.group(0)[0], line)
    return _XML_TEXT_RE.sub(">***<", out)


def blunt_mask(text: str) -> str:
    """비밀 값을 담을 만한 줄을 넓게 가려서 반환한다. 정확하지 않아도 된다(발췌는 볼 곳을 알려 주는 용도다).
    - ``pass``, ``token``, ``_key``, ``비밀번호`` 같은 단어가 있는 줄과 그 다음 두 줄은 따옴표 문자열과 XML 텍스트를 ``***``로 바꾼다
    - 그런 줄에서 자바 텍스트 블록(따옴표 세 개)이 열리면 닫힐 때까지 모두 가린다
    - ``name=`` 속성 값에 그런 단어가 있으면 그 줄과 다음 두 줄의 ``<value>`` 내용도 가린다"""
    lines = str(text or "").split("\n")
    mask_until = value_until = -1
    in_block = False
    for i, line in enumerate(lines):
        if in_block:
            # 텍스트 블록 안이다. 닫는 따옴표 앞까지 가린다
            close = line.find(_TEXT_BLOCK)
            if close < 0:
                lines[i] = (line[: len(line) - len(line.lstrip())] + "***") if line.strip() else line
                continue
            lead = line[:close]
            lines[i] = ((lead[: len(lead) - len(lead.lstrip())] + "***") if lead.strip() else lead) + line[close:]
            in_block = False
            continue
        named = any(_SECRET_WORD_RE.search(m.group(2)) for m in _NAME_ATTR_RE.finditer(line))
        if named:
            value_until = max(value_until, i + _REACH)
        trigger = bool(_SECRET_WORD_RE.search(line))
        if trigger:
            mask_until = max(mask_until, i + _REACH)
        out = line
        if trigger and line.count(_TEXT_BLOCK) % 2 == 1:
            head, _, tail = line.partition(_TEXT_BLOCK)
            out, in_block = _mask_line(head) + _TEXT_BLOCK + ("***" if tail.strip() else tail), True
        elif i <= mask_until:
            out = _mask_line(line)
        if i <= value_until:
            out = _VALUE_EL_RE.sub(lambda m: m.group(1) + ("***" if m.group(2).strip() else m.group(2)) + m.group(3),
                                   out)
        lines[i] = out
    return "\n".join(lines)


def scrub_excerpt(text: str, cfg: Any = None) -> str:
    """원본 발췌의 비밀 값을 가려서 반환한다. ``scrub_text``에 넓은 가리기(``blunt_mask``)를 더한다.
    예외를 던지지 않는다. 실패하면 정해진 문구를 반환한다."""
    from sbmig.core import scrub
    try:
        return scrub_text(blunt_mask(str(text or "")), cfg)
    except Exception:  # noqa: BLE001 - 가리지 못한 원문은 내보내지 않는다
        return scrub.WITHHELD


# 원본 소스의 줄을 담을 수 있는 필드. convert/queue.json에만 쓰고 발췌처럼 가린다
SOURCE_FIELDS = ("excerpt", "last_error")


def safe_queue(result: Any, cfg: Any = None) -> list[dict[str, str]]:
    """convert/queue.json에 쓸 확인 항목 목록을 반환한다. 발췌를 포함한 모든 값의 비밀 값을 가린다.

    The only place an excerpt of a source file leaves the converter: report.json and the journal carry none.
    `excerpt` and `last_error` get the blunt layer too (`scrub_excerpt`); the other fields `scrub_text` only."""
    return [{k: (scrub_excerpt(v, cfg) if k in SOURCE_FIELDS else scrub_text(v, cfg)) for k, v in d.items()}
            for d in queue_dicts(result, with_excerpt=True)]


def datasource_text(result: Any) -> str:
    """전환 결과의 DB 연결 정보를 ``configured:<벤더>``, ``not_found`` 같은 한 단어로 반환한다."""
    ds = getattr(result, "datasource", None)
    if ds is None:
        return "not_found"
    if getattr(ds, "configured", False):
        return f"configured:{getattr(ds, 'vendor', '') or 'unknown'}"
    return str(getattr(ds, "reason", "") or "not_found")


def result_problem(result: Any) -> str:
    """전환 결과를 쓸 수 없으면 그 이유를 한 문장으로 반환한다. 쓸 수 있으면 빈 문자열을 반환한다.
    - 출력 폴더를 거부해서 아무것도 쓰지 않았으면 실패다
    - 전환은 했지만 pom을 쓰지 못했으면 실패다. 생성한 트리를 빌드할 수 없다

    Pipeline wording (sbmig/fwconvert/pipeline.py `_out_root_problem`, skeleton.py `check_options`): "<out>: the output
    folder …" and "skeleton: … no pom written". `converted=True` with any "no pom written" error is a failed
    conversion, not a converted run."""
    errors = [str(e) for e in getattr(result, "errors", None) or []]
    if not getattr(result, "converted", False):
        hit = next((e for e in errors if ": the output folder " in e or "cannot check the output folder" in e), "")
        return f"the output folder was refused ({hit})" if hit else ""
    hit = next((e for e in errors if "no pom written" in e), "")
    return f"the generated project has no poms ({hit})" if hit else ""


def conversion_info(result: Any, framework: str, commit: str = "") -> ConversionInfo:
    """전환 결과로 report.json의 ``conversion``을 만든다. 확인 항목은 발췌 없이 ``QUEUE_CAP``개까지 넣는다."""
    ident = getattr(result, "identity", None)
    mods = getattr(ident, "modules", None)
    queue = queue_dicts(result, with_excerpt=False)
    counts = {str(k): int(v) for k, v in dict(getattr(result, "counts", None) or {}).items()
              if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return ConversionInfo(
        converted=bool(getattr(result, "converted", False)), framework=framework,
        project=str(getattr(ident, "project_name", "") or ""), package=str(getattr(ident, "package_base", "") or ""),
        modules=[str(m) for m in ((mods.boot, mods.domain, mods.service, mods.store) if mods is not None else ())],
        commit=commit, counts=counts, datasource=datasource_text(result) if getattr(result, "converted", False) else "",
        queue=queue[:QUEUE_CAP], queue_total=len(queue),
        unexposed=[str(u) for u in getattr(result, "unexposed", None) or []],
        skipped=[str(s) for s in getattr(result, "skipped", None) or []],
        errors=[str(e) for e in getattr(result, "errors", None) or []])
