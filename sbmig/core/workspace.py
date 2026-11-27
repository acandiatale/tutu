"""실행 한 번의 작업 공간을 다루는 모듈. 프로젝트 복사본(git 저장소)과 실행 디렉토리를 관리한다.

The intake's `Workspace.from_input` copies the input to `<out>/<run_id>/project`; later stages use ws.read /
write / commit / revert_to. Files are read and written back in their detected encoding; sources are never
converted to UTF-8. git never runs interactively: prompts, hooks and signing are off, every call has a
timeout.

Layout of one run (nothing else is kept):
    <out_dir>/<run_id>/
        project/            ← working copy (git repo; one commit per step/attempt)
        logs/               ← every Maven / OpenRewrite / deptree / classpath / effective-pom log (NNN-<label>.log)
                              and their work files: rewrite-N.yml, rewrite-artifact-versions.json,
                              deptree-<module>.txt, classpath-<module>.txt (+ .meta.json caches),
                              effective-depmgmt*.json
        errors.jsonl        ← the run's error list (one ErrorEntry per line), errors.md its human view
        journal.jsonl       ← append-only event log (every step, build, decision, attempt; the web stream)
        sbmig.log           ← DEBUG log, `[stage]` per line
        profile.json, plan.json, report.json, report.md, changes.patch, result.zip (the migrated project)
    (+ merge-conflicts/ when a multi-module merge dropped differing copies, web-status.json for web runs)
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from .scrub import output_scrubbing, scrub_json

log = logging.getLogger("sbmig.core.workspace")

# Job 모드에서 웹 파드가 이 파일로 실행 중단을 요청한다.
# 엔진은 노드 사이, 수정 루프는 라운드 사이에서 확인하고 최종 빌드 없이 멈춘다
STOP_REQUEST_FILE = "stop.request"
STOP_REQUEST_REASON = "stopped by user (stop.request) — the final `mvn clean package` did not run"

GIT_TIMEOUT_S = 600.0      # 작업 공간 git 명령 하나의 타임아웃(초)
# git 호출마다 넘기는 설정. 260자가 넘는 경로도 다룰 수 있게 한다.
# 전역 autocrlf 때문에 되돌릴 때 `.sh`가 CRLF로 바뀌는 문제도 막는다.
# 한글 파일 이름의 8진수 이스케이프, 다른 계정 소유 디렉토리에서의 실패도 막는다
GIT_CONFIG: tuple[str, ...] = ("commit.gpgsign=false", "tag.gpgsign=false", "core.longpaths=true",
                               "core.autocrlf=false", "core.quotepath=false", "safe.directory=*",
                               # 실행 저장소는 잠깐 쓰고 버리므로 커밋 뒤 백그라운드 repack을 끈다(NFS 볼륨)
                               "maintenance.auto=false", "gc.auto=0")
# .git/info/attributes에 넣는 값. 프로젝트의 .gitattributes보다 우선한다.
# 그래서 reset --hard가 줄바꿈, LFS, 인코딩을 바꾸지 않는다
GIT_INFO_ATTRIBUTES = "* -text -eol -filter -ident -working-tree-encoding\n"
# 빌드가 target/ 밖에 만드는 파일. 원래 프로젝트에 있던 파일이면 계속 추적하고, 아니면 무시한다
GENERATED_IGNORES: tuple[str, ...] = (".flattened-pom.xml", "**/src/main/generated/", "**/src/test/generated/",
                                      ".apt_generated/", ".apt_generated_tests/")
_CREATE_NO_WINDOW = 0x08000000


def _on_windows() -> bool:
    return os.name == "nt"


def _child_creationflags() -> int:
    """Windows에서 자식 프로세스에 숨은 콘솔을 따로 붙이는 생성 플래그를 반환한다. 다른 OS에서는 0이다.
    운영자가 누른 Ctrl+Break, Ctrl+C가 git, cmd.exe, java에 바로 전달되지 않게 한다.

    Ctrl+Break is the thread dump of `sbmig.core.diag`; sbmig decides what to kill."""
    return getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW) if _on_windows() else 0


def _as_text(x) -> str:
    """subprocess 출력을 문자열로 바꾼다.
    바이트는 콘솔 코드 페이지와 상관없이 UTF-8로 디코딩한다. 디코딩할 수 없는 바이트는 대체 문자가 된다.
    None(읽기 스레드가 죽은 경우)은 빈 문자열이다."""
    if x is None:
        return ""
    return x.decode("utf-8", errors="replace") if isinstance(x, (bytes, bytearray)) else str(x)


def _as_bytes(x) -> bytes:
    if x is None:
        return b""
    return x.encode("utf-8") if isinstance(x, str) else bytes(x)
DEFAULT_ENCODING_CANDIDATES: tuple[str, ...] = ("utf-8", "cp949", "euc-kr", "shift_jis", "latin-1")
_UTF8_BOM = b"\xef\xbb\xbf"
_ZIP_UTF8_FLAG = 0x800

# 플릿은 파일을 약 150만 개 다루므로 인코딩 감지 캐시의 크기를 제한한다
ENCODING_CACHE_MAX = 20_000     # 항목 수. 약 8MB


class _LRU:
    """여러 스레드에서 함께 쓸 수 있는 작은 LRU 맵 클래스. 인코딩 캐시는 플릿의 모든 작업 공간이 공유한다."""

    def __init__(self, maxsize: int):
        self.maxsize = maxsize
        self._d: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def __len__(self) -> int:
        return len(self._d)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


# 절대 경로별 (mtime_ns, size, candidates, encoding)
_ENCODING_CACHE = _LRU(ENCODING_CACHE_MAX)


class GitError(RuntimeError):
    """작업 공간의 git 명령이 실패하거나 타임아웃이 났을 때 던지는 예외 클래스."""


def _norm_encoding(name: str) -> str:
    import codecs
    try:
        return codecs.lookup(name).name
    except LookupError:
        return name.lower()


def _sniff_bytes(data: bytes, candidates: tuple[str, ...]) -> str:
    """바이트 데이터의 인코딩을 추정한다. UTF-8 BOM이 있으면 utf-8-sig다.
    없으면 오류 없이 디코딩되는 첫 후보를 반환한다. 맞는 후보가 없으면 utf-8이다."""
    if data.startswith(_UTF8_BOM):
        return "utf-8-sig"
    for enc in candidates:
        try:
            data.decode(enc)                 # 엄격하게 디코딩한다
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    return "utf-8"                           # 맞는 후보가 없으면 utf-8. 읽을 때 errors="replace"를 쓴다


def detect_encoding(path: str | Path, candidates: Optional[list[str] | tuple[str, ...]] = None) -> str:
    """텍스트 파일의 인코딩을 감지한다.
    BOM이 있으면 utf-8-sig, 없으면 오류 없이 디코딩되는 첫 후보를 반환한다.
    없는 파일, 빈 파일, ASCII 파일이면 utf-8이다. 결과는 경로, 수정 시각, 크기로 캐시한다.

    Default candidates: utf-8, cp949, euc-kr, shift_jis, latin-1."""
    cands = tuple(candidates) if candidates else DEFAULT_ENCODING_CANDIDATES
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return "utf-8"
    key = str(p.resolve())
    hit = _ENCODING_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size and hit[2] == cands:
        return hit[3]
    try:
        data = p.read_bytes()
    except OSError:
        return "utf-8"
    enc = _sniff_bytes(data, cands)
    _ENCODING_CACHE[key] = (st.st_mtime_ns, st.st_size, cands, enc)
    return enc


def _remember_encoding(p: Path, enc: str, cands: tuple[str, ...]) -> None:
    try:
        st = p.stat()
    except OSError:
        return
    _ENCODING_CACHE[str(p.resolve())] = (st.st_mtime_ns, st.st_size, cands, enc)


def _java_unicode_escape(text: str, enc: str) -> str:
    """대상 인코딩에 없는 문자를 Java `\\uXXXX` 이스케이프로 바꾼다.
    cp949 소스를 cp949 그대로 저장할 때 쓴다.

    \\uXXXX is valid anywhere in a .java source, comments and string literals included."""
    out: list[str] = []
    for ch in text:
        try:
            ch.encode(enc)
            out.append(ch)
        except UnicodeEncodeError:
            b = ch.encode("utf-16-be")
            out.extend(f"\\u{b[i]:02x}{b[i + 1]:02x}" for i in range(0, len(b), 2))
    return "".join(out)


def fix_zip_name(info: zipfile.ZipInfo) -> str:
    """zip 항목의 파일 이름을 복원한다. 한국어 Windows의 zip 도구가 cp949로 기록한 이름을 다시 디코딩한다.
    UTF-8 플래그가 없는 이름만 utf-8, cp949 순으로 다시 읽고, 둘 다 안 되면 원래 이름을 반환한다.

    Python decodes unflagged names as cp437; some tools wrote raw UTF-8 without the flag."""
    name = info.filename
    if info.flag_bits & _ZIP_UTF8_FLAG:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    if raw.isascii():
        return name
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def posix_rel(p: str | PurePath) -> str:
    """경로를 OS와 관계없이 슬래시(/)를 쓰는 상대 경로 문자열로 바꾼다. 경로를 비교하는 쪽이 모두 "/"를 쓴다."""
    return p.as_posix() if isinstance(p, PurePath) else str(p).replace(os.sep, "/")


def _relative_posix(p: PurePath, root: PurePath) -> Optional[str]:
    """root 기준의 posix 상대 경로를 반환한다.
    root 아래가 아니면 None을 반환한다.

    Case-insensitive on Windows."""
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


_MAVEN_OUTPUT_MARKERS = ("maven-status", "maven-archiver")      # Maven이 target/ 안에 만드는 기록 디렉토리


def _is_output_target(parent: Path, parent_parts: tuple[str, ...] | list[str], has_pom: Optional[bool] = None) -> bool:
    """`parent/target`이 Maven 빌드 산출물 디렉토리인지 판단한다.
    패키지 이름이 target인 소스를 지우지 않으려고 쓴다. src 아래면 산출물이 아니다.
    옆에 pom.xml이 있거나 안에 Maven이 만든 기록 디렉토리가 있으면 산출물로 본다.

    The bookkeeping-dir rule covers a module whose pom the restructure moved away."""
    if "src" in parent_parts:
        return False
    if has_pom is None:
        has_pom = (parent / "pom.xml").is_file()
    return has_pom or any((parent / "target" / m).is_dir() for m in _MAVEN_OUTPUT_MARKERS)


def is_build_output_path(rel: str | PurePath, root: Optional[str | Path] = None) -> bool:
    """프로젝트 상대 경로가 Maven 빌드 산출물 target/ 안에 있는지 확인한다.
    `com/x/target/` 같은 소스 패키지는 산출물로 보지 않는다.

    A `target` component counts only when no `src` comes before it and, with `root` given, its parent holds a
    pom.xml (or the dir has `maven-status/` / `maven-archiver/`)."""
    parts = [x for x in str(rel).replace("\\", "/").split("/") if x not in ("", ".")]
    for i, part in enumerate(parts):
        if part == "src":
            return False
        if part == "target" and (root is None or _is_output_target(Path(root).joinpath(*parts[:i]), parts[:i])):
            return True
    return False


_INTAKE_PATTERNS = shutil.ignore_patterns(".git", ".idea", "*.iml", ".DS_Store", "__MACOSX")


def _intake_ignore(base: Path):
    """접수에서 프로젝트를 복사할 때(copytree) 뺄 파일을 고르는 ignore 함수를 만든다.
    IDE / VCS 파일은 어디서나 빼고 `target`은 빌드 산출물일 때만 뺀다."""
    base = Path(base)

    def ignore(dirpath: str, names: list[str]) -> set[str]:
        out = set(_INTAKE_PATTERNS(dirpath, names))
        if "target" in names:
            d = Path(dirpath)
            try:
                rel_parts = d.relative_to(base).parts
            except ValueError:
                rel_parts = ()
            if (d / "target").is_dir() and _is_output_target(d, rel_parts):
                out.add("target")
        return out

    return ignore


def new_run_id(prefix: str = "run") -> str:
    """`<prefix>-YYYYmmdd-HHMMSS-xxxxxx` 형식의 새 실행 ID를 만든다. 기본 접두어는 run이다."""
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


_MIB = 1024 * 1024
RATIO_MIN_BYTES = _MIB              # 이보다 작게 풀리는 항목은 압축률을 보지 않는다(취약점 조치 접수와 같은 규칙)
_UNPACK_CHUNK = 1024 * 1024
_LIES = "the ZIP's directory understates its sizes"


class ZipRefused(ValueError):
    """올린 ZIP이 풀기 한도를 넘어 접수를 거절할 때 던지는 예외. 메시지가 보고서와 화면에 나가는 사유 문장이다."""


class ZipLimits(BaseModel):
    """올린 ZIP을 풀 때의 한도(바이트, 배수)를 담는 클래스. 0이면 그 한도는 없다.
    전체 크기, 항목 하나의 크기, 항목 하나의 압축률을 본다. 압축률은 `RATIO_MIN_BYTES` 이상 풀리는 항목만 본다.

    Same meaning as `intake.max_unpacked_mb` / `max_entry_mb` / `max_compression_ratio` and the remediation
    intake's limits."""
    max_unpacked_bytes: int = 0
    max_entry_bytes: int = 0
    max_compression_ratio: int = 0

    @classmethod
    def from_mb(cls, unpacked_mb: int, entry_mb: int, ratio: int) -> "ZipLimits":
        """MiB 단위 값으로 한도를 만든다."""
        return cls(max_unpacked_bytes=int(unpacked_mb) * _MIB, max_entry_bytes=int(entry_mb) * _MIB,
                   max_compression_ratio=int(ratio))

    @classmethod
    def from_config(cls, intake: Any = None) -> "ZipLimits":
        """``intake`` 설정(``IntakeConfig``)의 세 값으로 한도를 만든다. None이면 그 기본값을 쓴다."""
        if intake is None:
            from .config import IntakeConfig
            intake = IntakeConfig()
        return cls.from_mb(getattr(intake, "max_unpacked_mb", 0) or 0, getattr(intake, "max_entry_mb", 0) or 0,
                           getattr(intake, "max_compression_ratio", 0) or 0)


def _mib(n: int) -> str:
    return f"{n / _MIB:.0f}" if n >= _MIB else f"{n / _MIB:.1f}"


def _shown(name: str) -> str:
    # 사유 문장에 들어가는 항목 이름에서 제어 문자를 빼고 길이를 줄인다
    clean = "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in name)
    return clean if len(clean) <= 200 else clean[:197] + "..."


def _check_declared(plan: list[tuple[zipfile.ZipInfo, Path, str]], limits: ZipLimits) -> None:
    # 아무것도 쓰기 전에 ZIP 디렉토리에 기록된 크기로 한도를 먼저 확인한다
    total = 0
    for member, _target, name in plan:
        if member.is_dir():
            continue
        size = int(member.file_size)
        if limits.max_entry_bytes and size > limits.max_entry_bytes:
            raise ZipRefused(f"ZIP refused: the entry {_shown(name)} unpacks to {_mib(size)} MiB, over the limit of "
                             f"{_mib(limits.max_entry_bytes)} MiB (intake.max_entry_mb) — nothing was unpacked")
        if limits.max_compression_ratio and size >= RATIO_MIN_BYTES:
            packed = int(member.compress_size)
            if packed <= 0 or size / packed > limits.max_compression_ratio:
                ratio = f"{size / packed:.0f}" if packed > 0 else "∞"
                raise ZipRefused(f"ZIP refused: the entry {_shown(name)} is compressed {ratio}:1, over the limit of "
                                 f"{limits.max_compression_ratio}:1 (intake.max_compression_ratio) — nothing was unpacked")
        total += size
    if limits.max_unpacked_bytes and total > limits.max_unpacked_bytes:
        raise ZipRefused(f"ZIP refused: it unpacks to {_mib(total)} MiB, over the limit of "
                         f"{_mib(limits.max_unpacked_bytes)} MiB (intake.max_unpacked_mb) — nothing was unpacked")


def _safe_extract(zf: zipfile.ZipFile, dest: Path, limits: ZipLimits | None = None) -> None:
    """zip을 안전하게 푼다.
    깨진 파일 이름은 복원한다. 대상 디렉토리 밖을 가리키는 항목은 거부한다(zip slip 공격 방지).
    ``limits``가 있으면 풀기 전과 푸는 동안 한도를 확인한다. 넘으면 ``ZipRefused``를 던진다.
    이미 쓴 파일은 호출한 쪽이 지운다.

    The limits are checked against the ZIP directory's sizes before writing, then against the bytes
    written."""
    dest = dest.resolve()
    plan: list[tuple[zipfile.ZipInfo, Path, str]] = []
    for member in zf.infolist():
        name = fix_zip_name(member)
        if name != member.filename:
            log.info("zip entry name re-decoded: %r -> %r", member.filename, name)
        target = (dest / name).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise ValueError(f"zip slip blocked: {name}")
        plan.append((member, target, name))
    if limits is not None:
        _check_declared(plan, limits)
    total = 0
    for member, target, name in plan:
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as dst:
            if limits is None:
                shutil.copyfileobj(src, dst)
                continue
            size = 0
            while True:
                chunk = src.read(_UNPACK_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                total += len(chunk)
                # ZIP 디렉토리의 크기는 거짓일 수 있다. 실제로 쓴 바이트로 다시 확인한다
                if limits.max_entry_bytes and size > limits.max_entry_bytes:
                    raise ZipRefused(f"ZIP refused while unpacking: the entry {_shown(name)} wrote more than "
                                     f"{_mib(limits.max_entry_bytes)} MiB (intake.max_entry_mb) — {_LIES}")
                if limits.max_unpacked_bytes and total > limits.max_unpacked_bytes:
                    raise ZipRefused(f"ZIP refused while unpacking: more than {_mib(limits.max_unpacked_bytes)} MiB "
                                     f"written (intake.max_unpacked_mb) — {_LIES}")
                if limits.max_compression_ratio and size >= RATIO_MIN_BYTES and (
                        member.compress_size <= 0 or size / member.compress_size > limits.max_compression_ratio):
                    raise ZipRefused(f"ZIP refused while unpacking: the entry {_shown(name)} expanded more than "
                                     f"{limits.max_compression_ratio}:1 (intake.max_compression_ratio) — {_LIES}")
                dst.write(chunk)


_ROOT_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                     "build.xml")


def _detect_project_root(extracted: Path) -> Path:
    """압축을 푼 디렉토리에서 프로젝트 루트를 찾는다. 최상위 폴더가 하나뿐이면 그 안으로 들어간다.
    맨 위에 빌드 파일이 있으면 거기서 멈춘다. Gradle, Ant 파일에서도 멈춘다.
    그래야 접수가 실제 빌드 도구 이름으로 거절할 수 있다."""
    entries = [p for p in extracted.iterdir() if not p.name.startswith("__MACOSX") and p.name != ".DS_Store"]
    if any((extracted / n).exists() for n in _ROOT_BUILD_FILES):
        return extracted
    if len(entries) == 1 and entries[0].is_dir():
        return _detect_project_root(entries[0])
    return extracted


RUN_DIR_FILES = ("errors.jsonl", "errors.md", "report.md", "report.json", "sbmig.log", "changes.patch",
                 "journal.jsonl", "plan.json", "profile.json", "result.zip")   # 그 밖에 logs/, project/가 있다


def logs_dir(ws: Any) -> Path:
    """실행 디렉토리의 `logs` 폴더 경로를 반환한다. run_dir만 가진 객체(테스트, 화면)도 받는다."""
    d = getattr(ws, "logs_dir", None)
    return Path(d) if d is not None else Path(ws.run_dir) / "logs"


class Workspace:
    """실행 한 번의 작업 공간 클래스.
    root는 프로젝트 복사본이다(브랜치 sbmig/<run_id>). run_dir은 실행 결과를 담는 디렉토리다.
    모든 단계가 파일 읽기와 쓰기, 커밋과 되돌리기, diff, 저널 기록을 이 클래스로 한다."""
    def __init__(self, root: Path, run_dir: Path, run_id: str):
        self.root = Path(root).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.run_id = run_id
        self.logs_dir = self.run_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.run_dir / "journal.jsonl"
        # 실행이 다른 프로세스에 있으면 웹이 이 파일로 중단을 요청한다
        self.stop_request_path = self.run_dir / STOP_REQUEST_FILE
        self._log_counter = 0
        self.git_branch = f"sbmig/{run_id}"
        self._git_ready = False
        self.encoding_candidates: tuple[str, ...] = DEFAULT_ENCODING_CANDIDATES
        self._read_encodings: dict[str, str] = {}      # 절대 경로별로 마지막에 읽거나 쓴 인코딩
        # 접수에서 찾은 pom 복사본 폴더(IDE 출력이나 백업 폴더, boot/bin 등).
        # walk / glob이 target/처럼 건너뛴다
        self.ignored_dirs: set[str] = set()
        # 올린 zip이 풀기 한도를 넘어 접수에서 거절한 사유. None이면 정상 접수다
        self.intake_refusal: Optional[str] = None
        try:                                           # `dump-threads` 파일이 생기면 스레드 덤프를 남긴다
            from sbmig.core.diag import watch_dump_trigger
            watch_dump_trigger(self.run_dir)
        except Exception:  # noqa: BLE001  # pragma: no cover - diagnostics never break a run
            log.debug("dump-threads trigger not registered for %s", self.run_dir, exc_info=True)

    # ------------------------------------------------------------------ 생성
    @classmethod
    def from_input(cls, src: str | Path, out_dir: str | Path, run_id: Optional[str] = None,
                   limits: ZipLimits | None = None) -> "Workspace":
        """입력 폴더를 복사하거나 zip을 풀어 프로젝트 복사본을 만들고 Workspace를 반환한다. 접수가 호출한다.
        zip이 풀기 한도를 넘으면 푼 것을 모두 지운다. 이때는 빈 project 폴더와 거절 사유(``intake_refusal``)를
        가진 Workspace를 반환한다.

        The project lands in `<out_dir>/<run_id>/project`; `limits` None = the `intake` defaults.
        Raises FileExistsError when the project folder exists, ValueError for an input that is neither a
        directory nor a zip. A refusal journals `intake_refused`; the caller ends the run like an intake
        refusal."""
        src = Path(src).resolve()
        run_id = run_id or new_run_id()
        run_dir = Path(out_dir).resolve() / run_id
        project_dir = run_dir / "project"
        if project_dir.exists():
            raise FileExistsError(project_dir)
        project_dir.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, project_dir, symlinks=False, ignore=_intake_ignore(src))
            root = project_dir
        elif zipfile.is_zipfile(src):
            tmp = Path(tempfile.mkdtemp(prefix="sbmig-unzip-", dir=str(run_dir)))
            try:
                with zipfile.ZipFile(src) as zf:
                    _safe_extract(zf, tmp, limits if limits is not None else ZipLimits.from_config())
            except ZipRefused as e:
                # 한도를 넘은 zip은 쓴 것을 모두 지우고 빈 project 폴더로 접수 거절을 알린다
                shutil.rmtree(tmp, ignore_errors=True)
                project_dir.mkdir()
                ws = cls(project_dir, run_dir, run_id)
                ws.intake_refusal = str(e)
                log.error("intake: %s", e)
                ws.journal({"event": "intake_refused", "source": str(src), "reason": str(e)})
                return ws
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            detected = _detect_project_root(tmp)
            shutil.copytree(detected, project_dir, symlinks=False, ignore=_intake_ignore(detected))
            shutil.rmtree(tmp, ignore_errors=True)
            root = project_dir
        else:
            raise ValueError(f"input must be a directory or a zip: {src}")
        ws = cls(root, run_dir, run_id)
        ws.journal({"event": "intake", "source": str(src), "project_dir": str(project_dir)})
        return ws

    # ------------------------------------------------------------------ 파일
    def abs(self, rel: str | Path) -> Path:
        """프로젝트 상대 경로를 절대 경로로 바꾼다. 절대 경로는 그대로 반환한다."""
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def rel(self, path: str | Path) -> str:
        """경로를 '/'를 쓰는 프로젝트 상대 경로로 바꾼다. 프로젝트 밖 경로는 '/'를 쓰는 절대 경로로 반환한다."""
        p = Path(path)
        if not p.is_absolute():
            return p.as_posix()
        rel = _relative_posix(p.resolve(), self.root)
        return rel if rel is not None else p.as_posix()

    def is_build_output(self, rel: str | Path) -> bool:
        """경로가 이 프로젝트의 Maven 빌드 산출물(target/) 안에 있으면 True를 반환한다."""
        return is_build_output_path(self.rel(rel), self.root)

    def exists(self, rel: str | Path) -> bool:
        """프로젝트 안에 그 경로가 있는지 확인한다."""
        return self.abs(rel).exists()

    # -- 인코딩: 파일마다 감지한 인코딩으로 읽고 같은 인코딩으로 다시 쓴다
    def set_encoding_candidates(self, candidates: Optional[list[str] | tuple[str, ...]]) -> None:
        """인코딩 감지에 쓸 후보 순서(cfg.build.encoding_candidates)를 정한다. 비어 있으면 기본값을 쓴다."""
        self.encoding_candidates = tuple(candidates) if candidates else DEFAULT_ENCODING_CANDIDATES

    def encoding_of(self, rel: str | Path) -> str:
        """기존 파일의 인코딩을 감지해 반환한다. BOM이 있으면 utf-8-sig, 파일이 없으면 utf-8을 반환한다."""
        return detect_encoding(self.abs(rel), self.encoding_candidates)

    def read(self, rel: str | Path, encoding: Optional[str] = None) -> str:
        """파일을 감지한 인코딩으로 읽는다.
        ``encoding``을 주면 그 인코딩으로 읽는다. 줄바꿈(\\r\\n 포함)은 그대로 둔다.
        읽은 인코딩을 기억해 두고, write가 같은 인코딩으로 쓴다."""
        p = self.abs(rel)
        enc = encoding or detect_encoding(p, self.encoding_candidates)
        with open(p, "r", encoding=enc, errors="replace", newline="") as fh:
            text = fh.read()
        self._read_encodings[str(p.resolve())] = enc
        return text

    def write(self, rel: str | Path, text: str, encoding: Optional[str] = None) -> None:
        """텍스트를 파일의 원래 인코딩으로 쓴다. BOM은 유지하고 새 파일은 utf-8로 쓴다.
        그 인코딩에 없는 문자가 있으면 .java 파일은 \\uXXXX로 바꿔 쓴다.
        다른 파일은 UTF-8로 저장하고 경고 로그를 남긴다.

        encoding None = the encoding the file was read with, else the detected one. Newlines are untouched."""
        p = self.abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        key = str(p.resolve())
        enc = encoding
        if enc is None:
            if p.is_file():
                enc = detect_encoding(p, self.encoding_candidates)
                if enc == "utf-8" and self._read_encodings.get(key) not in (None, "utf-8"):
                    # 지금은 ASCII뿐이지만 cp949로 읽었던 파일이다.
                    # 나중에 한글이 들어올 수 있으니 원래 인코딩을 유지한다
                    enc = self._read_encodings[key]
            else:
                enc = self._read_encodings.get(key, "utf-8")
        try:
            data = text.encode(enc)
        except UnicodeEncodeError:
            if p.suffix == ".java":
                data = _java_unicode_escape(text, enc).encode(enc)
            else:
                log.warning("%s: text not representable in %s, writing UTF-8 instead", self.rel(p), enc)
                enc = "utf-8"
                data = text.encode(enc)
        except LookupError:
            enc = "utf-8"
            data = text.encode(enc)
        p.write_bytes(data)
        self._read_encodings[key] = enc
        _remember_encoding(p, enc, self.encoding_candidates)

    def delete(self, rel: str | Path) -> None:
        """파일이나 디렉토리를 지운다."""
        p = self.abs(rel)
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    def move(self, src_rel: str | Path, dst_rel: str | Path) -> None:
        """파일을 옮긴다. 대상 디렉토리가 없으면 만든다."""
        src, dst = self.abs(src_rel), self.abs(dst_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    def is_ignored(self, rel: str) -> bool:
        """``rel``이 접수에서 무시하기로 한 폴더(pom 복사본 폴더) 안에 있으면 True를 반환한다."""
        r = str(rel).replace("\\", "/").strip("/")
        return any(r == d or r.startswith(d + "/") for d in self.ignored_dirs if d)

    def walk(self) -> Iterator[tuple[str, list[str], list[str]]]:
        """프로젝트 폴더를 하위 폴더까지 순회한다.
        .git, Maven 빌드 산출물, ``ignored_dirs``에는 들어가지 않는다.

        Yields (rel dir with "/", "" for the root; dir names; file names)."""
        root = str(self.root)
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else posix_rel(rel_dir)
            parts = rel_dir.split("/") if rel_dir else []
            dirnames[:] = sorted(d for d in dirnames if d != ".git" and not (
                d == "target" and _is_output_target(Path(dirpath), parts, "pom.xml" in filenames))
                and not (self.ignored_dirs and self.is_ignored(f"{rel_dir}/{d}" if rel_dir else d)))
            yield rel_dir, dirnames, filenames

    def glob(self, pattern: str) -> list[str]:
        """이름이 패턴에 맞는 파일과 디렉토리의 프로젝트 상대 경로 목록을 반환한다.
        .git, 빌드 산출물, ``ignored_dirs``는 제외한다. 패턴에 '/'가 있으면 경로 전체를 패턴과 맞춘다."""
        if "/" in pattern or "\\" in pattern:
            return sorted(r for r in (self.rel(p) for p in self.root.rglob(pattern))
                          if ".git" not in r.split("/") and not is_build_output_path(r, self.root)
                          and not self.is_ignored(r))
        out: list[str] = []
        for rel_dir, dirnames, filenames in self.walk():
            for name in (*dirnames, *filenames):
                if fnmatch.fnmatch(name, pattern):
                    out.append(f"{rel_dir}/{name}" if rel_dir else name)
        return sorted(out)

    def java_files(self) -> list[str]:
        """프로젝트의 모든 .java 파일 경로를 반환한다."""
        return self.glob("*.java")

    def pom_files(self) -> list[str]:
        """프로젝트의 모든 pom.xml 경로를 반환한다."""
        return [p for p in self.glob("pom.xml")]

    # ------------------------------------------------------------------ 로그, 저널
    def new_log_path(self, label: str, ext: str = "log") -> Path:
        """실행 안에서 번호가 하나씩 늘어나는 로그 파일 경로 `logs/NNN-<label>.<ext>`를 만든다."""
        self._log_counter += 1
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
        return self.logs_dir / f"{self._log_counter:03d}-{safe}.{ext}"

    def stop_requested(self) -> bool:
        """실행 폴더에 ``stop.request``(Job 모드의 중단 요청)가 있는지 확인한다. 예외를 던지지 않는다."""
        try:
            return self.stop_request_path.is_file()
        except OSError:
            return False

    def journal(self, event: dict[str, Any]) -> None:
        """journal.jsonl에 이벤트 한 줄을 추가한다. 웹 화면과 사후 분석이 이 파일을 읽는다."""
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id, **event}
        line = json.dumps(rec, ensure_ascii=False, default=_json_default)
        if output_scrubbing():
            # 웹 화면이 실행 중에 이 파일을 읽는다. 그래서 기록할 때 마스킹한다.
            # JSON이 깨지지 않도록 값 단위로 마스킹한다
            clean = scrub_json(json.loads(line))
            # 마스킹에 실패하면 줄 전체가 정해진 형태로 바뀐다. 시각과 실행 ID는 따로 남긴다
            clean.setdefault("ts", rec["ts"])
            clean.setdefault("run_id", self.run_id)
            line = json.dumps(clean, ensure_ascii=False)
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def save_model(self, name: str, model: BaseModel) -> Path:
        """모델을 실행 디렉토리에 JSON 파일로 저장한다(profile.json, plan.json 등)."""
        p = self.run_dir / name
        p.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        return p

    # ------------------------------------------------------------------ git 스냅샷
    def _git(self, *args: str, check: bool = True, env: Optional[dict[str, str]] = None,
             input: Optional[bytes] = None) -> subprocess.CompletedProcess:
        """작업 공간에서 git 명령을 실행한다. 프롬프트, 서명, 훅을 끄고 타임아웃을 건다.
        출력은 콘솔 코드 페이지와 상관없이 UTF-8로 디코딩한다.

        A global git config may ask for a GPG passphrase, run hooks or call a credential manager; in a
        Windows service process that waits forever. A `text=True` cp949 decode on Windows dies inside
        subprocess's reader thread and leaves `stdout=None`. `_git_bytes` keeps the raw bytes (patch
        files)."""
        cp = self._git_bytes(*args, check=check, env=env, input=input)
        return subprocess.CompletedProcess(cp.args, cp.returncode, _as_text(cp.stdout), _as_text(cp.stderr))

    def _git_env(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GCM_INTERACTIVE": "never",
               "GIT_EDITOR": "true", "GIT_AUTHOR_NAME": "sbmig", "GIT_AUTHOR_EMAIL": "sbmig@local",
               "GIT_COMMITTER_NAME": "sbmig", "GIT_COMMITTER_EMAIL": "sbmig@local"}
        if extra:
            env.update(extra)
        return env

    def _git_cmd(self, args: Iterable[str]) -> list[str]:
        no_hooks = str(self.run_dir / "no-hooks")                       # 없는 디렉토리를 지정해 훅을 끈다
        cmd = ["git"]
        for kv in (*GIT_CONFIG, f"core.hooksPath={no_hooks}"):
            cmd += ["-c", kv]
        return cmd + list(args)

    def _git_bytes(self, *args: str, check: bool = True, env: Optional[dict[str, str]] = None,
                   input: Optional[bytes] = None, stdout: Any = None) -> subprocess.CompletedProcess:
        """git 명령을 실행하고 출력을 바이트 그대로 반환한다(패치 파일용).
        프롬프트와 편집기를 막고 작성자를 sbmig로 고정한다. 타임아웃이나 check 실패면 GitError를 던진다.
        - ``env``: 고정 값 위에 덧붙일 환경 변수
        - ``input``: stdin으로 보낼 바이트. 없으면 stdin을 닫는다
        - ``stdout``: 바이너리 파일 객체를 주면 출력이 그 파일로 간다. 이때 ``cp.stdout``은 비어 있다"""
        cmd = self._git_cmd(args)
        io_kw: dict[str, Any] = {"stderr": subprocess.PIPE,
                                 "stdout": stdout if stdout is not None else subprocess.PIPE}
        if input is not None:
            io_kw["input"] = input
        else:
            io_kw["stdin"] = subprocess.DEVNULL
        try:
            cp = subprocess.run(cmd, cwd=self.root, env=self._git_env(env), timeout=GIT_TIMEOUT_S,
                                creationflags=_child_creationflags(), **io_kw)
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S:.0f}s in {self.root} — something "
                           "outside sbmig is blocking git (antivirus / indexer lock, a global hook, a credential or "
                           "GPG prompt); sbmig already disables prompts, hooks and signing per call") from e
        if check and cp.returncode != 0:
            err = _as_text(cp.stderr).strip() or _as_text(cp.stdout).strip()
            raise GitError(f"git {' '.join(args)} failed: {err}")
        return cp

    def git_init(self) -> str:
        """프로젝트를 git 저장소(브랜치 sbmig/<run_id>)로 만들고, 원래 상태를 첫 커밋으로 남긴다.
        .gitignore와 속성 파일도 맞춘다. 첫 커밋의 sha를 반환한다."""
        if not (self.root / ".git").exists():
            self._git("init", "-q")
            self._git("checkout", "-q", "-b", self.git_branch)
        else:
            self._git("checkout", "-q", "-B", self.git_branch)
        info = self.root / ".git" / "info"
        if info.parent.is_dir():
            info.mkdir(exist_ok=True)
            (info / "attributes").write_text(GIT_INFO_ATTRIBUTES, encoding="utf-8")
        ignore = self.root / ".gitignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        present = set(existing.splitlines())
        for line in self._gitignore_lines():
            if line not in present:
                existing += ("\n" if existing and not existing.endswith("\n") else "") + line + "\n"
                present.add(line)
        ignore.write_text(existing, encoding="utf-8")
        self._git_ready = True
        return self.commit("sbmig: as-is snapshot", allow_empty=True)

    def _gitignore_lines(self) -> list[str]:
        """git_init이 루트 .gitignore에 넣을 줄 목록을 반환한다.
        target/은 pom 옆에 있는 빌드 산출물만 무시한다.
        빌드가 만드는 파일(.flattened-pom.xml 등)도 무시한다. 그래야 바뀐 파일이 없는지 정확히 알 수 있다.
        원래 프로젝트에 이미 있던 생성 파일은 무시하지 않는다.

        `!**/src/**/target/` keeps packages such as `src/main/java/com/x/target/`; every other as-is
        `target` dir that is not build output gets its own `!/<dir>/target/`. The `.../**` twins re-include
        the files when the project's own .gitignore says `**/target/**`."""
        keep_targets: list[str] = []
        seen_generated: set[str] = set()
        for rel_dir, dirnames, filenames in self.walk():
            parts = rel_dir.split("/") if rel_dir else []
            if "target" in dirnames and "src" not in parts:
                d = f"!/{rel_dir}/target/" if rel_dir else "!/target/"
                keep_targets += [d, d + "**"]
            if ".flattened-pom.xml" in filenames:
                seen_generated.add(".flattened-pom.xml")
            for d in (".apt_generated", ".apt_generated_tests"):
                if d in dirnames:
                    seen_generated.add(d + "/")
            if "generated" in dirnames and len(parts) >= 2 and parts[-2] == "src" and parts[-1] in ("main", "test"):
                seen_generated.add(f"**/src/{parts[-1]}/generated/")
        lines = ["target/", "!**/src/**/target/", "!**/src/**/target/**", *keep_targets, ".sbmig/", "*.class"]
        lines += [g for g in GENERATED_IGNORES if g not in seen_generated]
        return lines

    def _clean_excludes(self) -> list[str]:
        """`git clean`에 넘길 제외 인자를 만든다.
        빌드 산출물은 남기고, 소스인 target 디렉토리는 다른 파일처럼 정리한다."""
        out = ["-e", "target/", "-e", "!**/src/**/target/"]
        try:
            text = (self.root / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("!/") and line.endswith("/target/"):
                out += ["-e", line]
        return out

    def commit(self, message: str, allow_empty: bool = False, *, paths: Optional[Iterable[str]] = None) -> str:
        """지금 프로젝트 상태를 커밋하고 HEAD의 sha를 반환한다. 커밋할 것이 없으면 지금 HEAD를 반환한다.
        ``paths``가 없으면 무시된 jar까지 전부 커밋한다. ``paths``가 있으면 그 경로만 커밋한다.
        LLM 시도는 스크립트가 보고한 파일만 커밋한다. 그래야 빌드가 lib/에 복사한 jar가 섞이지 않는다.

        `paths` outside the project (absolute or with "..") are dropped. Raises GitError when git commit fails
        for another reason than "nothing to commit"."""
        if not self._git_ready:
            self.git_init()
        if paths is None:
            self._git("add", "-A")
            self._force_add_ignored_jars()
        else:
            wanted = [p for p in dict.fromkeys(str(x).replace("\\", "/").strip("/") for x in paths)
                      if p and not Path(p).is_absolute() and ".." not in p.split("/")]   # 프로젝트 안의 경로만
            if not wanted and not allow_empty:
                return self.head()
            # Windows 명령줄 길이 제한 때문에 200개씩 add한다. 실패하면 경로를 하나씩 다시 시도한다
            # 스크립트가 일부러 바꾼 경로는 무시 대상이어도 넣는다(-f)
            for i in range(0, len(wanted), 200):
                chunk = wanted[i:i + 200]
                if self._git("add", "-A", "-f", "--", *chunk, check=False).returncode != 0:
                    for one in chunk:                     # 잘못된 경로 하나 때문에 나머지를 놓치지 않게 한다
                        self._git("add", "-A", "-f", "--", one, check=False)
            if not allow_empty and self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return self.head()                        # 보고한 파일이 원래대로 돌아가 올릴 것이 없다
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        cp = self._git(*args, check=False)
        if cp.returncode != 0 and "nothing to commit" not in (cp.stdout + cp.stderr) \
                and "no changes added to commit" not in (cp.stdout + cp.stderr):
            raise GitError(cp.stderr.strip() or cp.stdout.strip())
        return self.head()

    def _force_add_ignored_jars(self) -> None:
        """빌드 산출물 밖의 jar는 프로젝트가 `*.jar` / `lib/`를 무시해도 강제로 add한다.
        빠지면 changes.patch와 되돌리기에서 jar를 잃는다. jar가 아닌 무시된 파일(로그, node_modules 등)은 넣지 않는다."""
        cp = self._git("ls-files", "-z", "--others", "--ignored", "--exclude-standard", check=False)
        jars = [f for f in (cp.stdout or "").split("\0")
                if f.lower().endswith(".jar") and "node_modules/" not in f and not is_build_output_path(f, self.root)]
        for i in range(0, len(jars), 200):
            self._git("add", "-f", "--", *jars[i:i + 200], check=False)

    def head(self) -> str:
        """현재 HEAD 커밋의 sha를 반환한다."""
        return self._git("rev-parse", "HEAD").stdout.strip()

    def status_paths(self) -> set[str]:
        """`git status`에 나오는 경로(수정, 삭제, 미추적 파일)를 프로젝트 상대 경로로 반환한다. 무시된 파일은 뺀다."""
        out = self._git("status", "--porcelain", "-z", "--untracked-files=all", check=False).stdout or ""
        paths: set[str] = set()
        items = out.split("\0")
        i = 0
        while i < len(items):
            item = items[i]
            if len(item) > 3:
                paths.add(item[3:].replace("\\", "/"))
                if item[0] in "RC":                      # 이름 변경이나 복사는 다음 항목에 이전 이름이 온다
                    i += 1
            i += 1
        return paths

    def restore_build_byproducts(self, before: set[str]) -> list[str]:
        """빌드가 target/ 밖에 만든 파일을 치운다. 다음 시도의 커밋과 diff에 섞이지 않게 한다.
        빌드 전의 ``before``에 없던 경로만 본다. 추적 파일은 되돌리고, 미추적 파일은 .git/info/exclude에 넣는다.
        정리한 경로 목록을 반환한다. 예외를 던지지 않는다.

        Examples of such products: a module jar maven-dependency-plugin copied into `lib/`, generated
        sources, a rewritten `.flattened-pom.xml`. Call right after a build with the `status_paths()` taken
        before it."""
        try:
            after = self.status_paths()
        except Exception:  # noqa: BLE001 - git 문제면 정리 없이 넘어간다
            return []
        new = sorted(p for p in after - set(before) if p and not is_build_output_path(p, self.root))
        if not new:
            return []
        tracked: set[str] = set()
        for i in range(0, len(new), 200):
            cp = self._git("ls-files", "-z", "--", *new[i:i + 200], check=False)
            tracked |= {x.replace("\\", "/") for x in (cp.stdout or "").split("\0") if x}
        restored = [p for p in new if p in tracked]
        for i in range(0, len(restored), 200):
            self._git("checkout", "-q", "--", *restored[i:i + 200], check=False)
        untracked = [p for p in new if p not in tracked]
        if untracked:
            exclude = self.root / ".git" / "info" / "exclude"
            try:
                exclude.parent.mkdir(exist_ok=True)
                present = set(exclude.read_text(encoding="utf-8").splitlines()) if exclude.exists() else set()
                lines = [f"/{p}" for p in untracked if f"/{p}" not in present]
                if lines:
                    with exclude.open("a", encoding="utf-8") as fh:
                        fh.write(("" if not present else "\n") + "\n".join(lines) + "\n")
            except OSError as e:
                log.warning("could not exclude build products %s: %s", untracked[:3], e)
        log.info("build products outside target/ set aside: %s%s", ", ".join(new[:5]),
                 f" (+{len(new) - 5} more)" if len(new) > 5 else "")
        return new

    def is_dirty(self) -> bool:
        """커밋하지 않은 변경이 있는지 확인한다."""
        return bool(self._git("status", "--porcelain").stdout.strip())

    def revert_to(self, sha: str) -> None:
        """프로젝트를 지정한 커밋 상태로 되돌리고 남은 파일을 지운다. 빌드 산출물은 남긴다."""
        self._git("reset", "-q", "--hard", sha)
        self._git("clean", "-qfd", *self._clean_excludes())

    def discard_uncommitted(self) -> None:
        """커밋하지 않은 변경을 모두 버린다. 빌드 산출물은 남긴다."""
        self._git("checkout", "-q", "--", ".")
        self._git("clean", "-qfd", *self._clean_excludes())

    def diff(self, from_sha: str, to_sha: Optional[str] = None, stat: bool = False) -> str:
        """두 커밋 사이의 diff 텍스트를 반환한다. ``to_sha``가 없으면 지금 프로젝트 상태와 비교한다."""
        args = ["diff", "--no-color"]
        if stat:
            args.append("--stat")
        args.append(from_sha)
        if to_sha:
            args.append(to_sha)
        return self._git(*args).stdout

    def diff_bytes(self, from_sha: str, to_sha: Optional[str] = None) -> bytes:
        """`git diff` 결과를 원본 바이트로 반환한다. 그래서 MS949 소스는 패치에서도 MS949로 남는다."""
        args = ["diff", "--no-color", from_sha] + ([to_sha] if to_sha else [])
        return _as_bytes(self._git_bytes(*args).stdout)

    def changed_files_since(self, sha: str) -> list[str]:
        """sha 이후 바뀐 파일과 미추적 파일 목록을 반환한다. 커밋했는지와 상관없이 본다.
        한글 파일 이름도 이스케이프 없이 그대로 나온다."""
        out = self._git("diff", "--name-only", "-z", sha).stdout
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
        return sorted({f for f in (out + "\0" + untracked).split("\0") if f.strip()})

    def changed_between(self, from_sha: str, to_sha: str) -> list[str]:
        """두 커밋 사이에 달라진 파일 목록(posix 경로, git 출력 순서)을 반환한다. 한글 이름도 그대로 나온다."""
        out = self._git("diff", "--name-only", "-z", from_sha, to_sha).stdout
        return [f for f in out.split("\0") if f.strip()]

    def numstat_since(self, sha: str) -> list[tuple[str, str, Optional[int], Optional[int]]]:
        """커밋 sha 이후 달라진 추적 파일마다 (경로, 상태, 추가 줄, 삭제 줄)을 반환한다.
        상태는 M / A / D 중 하나이고 이름 변경은 삭제와 추가로 나눈다. 바이너리 파일은 줄 수가 None이다.
        git이 실패하면 GitError를 던진다."""
        names = self._git("diff", "--name-status", "--no-renames", "-z", sha).stdout.split("\0")
        status: dict[str, str] = {}
        for i in range(0, len(names) - 1, 2):
            if names[i].strip():
                status[names[i + 1]] = names[i].strip()[:1]
        out: list[tuple[str, str, Optional[int], Optional[int]]] = []
        for item in self._git("diff", "--numstat", "--no-renames", "-z", sha).stdout.split("\0"):
            parts = item.split("\t", 2)
            if len(parts) != 3 or not parts[2]:
                continue
            a, r, path = parts
            out.append((path, status.get(path, "M"), int(a) if a.isdigit() else None, int(r) if r.isdigit() else None))
        return out

    def diff_file_bytes(self, sha: str, rel: str) -> bytes:
        """파일 하나가 커밋 sha 이후 어떻게 바뀌었는지 diff를 원본 바이트로 반환한다. 실패하면 GitError를 던진다."""
        return _as_bytes(self._git_bytes("diff", "--no-color", "--no-renames", sha, "--", rel).stdout)

    def file_at(self, sha: str, rel: str) -> Optional[str]:
        """커밋 sha 시점의 파일 내용을 반환한다(`git show <sha>:<path>`).
        내용은 UTF-8로 디코딩한다. 파일이 없거나 git이 실패하면 None을 반환한다. 예외를 던지지 않는다."""
        path = str(rel).replace("\\", "/").lstrip("/").removeprefix("./")
        try:
            cp = self._git("show", f"{sha}:{path}", check=False)
        except Exception:  # noqa: BLE001 - 타임아웃 GitError나 OSError면 알 수 없음으로 본다
            return None
        if cp.returncode != 0:
            return None
        return cp.stdout or ""

    def first_commit(self) -> str:
        """원래 상태를 저장한 첫 커밋의 sha를 반환한다."""
        return self._git("rev-list", "--max-parents=0", "HEAD").stdout.strip().splitlines()[-1]

    # ------------------------------------------------------------------ tree 객체(코드 변경 화면용)
    def _git_dir(self) -> Path:
        d = self.root / ".git"
        if d.is_dir():
            return d
        out = self._git("rev-parse", "--absolute-git-dir").stdout.strip()   # worktree나 `gitdir:` 파일인 경우
        return Path(out)

    def _temp_index(self) -> Path:
        return self._git_dir() / f"sbmig-tmp-{os.getpid()}-{uuid.uuid4().hex[:12]}.index"

    @staticmethod
    def _drop_index(p: Path) -> None:
        for q in (p, p.with_name(p.name + ".lock")):
            try:
                q.unlink()
            except OSError:
                pass

    def snapshot_tree(self) -> str:
        """지금 프로젝트 전체를 커밋하지 않고 tree 객체로 저장한 뒤 sha를 반환한다. HEAD와 index는 그대로다.
        무시된 jar도 넣는다. 그래서 지금 `commit()`이 만들 tree와 같다. 실패하면 GitError를 던진다.

        Steps: copy `.git/index` WITH its mtime (a fresh mtime would defeat git's racy-clean check, and a
        same-size edit in the index's second would pass as unchanged), `GIT_INDEX_FILE` = the copy, `git
        add -A`, force-add the ignored jars outside build output, `git write-tree`. The object store gains
        the new blobs."""
        tmp = self._temp_index()
        try:
            real = self._git_dir() / "index"
            if real.is_file():
                # 수정 시각까지 복사한다. 시각이 새로 찍히면 git이 같은 초에 바뀐 파일을 그대로라고 본다
                shutil.copy2(real, tmp)
            env = {"GIT_INDEX_FILE": str(tmp)}
            self._git("add", "-A", env=env)
            cp = self._git("ls-files", "-z", "--others", "--ignored", "--exclude-standard", env=env, check=False)
            jars = [f for f in (cp.stdout or "").split("\0")
                    if f.lower().endswith(".jar") and "node_modules/" not in f and not is_build_output_path(f, self.root)]
            if jars:
                self._git("add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", check=False,
                          env={**env, "GIT_LITERAL_PATHSPECS": "1"},         # jar 이름의 `[`를 glob으로 보지 않는다
                          input="\0".join(jars).encode("utf-8"))
            return self._git("write-tree", env=env).stdout.strip()
        finally:
            self._drop_index(tmp)

    def tree_entries(self, treeish: str) -> dict[str, tuple[str, str]]:
        """tree 안의 일반 파일(모드 100644, 100755)마다 경로와 (모드, sha)를 반환한다.
        하위 폴더까지 보고, 심볼릭 링크와 서브모듈은 뺀다. 실패하면 GitError를 던진다."""
        out = self._git_bytes("ls-tree", "-r", "-z", "--full-tree", treeish).stdout or b""
        entries: dict[str, tuple[str, str]] = {}
        for rec in out.split(b"\0"):
            if not rec:
                continue
            meta, _, path = rec.partition(b"\t")
            parts = meta.split(b" ")
            if len(parts) != 3 or parts[0] not in (b"100644", b"100755"):
                continue
            entries[path.decode("utf-8", errors="surrogateescape")] = (parts[0].decode(), parts[2].decode())
        return entries

    def make_tree(self, entries: Iterable[tuple[str, str, str]]) -> str:
        """(모드, sha, 경로) 목록으로 tree 객체를 만들고 sha를 반환한다. 목록이 비면 빈 tree다.
        blob은 이미 있어야 한다. 파일이면서 디렉토리인 경로나 없는 blob이면 GitError를 던진다."""
        tmp = self._temp_index()
        try:
            env = {"GIT_INDEX_FILE": str(tmp)}
            data = b"".join(f"{mode} blob {sha}\t".encode() + path.encode("utf-8", errors="surrogateescape") + b"\0"
                            for mode, sha, path in entries)
            if data:
                self._git_bytes("update-index", "-z", "--index-info", env=env, input=data)
            return self._git("write-tree", env=env).stdout.strip()
        finally:
            self._drop_index(tmp)

    def _cat_blobs(self, shas: Iterable[str], limit: int) -> Iterator[tuple[str, Optional[bytes], int]]:
        """blob 내용을 요청한 순서대로 `(sha, bytes, size)`로 낸다. git 호출은 한 번이다.
        `limit`보다 큰 blob은 bytes가 None이다. 없는 객체는 size가 -1이다.

        One blob in memory at a time; a writer thread feeds the requests so neither pipe can fill up. Raises
        GitError."""
        wanted = list(shas)
        if not wanted:
            return
        proc = subprocess.Popen(self._git_cmd(["cat-file", "--batch"]), cwd=self.root, env=self._git_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=_child_creationflags())
        timer = threading.Timer(GIT_TIMEOUT_S, proc.kill)
        timer.daemon = True
        timer.start()

        def feed() -> None:
            try:
                for i in range(0, len(wanted), 512):
                    proc.stdin.write("".join(s + "\n" for s in wanted[i:i + 512]).encode())
                proc.stdin.close()
            except OSError:
                pass

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        try:
            out = proc.stdout
            for sha in wanted:
                head = out.readline()
                if not head:
                    raise GitError(f"git cat-file --batch ended early in {self.root}")
                parts = head.split()
                if len(parts) < 3 or parts[1] == b"missing":
                    yield sha, None, -1
                    continue
                size = int(parts[2])
                if size <= limit:
                    data = out.read(size)
                    if len(data) != size:
                        raise GitError(f"git cat-file --batch: short read in {self.root}")
                else:
                    left = size
                    while left > 0:
                        chunk = out.read(min(left, 1 << 20))
                        if not chunk:
                            raise GitError(f"git cat-file --batch: short read in {self.root}")
                        left -= len(chunk)
                    data = None
                out.read(1)                                      # 내용 뒤의 줄바꿈
                yield sha, data, size
        finally:
            timer.cancel()
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            writer.join(timeout=5)

    # ------------------------------------------------------------------ 결과물
    def write_patch(self, name: str = "changes.patch") -> Path:
        """원래 상태부터 지금까지의 전체 변경을 changes.patch로 저장한다."""
        p = self.run_dir / name
        p.write_bytes(self.diff_bytes(self.first_commit()))
        return p

    def zip_result(self, name: str = "result.zip") -> Path:
        """이관된 프로젝트를 result.zip으로 묶는다. .git과 빌드 산출물은 제외한다."""
        out = self.run_dir / name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_dir, _dirs, filenames in self.walk():           # walk가 .git과 빌드 산출물을 뺀다
                for fname in sorted(filenames):
                    rel = f"{rel_dir}/{fname}" if rel_dir else fname
                    zf.write(self.root / rel, f"{self.root.name}/{rel}")
        return out


def _json_default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    return str(o)


class Stopwatch:
    """``time.monotonic``으로 경과 시간을 재는 클래스."""
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def elapsed(self) -> float:
        """시작한 뒤 지난 시간(초)을 반환한다."""
        return time.monotonic() - self.t0
