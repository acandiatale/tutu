"""2026-10-22 era libraries in the triage tables: jakarta-library-floor `classifier` (Ehcache 3), new jakarta-table
families, relocations.yml rows (reactive only), known_packages.yml rows (longest prefix) and the Boot 4.1 fallback
versions."""
from __future__ import annotations

from sbmig.core.models import GAV, ErrorKind, PomInfo
from sbmig.triage import cluster_errors
from sbmig.triage.handlers.pom_handlers import known_versions
from sbmig.triage.handlers.pom_handlers import jakarta_libraries_below, known_package_table, relocation_table

from .conftest import make_profile
from .fakes import err

G = GAV.parse
EHCACHE = "org.ehcache:ehcache"


def _g(coords: str, classifier: str | None = None) -> GAV:
    g = G(coords)
    return g.model_copy(update={"classifier": classifier}) if classifier else g


def _profile(*deps: GAV, managed: tuple[GAV, ...] = ()):
    return make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo", is_root=True, dependencies=list(deps),
                                      dependency_management=list(managed))])


# ---------------------------------------------------------------------------------------------------------------------
# jakarta-library-floor: classifier entries (Ehcache 3 → classifier jakarta)
# ---------------------------------------------------------------------------------------------------------------------
def test_ehcache_old_version_bumped_and_classified(kit):
    kit.pom(deps=[G(f"{EHCACHE}:3.8.1")])
    h = kit.handler("jakarta-library-floor")
    assert [c.path for c in h.apply(kit.ws, None, None, None)] == ["pom.xml"]
    d = kit.dep(EHCACHE)
    assert d.version == "3.12.0" and d.classifier == "jakarta"
    assert h.flags == ["jakarta-library-bumped"]
    assert h.notes[0].startswith("org.ehcache:ehcache 3.8.1 → 3.12.0 (classifier jakarta) (Ehcache 3's only javax")
    assert h.apply(kit.ws, None, None, None) == []                       # idempotent


def test_ehcache_on_the_jakarta_line_without_classifier_gets_only_the_classifier(kit):
    kit.pom(deps=[G(f"{EHCACHE}:3.10.8")])
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None)
    d = kit.dep(EHCACHE)
    assert d.version == "3.10.8" and d.classifier == "jakarta"           # ≥ jakarta_since: the version stays
    assert h.notes[0].startswith("org.ehcache:ehcache 3.10.8 → 3.10.8 (classifier jakarta)")


def test_ehcache_with_classifier_untouched(kit):
    kit.pom(deps=[_g(f"{EHCACHE}:3.12.0", "jakarta")])
    assert kit.handler("jakarta-library-floor").apply(kit.ws, None, None, None) == []
    assert kit.dep(EHCACHE).classifier == "jakarta" and kit.dep(EHCACHE).version == "3.12.0"


def test_ehcache_unversioned_child_of_a_managing_pom(fake_kit):
    fake_kit.pom(managed=[G(f"{EHCACHE}:3.9.11")], packaging="pom", artifact="parent")
    fake_kit.pom("app/pom.xml", deps=[G(EHCACHE)], artifact="app")
    assert fake_kit.handler("jakarta-library-floor").apply(fake_kit.ws, None, None, None)
    m = fake_kit.dep(EHCACHE, managed=True)
    assert m.version == "3.12.0" and m.classifier == "jakarta"
    c = fake_kit.dep(EHCACHE, "app/pom.xml")
    assert c.version is None and c.classifier == "jakarta"               # Maven matches management on the classifier


def test_classifier_counts_in_jakarta_libraries_below():
    [(lib, shown)] = [x for x in jakarta_libraries_below(_profile(G(f"{EHCACHE}:3.12.0"))) if x[0].key == EHCACHE]
    assert lib.classifier == "jakarta" and shown == "3.12.0 without classifier jakarta"
    assert not [x for x in jakarta_libraries_below(_profile(_g(f"{EHCACHE}:3.12.0", "jakarta")))
                if x[0].key == EHCACHE]
    assert [x for x in jakarta_libraries_below(_profile(G(f"{EHCACHE}:3.8.1"))) if x[0].key == EHCACHE][0][1] == "3.8.1"


def test_entries_without_classifier_never_get_one(kit):
    kit.pom(deps=[G("org.quartz-scheduler:quartz:2.3.2"), G("org.jooq:jooq:3.14.16")])
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None)
    assert kit.dep("org.quartz-scheduler:quartz").version == "2.5.2"
    assert kit.dep("org.jooq:jooq").version == "3.21.5"
    assert kit.dep("org.quartz-scheduler:quartz").classifier is None and kit.dep("org.jooq:jooq").classifier is None


def test_displaytag_old_coordinates_relocated(kit):
    kit.pom(deps=[G("displaytag:displaytag:1.2")])
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None)
    assert kit.dep("displaytag:displaytag") is None
    assert kit.dep("com.github.hazendaz:displaytag").version == "3.8.0"
    assert h.notes == ["displaytag:displaytag 1.2 → com.github.hazendaz:displaytag 3.8.0 (coordinates changed; "
                       "jakarta release since 3.0.0, requires Java 11) in pom.xml"]


def test_jersey_family_bumped_together_and_jakarta_line_kept(kit):
    kit.pom(deps=[G("org.glassfish.jersey.core:jersey-server:2.35"), G("org.glassfish.jersey.core:jersey-client:2.35"),
                  G("org.glassfish.jersey.containers:jersey-container-servlet:3.1.5")])
    assert kit.handler("jakarta-library-floor").apply(kit.ws, None, None, None)
    assert kit.dep("org.glassfish.jersey.core:jersey-server").version == "4.0.2"
    assert kit.dep("org.glassfish.jersey.core:jersey-client").version == "4.0.2"
    assert kit.dep("org.glassfish.jersey.containers:jersey-container-servlet").version == "3.1.5"   # ≥ 3.0.0


def test_floor_triggers_on_new_library_names(cfg):
    from sbmig.triage import HandlerRegistry

    h = HandlerRegistry.default(None).get("jakarta-library-floor")
    c = cluster_errors([err(message="cannot access org.ehcache.config.xml.XmlConfiguration",
                            raw="class file for javax.xml.bind.JAXBException not found",
                            file="src/main/java/com/acme/CacheConfig.java")])[0]
    assert h.matches(c, _profile(G(f"{EHCACHE}:3.12.0")), cfg)
    assert not h.matches(c, _profile(_g(f"{EHCACHE}:3.12.0", "jakarta")), cfg)


# ---------------------------------------------------------------------------------------------------------------------
# relocations.yml
# ---------------------------------------------------------------------------------------------------------------------
NEW_ROWS = {
    "org.springframework.session:spring-session": ("org.springframework.session:spring-session-core", "4.1.0"),
    "org.hibernate:hibernate-envers": ("org.hibernate.orm:hibernate-envers", "7.4.1.Final"),
    "org.hibernate.orm:hibernate-jpamodelgen": ("org.hibernate.orm:hibernate-processor", "7.4.1.Final"),
    "com.zaxxer:HikariCP-java7": ("com.zaxxer:HikariCP", "7.0.2"),
    "com.zaxxer:HikariCP-java6": ("com.zaxxer:HikariCP", "7.0.2"),
    "c3p0:c3p0": ("com.mchange:c3p0", "0.14.2"),
    "com.oracle:ojdbc6": ("com.oracle.database.jdbc:ojdbc11", "23.26.2.0.0"),
    "com.oracle:ojdbc7": ("com.oracle.database.jdbc:ojdbc11", "23.26.2.0.0"),
    "com.oracle.jdbc:ojdbc8": ("com.oracle.database.jdbc:ojdbc11", "23.26.2.0.0"),
    "com.oracle.ojdbc:ojdbc8": ("com.oracle.database.jdbc:ojdbc11", "23.26.2.0.0"),
    "com.oracle.database.jdbc:ojdbc6": ("com.oracle.database.jdbc:ojdbc11", "23.26.2.0.0"),
    "com.microsoft.sqlserver:sqljdbc4": ("com.microsoft.sqlserver:mssql-jdbc", "13.4.0.jre11"),
    "com.microsoft.sqlserver:sqljdbc42": ("com.microsoft.sqlserver:mssql-jdbc", "13.4.0.jre11"),
    "cubrid:cubrid-jdbc": ("org.cubrid:cubrid-jdbc", "11.3.2.0053"),
    "com.ibm.db2.jcc:db2jcc": ("com.ibm.db2:jcc", "12.1.4.0"),
    "com.ibm.db2.jcc:db2jcc4": ("com.ibm.db2:jcc", "12.1.4.0"),
    "dom4j:dom4j": ("org.dom4j:dom4j", "2.2.0"),
    "com.lowagie:itext": ("com.github.librepdf:openpdf", "2.4.0"),
    "org.bouncycastle:bcprov-jdk15on": ("org.bouncycastle:bcprov-jdk18on", "1.86"),
    "org.bouncycastle:bcpkix-jdk15on": ("org.bouncycastle:bcpkix-jdk18on", "1.86"),
    "org.bouncycastle:bcprov-jdk16": ("org.bouncycastle:bcprov-jdk18on", "1.86"),
    "org.hamcrest:hamcrest-all": ("org.hamcrest:hamcrest", "3.0"),
}


def test_relocation_rows_and_boot41_hibernate_versions():
    rows = {r.from_: r for r in relocation_table()}
    assert len(rows) == len(relocation_table())                                       # no duplicate `from`
    for frm, (to, version) in NEW_ROWS.items():
        r = rows[frm]
        assert (r.to, r.version) == (to, version), frm
        assert r.packages == [] and r.min_target_boot is None, frm                    # reactive only
    assert all(r.version is None or isinstance(r.version, str) for r in rows.values())   # no YAML floats
    assert rows["org.hamcrest:hamcrest-all"].scope == "test"
    assert rows["org.hibernate:hibernate-core"].version == "7.4.1.Final"
    assert rows["org.hibernate:hibernate-jpamodelgen"].version == "7.4.1.Final"
    assert rows["org.hibernate:hibernate-validator"].version == "9.1.0.Final"
    # opt-in / advisory-only on purpose
    assert not any(k.startswith(("com.tmax", "com.querydsl:", "org.apache.shiro:")) for k in rows)


def test_oracle_old_coordinates_relocated_on_the_resolution_error(kit, cfg):
    kit.pom(deps=[G("com.oracle:ojdbc6:11.2.0.3"), G("org.springframework.boot:spring-boot-starter-web")])
    h = kit.handler("dependency-relocation")
    prof = _profile(G("com.oracle:ojdbc6:11.2.0.3"))
    assert h.apply(kit.ws, None, prof, cfg) == []                                     # never pre-emptive
    c = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "Could not find artifact com.oracle:ojdbc6:jar:11.2.0.3",
                            artifact="com.oracle:ojdbc6:11.2.0.3")])[0]
    assert h.matches(c, prof, cfg)
    assert h.apply(kit.ws, c, prof, cfg)
    assert kit.dep("com.oracle:ojdbc6") is None
    assert kit.dep("com.oracle.database.jdbc:ojdbc11").version == "23.26.2.0.0"


def test_spring_session_monolith_never_keeps_its_1x_version(kit, cfg):
    kit.pom(deps=[G("org.springframework.session:spring-session:1.3.5.RELEASE")])
    h = kit.handler("dependency-relocation")
    c = cluster_errors([err(ErrorKind.DEPENDENCY_RESOLUTION, "x",
                            artifact="org.springframework.session:spring-session:1.3.5.RELEASE")])[0]
    assert h.apply(kit.ws, c, _profile(), cfg)
    assert kit.dep("org.springframework.session:spring-session-core").version == "4.1.0"
    kit.pom(deps=[G("org.springframework.session:spring-session")])
    assert h.apply(kit.ws, c, _profile(), cfg)
    assert kit.dep("org.springframework.session:spring-session-core").version is None      # stays BOM-managed


# ---------------------------------------------------------------------------------------------------------------------
# known_packages.yml + versions.yml
# ---------------------------------------------------------------------------------------------------------------------
def _row(pkg: str):
    return next(r for r in known_package_table() if r.package == pkg)


def test_known_package_rows():
    pkgs = [r.package for r in known_package_table()]
    assert len(pkgs) == len(set(pkgs))
    assert _row("org.apache.commons.fileupload2.jakarta.servlet6").artifact == \
        "org.apache.commons:commons-fileupload2-jakarta-servlet6"
    assert _row("org.apache.commons.fileupload2.core").version == "2.0.0-M5"
    assert _row("org.thymeleaf.spring6").managed and _row("org.springframework.hateoas").managed
    assert (_row("org.dom4j").artifact, _row("org.dom4j").version, _row("org.dom4j").managed) == \
        ("org.dom4j:dom4j", "2.2.0", False)
    assert _row("javassist").artifact == "org.javassist:javassist"
    assert _row("com.google.common").version == "33.7.2-jre"
    assert _row("org.apache.commons.mail2.jakarta").artifact == "org.apache.commons:commons-email2-jakarta"
    assert _row("org.apache.commons.collections4").managed is False
    assert _row("org.apache.commons.dbcp2").managed is True
    assert not any(r.package.startswith("org.ehcache") for r in known_package_table())   # needs a classifier
    # Boot 4.1 fallback versions
    assert _row("org.junit.jupiter").version == "6.0.3" and _row("org.mockito").version == "5.23.0"
    assert _row("org.apache.commons.lang3").version == "3.20.0"
    assert _row("com.fasterxml.jackson").version == "2.21.4"


def test_known_package_longest_prefix_keeps_existing_rows(kit):
    """`jakarta.servlet.jsp` (new, jsp-api) beats `jakarta.servlet` (servlet-api); `jakarta.servlet.jsp.jstl` beats both."""
    kit.pom()
    h = kit.handler("add-known-dependency")
    c = cluster_errors([err(file="src/main/java/Tag.java", message="package jakarta.servlet.jsp.tagext does not exist")])[0]
    assert h.apply(kit.ws, c, make_profile(), None)
    d = kit.dep("jakarta.servlet.jsp:jakarta.servlet.jsp-api")
    assert d is not None and d.scope == "provided" and d.version == "4.0.0"
    assert kit.dep("jakarta.servlet:jakarta.servlet-api") is None
    c2 = cluster_errors([err(file="src/main/java/F.java", message="package jakarta.servlet.http does not exist")])[0]
    assert h.apply(kit.ws, c2, make_profile(), None)
    assert kit.dep("jakarta.servlet:jakarta.servlet-api") is not None


def test_collections4_package_after_the_recipe(kit):
    kit.pom()
    h = kit.handler("add-known-dependency")
    c = cluster_errors([err(file="src/main/java/A.java",
                            message="package org.apache.commons.collections4 does not exist")])[0]
    assert h.apply(kit.ws, c, make_profile(), None)
    assert kit.dep("org.apache.commons:commons-collections4").version == "4.5.0"


def test_boot41_fallback_versions():
    """versions.yml (the config's in-house optional artifacts may still pin their own versions on top)."""
    kv = known_versions()
    for key, v in {"com.mysql:mysql-connector-j": "9.7.0", "com.h2database:h2": "2.4.240",
                   "org.postgresql:postgresql": "42.7.11", "org.apache.commons:commons-lang3": "3.20.0",
                   "com.fasterxml.jackson.core:jackson-databind": "2.21.4", "org.junit.jupiter:junit-jupiter": "6.0.3",
                   "org.mockito:mockito-core": "5.23.0", "org.hamcrest:hamcrest": "3.0",
                   "commons-io:commons-io": "2.22.0", "org.hibernate.orm:hibernate-core": "7.4.1.Final",
                   "org.hibernate.validator:hibernate-validator": "9.1.0.Final",
                   "org.glassfish.jaxb:jaxb-runtime": "4.0.9", "jakarta.xml.bind:jakarta.xml.bind-api": "4.0.5"}.items():
        assert kv[key] == v, key
