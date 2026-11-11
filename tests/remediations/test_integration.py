"""End-to-end remediation runs over the REAL intake / inventory / planner / apply / ivas / maven modules.

Nothing external: Maven is a fake runner that emulates resolution and packaging of the synthetic project from the
CURRENT poms in the directory it runs in (so a version text edit really changes what "Maven" resolves), IVAS is a fake
HTTP session serving the documented JSON shapes and answering from the emulated resolution of exactly the bytes it
received. No network, no real mvn, no sleeping.
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from lxml import etree

from sbmig.build.mvn import ProcessResult
from sbmig.core.config import BuildConfig, RemediationConfig, RemediationPolicyConfig
from sbmig.remediations import apply as apply_mod
from sbmig.remediations import graph
from sbmig.remediations import intake as intake_mod
from sbmig.remediations import inventory as inventory_mod
from sbmig.remediations import planner as planner_mod
from sbmig.remediations.interfaces import Components
from sbmig.remediations.ivas import IvasHttpClient, IvasSettings, run_scan
from sbmig.remediations.maven import ServerMaven
from sbmig.remediations.models import (
    AWAITING_APPROVAL,
    AWAITING_CONFIRMATION,
    D_APPROVED,
    D_PENDING,
    D_REJECTED,
    FAILED,
    INCOMPLETE,
    STOPPED,
    SUCCEEDED,
    Decision,
    RemediationState,
    RoundPlan,
    RunPaths,
    read_json,
    write_json_atomic,
)

from .fakes import FakeVersions

BASE = "https://ivas.invalid/api"
TOKEN = "tok-INTEGRATION-SECRET-5a4b"
PASSWORD = "pw-INTEGRATION-SECRET-77"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

SETTINGS = """<settings>
  <mirrors>
    <mirror><id>nexus</id><url>https://nexus.invalid/repository/public/</url><mirrorOf>*</mirrorOf></mirror>
  </mirrors>
</settings>
"""


# ============================================================================ Maven emulator
def _ln(el: Any) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _kid(el: Any, name: str) -> Any:
    if el is None:
        return None
    return next((c for c in el if _ln(c) == name), None)


def _kids(el: Any, name: str) -> list[Any]:
    return [] if el is None else [c for c in el if _ln(c) == name]


def _t(el: Any) -> str:
    return (el.text or "").strip() if el is not None else ""


class EmuFailure(Exception):
    pass


@dataclass
class EPom:
    g: str
    a: str
    v: str
    packaging: str
    parent: tuple[str, str, str] | None
    props: dict[str, str]
    deps: list[tuple[str, str, str, str, str]]          # g, a, raw version, scope, type
    dm: list[tuple[str, str, str, str, str]]
    modules: list[str]
    plugins: list[tuple[str, str, str]]


def _read_pom(path: Path) -> EPom:
    try:
        root = etree.fromstring(path.read_bytes(), etree.XMLParser(resolve_entities=False, no_network=True))
    except (OSError, etree.XMLSyntaxError) as e:
        raise EmuFailure(f"Non-parseable POM {path.name}: {type(e).__name__}") from None
    par = _kid(root, "parent")
    parent = (_t(_kid(par, "groupId")), _t(_kid(par, "artifactId")), _t(_kid(par, "version"))) if par is not None else None

    def deps(holder: Any) -> list[tuple[str, str, str, str, str]]:
        return [(_t(_kid(d, "groupId")), _t(_kid(d, "artifactId")), _t(_kid(d, "version")),
                 _t(_kid(d, "scope")) or "compile", _t(_kid(d, "type")) or "jar")
                for d in _kids(_kid(holder, "dependencies"), "dependency")]

    plugins = [(_t(_kid(p, "groupId")) or "org.apache.maven.plugins", _t(_kid(p, "artifactId")), _t(_kid(p, "version")))
               for p in _kids(_kid(_kid(root, "build"), "plugins"), "plugin")]
    return EPom(g=_t(_kid(root, "groupId")) or (parent[0] if parent else ""), a=_t(_kid(root, "artifactId")),
                v=_t(_kid(root, "version")) or (parent[2] if parent else ""),
                packaging=_t(_kid(root, "packaging")) or "jar", parent=parent,
                props={_ln(c): _t(c) for c in (_kid(root, "properties") if _kid(root, "properties") is not None else [])
                       if _ln(c)},
                deps=deps(root), dm=deps(_kid(root, "dependencyManagement")),
                modules=[_t(m) for m in _kids(_kid(root, "modules"), "module")], plugins=plugins)


@dataclass
class World:
    """The emulated repository: transitive dependencies, BOM contents, external parents' managed versions."""
    catalog: dict[str, list[str]] = field(default_factory=dict)
    boms: dict[str, dict[str, str]] = field(default_factory=dict)
    ext_parents: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class ENode:
    g: str
    a: str
    v: str
    scope: str
    children: list[ENode] = field(default_factory=list)

    @property
    def gav(self) -> str:
        return f"{self.g}:{self.a}:{self.v}"


@dataclass
class EModule:
    dir: str
    pom: EPom
    deps: list[ENode]

    def all_nodes(self) -> list[ENode]:
        out: list[ENode] = []

        def walk(ns: list[ENode]) -> None:
            for n in ns:
                out.append(n)
                walk(n.children)
        walk(self.deps)
        return out


def _interp(raw: str, props: dict[str, str]) -> str:
    out = raw
    for _ in range(5):
        if "${" not in out:
            break
        start = out.index("${")
        end = out.index("}", start)
        name = out[start + 2:end]
        if name not in props:
            break
        out = out[:start] + props[name] + out[end + 1:]
    return out


def emulate(project: Path, world: World) -> list[EModule]:
    """What Maven would resolve for the reactor in `project` (two pom levels, own management, BOM imports, transitive)."""
    root = _read_pom(project / "pom.xml")
    order: list[tuple[str, EPom]] = []
    queue = ["."]
    while queue:
        d = queue.pop(0)
        pom = root if d == "." else _read_pom(project / d / "pom.xml")
        order.append((d, pom))
        queue += [posixpath.normpath(m if d == "." else f"{d}/{m}") for m in pom.modules]
    out: list[EModule] = []
    for d, pom in order:
        chain = [pom] if d == "." or not pom.parent or pom.parent[1] != root.a else [pom, root]
        top = chain[-1]
        props: dict[str, str] = {}
        for p in reversed(chain):
            props.update(p.props)
        props.update({"project.version": pom.v, "project.groupId": pom.g})
        dm: dict[str, str] = {}
        if top.parent is not None and top.parent[1] != root.a:
            dm.update(world.ext_parents.get(":".join(top.parent), {}))
        for p in reversed(chain):
            for g, a, raw, scope, typ in p.dm:
                if scope == "import":
                    dm.update(world.boms.get(f"{g}:{a}:{_interp(raw, props)}", {}))
            for g, a, raw, scope, typ in p.dm:
                if scope != "import":
                    dm[f"{g}:{a}"] = _interp(raw, props)
        declared: dict[str, tuple[str, str]] = {}
        for p in reversed(chain):
            for g, a, raw, scope, _typ in p.deps:
                declared[f"{g}:{a}"] = (raw, scope)
        seen = set(declared)
        level: list[ENode] = []
        for ga, (raw, scope) in declared.items():
            v = _interp(raw, props) if raw else dm.get(ga, "")
            if not v or "${" in v:
                raise EmuFailure(f"'dependencies.dependency.version' for {ga}:jar is missing")
            g, a = ga.split(":")
            level.append(ENode(g, a, v, scope))
        roots = list(level)
        while level:                                     # breadth first: the nearest declaration wins
            nxt: list[ENode] = []
            for n in level:
                for child in world.catalog.get(n.gav, []):
                    cg, ca, cv = child.split(":")
                    if f"{cg}:{ca}" in seen:
                        continue
                    seen.add(f"{cg}:{ca}")
                    c = ENode(cg, ca, dm.get(f"{cg}:{ca}", cv), n.scope)
                    n.children.append(c)
                    nxt.append(c)
            level = nxt
        out.append(EModule(d, pom, roots))
    return out


def render_tree(m: EModule) -> str:
    lines = [f"{m.pom.g}:{m.pom.a}:{m.pom.packaging}:{m.pom.v}"]

    def walk(ns: list[ENode], prefix: str) -> None:
        for i, n in enumerate(ns):
            last = i == len(ns) - 1
            lines.append(prefix + ("\\- " if last else "+- ") + f"{n.g}:{n.a}:jar:{n.v}:{n.scope}")
            walk(n.children, prefix + ("   " if last else "|  "))
    walk(m.deps, "")
    return "\n".join(lines)


def _inner_jar(g: str, a: str, v: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"META-INF/maven/{g}/{a}/pom.properties", f"groupId={g}\nartifactId={a}\nversion={v}\n")
    return buf.getvalue()


class MavenEmu:
    """A `run_logged`-shaped runner: `dependency:tree` writes the emulated tree to -DoutputFile; `package` builds a
    jar / war (WEB-INF/lib with pom.properties) or fails when it meets a version from `fail_on`."""

    def __init__(self, world: World, fail_on: set[str] | None = None):
        self.world = world
        self.fail_on = set(fail_on or ())
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd, *, cwd, env, log_path, timeout_s):
        cwd, log_path = Path(cwd), Path(log_path)
        out = next((a.split("=", 1)[1] for a in cmd if a.startswith("-DoutputFile=")), None)
        self.calls.append({"cwd": cwd, "tree": out is not None, "cmd": list(cmd)})
        assert "-Dmaven.test.skip=true" in cmd and "-s" in cmd
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def done(code: int, text: str) -> ProcessResult:
            log_path.write_text(text, encoding="utf-8")
            return ProcessResult(exit_code=code, text=text, duration_s=0.01)
        try:
            mods = emulate(cwd, self.world)
        except EmuFailure as e:
            return done(1, f"[ERROR] {e}\n[INFO] BUILD FAILURE\n")
        if out is not None:
            Path(out).write_text("\n".join(render_tree(m) for m in mods) + "\n", encoding="utf-8")
            return done(0, "[INFO] BUILD SUCCESS\n")
        bad = sorted({n.gav for m in mods for n in m.all_nodes()} & self.fail_on)
        if bad:
            return done(1, f"[ERROR] COMPILATION ERROR : incompatible API in {', '.join(bad)}\n[INFO] BUILD FAILURE\n")
        for m in mods:
            target = (cwd if m.dir == "." else cwd / m.dir) / "target"
            if target.exists():
                import shutil
                shutil.rmtree(target)
            if m.pom.packaging == "pom":
                continue
            target.mkdir(parents=True)
            art = target / f"{m.pom.a}-{m.pom.v}.{'war' if m.pom.packaging == 'war' else 'jar'}"
            with zipfile.ZipFile(art, "w") as zf:
                zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
                if m.pom.packaging == "war":
                    for n in m.all_nodes():
                        if n.scope in ("compile", "runtime"):
                            zf.writestr(f"WEB-INF/lib/{n.a}-{n.v}.jar", _inner_jar(n.g, n.a, n.v))
        return done(0, "[INFO] Building\n[INFO] BUILD SUCCESS\n")


# ============================================================================ fake IVAS server
class Resp:
    def __init__(self, status: int = 200, body: Any = None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


@dataclass
class Vuln:
    cve: str
    patched: str


class IvasServer:
    """`requests.Session.request` stand-in. Keeps the file bytes of every upload exactly as received and answers each
    analysis from the emulated resolution of those bytes (plus tree files and declared plugins)."""

    def __init__(self, work: Path, world: World, vulns: dict[str, Vuln], *, with_top: bool = True,
                 file_vulns: dict[str, tuple[str, str, Vuln]] | None = None):
        self.work, self.world, self.vulns, self.with_top = work, world, vulns, with_top
        self.file_vulns = dict(file_vulns or {})          # path suffix → (ga, version, vuln)
        self.uploads: list[bytes] = []
        self.analyses: dict[str, list[dict]] = {}
        self.status_calls: dict[str, int] = {}
        self.uncertain_uploads: set[int] = set()          # 1-based upload numbers that time out after the body
        self.broken_library_page: set[int] = set()        # analyses whose library page 1 always answers 500
        self.on_status: Any = None
        self.calls: list[tuple[str, str]] = []

    # -- what IVAS "finds" in a ZIP
    def _findings(self, data: bytes, n: int) -> list[dict]:
        root = self.work / f"upload-{n}"
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            zf.extractall(root)
        top = "" if "pom.xml" in names else names[0].split("/")[0]
        project = root / top if top else root
        prefix = f"{top}/" if top and self.with_top else ""
        found: dict[tuple[str, str, str], dict] = {}

        def add(ga: str, v: str, vuln: Vuln, loc: str) -> None:
            key = (vuln.cve, ga, v)
            item = found.setdefault(key, {
                "id": vuln.cve, "library": {"identifier": ga, "version": v, "info": {"patchedVersion": vuln.patched}},
                "detectedLocations": [], "action": {"actionDivCd": "NE"}, "rating": [{"version": "v3", "score": 7.5}]})
            if {"filePath": loc} not in item["detectedLocations"]:
                item["detectedLocations"].append({"filePath": loc})
        try:
            mods = emulate(project, self.world)
        except EmuFailure:
            mods = []
        for m in mods:
            pom_loc = prefix + ("pom.xml" if m.dir == "." else f"{m.dir}/pom.xml")
            for node in m.all_nodes():
                if node.gav in self.vulns:
                    add(f"{node.g}:{node.a}", node.v, self.vulns[node.gav], pom_loc)
            for g, a, v in m.pom.plugins:
                if f"{g}:{a}:{v}" in self.vulns:
                    add(f"{g}:{a}", v, self.vulns[f"{g}:{a}:{v}"], pom_loc)
        for name in names:
            for suffix, (ga, v, vuln) in self.file_vulns.items():
                if name.endswith(suffix):
                    rel = name[len(top) + 1:] if top and not self.with_top else name
                    add(ga, v, vuln, rel)
        return [found[k] for k in sorted(found)]

    # -- HTTP
    def request(self, method: str, url: str, **kw: Any) -> Resp:
        assert url.startswith(BASE), url
        path = url[len(BASE):]
        self.calls.append((method, path))
        if method == "POST" and path == "/session":
            assert kw["json"] == {"userId": "svc-user", "password": PASSWORD}
            return Resp(200, {"token": TOKEN})
        assert kw["headers"].get("X-API-KEY") == TOKEN
        if method == "POST" and path == "/projects/SELF/sourcecode":
            body = kw["data"].read()
            boundary = kw["headers"]["Content-Type"].split("boundary=", 1)[1].encode()
            head = body.index(b'name="file"')
            start = body.index(b"\r\n\r\n", head) + 4
            end = body.rindex(b"\r\n--" + boundary + b"--")
            data = body[start:end]
            self.uploads.append(data)
            n = len(self.uploads)
            if n in self.uncertain_uploads:
                raise requests.exceptions.ReadTimeout("read timed out")
            self.analyses[f"anl-{n}"] = self._findings(data, n)
            return Resp(200, {"projectId": f"prj-{n}", "lbrdAnlId": f"anl-{n}"})
        parts = path.strip("/").split("/")          # projects SELF <p> analysis <a> ...
        assert parts[:2] == ["projects", "SELF"] and parts[3] == "analysis", path
        pid, aid, rest = parts[2], parts[4], parts[5:]
        items = self.analyses[aid]
        if rest == ["status"]:
            k = self.status_calls[aid] = self.status_calls.get(aid, 0) + 1
            if self.on_status is not None:
                self.on_status(aid, k)
            return Resp(200, {"projectId": pid, "lbrdAnlId": aid, "anlStatusDivCd": "ANALYSIS" if k == 1 else "DONE"})
        if rest == ["result", "summary"]:
            cats = ["components", "swComponents", "fileComponents", "codeSnippets", "file", "functionComponents",
                    "ospkgComponents", "binaryComponents"]
            totals = {c: {"total": len(items) if c == "components" else 0} for c in cats}
            return Resp(200, {"summary": {"resultCount": {"vulnerabilities": totals}}})
        page, size = int(kw["params"]["page"]), int(kw["params"]["size"])
        if rest == ["result", "libraryvulnerabilities"]:
            if int(aid.split("-")[1]) in self.broken_library_page and page == 1:
                return Resp(500, {"error": "boom"})
            return Resp(200, {"content": items[page * size:(page + 1) * size], "totalElements": len(items)})
        return Resp(200, {"content": [], "totalElements": 0})


# ============================================================================ harness
def zip_bytes(files: dict[str, bytes], wrap: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"{wrap}/{name}" if wrap else name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])
    return buf.getvalue()


def zip_contents(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Rec:
    def __init__(self, paths: RunPaths):
        self.paths = paths
        self.events: list[dict] = []
        self.stop_flag = False

    def save(self, st: RemediationState) -> None:
        write_json_atomic(self.paths.state, st.to_dict())

    def journal(self, event: dict) -> None:
        self.events.append(dict(event))

    def stop(self) -> bool:
        return self.stop_flag


@dataclass
class Env:
    tmp: Path
    cfg: Any
    upload: bytes
    paths: RunPaths
    state: RemediationState
    comps: Components
    mvn: MavenEmu
    ivas: IvasServer
    rec: Rec

    def run(self) -> RemediationState:
        self.state = graph.run_remediation(self.state, self.paths, self.cfg, self.comps, save=self.rec.save,
                                           journal=self.rec.journal, stop=self.rec.stop)
        return self.state

    def tree_hash(self) -> str:
        return intake_mod.tree_hash(self.paths.accepted, intake_mod.manifest_paths(self.paths.manifest))

    def events(self, name: str) -> list[dict]:
        return [e for e in self.rec.events if e.get("event") == name]


def make_cfg(tmp: Path, **rem: Any) -> SimpleNamespace:
    settings = tmp / "server" / "settings.xml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(SETTINGS, encoding="utf-8")
    jdk = tmp / "server" / "jdk"
    jdk.mkdir(exist_ok=True)
    (jdk / "release").write_text('JAVA_VERSION="21.0.2"\n', encoding="utf-8")
    policy = rem.pop("policy", {})
    return SimpleNamespace(build=BuildConfig(mvn="/opt/server-maven/bin/mvn", settings_file=str(settings),
                                             java_home=str(jdk), local_repo=str(tmp / "server" / "m2")),
                           remediation=RemediationConfig(policy=RemediationPolicyConfig(**policy), **rem))


def start(tmp: Path, files: dict[str, str | bytes], *, vulns: dict[str, Vuln], versions: dict[str, list[str]],
          wrap: str | None = None, world: World | None = None, fail_on: set[str] | None = None, with_top: bool = True,
          file_vulns: dict | None = None, version_source: Any = None, **rem: Any) -> Env:
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = make_cfg(tmp, **rem)
    raw = {k: v.encode("utf-8") if isinstance(v, str) else v for k, v in files.items()}
    upload = zip_bytes(raw, wrap)
    up = tmp / "upload.zip"
    up.write_bytes(upload)
    paths = RunPaths(tmp / "out" / "_remediations" / "rem-1")
    res = intake_mod.accept_zip(up, paths.root, intake_mod.limits_from_config(cfg))
    assert res.project_root == (wrap or ".")
    state = graph.new_state("rem-1", "app.zip", cfg, res, ["default.yml"], policy_hash=planner_mod.policy_hash(cfg))
    world = world or World()
    rec = Rec(paths)
    mvn = MavenEmu(world, fail_on)
    server = IvasServer(tmp / "ivas-side", world, vulns, with_top=with_top, file_vulns=file_vulns)
    st = IvasSettings(base_url=BASE, user_id="svc-user", password=PASSWORD, poll_seconds=0, page_size=2)
    maven = ServerMaven(cfg, runner=mvn, maven_probe=lambda c: (True, "Apache Maven 3.9.9; Java version: 21.0.2"),
                        journal=rec.journal)
    comps = Components(ivas=IvasHttpClient(st, session=server, sleep=lambda _s: None),
                       scan=functools.partial(run_scan, settings=st, sleep=lambda _s: None),
                       maven=maven, versions=version_source or FakeVersions(versions), intake=intake_mod,
                       inventory=inventory_mod.build_inventory, plan=planner_mod.plan_round, apply=apply_mod)
    return Env(tmp, cfg, upload, paths, state, comps, mvn, server, rec)


def assert_clean_artifacts(env: Env) -> None:
    """report.md exists; no absolute tmp path and no secret in report / state; the original ZIP is untouched."""
    assert env.paths.report_md.is_file() and env.paths.report_json.is_file()
    assert env.paths.original_zip.read_bytes() == env.upload
    for p in (env.paths.report_md, env.paths.report_json, env.paths.state):
        text = p.read_text(encoding="utf-8")
        assert str(env.tmp) not in text and str(env.tmp.resolve()) not in text, p.name
        assert TOKEN not in text and PASSWORD not in text, p.name
    for p in env.paths.root.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".md", ".log", ".patch"):
            text = p.read_text(encoding="utf-8", errors="replace")
            assert TOKEN not in text and PASSWORD not in text, p


def assert_one_line_changed(env: Env, pom: str) -> tuple[str, str]:
    """The delivered ZIP has the original's file set; only `pom` differs, by exactly one line. Returns (old, new) line."""
    orig, got = zip_contents(env.upload), zip_contents(env.paths.delivery_zip.read_bytes())
    assert set(got) == set(orig)
    changed = [n for n in orig if orig[n] != got[n]]
    assert changed == [pom], changed
    a, b = orig[pom].split(b"\n"), got[pom].split(b"\n")
    assert len(a) == len(b)
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    assert len(diff) == 1, diff
    return diff[0][0].decode("latin-1"), diff[0][1].decode("latin-1")


def assert_succeeded_delivery(env: Env, out: RemediationState, uploads: int = 2) -> None:
    assert out.status == SUCCEEDED, (out.reason, out.reasons, out.message)
    assert len(env.ivas.uploads) == uploads
    assert env.ivas.uploads[0] == env.upload                                   # baseline = the original bytes
    assert out.delivery.sha256 == sha(env.paths.delivery_zip.read_bytes()) == sha(env.ivas.uploads[-1])
    assert not list(env.paths.accepted.rglob("target"))
    state = read_json(env.paths.state)
    assert state["status"] == SUCCEEDED and state["delivery"]["sha256"] == out.delivery.sha256
    assert_clean_artifacts(env)


# ============================================================================ projects
def war_pom(lib: str = "1.2.3", extra_deps: str = "", head: str = "", plugins: str = "", nl: str = "\n") -> str:
    text = (f"{head}<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>example</groupId>\n"
            "  <artifactId>app</artifactId>\n"
            "  <version>0.1</version>\n"
            "  <packaging>war</packaging>\n"
            "  <dependencies>\n"
            "    <dependency>\n"
            "      <groupId>example</groupId>\n"
            "      <artifactId>lib</artifactId>\n"
            f"      <version>{lib}</version>\n"
            "    </dependency>\n"
            f"{extra_deps}"
            "  </dependencies>\n"
            f"{plugins}"
            "</project>\n")
    return text.replace("\n", nl)


APP = {"src/main/java/example/App.java": "package example;\nclass App {}\n",
       "src/main/webapp/index.jsp": "<html>hi</html>\n"}
LIB_VULN = {"example:lib:1.2.3": Vuln("CVE-2026-1001", "1.2.4")}
LIB_VERSIONS = {"example:lib": ["1.2.2", "1.2.3", "1.2.4", "1.2.5", "2.0.0"]}


# ============================================================================ scenarios
@pytest.mark.parametrize("wrap,with_top", [(None, True), ("app-src", True), ("app-src", False)])
def test_single_module_patch_in_the_same_major(tmp_path, wrap, with_top):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS, wrap=wrap,
                with_top=with_top)
    out = env.run()
    assert_succeeded_delivery(env, out)
    pom = f"{wrap}/pom.xml" if wrap else "pom.xml"
    old, new = assert_one_line_changed(env, pom)
    assert old.strip() == "<version>1.2.3</version>" and new.strip() == "<version>1.2.4</version>"
    plan = RoundPlan.from_dict(read_json(env.paths.plan(1)))
    assert [(p.ga, p.from_version, p.to_version, p.decision) for p in plan.proposals] == [
        ("example:lib", "1.2.3", "1.2.4", "auto")]
    assert plan.proposals[0].evidence == "repository:nexus.invalid"
    assert plan.proposals[0].edits[0].pom == "pom.xml"                       # project-relative, even when wrapped
    assert out.counts["baseline_findings"] == 1 and out.counts["final_findings"] == 0 and out.counts["fixed"] == 1
    # the candidate's resolution and packaging were really checked: the war carries the new jar only
    resolved = read_json(env.paths.resolved(1))
    assert {d["version"] for m in resolved["modules"] for d in m["dependencies"] if d["artifact_id"] == "lib"} == {"1.2.4"}
    assert out.rounds[0].verify_problems == []
    # every Maven call ran inside the run directory, never in accepted/
    assert all(str(c["cwd"]).startswith(str(env.paths.root)) and "accepted" not in Path(c["cwd"]).parts
               for c in env.mvn.calls)


def test_crlf_and_latin1_poms_keep_their_bytes(tmp_path):
    head = '<?xml version="1.0" encoding="ISO-8859-1"?>\r\n'
    text = war_pom(head=head, nl="\r\n").replace("<packaging>war</packaging>",
                                                  "<packaging>war</packaging>\r\n  <description>Café</description>")
    env = start(tmp_path, {"pom.xml": text.encode("iso-8859-1"), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS)
    out = env.run()
    assert_succeeded_delivery(env, out)
    old, new = assert_one_line_changed(env, "pom.xml")
    assert old.endswith("\r") and new == old.replace("1.2.3", "1.2.4")
    delivered = zip_contents(env.paths.delivery_zip.read_bytes())["pom.xml"]
    assert b"Caf\xe9" in delivered and delivered.count(b"\r\n") == text.count("\r\n")


ROOT_HEAD = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n"
             "  <artifactId>root</artifactId>\n  <version>1.0.0</version>\n  <packaging>pom</packaging>\n"
             "  <modules>\n    <module>core</module>\n    <module>web</module>\n  </modules>\n")
PARENT = ("  <parent>\n    <groupId>example</groupId>\n    <artifactId>root</artifactId>\n"
          "    <version>1.0.0</version>\n  </parent>\n")


def module(name: str, deps: str, packaging: str = "jar") -> str:
    return (f"<project>\n  <modelVersion>4.0.0</modelVersion>\n{PARENT}  <artifactId>{name}</artifactId>\n"
            f"  <packaging>{packaging}</packaging>\n  <dependencies>\n{deps}  </dependencies>\n</project>\n")


def dep(g: str, a: str, v: str | None) -> str:
    ver = f"      <version>{v}</version>\n" if v is not None else ""
    return f"    <dependency>\n      <groupId>{g}</groupId>\n      <artifactId>{a}</artifactId>\n{ver}    </dependency>\n"


@pytest.mark.parametrize("shape", ["property", "management"])
def test_multi_module_version_in_the_root(tmp_path, shape):
    if shape == "property":
        root = ROOT_HEAD + "  <properties>\n    <lib.version>1.2.3</lib.version>\n  </properties>\n</project>\n"
        core = module("core", dep("example", "lib", "${lib.version}"))
    else:
        root = (ROOT_HEAD + "  <dependencyManagement>\n    <dependencies>\n"
                + dep("example", "lib", "1.2.3").replace("\n    ", "\n      ").replace("    <dependency>", "      <dependency>")
                + "    </dependencies>\n  </dependencyManagement>\n</project>\n")
        core = module("core", dep("example", "lib", None))
    web = module("web", dep("example", "lib", "${lib.version}" if shape == "property" else None)
                 + dep("example", "other", "2.0.0"), packaging="war")
    files = {"pom.xml": root, "core/pom.xml": core, "web/pom.xml": web,
             "core/src/main/java/C.java": "class C {}\n", "web/src/main/webapp/index.jsp": "x\n"}
    env = start(tmp_path, files, vulns=LIB_VULN, versions=LIB_VERSIONS, wrap="shop")
    out = env.run()
    assert_succeeded_delivery(env, out)
    old, new = assert_one_line_changed(env, "shop/pom.xml")
    assert "1.2.3" in old and new == old.replace("1.2.3", "1.2.4")
    plan = RoundPlan.from_dict(read_json(env.paths.plan(1)))
    assert len(plan.proposals) == 1
    assert plan.proposals[0].kind == ("property_version" if shape == "property" else "own_management_version")


def _approve_or_reject(env: Env, decision: str) -> None:
    """Write the decision the way the service will: validate against the current tree / policy, then set it."""
    data = read_json(env.paths.plan(1))
    plan = RoundPlan.from_dict(data)
    tree = env.tree_hash()
    phash = planner_mod.policy_hash(env.cfg)
    for i, p in enumerate(plan.proposals):
        d = Decision(proposal_id=p.id, decision=decision, proposal_hash=p.proposal_hash,
                     source_tree_hash=p.source_tree_hash, policy_hash=p.policy_hash, diff=p.diff, by="tester")
        planner_mod.validate_decision(p, d, current_tree_hash=tree, current_policy_hash=phash)
        data["proposals"][i]["decision"] = decision
    write_json_atomic(env.paths.plan(1), data)


MAJOR = {"vulns": {"example:lib:1.2.3": Vuln("CVE-2026-2002", "2.0.1")},
         "versions": {"example:lib": ["1.2.3", "1.2.9", "2.0.1"]}}


def test_major_only_fix_waits_for_approval_then_applies(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, **MAJOR)
    out = env.run()
    assert out.status == AWAITING_APPROVAL and out.next_node == "plan"
    plan = RoundPlan.from_dict(read_json(env.paths.plan(1)))
    p = plan.proposals[0]
    assert p.decision == D_PENDING and p.risks == ["major"] and p.to_version == "2.0.1"
    assert "-      <version>1.2.3</version>" in p.diff and "+      <version>2.0.1</version>" in p.diff
    assert not env.paths.report_md.exists() and len(env.ivas.uploads) == 1
    _approve_or_reject(env, D_APPROVED)
    out = env.run()
    assert env.events("plan_reused") and not env.events("plan_stale")       # resume did not re-plan
    assert_succeeded_delivery(env, out)
    _old, new = assert_one_line_changed(env, "pom.xml")
    assert "2.0.1" in new


def test_major_only_fix_rejected_is_incomplete_with_the_original_bytes(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, **MAJOR)
    before = env.tree_hash()
    env.run()
    _approve_or_reject(env, D_REJECTED)
    out = env.run()
    assert out.status == INCOMPLETE and out.reason == "rejected_proposals"
    assert env.paths.delivery_zip.read_bytes() == env.upload and env.tree_hash() == before
    assert len(env.ivas.uploads) == 1
    assert_clean_artifacts(env)


def test_policy_change_before_resume_replans_and_never_uses_the_old_approval(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, **MAJOR)
    env.run()
    _approve_or_reject(env, D_APPROVED)
    env.cfg.remediation.policy.deny_versions = {"example:lib": ["1.2.9"]}    # the server policy changed meanwhile
    out = env.run()
    assert env.events("policy_changed") and env.events("plan_stale")
    assert out.status == AWAITING_APPROVAL and out.policy_hash == planner_mod.policy_hash(env.cfg)
    plan = RoundPlan.from_dict(read_json(env.paths.plan(1)))
    assert plan.policy_hash == out.policy_hash and plan.proposals[0].decision == D_PENDING
    assert (env.paths.round_dir(1) / "plan.stale.json").is_file()
    assert len(env.ivas.uploads) == 1 and env.tree_hash() == intake_mod.tree_hash(
        env.paths.accepted, intake_mod.manifest_paths(env.paths.manifest))


BOM_ROOT = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>example</groupId>\n"
            "  <artifactId>app</artifactId>\n  <version>0.1</version>\n  <packaging>war</packaging>\n"
            "  <dependencyManagement>\n    <dependencies>\n      <dependency>\n        <groupId>example</groupId>\n"
            "        <artifactId>bom</artifactId>\n        <version>1.0</version>\n        <type>pom</type>\n"
            "        <scope>import</scope>\n      </dependency>\n    </dependencies>\n  </dependencyManagement>\n"
            "  <dependencies>\n" + dep("example", "lib", None) + "  </dependencies>\n</project>\n")
PLUGIN = ("  <build>\n    <plugins>\n      <plugin>\n        <groupId>example</groupId>\n"
          "        <artifactId>plug</artifactId>\n        <version>1.0</version>\n      </plugin>\n    </plugins>\n"
          "  </build>\n")


@pytest.mark.parametrize("kind", ["vendored", "vendored-no-top", "plugin"])
def test_non_editable_findings_are_residual_and_nothing_changes(tmp_path, kind):
    # transitive / BOM- / parent-managed findings get a managed-override proposal now (test_override_* below)
    world = World(catalog={"example:lib:1.2.3": ["example:deep:0.5"]}, boms={"example:bom:1.0": {"example:lib": "1.2.3"}})
    files: dict[str, str | bytes] = {"pom.xml": war_pom(), **APP}
    vulns: dict[str, Vuln] = {}
    file_vulns = None
    if kind.startswith("vendored"):
        files["src/main/webapp/WEB-INF/lib/vendor-1.0.jar"] = _inner_jar("example", "vendor", "1.0")
        file_vulns = {"WEB-INF/lib/vendor-1.0.jar": ("example:vendor", "1.0", Vuln("CVE-2026-4004", "1.1"))}
    else:
        files["pom.xml"] = war_pom(plugins=PLUGIN)
        vulns = {"example:plug:1.0": Vuln("CVE-2026-5005", "1.1")}
    env = start(tmp_path, files, vulns=vulns, versions={"example:deep": ["0.6"], "example:lib": ["1.2.4"],
                                                        "example:vendor": ["1.1"], "example:plug": ["1.1"]},
                world=world, file_vulns=file_vulns, wrap="w" if kind == "vendored" else None,
                with_top=kind != "vendored-no-top")
    before = env.tree_hash()
    out = env.run()
    assert out.status == INCOMPLETE and out.reasons == ["residual_findings", "manual_review"], (out.reasons, out.message)
    expected = {"vendored": "vendored", "vendored-no-top": "vendored", "plugin": "plugin"}[kind]
    assert [r.origin for r in out.residuals] == [expected], [r.to_dict() for r in out.residuals]
    assert env.tree_hash() == before
    assert env.paths.delivery_zip.read_bytes() == env.upload and out.delivery.sha256 == sha(env.upload)
    assert len(env.ivas.uploads) == 1 and not env.paths.patch(1).exists()
    assert_clean_artifacts(env)


# ---------------------------------------------------------------------------- managed-version override (2026-11-11)
CORP_ROOT = ("<project>\n  <modelVersion>4.0.0</modelVersion>\n  <parent>\n    <groupId>example</groupId>\n"
             "    <artifactId>corp-parent</artifactId>\n    <version>1.0</version>\n  </parent>\n"
             "  <artifactId>app</artifactId>\n  <packaging>war</packaging>\n"
             "  <dependencies>\n" + dep("example", "lib", None) + "  </dependencies>\n</project>\n")
OVERRIDE_CASES = {
    # kind: (root pom, world, vulns, versions, ga, from → to, origin)
    "transitive": (war_pom(), World(catalog={"example:lib:1.2.3": ["example:deep:0.5"]}),
                   {"example:deep:0.5": Vuln("CVE-2026-3003", "0.6")}, {"example:deep": ["0.4", "0.6", "0.7"]},
                   "example:deep", "0.5", "0.6", "transitive"),
    "bom": (BOM_ROOT, World(boms={"example:bom:1.0": {"example:lib": "1.2.3"}}), dict(LIB_VULN), LIB_VERSIONS,
            "example:lib", "1.2.3", "1.2.4", "bom_managed"),
    "parent": (CORP_ROOT, World(ext_parents={"example:corp-parent:1.0": {"example:lib": "1.2.3"}}), dict(LIB_VULN),
               LIB_VERSIONS, "example:lib", "1.2.3", "1.2.4", "parent_managed"),
}


def assert_only_inserted_lines(env: Env, pom: str) -> list[str]:
    """The delivered ZIP has the original's file set; only `pom` differs, and only by inserted lines. Returns them."""
    import difflib
    orig, got = zip_contents(env.upload), zip_contents(env.paths.delivery_zip.read_bytes())
    assert set(got) == set(orig)
    assert [n for n in orig if orig[n] != got[n]] == [pom]
    a, b = orig[pom].decode().splitlines(keepends=True), got[pom].decode().splitlines(keepends=True)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    assert {op for op, *_ in ops} <= {"equal", "insert"}, ops
    return [ln for op, _i1, _i2, j1, j2 in ops if op == "insert" for ln in b[j1:j2]]


@pytest.mark.parametrize("kind", sorted(OVERRIDE_CASES))
def test_override_waits_for_approval_then_inserts_the_entry(tmp_path, kind):
    root, world, vulns, versions, ga, frm, to, origin = OVERRIDE_CASES[kind]
    # require_approval false and managed_override removed from always_approve: the override still waits
    env = start(tmp_path, {"pom.xml": root, **APP}, vulns=vulns, versions=versions, world=world,
                always_approve=["major"])
    before = env.tree_hash()
    out = env.run()
    assert out.status == AWAITING_APPROVAL and out.next_node == "plan", (out.reason, out.reasons, out.message)
    plan = RoundPlan.from_dict(read_json(env.paths.plan(1)))
    (p,) = plan.proposals
    assert (p.ga, p.from_version, p.to_version, p.kind, p.risks, p.decision) == (
        ga, frm, to, "managed_override", ["managed_override"], D_PENDING)
    assert p.edits[0].op == "insert" and p.edits[0].pom == "pom.xml"
    assert f"+        <version>{to}</version>" in p.diff or f"+      <version>{to}</version>" in p.diff
    assert "will NOT move this version any more" in p.note
    assert env.tree_hash() == before and len(env.ivas.uploads) == 1
    _approve_or_reject(env, D_APPROVED)
    out = env.run()
    assert_succeeded_delivery(env, out)
    inserted = assert_only_inserted_lines(env, "pom.xml")
    assert [ln.strip() for ln in inserted if "Id>" in ln or "<version>" in ln] == [
        f"<groupId>{ga.split(':')[0]}</groupId>", f"<artifactId>{ga.split(':')[1]}</artifactId>", f"<version>{to}</version>"]
    resolved = read_json(env.paths.resolved(1))
    assert {d["version"] for m in resolved["modules"] for d in m["dependencies"]
            if f"{d['group_id']}:{d['artifact_id']}" == ga} == {to}
    assert out.counts["final_findings"] == 0 and out.residuals == []


@pytest.mark.parametrize("kind", sorted(OVERRIDE_CASES))
def test_override_rejected_is_incomplete_and_nothing_changes(tmp_path, kind):
    root, world, vulns, versions, *_ = OVERRIDE_CASES[kind]
    env = start(tmp_path, {"pom.xml": root, **APP}, vulns=vulns, versions=versions, world=world)
    before = env.tree_hash()
    assert env.run().status == AWAITING_APPROVAL
    _approve_or_reject(env, D_REJECTED)
    out = env.run()
    assert out.status == INCOMPLETE and out.reason == "rejected_proposals", (out.reason, out.reasons)
    assert env.paths.delivery_zip.read_bytes() == env.upload and env.tree_hash() == before
    assert len(env.ivas.uploads) == 1
    assert_clean_artifacts(env)


def test_baseline_build_failure_never_scans(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS,
                fail_on={"example:lib:1.2.3"})
    out = env.run()
    assert out.status == FAILED and out.reason == "baseline_build_failed"
    assert env.ivas.uploads == [] and out.delivery is None and not env.paths.plan(1).exists()
    assert out.baseline_build.log == "baseline/build.log" and "COMPILATION ERROR" in out.baseline_build.summary
    assert_clean_artifacts(env)


def test_candidate_build_failure_discards_the_candidate(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS,
                fail_on={"example:lib:1.2.4"}, failed_candidate="discard")
    before = env.tree_hash()
    out = env.run()
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery is None and not env.paths.delivery_zip.exists()
    assert env.tree_hash() == before and len(env.ivas.uploads) == 1
    assert "+      <version>1.2.4</version>" in env.paths.patch(1).read_text()
    assert out.rounds[0].build.log == "rounds/1/build.log"
    assert "결과 ZIP 은 없고" in env.paths.report_md.read_text(encoding="utf-8")
    assert_clean_artifacts(env)


def test_candidate_build_failure_is_delivered_with_include(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS,
                fail_on={"example:lib:1.2.4"})
    before = env.tree_hash()
    out = env.run()
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert out.delivery is not None and not out.delivery.build_ok and not out.delivery.scanned
    assert out.delivery.sha256 == sha(env.paths.delivery_zip.read_bytes())
    # the failed change set is in result.zip (same file set, one version line), never rescanned
    _old, new = assert_one_line_changed(env, "pom.xml")
    assert "1.2.4" in new
    assert len(env.ivas.uploads) == 1 and out.final_scan == ""
    assert env.tree_hash() == before
    assert "+      <version>1.2.4</version>" in env.paths.patch(1).read_text()
    report = env.paths.report_md.read_text(encoding="utf-8")
    first = "\n".join(report.splitlines()[:6])
    assert "빌드가 실패한 수정본" in first and "rounds/1/build.log" in first and "source.zip" in first
    assert "재검사를 하지 않았습니다" in first
    assert json.loads(env.paths.report_json.read_text(encoding="utf-8"))["delivery"]["build_ok"] is False
    assert_clean_artifacts(env)


def test_round_two_candidate_failure_keeps_the_verified_round_one_delivery(tmp_path):
    vulns = {"example:lib:1.2.3": Vuln("CVE-2026-1001", "1.2.4"), "example:lib:1.2.4": Vuln("CVE-2026-1002", "1.2.5")}
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=vulns, versions=LIB_VERSIONS,
                fail_on={"example:lib:1.2.5"}, failed_candidate="discard")
    out = env.run()
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert [(r.round, r.committed) for r in out.rounds] == [(1, True), (2, False)]
    # round 1's built, committed and rescanned delivery stays; round 2's change is not in it
    assert out.delivery is not None and out.delivery.round == 1
    assert out.delivery.sha256 == sha(env.paths.delivery_zip.read_bytes()) == sha(env.ivas.uploads[1])
    assert len(env.ivas.uploads) == 2
    _old, new = assert_one_line_changed(env, "pom.xml")
    assert "1.2.4" in new
    assert "<version>1.2.4</version>" in (env.paths.accepted / "pom.xml").read_text()
    assert "1.2.5" in env.paths.patch(2).read_text()
    assert env.events("earlier_delivery_kept")
    report = env.paths.report_md.read_text(encoding="utf-8")
    assert "라운드 1 에서 빌드·재검사를 마친 상태" in report and "결과 ZIP 은 없고" not in report
    assert "verified state of round 1" in out.message
    assert_clean_artifacts(env)


def test_round_two_candidate_failure_with_include_keeps_round_one_as_verified_file(tmp_path):
    vulns = {"example:lib:1.2.3": Vuln("CVE-2026-1001", "1.2.4"), "example:lib:1.2.4": Vuln("CVE-2026-1002", "1.2.5")}
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=vulns, versions=LIB_VERSIONS,
                fail_on={"example:lib:1.2.5"})
    out = env.run()
    assert out.status == FAILED and out.reason == "candidate_build_failed"
    assert [(r.round, r.committed) for r in out.rounds] == [(1, True), (2, False)]
    assert out.delivery.round == 2 and not out.delivery.build_ok and not out.delivery.scanned
    _old, new = assert_one_line_changed(env, "pom.xml")
    assert "1.2.5" in new
    verified = env.paths.delivery_zip.with_name("result-r1-verified.zip")
    assert verified.is_file() and sha(verified.read_bytes()) == sha(env.ivas.uploads[1])
    assert len(env.ivas.uploads) == 2
    assert "<version>1.2.4</version>" in (env.paths.accepted / "pom.xml").read_text()
    assert env.events("verified_delivery_kept")
    report = env.paths.report_md.read_text(encoding="utf-8")
    assert "빌드가 실패한 수정본" in "\n".join(report.splitlines()[:6])
    assert "delivery/result-r1-verified.zip" in report
    assert_clean_artifacts(env)


def test_two_rounds_succeed(tmp_path):
    vulns = {"example:lib:1.2.3": Vuln("CVE-2026-1001", "1.2.4"), "example:lib:1.2.4": Vuln("CVE-2026-1002", "1.2.5")}
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=vulns, versions=LIB_VERSIONS)
    out = env.run()
    assert_succeeded_delivery(env, out, uploads=3)
    assert out.delivery.round == 2
    _old, new = assert_one_line_changed(env, "pom.xml")
    assert "1.2.5" in new


def test_missing_ivas_page_is_an_incomplete_scan_not_zero_findings(tmp_path):
    vulns = {"example:lib:1.2.3": Vuln("CVE-2026-1001", "1.2.4"), "example:other:2.0.0": Vuln("CVE-2026-1003", "2.0.1")}
    world = World(catalog={"example:lib:1.2.3": ["example:other:2.0.0"]})
    vulns["example:x:1"] = Vuln("CVE-2026-1004", "2")
    env = start(tmp_path, {"pom.xml": war_pom(extra_deps=dep("example", "x", "1")), **APP}, vulns=vulns,
                versions=LIB_VERSIONS, world=world)
    env.ivas.broken_library_page.add(1)                   # 3 items, page size 2: page 1 never arrives
    out = env.run()
    assert out.status == INCOMPLETE and out.reason == "scan_incomplete"
    scan = read_json(env.paths.scan("baseline"))
    assert scan["complete"] is False and "page_missing" in scan["incomplete_reasons"]
    assert not env.paths.plan(1).exists() and out.delivery is None and len(env.ivas.uploads) == 1
    assert_clean_artifacts(env)


def test_uncertain_upload_is_never_sent_again(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS)
    env.ivas.uncertain_uploads.add(1)
    out = env.run()
    assert out.status == AWAITING_CONFIRMATION and out.reason == "upload_uncertain"
    assert out.pending_upload == {"baseline": sha(env.upload)} and len(env.ivas.uploads) == 1
    out = env.run()                                       # a plain resume: still waiting, nothing sent
    assert out.status == AWAITING_CONFIRMATION and len(env.ivas.uploads) == 1
    assert read_json(env.paths.state)["status"] == AWAITING_CONFIRMATION


def test_stop_during_analysis_then_resume_with_the_receipt(tmp_path):
    env = start(tmp_path, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions=LIB_VERSIONS)

    def stop_on_first_poll(aid: str, k: int) -> None:
        if aid == "anl-1" and k == 1:
            env.rec.stop_flag = True
    env.ivas.on_status = stop_on_first_poll
    out = env.run()
    assert out.status == STOPPED and out.next_node == "baseline_scan"
    assert out.pending_receipt["baseline"]["analysis_id"] == "anl-1" and len(env.ivas.uploads) == 1
    assert "cancel" not in json.dumps(read_json(env.paths.state)).lower()
    env.rec.stop_flag = False
    out = env.run()
    assert_succeeded_delivery(env, out)                   # baseline once + final once: no extra upload
    assert env.ivas.status_calls["anl-1"] == 2


def test_repository_source_with_unapproved_host_gives_no_candidate(tmp_path):
    class Lookup:
        def __init__(self, host: str):
            self._answered = {("example", "lib"): f"https://{host}/repository/public/"}

        def versions(self, g, a):
            return ["1.2.3", "1.2.4"]

        def lookup_trace(self, g, a):
            return []

    def run_with(host: str, sub: str) -> RemediationState:
        env = start(tmp_path / sub, {"pom.xml": war_pom(), **APP}, vulns=LIB_VULN, versions={})
        env.comps.versions = planner_mod.RepositoryVersionSource(env.cfg, lookup=Lookup(host),
                                                                 allowed_hosts=env.comps.maven.allowed_hosts)
        return env.run()

    assert run_with("nexus.invalid", "ok").status == SUCCEEDED
    out = run_with("repo.elsewhere.invalid", "bad")
    assert out.status == INCOMPLETE and out.reasons == ["residual_findings", "no_candidate_version"]
    assert "no evidence" in out.residuals[0].detail


def test_default_components_wiring(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    lines: list[dict] = []
    comps = graph.default_components(cfg, journal=lines.append)
    assert isinstance(comps.maven, ServerMaven) and comps.maven.journal is not None
    assert comps.versions.allowed_hosts() == {"nexus.invalid"}
    assert comps.plan is planner_mod.plan_round and comps.inventory is inventory_mod.build_inventory
    assert comps.intake is intake_mod and comps.apply is apply_mod
    assert graph.default_components(cfg).maven.journal is None             # backward compatible call


def test_outside_project_change_is_a_text_only_violation(tmp_path, monkeypatch):
    files = {"pom.xml": war_pom(), **APP}
    env = start(tmp_path, files, vulns=LIB_VULN, versions=LIB_VERSIONS, wrap="app")
    # a ZIP wrapped in one folder may still carry macOS metadata beside it
    real_apply = apply_mod.apply_proposals

    def tamper(project, proposals):
        res = real_apply(project, proposals)
        (Path(project).parent / "notes.txt").write_text("changed\n")
        return res

    # put a file beside the project folder into the accepted tree + manifest (the intake allows __MACOSX/ there)
    (env.paths.accepted / "notes.txt").write_text("orig\n")
    data = read_json(env.paths.manifest)
    data["entries"].append({"path": "notes.txt", "size": 5, "sha256": sha(b"orig\n")})
    write_json_atomic(env.paths.manifest, data)
    env.comps.apply = SimpleNamespace(apply_proposals=tamper, verify_text_only=apply_mod.verify_text_only,
                                      verify_resolution=apply_mod.verify_resolution)
    out = env.run()
    assert out.status == FAILED and out.reason == "text_only_violation"
    assert "outside the project folder" in out.message
    assert out.delivery is None


def test_intake_accepts_macos_metadata_beside_the_project(tmp_path):
    raw = {"app/pom.xml": war_pom().encode(), "app/src/A.java": b"class A {}\n", "__MACOSX/app/._pom.xml": b"\x00\x05"}
    up = tmp_path / "u.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in raw.items():
            zf.writestr(zipfile.ZipInfo(k, date_time=ZIP_DATE), v)
    up.write_bytes(buf.getvalue())
    cfg = make_cfg(tmp_path)
    res = intake_mod.accept_zip(up, tmp_path / "run", intake_mod.limits_from_config(cfg))
    assert res.project_root == "app" and res.files == 3

