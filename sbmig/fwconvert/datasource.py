"""원본 프로젝트의 Spring XML과 properties 파일에서 DB 연결 정보를 읽는 모듈.
비밀번호는 있었는지만 기록하고 값은 어디에도 옮기지 않는다.

Contract: docs/FW-CONVERT.md section 2.6, out/scratch/fwconvert/ROUND2.md section P.

What counts as a datasource, in file order (the ``db_config`` files as the inventory lists them, sorted), then in
document order:
- a ``<bean>`` (any namespace) whose class's simple name ends with ``DataSource`` (case ignored) AND that has one of
  the URL properties (a proxy or routing datasource without a URL is not one);
- a JNDI lookup: an element named ``jndi-lookup`` (``jee:jndi-lookup``) or a bean of class ``…JndiObjectFactoryBean``.
The first one wins; every other one is named in ``DatasourceScan.notes`` (the record ``DatasourceInfo`` has no field
for them and its ``reason`` is a code).

Properties are read from ``<property name=… value=…>``, ``<property name=…><value>…</value></property>`` and the
``p:`` attributes (namespace ``http://www.springframework.org/schema/p``). ``${key}`` / ``${key:default}`` are
resolved from every ``*.properties`` file of the list (first definition in path order wins), at most
``MAX_ROUNDS`` rounds; a value that still holds ``${`` is not used: an unresolved URL gives ``reason
"unresolved"`` (a code this module adds to the three of the model), an unresolved user name or driver is left empty.

The password: ``has_password`` is true when a password property has a non-empty raw value, or when the URL carries
one. Credentials are taken out of the URL for every vendor and wrapper (``log4jdbc``, ``p6spy``, any case): the
``user/pw@`` form after the last ``word:`` of the prefix (``thin:``, ``oci:``), ``//user[:pw]@host`` user info (the
password runs to the LAST ``@``, so it may hold ``@`` / ``/`` / ``;`` / ``&``), and the parameters ``password``,
``passwd``, ``pwd``, ``pass`` (case ignored, after ``?`` / ``&`` / ``;``, a value in braces or quotes taken whole). A
user name found there becomes ``username`` when none is configured. Before the strip, a URL with whitespace or a control character, or with a
parameter NAME that percent-decoding (at most ``NORMALISE_ROUNDS``) / NFKC / case folding changes (``pass%77ord=``),
is withheld (``disguised``). After it, the final wall (``may_carry_credentials``) judges the normalised copy: any of
``WALL_WORDS`` (password, token, api key, private key, ssl key, keystore pass … never a bare ``key``: MySQL's
``allowPublicKeyRetrieval`` passes), a user value that is not a plain name, or an ``@`` other than the FIRST one
right after an Oracle / Tibero scheme part (``jdbc:oracle:thin:@``, ``…:oci8:@``, wrapper prefixes removed first)
withholds the URL: ``reason "unresolved"``, ``DatasourceScan.url_withheld``, the URL text in no note. A TNS
descriptor, ``thin:@//host``, the LDAP form and an IPv6 host pass. The password's value is never resolved, returned, logged or put into
a note. ``DatasourceScan.password_files`` names where it was (the XML file and, for a placeholder, the properties
files that define the keys, followed through nested placeholders).

Untrusted input: XML without entity resolution, DTD loading or network; only regular files inside the source root,
opened without following links and without blocking; at most ``MAX_XML_BYTES`` / ``MAX_PROPERTIES_BYTES`` per file
(a larger one is a note). Nothing raises for a bad file."""
from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

from sbmig.fwconvert.model import DatasourceInfo
from sbmig.fwconvert.rules import ConvertRules

MAX_XML_BYTES = 4 * 1024 * 1024
MAX_PROPERTIES_BYTES = 1024 * 1024
MAX_ROUNDS = 5
# 치환한 값의 최대 길이. 서로를 여러 번 부르는 값이 끝없이 커지지 않게 한다
MAX_VALUE_CHARS = 8192

P_NAMESPACE = "http://www.springframework.org/schema/p"
URL_KEYS = ("url", "jdbcUrl")
DRIVER_KEYS = ("driverClassName", "driverClass", "driver")
USER_KEYS = ("username", "user")
PASSWORD_KEYS = ("password",)

# JDBC URL의 앞부분과 벤더. 위에서부터 처음 맞는 것을 쓴다
URL_VENDORS = (
    ("jdbc:oracle:", "oracle"),
    ("jdbc:mysql:", "mysql"),
    ("jdbc:mariadb:", "mariadb"),
    ("jdbc:postgresql:", "postgresql"),
    ("jdbc:sqlserver:", "sqlserver"),
    ("jdbc:jtds:sqlserver:", "sqlserver"),
    ("jdbc:tibero:", "tibero"),
    ("jdbc:h2:", "h2"),
)
# 드라이버 클래스의 앞부분과 벤더
DRIVER_VENDORS = (
    ("oracle.jdbc.", "oracle"),
    ("com.mysql.", "mysql"),
    ("org.mariadb.", "mariadb"),
    ("org.postgresql.", "postgresql"),
    ("com.microsoft.sqlserver.", "sqlserver"),
    ("net.sourceforge.jtds.", "sqlserver"),
    ("com.tmax.tibero.", "tibero"),
    ("org.h2.", "h2"),
)
# 실제 드라이버를 감싸는 로깅용 URL 앞부분. 벤더는 그 뒤에서 찾는다
URL_WRAPPERS = ("jdbc:log4jdbc:", "jdbc:p6spy:")

_PLACEHOLDER = re.compile(r"\$\{([^${}]*)\}")
# JDBC URL의 앞부분. 감싸는 접두사(log4jdbc 등)와 thin, oci 같은 표시까지 포함한다
_JDBC_PREFIX = re.compile(r"jdbc:(?:[A-Za-z0-9_]+:)+", re.IGNORECASE)
# thin, oci 꼴의 사용자/비밀번호@. 비밀번호는 마지막 @까지다
_SLASH_CREDENTIALS = re.compile(r"^(?P<pre>jdbc:(?:[A-Za-z0-9_]+:)+)(?P<user>[^/:@\s()]+)/(?P<pw>.*)@(?P<rest>[^@]*)$",
                                re.IGNORECASE | re.DOTALL)
# //사용자:비밀번호@호스트 꼴의 앞부분. 사용자 정보는 // 뒤 첫 / ? ; 앞의 마지막 @까지다
_AUTHORITY_PREFIX = re.compile(r"^jdbc:(?:[A-Za-z0-9_]+:)+//", re.IGNORECASE)
# 비밀번호 매개변수. 중괄호나 따옴표로 묶은 값은 통째로 뺀다
_PASSWORD_PARAM = re.compile(r"(?P<sep>[?&;])\s*(?:password|passwd|pwd|pass)\s*=\s*"
                             r"(?:\{[^}]*\}?|\"[^\"]*\"?|'[^']*'?|[^&;]*)", re.IGNORECASE)
# 빼고 난 뒤에도 이 말이 남아 있으면 URL을 쓰지 않는다. 대소문자는 가리지 않는다
WALL_WORDS = ("password", "passwd", "pwd", "pass=", "secret", "credential", "token", "apikey", "api_key",
              "accesskey", "access_key", "privatekey", "private_key", "sslkey", "keypass", "storepass", "auth=")
# 첫 @ 앞이 정확히 이 꼴일 때만 @를 허용한다(Oracle, Tibero의 thin:@, oci:@). 감싸는 접두사는 먼저 뗀다
_ORACLE_AT_SCHEME = re.compile(r"^jdbc:(?:oracle|tibero):[a-z0-9]+:$")
_WRAPPER_IN_SCHEME = re.compile(r"^jdbc:(?:log4jdbc|p6spy):")
# 매개변수 이름. 구분자 뒤에서 = 앞까지다
_PARAM_NAME = re.compile(r"[?&;(,]([^=?&;(),]*)=")
NORMALISE_ROUNDS = 3
_USER_KEY = re.compile(r"(?<![A-Za-z0-9_])(?:user|username|uid)\s*=\s*(?P<val>[^&;,)(?]*)", re.IGNORECASE)
# 그대로 써도 되는 사용자 이름과 드라이버 클래스 이름
_PLAIN_NAME = re.compile(r"^[\w\-.$#]{1,128}$")
_CLASS_NAME = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")


@dataclass
class DatasourceScan:
    """DB 연결 정보를 읽은 결과와 사람에게 알릴 말을 담는 클래스. 비밀번호 값은 담지 않는다."""
    info: DatasourceInfo = field(default_factory=DatasourceInfo)
    notes: list[str] = field(default_factory=list)             # 함께 찾은 다른 데이터소스, 읽지 못한 파일
    password_files: list[str] = field(default_factory=list)    # 비밀번호가 적혀 있던 파일. 원본 루트 기준
    url_withheld: bool = False      # 인증 정보가 남아 있을 수 있어서 URL을 쓰지 않았으면 True


@dataclass
class _Candidate:
    kind: str                       # bean 또는 jndi
    source: str
    label: str
    props: dict[str, str] = field(default_factory=dict)


# ---- 파일 읽기 -------------------------------------------------------------------


def _clean_rel(rel: str) -> str | None:
    s = str(rel).replace("\\", "/")
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        return None
    parts = [p for p in s.split("/") if p and p != "."]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _read_limited(root: Path, rel: str, limit: int) -> tuple[bytes | None, str]:
    clean = _clean_rel(rel)
    if clean is None:
        return None, "not a relative path inside the source folder"
    path = root.joinpath(*clean.split("/"))
    try:
        # 링크 폴더를 거쳐 원본 폴더 밖을 가리키는 파일은 읽지 않는다
        if not path.resolve().is_relative_to(root.resolve()):
            return None, "resolves to a file outside the source folder"
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None, "not a regular file"
        # 파이프를 열 때 기다리지 않도록 non-blocking으로 열고, 연 뒤에 일반 파일인지 다시 확인한다
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
    except (OSError, RuntimeError) as e:
        return None, f"cannot read: {getattr(e, 'strerror', None) or e}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "not a regular file"
        with os.fdopen(fd, "rb", closefd=True) as fh:
            fd = -1
            data = fh.read(limit + 1)
    except OSError as e:
        return None, f"cannot read: {e.strerror or e}"
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > limit:
        return None, f"larger than {limit} bytes, not read"
    return data, ""


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


# ---- properties 파일 --------------------------------------------------------------


def _unescape(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "u" and i + 6 <= n and re.fullmatch(r"[0-9A-Fa-f]{4}", text[i + 2:i + 6]):
            out.append(chr(int(text[i + 2:i + 6], 16)))
            i += 6
            continue
        out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(nxt, nxt))
        i += 2
    return "".join(out)


def _ends_escaped(line: str) -> bool:
    count = len(line) - len(line.rstrip("\\"))
    return count % 2 == 1


def parse_properties(text: str) -> dict[str, str]:
    """Java properties 형식의 글을 읽어 키와 값을 반환한다. 같은 키가 또 나오면 뒤의 값을 쓴다.
    ``\\uXXXX`` 같은 이스케이프와 역슬래시로 이은 줄을 Java와 같이 처리한다."""
    out: dict[str, str] = {}
    lines = re.split(r"\r\n|\r|\n", text)
    i = 0
    while i < len(lines):
        line = lines[i].lstrip(" \t\f")
        i += 1
        if not line or line[0] in "#!":
            continue
        # 홀수 개의 역슬래시로 끝나는 줄은 다음 줄과 잇는다
        while _ends_escaped(line) and i < len(lines):
            line = line[:-1] + lines[i].lstrip(" \t\f")
            i += 1
        if _ends_escaped(line):
            line = line[:-1]
        j, n = 0, len(line)
        while j < n:
            if line[j] == "\\":
                j += 2
                continue
            if line[j] in "=: \t\f":
                break
            j += 1
        key = line[:j]
        rest = line[j:].lstrip(" \t\f")
        if rest[:1] in ("=", ":"):
            rest = rest[1:].lstrip(" \t\f")
        out[_unescape(key)] = _unescape(rest)
    return out


def _load_properties(root: Path, files: list[str], notes: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    origin: dict[str, str] = {}
    for rel in sorted(f for f in files if f.lower().endswith(".properties")):
        data, why = _read_limited(root, rel, MAX_PROPERTIES_BYTES)
        if data is None:
            notes.append(f"{rel}: {why}")
            continue
        for key, value in parse_properties(_decode(data)).items():
            # 여러 파일에 같은 키가 있으면 경로 순서로 먼저 온 파일의 값을 쓴다
            if key not in values:
                values[key] = value
                origin[key] = rel
    return values, origin


def _resolve(value: str, props: dict[str, str]) -> str:
    cur = value
    for _ in range(MAX_ROUNDS):
        if "${" not in cur:
            break

        def repl(m: re.Match[str]) -> str:
            key, sep, default = m.group(1).partition(":")
            if key in props:
                return props[key]
            return default if sep else m.group(0)

        nxt = _PLACEHOLDER.sub(repl, cur)
        if nxt == cur or len(nxt) > MAX_VALUE_CHARS:
            break
        cur = nxt
    return cur


# ---- Spring XML -------------------------------------------------------------------


def _local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _bean_props(el: etree._Element, wanted: set[str]) -> dict[str, str]:
    props: dict[str, str] = {}
    for name, value in el.attrib.items():
        q = etree.QName(name)
        if q.namespace == P_NAMESPACE and q.localname in wanted and q.localname not in props:
            props[q.localname] = value
    for child in el:
        if _local(child.tag) != "property":
            continue
        name = child.get("name") or ""
        if name not in wanted or name in props:
            continue
        if child.get("value") is not None:
            props[name] = child.get("value") or ""
            continue
        for sub in child:
            if _local(sub.tag) == "value":
                props[name] = (sub.text or "").strip()
                break
    return props


def _candidates(root: Path, rel: str, notes: list[str]) -> list[_Candidate]:
    data, why = _read_limited(root, rel, MAX_XML_BYTES)
    if data is None:
        notes.append(f"{rel}: {why}")
        return []
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, dtd_validation=False,
                             huge_tree=False, remove_comments=True, remove_pis=True)
    doc = None
    try:
        doc = etree.fromstring(data, parser)
    except (etree.LxmlError, ValueError, LookupError):
        # 선언한 인코딩을 읽지 못한 경우일 수 있다. 직접 디코딩하고 선언을 뺀 뒤 한 번 더 읽는다
        text = re.sub(r"^\s*<\?xml[^>]{0,500}\?>", "", _decode(data), count=1)
        try:
            doc = etree.fromstring(text.encode("utf-8"), parser)
        except (etree.LxmlError, ValueError, LookupError) as e:
            notes.append(f"{rel}: cannot parse the XML ({e.__class__.__name__})")
            return []
    if doc is None:
        return []
    wanted = set(URL_KEYS + DRIVER_KEYS + USER_KEYS + PASSWORD_KEYS)
    found: list[_Candidate] = []
    for el in doc.iter():
        local = _local(el.tag)
        label = f"{rel}#{el.get('id') or el.get('name') or el.get('jndi-name') or local}"
        if local == "jndi-lookup":
            found.append(_Candidate("jndi", rel, label))
            continue
        if local != "bean":
            continue
        simple = (el.get("class") or "").strip().rsplit(".", 1)[-1].lower()
        if simple.endswith("jndiobjectfactorybean"):
            found.append(_Candidate("jndi", rel, label))
        elif simple.endswith("datasource"):
            props = _bean_props(el, wanted)
            if any(props.get(k, "").strip() for k in URL_KEYS):
                found.append(_Candidate("bean", rel, label, props))
    return found


# ---- 벤더와 비밀번호 -----------------------------------------------------------------


def vendor_of(url: str, driver: str) -> str:
    """JDBC URL이나 드라이버 클래스로 DB 벤더를 정한다. URL을 먼저 보고, 모르면 빈 문자열을 반환한다."""
    low = (url or "").strip().lower()
    for wrapper in URL_WRAPPERS:
        if low.startswith(wrapper):
            low = "jdbc:" + low[len(wrapper):]
    for prefix, vendor in URL_VENDORS:
        if low.startswith(prefix):
            return vendor
    drv = (driver or "").strip()
    for prefix, vendor in DRIVER_VENDORS:
        if drv.startswith(prefix):
            return vendor
    return ""


def _strip_url_password(url: str) -> tuple[str, str, bool]:
    """URL에서 인증 정보를 뺀 URL, URL에 있던 사용자 이름, 비밀번호가 있었는지를 반환한다.
    벤더와 감싸는 접두사에 상관없이 ``사용자/비밀번호@``, ``//사용자:비밀번호@``, 비밀번호 매개변수를 뺀다.
    여기서 다 빼지 못한 꼴은 ``may_carry_credentials``가 막는다."""
    user, had = "", False
    m = _SLASH_CREDENTIALS.match(url)
    if m:
        user, had = m.group("user"), bool(m.group("pw"))
        url = f"{m.group('pre')}@{m.group('rest')}"
    else:
        m = _AUTHORITY_PREFIX.match(url)
        if m:
            rest = url[m.end():]
            cut = re.search(r"[/?;]", rest)
            authority = rest[:cut.start()] if cut else rest
            at = authority.rfind("@")
            if at >= 0:
                name, colon, pw = authority[:at].partition(":")
                user, had = name, bool(colon and pw)
                url = url[:m.end()] + rest[at + 1:]
    if _PASSWORD_PARAM.search(url):
        had = True
        # 첫 매개변수였으면 ? 를 남기고, 아니면 앞의 구분자째 뺀다
        url = _PASSWORD_PARAM.sub(lambda mm: "?" if mm.group("sep") == "?" else "", url)
        url = re.sub(r"\?[&;]+", "?", url).rstrip("?&")
    return url, user, had


def plain_name(text: str) -> bool:
    """사용자 이름으로 그대로 써도 되는 값이면 True를 반환한다.
    글자, 숫자, ``_ - . $ #``만 있고 128자 이하인 값이다. ``/``, ``:``, ``@``, 공백이 있으면 비밀번호가 섞였을 수 있다."""
    return bool(_PLAIN_NAME.match(text or ""))


def normalised(url: str) -> str:
    """판단용으로 URL을 고친 사본을 반환한다. 퍼센트 인코딩을 바뀌지 않을 때까지 최대 세 번 풀고 NFKC로 정규화한 뒤 casefold한다."""
    cur = url
    for _ in range(NORMALISE_ROUNDS):
        nxt = unquote(cur)
        if nxt == cur:
            break
        cur = nxt
    return unicodedata.normalize("NFKC", cur).casefold()


def disguised(url: str) -> bool:
    """URL에 공백이나 제어 문자가 있거나, 인코딩으로 감춘 매개변수 이름이 있으면 True를 반환한다.
    정상적인 URL은 매개변수 이름을 인코딩할 까닭이 없다."""
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in url):
        return True
    return any(normalised(name) != name.casefold() for name in _PARAM_NAME.findall(url))


def may_carry_credentials(url: str) -> bool:
    """인증 정보를 뺀 URL에 아직 인증 정보로 보이는 것이 남아 있으면 True를 반환한다.
    정규화한 사본으로 판단한다. 비밀번호, 토큰, 키를 뜻하는 말이 있거나 허용하지 않는 ``@``가 있으면 그렇다.
    ``@``는 첫 번째 것이 Oracle 꼴 ``thin:@``, ``oci:@`` 바로 뒤에 올 때만 허용한다."""
    if disguised(url):
        return True
    low = normalised(url)
    if any(word in low for word in WALL_WORDS):
        return True
    at = low.find("@")
    if at >= 0:
        scheme = _WRAPPER_IN_SCHEME.sub("jdbc:", low[:at])
        if not _ORACLE_AT_SCHEME.match(scheme) or "@" in low[at + 1:]:
            return True
    return any(not plain_name(m.group("val").strip()) for m in _USER_KEY.finditer(low))


def _placeholder_files(raw: str, props: dict[str, str], origin: dict[str, str]) -> list[str]:
    files: list[str] = []
    todo, seen = [raw], set()
    for _ in range(MAX_ROUNDS):
        nxt: list[str] = []
        for text in todo:
            for m in _PLACEHOLDER.finditer(text):
                key = m.group(1).partition(":")[0]
                if key in seen or key not in props:
                    continue
                seen.add(key)
                if origin.get(key) and origin[key] not in files:
                    files.append(origin[key])
                nxt.append(props[key])
        todo = nxt
    return files


def _first(props: dict[str, str], keys: tuple[str, ...]) -> tuple[str, str]:
    for k in keys:
        if props.get(k, "").strip():
            return k, props[k].strip()
    return "", ""


# ---- 진입점 ---------------------------------------------------------------------------


def scan_datasource(src_root: Path, db_config_files: list[str], rules: ConvertRules) -> DatasourceScan:
    """원본의 설정 파일에서 데이터소스를 찾아 연결 정보와 알릴 말을 함께 반환한다.
    파일 순서대로 보고 처음 찾은 데이터소스를 쓴다. 나머지는 ``notes``에 이름만 남긴다.
    비밀번호 값은 반환하지 않는다. 읽지 못한 파일도 예외 없이 ``notes``에 남긴다."""
    root = Path(src_root)
    scan = DatasourceScan()
    files = sorted({str(f) for f in db_config_files})
    props, origin = _load_properties(root, files, scan.notes)
    found: list[_Candidate] = []
    for rel in files:
        if rel.lower().endswith(".xml"):
            found += _candidates(root, rel, scan.notes)
    if not found:
        scan.info = DatasourceInfo(reason="not_found")
        return scan
    first, others = found[0], found[1:]
    for o in others:
        scan.notes.append(f"another datasource {o.label} ({o.kind}) was found and not used; "
                          f"{first.label} is the first in file order")
    if first.kind == "jndi":
        scan.info = DatasourceInfo(reason="jndi", source=first.source)
        return scan
    info = scan.info = DatasourceInfo(source=first.source)
    _, raw_url = _first(first.props, URL_KEYS)
    _, raw_driver = _first(first.props, DRIVER_KEYS)
    _, raw_user = _first(first.props, USER_KEYS)
    raw_password = _first(first.props, PASSWORD_KEYS)[1]
    if raw_password:
        info.has_password = True
        scan.password_files.append(first.source)
        # 비밀번호가 properties 값이면 그 파일도 알린다. 값은 읽지 않는다
        for m in _PLACEHOLDER.finditer(raw_password):
            key = m.group(1).partition(":")[0]
            if key in origin and origin[key] not in scan.password_files:
                scan.password_files.append(origin[key])
    raw_password = ""
    url = _resolve(raw_url, props)
    # 인코딩으로 감춘 이름과 공백은 빼기 전의 URL에서 본다. 빼고 나면 흔적이 사라질 수 있다
    hidden = disguised(url)
    url, url_user, url_had_password = _strip_url_password(url)
    if url_had_password:
        info.has_password = True
        # URL이 properties 값에서 왔으면 그 파일에도 비밀번호가 있다
        for f in [first.source, *_placeholder_files(raw_url, props, origin)]:
            if f not in scan.password_files:
                scan.password_files.append(f)
    driver = _resolve(raw_driver, props)
    user = _resolve(raw_user, props)
    info.driver_class = driver if _CLASS_NAME.match(driver) else ""
    user = "" if "${" in user else user
    # 사용자 이름은 평범한 이름일 때만 쓴다. 아니면 비밀번호가 섞였을 수 있어서 쓰지 않고 알린다
    for value, raw in ((user, raw_user), (url_user, raw_url)):
        if value and not plain_name(value):
            info.has_password = True
            for f in [first.source, *_placeholder_files(raw, props, origin)]:
                if f not in scan.password_files:
                    scan.password_files.append(f)
    info.username = next((v for v in (user, url_user) if v and plain_name(v)), "")
    if "${" in url:
        info.reason = "unresolved"
        scan.notes.append(f"{first.label}: the URL keeps a placeholder no properties file of the project defines")
        return scan
    if hidden or may_carry_credentials(url):
        # 인증 정보가 남아 있을 수 있는 URL은 쓰지 않는다
        info.reason = "unresolved"
        scan.url_withheld = True
        scan.notes.append(f"{first.label}: the URL was left out because it may still carry credentials")
        return scan
    info.url = url
    info.vendor = vendor_of(url, info.driver_class)
    if not info.vendor:
        info.reason = "unknown_vendor"
        return scan
    info.configured = True
    return scan


def read_datasource(src_root: Path, db_config_files: list[str], rules: ConvertRules) -> DatasourceInfo:
    """원본의 설정 파일에서 DB 연결 정보를 읽어 반환한다. 비밀번호 값은 담지 않는다.
    찾지 못했거나 JNDI이거나 벤더를 모르면 ``configured``가 False이고 ``reason``에 이유가 있다."""
    return scan_datasource(src_root, db_config_files, rules).info
