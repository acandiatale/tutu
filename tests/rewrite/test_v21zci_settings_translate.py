"""v2.1z-ci: the settings copy of the OpenRewrite call only ADDS shadow mirrors (`<mirrorOf>sbmig:<id></mirrorOf>`).

Properties, with both selection rules written again here, independently of `sbmig.rewrite.settings_translate`:
- Maven (guide-mirror-settings / `DefaultMirrorSelector`: an exact id first, then the first matching declaration;
  `*`, `external:*`, `external:http:*`, lists, `!id`; every token compared with the id as a whole) selects on the copy
  exactly what it selects on the original, for EVERY repository: the known ones, the same ids with https / localhost
  / file urls, and ids the translation never heard of;
- OpenRewrite (`MavenRepositoryMirror`, rewrite-maven 8.89.0: everything up to the first `:` dropped and the rest
  external-only, `*` whole, `!` exclusions, first match in file order, `<blocked>` ignored) selects on the copy the
  url (and credentials) Maven selects, for every known non-local repository that got a shadow; and every known
  non-local repository on which the two differ either got a shadow, or is skipped with a reason, or is refused by
  Maven (blocked mirror).
Merged settings = the user mirrors, then the global ones whose id the user file does not define.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import pytest

import sbmig.build.mvn as mvn_mod
import sbmig.rewrite.runner as runner_mod
import sbmig.rewrite.settings_translate as st
from sbmig.build.mvn import ProcessResult
from sbmig.core.models import RecipeRef
from sbmig.core.workspace import Workspace
from sbmig.rewrite import OpenRewriteRunner
from sbmig.rewrite.settings_translate import openrewrite_settings, project_repositories, translate_settings

NS = 'xmlns="http://maven.apache.org/SETTINGS/1.0.0"'
INHOUSE_PROFILE = """  <profiles>
    <profile>
      <id>inhouse</id>
      <repositories>
        <repository><id>inhouse</id><url>http://nexus.example.invalid:18803/</url></repository>
      </repositories>
    </profile>
  </profiles>
  <activeProfiles><activeProfile>inhouse</activeProfile></activeProfiles>
"""
# Maven 3.9's conf/settings.xml, its mirrors part (an example mirror inside a comment, then the HTTP blocker)
MAVEN_GLOBAL = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
  <mirrors>
    <!-- mirror
    <mirror>
      <id>mirrorId</id>
      <mirrorOf>repositoryId</mirrorOf>
      <url>http://my.repository.com/repo/path</url>
    </mirror>
     -->
    <mirror>
      <id>maven-default-http-blocker</id>
      <mirrorOf>external:http:*</mirrorOf>
      <name>Pseudo repository to mirror external repositories initially using HTTP.</name>
      <url>http://0.0.0.0/</url>
      <blocked>true</blocked>
    </mirror>
  </mirrors>
</settings>
""".encode()
GLOBAL_WITH_EXACT = MAVEN_GLOBAL.replace(b"    <mirror>\n      <id>maven-default-http-blocker</id>",
                                         b"    <mirror><id>global-central</id><mirrorOf>central</mirrorOf>"
                                         b"<url>https://central-mirror.example.invalid/</url></mirror>\n"
                                         b"    <mirror>\n      <id>maven-default-http-blocker</id>")


def mirror(mid: str, of: str, url: str, blocked: Optional[str] = None) -> str:
    b = f"<blocked>{blocked}</blocked>" if blocked is not None else ""
    return f"    <mirror><id>{mid}</id><mirrorOf>{of}</mirrorOf><url>{url}</url>{b}</mirror>"


def settings(*mirrors: str, servers: str = "", head: str = "", ns: str = NS, profile: str = INHOUSE_PROFILE) -> str:
    srv = f"  <servers>\n{servers}\n  </servers>\n" if servers else ""
    return (head + f"<settings {ns}>\n" + srv + "  <mirrors>\n" + "\n".join(mirrors) + "\n  </mirrors>\n"
            + profile + "</settings>\n")


M = "http://mirror.example.invalid:18801/"
CENTRAL_M = mirror("corp-central", "central", "http://public.example.invalid:18804/")
COMPANY = settings(mirror("maven-default-http-blocker", "external:http:*", M, "false"), CENTRAL_M)
SERVER = ("    <server><id>maven-default-http-blocker</id><username>deployer</username>"
          "<password>pw-not-real</password></server>")
COMMENTED = settings(
    "    <!-- the release group; keep first -->",
    "    <mirror>\n      <!-- inside -->\n      <id>corp-mirror</id>\n      <mirrorOf>external:http:*,!inhouse2</mirrorOf>"
    "\n      <url>" + M + "</url>\n    </mirror>",
    "    <!-- <mirror><id>old</id><mirrorOf>*</mirrorOf><url>http://old.example.invalid/</url></mirror> -->",
    CENTRAL_M,
    ns=NS + ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://maven.apache.org/'
            'SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd"',
    head='<?xml version="1.0" encoding="UTF-8"?>\n<!-- company settings -->\n')

# (name, user settings bytes, global settings bytes or None)
SETTINGS: list[tuple[str, bytes, Optional[bytes]]] = [
    ("star", settings(mirror("corp", "*", M)).encode(), MAVEN_GLOBAL),
    ("external-star", settings(mirror("corp", "external:*", M)).encode(), MAVEN_GLOBAL),
    ("external-http-star", settings(mirror("corp", "external:http:*", M), CENTRAL_M).encode(), MAVEN_GLOBAL),
    ("company-shape", COMPANY.encode(), MAVEN_GLOBAL),
    ("company-shape-with-server", settings(mirror("maven-default-http-blocker", "external:http:*", M, "false"),
                                           CENTRAL_M, servers=SERVER).encode(), MAVEN_GLOBAL),
    ("central-only", settings(CENTRAL_M).encode(), MAVEN_GLOBAL),
    ("id-list", settings(mirror("corp", "inhouse,central", M)).encode(), MAVEN_GLOBAL),
    ("id-list-space", settings(mirror("corp", "inhouse, central", M)).encode(), MAVEN_GLOBAL),
    ("star-not-inhouse", settings(mirror("corp", "*,!inhouse", M)).encode(), MAVEN_GLOBAL),
    ("external-star-not-inhouse", settings(mirror("corp", "external:*,!inhouse", M)).encode(), MAVEN_GLOBAL),
    ("not-inhouse-star", settings(mirror("corp", "!inhouse,*", M)).encode(), MAVEN_GLOBAL),
    ("two-overlapping", settings(mirror("pattern-first", "*", M),
                                 mirror("exact-second", "inhouse", "http://mirror2.example.invalid/")).encode(),
     MAVEN_GLOBAL),
    ("http-star-exclusions", settings(mirror("corp", "external:http:*,!inhouse,!old-http", M),
                                      mirror("rest", "*", "https://rest.example.invalid/")).encode(), MAVEN_GLOBAL),
    ("several-http-star", settings(mirror("first", "external:http:*,!inhouse2", M),
                                   mirror("second", "external:http:*", "http://second.example.invalid/"),
                                   CENTRAL_M).encode(), MAVEN_GLOBAL),
    ("blocked-and-http-star", settings(mirror("blk", "external:http:*", "http://blocked.example.invalid/", "true"),
                                       CENTRAL_M).encode(), MAVEN_GLOBAL),
    ("blocked-central", settings(mirror("corp", "inhouse", M),
                                 mirror("block-central", "central", "https://blocked.example.invalid/", "true")).encode(),
     MAVEN_GLOBAL),
    ("no-mirrors", settings().encode(), MAVEN_GLOBAL),
    ("no-mirrors-no-global", settings().encode(), None),
    ("no-mirrors-element", f"<settings {NS}>\n{INHOUSE_PROFILE}</settings>\n".encode(), MAVEN_GLOBAL),
    ("namespaces-and-comments", COMMENTED.encode(), MAVEN_GLOBAL),
    ("utf8-bom", b"\xef\xbb\xbf" + COMPANY.encode(), MAVEN_GLOBAL),
    ("latin-1", settings(mirror("maven-default-http-blocker", "external:http:*", M, "false"), CENTRAL_M,
                         head='<?xml version="1.0" encoding="ISO-8859-1"?>\n<!-- Équipe dépôt -->\n')
     .encode("iso-8859-1"), MAVEN_GLOBAL),
    ("utf16-bom", b"\xff\xfe" + settings(mirror("maven-default-http-blocker", "external:http:*", M, "false"),
                                         CENTRAL_M, head='<?xml version="1.0" encoding="UTF-16"?>\n')
     .encode("utf-16-le"), MAVEN_GLOBAL),
    ("crlf", COMPANY.replace("\n", "\r\n").encode(), MAVEN_GLOBAL),
    ("global-exact-behind-user-pattern", settings(mirror("corp", "external:*,!inhouse", M)).encode(),
     GLOBAL_WITH_EXACT),
    ("global-blocker-reached", settings(CENTRAL_M).encode(), MAVEN_GLOBAL),
    # two exact-id mirrors for one id behind a pattern: Maven takes the first of them
    ("two-exact-same-id", settings(mirror("all", "*", M), mirror("inhouse-first", "inhouse", "http://first.example.invalid/"),
                                   mirror("inhouse-second", "inhouse", "http://second.example.invalid/")).encode(),
     MAVEN_GLOBAL),
    ("user-redefines-blocker-inert", settings(mirror("maven-default-http-blocker", "none-such", "http://0.0.0.0/",
                                                     "false"), mirror("corp", "external:http:*", M)).encode(),
     MAVEN_GLOBAL),
    # OpenRewrite loses the id before a colon; the id is local, so a shadow cannot help it (skipped)
    ("id-then-external", settings(mirror("corp", "local-dev,external:*", M)).encode(), MAVEN_GLOBAL),
    ("http-and-https-id", settings(mirror("maven-default-http-blocker", "external:http:*", M, "false"),
                                   CENTRAL_M).encode(), MAVEN_GLOBAL),
    # a mirror url written with ${env.…} (Maven fills it in; the shadow copies the text as written)
    ("env-mirror-url", settings(mirror("maven-default-http-blocker", "external:http:*", "${env.NEXUS_URL}/group", "false"),
                                CENTRAL_M).encode(), MAVEN_GLOBAL),
    # a mirrorOf with ${…}: no choice can be computed, every id is skipped
    ("env-mirrorof", settings(mirror("x", "external:http:*", M), mirror("y", "${env.SBMIG_PROBE_OF}", M)).encode(),
     MAVEN_GLOBAL),
    # Maven compares "typo:inhouse-x" with the id as a whole (no match: direct); OpenRewrite drops "typo:" (mirror)
    ("direct-in-maven", settings(mirror("corp", "typo:inhouse-x", M)).encode(), MAVEN_GLOBAL),
]

# repositories the project's poms declare (handed to the translation)
POM_REPOS = [("inhouse2", "http://nexus2.example.invalid/repo"), ("secure", "https://secure.example.invalid/maven"),
             ("local-dev", "http://localhost:8081/repo"), ("loop", "http://127.0.0.1/repo"),
             ("files", "file:///opt/repo"), ("spring-milestones", "https://repo.spring.io/milestone"),
             ("old-http", "http://old.example.invalid/maven2"), ("inhouse-x", "https://inhouse-x.example.invalid/")]
KNOWN_SETTINGS_REPOS = [("central", "https://repo.maven.apache.org/maven2"), ("inhouse", "http://nexus.example.invalid:18803/")]
# repositories the translation never hears of: the known ids at other urls, and unknown ids
EXTRA_URLS = ("https://other.example.invalid/r", "http://localhost:9/r", "file:///tmp/r", "http://other.example.invalid/r")
UNKNOWN_REPOS = [(rid, u) for rid, _ in POM_REPOS + KNOWN_SETTINGS_REPOS for u in EXTRA_URLS] + \
    [("dep-pom-http", "http://dep.example.invalid/"), ("dep-pom-https", "https://dep.example.invalid/"),
     ("sbmig", "http://dep.example.invalid/"), ("inhouse,central", "http://dep.example.invalid/")]


# ------------------------------------------------------------------ reference rules (independent of the module)
def decode(data: bytes) -> tuple[str, str, bytes]:
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(enc), enc, bom
    m = re.match(rb'<\?xml[^>]*encoding="([^"]+)"', data)
    enc = m.group(1).decode() if m else "utf-8"
    return data.decode(enc), enc, b""


def parse(data: bytes) -> ET.Element:
    text = decode(data)[0]
    text = re.sub(r"^<\?xml[^>]*\?>", "", text)
    root = ET.fromstring(text)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]
    return root


def mirrors_of(data: Optional[bytes]) -> list[dict]:
    if data is None:
        return []
    out = []
    for m in parse(data).findall("./mirrors/mirror"):
        def v(tag, m=m):
            return (m.findtext(tag) or "").strip()
        out.append({"id": v("id"), "of": v("mirrorOf"), "url": v("url"), "blocked": v("blocked").lower() == "true"})
    return out


def servers_of(data: Optional[bytes]) -> dict[str, str]:
    if data is None:
        return {}
    return {s.findtext("id"): s.findtext("username") for s in parse(data).findall("./servers/server")}


def merged(user: bytes, glob: Optional[bytes]) -> list[dict]:
    u = mirrors_of(user)
    ids = {m["id"] for m in u}
    return u + [m for m in mirrors_of(glob) if m["id"] not in ids]


def is_local(url: str) -> bool:
    host = re.sub(r"^[a-z]+://", "", url).split("/")[0].split(":")[0]
    return url.startswith("file:") or host in ("localhost", "127.0.0.1")


def maven_ref(mirrors: list[dict], rid: str, url: str) -> Optional[dict]:
    """Maven's documented rule: a mirror whose mirrorOf is exactly the id first; else the first declaration that
    matches, where an explicit id or `!id` in the list decides and wildcards only count when nothing explicit did."""
    for m in mirrors:
        if m["of"] == rid:
            return m
    for m in mirrors:
        if m["of"] == "*":
            return m
        wildcard = False
        decided = None
        for tok in m["of"].split(","):
            if tok == "!" + rid and decided is None:
                decided = False
            elif tok == rid and decided is None:
                decided = True
            elif tok == "*" or (tok == "external:*" and not is_local(url)) or \
                    (tok == "external:http:*" and url.startswith("http://") and not is_local(url)):
                wildcard = True
        if decided if decided is not None else wildcard:
            return m
    return None


def openrewrite_ref(mirrors: list[dict], rid: str, url: str) -> Optional[dict]:
    """`MavenRepositoryMirror` (rewrite-maven 8.89.0), from its Java source:
         if ("local".equals(repository.getId())) return false;
         if ("*".equals(mirrorOf)) return true;
         int colonIndex = mirrorOf.indexOf(':');  ...  externalOnly = true;
         mirrorOfWithoutExternal = mirrorOf.substring(colonIndex + 1);   // then split(","), "!" = excluded
         if (externalOnly && isInternal(repository)) return false;
         if (excludedRepos.contains("*")) return includedRepos.contains(id);
         if (includedRepos.contains("*")) return !excludedRepos.contains(id);
         return !excludedRepos.contains(id) && includedRepos.contains(id);
       and `apply(mirrors, repo)` takes the first mirror in order that matches."""
    if rid == "local":
        return None
    for m in mirrors:
        of = m["of"]
        if of == "*":
            return m
        external_only = ":" in of
        toks = (of.split(":", 1)[1] if external_only else of).split(",")
        inc = {t for t in toks if not t.startswith("!")}
        exc = {t[1:] for t in toks if t.startswith("!")}
        if external_only and is_local(url):
            continue
        if ("*" in exc and rid in inc) or ("*" not in exc and "*" in inc and rid not in exc) or \
                ("*" not in exc and "*" not in inc and rid in inc and rid not in exc):
            return m
    return None


@pytest.fixture(scope="module")
def results() -> dict[str, st.SettingsTranslation]:
    return {n: translate_settings(u, global_settings=g, repositories=POM_REPOS) for n, u, g in SETTINGS}


def _copy(tr, user):
    return tr.data if tr.translated else user


MAVEN_CASES = [(n, u, g, r) for n, u, g in SETTINGS for r in POM_REPOS + KNOWN_SETTINGS_REPOS + UNKNOWN_REPOS]


@pytest.mark.parametrize("name,user,glob,repo", MAVEN_CASES, ids=[f"{c[0]}|{c[3][0]}|{c[3][1]}" for c in MAVEN_CASES])
def test_maven_selects_the_same_mirror_on_the_copy_for_every_repository(results, name, user, glob, repo):
    tr = results[name]
    before, after = merged(user, glob), merged(_copy(tr, user), glob)
    mb, ma = maven_ref(before, *repo), maven_ref(after, *repo)
    assert (mb and mb["id"]) == (ma and ma["id"])


OR_CASES = [(n, u, g, r) for n, u, g in SETTINGS for r in POM_REPOS + KNOWN_SETTINGS_REPOS]


@pytest.mark.parametrize("name,user,glob,repo", OR_CASES, ids=[f"{c[0]}|{c[3][0]}" for c in OR_CASES])
def test_openrewrite_gets_mavens_choice_where_a_shadow_was_written(results, name, user, glob, repo):
    tr = results[name]
    rid, url = repo
    before, after = merged(user, glob), merged(_copy(tr, user), glob)
    mb = maven_ref(before, rid, url)
    shadowed = {s["repository"] for s in tr.shadows}
    skipped = {s["repository"] for s in tr.skipped}
    if rid in shadowed and not is_local(url):
        chosen = openrewrite_ref(after, rid, url)
        assert chosen["of"] == "sbmig:" + rid
        assert chosen["url"] == (mb["url"] if mb else url)
        srv_before = servers_of(_copy(tr, user)) | {}
        assert servers_of(_copy(tr, user)).get(chosen["id"]) == srv_before.get(mb["id"] if mb else rid)
        return
    ob = openrewrite_ref(before, rid, url)
    if (ob and ob["id"]) != (mb and mb["id"]) and not is_local(url):
        assert rid in skipped or (mb is not None and mb["blocked"]), f"{rid}: no shadow, no reason"
    # without a shadow OpenRewrite reads the copy as it read the original
    oa = openrewrite_ref(after, rid, url)
    if rid not in shadowed:
        assert (oa and oa["id"]) == (ob and ob["id"])


@pytest.mark.parametrize("name,user,glob", SETTINGS, ids=[s[0] for s in SETTINGS])
def test_the_copy_only_inserts(results, name, user, glob):
    tr = results[name]
    if not tr.translated:
        return
    a, b = decode(user)[0], decode(tr.data)[0]
    ops = {op for op, *_ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()}
    assert ops <= {"equal", "insert"}, ops
    assert tr.data[:3] == user[:3]
    parse(tr.data)
    shadows = [m for m in mirrors_of(tr.data) if m["of"].startswith("sbmig:")]
    assert [m["id"] for m in mirrors_of(tr.data)][:len(shadows)] == [m["id"] for m in shadows]    # at the front


def test_which_files_get_shadows(results):
    got = {n: [s["repository"] for s in t.shadows] for n, t in results.items() if t.shadows}
    assert got == {
        "external-http-star": ["inhouse", "inhouse2", "old-http"],
        "company-shape": ["inhouse", "inhouse2", "old-http"],
        "company-shape-with-server": ["inhouse", "inhouse2", "old-http"],
        "two-overlapping": ["inhouse"],
        "http-star-exclusions": ["inhouse2"],
        "several-http-star": ["inhouse", "inhouse2", "old-http"],
        "namespaces-and-comments": ["inhouse", "old-http"],
        "utf8-bom": ["inhouse", "inhouse2", "old-http"],
        "latin-1": ["inhouse", "inhouse2", "old-http"],
        "utf16-bom": ["inhouse", "inhouse2", "old-http"],
        "crlf": ["inhouse", "inhouse2", "old-http"],
        "global-exact-behind-user-pattern": ["central"],
        "two-exact-same-id": ["inhouse"],
        "user-redefines-blocker-inert": ["inhouse", "inhouse2", "old-http"],
        "env-mirror-url": ["inhouse", "inhouse2", "old-http"],
        "direct-in-maven": ["inhouse-x"],
        "http-and-https-id": ["inhouse", "inhouse2", "old-http"],
    }
    # blocked mirrors are not imitated (Maven refuses those repositories in any case)
    assert not results["blocked-and-http-star"].needed and not results["global-blocker-reached"].needed


def test_the_company_shape_copy(results):
    tr = results["company-shape"]
    assert tr.record() == {"translated": True, "skipped": [], "reason": None,
                           "shadows": [{"repository": r, "mirror": "maven-default-http-blocker"}
                                       for r in ("inhouse", "inhouse2", "old-http")]}
    ms = mirrors_of(tr.data)
    assert [(m["id"], m["of"]) for m in ms[:3]] == [("sbmig-shadow-inhouse", "sbmig:inhouse"),
                                                    ("sbmig-shadow-inhouse2", "sbmig:inhouse2"),
                                                    ("sbmig-shadow-old-http", "sbmig:old-http")]
    assert all(m["url"] == M for m in ms[:3]) and ms[3:] == mirrors_of(COMPANY.encode())


def test_the_server_of_the_selected_mirror_is_copied(results):
    data = results["company-shape-with-server"].data
    assert servers_of(data) == {"maven-default-http-blocker": "deployer", "sbmig-shadow-inhouse": "deployer",
                                "sbmig-shadow-inhouse2": "deployer", "sbmig-shadow-old-http": "deployer"}
    assert data.decode().count("pw-not-real") == 4


def test_a_direct_repository_gets_its_own_url_and_server():
    text = settings(mirror("corp", "typo:inhouse-x", M),
                    servers="    <server><id>inhouse-x</id><username>own</username></server>")
    tr = translate_settings(text.encode(), global_settings=MAVEN_GLOBAL, repositories=POM_REPOS)
    assert {"repository": "inhouse-x", "mirror": None} in tr.shadows
    ms = {m["id"]: m for m in mirrors_of(tr.data)}
    assert ms["sbmig-shadow-inhouse-x"]["url"] == "https://inhouse-x.example.invalid/"
    assert servers_of(tr.data)["sbmig-shadow-inhouse-x"] == "own"


def test_skipped_ids_say_why(results):
    assert results["env-mirrorof"].skipped[0] == {"repository": "central",
                                                  "reason": "the mirrorOf of mirror y holds a ${…}"}
    assert not results["env-mirrorof"].translated
    loc = translate_settings(settings(mirror("corp", "local-dev,external:*", M)).encode(),
                             global_settings=MAVEN_GLOBAL, repositories=POM_REPOS)
    assert {"repository": "local-dev", "reason": "the repository is local, which a shadow mirror cannot reach"} \
        in loc.skipped
    two = translate_settings(COMPANY.encode(), global_settings=MAVEN_GLOBAL,
                             repositories=POM_REPOS + [("inhouse", "https://nexus.example.invalid/secure")])
    assert {"repository": "inhouse", "reason": "repositories with this id go to different places in Maven"} in two.skipped
    assert "inhouse" not in {s["repository"] for s in two.shadows}
    unfilled = translate_settings(COMPANY.encode(), global_settings=MAVEN_GLOBAL,
                                  repositories=[("a", "${repo.a.url}"), ("b", "${env.B}"), ("${env.ID}", "http://x.invalid/")])
    reasons = {s["repository"]: s["reason"] for s in unfilled.skipped}
    assert reasons == {"a": "its url holds a ${…} and Maven's mirror depends on it",
                       "b": "its url holds a ${…} and Maven's mirror depends on it",
                       "${env.ID}": "the repository id holds a ${…}"}


def test_an_unfilled_url_is_fine_when_maven_decides_by_id():
    text = settings(mirror("a-mirror", "a", "http://a-mirror.example.invalid/"), mirror("all", "*", M))
    tr = translate_settings(text.encode(), global_settings=MAVEN_GLOBAL, repositories=[("a", "${repo.a.url}")])
    assert tr.skipped == [] and tr.shadows == []       # OpenRewrite takes a-mirror too: nothing to do


def test_an_env_mirror_url_is_copied_as_written(results):
    tr = results["env-mirror-url"]
    assert tr.translated and b"<url>${env.NEXUS_URL}/group</url></mirror>" in tr.data


@pytest.mark.parametrize("data,why", [
    (b"<settings><mirrors><mirror><id>x</mirror></settings>", "not well-formed"),
    (b"garbage", "well-formed"),
    (settings("    <mirror><id>a</id><mirrorOf>*</mirrorOf><url>" + M + "</url><mirrorOfLayouts>default</mirrorOfLayouts>"
              "</mirror>").encode(), "mirrorOfLayouts"),
    (b'<!DOCTYPE settings [<!ENTITY x "y">]><settings/>', "declaration"),
    (b'<?xml version="1.0" encoding="UTF-8"?><settings>\xff</settings>', "decoded"),
])
def test_unreadable_files_are_refused_with_a_reason(data, why):
    tr = translate_settings(data, global_settings=MAVEN_GLOBAL)
    assert tr.needed and not tr.translated and tr.data is None and why in tr.reason


def test_an_empty_mirrors_element_is_not_rewritten():
    glob = settings(mirror("corp", "external:http:*", M), profile="").encode()
    tr = translate_settings(f"<settings {NS}>\n  <mirrors/>\n{INHOUSE_PROFILE}</settings>\n".encode(), global_settings=glob)
    assert tr.data is None and "empty <mirrors/>" in tr.reason


def test_a_missing_mirrors_or_servers_element_is_inserted_before_the_end():
    text = (f"<settings {NS}>\n{INHOUSE_PROFILE}</settings>\n")
    tr = translate_settings(text.encode(), global_settings=COMPANY.encode().replace(b"<mirrors>", SERVER_BLOCK + b"<mirrors>"))
    assert tr.translated
    out = tr.data.decode()
    assert out.startswith(text[:-len("</settings>\n")]) and out.endswith("</settings>\n")
    assert servers_of(tr.data) == {"sbmig-shadow-inhouse": "deployer"}


SERVER_BLOCK = ("<servers>" + SERVER + "</servers>\n  ").encode()


def test_no_record_carries_a_url_or_a_credential(results):
    blob = json.dumps({n: t.record() for n, t in results.items()})
    assert "://" not in blob and "pw-not-real" not in blob and "deployer" not in blob


# ------------------------------------------------------------------ the parsing assumption
def test_openrewrites_reading_of_a_shadow_mirrorof_is_pinned():
    # rewrite-maven 8.89.0, MavenRepositoryMirror:
    #     int colonIndex = mirrorOf.indexOf(':');
    #     ... externalOnly = true; mirrorOfWithoutExternal = mirrorOf.substring(colonIndex + 1);
    #     mirrorsOf = Arrays.stream(mirrorOfWithoutExternal.split(",")).collect(toList());
    # and Maven Resolver, DefaultMirrorSelector.matchPattern compares each token with repository.getId() whole.
    # A plugin that reads `mirrorOf` differently breaks the shadows; the integration test below notices it.
    assert st.openrewrite_matches("sbmig:inhouse", "inhouse", "http://nexus.example.invalid/")
    assert st.openrewrite_matches("sbmig:inhouse", "inhouse", "https://nexus.example.invalid/")
    assert not st.openrewrite_matches("sbmig:inhouse", "inhouse", "http://localhost/r")
    assert not st.openrewrite_matches("sbmig:inhouse", "other", "http://nexus.example.invalid/")
    for rid in ("inhouse", "sbmig", "sbmig:inhouse2", "*"):
        assert not st.maven_matches("sbmig:inhouse", rid, "http://nexus.example.invalid/")


# ------------------------------------------------------------------ project repositories
def test_project_repositories_reads_every_pair_profiles_and_unfilled_urls(tmp_path):
    (tmp_path / "m").mkdir()
    (tmp_path / "target").mkdir()
    (tmp_path / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><properties><repo.b>http://b.example.invalid/</repo.b>'
        "</properties><repositories><repository><id>a</id><url>http://same.example.invalid/</url></repository>"
        "<repository><id>a2</id><url>http://same.example.invalid/</url></repository>"
        "<repository><id>b</id><url>${repo.b}</url></repository></repositories>"
        "<profiles><profile><id>p</id><repositories><repository><id>c</id><url>${repo.c.url}</url></repository>"
        "</repositories></profile></profiles></project>")
    (tmp_path / "m/pom.xml").write_text("<project><repositories><repository><id>a</id><url>https://other.example.invalid/"
                                        "</url></repository></repositories></project>")
    (tmp_path / "target/pom.xml").write_text("<project><repositories><repository><id>t</id><url>http://t/</url>"
                                             "</repository></repositories></project>")
    assert project_repositories(tmp_path) == [("a", "http://same.example.invalid/"), ("a2", "http://same.example.invalid/"),
                                              ("b", "http://b.example.invalid/"), ("c", "${repo.c.url}"),
                                              ("a", "https://other.example.invalid/")]


# ------------------------------------------------------------------ the file side
@pytest.fixture()
def cfg_with(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(mvn_mod, "maven_global_settings", lambda cfg: None)
    monkeypatch.setattr("sbmig.build.repo_versions.global_settings_file", lambda cfg=None: None)

    def make(text: bytes, *, switch: bool = True, name: str = "settings.xml"):
        p = tmp_path / name
        p.write_bytes(text)
        c = cfg.model_copy(deep=True)
        c.build.settings_file = str(p)
        c.openrewrite.translate_settings = switch
        return c
    return make


def test_the_copy_is_private_and_removed(cfg_with, tmp_path):
    c = cfg_with(COMPANY.encode())
    with openrewrite_settings(c, tmp_path) as prep:
        assert prep.path is not None and prep.path.is_file()
        assert stat.S_IMODE(prep.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(prep.path.parent.stat().st_mode) == 0o700
        assert tmp_path not in prep.path.parents
        assert b"<mirrorOf>sbmig:inhouse</mirrorOf>" in prep.path.read_bytes()
        folder = prep.path.parent
    assert not folder.exists()
    with pytest.raises(RuntimeError):
        with openrewrite_settings(c, tmp_path) as prep:
            folder = prep.path.parent
            raise RuntimeError("the Maven call failed")
    assert not folder.exists()


def test_switch_off_and_plain_files_make_no_copy(cfg_with, tmp_path):
    with openrewrite_settings(cfg_with(COMPANY.encode(), switch=False), tmp_path) as prep:
        assert prep.path is None and prep.translation is None and prep.error is None
    with openrewrite_settings(cfg_with(settings(mirror("corp", "*", M)).encode()), tmp_path) as prep:
        assert prep.path is None and not prep.translation.needed


def test_unreadable_file_and_write_error_fall_back_to_the_original(cfg_with, tmp_path, monkeypatch):
    c = cfg_with(COMPANY.encode())
    Path(c.build.settings_file).unlink()
    Path(c.build.settings_file).mkdir()                                               # reading a folder raises
    with openrewrite_settings(c, tmp_path) as prep:
        assert prep.path is None and prep.error and prep.error.startswith("IsADirectoryError")
    c = cfg_with(COMPANY.encode(), name="settings-2.xml")
    made: list[str] = []
    real = st.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real(**kw))
        return made[-1]
    monkeypatch.setattr(st.tempfile, "mkdtemp", mkdtemp)

    real_open = os.open

    def refuse(path, flags, *a, **kw):
        if str(path).endswith("settings.xml") and flags & os.O_CREAT:
            raise OSError(28, "No space left on device")
        return real_open(path, flags, *a, **kw)
    monkeypatch.setattr(st.os, "open", refuse)
    with openrewrite_settings(c, tmp_path) as prep:
        assert prep.path is None and "No space left" in prep.error
    assert made and not os.path.exists(made[0])


# ------------------------------------------------------------------ through the runner
UP21 = "org.openrewrite.java.migrate.UpgradeToJava21"
OK_LOG = "[WARNING] Changes have been made to src/main/java/A.java by:\n[WARNING]     " + UP21 + "\n[INFO] BUILD SUCCESS\n"


@pytest.fixture()
def ws(tmp_path) -> Workspace:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>\n")
    w = Workspace(root, tmp_path / "run", "t-run")
    w.git_init()
    return w


@pytest.fixture()
def calls(monkeypatch):
    seen: list[dict] = []

    def fake_run(cmd, *, cwd, env, log_path, timeout_s):
        i = cmd.index("-s")
        p = Path(cmd[i + 1])
        seen.append({"cmd": list(cmd), "settings": p.read_bytes() if p.is_file() else None, "path": p})
        log_path.write_text(OK_LOG, encoding="utf-8")
        return ProcessResult(exit_code=0, text=OK_LOG, duration_s=0.1)
    monkeypatch.setattr(runner_mod, "run_logged", fake_run)
    monkeypatch.setattr(runner_mod, "resolve_recipe_artifact_versions",
                        lambda cfg, env, artifacts, cache_path, work_dir: {a: "1.0" for a in artifacts})
    return seen


def _events(ws, name):
    return [e for e in (json.loads(x) for x in ws.journal_path.read_text().splitlines()) if e["event"] == name]


def test_the_runner_passes_the_copy_and_records_once(cfg_with, ws, calls, caplog):
    c = cfg_with(COMPANY.encode())
    caplog.set_level("INFO", logger="sbmig.rewrite")
    for _ in range(2):
        assert OpenRewriteRunner(ws, c).run([RecipeRef(id=UP21)]).ok
    original = str(Path(c.build.settings_file).absolute())
    assert all(x["path"] != Path(original) and b"<id>sbmig-shadow-inhouse</id>" in x["settings"] for x in calls)
    assert not any(x["path"].exists() for x in calls)                                 # removed after each call
    (ev,) = _events(ws, "rewrite_settings")
    assert {k: ev[k] for k in ("translated", "shadows", "skipped", "reason")} == {
        "translated": True, "shadows": [{"repository": "inhouse", "mirror": "maven-default-http-blocker"}],
        "skipped": [], "reason": None}
    lines = [r.getMessage() for r in caplog.records if "settings file" in r.getMessage()]
    assert lines == [("rewrite: OpenRewrite gets a copy of the settings file settings.xml with 1 shadow mirror(s) "
                      "(inhouse -> maven-default-http-blocker; skipped: none)")]
    assert "mirror.example.invalid" not in caplog.text


def test_a_file_that_needs_nothing_gives_the_same_command(cfg_with, ws, calls):
    star = settings(mirror("corp", "*", M)).encode()
    OpenRewriteRunner(ws, cfg_with(star)).run([RecipeRef(id=UP21)])
    OpenRewriteRunner(ws, cfg_with(star, switch=False)).run([RecipeRef(id=UP21)])
    norm = [[re.sub(r"rewrite-\d+\.yml", "rewrite-N.yml", a) for a in x["cmd"]] for x in calls]
    assert norm[0] == norm[1] and calls[0]["settings"] == star
    assert not _events(ws, "rewrite_settings")


def test_a_refusal_and_an_error_are_recorded_and_the_original_is_passed(cfg_with, ws, calls, caplog, monkeypatch):
    caplog.set_level("INFO", logger="sbmig.rewrite")
    c = cfg_with(b"garbage")
    assert OpenRewriteRunner(ws, c).run([RecipeRef(id=UP21)]).ok
    assert calls[-1]["settings"] == b"garbage"
    (ev,) = _events(ws, "rewrite_settings")
    assert ev["translated"] is False and "well-formed" in ev["reason"]

    def boom(*a, **kw):
        raise ValueError("unexpected")
    monkeypatch.setattr(st, "translate_settings", boom)
    c2 = cfg_with(COMPANY.encode())
    assert OpenRewriteRunner(ws, c2).run([RecipeRef(id=UP21)]).ok
    assert calls[-1]["settings"] == COMPANY.encode()
    ev = _events(ws, "rewrite_settings")[-1]
    assert ev["reason"] == "error: ValueError: unexpected"
    warn = [r for r in caplog.records if r.levelname == "WARNING" and "no settings copy for OpenRewrite" in r.getMessage()]
    assert len(warn) == 1


def test_each_run_gets_its_own_record_in_one_process(cfg_with, tmp_path, calls):
    # the in-process web mode runs one migration after another in the same process
    c = cfg_with(COMPANY.encode())
    seen = []
    for n in (1, 2):
        root = tmp_path / f"proj{n}"
        root.mkdir()
        (root / "pom.xml").write_text("<project/>\n")
        w = Workspace(root, tmp_path / f"run{n}", f"t-run{n}")
        w.git_init()
        OpenRewriteRunner(w, c).run([RecipeRef(id=UP21)])
        OpenRewriteRunner(w, c).run([RecipeRef(id=UP21)])
        seen.append(len(_events(w, "rewrite_settings")))
    assert seen == [1, 1]


# ------------------------------------------------------------------ end to end with the real plugin
JDK21 = Path(os.environ.get("SBMIG_TEST_JAVA21_HOME",
                            "/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-21.0.1/Contents/Home"))
SHADOW_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>com.example.sbmigit</groupId><artifactId>app</artifactId><version>1</version>
<properties><maven.compiler.source>1.8</maven.compiler.source><maven.compiler.target>1.8</maven.compiler.target>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>com.example.sbmigit</groupId><artifactId>shadowdep</artifactId><version>1</version>
<classifier>cls</classifier></dependency></dependencies></project>
"""


@pytest.mark.integration
def test_real_plugin_reads_a_shadow_mirror(cfg, tmp_path):
    """One shadow end to end with Maven and the pinned rewrite-maven-plugin: the original repository `inhouse`
    (http, a name that does not resolve) is mirrored by `external:http:*` to a local server holding a dependency
    used with a classifier (so OpenRewrite always downloads its pom). Without the copy OpenRewrite asks `inhouse`
    and fails; with it, the shadow sends it to the mirror. A plugin that reads `mirrorOf` differently fails here."""
    import threading
    import zipfile
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    from sbmig.build.mvn import maven_available
    if not JDK21.exists():
        pytest.skip("JDK 21 not available")
    ok, msg = maven_available(cfg)
    if not ok:
        pytest.skip(f"mvn not available: {msg}")
    served = tmp_path / "served/com/example/sbmigit/shadowdep/1"
    served.mkdir(parents=True)
    (served / "shadowdep-1.pom").write_text(
        "<project><modelVersion>4.0.0</modelVersion><groupId>com.example.sbmigit</groupId>"
        "<artifactId>shadowdep</artifactId><version>1</version></project>")
    with zipfile.ZipFile(served / "shadowdep-1-cls.jar", "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    asked: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path / "served"), **kw)

        def log_message(self, *a):
            asked.append(self.path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        user = (f"<settings>\n  <mirrors>\n    <mirror><id>corp</id><mirrorOf>external:http:*</mirrorOf>"
                f"<url>http://127.0.0.1:{port}/</url></mirror>\n  </mirrors>\n  <profiles><profile><id>p</id>"
                "<repositories><repository><id>inhouse</id><url>http://inhouse.invalid/repo</url></repository>"
                "</repositories></profile></profiles>\n  <activeProfiles><activeProfile>p</activeProfile>"
                "</activeProfiles>\n</settings>\n")
        (tmp_path / "settings.xml").write_text(user)
        results = {}
        for switch in (False, True):
            src = tmp_path / f"src-{switch}"
            (src / "src/main/java/com/example").mkdir(parents=True)
            (src / "pom.xml").write_text(SHADOW_POM)
            (src / "src/main/java/com/example/App.java").write_text("package com.example; public class App {}\n")
            w = Workspace.from_input(src, tmp_path / f"out-{switch}", run_id=f"it-shadow-{switch}")
            w.git_init()
            c = cfg.model_copy(deep=True)
            c.build.java_home = str(JDK21)
            c.build.settings_file = str(tmp_path / "settings.xml")
            c.build.local_repo = str(tmp_path / f"m2-{switch}")
            home_repo = Path.home() / ".m2" / "repository"
            if home_repo.is_dir():
                c.build.extra_args = [*c.build.extra_args, f"-Dmaven.repo.local.tail={home_repo}"]
            c.build.extra_args = [*c.build.extra_args, f"-Drewrite.pomCacheDirectory={tmp_path}/rcache-{switch}"]
            c.openrewrite.translate_settings = switch
            c.openrewrite.download_retry = 0
            # a pinned recipe version: no BOM lookup (it would look for the BOM in the head repository only)
            c.openrewrite.recipe_artifact_versions = {"org.openrewrite.recipe:rewrite-migrate-java": "3.42.0"}
            results[switch] = OpenRewriteRunner(w, c).run([RecipeRef(id=UP21)])
        assert not results[False].ok and "Unable to download POM: com.example.sbmigit:shadowdep:1" in results[False].error
        assert results[True].ok, results[True].error
        assert asked.count("/com/example/sbmigit/shadowdep/1/shadowdep-1.pom") >= 2   # Maven, then OpenRewrite
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ properties along the parent chain
def _p(path: Path, gav: tuple[str, str, str], *, parent: Optional[tuple[str, str, str]] = None, rel: Optional[str] = None,
       props: str = "", repos: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    par = ""
    if parent:
        r = "" if rel is None else (f"<relativePath>{rel}</relativePath>" if rel else "<relativePath/>")
        par = f"<parent><groupId>{parent[0]}</groupId><artifactId>{parent[1]}</artifactId><version>{parent[2]}</version>{r}</parent>"
    path.write_text(f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>{par}'
                    f"<groupId>{gav[0]}</groupId><artifactId>{gav[1]}</artifactId><version>{gav[2]}</version>"
                    f"<properties>{props}</properties><repositories>{repos}</repositories></project>")


ROOT_GAV, MID_GAV, LEAF_GAV = ("com.example", "root", "1"), ("com.example", "mid", "1"), ("com.example", "leaf", "1")
INHOUSE_REPO = "<repository><id>inhouse</id><url>${nexus.base}/maven-releases/</url></repository>"


def test_a_property_of_a_parent_in_the_project_fills_the_url_and_a_shadow_is_written(tmp_path):
    # the reviewer's shape: the root pom defines nexus.base, a module declares inhouse = ${nexus.base}/maven-releases/
    _p(tmp_path / "pom.xml", ROOT_GAV, props="<nexus.base>http://nexus.example.invalid/repository</nexus.base>")
    (tmp_path / "m").mkdir()
    (tmp_path / "m/pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><parent><groupId>com.example'
        "</groupId><artifactId>root</artifactId><version>1</version></parent><artifactId>m</artifactId><repositories>"
        f"{INHOUSE_REPO}</repositories></project>")             # groupId / version inherited from the parent
    repos = project_repositories(tmp_path)
    assert repos == [("inhouse", "http://nexus.example.invalid/repository/maven-releases/")]
    tr = translate_settings(COMPANY.replace("<id>inhouse</id><url>http://nexus.example.invalid:18803/</url>",
                                            "<id>inhouse</id><url>http://nexus.example.invalid/repository/maven-releases/"
                                            "</url>").encode(), global_settings=MAVEN_GLOBAL, repositories=repos)
    assert {"repository": "inhouse", "mirror": "maven-default-http-blocker"} in tr.shadows and tr.skipped == []


def test_three_levels_and_a_child_override(tmp_path):
    _p(tmp_path / "pom.xml", ROOT_GAV, props="<nexus.base>http://root.example.invalid</nexus.base><x>root-x</x>")
    _p(tmp_path / "mid/pom.xml", MID_GAV, parent=ROOT_GAV, props="<x>mid-x</x>")
    _p(tmp_path / "mid/leaf/pom.xml", LEAF_GAV, parent=MID_GAV,
       repos="<repository><id>a</id><url>${nexus.base}/${x}/</url></repository>")
    _p(tmp_path / "mid/leaf2/pom.xml", ("com.example", "leaf2", "1"), parent=MID_GAV, props="<x>own-x</x>",
       repos="<repository><id>b</id><url>${nexus.base}/${x}/</url></repository>")
    assert project_repositories(tmp_path) == [("a", "http://root.example.invalid/mid-x/"),
                                              ("b", "http://root.example.invalid/own-x/")]


def test_a_cycle_ends_the_walk(tmp_path):
    _p(tmp_path / "a/pom.xml", ("g", "a", "1"), parent=("g", "b", "1"), rel="../b/pom.xml", props="<p1>one</p1>",
       repos="<repository><id>r</id><url>http://${p1}.${p2}.${p3}/</url></repository>")
    _p(tmp_path / "b/pom.xml", ("g", "b", "1"), parent=("g", "a", "1"), rel="../a/pom.xml", props="<p2>two</p2>")
    assert project_repositories(tmp_path) == [("r", "http://one.two.${p3}/")]


def test_a_parent_outside_the_project_or_with_other_coordinates_is_not_read(tmp_path):
    proj = tmp_path / "proj"
    _p(tmp_path / "pom.xml", ROOT_GAV, props="<nexus.base>http://outside.example.invalid</nexus.base>")
    _p(proj / "pom.xml", ("com.example", "app", "1"), parent=ROOT_GAV, repos=INHOUSE_REPO,
       props="<other.base>http://app.example.invalid</other.base>")
    _p(proj / "m/pom.xml", ("com.example", "m", "1"), parent=("com.example", "not-the-root", "1"),
       repos="<repository><id>other</id><url>${other.base}/x/</url></repository>")   # ../pom.xml is app, not it
    _p(proj / "n/pom.xml", ("com.example", "n", "1"), parent=ROOT_GAV, rel="",
       repos="<repository><id>third</id><url>${nexus.base}/y/</url></repository>")
    repos = project_repositories(proj)
    assert repos == [("inhouse", "${nexus.base}/maven-releases/"), ("other", "${other.base}/x/"),
                     ("third", "${nexus.base}/y/")]
    tr = translate_settings(COMPANY.encode(), global_settings=MAVEN_GLOBAL, repositories=repos[:1])
    assert tr.skipped == [{"repository": "inhouse", "reason": "its url holds a ${…} and Maven's mirror depends on it"}]


def test_a_repository_id_beginning_with_the_shadow_prefix_means_no_copy():
    # Maven compares a mirror's whole mirrorOf with the id first: a repository "sbmig:rel" would take shadow "sbmig:rel"
    tr = translate_settings(COMPANY.encode(), global_settings=MAVEN_GLOBAL,
                            repositories=POM_REPOS + [("sbmig:rel", "https://r-b.example.invalid/")])
    assert tr.needed and not tr.translated and tr.data is None and "sbmig:" in tr.reason


def test_no_shadow_when_openrewrite_would_ask_the_same_url_anyway():
    # OpenRewrite takes another mirror than Maven, but at the same url: a shadow would only cost
    text = settings(mirror("first", "typo:secure", M), mirror("second", "secure", M))
    tr = translate_settings(text.encode(), global_settings=MAVEN_GLOBAL, repositories=POM_REPOS)
    assert "secure" not in {s["repository"] for s in tr.shadows} and not tr.skipped
    other = settings(mirror("first", "typo:secure", M), mirror("second", "secure", "http://second.example.invalid/"))
    tr = translate_settings(other.encode(), global_settings=MAVEN_GLOBAL, repositories=POM_REPOS)
    assert {"repository": "secure", "mirror": "second"} in tr.shadows


# ------------------------------------------------------------------ names that outrank a pom property in Maven
def _round4_project(root: Path) -> None:
    """The reviewer's round-4 project: the root pom defines repo.url (http) and a nested value; modules declare
    repositories at ${repo.url} and ${nested}."""
    _p(root / "pom.xml", ROOT_GAV, props="<repo.url>http://r-a.example.invalid/rel/</repo.url><nested>${host.part}"
                                         "/nested/</nested><host.part>http://r-c.example.invalid</host.part>")
    _p(root / "m/pom.xml", ("com.example", "m", "1"), parent=ROOT_GAV,
       repos="<repository><id>rel</id><url>${repo.url}</url></repository>"
             "<repository><id>nest</id><url>${nested}</url></repository>")


PROFILE_SETTINGS = ('<settings><mirrors><mirror><id>maven-default-http-blocker</id><mirrorOf>external:http:*</mirrorOf>'
                    '<url>https://nexus.example.invalid/group/</url></mirror></mirrors><profiles><profile><id>p</id>'
                    '<properties><repo.url>https://r-b.example.invalid/rel/</repo.url></properties></profile></profiles>'
                    '{active}</settings>')


@pytest.mark.parametrize("active", ["<activeProfiles><activeProfile>p</activeProfile></activeProfiles>", ""])
def test_a_settings_profile_property_is_never_filled_from_the_pom(tmp_path, active):
    # Maven uses the profile's https url, which it asks directly; the pom's http url would have sent OpenRewrite to the
    # blocker's mirror. Active or not: activation by property / file / OS is not judged here.
    _round4_project(tmp_path)
    data = PROFILE_SETTINGS.format(active=active).encode()
    repos = project_repositories(tmp_path, not_fillable=st.settings_property_names(data, None))
    assert repos == [("rel", "${repo.url}"), ("nest", "http://r-c.example.invalid/nested/")]
    tr = translate_settings(data, global_settings=b"<settings/>", repositories=repos)
    assert "rel" not in {s["repository"] for s in tr.shadows}
    assert {"repository": "rel", "reason": "its url holds a ${…} and Maven's mirror depends on it"} in tr.skipped
    assert {"repository": "nest", "mirror": "maven-default-http-blocker"} in tr.shadows      # nested value filled


def test_a_command_line_define_is_never_filled_from_the_pom(tmp_path):
    _round4_project(tmp_path)
    names = st.define_names(["mvn", "-B", "-Drepo.url=https://r-b.example.invalid/rel/", "-Dx", "-P", "p"])
    assert names == {"repo.url", "x"}
    assert project_repositories(tmp_path, not_fillable=names)[0] == ("rel", "${repo.url}")
    assert project_repositories(tmp_path)[0] == ("rel", "http://r-a.example.invalid/rel/")       # the plain shape


def test_names_maven_resolves_elsewhere_are_never_filled(tmp_path):
    _p(tmp_path / "pom.xml", ROOT_GAV, props="<env.NEXUS>http://a.invalid</env.NEXUS><project.version>9</project.version>"
                                             "<user.home>/x</user.home>",
       repos="<repository><id>a</id><url>${env.NEXUS}/r/</url></repository>"
             "<repository><id>b</id><url>http://b.invalid/${project.version}/</url></repository>"
             "<repository><id>c</id><url>file://${user.home}/r/</url></repository>")
    assert [u for _, u in project_repositories(tmp_path)] == ["${env.NEXUS}/r/", "http://b.invalid/${project.version}/",
                                                             "file://${user.home}/r/"]


def test_the_runner_hands_its_defines_and_maven_opts_to_the_reader(cfg_with, ws, calls, monkeypatch):
    _round4_project(ws.root)
    seen = {}
    real = st.project_repositories

    def spy(root, limit=400, not_fillable=()):
        seen["names"] = set(not_fillable)
        return real(root, limit, not_fillable)
    monkeypatch.setattr(st, "project_repositories", spy)
    c = cfg_with(PROFILE_SETTINGS.format(active="").encode())
    c.build.extra_args = [*c.build.extra_args, "-Dfrom.cmd=1"]
    c.build.env = {"MAVEN_OPTS": "-Dfrom.opts=1"}
    OpenRewriteRunner(ws, c).run([RecipeRef(id=UP21)])
    assert {"repo.url", "from.cmd", "from.opts"} <= seen["names"]


def test_an_empty_relative_path_is_not_followed_even_when_the_parent_file_matches(tmp_path):
    _p(tmp_path / "pom.xml", ROOT_GAV, props="<nexus.base>http://nexus.example.invalid/repository</nexus.base>")
    _p(tmp_path / "m/pom.xml", ("com.example", "m", "1"), parent=ROOT_GAV, rel="", repos=INHOUSE_REPO)
    assert project_repositories(tmp_path) == [("inhouse", "${nexus.base}/maven-releases/")]


# ------------------------------------------------------------------ credentials and url normalisation
def test_same_url_but_other_credentials_gets_a_shadow():
    # a project repository at exactly the redefined blocker's url: Maven asks it under the blocker's id (with the
    # blocker's server), OpenRewrite under the repository's own id (no server): a shadow carries the server
    text = settings(mirror("maven-default-http-blocker", "external:http:*", M, "false"), CENTRAL_M, servers=SERVER)
    tr = translate_settings(text.encode(), global_settings=MAVEN_GLOBAL, repositories=[("rel", M)])
    assert {"repository": "rel", "mirror": "maven-default-http-blocker"} in tr.shadows
    assert servers_of(tr.data)["sbmig-shadow-rel"] == "deployer"
    # without any server on either side the same url needs no shadow
    plain = translate_settings(COMPANY.encode(), global_settings=MAVEN_GLOBAL, repositories=[("rel", M)])
    assert "rel" not in {s["repository"] for s in plain.shadows}


def test_urls_that_differ_only_in_form_need_no_shadow():
    text = settings(mirror("first", "typo:secure", "HTTP://Mirror.Example.invalid:80/x"),
                    mirror("second", "secure", "http://mirror.example.invalid/x/"))
    tr = translate_settings(text.encode(), global_settings=MAVEN_GLOBAL, repositories=POM_REPOS)
    assert "secure" not in {s["repository"] for s in tr.shadows}
    text = settings(mirror("first", "typo:secure", "http://mirror.example.invalid:8081/x"),
                    mirror("second", "secure", "http://mirror.example.invalid/x/"))
    tr = translate_settings(text.encode(), global_settings=MAVEN_GLOBAL, repositories=POM_REPOS)
    assert {"repository": "secure", "mirror": "second"} in tr.shadows


# ------------------------------------------------------------------ pom profiles and the other ways of giving -D
def _round5_project(root: Path, *, unrelated: bool = False) -> None:
    """The reviewer's round-5 project: the root pom's repo.url is redefined by a profile active by default, the
    module's own.url by a profile activated by the jdk."""
    prof = "<other.name>x</other.name>" if unrelated else "<repo.url>https://r-p.example.invalid/rel/</repo.url>"
    (root / "m").mkdir(parents=True, exist_ok=True)
    (root / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>com.example'
        "</groupId><artifactId>root</artifactId><version>1</version><packaging>pom</packaging><properties><repo.url>"
        "http://r-a.example.invalid/rel/</repo.url></properties><profiles><profile><id>prod</id><activation>"
        f"<activeByDefault>true</activeByDefault></activation><properties>{prof}</properties></profile></profiles>"
        "<modules><module>m</module></modules></project>")
    own = "<other.name>y</other.name>" if unrelated else "<own.url>https://r-j.example.invalid/own/</own.url>"
    (root / "m/pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><parent><groupId>'
        "com.example</groupId><artifactId>root</artifactId><version>1</version></parent><artifactId>m</artifactId>"
        "<properties><own.url>http://r-o.example.invalid/own/</own.url></properties><profiles><profile><id>jdk</id>"
        f"<activation><jdk>[17,)</jdk></activation><properties>{own}</properties></profile></profiles>"
        "<repositories><repository><id>rel</id><url>${repo.url}</url></repository><repository><id>own</id>"
        "<url>${own.url}</url></repository></repositories></project>")


def test_a_name_any_pom_profile_of_the_chain_defines_is_never_filled(tmp_path):
    _round5_project(tmp_path)
    assert project_repositories(tmp_path) == [("rel", "${repo.url}"), ("own", "${own.url}")]
    tr = translate_settings(COMPANY.encode(), global_settings=MAVEN_GLOBAL, repositories=project_repositories(tmp_path))
    assert {s["repository"] for s in tr.skipped} >= {"rel", "own"} and not {"rel", "own"} & {
        s["repository"] for s in tr.shadows}


def test_a_profile_defining_another_name_does_not_stop_the_filling(tmp_path):
    _round5_project(tmp_path, unrelated=True)
    assert project_repositories(tmp_path) == [("rel", "http://r-a.example.invalid/rel/"),
                                              ("own", "http://r-o.example.invalid/own/")]


@pytest.mark.parametrize("args,name", [
    (["-Down.url=https://x/"], "own.url"), (["-Down.url"], "own.url"), (["-D", "own.url=https://x/"], "own.url"),
    (["--define", "own.url=https://x/"], "own.url"), (["--define=own.url=https://x/"], "own.url")])
def test_every_define_form_names_the_property(args, name):
    assert st.define_names(["mvn", "-B", *args, "validate"]) == {name}


def test_maven_args_of_the_child_environment_count_as_defines(cfg_with, ws, calls, monkeypatch):
    seen = {}
    real = st.project_repositories

    def spy(root, limit=400, not_fillable=()):
        seen["names"] = set(not_fillable)
        return real(root, limit, not_fillable)
    monkeypatch.setattr(st, "project_repositories", spy)
    c = cfg_with(COMPANY.encode())
    c.build.env = {"MAVEN_ARGS": "-D own.url=https://x/ --define=repo.url=https://y/", "MAVEN_OPTS": "-Dfrom.opts"}
    OpenRewriteRunner(ws, c).run([RecipeRef(id=UP21)])
    assert {"own.url", "repo.url", "from.opts"} <= seen["names"]


def test_of_two_servers_with_one_id_the_first_is_copied():
    # real Maven 3.9.10 (scratch dupsrv/run.py): with two <server> entries of one id it sends the FIRST one's
    # credentials (good-then-bad resolved, bad-then-good got 401)
    text = settings(mirror("maven-default-http-blocker", "external:http:*", M, "false"), CENTRAL_M,
                    servers=SERVER + "\n" + SERVER.replace("deployer", "second-user"))
    tr = translate_settings(text.encode(), global_settings=MAVEN_GLOBAL)
    assert servers_of(tr.data)["sbmig-shadow-inhouse"] == "deployer"
