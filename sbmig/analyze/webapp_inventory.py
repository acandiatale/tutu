"""pom 이 없는 Tomcat 웹앱 트리(풀어 놓은 WAR: JSP + 자바 소스 + ``WEB-INF/lib`` jar)의 읽기 전용
목록(``sbmig inventory``). 왜 있나: 회사 결정(2026-10-23) — pom 없는 트리는 마이그레이션하지 않고 접수에서
거부한다(pom 합성 없음, JSP 정리 없음). 이 모듈은 그 거부 안내문에 붙일 "이 트리가 무엇인지" 를 담당 팀에
설명하는 도구일 뿐, 아무것도 바꾸지 않는다. 흐름상 위치: CLI ``sbmig inventory <dir>`` 가
:func:`scan_webapp_tree` 를 부르고 ``sbmig.report.inventory`` 가 보고서로 쓴다(마이그레이션 실행 흐름 밖).
모으는 것: web.xml 스펙·서블릿·프레임워크, JNDI 이름, JSP / 정적 파일 수, 파일 이름으로 추정한 jar
좌표(``knowledge/libraries/jar_names.yml``), class 파일의 Java 수준, 소스 루트, 중복 클래스, 웹앱 루트마다의
Maven 모듈 제안(참고용). 파일 하나가 깨져도 경고일 뿐 예외는 없다.

Read-only inventory of a Tomcat webapp tree WITHOUT a pom (`sbmig inventory`, 2026-10-23).

Classic exploded WARs: JSPs, Java sources somewhere (often `WEB-INF/src`), `WEB-INF/lib` jars (plus `*.jar.old`
backups), `WEB-INF/classes`. `scan_webapp_tree(root)` collects file-system facts only — web.xml spec / servlets /
frameworks, JNDI names, JSP / static counts, jar coordinates guessed from the file name
(`knowledge/libraries/jar_names.yml`), class-file Java levels, source roots (where `package a.b;` matches the
path), duplicate classes — and proposes one Maven module per webapp root. Nothing is changed; a bad single file is
a warning, never an exception. The pom synthesis of the next round consumes the `WebappInventory`.


"""
from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from sbmig.core.models import JarInfo, ModuleProposal, SourceRoot, WebappInventory, WebappRoot
from sbmig.core.workspace import detect_encoding

from .source_scanner import _ns_key, scan_java_text

log = logging.getLogger("sbmig.analyze")

JAR_NAMES_FILE = Path(__file__).resolve().parents[2] / "knowledge" / "libraries" / "jar_names.yml"
SKIP_DIRS = frozenset({".git", "target", ".svn", "node_modules", "__MACOSX", ".idea", ".hg"})
BUILD_FILE_NAMES = frozenset({"pom.xml", "build.xml", ".classpath", ".project", "build.gradle", "build.gradle.kts"})
JSP_EXT = frozenset({".jsp", ".jspf", ".jspx"})
STATIC_EXT = frozenset({".html", ".htm", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff",
                        ".woff2", ".ttf", ".eot"})
MAX_XML_BYTES = 2_000_000
MAX_JSP_BYTES = 1_000_000

_VERSION_RE = re.compile(r"^(?P<stem>.+?)[-_](?P<ver>\d[\w.\-]*?)\.jar$", re.I)
_SPLIT_RE = re.compile(r"[-_](?=\d)")
_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_WEBAPP_VERSION_RE = re.compile(r"<web-app\b[^>]*?\bversion\s*=\s*[\"']([\d.]+)[\"']", re.S)
_DTD_RE = re.compile(r"web-app_(\d)_(\d)\.dtd")
_CLASS_VALUE_RE = re.compile(r"<(?:servlet|filter|listener)-class>\s*([^<\s]+)\s*</", re.S)
_RESOURCE_NAME_RE = re.compile(r"<Resource\b[^>]*?\bname\s*=\s*[\"']([^\"']+)[\"']", re.S)
_RES_REF_RE = re.compile(r"<(?:res-ref-name|resource-env-ref-name)>\s*([^<]+?)\s*</", re.S)
_JSP_JAVAX_IMPORT_RE = re.compile(r"<%@\s*page\b[^%]*\bimport\s*=\s*[\"'][^\"']*\bjavax\.", re.S)

# 프레임워크 분류(import 접두어). 한 파일이 여러 분류에 동시에 셀 수 있다
# framework categories of the imports (first matching prefixes win nothing: a file can count for several)
FRAMEWORK_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("spring", ("org.springframework.",)),
    ("struts1", ("org.apache.struts.",)),
    ("struts2", ("org.apache.struts2.", "com.opensymphony.xwork2.")),
    ("ibatis2", ("com.ibatis.", "org.springframework.orm.ibatis.")),
    ("mybatis", ("org.apache.ibatis.", "org.mybatis.")),
    ("hibernate", ("org.hibernate.",)),
    ("egov", ("egovframework.", "org.egovframe.")),
)
JDBC_PREFIXES = ("java.sql.", "javax.sql.")
JDK_INTERNAL_PREFIXES = ("sun.", "com.sun.")
CLASS_MAJOR_JAVA = {45: "1.1", 46: "1.2", 47: "1.3", 48: "1.4", 49: "1.5", 50: "1.6", 51: "1.7", 52: "1.8"}


# ------------------------------------------------------------------------------------------------ jar names table
@lru_cache(maxsize=4)
def jar_names(path: Optional[str] = None) -> dict[str, tuple[str, str]]:
    """jar 이름 표(``knowledge/libraries/jar_names.yml``) →
    ``{소문자 stem: (groupId:artifactId, 메모)}``. 못 읽으면 {}.

    `knowledge/libraries/jar_names.yml` → {stem.lower(): (groupId:artifactId, note)}; {} when unreadable."""
    p = Path(path) if path else JAR_NAMES_FILE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("jar name table %s unreadable: %s", p, e)
        return {}
    out: dict[str, tuple[str, str]] = {}
    for row in data.get("jars") or []:
        if isinstance(row, dict) and row.get("stem") and row.get("artifact"):
            out[str(row["stem"]).strip().lower()] = (str(row["artifact"]).strip(), str(row.get("note") or ""))
    return out


def _jar_base(name: str) -> Optional[str]:
    """jar 파일 이름(백업 ``x-1.0.jar.old`` 는 ``x-1.0.jar`` 로), jar 가 아니면 None.

    `x-1.0.jar` → `x-1.0.jar`; a backup `x-1.0.jar.old` → `x-1.0.jar`; anything else None."""
    low = name.lower()
    if low.endswith(".jar"):
        return name
    i = low.rfind(".jar.")
    return name[:i + 4] if i > 0 else None


def guess_jar(name: str) -> tuple[Optional[str], Optional[str], str]:
    """jar 파일 이름에서 (추정 좌표, 추정 버전, stem). 표가 아는 가장 긴 stem
    이 이기고(``commons-lang3-3.1.jar`` → commons-lang3 / 3.1), 모르는 이름은 정규식으로 이름-버전을 가르거나
    ``.jar`` 를 뗀 이름 전체를 stem 으로 쓴다.

    (artifact_guess, version_guess, stem) of a jar file name. The longest stem the table knows wins (`commons-
    lang3-3.1.jar` → commons-lang3 / 3.1, `ojdbc6.jar` → ojdbc6 / None); unknown names fall back to the regex split
    (`acme-util-1.2.jar` → acme-util / 1.2) or to the whole name without `.jar`."""
    base = _jar_base(name) or name
    bare = base[:-4] if base.lower().endswith(".jar") else base
    table = jar_names()
    hit = table.get(bare.lower())
    if hit:
        return hit[0], None, bare
    for m in reversed(list(_SPLIT_RE.finditer(bare))):          # longest stem first
        stem, ver = bare[:m.start()], bare[m.end():]
        hit = table.get(stem.lower())
        if hit:
            return hit[0], ver or None, stem
    m = _VERSION_RE.match(base)
    if m:
        return None, m.group("ver"), m.group("stem")
    return None, None, bare


def jar_note(jar: JarInfo) -> str:
    """jar 의 표 메모(없으면 "") — 인벤토리 보고서의 ``note`` 칸.

    The table note for a jar ("" when none) — the writer's `note` column."""
    _, _, stem = guess_jar(jar.name)
    hit = jar_names().get(stem.lower())
    return hit[1] if hit else ""


def java_level(major: Optional[int]) -> Optional[str]:
    """class 파일 major 버전 → Java 수준(45 → 1.1 … 52 → 1.8, 53 이상은 major − 44). 모르면 None."""
    if not major:
        return None
    return CLASS_MAJOR_JAVA.get(major) or (str(major - 44) if major > 52 else None)


# ------------------------------------------------------------------------------------------------ helpers
def _under(rel: str, d: str) -> bool:
    return d == "" or rel == d or rel.startswith(d + "/")


def _join(d: str, *parts: str) -> str:
    return posixpath.join(d, *parts) if d else posixpath.join(*parts)


def _read_text(p: Path, limit: int, warnings: list[str], rel: str) -> str:
    """파일 앞부분(``limit`` 바이트)을 utf-8 → cp949 → latin-1 순으로 디코딩. 못 읽으면 경고 + ""."""
    try:
        data = p.read_bytes()[:limit]
    except OSError as e:
        warnings.append(f"{rel}: unreadable ({e})")
        return ""
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _sha1(p: Path) -> str:
    """파일 sha1(64 KB 씩 읽음) — 같은 jar 의 사본을 알아보는 데 쓴다."""
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _class_major(p: Path) -> Optional[int]:
    """class 파일 헤더(CAFEBABE 다음 major 버전)를 읽는다. class 파일이 아니면 None."""
    try:
        with p.open("rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != b"\xca\xfe\xba\xbe":
        return None
    return int.from_bytes(head[6:8], "big")


def _sample(items, n: int) -> list[str]:
    return sorted(items)[:n]


def _walk(root: Path) -> tuple[list[str], set[str]]:
    """루트 아래 모든 파일(posix 상대 경로)과 모든 디렉토리.
    ``SKIP_DIRS`` (.git, target, node_modules …)는 건너뛴다.

    All files (root-relative posix) and all directories, skipping SKIP_DIRS."""
    files: list[str] = []
    dirs: set[str] = {""}
    for cur, dnames, fnames in os.walk(root):
        dnames[:] = sorted(d for d in dnames if d not in SKIP_DIRS)
        rel_dir = Path(cur).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        for d in dnames:
            dirs.add(_join(rel_dir, d))
        for f in sorted(fnames):
            files.append(_join(rel_dir, f))
    return files, dirs


# ------------------------------------------------------------------------------------------------ webapp roots
def _scan_web_xml(root: Path, wr: WebappRoot, warnings: list[str]) -> None:
    """web.xml 을 정규식으로 훑는다(스펙, 서블릿·필터·리스너·taglib 수, 서블릿 클래스로 본
    프레임워크, JNDI 참조). 완전 파싱(:mod:`sbmig.analyze.webxml`)이 아니라 목록용 대략치다."""
    if not wr.web_xml:
        return
    text = _XML_COMMENT_RE.sub("", _read_text(root / wr.web_xml, MAX_XML_BYTES, warnings, wr.web_xml))
    m = _WEBAPP_VERSION_RE.search(text)
    d = _DTD_RE.search(text)
    wr.servlet_spec = m.group(1) if m else (f"{d.group(1)}.{d.group(2)}" if d else None)
    wr.servlets = len(re.findall(r"<servlet[\s>]", text))
    wr.filters = len(re.findall(r"<filter[\s>]", text))
    wr.listeners = len(re.findall(r"<listener[\s>]", text))
    wr.taglibs = len(re.findall(r"<taglib[\s>]", text))
    fw: list[str] = []
    for cls in _CLASS_VALUE_RE.findall(text):
        name = None
        if cls == "org.springframework.web.servlet.DispatcherServlet":
            name = "spring-mvc"
        elif cls == "org.apache.struts.action.ActionServlet":
            name = "struts1"
        elif cls.startswith("org.apache.struts2.dispatcher.") or cls.endswith("FilterDispatcher"):
            name = "struts2"
        elif cls in ("javax.faces.webapp.FacesServlet", "jakarta.faces.webapp.FacesServlet"):
            name = "jsf"
        if name and name not in fw:
            fw.append(name)
    if "egovframework" in text and "egov" not in fw:
        fw.append("egov")
    if not fw and wr.servlets > 0:
        fw.append("plain-servlet")
    wr.frameworks = fw
    for n in _RES_REF_RE.findall(text):
        if n not in wr.jndi_resources:
            wr.jndi_resources.append(n)


def _scan_lib(root: Path, wr: WebappRoot, lib_files: list[str], warnings: list[str]) -> None:
    """``WEB-INF/lib`` 의 jar 마다 크기·sha1·추정 좌표를 적는다. ``*.jar.old`` 백업은 무시 표시,
    같은 라이브러리의 버전이 여럿이면 경고(Tomcat 이 둘 다 올린다)."""
    lib = _join(wr.dir, "WEB-INF", "lib")
    by_stem: dict[str, list[str]] = defaultdict(list)
    for rel in lib_files:
        name = posixpath.basename(rel)
        direct = posixpath.dirname(rel) == lib
        low = name.lower()
        if direct and low.endswith(".jar"):
            ignored = False
        elif direct and _jar_base(name) is not None:
            ignored = True
        else:
            wr.ignored_files.append(rel)
            continue
        p = root / rel
        try:
            size, sha1 = p.stat().st_size, _sha1(p)
        except OSError as e:
            warnings.append(f"{rel}: unreadable ({e})")
            size, sha1 = 0, ""
        art, ver, stem = guess_jar(name)
        wr.lib_jars.append(JarInfo(path=rel, name=name, size=size, sha1=sha1, version_guess=ver,
                                   artifact_guess=art, ignored=ignored))
        if ignored:
            wr.ignored_files.append(rel)
        else:
            by_stem[(art or stem).lower()].append(name)
    for key, names in sorted(by_stem.items()):
        if len(names) > 1:
            warnings.append(f"{lib}: {len(names)} versions of the same library ({key}): {', '.join(sorted(names))} "
                            "— Tomcat loads both; keep one")


def _scan_classes(root: Path, wr: WebappRoot, class_files: list[str]) -> dict[str, str]:
    """``WEB-INF/classes`` 의 class 파일 Java 수준(최소 / 최대)을 적고
    ``{바깥 클래스 FQCN: 경로}`` 를 돌려준다 (소스 없는 클래스를 찾는 데 쓴다).

    Class-file majors of WEB-INF/classes; returns {outer FQCN: rel path}."""
    base = _join(wr.dir, "WEB-INF", "classes") + "/"
    majors: list[int] = []
    fqcns: dict[str, str] = {}
    for rel in class_files:
        wr.classes_count += 1
        m = _class_major(root / rel)
        if m:
            majors.append(m)
        inner = rel[len(base):-len(".class")]
        if "$" in posixpath.basename(inner):
            continue
        fqcns[inner.replace("/", ".")] = rel
    if majors:
        wr.class_major_min, wr.class_major_max = min(majors), max(majors)
        wr.java_version_from_classes = java_level(wr.class_major_max)
    return fqcns


# ------------------------------------------------------------------------------------------------ sources
def _source_root_of(rel: str, package: str) -> tuple[str, bool]:
    """``package a.b;`` 선언과 디렉토리가 맞으면 (그 위 디렉토리
    = 소스 루트, True), 안 맞으면 (자기 디렉토리, False)."""
    d = posixpath.dirname(rel)
    if not package:
        return d, True
    pp = package.replace(".", "/")
    if d == pp:
        return "", True
    if d.endswith("/" + pp):
        return d[:-len(pp) - 1], True
    return d, False


def _inside(d: str, webapp_dirs: list[str]) -> Optional[str]:
    """``d`` 를 품는 가장 깊은 웹앱 루트(없으면 None).

    The deepest webapp root dir `d` lies under (or None)."""
    best: Optional[str] = None
    for w in webapp_dirs:
        if _under(d, w) and (best is None or len(w) > len(best)):
            best = w
    return best


# ------------------------------------------------------------------------------------------------ main entry
def scan_webapp_tree(root: Path, *, max_sample: int = 8, name: Optional[str] = None) -> WebappInventory:
    """pom 없는 웹앱 트리의 목록(읽기 전용) → ``WebappInventory``. 순서: 빌드 파일 → 웹앱 루트(WEB-INF 가 있는
    디렉토리) → web.xml / context.xml / lib / classes / JSP → 소스 루트 → 소스 없는 클래스 → 모듈 제안.
    ``name`` 은 제안에서 루트 폴더 이름 대신 쓸 이름(zip 을 임시 폴더에 푼 경우).

    Inventory of a pom-less webapp tree (read only). See the module docstring. `name` replaces the root's
    basename in the proposals (a zip extracted to a temp dir)."""
    root = Path(root).resolve()
    inv = WebappInventory(root=str(root))
    warnings = inv.warnings
    files, dirs = _walk(root)

    # 1. build files
    inv.build_files = [f for f in files if posixpath.basename(f) in BUILD_FILE_NAMES]
    for f in inv.build_files:
        if posixpath.basename(f) == "pom.xml":
            warnings.append(f"this tree has a pom at {f}: use `sbmig run` for that module")

    # 웹앱 루트 = WEB-INF 를 자식으로 가진 디렉토리. WEB-INF
    # 안에 또 있는 WEB-INF(백업 사본 등)는 루트로 치지 않는다
    # 2. webapp roots: directories with a WEB-INF child (WEB-INF nested inside another WEB-INF does not count)
    webapp_dirs = sorted(posixpath.dirname(d) if "/" in d else "" for d in dirs
                         if posixpath.basename(d) == "WEB-INF" and "WEB-INF" not in d.split("/")[:-1])
    if not webapp_dirs:
        warnings.append("no WEB-INF directory anywhere: this is not an exploded webapp")
    roots: list[WebappRoot] = []
    class_fqcns: dict[str, dict[str, str]] = {}
    jsp_javax_by_dir: dict[str, int] = {}               # JSPs with `<%@ page import="javax.…" %>` → proposal notes
    for wd in webapp_dirs:
        wr = WebappRoot(dir=wd)
        web_inf = _join(wd, "WEB-INF")
        nested = [o for o in webapp_dirs if o != wd and _under(o, wd)]

        # 이 루트의 파일 = 루트 아래이면서 안쪽에 중첩된 다른 웹앱
        # 루트 아래는 아닌 것 (기본 인자로 루프 변수를 고정한다)
        def mine(rel: str, _wd=wd, _nested=nested) -> bool:
            return _under(rel, _wd) and not any(_under(rel, o) for o in _nested)

        wx = _join(web_inf, "web.xml")
        if wx in files:
            wr.web_xml = wx
        else:
            warnings.append(f"{web_inf} has no web.xml (Servlet 3 annotations only, or an incomplete copy)")
        try:
            _scan_web_xml(root, wr, warnings)
        except Exception as e:  # noqa: BLE001 — one bad web.xml must not stop the inventory
            warnings.append(f"{wx}: could not be read ({e})")
        ctx = _join(wd, "META-INF", "context.xml")
        if ctx in files:
            text = _XML_COMMENT_RE.sub("", _read_text(root / ctx, MAX_XML_BYTES, warnings, ctx))
            ctx_names = list(dict.fromkeys(_RESOURCE_NAME_RE.findall(text)))
            wr.jndi_resources = ctx_names + [n for n in wr.jndi_resources if n not in ctx_names]
        lib_dir = _join(web_inf, "lib")
        cls_dir = _join(web_inf, "classes")
        jsps: list[str] = []
        jsp_javax = 0
        lib_files: list[str] = []
        class_files: list[str] = []
        for rel in files:
            if not mine(rel):
                continue
            ext = posixpath.splitext(rel)[1].lower()
            if _under(rel, lib_dir):
                lib_files.append(rel)
                continue
            if ext in JSP_EXT:
                jsps.append(rel)
                try:
                    if _JSP_JAVAX_IMPORT_RE.search(_read_text(root / rel, MAX_JSP_BYTES, warnings, rel)):
                        jsp_javax += 1
                except Exception:  # noqa: BLE001
                    pass
            elif ext in STATIC_EXT and not _under(rel, web_inf):
                wr.static_count += 1
            elif ext == ".class" and _under(rel, cls_dir):
                class_files.append(rel)
            elif ext == ".xml" and _under(rel, web_inf):
                if "<beans" in _read_text(root / rel, MAX_XML_BYTES, warnings, rel):
                    wr.spring_xml.append(rel)
        wr.jsp_count, wr.jsp_sample = len(jsps), _sample(jsps, max_sample)
        _scan_lib(root, wr, lib_files, warnings)
        class_fqcns[wd] = _scan_classes(root, wr, class_files)
        if wr.class_major_min and wr.class_major_max and wr.class_major_min != wr.class_major_max:
            warnings.append(f"{cls_dir}: class files of mixed Java levels "
                            f"({java_level(wr.class_major_min)} … {java_level(wr.class_major_max)})")
        jsp_javax_by_dir[wd] = jsp_javax
        roots.append(wr)
    inv.webapp_roots = roots

    # 3. source roots
    groups: dict[str, list[tuple[str, object, str, bool]]] = defaultdict(list)
    no_package: list[str] = []
    mismatched: list[str] = []
    for rel in files:
        if not rel.endswith(".java") or "/WEB-INF/lib/" in "/" + rel:
            continue
        p = root / rel
        try:
            data = p.read_bytes()
            enc = detect_encoding(p)
            text = data.decode(enc, errors="replace")
            info = scan_java_text(rel, text)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{rel}: could not be read ({e})")
            continue
        sroot, ok = _source_root_of(rel, info.package)
        if not info.package:
            no_package.append(rel)
        elif not ok:
            mismatched.append(rel)
        groups[sroot].append((rel, info, enc, all(b < 0x80 for b in data)))
    if no_package:
        warnings.append(f"{len(no_package)} .java file(s) without a package declaration (default package): "
                        + ", ".join(_sample(no_package, max_sample)))
    if mismatched:
        warnings.append(f"{len(mismatched)} .java file(s) whose package does not match the directory (their own "
                        "directory is taken as the source root): " + ", ".join(_sample(mismatched, max_sample)))
    fqcn_roots: dict[str, set[str]] = defaultdict(set)
    sroots: list[SourceRoot] = []
    for sdir in sorted(groups):
        entries = groups[sdir]
        sr = SourceRoot(dir=sdir, java_files=len(entries), inside_webapp=_inside(sdir, webapp_dirs))
        pkgs: set[str] = set()
        encs: Counter = Counter()
        javax: Counter = Counter()
        fws: Counter = Counter()
        jdbc = 0
        for _rel, info, enc, ascii_only in entries:
            if info.package:
                pkgs.add(".".join(info.package.split(".")[:2]))
            if not ascii_only:
                encs["utf-8" if enc == "utf-8-sig" else enc] += 1
            imps = info.imports
            for k in {_ns_key(i, "javax") for i in imps} - {None}:
                javax[k] += 1
            for fw_name, prefixes in FRAMEWORK_IMPORTS:
                if any(i.startswith(prefixes) for i in imps):
                    fws[fw_name] += 1
            if any(i.startswith(JDBC_PREFIXES) for i in imps):
                jdbc += 1
            if any(i.startswith(JDK_INTERNAL_PREFIXES) for i in imps):
                sr.jdk_internal_imports += 1
            fqcn_roots[info.fqcn].add(sdir)
        if not fws and jdbc:
            fws["jdbc-only"] = jdbc
        sr.packages = sorted(pkgs)
        sr.encoding = sorted(encs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if encs else "utf-8"
        if len(encs) > 1:
            warnings.append(f"{sdir or '.'}: sources in mixed encodings ({', '.join(sorted(encs))})")
        sr.javax_imports = dict(sorted(javax.items()))
        sr.framework_imports = dict(sorted(fws.items()))
        sroots.append(sr)
    inv.source_roots = sroots
    dups = sorted(f for f, rs in fqcn_roots.items() if len(rs) > 1)
    inv.duplicate_classes, inv.duplicate_classes_sample = len(dups), dups[:max_sample]

    # classes without source
    for wr in roots:
        missing = sorted(f for f in class_fqcns.get(wr.dir, {}) if f not in fqcn_roots)
        wr.classes_without_source, wr.classes_without_source_sample = len(missing), missing[:max_sample]

    # 4. proposals
    inv.proposals = _proposals(name or root.name, inv, max_sample, jsp_javax_by_dir)
    return inv


def _proposals(root_name: str, inv: WebappInventory, max_sample: int,
               jsp_javax_by_dir: dict[str, int]) -> list[ModuleProposal]:
    """웹앱 루트마다 Maven 모듈 하나를 제안한다(이름, 소스 디렉토리, Java 수준, 의존성 추정 수, 메모). 루트가
    없고 소스만 있으면 jar 모듈 하나. 참고용 제안일 뿐이다 — pom 합성은 회사 결정으로 하지 않는다.
    주의: 메모 문구의 "source-encoding handler" 는 v1 핸들러 이름이다(v2 에는 없음)."""
    roots, sroots = inv.webapp_roots, inv.source_roots
    majors = [wr.class_major_max for wr in roots if wr.class_major_max]
    overall = java_level(max(majors)) if majors else None
    outside = [s.dir for s in sroots if s.inside_webapp is None]
    out: list[ModuleProposal] = []
    if not roots:
        if sroots:
            inv.warnings.append("no webapp root, only Java sources: proposing one jar module")
            mp = ModuleProposal(name=root_name, webapp_dir="", packaging="jar",
                                source_dirs=[s.dir for s in sroots], java_version=overall or "1.8")
            _source_notes(mp, sroots)
            out.append(mp)
        return out
    single = len(roots) == 1
    for wr in roots:
        name = posixpath.basename(wr.dir) if wr.dir else root_name
        if single:
            src = [s.dir for s in sroots]
        else:
            src = [s.dir for s in sroots if s.inside_webapp == wr.dir] + outside
        mp = ModuleProposal(name=name, webapp_dir=wr.dir, source_dirs=src,
                            java_version=wr.java_version_from_classes or (overall if single else None) or "1.8")
        jars = [j for j in wr.lib_jars if not j.ignored]
        unknown = [j.name for j in jars if not j.artifact_guess]
        mp.dependencies_known = len(jars) - len(unknown)
        mp.dependencies_unknown = len(unknown)
        if not wr.java_version_from_classes:
            mp.notes.append(f"no class files: Java level {mp.java_version} assumed")
        if unknown:
            mp.notes.append(f"{len(unknown)} jar(s) without coordinates → lib/ + system scope or Nexus install: "
                            + ", ".join(sorted(unknown)[:max_sample]))
        if wr.servlet_spec in ("2.2", "2.3"):
            mp.notes.append(f"web.xml {wr.servlet_spec} DTD → the jakarta recipe rewrites it to the Servlet 6 schema")
        if wr.classes_without_source:
            mp.notes.append(f"{wr.classes_without_source} class(es) have no source: they can only be packaged "
                            "as a jar")
        if wr.jndi_resources:
            mp.notes.append("JNDI resources (" + ", ".join(wr.jndi_resources) + ") → spring.datasource.* or "
                            "Tomcat context.xml")
        jsp_javax = jsp_javax_by_dir.get(wr.dir, 0)
        if jsp_javax:
            mp.notes.append(f"{jsp_javax} JSP(s) import javax.* classes: recipes do not rewrite JSPs — check them "
                            "after the jakarta migration")
        if outside and not single:
            mp.notes.append("shared sources (" + ", ".join(d or "." for d in outside) + ") → a `common` jar module "
                            "would be needed")
        _source_notes(mp, [s for s in sroots if s.dir in src])
        out.append(mp)
    return out


def _source_notes(mp: ModuleProposal, sroots: list[SourceRoot]) -> None:
    """소스 루트에서 나온 메모를 제안에 더한다: UTF-8 이 아닌 인코딩(cp949 / euc-kr → MS949 로 표기),
    sun.* / com.sun.* 같은 JDK 내부 API import."""
    encs = sorted({s.encoding for s in sroots if s.encoding not in ("utf-8", "ascii")})
    for enc in encs:
        label = "MS949" if enc.lower().replace("-", "_") in ("cp949", "ms949", "euc_kr") else enc
        mp.notes.append(f"sources are {label} ({enc}): the source-encoding handler sets project.build.sourceEncoding")
    if any(s.jdk_internal_imports for s in sroots):
        n = sum(s.jdk_internal_imports for s in sroots)
        mp.notes.append(f"{n} source file(s) import sun.* / com.sun.*: JDK internals, see the jdk-removed-apis advisory")
