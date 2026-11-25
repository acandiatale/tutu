"""OpenRewrite 호출에 넘길 Maven settings 복사본을 만드는 모듈.
- 복사본은 원본에 그림자 미러만 더한다. Maven은 그림자 미러를 어느 저장소에도 쓰지 않으므로 Maven의 동작은 그대로다
- OpenRewrite는 ``mirrorOf``의 첫 ``:`` 앞을 버리고 읽으므로 ``sbmig:<저장소 id>``를 그 저장소의 미러로 쓴다
- 원본 바이트는 그대로 두고 요소를 끼워 넣기만 한다. 만들 수 없으면 원본을 쓰고 이유를 남긴다

The settings copy of the OpenRewrite call (`docs/INTERFACES.md` "v2.1z-ci", rules 1 to 3). The same `-s` is read
by Maven inside that call (the forked lifecycle of `rewrite:run`), so the copy never moves or edits anything: it
inserts, at the front of the user file's `<mirrors>`, one shadow mirror per known repository id whose OpenRewrite
choice differs from Maven's, `<mirrorOf>sbmig:<id></mirrorOf>` with the url of the mirror Maven selects (the
repository's own url when Maven selects none), plus a copy of that mirror's (or repository's) `<server>`.
Maven (`DefaultMirrorSelector`) compares each `mirrorOf` token with the repository id as a whole, so no repository
matches `sbmig:<id>`; OpenRewrite (`MavenRepositoryMirror`, rewrite-maven 8.89.0) drops everything up to the first
`:`, takes the rest as the id list for non-local repositories, and uses the first matching mirror in file order.
`translate_settings` is pure (bytes in, bytes out); `openrewrite_settings` does the file work (0600 copy in a
private temp folder, removed on exit); `project_repositories` reads the project's poms.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

CENTRAL = ("central", "https://repo.maven.apache.org/maven2")    # the super POM's repository
SHADOW_PREFIX = "sbmig:"
SHADOW_ID = "sbmig-shadow-"
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_DECL_ENC = re.compile(rb"""^<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_TOKEN = re.compile(
    r"<!--.*?-->"
    r"|<!\[CDATA\[.*?\]\]>"
    r"|<\?.*?\?>"
    r"|<!(?P<decl>[A-Za-z]+)"
    r"|</(?P<end>[^\s>]+)\s*>"
    r"|<(?P<start>[^\s/>!?]+)(?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*\s*(?P<empty>/?)>"
    r"|<", re.S)
_HTTP_PROTOCOLS = ("http", "dav", "dav:http", "dav+http")
# url을 모르는 저장소는 이 네 주소를 모두 가질 수 있는 것으로 보고 판단한다
_UNKNOWN_URLS = ("http://unknown.invalid/", "https://unknown.invalid/", "http://localhost/", "file:///unknown")
_POM_SKIP = frozenset({"target", "node_modules", "out", "build"})


class Refused(Exception):
    """settings 파일을 읽을 수 없거나 그림자 미러를 끼워 넣을 수 없을 때 던지는 예외. 메시지가 저널의 이유가 된다."""


@dataclass
class SettingsTranslation:
    """그림자 미러를 더한 결과를 담는 클래스. ``data``가 None이면 원본을 그대로 쓴다.

    `needed` False = nothing to record (no shadow, nothing skipped). `translated` True = `data` holds the copy.
    `record()` is the journal payload of `rewrite_settings` (ids only)."""
    data: Optional[bytes] = None
    needed: bool = False
    translated: bool = False
    shadows: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    reason: Optional[str] = None

    def record(self) -> dict[str, Any]:
        """저널 ``rewrite_settings``에 쓸 값을 반환한다. 저장소와 미러 id만 담는다."""
        return {"translated": self.translated, "shadows": [dict(s) for s in self.shadows],
                "skipped": [dict(s) for s in self.skipped], "reason": self.reason}


@dataclass(eq=False)
class Mirror:
    """settings의 ``<mirror>`` 하나를 담는 클래스. ``url_text``는 파일에 있는 url 텍스트 그대로다."""
    id: str
    url: str
    mirror_of: str
    blocked: bool = False
    url_text: str = ""


# ------------------------------------------------------------------ 미러 선택 규칙
def _jsplit(s: str) -> list[str]:
    # Java의 String.split(",")처럼 끝의 빈 조각을 버린다
    if "," not in s:
        return [s]
    parts = s.split(",")
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def _scheme_host(url: str) -> tuple[str, str]:
    u = (url or "").strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*(?::[A-Za-z][A-Za-z0-9+.-]*)?):(?://([^/?#]*))?", u)
    if not m:
        return "", ""
    netloc = (m.group(2) or "").rsplit("@", 1)[-1]
    host = netloc[1:netloc.index("]")] if netloc.startswith("[") and "]" in netloc else netloc.split(":", 1)[0]
    return m.group(1), host


def _local_host(host: str) -> bool:
    return host in ("localhost", "127.0.0.1")


def maven_matches(mirror_of: str, repo_id: str, url: str) -> bool:
    """Maven의 ``mirrorOf`` 규칙으로 저장소가 이 미러에 들어가는지 확인한다.

    Maven Resolver `DefaultMirrorSelector.matchPattern`: `*` or the whole value equal to the id; else the tokens in
    order: `!id` that names the repository ends with false, an equal id ends with true, `external:*` (not localhost /
    127.0.0.1 / file) / `external:http:*` (http or dav, not local) / `*` set true and go on. Tokens are not trimmed."""
    if mirror_of == "*" or mirror_of == repo_id:
        return True
    scheme, host = _scheme_host(url)
    external = not _local_host(host) and scheme.lower() != "file"
    external_http = scheme.lower() in _HTTP_PROTOCOLS and not _local_host(host)
    result = False
    for t in _jsplit(mirror_of):
        if len(t) > 1 and t.startswith("!"):
            if t[1:] == repo_id:
                return False
        elif t == repo_id:
            return True
        elif (t == "external:*" and external) or (t == "external:http:*" and external_http) or t == "*":
            result = True
    return result


def openrewrite_matches(mirror_of: str, repo_id: str, url: str) -> bool:
    """OpenRewrite의 ``mirrorOf`` 규칙으로 저장소가 이 미러에 들어가는지 확인한다.

    rewrite-maven 8.89.0 `MavenRepositoryMirror.matches`: never the id `local`; `*` whole; the value cut after its
    first `:` (a colon anywhere means external only: file / localhost / 127.0.0.1 excluded); `!*` = named ids only,
    `*` = all but the excluded, else included and not excluded. `<blocked>` plays no part."""
    if repo_id == "local":
        return False
    if mirror_of == "*":
        return True
    colon = mirror_of.find(":")
    rest = mirror_of[colon + 1:] if colon >= 0 else mirror_of
    toks = _jsplit(rest)
    excluded = {t[1:] for t in toks if t.startswith("!")}
    included = {t for t in toks if not t.startswith("!")}
    if colon >= 0:
        scheme, host = _scheme_host(url)
        if (url or "").lower().startswith("file:") or scheme.lower() == "file" or _local_host(host):
            return False
    if "*" in excluded:
        return repo_id in included
    if "*" in included:
        return repo_id not in excluded
    return repo_id not in excluded and repo_id in included


def maven_choice(mirrors: Sequence[Mirror], repo_id: str, url: str) -> Optional[Mirror]:
    """Maven이 이 저장소에 쓰는 미러를 반환한다. 미러가 없으면 None을 반환한다.

    Pass 1: the first mirror whose whole `mirrorOf` equals the id; pass 2: the first pattern match in list order."""
    for m in mirrors:
        if m.mirror_of == repo_id:
            return m
    for m in mirrors:
        if maven_matches(m.mirror_of, repo_id, url):
            return m
    return None


def openrewrite_choice(mirrors: Sequence[Mirror], repo_id: str, url: str) -> Optional[Mirror]:
    """OpenRewrite가 이 저장소에 쓰는 미러를 반환한다. 미러가 없으면 None을 반환한다."""
    return next((m for m in mirrors if openrewrite_matches(m.mirror_of, repo_id, url)), None)



# ------------------------------------------------------------------ XML 텍스트
@dataclass
class _Node:
    name: str
    start: int
    end: int
    cstart: int
    cend: int
    children: list["_Node"] = field(default_factory=list)

    def kid(self, name: str) -> Optional["_Node"]:
        return next((c for c in self.children if c.name == name), None)

    def kids(self, name: str) -> list["_Node"]:
        return [c for c in self.children if c.name == name]


class _Doc:
    """One settings file: decoded text, its node tree (spans) and the ElementTree view (values)."""

    def __init__(self, data: bytes, label: str) -> None:
        self.label = label
        self.bom, enc = b"", "utf-8"
        for bom, name in _BOMS:
            if data.startswith(bom):
                self.bom, enc = bom, name
                break
        else:
            m = _DECL_ENC.match(data)
            if m:
                enc = m.group(1).decode("ascii")
        self.encoding = enc
        try:
            self.text = data[len(self.bom):].decode(enc)
        except (LookupError, UnicodeDecodeError) as e:
            raise Refused(f"the {label} settings file cannot be decoded as {enc} ({type(e).__name__})") from None
        if self.bom + self.text.encode(enc) != data:
            raise Refused(f"the {label} settings file does not round-trip through {enc}")
        self.root = self._tree()
        if ":" in self.root.name:
            raise Refused(f"the {label} settings file uses a namespace prefix on its elements")
        parse_text = re.sub(r"^\s*<\?xml.*?\?>", lambda m: " " * len(m.group(0)), self.text, count=1, flags=re.S)
        try:
            self.et = ET.fromstring(parse_text)
        except ET.ParseError as e:
            raise Refused(f"the {label} settings file is not well-formed XML ({e})") from None
        for c in self.et.iter():
            if isinstance(c.tag, str) and c.tag.startswith("{"):
                c.tag = c.tag.split("}", 1)[1]

    def _tree(self) -> _Node:
        root: Optional[_Node] = None
        stack: list[_Node] = []
        for m in _TOKEN.finditer(self.text):
            if m.group("decl"):
                raise Refused(f"the {self.label} settings file has a <!{m.group('decl')}> declaration")
            if m.group("start"):
                n = _Node(m.group("start"), m.start(), m.end(), m.end(), m.end())
                if stack:
                    stack[-1].children.append(n)
                elif root is None:
                    root = n
                else:
                    raise Refused(f"the {self.label} settings file has two root elements")
                if not m.group("empty"):
                    stack.append(n)
            elif m.group("end"):
                if not stack or stack[-1].name != m.group("end"):
                    raise Refused(f"the {self.label} settings file is not well-formed XML (tags do not nest)")
                n = stack.pop()
                n.cend, n.end = m.start(), m.end()
            elif m.group(0) == "<":
                raise Refused(f"the {self.label} settings file is not well-formed XML (a stray '<')")
        if root is None or stack:
            raise Refused(f"the {self.label} settings file is not well-formed XML (no complete root element)")
        return root

    def encode(self, text: str) -> bytes:
        return self.bom + text.encode(self.encoding, errors="xmlcharrefreplace")

    # 값은 ElementTree에서, 위치는 직접 만든 트리에서 읽고 둘이 같은 구조인지 확인한다
    def section(self, name: str, item: str) -> list[tuple[_Node, Any]]:
        nodes = self.root.kids(name)
        els = [c for c in self.et if c.tag == name]
        if len(nodes) > 1 or len(els) > 1:
            raise Refused(f"the {self.label} settings file has more than one <{name}> element")
        if not nodes:
            return []
        items, item_els = nodes[0].kids(item), [c for c in els[0] if c.tag == item]
        if len(items) != len(item_els) or any(":" in c.name for c in nodes[0].children):
            raise Refused(f"the {self.label} settings file's <{name}> could not be read element by element")
        return list(zip(items, item_els))



def _val(el: Any, tag: str) -> Optional[str]:
    c = el.find(tag)
    return None if c is None else (c.text or "").strip()


def _mirrors(doc: _Doc) -> list[Mirror]:
    out: list[Mirror] = []
    for node, el in doc.section("mirrors", "mirror"):
        mid, url, of = _val(el, "id"), _val(el, "url"), _val(el, "mirrorOf")
        if not mid or not of or not url or len(node.kids("url")) != 1:
            raise Refused(f"a mirror of the {doc.label} settings file has no single <id>, <mirrorOf> and <url>")
        if el.find("mirrorOfLayouts") is not None:
            raise Refused(f"mirror {mid} has <mirrorOfLayouts>, which the translation does not read")
        u = node.kid("url")
        out.append(Mirror(mid, url, of, (_val(el, "blocked") or "").lower() == "true",
                          doc.text[u.cstart:u.cend].strip()))
    return out


def _servers(doc: Optional[_Doc]) -> dict[str, str]:
    """server id -> the element's source text (the first one per id)."""
    out: dict[str, str] = {}
    for node, el in (doc.section("servers", "server") if doc is not None else []):
        sid, kid = _val(el, "id"), node.kid("id")
        if sid and sid not in out and kid is not None:
            out[sid] = doc.text[node.start:kid.cstart] + "\0" + doc.text[kid.cend:node.end]
    return out


def _profile_repos(doc: Optional[_Doc]) -> list[tuple[str, str]]:
    """Repositories of the profiles the file activates itself (`<activeProfiles>`, `activeByDefault`)."""
    if doc is None:
        return []
    active = {(a.text or "").strip() for a in doc.et.findall("./activeProfiles/activeProfile")}
    out: list[tuple[str, str]] = []
    for prof in doc.et.findall("./profiles/profile"):
        by_default = (prof.findtext("./activation/activeByDefault") or "").strip().lower() == "true"
        if _val(prof, "id") in active or by_default:
            out += [(_val(r, "id") or "", _val(r, "url") or "") for r in prof.findall("./repositories/repository")]
    return out


def project_repositories(root: Optional[Path], limit: int = 400,
                         not_fillable: Iterable[str] = ()) -> list[tuple[str, str]]:
    """프로젝트 pom들이 선언한 저장소의 (id, url)을 모두 반환한다. pom의 ``<profiles>`` 안도 읽는다.
    url의 ``${…}``는 그 pom과 프로젝트 안 부모 pom들의 ``<properties>``로 채우고, 채우지 못한 것은 그대로 둔다.
    ``not_fillable``의 이름과 Maven이 다른 곳에서 채우는 이름은 채우지 않는다. 읽지 못한 pom은 건너뛴다.

    Every (id, url) pair once (two ids at one url are two entries); `target/`, hidden folders and build output folders
    are not walked; at most `limit` poms. Properties: the pom's own first, then its parents' (`<relativePath>`,
    default `../pom.xml`, followed only while the file is inside the project and its groupId / artifactId / version
    match the `<parent>` element; at most `_CHAIN` poms, a cycle ends the walk). Like `repo_versions.pom_repositories`,
    `${project.…}` and other model values are not filled. A name is filled only when nothing outranks the pom in
    Maven: not in `not_fillable` (the caller passes the `<properties>` names of every settings profile and the `-D`
    names of the command, `MAVEN_OPTS` and `MAVEN_ARGS`), not defined in `<properties>` by any `<profile>` of a pom of
    the chain (active or not: activation by jdk / property / file / OS is not judged) and not a name Maven resolves elsewhere (`_ELSEWHERE`: `env.*`,
    `project.*`, `settings.*`, Java system properties ...). Nested values are filled up to `_PASSES` times."""
    out: list[tuple[str, str]] = []
    if root is None or not Path(root).is_dir():
        return out
    base = Path(root).resolve()
    poms: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _POM_SKIP and not d.startswith("."))
        if "pom.xml" in filenames:
            poms.append(Path(dirpath) / "pom.xml")
        if len(poms) >= limit:
            break
    cache: dict[Path, Any] = {}
    blocked = set(not_fillable)
    for pom in poms:
        el = _pom(pom, cache)
        if el is None:
            continue
        props, profile_names = _chain_properties(pom, base, cache)
        for r in el.findall("./repositories/repository") + el.findall("./profiles/profile/repositories/repository"):
            rid, url = _val(r, "id") or "", _fill(_val(r, "url") or "", props, blocked | profile_names)
            if rid and url and (rid, url) not in out:
                out.append((rid, url))
    return out


_CHAIN = 10
_PASSES = 5
# Maven이 pom 속성보다 먼저 보거나 다른 곳에서 채우는 이름. pom 값으로 채우면 Maven과 다른 주소가 될 수 있다
_ELSEWHERE = ("env.", "project.", "pom.", "settings.", "maven.", "java.", "user.", "os.", "file.", "line.", "path.",
              "sun.", "jdk.", "basedir")
_PROP_RE = re.compile(r"\$\{([^}]+)\}")


def _fill(url: str, props: dict[str, str], blocked: set[str]) -> str:
    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in blocked or name.startswith(_ELSEWHERE) or name not in props:
            return m.group(0)
        return props[name]
    for _ in range(_PASSES):
        new = _PROP_RE.sub(one, url)
        if new == url:
            break
        url = new
    return url


def settings_property_names(*files: Optional[bytes]) -> set[str]:
    """settings 파일들의 모든 프로필(활성 여부와 상관없이)이 정의한 ``<properties>`` 이름을 반환한다.
    읽지 못한 파일은 건너뛴다.

    Maven gives a settings profile's properties precedence over a pom's; whether a profile is active (by
    property, file or OS) is not judged here, so every profile counts."""
    names: set[str] = set()
    for data in files:
        if not data:
            continue
        try:
            doc = _Doc(data, "settings")
        except Refused:
            continue
        for p in doc.et.findall("./profiles/profile/properties/*"):
            if isinstance(p.tag, str):
                names.add(p.tag)
    return names


def define_names(args: Iterable[str]) -> set[str]:
    """명령 인자와 ``MAVEN_OPTS`` / ``MAVEN_ARGS`` 조각에서 ``-D``로 정의한 속성 이름을 모아 반환한다.

    Forms Maven accepts: `-Dname=value`, `-Dname` (value `true`), `-D name=value`, `--define name=value`,
    `--define=name=value`."""
    names: set[str] = set()
    items = list(args)
    for i, a in enumerate(items):
        body = None
        if a in ("-D", "--define"):
            body = items[i + 1] if i + 1 < len(items) else None
        elif a.startswith("--define="):
            body = a[len("--define="):]
        elif a.startswith("-D") and len(a) > 2:
            body = a[2:]
        if body:
            names.add(body.split("=", 1)[0])
    return names


def _pom(path: Path, cache: dict[Path, Any]) -> Any:
    if path not in cache:
        try:
            el = ET.fromstring(path.read_bytes())
            for c in el.iter():
                if isinstance(c.tag, str) and "}" in c.tag:
                    c.tag = c.tag.split("}", 1)[1]
            cache[path] = el
        except (OSError, ET.ParseError, ValueError):
            cache[path] = None
    return cache[path]


def _coords(el: Any) -> tuple[str, str, str]:
    """(groupId, artifactId, version) of a pom, the groupId / version inherited from its `<parent>` when absent."""
    def get(tag: str) -> str:
        return _val(el, tag) or _val(el, f"parent/{tag}") or ""
    return get("groupId"), _val(el, "artifactId") or "", get("version")


def _chain_properties(pom: Path, base: Path, cache: dict[Path, Any]) -> tuple[dict[str, str], set[str]]:
    """(properties of the chain, child first; names any `<profile>` of the chain defines in `<properties>`)."""
    props: dict[str, str] = {}
    profile_names: set[str] = set()
    cur, seen = pom.resolve(), set()
    for _ in range(_CHAIN):
        el = _pom(cur, cache)
        if el is None or cur in seen:
            break
        seen.add(cur)
        for p in el.findall("./properties/*"):
            if isinstance(p.tag, str):
                props.setdefault(p.tag, (p.text or "").strip())
        # 활성화된 pom 프로필의 값이 위의 값을 이기므로, 어느 프로필이든 정의한 이름은 채우지 않는다
        profile_names |= {p.tag for p in el.findall("./profiles/profile/properties/*") if isinstance(p.tag, str)}
        parent = el.find("parent")
        if parent is None:
            break
        rel = _val(parent, "relativePath")
        if rel == "":
            break                        # 빈 relativePath는 부모를 저장소에서 받는다는 뜻이다
        nxt = (cur.parent / (rel or "../pom.xml")).resolve()
        if nxt.is_dir():
            nxt = nxt / "pom.xml"
        if not nxt.is_file() or base not in nxt.parents:
            break
        want = (_val(parent, "groupId") or "", _val(parent, "artifactId") or "", _val(parent, "version") or "")
        pel = _pom(nxt, cache)
        if pel is None or _coords(pel) != want:
            break
        cur = nxt
    return props, profile_names


# ------------------------------------------------------------------ 그림자 미러
def translate_settings(settings: bytes, *, global_settings: Optional[bytes] = None,
                       repositories: Iterable[tuple[str, str]] = ()) -> SettingsTranslation:
    """OpenRewrite가 Maven과 다른 미러를 고르는 저장소마다 그림자 미러를 더한 복사본을 만든다. 파일은 읽거나 쓰지 않는다.
    더할 것이 없으면 ``needed``가 False인 결과를, 파일을 읽을 수 없으면 ``reason``이 있는 결과를 반환한다.

    `repositories`: (id, url) of the project's poms (`project_repositories`); the active profiles of both settings
    files and Central are added here. A known id that begins `sbmig:` means no copy at all (Maven would match a
    shadow's whole `mirrorOf` with it). A shadow is written only where OpenRewrite would ask another url than Maven
    (compared after `_norm`), or would look up credentials under an id whose `<server>` is not Maven's (Maven: the
    selected mirror's id, else the repository's; OpenRewrite: the mirror it selects, else the repository's).
    Per id (rule 2), no shadow and a `skipped` entry when: the id holds `${`, a comma, `!` or `*`, or is `local`; a mirror's `mirrorOf` holds `${` (every id), or the mirror
    Maven selects has `${` in its id; its repositories go to different places in Maven; it is local only
    (OpenRewrite applies a mirror to non-local repositories); its url is unfilled and Maven's choice depends on it,
    or Maven selects no mirror (the shadow would need that url). No shadow and no entry when Maven's choice is a
    blocked mirror. Only insertions: the original bytes stay."""
    try:
        return _translate(settings, global_settings, list(repositories))
    except Refused as e:
        return SettingsTranslation(needed=True, reason=str(e))


def _translate(settings: bytes, global_settings: Optional[bytes],
               project_repos: list[tuple[str, str]]) -> SettingsTranslation:
    user = _Doc(settings, "user")
    glob = _Doc(global_settings, "global") if global_settings else None
    user_m = _mirrors(user)
    glob_m = _mirrors(glob) if glob is not None else []
    merged = user_m + [m for m in glob_m if m.id not in {x.id for x in user_m}]
    servers = {**_servers(glob), **_servers(user)}
    groups: dict[str, list[str]] = {}
    for rid, url in [CENTRAL, *_profile_repos(user), *_profile_repos(glob), *project_repos]:
        if rid and url and url not in groups.setdefault(rid, []):
            groups[rid].append(url)
    # Maven은 mirrorOf 전체를 id와 먼저 비교하므로 id가 sbmig:로 시작하면 그림자 미러가 그 저장소를 가져간다
    if any(rid.startswith(SHADOW_PREFIX) for rid in groups):
        return SettingsTranslation(needed=True, reason="a known repository id begins with sbmig:, which a shadow "
                                                       "mirror would take in Maven; no copy is made")
    unknown_of = next((m.id for m in merged if "${" in m.mirror_of), None)
    shadows: list[tuple[str, Optional[Mirror], str]] = []
    skipped: list[dict[str, str]] = []
    for rid, urls in groups.items():
        why, shadow = _plan(rid, urls, merged, unknown_of, servers)
        if why:
            skipped.append({"repository": rid, "reason": why})
        elif shadow is not None:
            shadows.append(shadow)
    if not shadows:
        return SettingsTranslation(needed=bool(skipped), skipped=skipped)
    data = _write(user, shadows, servers, {m.id for m in merged} | set(servers))
    if [m.id for m in _mirrors(_Doc(data, "copy"))][len(shadows):] != [m.id for m in user_m]:
        raise Refused("the written copy does not read back as planned")
    return SettingsTranslation(data=data, needed=True, translated=True, skipped=skipped,
                               shadows=[{"repository": rid, "mirror": m.id if m else None} for rid, m, _ in shadows])


def _plan(rid: str, urls: list[str], merged: list[Mirror], unknown_of: Optional[str],
          servers: dict[str, str]) -> tuple[Optional[str], Optional[tuple[str, Optional[Mirror], str]]]:
    """(reason to skip, None) or (None, (id, Maven's mirror, shadow url text)) or (None, None) = nothing to do."""
    if "${" in rid:
        return "the repository id holds a ${…}", None
    if rid == "local" or any(c in rid for c in ",!*"):
        return "the repository id cannot be named in a shadow mirror", None
    if unknown_of is not None:
        return f"the mirrorOf of mirror {unknown_of} holds a ${{…}}", None
    places: set[str] = set()
    choice: Optional[Mirror] = None
    differs = local_differs = False
    for url in urls:
        probes = _UNKNOWN_URLS if "${" in url else (url,)
        picks = {maven_choice(merged, rid, u) for u in probes}
        if len(picks) > 1:
            return "its url holds a ${…} and Maven's mirror depends on it", None
        choice = picks.pop()
        places.add(choice.id if choice else url)
        for u in probes:
            oa = openrewrite_choice(merged, rid, u)
            # 주소가 다르거나 자격 증명을 찾는 id가 다른 server를 가리킬 때만 그림자 미러를 쓴다
            same_url = _norm(oa.url if oa else u) == _norm(choice.url if choice else u)
            same_server = servers.get(oa.id if oa else rid) == servers.get(choice.id if choice else rid)
            if not (same_url and same_server):
                if _local(u):
                    local_differs = local_differs or "${" not in url
                else:
                    differs = True
    if len(places) > 1:
        return "repositories with this id go to different places in Maven", None
    if not differs:
        return ("the repository is local, which a shadow mirror cannot reach" if local_differs else None), None
    if choice is not None and choice.blocked:
        return None, None
    if choice is not None and "${" in choice.id:
        return f"the id of mirror {choice.id} holds a ${{…}}", None
    if choice is None:
        if "${" in urls[0]:
            return "its url holds a ${…} and Maven asks it directly", None
        return None, (rid, None, escape(urls[0]))
    return None, (rid, choice, choice.url_text)


def _norm(url: str) -> str:
    """scheme / host in lower case, the default port and trailing slashes dropped; a url that does not parse is
    compared as written (a needless shadow is harmless)."""
    from urllib.parse import urlsplit
    try:
        sp = urlsplit(url.strip())
        scheme, host = sp.scheme.lower(), (sp.hostname or "").lower()
        port = sp.port
    except ValueError:
        return url
    if not scheme or not host:
        return url
    if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}{sp.path.rstrip('/')}" + (f"?{sp.query}" if sp.query else "")


def _local(url: str) -> bool:
    scheme, host = _scheme_host(url)
    return url.lower().startswith("file:") or scheme.lower() == "file" or _local_host(host)


def _write(user: _Doc, shadows: list[tuple[str, Optional[Mirror], str]], servers: dict[str, str],
           taken: set[str]) -> bytes:
    """The user file with the shadows inserted at the front of `<mirrors>` and their servers at the front of
    `<servers>` (an element that does not exist is inserted before `</settings>`; an empty `<x/>` is refused)."""
    text, root = user.text, user.root
    if root.cstart == root.end:
        raise Refused("the user settings file is an empty <settings/> element")
    nl = "\r\n" if "\r\n" in text else "\n"
    child = _indent(text, root.children[0].start, "  ") if root.children else "  "
    item = child * 2
    mirrors, server_items = [], []
    for rid, mb, url_text in shadows:
        sid, n = SHADOW_ID + rid, 2
        while sid in taken:
            sid, n = f"{SHADOW_ID}{rid}-{n}", n + 1
        taken.add(sid)
        mirrors.append(f"<mirror><id>{escape(sid)}</id><mirrorOf>{escape(SHADOW_PREFIX + rid)}</mirrorOf>"
                       f"<url>{url_text}</url></mirror>")
        src = servers.get(mb.id if mb is not None else rid)
        if src is not None:
            server_items.append(src.replace("\0", escape(sid), 1))
    edits: list[tuple[int, str]] = []
    new_blocks = ""
    for name, items in (("servers", server_items), ("mirrors", mirrors)):
        if not items:
            continue
        node = root.kid(name)
        if node is not None and node.cstart == node.end:
            raise Refused(f"the user settings file has an empty <{name}/> element")
        if node is not None:
            first = next(iter(node.children), None)
            ind = _indent(text, first.start, item) if first is not None else item
            edits.append((node.cstart, "".join(nl + ind + x for x in items)))
        else:
            new_blocks += child + f"<{name}>" + "".join(nl + item + x for x in items) + nl + child + f"</{name}>" + nl
    if new_blocks:
        line = text.rfind("\n", 0, root.cend) + 1
        edits.append((line, new_blocks) if text[line:root.cend].strip() == "" else (root.cend, nl + new_blocks))
    for at, new in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:at] + new + text[at:]
    return user.encode(text)


def _indent(text: str, pos: int, fallback: str) -> str:
    lead = text[text.rfind("\n", 0, pos) + 1:pos]
    return lead if lead.strip() == "" else fallback


# ------------------------------------------------------------------ 파일 작업
@dataclass
class PreparedSettings:
    """OpenRewrite 호출에 쓸 settings 준비 결과를 담는 클래스.
    ``path``가 None이면 원본을 쓴다. ``error``는 준비 중 난 예외의 요약이다."""
    path: Optional[Path] = None
    translation: Optional[SettingsTranslation] = None
    error: Optional[str] = None


@contextmanager
def openrewrite_settings(cfg: Any, project_root: Optional[Path],
                         defines: Iterable[str] = ()) -> Iterator[PreparedSettings]:
    """OpenRewrite 호출 동안 쓸 settings 복사본을 만들고, 호출이 끝나면 지운다.
    ``openrewrite.translate_settings``가 꺼져 있거나 ``build.settings_file``이 없으면 아무것도 하지 않는다.
    읽기, 계산, 쓰기 중 예외가 나면 원본을 쓰도록 ``error``만 채운다.

    The copy is written with mode 0600 into a fresh `tempfile.mkdtemp` folder (0700), never into the run folder, and
    the folder is removed when the `with` block ends, also on an exception raised inside it (which propagates). Its
    content is never logged. Global settings: `<maven home>/conf/settings.xml` (what `-gs` passes), else
    `repo_versions.global_settings_file`. `defines`: the `-D` names of the OpenRewrite command and of its
    `MAVEN_OPTS`; with the settings profiles' property names they are never filled from a pom."""
    folder: Optional[str] = None
    prepared = PreparedSettings()
    try:
        sf = getattr(cfg.build, "settings_file", None)
        if getattr(cfg.openrewrite, "translate_settings", True) and sf and str(sf).strip():
            from sbmig.build.mvn import maven_global_settings
            from sbmig.build.repo_versions import global_settings_file
            gs = maven_global_settings(cfg) or global_settings_file(cfg)
            data = Path(str(sf)).expanduser().read_bytes()
            gdata = gs.read_bytes() if gs is not None else None
            blocked = settings_property_names(data, gdata) | set(defines)
            tr = translate_settings(data, global_settings=gdata,
                                    repositories=project_repositories(project_root, not_fillable=blocked))
            prepared = PreparedSettings(translation=tr)
            if tr.data is not None:
                folder = tempfile.mkdtemp(prefix="sbmig-rewrite-settings-")
                os.chmod(folder, 0o700)
                path = Path(folder) / "settings.xml"
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(tr.data)
                prepared.path = path
    except Exception as e:  # noqa: BLE001 - 복사본을 만들지 못하면 원본 settings로 계속 진행한다
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
            folder = None
        prepared = PreparedSettings(error=f"{type(e).__name__}: {str(e)[:160]}")
    try:
        yield prepared
    finally:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)


__all__ = ["Mirror", "PreparedSettings", "Refused", "SettingsTranslation", "define_names", "maven_choice",
           "maven_matches", "settings_property_names",
           "openrewrite_choice", "openrewrite_matches", "openrewrite_settings", "project_repositories",
           "translate_settings"]
