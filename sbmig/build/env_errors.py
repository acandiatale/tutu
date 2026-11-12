"""프로젝트가 아니라 빌드 환경 때문에 생긴 실패를 빌드 로그에서 찾아내는 모듈.
- Windows의 ``target/`` 파일 잠금을 찾는다.
  백신, 인덱서, IDE, 남아 있는 java 프로세스가 방금 쓴 jar를 잡고 있는 경우다
- ``MavenRunner.run``의 재시도(``build.file_lock_retries``), 트리아지의 ``file_lock`` 차단, ``error_stage``
  판단이 ``file_lock_hit``을 쓴다
- 패턴은 ``knowledge/config/build_env_errors.yml``의 ``file_lock:``
  목록에서 읽고, 파일이 없거나 비어 있으면 내장 목록을 쓴다
- Maven의 HTTP 저장소 차단도 찾는다(``http_blocker_hit``). ``Blocked mirror for repositories`` 문구나
  ``http://0.0.0.0`` 주소가 있어야 차단으로 보며, ``maven-default-http-blocker`` id만으로는 차단이 아니다
- HTTP 차단은 sbmig가 고칠 수 없는 환경 문제이므로 실행을 ``aborted:http-blocked``로 중단하고 settings 수정
  방법을 안내한다. 사전 점검은 ``active_http_blockers``로 경고만 한다
- 예외를 던지지 않는다

Build-environment errors (v2.1r, docs/INTERFACES.md "v2.1r"): failures caused by the build machine, not the project —
for now Windows file locks on the build output (`target/`): an antivirus / indexer / IDE / a lingering JVM holding a
freshly written jar. `file_lock_hit` tells whether a log text shows one (and which path); the regexes come from
`knowledge/config/build_env_errors.yml` (`file_lock:`), the built-in `BUILTIN_FILE_LOCK` when that file is missing,
unreadable or empty. Never raises.

v2.1z-ah: Maven's HTTP repository blocker (Maven >= 3.8.1, mirror `maven-default-http-blocker` in
`<maven home>/conf/settings.xml`, or any mirror with `<blocked>true</blocked>`): `http_blocker_hit` needs evidence of a
block — Maven's `Blocked mirror for repositories: [...]` text (the blocked repositories are read from it, credentials
removed) or the built-in blocker's pseudo address `http://0.0.0.0`; the id `maven-default-http-blocker` alone is not
evidence (a settings file may redefine it as an ordinary mirror). `http_blocked_reason` /
`http_blocked_followup` say what to change in the Maven settings, `active_http_blockers` checks the settings files and
the poms before any build (preflight warning only)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    """파일 잠금을 찾는 정규식 목록을 반환한다.
    잘못된 정규식은 경고하고 건너뛰며, 파일이 없거나 비어 있으면 내장 목록을 쓴다. 파일 경로별로 캐시한다.

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
    """패턴 캐시를 비운다. 테스트에서 규칙 파일을 바꾼 뒤 호출한다."""
    _CACHE.clear()


def file_lock_hit(text: str, cfg: Any = None) -> str | None:
    """로그에서 파일 잠금이 보이면 잠긴 경로를, 아니면 None을 반환한다.
    경로를 찾지 못하면 ``?``를 반환하고, 예외를 던지지 않는다.

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


# ---------------------------------------------------------------------------------------------- v2.1z-ah: HTTP blocker
HTTP_BLOCKER_ID = "maven-default-http-blocker"     # the mirror id Maven >= 3.8.1 ships in <maven home>/conf/settings.xml
HTTP_BLOCKED_FLAG = "aborted:http-blocked"
HTTP_BLOCKED_KIND = "http-blocked"                 # `infra:http-blocked`
# the Handover line starts with this (sbmig/report/handover.py keeps the same string — the report does not import build)
HTTP_BLOCKED_PREFIX = "Maven (3.8.1+) refuses the HTTP repositor"
# Maven 3.8 / 3.9: `Could not transfer artifact g:a:pom:v from/to maven-default-http-blocker (http://0.0.0.0/): Blocked
# mirror for repositories: [id (http://host/path, default, releases+snapshots), id2 (…, default, snapshots)]`
_BLOCKED_LIST_RE = re.compile(r"(?:from/to (?P<mid>[^\s()]+) \((?P<murl>[^()\s]*)\): )?"
                              r"Blocked mirror for repositories: \[(?P<list>[^\]\r\n]*)\]")
_BLOCKED_ENTRY_RE = re.compile(r"(?P<id>[^\s,()\[\]]+) \((?P<url>[^,()\s]+)(?:,[^()]*)?\)")
# Maven's built-in blocker points at the pseudo address http://0.0.0.0/. 빌드 로그 전체를 검사하므로 앱이
# 출력한 `http://0.0.0.0:8080` 같은 줄은 차단으로 보지 않도록 Maven의 문장 형태만 본다:
# `from/to <id> (http://0.0.0.0/)`, `in <id> (http://0.0.0.0/)`, `was not found in | failed to transfer from http://0.0.0.0/
# during a previous attempt` (the whole build log is searched, so only Maven's own message shapes count)
_BLOCKER_URL_RE = re.compile(r"(?:from/to|\bin) [^\s()]+ \(https?://0\.0\.0\.0/?\)"
                             r"|(?:was not found in|failed to transfer from) https?://0\.0\.0\.0/? during a previous attempt")
# the mirror id next to that pseudo address: `from/to <id> (http://0.0.0.0/)`, `in <id> (http://0.0.0.0/)`
_BLOCKER_URL_ID_RE = re.compile(r"(?:from/to|\bin) (?P<mid>[^\s()]+) \(https?://0\.0\.0\.0/?\)")
# the cached-failure form: `was not found in http://0.0.0.0/ … until the update interval of <id> has elapsed`
_CACHED_ID_RE = re.compile(r"update interval of (?P<mid>[^\s()]+) has elapsed")


@dataclass(frozen=True)
class BlockedRepository:
    """Maven이 차단 미러 때문에 거부한 저장소 하나의 id와, 자격 증명을 뺀 URL을 담는 클래스.

    One repository Maven refused through a blocked mirror (id, URL without credentials)."""
    id: str
    url: str


@dataclass
class HttpBlockerHit:
    """로그에서 읽은 HTTP 차단 정보를 담는 클래스.
    차단 미러 id와 거부된 저장소 목록을 담으며, 로그에 저장소 이름이 없으면 목록은 비어 있다.

    Maven's HTTP blocker as a log shows it: the blocking mirror's id and the refused repositories (empty when the log
    names none)."""
    mirror_id: str = HTTP_BLOCKER_ID
    repositories: list[BlockedRepository] = field(default_factory=list)

    def repositories_text(self) -> str:
        """거부된 저장소를 `` `id` (url), `id2` (url2)`` 형식의 문자열로 반환한다.
        이름이 없으면 ``(not named in the log)``를 반환한다."""
        if not self.repositories:
            return "(not named in the log)"
        return ", ".join(f"`{r.id}` ({r.url})" for r in self.repositories)

    def ids_text(self) -> str:
        """거부된 저장소 id를 `` `id`, `id2` `` 형식의 문자열로 반환한다.
        이름이 없으면 ``that repository``를 반환한다."""
        return ", ".join(f"`{r.id}`" for r in self.repositories) or "that repository"

    def as_json(self) -> dict[str, Any]:
        """저널에 기록할 dict를 만든다. 자격 증명은 넣지 않는다."""
        return {"mirror": self.mirror_id, "repositories": [{"id": r.id, "url": r.url} for r in self.repositories]}


def strip_userinfo(url: str) -> str:
    """로그, 보고서, 저널에 자격 증명이 남지 않도록 URL에서 ``user:password@`` 부분을 지운다.

    The URL without its `user:password@` part (never echo credentials)."""
    u = (url or "").strip()
    try:
        sp = urlsplit(u)
        if sp.username is not None or sp.password is not None or "@" in sp.netloc:
            host = sp.netloc.rsplit("@", 1)[-1]
            return urlunsplit((sp.scheme, host, sp.path, sp.query, sp.fragment))
        return u
    except Exception:  # noqa: BLE001 - a URL that does not parse: drop everything up to the last @
        return re.sub(r"//[^/@\s]*@", "//", u)


def http_blocker_hit(text: str) -> HttpBlockerHit | None:
    """Maven이 실제로 저장소를 막았다는 증거가 있을 때만 차단 미러와 거부된 저장소를 반환하고, 아니면 None을
    반환한다. 예외를 던지지 않는다.
    회사 settings는 ``maven-default-http-blocker`` id를 실제 Nexus를 가리키는 일반 미러로 다시 정의하므로,
    평범한 아티팩트 누락에도 그 id가 나오기 때문이다.

    The blocker Maven reported, only with evidence that Maven actually blocked: the phrase `Blocked mirror for repositories:
    [...]` (any mirror id), or the built-in blocker's pseudo address `http://0.0.0.0` (the transfer / cached-failure forms
    without the phrase). The bare id `maven-default-http-blocker` is not evidence: a settings file may redefine that id as
    an ordinary mirror with a real URL, and Maven names it in every plain resolution failure. Returns the mirror id and the
    refused repositories of every occurrence (deduplicated by id, credentials removed); None when the text shows no block.
    Never raises."""
    if not text:
        return None
    try:
        phrase = "Blocked mirror for repositories" in text
        if not phrase and not _BLOCKER_URL_RE.search(text):
            return None
        hit = HttpBlockerHit()
        mid = ""
        seen: set[str] = set()
        for m in _BLOCKED_LIST_RE.finditer(text):
            mid = mid or (m.group("mid") or "")
            for e in _BLOCKED_ENTRY_RE.finditer(m.group("list") or ""):
                rid = e.group("id")
                if rid not in seen:
                    seen.add(rid)
                    hit.repositories.append(BlockedRepository(rid, strip_userinfo(e.group("url"))))
        if not mid:
            # 다른 줄에 나온 일반 미러 id를 차단 미러로 착각하지 않도록 가짜 주소가 있는 줄에서만 id를 읽는다
            for line in text.splitlines():
                if not _BLOCKER_URL_RE.search(line):
                    continue
                m2 = _BLOCKER_URL_ID_RE.search(line) or _CACHED_ID_RE.search(line)
                if m2:
                    mid = m2.group("mid")
                    break
        hit.mirror_id = mid or HTTP_BLOCKER_ID
        return hit
    except Exception as e:  # noqa: BLE001 - a matcher problem means "no blocker", never a failed step
        log.debug("http blocker check failed: %s: %s", type(e).__name__, e)
        return None


def _user_settings(cfg: Any) -> tuple[Path, str]:
    """Maven이 쓰는 사용자 settings 파일을 반환한다.
    ``build.settings_file``(``-s``)을 먼저 쓰고, 없으면 ``~/.m2/settings.xml``을 쓴다."""
    sf = getattr(getattr(cfg, "build", None), "settings_file", None) if cfg is not None else None
    if sf and str(sf).strip():
        return Path(str(sf)).expanduser(), "build.settings_file"
    return Path.home() / ".m2" / "settings.xml", "default"


def _global_settings(cfg: Any) -> Path | None:
    try:
        from .repo_versions import global_settings_file
        return global_settings_file(cfg)
    except Exception as e:  # noqa: BLE001
        log.debug("global settings not resolved: %s", e)
        return None


def _blocker_origin(cfg: Any, mirror_id: str) -> str:
    """차단 미러를 정의한 settings 파일을 찾는다.
    사용자, 전역 settings 순으로 찾으며, Maven 기본 차단 미러이면 전역 settings로 본다."""
    try:
        from .repo_versions import read_settings
        user, _o = _user_settings(cfg)
        g = _global_settings(cfg)
        for f in (user, g):
            d = read_settings(str(f)) if f is not None else None
            if d is not None and any(mid == mirror_id for mid, _u, _mo, _c in d.mirrors):
                return str(f)
        if mirror_id == HTTP_BLOCKER_ID:
            return str(g) if g is not None else "<maven home>/conf/settings.xml"
    except Exception as e:  # noqa: BLE001
        log.debug("blocker origin: %s", e)
    return "<maven home>/conf/settings.xml" if mirror_id == HTTP_BLOCKER_ID else "a settings file Maven reads"


def http_blocked_reason(hit: HttpBlockerHit) -> str:
    """Maven이 HTTP 저장소를 차단 미러로 거부했다는 문장을 만든다. 중단 사유와 조치 요약이 같은 문장을 쓴다.

    One sentence: Maven (3.8.1+) refuses the HTTP repository through the blocked mirror — the project and sbmig are not the
    cause (the abort reason and the remediation summary use the same words)."""
    plural = "ies" if len(hit.repositories) > 1 else "y"
    return (f"{HTTP_BLOCKED_PREFIX}{plural} {hit.repositories_text()} via the blocked mirror `{hit.mirror_id}` — "
            "not a project or sbmig problem")


def http_blocked_followup(hit: HttpBlockerHit, cfg: Any = None) -> str:
    """인수인계에 넣을 한 줄을 만든다. 이유와 고칠 방법(https URL로 바꾸기, 차단 미러의 mirrorOf에
    ``,!<저장소 ID>`` 추가)과 차단 미러를 정의한 파일을 담는다.
    한국어 문구는 ``sbmig/report/i18n.py``의 PATTERNS에 있으므로 함께 바꿔야 한다.

    The Handover line: the reason, what to change (a) an https URL, (b) in the settings file Maven uses exempt the
    repository from the blocking mirror (`,!<id>` in its mirrorOf — the company's own way), or add a mirror for the repository with `<blocked>false</blocked>`, and
    where the blocker comes from. Korean in `sbmig/report/i18n.py` PATTERNS (change both)."""
    user, origin = _user_settings(cfg)
    return (f"{http_blocked_reason(hit)}. Fix the Maven settings: (a) an https URL, or (b) in {user} ({origin}) exempt "
            f"{hit.ids_text()} from mirror `{hit.mirror_id}` (append `,!<repository id>` to its `<mirrorOf>`), or add a mirror "
            f"for {hit.ids_text()} (same URL, `<blocked>false</blocked>`); blocker from {_blocker_origin(cfg, hit.mirror_id)}")


@dataclass(frozen=True)
class ActiveBlocker:
    """사전 점검에서 찾은 활성 차단 하나를 담는 클래스.
    저장소, 그 저장소를 가리는 차단 미러, 미러를 정의한 파일을 담는다.

    A repository the effective Maven settings send to a blocked mirror: repository id / URL / where declared, the mirror id
    and the settings file that defines it."""
    repo_id: str
    url: str
    declared: str
    mirror_id: str
    mirror_origin: str


def _select_mirror(mirrors: list[tuple[str, str, bool, str]], repo_id: str, repo_url: str
                   ) -> tuple[str, str, bool, str] | None:
    """Maven의 규칙대로 저장소에 적용될 미러를 고른다.
    mirrorOf가 저장소 id와 정확히 같은 미러를 먼저 고르고, 없으면 순서대로 처음 패턴이 맞는 미러를 고른다.

    Maven's mirror selection: a mirror whose mirrorOf equals the repository id exactly wins, else the first pattern match."""
    from .repo_versions import mirror_covers
    exact = next((m for m in mirrors if m[1].strip() == repo_id), None)
    if exact is not None:
        return exact
    return next((m for m in mirrors if mirror_covers(m[1], repo_id, repo_url)), None)


def active_http_blockers(cfg: Any, project_root: Path | None = None) -> list[ActiveBlocker]:
    """Maven이 실제로 쓸 settings에서 차단 미러에 가려지는 ``http://`` 저장소 목록을 반환한다.
    사전 점검에서 쓰며, 예외를 던지지 않는다.
    미러 병합과 선택은 Maven과 같은 규칙을 쓰고, 읽지 못한 파일은 빼고 판단한다.

    The `http://` repositories (settings active-profile repositories / pluginRepositories, the project poms' repositories)
    that the effective Maven settings route to a blocked mirror: mirrors merged like Maven (user entries first, a global
    mirror whose id the user settings redefine is dropped), selected like Maven (exact id first, then the first pattern
    match). Never raises."""
    out: list[ActiveBlocker] = []
    try:
        from .repo_versions import is_blocking_mirror, pom_repositories, read_settings
        user, _o = _user_settings(cfg)
        files = [user]
        g = _global_settings(cfg)
        if g is not None and (not user.exists() or g.resolve() != user.resolve()):
            files.append(g)
        mirrors: list[tuple[str, str, bool, str]] = []      # (id, mirrorOf, blocked, origin) — merged like Maven
        repos: list[tuple[str, str, str]] = []               # (id, url, declared)
        ids: set[str] = set()
        for f in files:
            d = read_settings(str(f))
            if d is None:
                continue
            for mid, url, mo, _c in d.mirrors:
                if mid and mid in ids:
                    continue                                  # the user settings redefined this id (Maven's merge)
                ids.add(mid)
                mirrors.append((mid, mo, is_blocking_mirror(url, mid in d.blocked), str(f)))
            for rid, url, _c in [*d.repos, *getattr(d, "plugin_repos", [])]:
                repos.append((rid, url, f"settings {f}"))
        for rid, url, origin in pom_repositories(project_root):
            repos.append((rid, url, origin))
        seen: set[tuple[str, str]] = set()
        for rid, url, declared in repos:
            if not url.lower().startswith("http://") or (rid, url) in seen:
                continue
            seen.add((rid, url))
            sel = _select_mirror(mirrors, rid, url)
            if sel is not None and sel[2]:
                out.append(ActiveBlocker(rid, strip_userinfo(url), declared, sel[0], sel[3]))
    except Exception as e:  # noqa: BLE001 - a preflight check never fails a run
        log.debug("http blocker preflight failed: %s: %s", type(e).__name__, e)
    return out


__all__ = ["BUILTIN_FILE_LOCK", "ENV_ERRORS_FILE", "HTTP_BLOCKED_FLAG", "HTTP_BLOCKED_KIND", "HTTP_BLOCKED_PREFIX",
           "HTTP_BLOCKER_ID", "ActiveBlocker", "BlockedRepository", "HttpBlockerHit", "active_http_blockers",
           "clear_cache", "file_lock_hit", "file_lock_patterns", "http_blocked_followup", "http_blocked_reason",
           "http_blocker_hit", "strip_userinfo"]
