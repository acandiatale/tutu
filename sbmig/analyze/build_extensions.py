"""접수 때 배포 전송 빌드 확장 제거 (2026-11-05, 회사 실행).

pom 의 ``<build><extensions>`` 에 든 Wagon 전송 제공자(``org.apache.maven.wagon:wagon-webdav`` 등)는 ``mvn deploy`` 가
``dav:`` / ``scp:`` / ``ftp:`` 주소로 올릴 때만 쓰이는데, Maven 은 어떤 goal 보다 먼저(OpenRewrite 포함) 확장을 내려받아
로드한다. 저장소가 더는 주지 않는 2007년 jar 하나가 ``Unresolveable build extension`` 으로 실행 전체를 막았다. 패키징에
필요 없고 배포 대상은 회사 부모가 주므로, 지식 목록(``intake.build_extensions_file``)에 맞는 확장을 첫 Maven 호출 전에
모든 pom(프로필 안 포함)에서 지운다. 목록에 없는 확장(os-maven-plugin, 빌드 캐시 …)은 빌드에 참여하므로 손대지 않고
기록만 한다. 좌표는 코드가 아니라 지식 파일에 있다.

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
    """지식 목록의 한 줄: groupId(정확히) + artifactId(셸 글롭).

    One knowledge row: exact groupId, artifactId as a shell glob."""
    group: str
    artifact: str = "*"
    note: str = ""

    def matches(self, group_id: str | None, artifact_id: str | None) -> bool:
        return bool(group_id) and group_id == self.group and fnmatch.fnmatchcase(artifact_id or "", self.artifact)


@dataclass
class RemovedExtension:
    """지운 확장 하나: pom(상대 경로), 좌표, 프로필 id(프로필 안이었으면)."""
    pom: str
    coordinates: str
    profile: str | None = None

    def where(self) -> str:
        return f"{self.pom}" + (f" (profile {self.profile})" if self.profile else "")


@dataclass
class StripResult:
    """제거 결과: 지운 확장들과, 목록에 없어 그대로 둔 확장들 (pom, 좌표)."""
    removed: list[RemovedExtension] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    poms_changed: list[str] = field(default_factory=list)


def load_deploy_transports(cfg: Any) -> list[DeployTransport]:
    """``intake.build_extensions_file`` 의 ``deploy_transports`` 목록. 파일이 없거나 깨졌으면 경고 한 번과 빈 목록
    (그러면 접수는 아무것도 지우지 않는다).

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
    """모든 pom(``find_pom_files``: 접수가 무시한 폴더 제외)의 ``build/extensions`` 와 ``profiles/*/build/extensions`` 에서
    목록에 맞는 확장을 지우고 빈 ``<extensions>`` 도 걷어 저장한다. 목록에 없는 확장은 ``kept`` 에 기록만. 절대 예외 없음
    (pom 하나가 깨졌으면 그 pom 만 건너뛴다).

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
                for ext in list(ed.children(exts, "extension")):
                    g = ed.resolve(ed.text_of(ext, "groupId")) or ""
                    a = ed.resolve(ed.text_of(ext, "artifactId")) or ""
                    v = ed.resolve(ed.text_of(ext, "version")) or ""
                    coords = f"{g}:{a}" + (f":{v}" if v else "")
                    if any(r.matches(g, a) for r in rows):
                        ed.remove_node(ext)
                        res.removed.append(RemovedExtension(pom=rel, coordinates=coords, profile=profile))
                    else:
                        res.kept.append((rel, coords))
                ed.remove_if_empty(exts)
            if ed.changed:
                ed.save()
                res.poms_changed.append(rel)
        except Exception as e:  # noqa: BLE001 - one unreadable pom must not stop the intake; Maven will report it
            log.warning("build extensions: %s skipped (%s: %s)", rel, type(e).__name__, e)
    return res


def commit_message(res: StripResult) -> str:
    """커밋 제목 + 본문 한 줄씩: ``intake: deploy transport extension(s) removed (N)``."""
    if not res.removed:
        return ""
    body = "\n".join(f"- {r.coordinates} in {r.where()}" for r in res.removed)
    return (f"intake: deploy transport extension(s) removed ({len(res.removed)}) — mvn deploy transports (dav:/scp:/ftp:) "
            f"that `mvn clean package` never needs and the repository no longer has to serve\n\n{body}")


def followups(res: StripResult) -> list[str]:
    """인수인계 줄(영문, 다른 후속 조치와 같은 말투): 지운 확장마다 하나."""
    return [f"build extension {r.coordinates} was removed from {r.where()}: it is a deploy transport (WebDAV / SSH / FTP) "
            f"the old <distributionManagement> needed for `mvn deploy`; packaging never used it and the company parent "
            f"supplies the deploy target — if a team still deploys over that protocol, it must re-add the extension there"
            for r in res.removed]
