"""동봉 jar 연결(v2.1z-i): 모듈 pom 에 트리 안 jar 를 Maven 의존성으로 붙인다 — 저장소에 그 좌표가 있으면 보통 의존성,
없으면 jar 를 ``<module>/lib/<name>`` 로 복사하고 ``system`` scope(``${project.basedir}/lib/<name>``)로. system 의존성은
spring-boot-maven-plugin 이 기본으로 묶지 않으므로 그 모듈을 묶는 boot 모듈의 플러그인에 ``includeSystemScope`` 를 켠다
(``java -jar`` 가 그 클래스를 보게). 좌표를 추측하지 않는다: pom.properties 가 준 좌표가 없으면 Ant 접수(v2.1j)와 같은
규칙으로 자리표시 좌표(프로젝트 groupId + 파일 stem, 버전 = 파일명 버전 또는 "0")를 쓰고 인수인계 ⚠ 로 Nexus 등록을 요청한다.

**계약(PM, 2026-11-04) — 구현은 재구조화 에이전트 몫**

Wiring a bundled jar (v2.1z-i) into a module pom: a normal dependency when the repository has the coordinates, else
the jar is copied to `<module>/lib/<name>` and declared with `system` scope (`${project.basedir}/lib/<name>`) — plus
`includeSystemScope` on the boot module's spring-boot-maven-plugin, since repackage leaves system jars out by default.
Coordinates are never guessed: without pom.properties the Ant-intake placeholder scheme applies (project groupId +
file stem, version from the file name or "0") and the Handover asks for the Nexus upload.
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import shutil
from dataclasses import dataclass, field
from typing import Any

from sbmig.core.models import GAV, JarCoordinate

from .pom_editor import PomEditError, PomEditor

__all__ = ["BUNDLED_TAIL", "SYSTEM_PATH_PREFIX", "WiredJar", "bundled_jar_followup", "copy_jar_to_module_lib",
           "ensure_include_system_scope", "webinf_left_followup", "wire_bundled_jar"]

log = logging.getLogger(__name__)

BUNDLED_TAIL = ("wired by sbmig so the build sees the classes the as-is IDE build saw; publish the jar to Nexus "
                "(`mvn deploy:deploy-file -Dfile=<jar> -DgroupId=<g> -DartifactId=<a> -Dversion=<v> -Dpackaging=jar "
                "-Durl=<repository> -DrepositoryId=<id>`) and replace the system dependency with the real coordinates")


# system 의존성의 systemPath 접두어(모듈 디렉토리 기준) — _relocate_system_dep 과 같은 모양
SYSTEM_PATH_PREFIX = "${project.basedir}/lib/"
BOOT_PLUGIN = ("org.springframework.boot", "spring-boot-maven-plugin")
# Ant 접수(v2.1j)와 같은 사유 문구 — 사람이 pom 주석·인수인계에서 같은 말을 보게
UNKNOWN_WHY = "no pom.properties, not in the jar name table"
WEBINF_JARS_SHOWN = 5


@dataclass
class WiredJar:
    """연결 결과 한 건: 어느 pom 에 어떤 좌표로(scope, systemPath), jar 가 어디로 복사됐고, includeSystemScope 를 켠 boot pom 들.

    One wiring: the pom edited, the coordinates written (scope / systemPath), where the jar was copied, the boot poms
    that got `includeSystemScope`, and the Handover line."""
    jar: JarCoordinate
    module_pom: str                                   # workspace-relative pom path edited
    group_id: str
    artifact_id: str
    version: str
    scope: str                                        # "compile" (repository has it) | "system"
    system_path: str = ""                             # "${project.basedir}/lib/<name>" for system scope
    copied_to: str = ""                               # workspace-relative path of the copy ("" = none)
    boot_poms: list[str] = field(default_factory=list)   # poms where includeSystemScope was set
    placeholder: bool = False                         # coordinates synthesized (no pom.properties / lookup hit)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


def _module_dir(module_pom: str) -> str:
    d = posixpath.dirname(module_pom.replace("\\", "/"))
    return "" if d in ("", ".") else d


def copy_jar_to_module_lib(ws: Any, src_rel: str, module_dir: str, artifact_id: str) -> tuple[str, str, bool]:
    """jar ``src_rel`` 을 ``<module_dir>/lib/<이름>`` 으로 복사한다(E7 / v2.1z-i 공용). 같은 이름의 다른 jar 가
    이미 있으면 ``<artifactId>-<이름>`` 으로 둘 다 살린다. 이미 그 자리면(원본이 곧 대상) 복사하지 않는다.
    돌려주는 값: (대상 상대 경로, systemPath 에 쓸 파일 이름, 이번에 복사했는가).

    Copy the jar `src_rel` to `<module_dir>/lib/<name>` (shared by the restructure's E7 relocation and the
    bundled-jar wiring); another jar of the same name already there → `<artifactId>-<name>`. Returns
    (destination, file name for `${project.basedir}/lib/<name>`, copied this time)."""
    name = posixpath.basename(src_rel)
    lib = f"{module_dir}/lib" if module_dir not in ("", ".") else "lib"
    dst = f"{lib}/{name}"
    if dst == src_rel:
        return dst, name, False
    # 같은 이름의 다른 jar 가 이미 lib/ 에 있으면 artifactId 를 앞에 붙여 둘 다 살린다
    if ws.abs(dst).exists() and ws.abs(dst).read_bytes() != ws.abs(src_rel).read_bytes():
        name = f"{artifact_id}-{name}"
        dst = f"{lib}/{name}"
    if ws.abs(dst).exists():
        return dst, name, False
    ws.abs(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ws.abs(src_rel), ws.abs(dst))
    return dst, name, True


def _sha1(ws: Any, rel: str) -> str:
    try:
        return hashlib.sha1(ws.abs(rel).read_bytes()).hexdigest()   # identity, not security
    except OSError:
        return ""


def _locate_jar(ws: Any, jar: JarCoordinate) -> str:
    """분석 시점 경로(as-is)에 jar 가 없으면(재구성이 웹 루트를 back 모듈로 옮겼다) 같은 이름의 파일을 트리에서
    찾는다: sha1 이 같은 것 → as-is 경로로 끝나는 것 → 하나뿐인 것. 못 찾으면 ValueError."""
    rel = jar.path.replace("\\", "/")
    if ws.abs(rel).is_file():
        return rel
    name = posixpath.basename(rel)
    found = [x for x in ws.glob(name) if ws.abs(x).is_file()]
    if jar.sha1:
        same = [x for x in found if _sha1(ws, x) == jar.sha1]
        if same:
            found = same
    tail = [x for x in found if x.endswith("/" + rel)]
    if tail:
        return min(tail, key=len)
    if len(found) == 1:
        return found[0]
    raise ValueError(f"bundled jar {jar.path} not found in the project"
                     + (f" ({len(found)} files named {name})" if found else ""))


def _load(ws: Any, pom: str) -> PomEditor:
    try:
        return PomEditor(ws.abs(pom))
    except PomEditError as e:
        raise ValueError(f"cannot edit {pom}: {e}") from e


def _comment_before(ed: PomEditor, el: Any, text: str) -> None:
    """``el`` 바로 앞에 같은 들여쓰기로 XML 주석을 넣는다(``--`` 는 XML 주석에 쓸 수 없어 ``-`` 로)."""
    from lxml import etree

    parent = el.getparent()
    prev = el.getprevious()
    before = (prev.tail if prev is not None else parent.text) or ""
    c = etree.Comment(" " + text.replace("--", "-") + " ")
    parent.insert(parent.index(el), c)
    c.tail = before


def _coordinates(ws: Any, ed: PomEditor, jar: JarCoordinate, src_rel: str, project_group: str
                 ) -> tuple[str, str, str, bool]:
    """(g, a, v, placeholder). pom.properties 등이 준 완전한 좌표는 그대로; 아니면 Ant 접수 규칙의 자리표시 좌표."""
    from sbmig.analyze.ant import maven_artifact_id
    from sbmig.analyze.jar_coords import stem_and_version

    if jar.group_id and jar.artifact_id and jar.version:
        return jar.group_id, jar.artifact_id, jar.version, False
    stem, file_version = stem_and_version(posixpath.basename(src_rel))
    g = jar.group_id or project_group or ed.effective_group_id() or "bundled"
    a = jar.artifact_id or maven_artifact_id(stem, "lib")
    v = file_version or jar.version or "0"
    return g, a, v, True


def _system_path_name(ed: PomEditor, el: Any) -> str:
    return posixpath.basename((ed.text_of(el, "systemPath") or "").replace("\\", "/"))


def wire_bundled_jar(ws: Any, cfg: Any, *, module_pom: str, jar: JarCoordinate, repository_has: bool,
                     boot_poms: list[str] | None = None, project_group: str = "") -> WiredJar:
    """``module_pom`` 에 ``jar`` 를 의존성으로 쓴다. ``repository_has`` 가 True(호출자가 저장소 SET 에서 jar 의 좌표
    ``g:a:v`` 를 확인했다)면 그 좌표의 보통(compile) 의존성 한 줄; False 면 jar 를 ``<모듈 디렉토리>/lib/<파일명>`` 으로
    복사하고(같은 이름이 있으면 ``_relocate_system_dep`` 처럼 artifactId 를 앞에 붙인다) ``system`` scope +
    ``<systemPath>${project.basedir}/lib/<name></systemPath>`` + 이유 주석(Ant 접수의 문구 재사용). 좌표: jar.group_id /
    artifact_id / version 이 있으면 그대로, 없으면 ``project_group`` + ``maven_artifact_id(stem)`` + 파일명 버전("0")
    (``placeholder=True``). 이미 같은 g:a 가 그 pom 에 있으면 다시 쓰지 않는다(멱등). system 이면 ``boot_poms`` 각각에
    ``ensure_include_system_scope`` — 그 전에 boot pom 에도 같은 g:a:v 를 system 으로 선언한다(system 은 전이되지 않음;
    systemPath 는 복사본 하나를 상대 경로로). 파일은 바꾸되 커밋하지 않는다(호출자 = 루프의 사전 시도가 커밋·채점·롤백).

    Write `jar` as a dependency of `module_pom`: `repository_has` → a plain compile dependency on the jar's `g:a:v`;
    else copy the jar to `<module dir>/lib/<name>` and declare `system` scope with
    `${project.basedir}/lib/<name>`, the Ant-intake comment, and placeholder coordinates when the jar has none.
    Idempotent on the g:a. System scope → the same g:a:v is also declared `system` in each of `boot_poms` (Maven
    does not propagate system scope; `systemPath` = `${project.basedir}/<relpath to the one copy>`, no second copy)
    and `ensure_include_system_scope` on it; `WiredJar.boot_poms` lists the boot poms edited. Edits files, never
    commits (the loop's pre-try owns commit / scoring / rollback). Raises `RestructureError`-like `ValueError` on an
    unparsable pom (the caller reports it as a failed apply)."""
    ed = _load(ws, module_pom)
    mdir = _module_dir(module_pom)
    full = bool(jar.group_id and jar.artifact_id and jar.version)
    if repository_has and full:
        w = WiredJar(jar=jar, module_pom=module_pom, group_id=jar.group_id, artifact_id=jar.artifact_id,
                     version=jar.version, scope="compile")
        if ed.has_dependency(jar.group_id, jar.artifact_id):
            w.notes.append(f"{w.key} already declared in {module_pom}")
            return w
        ed.add_dependency(GAV(group_id=jar.group_id, artifact_id=jar.artifact_id, version=jar.version))
        ed.save()
        w.notes.append(f"{w.key}:{w.version} added to {module_pom} (the repository has it)")
        return w

    src = _locate_jar(ws, jar)
    g, a, v, placeholder = _coordinates(ws, ed, jar, src, project_group)
    name = posixpath.basename(src)
    # 멱등: 같은 g:a 가 이미 이 jar(같은 파일 이름의 systemPath)를 가리키면 다시 쓰지 않는다. 다른 jar 가 같은
    # g:a 를 쓰고 있으면 Ant 접수처럼 artifactId 에 sha1 꼬리를 붙인다
    for _ in range(2):
        same = [e for e in ed.dependency_elements(False)
                if ed.text_of(e, "groupId") == g and ed.text_of(e, "artifactId") == a]
        if not same:
            break
        el = same[0]
        if not placeholder or (ed.text_of(el, "scope") or "") != "system" or _system_path_name(ed, el) in (
                name, f"{a}-{name}"):
            w = WiredJar(jar=jar, module_pom=module_pom, group_id=g, artifact_id=a,
                         version=ed.text_of(el, "version") or v, scope=ed.text_of(el, "scope") or "compile",
                         system_path=ed.text_of(el, "systemPath") or "", placeholder=placeholder)
            w.notes.append(f"{w.key} already declared in {module_pom}")
            return w
        sha = jar.sha1 or _sha1(ws, src) or "x"
        a = f"{a}-{sha[:7]}"
    dst, lib_name, copied = copy_jar_to_module_lib(ws, src, mdir, a)
    system_path = SYSTEM_PATH_PREFIX + lib_name
    if placeholder:
        why = f"Maven coordinates unknown ({UNKNOWN_WHY})"
    else:
        why = f"{g}:{a}:{v} ({jar.source}) is not in the repository"
    comment = (f"{name}: {why} — system scope keeps the build passing; install the jar into Nexus and replace "
               "this entry")
    _declare_system(ed, g, a, v, system_path, comment)
    ed.save()
    w = WiredJar(jar=jar, module_pom=module_pom, group_id=g, artifact_id=a, version=v, scope="system",
                 system_path=system_path, copied_to=dst if copied or dst != src else "", placeholder=placeholder)
    if copied:
        w.notes.append(f"bundled jar {src} copied to {dst}")
    w.notes.append(f"{g}:{a}:{v} (system, {system_path}) added to {module_pom}")
    # Maven 은 system 의존성을 전이하지 않는다: back 모듈에만 쓰면 boot 의 repackage 아카이브에 들어가지 않으므로
    # boot pom 마다 같은 g:a:v 를 system 으로 한 번 더 선언한다 — systemPath 는 복사본 하나를 boot pom 디렉토리에서 가리킨다
    for bp in boot_poms or []:
        edited = False
        if bp != module_pom:
            bed = _load(ws, bp)
            if not any(bed.text_of(e, "groupId") == g and bed.text_of(e, "artifactId") == a
                       for e in bed.dependency_elements(False)):
                rel = posixpath.relpath(dst, _module_dir(bp) or ".")
                _declare_system(bed, g, a, v, "${project.basedir}/" + rel, comment)
                bed.save()
                edited = True
                w.notes.append(f"{g}:{a}:{v} (system, ${{project.basedir}}/{rel}) added to {bp}")
        if ensure_include_system_scope(ws, bp):
            edited = True
            w.notes.append(f"includeSystemScope set on spring-boot-maven-plugin in {bp}")
        if edited:
            w.boot_poms.append(bp)
    return w


def _declare_system(ed: PomEditor, g: str, a: str, v: str, system_path: str, comment: str) -> None:
    """``g:a:v`` 를 system scope + systemPath + 이유 주석으로 직접 의존성에 쓴다(저장은 호출자)."""
    ed.add_dependency(GAV(group_id=g, artifact_id=a, version=v, scope="system"))
    el = next(e for e in ed.dependency_elements(False)
              if ed.text_of(e, "groupId") == g and ed.text_of(e, "artifactId") == a)
    ed.set_child_text(el, "systemPath", system_path)
    _comment_before(ed, el, comment)


def ensure_include_system_scope(ws: Any, boot_pom: str) -> bool:
    """``boot_pom`` 의 spring-boot-maven-plugin ``<configuration>`` 에 ``<includeSystemScope>true</includeSystemScope>`` 를
    보장한다(플러그인이 없으면 아무것도 안 함 → False). 이미 있으면 False, 새로 썼으면 True.

    Ensure `<includeSystemScope>true</includeSystemScope>` in the spring-boot-maven-plugin configuration of
    `boot_pom`. True when written, False when already there or the plugin is absent."""
    ed = _load(ws, boot_pom)
    plugins = ed.find_plugins(*BOOT_PLUGIN, where="build") or ed.find_plugins(*BOOT_PLUGIN, where="management")
    if not plugins:
        log.debug("no spring-boot-maven-plugin in %s: includeSystemScope not set", boot_pom)
        return False
    plugin = plugins[0]
    conf = ed.child(plugin, "configuration")
    if conf is not None and (ed.text_of(conf, "includeSystemScope") or "").strip().lower() == "true":
        return False
    if conf is None:
        conf = ed._insert_ordered(plugin, "configuration")
    ed.set_child_text(conf, "includeSystemScope", "true")
    ed.reindent(plugin)
    ed.save()
    return True


def bundled_jar_followup(w: WiredJar) -> str:
    """인수인계 한 줄: "bundled jar `<name>` (<why>) wired into `<module pom>` as a <scope> dependency `<g:a:v>` — <BUNDLED_TAIL>"
    (system 일 때; compile 이면 "… as a compile dependency `g:a:v` (the repository has it)"). i18n PATTERNS 가 옮긴다.

    The Handover line for one wiring (system: placeholder coordinates + the Nexus ask; compile: the repository had it)."""
    name = posixpath.basename(w.copied_to or w.jar.path)
    gav = f"{w.group_id}:{w.artifact_id}:{w.version}"
    if w.scope != "system":
        return f"bundled jar `{name}` wired into `{w.module_pom}` as a compile dependency `{gav}` (the repository has it)"
    why = (f"Maven coordinates unknown — {UNKNOWN_WHY}" if w.placeholder
           else f"{w.jar.source} coordinates, not in the repository")
    return f"bundled jar `{name}` ({why}) wired into `{w.module_pom}` as a system dependency `{gav}` — {BUNDLED_TAIL}"


def webinf_left_followup(left: list[str], where: str) -> str:
    """웹 루트를 옮긴 뒤 WEB-INF/lib · WEB-INF/classes · META-INF 에 남은 파일의 인수인계 한 줄. jar 가 있으면 그
    이름(최대 5개, 그 뒤 " …")과 "빌드 오류가 그 jar 를 필요로 하면 sbmig 가 연결한다(`sbmig-bundled`)"를 말한다;
    jar 가 없으면 예전 문장 그대로.

    The webapp-move follow-up for files left under WEB-INF/lib, WEB-INF/classes or META-INF: names the jars (≤ 5,
    then " …") and says the fix loop wires a bundled jar a build error needs; without jars the old sentence."""
    head = (f"{len(left)} file(s) under WEB-INF/lib, WEB-INF/classes or META-INF stay in {where} (not packaged)")
    jars = [posixpath.basename(x) for x in left if x.lower().endswith(".jar")]
    if not jars:
        return head + ": declare the jars as Maven dependencies / move the classes into src/main/java"
    shown = ", ".join(f"`{j}`" for j in jars[:WEBINF_JARS_SHOWN]) + (" …" if len(jars) > WEBINF_JARS_SHOWN else "")
    others = len(left) > len(jars)
    return (head + f": jar(s) {shown} — sbmig wires a bundled jar into its module pom when a build error needs it "
            "(`sbmig-bundled`), declare the others as Maven dependencies"
            + ("; move the classes into src/main/java" if others else ""))
