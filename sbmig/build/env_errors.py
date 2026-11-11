"""빌드 환경 오류 판별(v2.1r) — 프로젝트가 아니라 빌드 PC 탓인 실패(지금은 Windows 의 ``target/`` 파일 잠금: 백신·
인덱서·IDE·남은 java 프로세스가 막 쓴 jar 를 붙잡음)를 로그 글에서 알아본다. 흐름상 위치: ``MavenRunner.run`` 이 실패한
빌드를 잠깐 기다렸다 다시 돌릴지(``build.file_lock_retries``), 트리아지(``sbmig.triage.blocked``)가 그 케이스를 LLM 없이
``file_lock`` 으로 차단할지, 오류 단계(``sbmig.errors.entries.error_stage``)가 그 오류를 '어느 단계도 증명하지 못함' 으로
볼지 정한다. 핵심 진입점: ``file_lock_hit(text, cfg=None)``, ``file_lock_patterns(cfg=None)``. 지킬 것: 패턴은
``knowledge/config/build_env_errors.yml`` 의 ``file_lock:`` 목록(회사가 자기 백신 문구를 더할 수 있게), 파일이 없거나
비면 내장 목록; 예외를 내지 않는다. v2.1z-ah: Maven(3.8.1 이상)의 HTTP 저장소 차단 미러(``maven-default-http-blocker`` /
``Blocked mirror for repositories``)도 여기서 알아본다(``http_blocker_hit``) — 프로젝트도 sbmig 도 고칠 수 없는 환경 문제라
실행을 한 번에 멈추고(``aborted:http-blocked``) 운영자에게 Maven settings 를 고치라고 말한다(``http_blocked_followup``);
사전 점검은 settings 에 그런 차단이 이미 걸려 있는지 본다(``active_http_blockers``, 경고만).

Build-environment errors (v2.1r, docs/INTERFACES.md "v2.1r"): failures caused by the build machine, not the project —
for now Windows file locks on the build output (`target/`): an antivirus / indexer / IDE / a lingering JVM holding a
freshly written jar. `file_lock_hit` tells whether a log text shows one (and which path); the regexes come from
`knowledge/config/build_env_errors.yml` (`file_lock:`), the built-in `BUILTIN_FILE_LOCK` when that file is missing,
unreadable or empty. Never raises.

v2.1z-ah: Maven's HTTP repository blocker (Maven >= 3.8.1, mirror `maven-default-http-blocker` in
`<maven home>/conf/settings.xml`, or any mirror with `<blocked>true</blocked>`): `http_blocker_hit` reads the blocked
repositories from Maven's `Blocked mirror for repositories: [...]` text (credentials removed), `http_blocked_reason` /
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
_BLOCKER_ID_RE = re.compile(re.escape(HTTP_BLOCKER_ID))


@dataclass(frozen=True)
class BlockedRepository:
    """Maven 이 차단 미러 때문에 거부한 저장소 하나(id, 자격증명을 뺀 URL).

    One repository Maven refused through a blocked mirror (id, URL without credentials)."""
    id: str
    url: str


@dataclass
class HttpBlockerHit:
    """로그에서 읽은 HTTP 차단: 차단 미러 id 와 거부된 저장소들(로그에 이름이 없으면 빈 목록).

    Maven's HTTP blocker as a log shows it: the blocking mirror's id and the refused repositories (empty when the log
    names none)."""
    mirror_id: str = HTTP_BLOCKER_ID
    repositories: list[BlockedRepository] = field(default_factory=list)

    def repositories_text(self) -> str:
        """`` `id` (url), `id2` (url2)`` — 이름이 없으면 ``(not named in the log)``."""
        if not self.repositories:
            return "(not named in the log)"
        return ", ".join(f"`{r.id}` ({r.url})" for r in self.repositories)

    def ids_text(self) -> str:
        """`` `id`, `id2` `` — 이름이 없으면 ``that repository``."""
        return ", ".join(f"`{r.id}`" for r in self.repositories) or "that repository"

    def as_json(self) -> dict[str, Any]:
        """저널용 dict(자격증명 없음)."""
        return {"mirror": self.mirror_id, "repositories": [{"id": r.id, "url": r.url} for r in self.repositories]}


def strip_userinfo(url: str) -> str:
    """URL 의 ``user:password@`` 를 지운다(로그·보고서·저널에 자격증명을 남기지 않게). 못 읽는 URL 은 ``@`` 앞을 지운다.

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
    """Maven 출력에 HTTP 차단 미러가 보이면(``Blocked mirror for repositories`` / ``maven-default-http-blocker``) 그 미러와
    거부된 저장소들(모든 줄에서 모아 id 기준 중복 제거, URL 의 자격증명은 지움), 아니면 None. 예외 없음.

    The blocker Maven reported (`Blocked mirror for repositories: [...]` or the `maven-default-http-blocker` id) with the
    refused repositories of every occurrence (deduplicated by id, credentials removed); None when the text shows none.
    Never raises."""
    if not text or ("Blocked mirror for repositories" not in text and HTTP_BLOCKER_ID not in text):
        return None
    try:
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
        if not mid and not hit.repositories and not _BLOCKER_ID_RE.search(text):
            return None
        hit.mirror_id = mid or HTTP_BLOCKER_ID
        return hit
    except Exception as e:  # noqa: BLE001 - a matcher problem means "no blocker", never a failed step
        log.debug("http blocker check failed: %s: %s", type(e).__name__, e)
        return None


def _user_settings(cfg: Any) -> tuple[Path, str]:
    """Maven 이 쓰는 사용자 settings: ``build.settings_file`` (``-s``), 없으면 ``~/.m2/settings.xml``."""
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
    """차단 미러가 어디서 오는지: 그 id 를 정의한 settings 파일(사용자 → 전역), 기본 차단기면 전역 settings
    (``<maven home>/conf/settings.xml``)."""
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
    """한 문장: Maven(3.8.1 이상)이 HTTP 저장소 ``<id>`` (``<url>``)를 차단 미러로 거부한다 — 프로젝트·sbmig 탓이 아니다.
    중단 사유·조치(remediation) 요약이 같은 문장을 쓴다.

    One sentence: Maven (3.8.1+) refuses the HTTP repository through the blocked mirror — the project and sbmig are not the
    cause (the abort reason and the remediation summary use the same words)."""
    plural = "ies" if len(hit.repositories) > 1 else "y"
    return (f"{HTTP_BLOCKED_PREFIX}{plural} {hit.repositories_text()} via the blocked mirror `{hit.mirror_id}` — "
            "not a project or sbmig problem")


def http_blocked_followup(hit: HttpBlockerHit, cfg: Any = None) -> str:
    """인수인계 한 줄(운영자가 복사 없이 읽는 글): 이유 + 고칠 곳 (a) https URL, (b) Maven 이 쓰는 settings 파일에서 차단 미러의
    mirrorOf 에 ``,!<저장소 ID>`` 예외를 붙이거나(회사 settings 가 쓰는 방식) 그 저장소용 미러를 ``<blocked>false</blocked>`` 로 추가 + 차단 미러의 출처.
    한국어는 ``sbmig/report/i18n.py`` 의 PATTERNS(바꾸면 함께).

    The Handover line: the reason, what to change (a) an https URL, (b) in the settings file Maven uses exempt the
    repository from the blocking mirror (`,!<id>` in its mirrorOf — the company's own way), or add a mirror for the repository with `<blocked>false</blocked>`, and
    where the blocker comes from. Korean in `sbmig/report/i18n.py` PATTERNS (change both)."""
    user, origin = _user_settings(cfg)
    return (f"{http_blocked_reason(hit)}. Fix the Maven settings: (a) an https URL, or (b) in {user} ({origin}) exempt "
            f"{hit.ids_text()} from mirror `{hit.mirror_id}` (append `,!<repository id>` to its `<mirrorOf>`), or add a mirror "
            f"for {hit.ids_text()} (same URL, `<blocked>false</blocked>`); blocker from {_blocker_origin(cfg, hit.mirror_id)}")


@dataclass(frozen=True)
class ActiveBlocker:
    """사전 점검이 찾은 활성 차단: 저장소(id, 자격증명 없는 URL, 선언한 곳)와 그것을 덮는 차단 미러(id, 정의한 파일).

    A repository the effective Maven settings send to a blocked mirror: repository id / URL / where declared, the mirror id
    and the settings file that defines it."""
    repo_id: str
    url: str
    declared: str
    mirror_id: str
    mirror_origin: str


def _select_mirror(mirrors: list[tuple[str, str, bool, str]], repo_id: str, repo_url: str
                   ) -> tuple[str, str, bool, str] | None:
    """Maven 의 미러 선택(DefaultMirrorSelector): mirrorOf 가 저장소 id 와 정확히 같은 미러가 먼저, 없으면 순서대로 첫 패턴 매칭.

    Maven's mirror selection: a mirror whose mirrorOf equals the repository id exactly wins, else the first pattern match."""
    from .repo_versions import mirror_covers
    exact = next((m for m in mirrors if m[1].strip() == repo_id), None)
    if exact is not None:
        return exact
    return next((m for m in mirrors if mirror_covers(m[1], repo_id, repo_url)), None)


def active_http_blockers(cfg: Any, project_root: Path | None = None) -> list[ActiveBlocker]:
    """사전 점검(v2.1z-ah): Maven 이 실제로 쓸 settings(사용자 = ``build.settings_file`` 또는 ``~/.m2/settings.xml``, 그리고 전역
    ``<maven home>/conf/settings.xml``)에서 차단 미러가 덮는 ``http://`` 저장소들 — settings 활성 프로필의 저장소·플러그인 저장소와
    프로젝트 pom 들의 ``<repositories>``. 미러는 Maven 처럼 합친다(사용자 것이 먼저, 같은 id 의 전역 미러는 사용자 것으로 대체), 선택도
    Maven 처럼(정확한 id 먼저, 그다음 순서대로 패턴). 확실할 때만 돌려준다 — 읽지 못한 파일이 있으면 그 파일 없이 판단한다. 예외 없음.

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
