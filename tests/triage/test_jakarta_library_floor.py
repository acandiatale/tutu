"""X1b `jakarta-library-floor` (knowledge/libraries/jakarta_libraries.yml) + pre-emptive `dependency-relocation`
for the javax API rows (fourth company project, 2026-10-22: QLRM's JpaResultMapper takes javax.persistence.Query)."""
from __future__ import annotations

from sbmig.core.jakarta_libraries import jakarta_library_for
from sbmig.core.models import GAV, BuildError, ErrorKind, PomInfo
from sbmig.triage import HandlerRegistry, cluster_errors

from .conftest import make_profile

G = GAV.parse
QLRM = "org.qlrm:qlrm"
CONVERT = "incompatible types: jakarta.persistence.Query cannot be converted to javax.persistence.Query"
NO_SUITABLE = "no suitable method found for list(jakarta.persistence.Query,Class<Foo>)"


def _cluster(message: str):
    cl = cluster_errors([BuildError(kind=ErrorKind.COMPILE, message=message, raw=message,
                                    file="src/main/java/com/acme/Dao.java", line=7)])
    assert len(cl) == 1
    return cl[0]


def _qlrm_profile(version):
    deps = [G(f"{QLRM}:{version}")] if version else []
    return make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo", is_root=True, dependencies=deps)])


def test_table_entry_is_the_one_we_test_against():
    lib = jakarta_library_for(QLRM)
    assert lib is not None and lib.jakarta_since == "4.0.1" and lib.version == "4.2.1" and lib.requires_java == 17


def test_direct_dependency_bumped_with_flag_and_note(kit):
    kit.pom(deps=[G(f"{QLRM}:2.1.1"), G("org.projectlombok:lombok:1.18.38")])
    h = kit.handler("jakarta-library-floor")
    assert [c.path for c in h.apply(kit.ws, None, None, None)] == ["pom.xml"]
    assert kit.dep(QLRM).version == "4.2.1"
    assert kit.dep("org.projectlombok:lombok").version == "1.18.38"
    assert h.flags == ["jakarta-library-bumped"]
    assert len(h.notes) == 1
    note = h.notes[0]
    assert note.startswith("org.qlrm:qlrm 2.1.1 → 4.2.1 (QLRM (JpaResultMapper") and note.endswith(
        "; requires Java 17) in pom.xml")
    assert h.apply(kit.ws, None, None, None) == []                          # idempotent
    assert h.flags == [] and h.notes == []


def test_managed_version_bumped(kit):
    kit.pom(managed=[G(f"{QLRM}:3.0.4")], deps=[G(QLRM)])
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None)
    assert kit.dep(QLRM, managed=True).version == "4.2.1"
    assert "org.qlrm:qlrm 3.0.4 → 4.2.1" in h.notes[0]


def test_property_version_bumped_through_the_property(kit):
    kit.pom(deps=[G(QLRM + ":${qlrm.version}")], props={"qlrm.version": "1.7.1"})
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None)
    assert kit.read().get_property("qlrm.version") == "4.2.1"
    assert kit.dep(QLRM).version == "${qlrm.version}"


def test_jakarta_releases_untouched(kit):
    for v in ("4.0.1", "4.2.1"):
        kit.pom(deps=[G(f"{QLRM}:{v}")])
        h = kit.handler("jakarta-library-floor")
        assert h.apply(kit.ws, None, None, None) == []
        assert kit.dep(QLRM).version == v


def test_property_already_on_jakarta_untouched(kit):
    kit.pom(deps=[G(QLRM + ":${qlrm.version}")], props={"qlrm.version": "4.0.1"})
    assert kit.handler("jakarta-library-floor").apply(kit.ws, None, None, None) == []
    assert kit.read().get_property("qlrm.version") == "4.0.1"


def test_unversioned_use_follows_the_managing_pom(fake_kit):
    fake_kit.pom(managed=[G(f"{QLRM}:2.1.1")], packaging="pom", artifact="parent")
    fake_kit.pom("app/pom.xml", deps=[G(QLRM)], artifact="app")
    h = fake_kit.handler("jakarta-library-floor")
    assert [c.path for c in h.apply(fake_kit.ws, None, None, None)] == ["pom.xml"]
    assert fake_kit.dep(QLRM, managed=True).version == "4.2.1"
    assert fake_kit.dep(QLRM, "app/pom.xml").version is None             # stays managed


def test_plugin_dependency_bumped_by_text_but_jakarta_one_kept(fake_kit):
    extra = ("\n        <plugin>\n          <groupId>com.example</groupId>\n          <artifactId>gen-plugin</artifactId>\n"
             "          <dependencies>\n            <dependency>\n              <groupId>org.qlrm</groupId>\n"
             "              <artifactId>qlrm</artifactId>\n              <version>2.1.1</version>\n"
             "            </dependency>\n          </dependencies>\n        </plugin>")
    fake_kit.pom(extra_build=extra)
    h = fake_kit.handler("jakarta-library-floor")
    assert h.apply(fake_kit.ws, None, None, None)
    assert "<version>4.2.1</version>" in fake_kit.text() and "2.1.1" not in fake_kit.text()
    assert "org.qlrm:qlrm 2.1.1 → 4.2.1" in h.notes[0]
    fake_kit.pom(extra_build=extra.replace("2.1.1", "4.0.1"))
    assert h.apply(fake_kit.ws, None, None, None) == []
    assert "<version>4.0.1</version>" in fake_kit.text()


OLD = "ch.simas.qlrm:qlrm"
OLD_NOTE = ("ch.simas.qlrm:qlrm 1.7.1 → org.qlrm:qlrm 4.2.1 (coordinates changed; jakarta release since 4.0.1, "
            "requires Java 17) in pom.xml")


def test_old_coordinates_relocated_direct(kit):
    kit.pom(deps=[G(f"{OLD}:1.7.1")])
    h = kit.handler("jakarta-library-floor")
    assert [c.path for c in h.apply(kit.ws, None, None, None)] == ["pom.xml"]
    assert kit.dep(OLD) is None
    assert kit.dep(QLRM).version == "4.2.1"
    assert h.flags == ["jakarta-library-bumped"] and h.notes == [OLD_NOTE]
    assert h.apply(kit.ws, None, None, None) == []


def test_old_coordinates_relocated_managed(kit):
    kit.pom(managed=[G(f"{OLD}:1.7.1")], deps=[G(OLD)])
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None)
    assert kit.dep(OLD, managed=True) is None and kit.dep(OLD) is None
    assert kit.dep(QLRM, managed=True).version == "4.2.1"
    assert h.notes == [OLD_NOTE]


def test_old_coordinates_property_replaced_by_literal(kit):
    kit.pom(deps=[G(OLD + ":${qlrm.version}")], props={"qlrm.version": "1.7.1"})
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None)
    assert kit.dep(OLD) is None
    assert kit.dep(QLRM).version == "4.2.1"
    assert h.notes == [OLD_NOTE]


def test_old_coordinates_unversioned_child_stays_managed(fake_kit):
    fake_kit.pom(managed=[G(f"{OLD}:1.7.1")], packaging="pom", artifact="parent")
    fake_kit.pom("app/pom.xml", deps=[G(OLD)], artifact="app")
    assert fake_kit.handler("jakarta-library-floor").apply(fake_kit.ws, None, None, None)
    assert fake_kit.dep(QLRM, managed=True).version == "4.2.1"
    assert fake_kit.dep(QLRM, "app/pom.xml").version is None


def test_matches_old_coordinates(cfg):
    h = HandlerRegistry.default(None).get("jakarta-library-floor")
    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo", is_root=True,
                                      dependencies=[G(f"{OLD}:1.7.1")])])
    assert h.matches(_cluster(CONVERT), prof, cfg)


def test_matches_needs_a_library_below_its_jakarta_release(cfg):
    h = HandlerRegistry.default(None).get("jakarta-library-floor")
    c = _cluster(CONVERT)
    assert h.matches(c, _qlrm_profile("2.1.1"), cfg)
    assert h.matches(c, _qlrm_profile("${qlrm.version}"), cfg)            # unresolvable → assume old
    assert not h.matches(c, _qlrm_profile("4.0.1"), cfg)
    assert not h.matches(c, _qlrm_profile(None), cfg)                     # no qlrm declared
    assert h.matches(_cluster(NO_SUITABLE), _qlrm_profile("2.1.1"), cfg)
    assert h.matches(_cluster("cannot access org.qlrm.mapper.JpaResultMapper"), _qlrm_profile("2.1.1"), cfg)
    assert not h.matches(_cluster("cannot find symbol"), _qlrm_profile("2.1.1"), cfg)
    managed_prop = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo", is_root=True,
                                              properties={"qlrm.version": "4.2.1"},
                                              dependency_management=[G(QLRM + ":${qlrm.version}")])])
    assert not h.matches(c, managed_prop, cfg)                            # local ${prop} resolved


def test_registry_precedence(cfg):
    reg = HandlerRegistry.default(None)
    ids = [h.id for h in reg.matching(_cluster(CONVERT), _qlrm_profile("2.1.1"), cfg)]
    assert "jakarta-library-floor" in ids
    order = reg.ids()
    assert order.index("dependency-relocation") + 1 == order.index("jakarta-library-floor")
    assert order.index("jakarta-library-floor") < order.index("querydsl-jakarta-classifier")
    assert reg.get("jakarta-library-floor").preemptive_phase.value == "spring"


# ---------------------------------------------------------------------------------------------------------------------
# dependency-relocation pre-emptive: only the javax → jakarta API rows
# ---------------------------------------------------------------------------------------------------------------------
def test_relocation_preemptive_moves_javax_api_only(kit, cfg):
    kit.pom(deps=[G("javax.persistence:javax.persistence-api:2.2"), G("org.springframework.boot:spring-boot-starter-web"),
                  G("javax.validation:validation-api")])
    h = kit.handler("dependency-relocation")
    assert h.preemptive_phase.value == "spring"
    prof = make_profile(poms=[PomInfo(path="pom.xml", artifact_id="demo", is_root=True)])
    assert [c.path for c in h.apply(kit.ws, None, prof, cfg)] == ["pom.xml"]
    assert kit.dep("javax.persistence:javax.persistence-api") is None
    assert kit.dep("jakarta.persistence:jakarta.persistence-api").version == "3.2.0"      # old one was explicit
    assert kit.dep("jakarta.validation:jakarta.validation-api") is not None
    assert kit.dep("org.springframework.boot:spring-boot-starter-web") is not None       # never renamed pre-emptively
    assert kit.dep("org.springframework.boot:spring-boot-starter-webmvc") is None
    assert h.apply(kit.ws, None, prof, cfg) == []


def test_relocation_preemptive_leaves_servlet_and_mysql_alone(fake_kit, cfg):
    fake_kit.pom(deps=[G("javax.servlet:javax.servlet-api:4.0.1"), G("mysql:mysql-connector-java:8.0.33")])
    assert fake_kit.handler("dependency-relocation").apply(fake_kit.ws, None, None, cfg) == []


def test_unversioned_parent_managed_declaration_is_not_pinned(kit):
    """An unversioned qlrm (version from the company parent / a BOM) is never pinned over the parent."""
    kit.pom(deps=[G("org.qlrm:qlrm")])
    h = kit.handler("jakarta-library-floor")
    assert h.apply(kit.ws, None, None, None) == []
    assert kit.dep("org.qlrm:qlrm").version is None
    # …but an unversioned old coordinate is still relocated (the old coordinates cannot be right)
    kit.pom("old/pom.xml", deps=[G("ch.simas.qlrm:qlrm")])
    changes = h.apply(kit.ws, None, None, None)
    assert [c.path for c in changes] == ["old/pom.xml"]
    assert kit.dep("org.qlrm:qlrm", "old/pom.xml").version == "4.2.1"
