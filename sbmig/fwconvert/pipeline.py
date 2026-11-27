"""사내 프레임워크 프로젝트 하나를 Spring Boot 프로젝트로 전환하는 모듈.
변환 단계를 차례로 호출하고 생성한 프로젝트를 출력 폴더에 쓴 뒤 요약을 반환한다.

Contract: out/scratch/fwconvert/ROUND2.md section P (the step order), docs/FW-CONVERT.md sections 3 and 4.

``convert_project`` never writes under ``src_root``. It refuses an ``out_root`` that is a file, is not empty, or
lies inside ``src_root`` (an error, nothing written). Nothing to convert (``has_convertible``: no SQL file whose root is
the query-map element with a query, no service file with an ``activity`` inside a ``service``; a Spring context named
``*-service.xml`` or a bean-dialect file does not count): ``converted=False`` and ``out_root`` is not touched (not
even created). ``has_convertible`` (the bool of ``convertible_summary``) is a tolerant textual test, like the converters'
readers: comments are removed, a SQL file counts when its first start tag has the query-map local name (any prefix)
and a ``query`` start tag follows anywhere (nested too); a service file counts when an ``activity`` start tag follows
a ``service`` start tag. Bad XML, undefined entities, a DOCTYPE do not matter. It examines ``HEAD_BYTES`` of text
outside comments per file (reading at most ``RAW_CAP`` raw bytes), stops reading at the first hit (later candidates
are only counted), skips the inventory's ``SKIP_DIRS`` (``build`` included, as ``scan_tree`` does) and links, and
reads no more than ``MAX_CANDIDATES`` candidates.

Border checks after the generators: ``ajax_targets_without_controller`` gives hand-over items, ``duplicate_mappings``
gives error lines. The screen check compares every source ``.jsp`` / ``.js`` / ``.css`` of the inventory with the
copy: copied and present = fine; a static file inside ``WEB-INF`` (refused on purpose by the copy) = a ``skipped``
line; anything else (a target collision, a read failure, a file gone after the copy) = hand-over item
``FRONTEND_MISSING``.

``static_top`` for the resource handlers is computed from the inventory with the copy's own path rule
(``frontcopy.target_rel``) before the copy runs, because the skeleton is generated first (the step order of the
contract). A static file that lands at the top of the static folder gets no handler: a ``skipped`` line says so.

Datasource: ``datasource.scan_datasource`` (``read_datasource`` plus notes); not configured = hand-over item
``[DATASOURCE]`` with the reason; a password in the source = ``[DATASOURCE_PASSWORD]`` naming the files, never the
value; a found vendor without a driver coordinate = ``[DATASOURCE_DRIVER]``; other datasources found = error lines.

Counts: ``services`` (service definitions read), ``mappers`` (mapper files), ``statements`` (their statements),
``resources``, ``java_files`` (distinct ``.java`` paths generated and written), ``jsp`` / ``static`` (files copied),
``js_rewritten`` (JS and JSP files the ajax rewrite changed), ``pages`` (page controllers), ``queue``.

``unexposed`` = the ``unexposed_endpoints`` lines, then the ``unreachable_methods`` lines prefixed ``no address: ``."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath

from sbmig.fwconvert.ajax import transform_ajax
from sbmig.fwconvert.apimap import extract_api_map, scan_skipped
from sbmig.fwconvert.datasource import DatasourceScan, scan_datasource
from sbmig.fwconvert.frontcopy import copy_frontend, file_kind, fix_jsp_paths, target_rel
from sbmig.fwconvert.identity import derive_identity
from sbmig.fwconvert.inventory import SKIP_DIRS, Inventory, risk_queue, scan_tree
from sbmig.fwconvert.javagen import (
    duplicate_mappings,
    generate_page_controllers,
    generate_resource,
    write_generated,
)
from sbmig.fwconvert.model import (
    ConvertOptions,
    ConvertResult,
    DatasourceInfo,
    GeneratedFile,
    Layout,
    MapperIndex,
    QueueItem,
)
from sbmig.fwconvert.resources import (
    ajax_targets_without_controller,
    build_resources,
    unexposed_endpoints,
    unreachable_methods,
)
from sbmig.fwconvert.rules import ConvertRules
from sbmig.fwconvert.servicexml import decompose_services
from sbmig.fwconvert.skeleton import check_options, driver_coordinate, generate_skeleton
from sbmig.fwconvert.sqlmap import convert_mappers

# 화면 검사에서 복사됐는지 확인하는 확장자
SCREEN_EXTENSIONS = (".jsp", ".js", ".css")
# 전환할 파일인지 볼 때 읽는 앞부분의 크기와, 살펴보는 파일 수의 상한
HEAD_BYTES = 1024 * 1024
# 주석을 건너뛰며 읽는 원본 바이트의 상한
RAW_CAP = 16 * 1024 * 1024
MAX_CANDIDATES = 5000
_CHUNK = 64 * 1024
_START_TAG = re.compile(rb"<(?![!?/])(?:[A-Za-z_][\w.\-]*:)?([A-Za-z_][\w.\-]*)")
_CDATA = re.compile(rb"<!\[CDATA\[.*?(?:\]\]>|\Z)", re.DOTALL)
# activity 형식의 모양. 접두사 없는 service 태그와, class 속성이 있는 접두사 없는 activity 태그다
_SERVICE_TAG = re.compile(rb"<service[\s/>]")
_ACTIVITY_TAG = re.compile(rb"<activity\s[^<>]{0,4000}?(?<![\w:.\-])class\s*=")


def _text_head(path: Path) -> bytes | None:
    """파일 앞부분에서 XML 주석을 뺀 바이트를 ``HEAD_BYTES``까지 반환한다. 일반 파일이 아니거나 읽지 못하면 None이다.
    주석은 세지 않지만 원본은 ``RAW_CAP``까지만 읽는다."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    out = bytearray()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = -1
            pending, in_comment, raw = b"", False, 0
            while len(out) < HEAD_BYTES and raw < RAW_CAP:
                chunk = fh.read(_CHUNK)
                raw += len(chunk)
                data = pending + chunk
                pending = b""
                while data:
                    if in_comment:
                        j = data.find(b"-->")
                        if j < 0:
                            # 주석 끝 표시가 두 조각에 걸칠 수 있어서 끝 두 바이트를 남긴다
                            pending = data[-2:] if chunk else b""
                            data = b""
                        else:
                            data, in_comment = data[j + 3:], False
                    else:
                        i = data.find(b"<!--")
                        if i < 0:
                            keep = len(data) - 3 if chunk else len(data)
                            out += data[:max(keep, 0)]
                            pending = data[max(keep, 0):]
                            data = b""
                        else:
                            out += data[:i]
                            data, in_comment = data[i + 4:], True
                if not chunk:
                    break
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    return bytes(out[:HEAD_BYTES])


def _looks_convertible(text: bytes, kind: str, rules: ConvertRules) -> bool:
    """주석과 CDATA를 뺀 앞부분의 태그만 보고 프레임워크의 SQL 파일이나 activity 형식 서비스인지 판단한다.
    변환기처럼 너그럽게 본다. 잘못된 XML이나 정의하지 않은 엔터티가 있어도 태그가 맞으면 센다.
    서비스는 접두사 없는 ``service`` 태그 뒤에 ``class`` 속성이 있는 접두사 없는 ``activity`` 태그가 있어야 한다."""
    text = _CDATA.sub(b" ", text)
    if kind == "sql":
        names = [m.group(1) for m in _START_TAG.finditer(text)]
        root = rules.text("files.sql_root", "queryMap").encode()
        query = rules.text("files.sql_query", "query").encode()
        return bool(names) and names[0] == root and query in names[1:]
    service = _SERVICE_TAG.search(text)
    return bool(service) and bool(_ACTIVITY_TAG.search(text, service.end()))


def convertible_summary(src_root: Path, rules: ConvertRules) -> tuple[bool, int]:
    """원본에 전환할 프레임워크 파일이 있는지와, 이름으로 후보가 된 파일 수를 반환한다.
    후보는 SQL 확장자나 서비스 XML 이름을 가진 파일이다. 전환할 파일을 찾은 뒤에는 이름만 센다.
    읽지 못한 파일과 폴더는 건너뛰고 예외를 던지지 않는다."""
    root = Path(src_root)
    if not root.is_dir():
        return False, 0
    sql_ext = rules.text("files.sql_extension").lower()
    svc = rules.text("files.service_xml_suffix").lower()
    found, seen = False, 0
    try:
        for cur, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(cur) / d).is_symlink())
            for name in sorted(names):
                low = name.lower()
                kind = "sql" if sql_ext and low.endswith(sql_ext) else "service" if svc and low.endswith(svc) else ""
                if not kind:
                    continue
                seen += 1
                if found or seen > MAX_CANDIDATES:
                    continue
                text = _text_head(Path(cur) / name)
                found = bool(text) and _looks_convertible(text, kind, rules)
    except OSError:
        return found, seen
    return found, seen


def has_convertible(src_root: Path, rules: ConvertRules) -> bool:
    """원본에 전환할 프레임워크 파일이 하나라도 있으면 True를 반환한다.
    SQL 파일은 첫 태그가 query-map 요소이고 그 뒤에 query 태그가 있어야 한다.
    서비스 XML은 ``service`` 태그 뒤에 ``activity`` 태그가 있어야 한다. Spring 설정 파일, bean 형식, 빈 파일은 세지 않는다."""
    return convertible_summary(src_root, rules)[0]


def _out_root_problem(src_root: Path, out_root: Path) -> str:
    try:
        if out_root.exists() and not out_root.is_dir():
            return f"{out_root}: the output folder is a file"
        if out_root.is_dir() and any(out_root.iterdir()):
            return f"{out_root}: the output folder is not empty, nothing written"
        out_real, src_real = out_root.resolve(), src_root.resolve()
    except (OSError, RuntimeError) as e:
        return f"{out_root}: cannot check the output folder ({e.__class__.__name__}: {e})"
    if out_real == src_real or out_real.is_relative_to(src_real):
        return f"{out_root}: the output folder lies inside the source folder, nothing written"
    return ""


def _in_web_inf(rel: str) -> bool:
    return any(p.upper() == "WEB-INF" for p in rel.split("/")[:-1])


def static_tops(inv: Inventory, rules: ConvertRules) -> tuple[list[str], list[str]]:
    """복사할 정적 파일의 최상위 폴더 이름과, 정적 파일 폴더 맨 위에 놓일 파일의 원본 경로를 반환한다.
    JSP와 ``WEB-INF`` 안의 정적 파일은 복사하지 않거나 리소스로 내보내지 않으므로 세지 않는다."""
    tops: set[str] = set()
    loose: list[str] = []
    for rel in inv.static:
        if file_kind(rel, rules) != "static" or _in_web_inf(rel):
            continue
        inner = target_rel(rel, rules)
        if "/" in inner:
            tops.add(inner.split("/", 1)[0])
        elif inner:
            loose.append(rel)
    return sorted(tops), sorted(loose)


def _datasource_queue(scan: DatasourceScan, options: ConvertOptions) -> list[QueueItem]:
    info = scan.info
    items: list[QueueItem] = []
    if not info.configured:
        why = {"not_found": "no datasource bean (a class ending in DataSource with a URL) in the project's "
                            "configuration files",
               "jndi": "the datasource is a JNDI lookup; the server's JNDI name cannot be carried into "
                       "application.yml",
               "unknown_vendor": "the JDBC URL and the driver class name no known database vendor",
               "unresolved": ("the JDBC URL was left out because it may carry credentials (user info or a "
                              "password parameter that could not be taken out safely)" if scan.url_withheld else
                              "the JDBC URL keeps a placeholder that no properties file of the project defines"),
               }.get(info.reason, info.reason or "unknown")
        items.append(QueueItem(component="datasource", reason=f"[DATASOURCE] {info.reason}: {why}; "
                               "application.yml has no spring.datasource keys", source_path=info.source))
    elif not driver_coordinate(options, info)[0]:
        items.append(QueueItem(component="datasource",
                               reason=f"[DATASOURCE_DRIVER] vendor {info.vendor} has no JDBC driver coordinate in "
                                      "the conversion settings; no driver dependency was added",
                               source_path=info.source))
    if info.has_password:
        where = ", ".join(scan.password_files) or info.source
        items.append(QueueItem(component="datasource",
                               reason=f"[DATASOURCE_PASSWORD] a database password is written in {where}; it was not "
                                      "carried over, application.yml reads ${SPRING_DATASOURCE_PASSWORD:}",
                               source_path=info.source))
    return items


def _screen_check(inv: Inventory, out_root: Path, copied: list[tuple[str, str]], rejected: list[str],
                  copy_errors: list[str]) -> tuple[list[QueueItem], list[str]]:
    target_of = dict(copied)
    refused = set(rejected)
    items: list[QueueItem] = []
    skipped: list[str] = []
    for rel in sorted(set(inv.jsp + inv.static)):
        if PurePosixPath(rel).suffix.lower() not in SCREEN_EXTENSIONS:
            continue
        if rel in refused:
            continue
        target = target_of.get(rel)
        if target is not None and (out_root / target).is_file():
            continue
        if target is not None:
            why = f"copied to {target} but not there any more"
        else:
            why = next((e[len(rel) + 2:] for e in copy_errors if e.startswith(rel + ": ")), "not copied")
        items.append(QueueItem(component=f"frontend::{rel}", reason=f"FRONTEND_MISSING {rel}: {why}",
                               source_path=rel))
    for rel in sorted(refused):
        skipped.append(f"{rel}: a static file inside WEB-INF, not copied (refused on purpose)")
    return items, skipped


def convert_project(src_root: Path, out_root: Path, rules: ConvertRules, options: ConvertOptions) -> ConvertResult:
    """원본 프로젝트를 읽어 생성한 Spring Boot 프로젝트를 ``out_root``에 쓰고 요약을 반환한다.
    원본 폴더에는 쓰지 않는다. 출력 폴더가 비어 있지 않거나 원본 안에 있으면 아무것도 쓰지 않고 오류를 남긴다.
    서비스 XML과 SQL 파일이 하나도 없으면 ``converted``가 False이고 출력 폴더는 건드리지 않는다.
    옵션에 Boot 버전이나 MyBatis 스타터 버전이 없으면 pom 없이 나머지만 쓰고 오류에 남긴다."""
    src, out = Path(src_root), Path(out_root)
    result = ConvertResult()
    problem = _out_root_problem(src, out)
    if problem:
        result.errors.append(problem)
        return result
    # 프레임워크의 SQL 파일이나 activity 형식 서비스가 없으면 전환하지 않는다. 출력 폴더도 만들지 않는다
    if not has_convertible(src, rules):
        return result
    inv = scan_tree(src, rules)
    identity = derive_identity(options.source_name, rules, domain=options.domain, override=options.override)
    layout = Layout(identity)
    result.converted = True
    result.identity = identity

    mapped = convert_mappers(src, inv.sql, inv.mapper, layout, rules)
    index = MapperIndex(mapped.mappers)
    services = decompose_services(src, inv.service_xml, inv.java, index, rules)
    # 변환기가 실제로 읽은 것이 없으면 전환하지 않는다. 복사할 MyBatis mapper만 있는 것은 프레임워크 프로젝트가 아니다
    activity_services = [d for d in services.services if d.dialect == "activity" and d.activities]
    converted_statements = sum(len(m.statements) for m in mapped.mappers if m.origin == "converted")
    if not activity_services and not converted_statements:
        return ConvertResult(errors=inv.errors + mapped.errors + services.errors)
    front = inv.jsp + inv.static
    api_map = extract_api_map(src, rules, front)
    resources = build_resources(services.services, index, api_map, rules)
    files: list[GeneratedFile] = list(mapped.files)
    queue: list[QueueItem] = risk_queue(inv.components, src, rules) + list(services.queue)
    for res in resources:
        made, items = generate_resource(res, layout, rules)
        files += made
        queue += items

    scan = scan_datasource(src, inv.db_config, rules)
    datasource: DatasourceInfo = scan.info
    result.datasource = datasource
    tops, loose = static_tops(inv, rules)
    option_errors = check_options(options, datasource)
    files += generate_skeleton(layout, options, datasource, rules, tops)
    write_errors = write_generated(out, files)

    copied = copy_frontend(src, out, layout, rules, front)
    moved = fix_jsp_paths(out, layout, rules, copied.copied)
    ajax = transform_ajax(out, layout, rules)
    static = layout.static_dir + "/"
    jsps = sorted(t[len(static):] for _, t in copied.copied if t.startswith(static) and t.endswith(".jsp"))
    pages = generate_page_controllers(api_map, jsps, layout, rules,
                                      reserved=[PurePosixPath(f.path).stem for f in files if f.path.endswith(".java")])
    page_errors = write_generated(out, pages.files)
    all_files = files + pages.files

    queue += moved.queue + ajax.queue + ajax_targets_without_controller(ajax.services, resources, rules)
    missing, refused = _screen_check(inv, out, copied.copied, copied.rejected_web_inf, copied.errors)
    queue += missing + _datasource_queue(scan, options)
    clashes = duplicate_mappings(all_files)

    result.errors = (option_errors + clashes + inv.errors + mapped.errors + services.errors + write_errors
                     + copied.errors + moved.errors + ajax.errors + page_errors + scan.notes)
    result.queue = queue
    result.unexposed = unexposed_endpoints(resources, rules)
    # 주소가 아예 없는 메서드도 함께 남긴다. 앞에 붙인 말로 두 경우를 구별한다
    result.unexposed += [f"no address: {line}" for line in unreachable_methods(resources, rules)]
    result.skipped = (scan_skipped(src, rules, front) + refused
                      + [f"{rel}: lands at the top of the static folder; no resource handler serves it" for rel in loose]
                      + [f"page: {s}" for s in pages.skipped])
    failed = {e.split(":", 1)[0] for e in write_errors + page_errors}
    java = {f.path for f in all_files if f.path.endswith(".java") and f.path not in failed}
    result.counts = {
        "services": len(services.services),
        "mappers": len(mapped.mappers),
        "statements": sum(len(m.statements) for m in mapped.mappers),
        "resources": len(resources),
        "java_files": len(java),
        "jsp": copied.by_kind.get("jsp", 0),
        "static": copied.by_kind.get("static", 0),
        "js_rewritten": len(ajax.files_transformed),
        "pages": len(pages.files),
        "queue": len(queue),
    }
    return result
