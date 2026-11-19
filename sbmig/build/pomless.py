"""저장소에 jar만 있고 pom이 없는 라이브러리 때문에 Maven이 멈췄을 때 로컬 저장소에 최소 pom을 만들고 같은 명령을 다시 실행하는 모듈.
의존성을 해석하는 모든 Maven 호출이 ``run_with_stubs``를 거친다(빌드, OpenRewrite, effective-pom, dependency:tree, 취약점 조치).
- 실패 로그에서 pom 전송 오류로 읽지 못한 jar 의존성(분류자 없음)만 찾는다
- jar는 어느 저장소에서든 받을 수 있고 pom은 어느 저장소에서도 받지 못할 때만 파일을 만든다
- 있는 파일은 덮어쓰지 않고, 라이브러리마다 실행당 한 번, ``build.pomless_stub_max``개까지만 만든다
- 프로젝트 파일은 바꾸지 않는다

v2.1z-ax — a library whose repository has the jar but no pom (docs/INTERFACES.md "v2.1z-ax").

Measured with Maven 3.9.10: Maven asks every repository for the pom of a dependency; when one of them answers with a
transfer error (a Central proxy in a closed network answers 502 for a path it never cached, a refused connection) the
build stops with `Failed to read artifact descriptor for G:A:jar:V` caused by `Could not transfer artifact G:A:pom:V
from/to <id> (<url>)`. Maven does not cache transfer errors, so a repeat in the same local repository prints the same
form again (the resolver's "failed to transfer … during a previous attempt" form is accepted as well). With a pom file
at `<local repository>/<g path>/<a>/<v>/<a>-<v>.pom` Maven asks no repository for the descriptor and takes the jar from
the repository that has it (either repository order; a leftover `.pom.lastUpdated` does not matter).

`run_with_stubs(cfg, run, journal=, memo=)` runs `run(0)`; while the result failed (not timed out) and its log names
such libraries, each is proven (`prove`: the jar is obtainable from a repository or already in the local repository,
and no repository returned the pom) and gets a stub (`write_stub`), then `run(k)` runs the same command again.
Journal `pomless_check {ga, version, reason}` (no stub) / `pomless_stub {ga, version, path, jar_from, created}`.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TypeVar

from .env_errors import http_blocker_hit, strip_userinfo
from .mvn import maven_global_settings

log = logging.getLogger("sbmig.build.pomless")

R = TypeVar("R")

FLAG = "pomless-library"
# 최소 pom의 주석 첫 문장이다. 쓰는 쪽과 나중 실행의 확인이 같은 문구를 쓴다
STUB_MARKER = "Written by sbmig: the repository has a jar but no pom for"
HEAD_BYTES = 512                 # 최소 pom인지 볼 때 읽는 파일 앞부분의 크기
MAX_USED_CHECKS = 300            # 나중 실행이 확인하는 의존성 좌표 수의 상한
# 좌표 한 부분에 허용하는 문자다. 파일 경로에 들어가므로 경로 구분자와 제어 문자를 막는다
_PART_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_COORD = r"[A-Za-z0-9_.\-]+"
_DESCRIPTOR_RE = re.compile(r"Failed to read artifact descriptor for (?P<c>" + _COORD + r"(?::" + _COORD + r"){2,3})")
# 플러그인과 빌드 확장의 해석 실패는 의존성이 아니므로 직전 줄의 이 문구로 거른다
_PLUGIN_RE = re.compile(r"Plugin \S+ or one of its dependencies could not be resolved|build extension", re.IGNORECASE)
_CONTEXT_LINES = 3


@dataclass
class PomlessMemo:
    """한 실행에서 처리한 라이브러리와 만든 최소 pom을 기억하는 클래스.

    Per run: the libraries already handled (stubbed or refused, never again) and the stubs used."""
    handled: set[str] = field(default_factory=set)
    stubs: list[dict[str, Any]] = field(default_factory=list)
    lookup: Any = None


def memo_for(holder: Any) -> PomlessMemo:
    """``holder``(Workspace 등)에 붙은 ``PomlessMemo``를 반환하고, 없으면 만들어 붙인다.

    The run's memo, kept on the holder (the Workspace, the remediation's ServerMaven)."""
    memo = holder.__dict__.get("_pomless_memo")
    if not isinstance(memo, PomlessMemo):
        memo = PomlessMemo()
        holder.__dict__["_pomless_memo"] = memo
    return memo


def plain_coordinate(group_id: str, artifact_id: str, version: str) -> bool:
    """좌표가 파일 경로와 XML 주석에 넣어도 되는 평범한 Maven 좌표인지 확인한다.

    No `/`, `\\`, `..`, control characters or `--` (the stub carries an XML comment); a plain Maven coordinate only."""
    for part in (group_id, artifact_id, version):
        if not part or not _PART_RE.match(part) or ".." in part or "--" in part:
            return False
    return True


def _transfer_res(g: str, a: str, v: str) -> list[re.Pattern[str]]:
    pom = re.escape(f"{g}:{a}:pom:{v}")
    return [re.compile(r"Could not transfer artifact " + pom + r" from/to "),
            re.compile(pom + r" failed to transfer from \S+ during a previous attempt")]


def detect(text: str) -> list[tuple[str, str, str]]:
    """실패한 Maven 로그에서 pom 전송 오류 때문에 정보를 읽지 못한 jar 의존성의 (g, a, v) 목록을 반환한다.
    부모 pom, import BOM, 플러그인, 빌드 확장, 분류자나 jar가 아닌 타입, HTTP 차단 미러는 넣지 않는다.

    `Failed to read artifact descriptor for G:A:jar:V` (exactly a jar without classifier) whose cause — on the same
    line (Maven 3.8) or within the next lines (3.9: `Caused by: The following artifacts could not be resolved: G:A:pom:V
    (absent): Could not transfer artifact G:A:pom:V from/to …`) — is a transfer error on that library's pom. A
    descriptor line under a plugin / build extension failure is skipped, and so is a transfer to Maven's HTTP blocker.
    `Non-resolvable parent / import POM` and `Could not find artifact` never carry a descriptor line."""
    lines = (text or "").splitlines()
    out: list[tuple[str, str, str]] = []
    for i, line in enumerate(lines):
        m = _DESCRIPTOR_RE.search(line)
        if not m:
            continue
        parts = m.group("c").split(":")
        if len(parts) != 4 or parts[2] != "jar":
            continue
        g, a, _t, v = parts
        before = " ".join(lines[max(0, i - 2):i]) + " " + line[:m.start()]
        if _PLUGIN_RE.search(before):
            continue
        window = [line[m.end():]]
        for nxt in lines[i + 1:i + 1 + _CONTEXT_LINES]:
            if _DESCRIPTOR_RE.search(nxt) or "Non-resolvable" in nxt:
                break
            window.append(nxt)
        cause = next((w for w in window if any(rx.search(w) for rx in _transfer_res(g, a, v))), None)
        if cause is None or http_blocker_hit(cause) is not None:
            continue
        if (g, a, v) not in out:
            out.append((g, a, v))
    return out


def _settings_local_repo(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.is_file():
        return None
    try:
        m = re.search(r"<localRepository>\s*([^<]*?)\s*</localRepository>",
                      path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1) if m and m.group(1) else None


def _interpolate(value: str) -> Optional[str]:
    value = value.replace("${user.home}", str(Path.home()))
    value = re.sub(r"\$\{env\.([A-Za-z0-9_.]+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value)
    return None if "${" in value else value


def maven_local_repo(cfg: Any) -> Optional[Path]:
    """이번 Maven 명령이 실제로 쓰는 로컬 저장소 경로를 반환한다. 알 수 없으면 None을 반환한다.
    ``build.local_repo`` -> ``build.extra_args``의 ``-Dmaven.repo.local`` -> 사용자 settings -> 전역 settings -> ``~/.m2/repository`` 순서다.

    Maven's own order for this run: `-Dmaven.repo.local` on the command line (`build.local_repo`, which
    `base_command` passes last, else one in `build.extra_args`), else `<localRepository>` of the user settings (the
    `-s` file when `build.settings_file` is set — Maven then does not read `~/.m2/settings.xml` — else
    `~/.m2/settings.xml`), else of the global settings (`<maven home>/conf/settings.xml`), else `~/.m2/repository`.
    `${user.home}` / `${env.X}` are resolved; anything else left unresolved → None (no stub)."""
    b = cfg.build
    if b.local_repo and str(b.local_repo).strip():
        return Path(str(b.local_repo)).expanduser().absolute()
    for arg in reversed(list(getattr(b, "extra_args", []) or [])):
        if str(arg).startswith("-Dmaven.repo.local="):
            v = _interpolate(str(arg).split("=", 1)[1])
            return Path(v).expanduser().absolute() if v else None
    user = (Path(str(b.settings_file)).expanduser() if b.settings_file and str(b.settings_file).strip()
            else Path.home() / ".m2" / "settings.xml")
    raw = _settings_local_repo(user)
    if raw is None:
        try:
            raw = _settings_local_repo(maven_global_settings(cfg))
        except Exception:  # Maven 설치 위치를 몰라도 기본 경로를 쓰면 되므로 넘어간다
            raw = None
    if raw is None:
        return Path.home() / ".m2" / "repository"
    v = _interpolate(raw)
    return Path(v).expanduser().absolute() if v else None


def stub_text(group_id: str, artifact_id: str, version: str) -> str:
    """최소 pom의 내용을 반환한다. 의존성은 넣지 않는다.

    The minimal pom: coordinates, `packaging` jar, no dependencies (what Maven itself assumes for a missing pom), and
    an XML comment saying who wrote it, why, and that it must be deleted once a real pom is uploaded."""
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            f"<!-- {STUB_MARKER} {group_id}:{artifact_id}:{version}, and a\n"
            "     repository that failed to answer for the pom stopped Maven. This minimal pom declares no dependencies\n"
            "     (what Maven itself assumes for a missing pom). Delete this file when the real pom is uploaded to the\n"
            "     repository. -->\n"
            "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            f"  <groupId>{group_id}</groupId>\n"
            f"  <artifactId>{artifact_id}</artifactId>\n"
            f"  <version>{version}</version>\n"
            "  <packaging>jar</packaging>\n"
            "</project>\n")


def write_stub(path: Path, group_id: str, artifact_id: str, version: str) -> bool:
    """최소 pom을 같은 폴더의 임시 파일에 쓴 뒤 덮어쓰지 않는 방식으로 옮긴다. 새로 만들었으면 True를 반환한다.
    파일이 이미 있거나 그사이 다른 파드가 만들었으면 False를 반환한다.

    Atomic and never over an existing file: temp file in the same folder, then a hard link to the final name (fails
    when the name exists — another build pod on the same NFS volume won) and the temp removed. A file system without
    hard links falls back to a rename after an existence check. Returns True when this call created the file."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.sbmig-{os.getpid()}-{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(stub_text(group_id, artifact_id, version))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            # 하드 링크를 지원하지 않는 파일 시스템이면 있는지 확인한 뒤 이름을 바꾼다
            if path.exists():
                return False
            os.replace(tmp, path)
            return True
    finally:
        tmp.unlink(missing_ok=True)


def _lookup(cfg: Any, memo: PomlessMemo, project_root: Optional[Path]) -> Any:
    if memo.lookup is None:
        from . import repo_versions  # 테스트가 모듈의 RepositoryLookup을 오프라인 가짜로 바꾼다
        try:
            memo.lookup = repo_versions.RepositoryLookup(cfg, project_root=project_root)
        except TypeError:
            memo.lookup = repo_versions.RepositoryLookup(cfg)
    return memo.lookup


def prove(lookup: Any, local_repo: Path, group_id: str, artifact_id: str, version: str) -> tuple[Optional[str], str]:
    """jar는 받을 수 있고 pom은 어느 저장소에서도 받지 못하는지 확인한다.
    (jar를 받은 곳, "")이나 (None, 이유)를 반환한다.

    The rule: no repository returned the pom (200) — `missing`, `denied` (401 / 403: the same credentials Maven uses,
    so Maven cannot read it either) and `failed` all count as "not returned" — AND the jar is in the local repository or
    one repository answered `ok` for it (`denied` / `missing` / `failed` do not count). Returns (jar source, "") or
    (None, reason) with reason `no-repository`, `pom-available`, `jar-unavailable`. A lookup without `file_answers` (the
    offline test fake) is `no-repository`."""
    urls = lookup.repository_urls() if hasattr(lookup, "repository_urls") else []
    if not urls or not hasattr(lookup, "file_answers"):
        return None, "no-repository"
    poms = lookup.file_answers(group_id, artifact_id, version, "pom")
    if any(o == "ok" for _u, o, _d in poms):
        return None, "pom-available"
    jar = local_repo.joinpath(*group_id.split("."), artifact_id, version, f"{artifact_id}-{version}.jar")
    if jar.is_file():
        return "local repository", ""
    jars = lookup.file_answers(group_id, artifact_id, version, "jar")
    hit = next((u for u, o, _d in jars if o == "ok"), None)
    return (strip_userinfo(hit), "") if hit else (None, "jar-unavailable")


def _journal(journal: Optional[Callable[[dict], None]], event: dict) -> None:
    if journal is None:
        return
    try:
        journal(event)
    except Exception:  # 저널 기록 실패가 빌드를 멈추지 않게 한다
        log.debug("pomless journal line not written", exc_info=True)


def _stub_round(cfg: Any, text: str, *, journal: Optional[Callable[[dict], None]], memo: PomlessMemo,
                project_root: Optional[Path], lookup: Any) -> int:
    """로그에 나온 라이브러리마다 확인하고 최소 pom을 둔다. 이번에 준비한 파일 수를 반환한다."""
    ready = 0
    on = bool(getattr(cfg.build, "pomless_stub", True))
    cap = max(0, int(getattr(cfg.build, "pomless_stub_max", 20) or 0))
    for g, a, v in detect(text):
        gav = f"{g}:{a}:{v}"
        if gav in memo.handled:
            continue
        memo.handled.add(gav)
        check = {"event": "pomless_check", "ga": f"{g}:{a}", "version": v}
        if not on:
            _journal(journal, {**check, "reason": "off"})
            continue
        if not plain_coordinate(g, a, v):
            log.warning("maven: %s has no readable pom, but its coordinates are not a plain Maven coordinate — no "
                        "minimal pom written", gav)
            _journal(journal, {**check, "reason": "bad-coordinate"})
            continue
        if len(memo.stubs) >= cap:
            _journal(journal, {**check, "reason": "cap"})
            continue
        repo = maven_local_repo(cfg)
        if repo is None:
            _journal(journal, {**check, "reason": "no-repository"})
            continue
        path = repo.joinpath(*g.split("."), a, v, f"{a}-{v}.pom")
        if path.exists():
            jar_from, created = "", False
        else:
            jar_from, why = prove(lookup if lookup is not None else _lookup(cfg, memo, project_root), repo, g, a, v)
            if jar_from is None:
                log.info("maven: %s — the descriptor could not be read; no minimal pom written (%s)", gav, why)
                _journal(journal, {**check, "reason": why})
                continue
            try:
                created = write_stub(path, g, a, v)
            except OSError as e:
                log.warning("maven: minimal pom for %s not written (%s: %s)", gav, type(e).__name__, e)
                _journal(journal, {**check, "reason": "no-repository"})
                continue
        stub = {"ga": f"{g}:{a}", "version": v, "path": str(path), "jar_from": jar_from, "created": created}
        memo.stubs.append(stub)
        _journal(journal, {"event": "pomless_stub", **stub})
        log.info("maven: %s has a jar but no pom in the repository — %s minimal pom %s (jar from %s); running the same "
                 "command again", gav, "wrote a" if created else "using the existing", path, jar_from or "the repository")
        ready += 1
    return ready


def run_with_stubs(cfg: Any, run: Callable[[int], R], *, journal: Optional[Callable[[dict], None]],
                   memo: PomlessMemo, project_root: Optional[Path] = None, lookup: Any = None,
                   text: Callable[[R], str] = lambda r: getattr(r, "text", "") or "",
                   failed: Callable[[R], bool] = lambda r: getattr(r, "exit_code", 0) != 0
                   and not getattr(r, "timed_out", False)) -> R:
    """Maven 명령을 실행하고, jar만 있는 라이브러리 때문에 실패했으면 최소 pom을 둔 뒤 같은 명령을 다시 실행한다.
    ``run(k)``의 k는 0이 첫 실행이고 1부터가 다시 실행한 횟수다. 마지막 결과를 반환한다.

    The one shared function behind every Maven call of sbmig that resolves dependencies. `run(0)` first; then, while
    the result `failed` and its log names libraries that got a stub in this round, `run(k)` again. Rounds per command are
    bounded by `build.pomless_stub_max` (each round needs a library not handled before in this run)."""
    result = run(0)
    rounds = max(0, int(getattr(cfg.build, "pomless_stub_max", 20) or 0))
    k = 0
    while failed(result) and k < max(1, rounds):
        if not _stub_round(cfg, text(result), journal=journal, memo=memo, project_root=project_root, lookup=lookup):
            break
        k += 1
        result = run(k)
    return result


def round_log_path(log_path: Path, k: int) -> Path:
    """k번째 다시 실행의 로그 경로를 반환한다(0이면 원래 경로).

    `<stem>-pomless<k><suffix>` beside the first log; k = 0 → the first log."""
    p = Path(log_path)
    return p if k == 0 else p.with_name(f"{p.stem}-pomless{k}{p.suffix}")


def run_logged_with_stubs(cfg: Any, cmd: list[str], *, cwd: Path, env: Any, log_path: Path, timeout_s: Optional[float],
                          journal: Optional[Callable[[dict], None]], memo: PomlessMemo,
                          project_root: Optional[Path] = None, runner: Optional[Callable[..., Any]] = None
                          ) -> tuple[Any, Path]:
    """``run_logged``로 명령을 실행하고 ``run_with_stubs`` 규칙으로 다시 실행한다. (마지막 결과, 그 로그 경로)를 반환한다.

    `run_logged` (or the caller's `runner`, so a module's own binding — and a test's patch of it — is used) behind
    `run_with_stubs`; round k logs to `round_log_path(log_path, k)`. Returns (last ProcessResult, its log path)."""
    if runner is None:
        from .mvn import run_logged as runner
    last = [Path(log_path)]

    def run(k: int) -> Any:
        last[0] = round_log_path(log_path, k)
        return runner(cmd, cwd=cwd, env=env, log_path=last[0], timeout_s=timeout_s)
    pr = run_with_stubs(cfg, run, journal=journal, memo=memo, project_root=project_root)
    return pr, last[0]


def is_stub(path: Path) -> bool:
    """파일 앞부분에 ``STUB_MARKER``가 있으면 sbmig가 만든 최소 pom으로 보고 True를 반환한다. 읽지 못하면 False다.

    Reads only the first `HEAD_BYTES` bytes; a missing / unreadable file (or a directory) is False, never an error."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return False
    return STUB_MARKER.encode("ascii") in head


def known_coordinates(ws: Any) -> list[tuple[str, str, str]]:
    """새 Maven 호출 없이 이번 실행이 아는 의존성 좌표 (g, a, v) 목록을 반환한다.
    이번 실행이 저장한 전체 dependency:tree 출력, effective pom 저장본의 직접 의존성, 지금 pom들의 선언 순서로 모은다.

    No Maven call. Sources, in this order (union, each coordinate once): (1) the unfiltered `dependency:tree`
    outputs this run captured (`logs/deptree-*.txt` whose `.meta.json` has no `includes`: transitives included);
    (2) the direct dependencies of the effective-pom captures (`logs/effective-*.json`, `direct` + `by_module`:
    versions as Maven resolved them, managed ones included); (3) the declared dependencies of the final project poms,
    versions interpolated from the project's own properties / management (`parse_poms`). Misses: a library reached
    only transitively when no unfiltered tree was captured; a version that only a parent / BOM outside the project
    sets when no effective capture saw the dependency. Never raises."""
    out: list[tuple[str, str, str]] = []

    def add(g: Any, a: Any, v: Any) -> None:
        if g and a and v and "${" not in str(v) and (str(g), str(a), str(v)) not in out:
            out.append((str(g), str(a), str(v)))
    try:
        import json

        from sbmig.core.workspace import logs_dir
        logs = logs_dir(ws)
        if logs.is_dir():
            from .deptree import parse_tree
            for meta in sorted(logs.glob("deptree-*.meta.json")):
                try:
                    if json.loads(meta.read_text(encoding="utf-8")).get("includes"):
                        continue
                    tree = meta.with_name(meta.name.removesuffix(".meta.json") + ".txt")
                    stack = list(parse_tree(tree.read_text(encoding="utf-8", errors="replace")))
                except (OSError, ValueError):
                    continue
                while stack:
                    node = stack.pop(0)
                    if not node.root and (node.gav.type or "jar") == "jar" and not node.gav.classifier:
                        add(node.gav.group_id, node.gav.artifact_id, node.gav.version)
                    stack.extend(node.children)
            for eff in sorted(logs.glob("effective-*.json")):
                try:
                    data = json.loads(eff.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                maps = [data.get("direct") or {}] + [(m or {}).get("direct") or {}
                                                     for m in (data.get("by_module") or {}).values()]
                for direct in maps:
                    for ga, v in direct.items():
                        g, _sep, a = str(ga).partition(":")
                        add(g, a, v)
    except Exception as e:  # noqa: BLE001 - 좌표를 못 모아도 실행은 계속한다
        log.debug("pomless: run files not read: %s: %s", type(e).__name__, e)
    try:
        from sbmig.analyze.pom_parser import parse_poms
        poms, _skipped = parse_poms(ws)
        for pom in poms:
            for d in pom.dependencies:
                if (d.type or "jar") == "jar" and not d.classifier:
                    add(d.group_id, d.artifact_id, d.version)
    except Exception as e:  # noqa: BLE001 - pom을 읽지 못해도 실행은 계속한다
        log.debug("pomless: project poms not read: %s: %s", type(e).__name__, e)
    return out


def stubs_used(cfg: Any, coords: list[tuple[str, str, str]], *, skip: Optional[set[str]] = None,
               limit: int = MAX_USED_CHECKS) -> list[dict[str, Any]]:
    """좌표마다 로컬 저장소의 pom이 sbmig가 만든 최소 pom인지 보고, 맞는 것의 {ga, version, path} 목록을 반환한다.
    ``limit``개 좌표까지만 확인한다.

    For at most `limit` coordinates (not in `skip`, plain only): `<local repository>/<g>/<a>/<v>/<a>-<v>.pom` exists
    and its head carries `STUB_MARKER`. One `stat` + one read of `HEAD_BYTES` per existing pom. Never raises."""
    skip = skip or set()
    repo = maven_local_repo(cfg)
    if repo is None:
        return []
    todo = [(g, a, v) for g, a, v in coords if f"{g}:{a}:{v}" not in skip and plain_coordinate(g, a, v)]
    if len(todo) > limit:
        log.info("pomless: %d dependency coordinates known; only the first %d were checked for a minimal pom of an "
                 "earlier run", len(todo), limit)
        todo = todo[:limit]
    found: list[dict[str, Any]] = []
    for g, a, v in todo:
        path = repo.joinpath(*g.split("."), a, v, f"{a}-{v}.pom")
        if is_stub(path):
            found.append({"ga": f"{g}:{a}", "version": v, "path": str(path)})
    return found


def handover_line(libraries: list[str]) -> str:
    """최소 pom으로 빌드한 라이브러리를 알리는 인수인계 문장(영어)을 반환한다.

    The one Handover line naming the libraries (`g:a:v`); true whether the stub was written in this run or an
    earlier one."""
    named = ", ".join(f"`{x}`" for x in libraries)
    return (f"libraries with a jar but no pom in the repository: {named} — the build used a minimal pom that sbmig "
            "wrote for each into the server's local Maven repository; Maven therefore does not know what these "
            "libraries themselves depend on (declare anything they need in the project), and the lasting fix is that "
            "the library's owner uploads its pom to the repository")
