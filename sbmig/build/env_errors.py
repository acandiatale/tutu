"""빌드 환경 오류 판별(v2.1r) — 프로젝트가 아니라 빌드 PC 탓인 실패(지금은 Windows 의 ``target/`` 파일 잠금: 백신·
인덱서·IDE·남은 java 프로세스가 막 쓴 jar 를 붙잡음)를 로그 글에서 알아본다. 흐름상 위치: ``MavenRunner.run`` 이 실패한
빌드를 잠깐 기다렸다 다시 돌릴지(``build.file_lock_retries``), 트리아지(``sbmig.triage.blocked``)가 그 케이스를 LLM 없이
``file_lock`` 으로 차단할지, 오류 단계(``sbmig.errors.entries.error_stage``)가 그 오류를 '어느 단계도 증명하지 못함' 으로
볼지 정한다. 핵심 진입점: ``file_lock_hit(text, cfg=None)``, ``file_lock_patterns(cfg=None)``. 지킬 것: 패턴은
``knowledge/config/build_env_errors.yml`` 의 ``file_lock:`` 목록(회사가 자기 백신 문구를 더할 수 있게), 파일이 없거나
비면 내장 목록; 예외를 내지 않는다.

Build-environment errors (v2.1r, docs/INTERFACES.md "v2.1r"): failures caused by the build machine, not the project —
for now Windows file locks on the build output (`target/`): an antivirus / indexer / IDE / a lingering JVM holding a
freshly written jar. `file_lock_hit` tells whether a log text shows one (and which path); the regexes come from
`knowledge/config/build_env_errors.yml` (`file_lock:`), the built-in `BUILTIN_FILE_LOCK` when that file is missing,
unreadable or empty. Never raises."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("sbmig.build.env_errors")

ENV_ERRORS_FILE = "knowledge/config/build_env_errors.yml"
# the same entries as knowledge/config/build_env_errors.yml (the fallback when that file is missing)
BUILTIN_FILE_LOCK: tuple[str, ...] = (
    r"java\.nio\.file\.(?:FileSystemException|AccessDeniedException|FileAlreadyExistsException)\b[^\n]*[\\/]target[\\/]",
    r"다른 프로세스가 파일을 사용 중",
    r"being used by another process",
    r"The process cannot access the file",
    r"Unable to delete[^\n]*target",
    r"Failed to delete[^\n]*[\\/]target[\\/]",
)
# a path with a `target` directory in it (Windows `C:\…\target\…` or posix); stops at whitespace and `:` after the drive
_TARGET_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[^\s:;'\"<>|]*[\\/]target(?:[\\/][^\s:;'\"<>|]*)?")

_CACHE: dict[str, list[re.Pattern[str]]] = {}


def _rules_path(cfg: Any) -> Path:
    if cfg is not None and hasattr(cfg, "resolve_path"):
        try:
            return Path(cfg.resolve_path(ENV_ERRORS_FILE))
        except Exception as e:  # noqa: BLE001 - a broken config falls back to the package copy
            log.debug("build_env_errors path: %s", e)
    from sbmig.core.config import PACKAGE_ROOT
    return PACKAGE_ROOT / ENV_ERRORS_FILE


def _compile(raw: list[Any], where: str) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for p in raw:
        try:
            out.append(re.compile(str(p)))
        except re.error as e:
            log.warning("build_env_errors: file_lock pattern %r in %s ignored: %s", p, where, e)
    return out


def file_lock_patterns(cfg: Any = None) -> list[re.Pattern[str]]:
    """파일 잠금 정규식들: ``knowledge/config/build_env_errors.yml`` 의 ``file_lock:`` (깨진 정규식은 경고 후 건너뜀),
    파일이 없거나 비면 내장 목록. 경로별로 캐시.

    The file-lock regexes: the `file_lock:` list of `knowledge/config/build_env_errors.yml` (bad regexes are logged and
    skipped), else `BUILTIN_FILE_LOCK`. Cached per file path."""
    path = _rules_path(cfg)
    key = str(path)
    if key in _CACHE:
        return _CACHE[key]
    pats: list[re.Pattern[str]] = []
    try:
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            raw = data.get("file_lock") if isinstance(data, dict) else None
            if isinstance(raw, list):
                pats = _compile(raw, key)
    except (OSError, yaml.YAMLError) as e:
        log.warning("build_env_errors not loaded from %s: %s — using the built-in patterns", path, e)
    if not pats:
        pats = _compile(list(BUILTIN_FILE_LOCK), "the built-in list")
    _CACHE[key] = pats
    return pats


def clear_cache() -> None:
    """패턴 캐시를 비운다(테스트용, 규칙 파일을 바꾼 뒤)."""
    _CACHE.clear()


def file_lock_hit(text: str, cfg: Any = None) -> str | None:
    """로그 글에 파일 잠금이 보이면 잠긴 경로(``target`` 이 든 경로, 못 찾으면 ``?``), 아니면 None. 예외 없음.

    The locked path when `text` shows a Windows file lock (the first path with a `target` directory on the matching
    line, `?` when the line names none), None when no file-lock pattern matches. Never raises."""
    if not text:
        return None
    try:
        for rx in file_lock_patterns(cfg):
            m = rx.search(text)
            if not m:
                continue
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            line = text[start:end if end >= 0 else len(text)]
            p = _TARGET_PATH_RE.search(line)
            return p.group(0).rstrip(".,)") if p else "?"
    except Exception as e:  # noqa: BLE001 - a matcher problem means "not a file lock", never a failed build step
        log.debug("file lock check failed: %s: %s", type(e).__name__, e)
    return None


__all__ = ["BUILTIN_FILE_LOCK", "ENV_ERRORS_FILE", "clear_cache", "file_lock_hit", "file_lock_patterns"]
