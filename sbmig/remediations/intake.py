"""취약점 조치의 접수 단계를 처리하는 모듈. 업로드된 ZIP 검사, 원본 보관, manifest, 작업 트리(accepted/),
서버의 Maven 실행 환경 검사, 결과 ZIP 확정을 맡는다.
- ZIP은 풀기 전에 전부 검사한다(경로 탈출, 절대 경로, 심볼릭 링크와 특수 파일, 암호화,
  중복과 정규화 충돌, 항목 수, 크기, 압축률). 선언된 크기는 거짓일 수 있으므로 푸는
  동안에도 실제 바이트로 다시 확인하고, 어기면 푼 트리를 지우고 거부한다
- 원본 ZIP은 ``original/source.zip``에 읽기 전용으로 두고 다시 쓰지 않는다
- 이후의 모든 해시, 복사, 결과 ZIP은 manifest의 ``paths``를 기준으로 한다.
  업로드한 ZIP에 들어 있던 빌드 산출물(pom.xml 옆의 ``target/``)은 manifest와 accepted/에서 뺀다.
  원본 ZIP에는 그대로 남는다
- 프로젝트 안의 어떤 것도 실행하지 않는다
- ``__MACOSX/``와 ``.DS_Store``는 일반 항목으로 다루고, 최상위 폴더 하나로 감싸였는지 판단할 때만 세지 않는다
- 압축률 검사는 풀린 크기가 ``RATIO_MIN_BYTES``(1 MiB) 이상인 항목에만 한다.
  빈 폴더는 manifest에 없으므로 결과 ZIP에도 없다

Intake of the remediation feature: validates an uploaded ZIP before extracting anything, preserves the original read-only,
writes the manifest and the working tree, checks the server's Maven boundary and freezes the deterministic delivery ZIP.
`__MACOSX/` and `.DS_Store` are ordinary entries (kept everywhere) and only ignored when deciding "single top-level
directory". The compression-ratio check applies to entries of at least 1 MiB unpacked. Build output carried by the
upload (`target/` beside a `pom.xml`) is validated like every entry but left out of `accepted/` and the manifest
(v2.1z-bh); `original/source.zip` keeps it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .models import (BoundaryFinding, DeliveryInfo, IntakeLimits, IntakeRejected, IntakeResult, ManifestEntry,
                     RemediationError, RunPaths, StateConflict, canonical_json, read_json, sha256_file, sha256_text,
                     write_json_atomic)

log = logging.getLogger("sbmig.remediations.intake")

MIB = 1024 * 1024
RATIO_MIN_BYTES = 1 * MIB                 # 이보다 작은 항목은 압축률을 확인하지 않는다
NOISE_TOP = ("__MACOSX", ".DS_Store")     # 최상위 폴더를 판단할 때만 무시하는 이름
FIXED_DATE = (1980, 1, 1, 0, 0, 0)        # 결과 ZIP 내용이 매번 같도록 고정하는 시각
_CHUNK = 1 << 20
_SUPPORTED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA)
_DRIVE = re.compile(r"^[A-Za-z]:")
_PROP = re.compile(r"\$\{([^}]+)\}")


# ---------------------------------------------------------------------------- 한도
def limits_from_config(cfg: Any) -> IntakeLimits:
    """설정 ``remediation.max_*``(MiB 단위)를 바이트 단위의 ``IntakeLimits``로 바꾼다.

    Build the intake limits from `cfg.remediation` (MiB → bytes)."""
    r = cfg.remediation
    return IntakeLimits(max_zip_bytes=int(r.max_upload_mb) * MIB, max_entries=int(r.max_entries),
                        max_unpacked_bytes=int(r.max_unpacked_mb) * MIB, max_entry_bytes=int(r.max_entry_mb) * MIB,
                        max_compression_ratio=int(r.max_compression_ratio))


# ---------------------------------------------------------------------------- 경로 규칙
def _reject(message: str, code: str, **details: Any) -> IntakeRejected:
    return IntakeRejected(message, code=code, **details)


def _safe_display(name: str) -> str:
    # 로그 위조를 막기 위해 오류 메시지에 제어 문자를 그대로 넣지 않는다
    return name.encode("unicode_escape", "backslashreplace").decode("ascii", "replace")[:300]


def _check_name(raw: str) -> tuple[str, bool]:
    """ZIP 항목 이름 하나를 검사하고 (슬래시로 구분한 경로, 폴더 여부)를 반환한다.
    규칙을 어기면 IntakeRejected를 던진다."""
    shown = _safe_display(raw)
    if not raw:
        raise _reject("empty entry name", "path_traversal")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise _reject(f"control character in entry name: {shown}", "path_traversal", entry=shown)
    if raw.startswith(("/", "\\")) or _DRIVE.match(raw):
        raise _reject(f"absolute path in ZIP: {shown}", "absolute_path", entry=shown)
    if "\\" in raw:
        # 백슬래시는 Windows에서 폴더 구분자이므로 "a\..\x" 같은 경로 탈출을 막기 위해 거부한다
        raise _reject(f"backslash in entry name: {shown}", "path_traversal", entry=shown)
    is_dir = raw.endswith("/")
    path = raw[:-1] if is_dir else raw
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            raise _reject(f"unsafe path segment in ZIP: {shown}", "path_traversal", entry=shown)
        if not seg.rstrip(" ."):
            raise _reject(f"segment made of dots / spaces only: {shown}", "path_traversal", entry=shown)
    return path, is_dir


def _norm_segment(seg: str) -> str:
    # Windows / macOS 파일 시스템에서 같은 이름이 되는 경로를 한 키로 만든다.
    # NFC, 대소문자, 끝의 점과 공백을 무시한다
    s = unicodedata.normalize("NFC", seg)
    s = unicodedata.normalize("NFC", s.casefold())
    return s.rstrip(" .")


def _norm_key(path: str) -> str:
    return "/".join(_norm_segment(s) for s in path.split("/"))


def _safe_rel(path: str) -> str:
    """manifest 경로 하나가 안전한 상대 경로인지 검사하고 그 경로를 반환한다.
    안전하지 않거나 폴더 경로면 예외를 던진다."""
    p, is_dir = _check_name(path)
    if is_dir:
        raise RemediationError(f"directory path in file list: {_safe_display(path)}", code="path_traversal")
    return p


# ---------------------------------------------------------------------------- 접수
def _entry_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _validate(zf: zipfile.ZipFile, limits: IntakeLimits,
              build_targets: list[str] | None = None) -> tuple[list[tuple[zipfile.ZipInfo, str]], list[str], str]:
    """ZIP을 풀기 전에 모든 항목을 검사하고 (파일 항목과 경로, 폴더 경로, project_root)를 반환한다."""
    infos = zf.infolist()
    if not infos:
        raise _reject("the ZIP has no entries", "empty")
    if len(infos) > limits.max_entries:
        raise _reject(f"too many entries: {len(infos)} > {limits.max_entries}", "too_many_entries")

    files: list[tuple[zipfile.ZipInfo, str]] = []
    dirs: list[str] = []
    seen: set[str] = set()
    total = 0
    for info in infos:
        path, is_dir = _check_name(info.orig_filename)
        shown = _safe_display(info.orig_filename)
        if info.flag_bits & 0x1:
            raise _reject(f"encrypted entry: {shown}", "encrypted", entry=shown)
        mode = _entry_mode(info)
        ftype = stat.S_IFMT(mode)
        if ftype == stat.S_IFLNK:
            raise _reject(f"symbolic link in ZIP: {shown}", "symlink", entry=shown)
        if ftype not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise _reject(f"special file in ZIP: {shown}", "special_file", entry=shown)
        if ftype == stat.S_IFDIR and not is_dir:
            raise _reject(f"directory mode on a file entry: {shown}", "special_file", entry=shown)
        # 완전히 같은 이름만 duplicate_entry로 본다. "x" 파일과 "x/" 폴더는 아래 정규화 충돌 검사에서 잡는다
        if info.orig_filename in seen:
            raise _reject(f"duplicate entry: {shown}", "duplicate_entry", entry=shown)
        seen.add(info.orig_filename)
        if is_dir:
            dirs.append(path)
            continue
        if info.compress_type not in _SUPPORTED_COMPRESSION:
            raise _reject(f"unsupported compression method {info.compress_type}: {shown}", "not_zip", entry=shown)
        if info.file_size > limits.max_entry_bytes:
            raise _reject(f"entry too large: {shown} ({info.file_size} bytes)", "too_large", entry=shown)
        if info.file_size >= RATIO_MIN_BYTES and (
                info.compress_size <= 0 or info.file_size / info.compress_size > limits.max_compression_ratio):
            raise _reject(f"compression ratio over {limits.max_compression_ratio}: {shown}", "compression_ratio",
                          entry=shown)
        total += info.file_size
        if total > limits.max_unpacked_bytes:
            raise _reject(f"declared unpacked size over {limits.max_unpacked_bytes} bytes", "too_large")
        files.append((info, path))

    _check_collisions([p for _, p in files], dirs)
    if not files:
        raise _reject("the ZIP has no files", "empty")
    return files, dirs, _project_root([p for _, p in files], dirs, build_targets)


def _check_collisions(files: list[str], dirs: list[str]) -> None:
    """정규화(NFC, 대소문자, 끝의 점과 공백) 뒤에 겹치는 이름, 파일과 이름이
    같은 폴더, 파일을 부모 폴더로 쓰는 항목을 거부한다."""
    file_keys: dict[str, str] = {}
    for p in files:
        k = _norm_key(p)
        if k in file_keys:
            raise _reject(f"names collide after normalization: {_safe_display(file_keys[k])} / {_safe_display(p)}",
                          "normalized_collision")
        file_keys[k] = p
    dir_keys: dict[str, str] = {}
    dset = set(dirs)
    for p in [*dirs, *files]:
        segs = p.split("/")
        prefixes = ["/".join(segs[:i]) for i in range(1, len(segs))]
        if p in dset:
            prefixes.append(p)
        for d in prefixes:
            k = _norm_key(d)
            prev = dir_keys.setdefault(k, d)
            if prev != d:
                raise _reject(f"directory names collide after normalization: {_safe_display(prev)} / {_safe_display(d)}",
                              "normalized_collision")
            if k in file_keys:
                raise _reject(f"a file and a directory share a name: {_safe_display(file_keys[k])}", "normalized_collision")


def _project_root(files: list[str], dirs: list[str], build_targets: list[str] | None = None) -> str:
    if "pom.xml" in files:
        return "."
    # 루트 aggregator 없이 서비스마다 빌드한 이관 트리는, 모든 빌드 대상 폴더에 pom.xml이 있으면 ZIP
    # 최상위를 프로젝트 루트로 본다
    targets = [t.strip("/") for t in (build_targets or []) if t.strip("/") not in ("", ".")]
    if targets and all(f"{t}/pom.xml" in files for t in targets):
        return "."
    tops = {p.split("/")[0] for p in [*files, *dirs]} - set(NOISE_TOP)
    if len(tops) == 1:
        top = next(iter(tops))
        if top not in files and f"{top}/pom.xml" in files:
            return top
    raise _reject("no Maven pom.xml at the ZIP root or in its single top-level directory", "no_pom")


BUILD_OUTPUT_DIR = "target"
_MAX_NOTE_FOLDERS = 5


def build_output_paths(files: list[str]) -> list[str]:
    """``files`` 중 Maven 빌드 산출물 폴더 아래에 있는 경로를 정렬해서 반환한다.
    경로의 한 단계가 ``target``이고 그 폴더를 담은 폴더에 ``pom.xml``이 있으면 산출물이며, 가장 바깥 단계로 판단한다.
    그 앞 단계에 ``src``가 있으면 산출물로 보지 않는다(``core.workspace.is_build_output_path``와 같은 규칙).

    The paths under a Maven build-output folder: a segment exactly `target` whose holding folder has a `pom.xml` in
    `files` and no `src` segment before it (the outermost such segment decides; the rule of
    `core.workspace.is_build_output_path`, so `src/test/resources/proj/target/…` stays content). A `pom.xml` inside
    build output never makes another folder a module."""
    fileset = set(files)
    return sorted(p for p in fileset if _build_output_dir(p, fileset) is not None)


def _build_output_dir(path: str, fileset: set[str]) -> str | None:
    # 바깥 단계부터 보므로 target 안의 pom.xml은 판단에 쓰이지 않는다. src 아래의 target은 소스나 테스트 자료다
    segs = path.split("/")
    for i, seg in enumerate(segs[:-1]):
        if seg == "src":
            return None
        if seg == BUILD_OUTPUT_DIR:
            holder = "/".join(segs[:i])
            if (f"{holder}/pom.xml" if holder else "pom.xml") in fileset:
                return "/".join(segs[:i + 1])
    return None


def _build_output_note(left_out: list[str], files: list[str]) -> str:
    """뺀 빌드 산출물의 개수와 폴더를 한 줄로 만든다. 폴더는 5개까지 쓰고 나머지는 개수만 쓴다."""
    fileset = set(files)
    folders = sorted({d for p in left_out if (d := _build_output_dir(p, fileset)) is not None})
    shown = ", ".join(folders[:_MAX_NOTE_FOLDERS])
    if len(folders) > _MAX_NOTE_FOLDERS:
        shown += f" +{len(folders) - _MAX_NOTE_FOLDERS} more"
    return f"build output left out: {len(left_out)} file(s) under {shown}"


def _extract(zf: zipfile.ZipFile, files: list[tuple[zipfile.ZipInfo, str]], dirs: list[str], dest: Path,
             limits: IntakeLimits) -> list[ManifestEntry]:
    """검사를 통과한 항목을 ``dest``에 일반 파일로만 푼다.
    선언된 크기는 거짓일 수 있으므로 실제 바이트로 한도를 다시 확인한다."""
    dest.mkdir(parents=True, exist_ok=False)
    root = dest.resolve()
    for d in dirs:
        (dest / d).mkdir(parents=True, exist_ok=True)
    entries: list[ManifestEntry] = []
    total = 0
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for info, path in files:
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(root):        # 이름 검사를 통과했어도 경로가 밖으로 나가지 않는지 한 번 더 확인한다
            raise _reject(f"entry escapes the tree: {_safe_display(path)}", "path_traversal")
        # 실행 비트만 반영하고 setuid / setgid / sticky / 쓰기 권한 같은 ZIP의 다른 비트는 무시한다
        mode = 0o755 if _entry_mode(info) & 0o111 else 0o644
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_BINARY", 0), 0o600)
        h = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if size > limits.max_entry_bytes:
                        raise _reject(f"entry too large while extracting: {_safe_display(path)}", "too_large")
                    if total > limits.max_unpacked_bytes:
                        raise _reject("unpacked size over the limit while extracting", "too_large")
                    if size >= RATIO_MIN_BYTES and (info.compress_size <= 0
                                                    or size / info.compress_size > limits.max_compression_ratio):
                        raise _reject(f"compression ratio over the limit while extracting: {_safe_display(path)}",
                                      "compression_ratio")
                    h.update(chunk)
                    out.write(chunk)
        except IntakeRejected:
            raise
        except (zipfile.BadZipFile, EOFError, zlib.error, NotImplementedError, RuntimeError, ValueError) as e:
            raise _reject(f"corrupt or unreadable entry {_safe_display(path)}: {type(e).__name__}", "not_zip") from None
        os.chmod(target, mode)
        entries.append(ManifestEntry(path=path, size=size, sha256=h.hexdigest()))
    return sorted(entries, key=lambda e: e.path)


def _make_writable_and_remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        try:
            os.chmod(path, 0o644)          # Windows는 읽기 전용 파일을 지우지 못하므로 권한을 먼저 바꾼다
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass


def accept_zip(zip_path: Path, run_dir: Path, limits: IntakeLimits, *,
               build_targets: list[str] | None = None) -> IntakeResult:
    """업로드된 ZIP을 검사하고 실행 폴더에 원본(읽기 전용), manifest, accepted/ 트리를 만든다.
    규칙을 어기면 ``IntakeRejected``를 던지고 이 함수가 만든 것은 모두 지운다.
    ``zip_path``가 이미 ``original/source.zip``이면 그대로 쓰고 거부돼도 지우지 않는다.
    아니면 복사해서 쓰며, 업로드된 임시 파일은 호출한 쪽이 지운다.

    Validate the uploaded ZIP and lay out `original/source.zip` (read-only), `original/manifest.json` and `accepted/`.
    Everything this call created is removed on rejection. `build_targets` lets a tree without a root pom in (the
    migration's per-service build targets)."""
    zip_path = Path(zip_path)
    paths = RunPaths(run_dir)
    if paths.accepted.exists():
        raise StateConflict("accepted/ already exists in this run directory")
    try:
        zsize = zip_path.stat().st_size
    except OSError:
        raise _reject("uploaded file is unreadable", "not_zip") from None
    if zsize > limits.max_zip_bytes:
        raise _reject(f"ZIP too large: {zsize} > {limits.max_zip_bytes} bytes", "too_large")

    in_place = zip_path.resolve() == paths.original_zip.resolve()
    created: list[Path] = []
    partial = paths.root / ".accepted.partial"
    try:
        paths.original_zip.parent.mkdir(parents=True, exist_ok=True)
        if not in_place:
            # 검사한 바이트와 보관한 바이트가 같도록 먼저 복사하고 복사한 파일을 검사한다
            fd, tmp = tempfile.mkstemp(prefix=".source.", dir=str(paths.original_zip.parent))
            os.close(fd)
            created.append(Path(tmp))
            shutil.copyfile(zip_path, tmp)
            os.replace(tmp, paths.original_zip)
            created.remove(Path(tmp))
            created.append(paths.original_zip)
        os.chmod(paths.original_zip, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        zip_sha = sha256_file(paths.original_zip)

        try:
            zf = zipfile.ZipFile(paths.original_zip)
        except (zipfile.BadZipFile, OSError, ValueError, EOFError):
            raise _reject("not a ZIP file or unreadable central directory", "not_zip") from None
        with zf:
            files, dirs, project_root = _validate(zf, limits, build_targets)
            all_paths = [p for _, p in files]
            left_out = build_output_paths(all_paths)
            if left_out:
                # 업로드에 들어 있던 빌드 산출물은 mvn clean이 지우므로 manifest와 accepted/에 넣지 않는다
                skip = set(left_out)
                fileset = set(all_paths)
                files = [(i, p) for i, p in files if p not in skip]
                dirs = [d for d in dirs if _build_output_dir(f"{d}/-", fileset) is None]
                if not files:
                    raise _reject("the ZIP has only build output (target/ beside a pom.xml)", "empty")
            if partial.exists():
                shutil.rmtree(partial)
            created.append(partial)
            entries = _extract(zf, files, dirs, partial, limits)

        created.append(paths.manifest)
        write_json_atomic(paths.manifest, {"schema": 1, "zip_sha256": zip_sha, "entries": [e.to_dict() for e in entries]})
        os.replace(partial, paths.accepted)
        created.remove(partial)
    except BaseException:
        for p in reversed(created):
            _make_writable_and_remove(p)
        raise

    notes: list[str] = []
    noise = sorted({e.path.split("/")[0] for e in entries if e.path.split("/")[0] in NOISE_TOP})
    if noise and project_root != ".":
        notes.append(f"macOS metadata kept beside the project folder: {', '.join(noise)}")
    if left_out:
        note = _build_output_note(left_out, all_paths)
        notes.append(note)
        log.info("intake: %s", note)
    file_paths = [e.path for e in entries]
    result = IntakeResult(original_zip=paths.rel(paths.original_zip), zip_sha256=zip_sha, manifest=paths.rel(paths.manifest),
                          accepted_dir=paths.rel(paths.accepted), project_root=project_root,
                          tree_hash=tree_hash(paths.accepted, file_paths), files=len(entries),
                          unpacked_bytes=sum(e.size for e in entries), notes=notes,
                          build_output_files=len(left_out))
    log.info("intake accepted: %d files, %d bytes, project_root=%s, zip_sha256=%s", result.files, result.unpacked_bytes,
             project_root, zip_sha)
    return result


# ---------------------------------------------------------------------------- 트리 해시 / 복사
def manifest_paths(manifest: Path) -> list[str]:
    """manifest.json에 있는 파일 경로 목록을 정렬해서 반환한다.
    읽을 수 없으면 ``RemediationError``를 던진다.

    The sorted file paths of `original/manifest.json`."""
    data = read_json(Path(manifest))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise RemediationError(f"manifest unreadable: {Path(manifest).name}")
    return sorted(str(e["path"]) for e in data["entries"] if isinstance(e, dict) and "path" in e)


def _file_sha(path: Path) -> str:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "MISSING"
    if not stat.S_ISREG(st.st_mode):
        return "NOT_REGULAR"            # 심볼릭 링크나 폴더는 따라가지 않는다
    return sha256_file(path)


def tree_hash(tree: Path, paths: list[str]) -> str:
    """``paths``에 있는 파일만으로 트리 해시를 계산한다.
    정렬한 ``[경로, 파일 sha256]`` 쌍을 정규 JSON으로 만들어 sha256을 구한다.
    없는 파일은 ``"MISSING"``, 일반 파일이 아니면 ``"NOT_REGULAR"``로 넣는다.
    목록 밖의 파일(target/ 등)은 해시에 영향을 주지 않는다.

    Tree hash over exactly `paths`; files outside the list never matter."""
    tree = Path(tree)
    pairs = [[p, _file_sha(tree / p)] for p in sorted(set(paths))]
    return sha256_text(canonical_json(pairs))


def _no_symlink_on_path(base: Path, rel: str) -> Path:
    """``base/rel``까지의 모든 단계가 링크가 아닌지 확인하고 그 경로를 반환한다. 링크는 따라가지 않는다."""
    cur = base
    for seg in rel.split("/"):
        cur = cur / seg
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise RemediationError(f"file listed in the manifest is missing: {_safe_display(rel)}",
                                   code="missing_file") from None
        if stat.S_ISLNK(st.st_mode):
            raise RemediationError(f"symbolic link in the tree: {_safe_display(rel)}", code="symlink")
    if not stat.S_ISREG(os.lstat(cur).st_mode):
        raise RemediationError(f"not a regular file: {_safe_display(rel)}", code="special_file")
    return cur


def _real_dirs_to(base: Path, rel: str) -> Path:
    """``base`` 아래 ``rel``의 부모 폴더를 링크를 따라가지 않고 만들고 그 경로를 반환한다.
    중간 단계가 링크이거나 폴더가 아니면 예외를 던진다."""
    cur = base
    for seg in rel.split("/")[:-1]:
        cur = cur / seg
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur)
            continue
        if stat.S_ISLNK(st.st_mode):
            raise RemediationError(f"symbolic link in the tree: {_safe_display(rel)}", code="symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise RemediationError(f"not a directory on the path of {_safe_display(rel)}", code="special_file")
    return cur


def restore_missing(tree: Path, source: Path, paths: list[str]) -> list[str]:
    """``paths`` 가운데 ``tree``에 없는 파일을 ``source``에서 복사해 되살리고, 되살린 경로를 정렬해서 반환한다.
    이미 있는 경로는 덮어쓰지 않고, 양쪽 모두 없는 경로는 건너뛴다. 권한은 실행 비트만 옮기고 링크는 따라가지 않는다.

    Copy back from `source` (normally `accepted/`) every path of `paths` that no longer exists in `tree` (lstat),
    bytes and exec bit only; returns the restored paths, sorted. A path that exists in `tree` in any form is never
    touched; a path missing in both is skipped and not returned. Symlinks on either side are refused like
    `copy_tree` (`RemediationError(code="symlink")`), never followed or created."""
    tree, source = Path(tree), Path(source)
    restored: list[str] = []
    for rel in sorted(set(paths)):
        rel = _safe_rel(rel)
        target = tree / rel
        try:
            os.lstat(target)
            continue                          # 링크든 폴더든 무엇이 있으면 손대지 않는다
        except FileNotFoundError:
            pass
        except NotADirectoryError:
            raise RemediationError(f"not a directory on the path of {_safe_display(rel)}", code="special_file") from None
        try:
            src_file = _no_symlink_on_path(source, rel)
        except RemediationError as e:
            if e.code == "missing_file":
                continue
            raise
        parent = _real_dirs_to(tree, rel)
        out = parent / rel.split("/")[-1]
        mode = 0o755 if os.lstat(src_file).st_mode & 0o111 else 0o644
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as dst, open(src_file, "rb") as fh:
            shutil.copyfileobj(fh, dst, _CHUNK)
        os.chmod(out, mode)
        restored.append(rel)
    return restored


def _same_bytes(a: Path, b: Path, size_a: int) -> bool:
    """두 파일의 크기를 먼저 비교하고, 같을 때만 바이트를 조금씩 읽어 비교한다."""
    if os.lstat(b).st_size != size_a:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(_CHUNK), fb.read(_CHUNK)
            if x != y:
                return False
            if not x:
                return True


class SyncFailed(RemediationError):
    """manifest 경로 일부를 기대하는 바이트로 되돌리지 못했을 때 던지는 오류. 되돌린 경로와 실패한 경로를 함께 담는다.

    Raised by `sync_tree` / `sync_tree_table` after EVERY path was tried: `missing` / `changed` = what was put back,
    `failed` = `[(path, error type name, error code)]` in path order. `code` is the first failure's code (a
    `RemediationError`'s own code, e.g. `symlink`, else `sync_failed`)."""

    def __init__(self, missing: list[str], changed: list[str], failed: list[tuple[str, str, str]]):
        first = failed[0]
        super().__init__(f"{len(failed)} path(s) could not be put back, first: {first[0]}: {first[1]}",
                         code=first[2] or "sync_failed")
        self.missing, self.changed, self.failed = list(missing), list(changed), list(failed)


def _make_writable_on_path(tree: Path, rel: str) -> None:
    """``tree``에서 ``rel``까지 있는 폴더와 그 파일에 소유자 쓰기 권한을 한 번 더한다. 링크는 건드리지 않는다.

    A build can leave read-only folders or files (a plugin's output, `chmod -w`): before giving up on a path the
    existing folders from `tree` down to its parent and the file itself get the owner's write (and search) bit."""
    cur = Path(tree)
    parts = rel.split("/")
    for i, seg in enumerate(parts):
        cur = cur / seg
        try:
            st = os.lstat(cur)
        except OSError:
            return
        if stat.S_ISLNK(st.st_mode):
            return
        add = stat.S_IWUSR | (stat.S_IXUSR | stat.S_IRUSR if stat.S_ISDIR(st.st_mode) else 0)
        if i == len(parts) - 1 and not stat.S_ISREG(st.st_mode):
            return
        try:
            os.chmod(cur, stat.S_IMODE(st.st_mode) | add)
        except OSError:
            pass
    try:
        os.chmod(tree, stat.S_IMODE(os.lstat(tree).st_mode) | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR)
    except OSError:
        pass


def _write_back(tree: Path, rel: str, src: Path, mode: int, want_sha: str | None = None) -> None:
    """``src``의 바이트를 ``tree/rel``에 임시 파일과 이름 바꾸기로 쓴다. 권한 오류면 폴더와 파일을 쓸 수 있게 한 뒤 한 번 더
    시도한다. ``want_sha``가 있으면 복사한 바이트의 sha256이 그 값과 다를 때 예외를 던진다."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for attempt in (0, 1):
        try:
            parent = _real_dirs_to(tree, rel)
            out = parent / rel.split("/")[-1]
            tmp = parent / f".{out.name}.sync-tmp"
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            h = hashlib.sha256()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_BINARY", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as dst, open(src, "rb") as fh:
                    while True:
                        chunk = fh.read(_CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                        dst.write(chunk)
                if want_sha is not None and h.hexdigest() != want_sha:
                    raise RemediationError(f"the expected file of {_safe_display(rel)} is not the recorded one",
                                           code="expected_changed")
                os.chmod(tmp, mode)
                os.replace(tmp, out)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return
        except PermissionError:
            if attempt:
                raise
            _make_writable_on_path(tree, rel)


def _target_stat(tree: Path, rel: str) -> os.stat_result | None:
    """``tree/rel``의 lstat 결과를 반환한다. 없으면 None이고, 링크나 일반 파일이 아닌 것이면 예외를 던진다."""
    try:
        st = os.lstat(tree / rel)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        raise RemediationError(f"not a directory on the path of {_safe_display(rel)}", code="special_file") from None
    if stat.S_ISLNK(st.st_mode):
        raise RemediationError(f"symbolic link in the tree: {_safe_display(rel)}", code="symlink")
    if not stat.S_ISREG(st.st_mode):
        raise RemediationError(f"not a regular file: {_safe_display(rel)}", code="special_file")
    return st


def _source_stat(src: Path, rel: str) -> os.stat_result | None:
    """기대하는 파일의 lstat 결과를 반환한다. 없으면 None이고, 링크나 일반 파일이 아닌 것이면 예외를 던진다."""
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise RemediationError(f"symbolic link in the tree: {_safe_display(rel)}", code="symlink")
    if not stat.S_ISREG(st.st_mode):
        raise RemediationError(f"not a regular file: {_safe_display(rel)}", code="special_file")
    return st


def _failure(rel: str, e: BaseException) -> tuple[str, str, str]:
    return (_safe_display(rel), type(e).__name__, e.code if isinstance(e, RemediationError) else "")


def sync_tree(tree: Path, expected: Any, paths: list[str]) -> tuple[list[str], list[str]]:
    """``tree``의 manifest 파일을 기대하는 바이트(``expected(path)``가 가리키는 파일)와 맞춘다.
    없는 파일은 되살리고 바이트가 다른 파일은 덮어쓰며, (되살린 경로, 덮어쓴 경로)를 정렬해서 반환한다.
    되돌리지 못한 경로가 있어도 모든 경로를 시도한 뒤 ``SyncFailed``를 던진다.

    Bring every path of `paths` in `tree` to the bytes of the file `expected(path)` names (normally `accepted/<p>`,
    `rounds/<r>/applied/<p>` or the fix loop's `fixed/<p>`): a path missing in `tree` is copied back (as
    `restore_missing` did), a path whose bytes differ is overwritten (size first, then bytes; no hashing). The exec bit
    comes from the expected file. A path whose expected file does not exist is left alone. Symlinks on either side are
    refused (code `symlink`), never followed or created; a directory where a file belongs is `special_file`. Every
    path is tried; a write refused for permissions is tried once more after the folders on its path and the file got
    the owner's write bit. Any path that could not be put back → `SyncFailed` (with what was put back) after the last
    path. Returns `(missing, changed)`, both sorted. `sync_tree_table` is the same with a recorded size + sha256 table
    instead of reading the expected files."""
    tree = Path(tree)
    missing: list[str] = []
    changed: list[str] = []
    failed: list[tuple[str, str, str]] = []
    for raw in sorted(set(paths)):
        rel = raw
        try:
            rel = _safe_rel(raw)
            src = Path(expected(rel))
            st_src = _source_stat(src, rel)
            if st_src is None:
                continue                          # 기대하는 파일이 없으면 판단할 수 없으므로 손대지 않는다
            st_dst = _target_stat(tree, rel)
            if st_dst is not None and _same_bytes(tree / rel, src, st_dst.st_size):
                continue
            _write_back(tree, rel, src, 0o755 if st_src.st_mode & 0o111 else 0o644)
            (changed if st_dst is not None else missing).append(rel)
        except Exception as e:  # noqa: BLE001 - 모든 경로를 시도한 뒤 한꺼번에 알린다
            failed.append(_failure(rel, e))
    if failed:
        raise SyncFailed(missing, changed, failed)
    return missing, changed


def file_table(tree: Path, paths: list[str]) -> dict[str, tuple[int, str]]:
    """``paths``의 파일마다 (크기, sha256)을 담은 표를 반환한다. 트리를 한 번 읽는다. 없는 파일은 넣지 않는다.

    `{path: (size, sha256)}` of the regular files of `paths` in `tree` (one full read); a missing path or one that is
    not a regular file is left out."""
    tree = Path(tree)
    out: dict[str, tuple[int, str]] = {}
    for rel in sorted(set(paths)):
        try:
            st = os.lstat(tree / rel)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            out[rel] = (st.st_size, sha256_file(tree / rel))
    return out


def table_hash(table: dict[str, tuple[int, str]], paths: list[str]) -> str:
    """표에서 ``tree_hash``와 같은 규칙의 트리 해시를 계산한다. 표에 없는 경로는 ``"MISSING"``이다.

    The `tree_hash` of the tree the table describes, without reading any file."""
    pairs = [[p, table[p][1] if p in table else "MISSING"] for p in sorted(set(paths))]
    return sha256_text(canonical_json(pairs))


def sync_tree_table(tree: Path, expected: Any, table: dict[str, tuple[int, str]],
                    paths: list[str]) -> tuple[list[str], list[str]]:
    """``tree``의 manifest 파일을 기록된 (크기, sha256) 표와 비교해 다른 파일만 기대하는 파일에서 되돌린다.
    트리의 각 파일은 많아야 한 번 읽고, 같은 파일의 기대하는 파일은 열지 않는다.

    `sync_tree` against a recorded table (`rounds/<r>/expected.json`, plus the fix's files): each path of `tree` is
    compared by size, then by its sha256 (so each candidate file is read at most once), and only a missing or
    differing path opens `expected(path)` and is overwritten; the copied bytes must hash to the recorded sha256 (else
    code `expected_changed`). A path the table does not list falls back to `sync_tree`'s byte comparison. Same
    refusals, permission retry, `SyncFailed` after every path, and return value as `sync_tree`."""
    tree = Path(tree)
    missing: list[str] = []
    changed: list[str] = []
    failed: list[tuple[str, str, str]] = []
    for raw in sorted(set(paths)):
        rel = raw
        try:
            rel = _safe_rel(raw)
            row = table.get(rel)
            src = Path(expected(rel))
            st_dst = _target_stat(tree, rel)
            if row is None:
                st_src = _source_stat(src, rel)
                if st_src is None or (st_dst is not None and _same_bytes(tree / rel, src, st_dst.st_size)):
                    continue
                _write_back(tree, rel, src, 0o755 if st_src.st_mode & 0o111 else 0o644)
            else:
                size, sha = int(row[0]), str(row[1])
                if st_dst is not None and st_dst.st_size == size and sha256_file(tree / rel) == sha:
                    continue
                st_src = _source_stat(src, rel)
                if st_src is None:
                    raise FileNotFoundError(f"the expected file of {_safe_display(rel)} is missing")
                _write_back(tree, rel, src, 0o755 if st_src.st_mode & 0o111 else 0o644, want_sha=sha)
            (changed if st_dst is not None else missing).append(rel)
        except Exception as e:  # noqa: BLE001 - 모든 경로를 시도한 뒤 한꺼번에 알린다
            failed.append(_failure(rel, e))
    if failed:
        raise SyncFailed(missing, changed, failed)
    return missing, changed


def copy_tree(src: Path, dst: Path, paths: list[str]) -> None:
    """``src``에서 ``paths``의 파일만 새 폴더 ``dst``로 복사하고, 권한은 실행 비트만 옮긴다.
    ``dst``는 없거나 비어 있어야 한다.
    복사 중 링크를 만나면 따라가지 않고 ``RemediationError(code="symlink")``를 던진다.

    Copy exactly `paths` into a fresh `dst`; refuses symlinks, never follows them."""
    src, dst = Path(src), Path(dst)
    if dst.exists() and (dst.is_symlink() or not dst.is_dir() or any(dst.iterdir())):
        raise StateConflict(f"copy destination is not a fresh directory: {dst.name}")
    dst.mkdir(parents=True, exist_ok=True)
    for rel in sorted(set(paths)):
        rel = _safe_rel(rel)
        source = _no_symlink_on_path(src, rel)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        os.chmod(target, 0o755 if os.lstat(source).st_mode & 0o111 else 0o644)


# ---------------------------------------------------------------------------- 결과 ZIP
def freeze_delivery(tree: Path, out_zip: Path, round: int, paths: list[str]) -> DeliveryInfo:
    """``paths``의 파일만 담은 ZIP을 원자적으로 쓴다.
    경로를 정렬하고 시각(1980-01-01), 권한(0644, 실행 파일은 0755), 압축 방식(deflate)을 고정한다.
    같은 트리면 항상 같은 바이트(같은 sha256)가 나오며, 이 바이트를 그대로 IVAS에 다시 올린다.

    Deterministic delivery ZIP of exactly `paths`, written atomically; same tree → same bytes."""
    tree, out_zip = Path(tree), Path(out_zip)
    rels = [_safe_rel(p) for p in sorted(set(paths))]
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out_zip.name}.", dir=str(out_zip.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for rel in rels:
                source = _no_symlink_on_path(tree, rel)
                st = os.lstat(source)
                info = zipfile.ZipInfo(rel, date_time=FIXED_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | (0o755 if st.st_mode & 0o111 else 0o644)) << 16
                info.file_size = st.st_size
                with open(source, "rb") as f, zf.open(info, "w", force_zip64=st.st_size > 0x7FFFFFFF) as out:
                    shutil.copyfileobj(f, out, _CHUNK)
        # Windows의 fsync(_commit)는 읽기 전용 핸들을 EBADF로 거부하므로 쓰기 가능한 핸들로 연다
        # a writable handle: Windows' fsync refuses a read-only descriptor with EBADF ("r+b" never truncates)
        with open(tmp, "r+b") as f:
            os.fsync(f.fileno())
        os.replace(tmp, out_zip)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        rel_path = out_zip.resolve().relative_to(out_zip.parent.parent.resolve()).as_posix()
    except ValueError:
        rel_path = out_zip.name
    info = DeliveryInfo(path=rel_path, sha256=sha256_file(out_zip), size=out_zip.stat().st_size, files=len(rels),
                        tree_hash=tree_hash(tree, rels), round=round)
    log.info("delivery frozen: round=%d files=%d sha256=%s", round, info.files, info.sha256)
    return info


def verify_zip_sha256(path: Path, expected: str) -> bool:
    """파일이 있고 sha256이 ``expected``와 같은지 확인한다. 대소문자는 무시한다.

    True when the file exists and its sha256 equals `expected`."""
    try:
        return bool(expected) and sha256_file(Path(path)).lower() == expected.strip().lower()
    except OSError:
        return False


# ---------------------------------------------------------------------------- 서버 실행 경계
def _host_set(hosts: list[str]) -> set[str]:
    out: set[str] = set()
    for h in hosts or []:
        h = str(h).strip()
        if not h:
            continue
        if "://" in h:
            h = urlsplit(h).hostname or ""
        else:
            h = h.split("/")[0].rsplit("@", 1)[-1]
            if h.count(":") == 1:
                h = h.split(":")[0]
        if h:
            out.add(h.lower().strip("[]"))
    return out


def _localname(el: Any) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _child_text(el: Any, name: str) -> str:
    for c in el:
        if _localname(c) == name:
            return (c.text or "").strip()
    return ""


def _resolve_props(value: str, props: dict[str, str]) -> tuple[str, bool]:
    """같은 pom의 <properties>로 ``${x}``를 값으로 바꾼다. 모두 바꿨으면 (값, True)를 반환한다."""
    for _ in range(10):
        if not _PROP.search(value):
            return value, True
        changed = False

        def sub(m: re.Match) -> str:
            nonlocal changed
            if m.group(1) in props:
                changed = True
                return props[m.group(1)]
            return m.group(0)
        value = _PROP.sub(sub, value)
        if not changed:
            break
    return value, not _PROP.search(value)


def _display_url(url: str) -> str:
    # 자격 증명(user:pass@)과, 토큰이 들어갈 수 있는 쿼리 문자열은 기록하지 않는다
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparsable url>"
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}" if parts.scheme else _PROP.sub("${…}", url.split("?")[0])[:200]


def _check_pom(pom: Path, rel: str, allowed: set[str], action: str) -> list[BoundaryFinding]:
    from lxml import etree

    from .apply import PomReadError, read_pom_xml, xml_reason

    # XXE를 막기 위해 외부 엔티티, DTD, 네트워크를 읽지 않는 파서를 쓴다.
    # pom 안의 어떤 것도 실행하거나 조회하지 않는다
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, recover=False)
    try:
        # Maven은 선언과 다른 인코딩(UTF-8 선언의 MS949 등)도 받으므로 이관 쪽 규칙으로 읽은 내용을 검사한다
        root = etree.fromstring(read_pom_xml(pom), parser)
    except PomReadError as e:
        return [BoundaryFinding(code="unparsable_pom", path=rel, action="refuse", detail=str(e))]
    except (etree.XMLSyntaxError, ValueError) as e:
        return [BoundaryFinding(code="unparsable_pom", path=rel, action="refuse", detail=xml_reason(e))]
    props: dict[str, str] = {}
    for c in root:
        if _localname(c) == "properties":
            for p in c:
                name = _localname(p)
                if name:
                    props[name] = (p.text or "").strip()
    out: list[BoundaryFinding] = []
    for el in root.iter():
        name = _localname(el)
        if name not in ("repository", "pluginRepository"):
            continue
        parent = el.getparent()
        pname = _localname(parent) if parent is not None else ""
        if (name, pname) not in (("repository", "repositories"), ("pluginRepository", "pluginRepositories")):
            continue                     # <distributionManagement><repository> 등은 문제가 아니다
        code = "foreign_repository" if name == "repository" else "foreign_plugin_repository"
        rid = _child_text(el, "id")
        raw = _child_text(el, "url")
        notes = [f"id={rid}"] if rid else []
        if not raw:
            out.append(BoundaryFinding(code=code, path=rel, action=action, detail="; ".join([*notes, "no url"])))
            continue
        url, resolved = _resolve_props(raw, props)
        if not resolved:
            out.append(BoundaryFinding(code=code, path=rel, action=action,
                                       detail="; ".join([*notes, f"unresolved property in url {_display_url(url)}"])))
            continue
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            out.append(BoundaryFinding(code=code, path=rel, action=action, detail="; ".join([*notes, "unparsable url"])))
            continue
        scheme = (parts.scheme or "").lower()
        if scheme != "https":
            notes.append(f"non-https ({scheme or 'no scheme'})")
        if host and host in allowed:
            if scheme != "https":
                log.info("boundary: allowed host over non-https in %s: %s", rel, _display_url(url))
            continue
        notes.append(f"host {host or '<none>'} not approved: {_display_url(url)}")
        out.append(BoundaryFinding(code=code, path=rel, action=action, detail="; ".join(notes)))
    return out


def project_settings_detail(project: Path, cfg: Any) -> str:
    """프로젝트의 ``.mvn/maven.config``가 지정한 settings를 빌드에서 어떻게 다뤘는지 한 문장으로 반환한다.
    settings 옵션이 없거나 서버 settings가 없으면 빈 문자열을 반환한다.

    The boundary detail of `.mvn/maven.config`: whether its settings options are overridden by the server's (`-s` /
    `-gs` of `sbmig.build.mvn.base_command`), plus its offline option; "" without settings options or server settings."""
    build = getattr(cfg, "build", None)
    if build is None or not str(getattr(build, "settings_file", "") or "").strip():
        return ""
    from sbmig.build.mvn import project_maven_settings, project_settings_ignored
    try:
        found = project_maven_settings(Path(project))
    except Exception:                                    # noqa: BLE001 - a detail never stops the intake
        return ""
    if found is None or not found.names_settings:
        return ""
    if project_settings_ignored(cfg, found):
        out = f"names its own Maven settings ({found.shown()}), not used: every build used the server's settings"
    else:
        out = (f"names its own Maven settings ({found.shown()}), read by Maven as its global settings together with "
               "the server's")
    if found.offline and not getattr(build, "offline", False):
        out += f"; also turns on offline mode ({', '.join(found.offline)}): Maven downloads nothing"
    return out


MVN_NOT_USED = "not used: the build did not read the project's .mvn folder"


def project_mvn_detail(project: Path, cfg: Any, rel: str) -> Optional[str]:
    """``build.ignore_project_mvn``이면 ``.mvn`` 설정 파일의 처리 내용을 한 문장으로 반환한다. 꺼져 있으면 None을 반환한다.

    The boundary detail of a `.mvn` config file when Maven does not read the folder (`build.ignore_project_mvn`):
    "not used", plus for `maven.config` the options carried over to the command line; None with the switch off."""
    from sbmig.build.mvn import ignore_project_mvn, project_maven_settings
    if not ignore_project_mvn(cfg):
        return None
    if rel != ".mvn/maven.config":
        return MVN_NOT_USED
    try:
        found = project_maven_settings(Path(project))
    except Exception:                                    # noqa: BLE001 - a detail never stops the intake
        found = None
    carried = found.carried() if found is not None else []
    return MVN_NOT_USED + (f"; carried over to the command line: {' '.join(carried)}" if carried else "")


def check_boundary(project: Path, cfg: Any, allowed_hosts: list[str]) -> list[BoundaryFinding]:
    """프로젝트 안에서 서버의 Maven 실행 환경 제한을 우회할 수 있는 요소를 찾는다.
    아무것도 실행하지 않으며, 요소마다 처리 방법(``action``)은 설정에서 정한다.
    ``.mvn/maven.config`` · ``jvm.config`` (project_maven_config), ``.mvn/extensions.xml`` (maven_extensions) →
    ``remediation.project_maven_config``; ``mvnw`` · ``mvnw.cmd`` · ``.mvn/wrapper/*`` (maven_wrapper) → ``remediation.maven_wrapper``;
    심볼릭 링크와 읽을 수 없는 pom은 항상 refuse로 처리하고, 프로젝트 안의
    settings.xml(settings_in_project)은 ignore로 기록만 한다. 빌드에는 서버의 settings만 쓴다.
    승인되지 않은 호스트의 <repository> / <pluginRepository>는 ``remediation.foreign_repository`` 설정을 따른다.

    Find what could bypass the server's Maven boundary. Nothing from the project is ever run."""
    project = Path(project)
    r = cfg.remediation
    allowed = _host_set(list(allowed_hosts or []))
    findings: list[BoundaryFinding] = []

    def add(code: str, rel: str, action: str, detail: str = "") -> None:
        findings.append(BoundaryFinding(code=code, path=rel, action=action, detail=detail))

    poms: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(project, followlinks=False):
        base = Path(dirpath)
        reld = base.relative_to(project).as_posix()
        for name in sorted([*dirnames, *filenames]):
            full = base / name
            rel = name if reld == "." else f"{reld}/{name}"
            if full.is_symlink():
                add("symlink", rel, "refuse", "symbolic link in the project")
                continue
            if name in dirnames:
                continue
            if name in ("settings.xml", "settings-security.xml"):
                add("settings_in_project", rel, "ignore", "the server's settings.xml is the only one used")
            if name == "pom.xml":
                poms.append((full, rel))
            mvn_detail = project_mvn_detail(project, cfg, rel) if rel.startswith(".mvn/") else None
            if rel == ".mvn/maven.config":
                add("project_maven_config", rel, r.project_maven_config,
                    mvn_detail if mvn_detail is not None else project_settings_detail(project, cfg))
            elif rel == ".mvn/jvm.config":
                add("project_maven_config", rel, r.project_maven_config, mvn_detail or "")
            elif rel == ".mvn/extensions.xml":
                add("maven_extensions", rel, r.project_maven_config, mvn_detail or "")
            elif rel in ("mvnw", "mvnw.cmd") or rel.startswith(".mvn/wrapper/"):
                add("maven_wrapper", rel, r.maven_wrapper, "the server's Maven is used; the wrapper is never run")
        # 링크인 폴더로는 내려가지 않는다
        dirnames[:] = sorted(d for d in dirnames if not (base / d).is_symlink())

    for pom, rel in poms:
        findings.extend(_check_pom(pom, rel, allowed, r.foreign_repository))
    for f in findings:
        log.info("boundary: %s %s (%s) %s", f.code, f.path, f.action, f.detail)
    return findings
