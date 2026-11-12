"""접수 단계에서 배포 전송용 build extension을 pom에서 지우는 모듈.
Wagon 전송 확장은 ``mvn deploy``에만 쓰이지만 Maven은 어떤 goal보다 먼저 확장을 받으므로, 받지 못하면
``Unresolveable build extension``으로 실행 전체가 막힌다.
- 목록 파일(``intake.build_extensions_file``)에 있는 확장을 첫 Maven 호출 전에 모든 pom에서 지운다.
  프로필 안의 확장도 포함한다
- 목록에 없는 확장은 빌드에 쓰일 수 있으므로 그대로 두고 기록만 한다
- 지울 좌표는 코드가 아니라 목록 파일에 둔다

Remove deploy-transport build extensions at intake: Wagon providers in `<build><extensions>` only teach `mvn deploy` a URL
scheme, yet Maven resolves every extension while building the project model — before any goal, OpenRewrite included —
so an unresolvable one stops the whole run. The intake removes the extensions matched by the knowledge list
(`intake.build_extensions_file`) from every pom (profiles included) before the first Maven call; unlisted extensions take
part in the build and are only recorded."""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sbmig.analyze.pom_parser import find_pom_files

log = logging.getLogger("sbmig.analyze.build_extensions")


@dataclass(frozen=True)
class DeployTransport:
    """목록 파일의 한 줄을 담는 데이터 클래스.
    groupId는 정확히 일치해야 하고, artifactId는 셸 글롭 패턴으로 비교한다.

    One knowledge row: exact groupId, artifactId as a shell glob."""
    group: str
    artifact: str = "*"
    note: str = ""

    def matches(self, group_id: str | None, artifact_id: str | None) -> bool:
        return bool(group_id) and group_id == self.group and fnmatch.fnmatchcase(artifact_id or "", self.artifact)


@dataclass
class RemovedExtension:
    """지운 확장 하나를 담는 데이터 클래스. pom 상대 경로, 좌표, 프로필 id를 담는다."""
    pom: str
    coordinates: str
    profile: str | None = None

    def where(self) -> str:
        return f"{self.pom}" + (f" (profile {self.profile})" if self.profile else "")


@dataclass
class StripResult:
    """제거 결과를 담는 데이터 클래스. 지운 확장과, 목록에 없어 그대로 둔 확장을 담는다."""
    removed: list[RemovedExtension] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    poms_changed: list[str] = field(default_factory=list)


def load_deploy_transports(cfg: Any) -> list[DeployTransport]:
    """``intake.build_extensions_file``에서 ``deploy_transports`` 목록을 읽는다.
    파일이 없거나 깨졌으면 경고를 한 번 남기고 빈 목록을 반환하므로, 접수는 아무것도 지우지 않는다.

    The `deploy_transports` rows of `intake.build_extensions_file`; a missing / broken file → one warning and []."""
    rel = getattr(getattr(cfg, "intake", None), "build_extensions_file", "") or ""
    if not rel:
        return []
    path = cfg.resolve_path(rel) if hasattr(cfg, "resolve_path") else Path(rel)
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("build extensions table %s not read (%s: %s) — no deploy transport extension is removed at intake",
                    path, type(e).__name__, e)
        return []
    out: list[DeployTransport] = []
    for row in (data.get("deploy_transports") or []) if isinstance(data, dict) else []:
        if isinstance(row, dict) and row.get("group"):
            out.append(DeployTransport(group=str(row["group"]).strip(), artifact=str(row.get("artifact") or "*").strip(),
                                       note=str(row.get("note") or "")))
    return out


def strip_deploy_extensions(ws: Any, cfg: Any, table: list[DeployTransport] | None = None) -> StripResult:
    """모든 pom의 ``build/extensions``와 ``profiles/*/build/extensions``에서 목록에 있는 확장을 지우고, 비게
    된 ``<extensions>``도 지운다.
    목록에 없는 확장은 ``kept``에 기록만 한다. 예외를 던지지 않고, 깨진 pom은 건너뛴다.

    Remove the listed extensions from every pom's `build/extensions` and `profiles/*/build/extensions`, drop an emptied
    `<extensions>`, save; unlisted extensions are recorded in `kept`. Never raises (a broken pom is skipped)."""
    from sbmig.restructure.pom_editor import PomEditor

    res = StripResult()
    rows = load_deploy_transports(cfg) if table is None else table
    if not rows:
        return res
    for rel in find_pom_files(ws):
        try:
            ed = PomEditor(ws.abs(rel))
            containers: list[tuple[Any, str | None]] = []
            build = ed.find("build")
            if build is not None:
                containers.append((ed.child(build, "extensions"), None))
            profiles = ed.find("profiles")
            for prof in (ed.children(profiles, "profile") if profiles is not None else []):
                pb = ed.child(prof, "build")
                if pb is not None:
                    containers.append((ed.child(pb, "extensions"), ed.text_of(prof, "id") or "?"))
            for exts, profile in containers:
                if exts is None:
                    continue
                removed_here = 0
                for ext in list(ed.children(exts, "extension")):
                    # `${…}` resolved with THIS pom's properties — the match uses the resolved value (a property defined in a
                    # parent pom stays literal and the extension is kept: the loop's note handles it)
                    g = ed.resolve(ed.text_of(ext, "groupId")) or ""
                    a = ed.resolve(ed.text_of(ext, "artifactId")) or ""
                    v = ed.resolve(ed.text_of(ext, "version")) or ""
                    coords = f"{g}:{a}" + (f":{v}" if v else "")
                    if any(r.matches(g, a) for r in rows):
                        ed.remove_node(ext)
                        removed_here += 1
                        res.removed.append(RemovedExtension(pom=rel, coordinates=coords, profile=profile))
                    else:
                        res.kept.append((rel, coords))
                if removed_here:                  # an as-is empty <extensions/> is not ours to rewrite (it would ride into the next commit)
                    ed.remove_if_empty(exts)
            if ed.changed:
                ed.save()
                res.poms_changed.append(rel)
        except Exception as e:  # noqa: BLE001 - one unreadable pom must not stop the intake; Maven will report it
            log.warning("build extensions: %s skipped (%s: %s)", rel, type(e).__name__, e)
    return res


def commit_message(res: StripResult) -> str:
    """커밋 제목과 본문을 만든다. 예: ``intake: deploy transport extension(s) removed (N)``"""
    if not res.removed:
        return ""
    body = "\n".join(f"- {r.coordinates} in {r.where()}" for r in res.removed)
    return (f"intake: deploy transport extension(s) removed ({len(res.removed)}) — mvn deploy transports (dav:/scp:/ftp:) "
            f"that `mvn clean package` never needs and the repository no longer has to serve\n\n{body}")


def followups(res: StripResult) -> list[str]:
    """지운 확장마다 영어로 된 인수인계 줄을 하나씩 만든다. 다른 후속 조치와 같은 말투로 쓴다."""
    return [f"build extension {r.coordinates} was removed from {r.where()}: it is a deploy transport (WebDAV / SSH / FTP) "
            f"the old <distributionManagement> needed for `mvn deploy`; packaging never used it and the company parent "
            f"supplies the deploy target — if a team still deploys over that protocol, it must re-add the extension there"
            for r in res.removed]
