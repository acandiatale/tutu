"""Korean presentation of the remediation's English sentences (sbmig/remediations/i18n.py)."""
from __future__ import annotations

import re

import pytest

from sbmig.remediations import apply as apply_mod
from sbmig.remediations import i18n
from sbmig.remediations import maven as maven_mod
from sbmig.remediations import planner as planner_mod
from sbmig.remediations.models import ModuleResolution, PackagedLib, Proposal, ResolvedDep, ResolvedModel

HANGUL = re.compile(r"[가-힣]")
GA, GA2 = "org.example:lib", "com.example.inhouse:inhouse-core"

# (rule key, English sentence as the remediation code writes it, identifiers that must survive the translation)
SAMPLES: list[tuple[str, str, list[str]]] = [
    ("not_changed_auto", "plugin: not changed automatically", []),
    ("policy_deny", f"{GA} matches policy.deny", [GA]),
    ("unknown_origin", "unknown origin 'weird'", ["weird"]),
    ("no_location", "no version location in the project poms", []),
    ("not_plain_text", "app-domain/pom.xml:12 version text '${v}' is not a plain version", ["app-domain/pom.xml 12행", "${v}"]),
    ("parent_not_plain", "pom.xml:7 parent version '${p}' is not plain", ["pom.xml 7행", "${p}"]),
    ("resolved_not_plain", "resolved version '1.0-${x}' is not a plain version", ["1.0-${x}"]),
    ("stale_gone", "pom.xml:30 no longer exists or cannot be read", ["pom.xml 30행"]),
    ("stale_count", "pom.xml:30 contains '1.0' 2 time(s), expected exactly once (file changed?)", ["pom.xml 30행", "1.0"]),
    ("different_texts", "pom.xml:30: inventory names different version texts for one line", ["pom.xml 30행"]),
    ("pin_not_newer", "policy pin 1.2.3 is not newer than the current 1.2.4", ["1.2.3", "1.2.4"]),
    ("pin_denied", "policy pin 1.2.3 is also listed in policy.deny_versions", ["1.2.3"]),
    ("pin_major", "policy pin 2.0.0 is a major change and policy.allow_major is false", ["2.0.0"]),
    ("pin_for_not_newer", "policy pin 3.1 for org.example:parent is not newer than 3.2", ["3.1", "org.example:parent", "3.2"]),
    ("pin_for_major", "policy pin 4.0 for org.example:parent is a major change and policy.allow_major is false",
     ["4.0", "org.example:parent"]),
    ("pin_below_hint", "policy pin 1.2.3 is below the IVAS patched hint 1.2.5; 1.3.0", ["1.2.3", "1.2.5", "1.3.0"]),
    ("no_hint", "no patched version known (IVAS gave none) and no policy pin", []),
    ("hint_not_plain", "IVAS patched version hint(s) '[2.0,3.0)' are not plain versions; not chosen automatically",
     ["[2.0,3.0)"]),
    ("only_major", "only major upgrades are available (3.0.1, 3.1.0) and policy.allow_major is false", ["3.0.1", "3.1.0"]),
    ("repo_listed", ("repository listed 1.0, 1.1, 1.2 (+3 more); excluded: 1.1 (prerelease), 1.2 (< patched hint 1.3, 2.0; "
     "1.4); needed > 1.0 and ≥ patched hint 1.3, 2.0; 1.4; the hinted 1.3, 1.4 are not in the repository"),
     ["1.0", "1.1", "1.2", "1.3", "2.0", "1.4"]),
    ("no_evidence", "the repository gave no evidence for 1.2.6", ["1.2.6"]),
    ("major_not_hinted", ("only other major versions are available (2.0.0, 20030203.000550 (date-stamped)) and no IVAS "
     "patched hint names that major (hint 1.2.5)"), ["2.0.0", "20030203.000550", "1.2.5"]),
    ("dated_current", ("the current version 20030203.000550 is date-stamped and cannot be compared with the IVAS patched "
     "hint 2.7; not chosen automatically"), ["20030203.000550", "2.7"]),
    ("meets_hint", ("IVAS still reports 2.15.0 although it meets the patched hint 2.12.7.1, 2.13.4.2; 2.14.0: ask the IVAS "
     "team to check the finding"), ["2.15.0", "2.12.7.1, 2.13.4.2; 2.14.0"]),
    ("shared_property", ("version property ${jackson.version} (pom.xml) also sets the version of com.example:core, "
     "com.example:annotations; 2.12.7.1 is not in the repository for com.example:core: the property is not changed "
     "automatically"), ["${jackson.version}", "pom.xml", "com.example:core, com.example:annotations", "2.12.7.1"]),
    ("profile_only", (f"{GA} is declared in app-back/pom.xml only inside profile(s) legacy, jdk8, which are not active by "
     "default; not changed automatically"), [GA, "app-back/pom.xml", "legacy, jdk8"]),
    ("declared_twice", f"{GA} is declared 2 times in one <dependencies> block of app-back/pom.xml; not changed automatically",
     [GA, "app-back/pom.xml", "2번"]),
    ("structure_unparsable", "app/pom.xml: XML could not be parsed for the structure check (XMLSyntaxError)",
     ["app/pom.xml", "XMLSyntaxError"]),
    ("structure_changed", "app/pom.xml: XML element structure changed", ["app/pom.xml"]),
    ("structure_text_between", "app/pom.xml: text between elements changed after <dependency>", ["app/pom.xml", "dependency"]),
    ("structure_text_of", "app/pom.xml: text of <groupId> changed (only <version> / property values may change)",
     ["app/pom.xml", "groupId"]),
    ("parent_pin_no_text", "policy pins the BOM org.example:bom at 5.0, but its version text is not in the project poms",
     ["org.example:bom", "5.0"]),
    ("parent_denied", "the parent org.example:parent matches policy.deny", ["org.example:parent"]),
    ("root_entries_many", (f"the root pom.xml already has <dependencyManagement> entries for {GA} (top level, profile dev); "
     "a second entry is never added"), [GA, "dev"]),
    ("root_entry_mismatch", (f"the root pom.xml already has a <dependencyManagement> entry for {GA} whose version "
     "(${lib.version}) is not the resolved 1.0 as plain text; a second entry is never added"), [GA, "${lib.version}", "1.0"]),
    ("variant", (f"transitive dependency {GA} resolves as a non-jar type or with a classifier (type/classifier variant "
     "(jar/tests)); a plain override entry would not govern it"), [GA, "jar/tests"]),
    ("root_unreadable", "cannot place an override entry safely: the root pom.xml is not readable", []),
    ("cannot_place", "cannot place an override entry safely: pom.xml: anchor line 12 is not </dependencies>",
     ["pom.xml"]),
    ("module_pom_unreadable_override",
     "cannot place an override entry safely: the pom.xml of module app-back is not readable", ["app-back"]),
    ("place_not_project", "cannot place an override entry safely: app-back/pom.xml: the root element is not <project>",
     ["app-back/pom.xml"]),
    ("place_two_sections",
     "cannot place an override entry safely: app-back/pom.xml: more than one <dependencyManagement> section",
     ["app-back/pom.xml"]),
    ("place_dependencies_count", ("cannot place an override entry safely: app-back/pom.xml: <dependencyManagement> does not "
     "hold exactly one <dependencies> section"), ["app-back/pom.xml"]),
    ("place_close_dependencies", ("cannot place an override entry safely: app-back/pom.xml:40: the closing </dependencies> "
     "of <dependencyManagement> is not on its own line"), ["app-back/pom.xml 40행"]),
    ("place_tag_line", "cannot place an override entry safely: app-back/pom.xml:12: <dependencies> does not start its own line",
     ["app-back/pom.xml 12행", "dependencies"]),
    ("place_close_project",
     "cannot place an override entry safely: app-back/pom.xml:88: the closing </project> is not on its own line",
     ["app-back/pom.xml 88행"]),
    ("override_no_effect", ("the override entries in app-back/pom.xml, app-front/pom.xml did not change the resolved version "
     f"of {GA} (1.0 stays): a dependency declared with a version, here or in a parent, wins over <dependencyManagement>; "
     "the entries were removed again"), ["app-back/pom.xml, app-front/pom.xml", GA, "1.0"]),
    ("override_no_effect_earlier", (f"the override entry for {GA} → 1.1 in app-back/pom.xml had no effect in an earlier round "
     "of this run; not proposed again"), [GA, "1.1", "app-back/pom.xml"]),
    ("info_outside_entry", (f"info: {GA} in module app-x still resolves at 1.0 — outside the modules the inserted entry in "
     "app-back/pom.xml governs (p-1-2); left for the next round"), [GA, "app-x", "1.0", "app-back/pom.xml", "p-1-2"]),
    ("ineffective_not_removed", ("the ineffective override entr(ies) p-1-2, p-1-3 were not removed and rebuilt: a stop was "
     "requested or the time budget is used up"), ["p-1-2, p-1-3"]),
    ("after_removing_ineffective", "after removing the ineffective override entr(ies) p-1-2", ["p-1-2"]),
    ("rebuild_without_failed", "the candidate without p-1-2 could not be rebuilt: p-1-1: pom.xml:12: anchor changed",
     ["p-1-2", "p-1-1"]),
    ("override_twice", f"cannot place an override entry safely: pom.xml: two override entries for {GA}:jar:jakarta",
     ["pom.xml", f"{GA}:jar:jakarta"]),
    ("override_exists", (f"cannot place an override entry safely: pom.xml: already has a <dependencyManagement> entry for "
     f"{GA}:test-jar; a second entry is never added"), ["pom.xml", f"{GA}:test-jar"]),
    ("override_key_not_plain", ("cannot place an override entry safely: pom.xml:58: override type / classifier 'jar' / "
     "'${c}' is not plain coordinate text"), ["pom.xml 58행", "'jar'", "'${c}'"]),
    ("override_dtd", "cannot place an override entry safely: app/pom.xml declares a DTD / entities; not edited",
     ["app/pom.xml"]),
    ("variant_key_not_plain", (f"managed version {GA} resolves as test-jar/${{x}}, which cannot be written as plain "
     "override coordinates"), [GA, "test-jar", "${x}"]),
    ("variant_note", ("the entry names type test-jar and classifier tests like the resolved dependency: an entry without "
     "it would not govern that dependency"), ["test-jar", "tests"]),
    ("override_count", f"pom.xml: expected exactly one inserted <dependency> {GA}:jar:jakarta, found 2",
     ["pom.xml", f"{GA}:jar:jakarta", "2"]),
    ("override_shape", (f"pom.xml: inserted <dependency> {GA}:test-jar:tests is not exactly groupId / artifactId / version "
     "1.1 / type test-jar / classifier tests"), ["pom.xml", f"{GA}:test-jar:tests", "1.1", "test-jar", "tests"]),
    ("override_extra", "pom.xml: the inserted <dependencyManagement> holds more than the planned override entries",
     ["pom.xml"]),
    ("not_inheriting_root", ("module(s) app-boot, app-domain do not inherit from the root pom.xml: an override entry there "
     "would not reach them"), ["app-boot", "app-domain"]),
    ("declared_wins", (f"{GA} is declared with a version in app-domain/pom.xml (1.0): a declared version wins over "
     "<dependencyManagement>, so an override entry would not change it"), [GA, "app-domain/pom.xml", "1.0"]),
    ("inventory_incomplete_plan", "inventory incomplete: nothing planned (module app: pom not readable (missing))", ["app"]),
    ("inventory_incomplete", "inventory incomplete", []),
    ("inventory_incomplete_msg", "inventory incomplete: Maven resolution is incomplete", []),
    ("inhouse_self", f"{GA2} is an in-house artifact: its owner publishes the fixed version", [GA2]),
    ("inhouse_via", (f"brought in by the in-house artifact(s) {GA2}, com.example.inhouse:comp-jar: only their owner can "
     "change it"), [GA2, "com.example.inhouse:comp-jar"]),
    ("not_applied", "proposal p-1-2 not applied: pom.xml:12: anchor line changed (expected '<x>')", ["p-1-2", "pom.xml:12"]),
    ("prop_governs", "property ${lib.version} may govern other artifacts", ["${lib.version}"]),
    ("also_resolved_edit", ("also resolved in app-domain, app-store through another origin; this edit may not govern them "
     "— whatever stays at 1.0 is planned in the next round"), ["app-domain", "app-store", "1.0"]),
    ("raises_parent", (f"raises the parent org.example:parent 3.0 → 3.1 (server pin) for {GA}:1.0; the versions it manages "
     "may change too"), ["org.example:parent", "3.0", "3.1", f"{GA}:1.0"]),
    ("uses_existing", "uses the existing <dependencyManagement> entry of the root pom.xml", []),
    ("override_transitive", (f"adds a <dependencyManagement> entry {GA}:1.1 to the root pom.xml: pins the transitive "
     "dependency brought by org.example:a:1 → org.example:b:2 (1.0 → 1.1)"), [f"{GA}:1.1", "org.example:a:1", "org.example:b:2"]),
    ("override_managed", (f"adds a <dependencyManagement> entry {GA}:1.1 to the root pom.xml: overrides the version managed "
     "by org.example:bom:5 (1.0 → 1.1)"), [f"{GA}:1.1", "org.example:bom:5"]),
    ("override_warning", planner_mod.OVERRIDE_WARNING, []),
    ("merge_covers", f"one edit also covers {GA}-extra:1.0 (same version line)", [f"{GA}-extra:1.0"]),
    ("merge_target", f"target 1.2 chosen for {GA} (highest of the merged targets)", ["1.2", GA]),
    ("managed_by_direct", "managed by org.example:parent:3.0, which the project poms do not declare directly",
     ["org.example:parent:3.0"]),
    ("managed_by", "managed by org.example:bom:5.0", ["org.example:bom:5.0"]),
    ("ambiguous", "ambiguous provenance: version may come from parent org.example:p:1, BOM org.example:b:2",
     ["org.example:p:1", "org.example:b:2"]),
    ("no_manager", "no version declared in the project and no parent / BOM to manage it", []),
    ("text_not_located", f"version text of {GA} in app/pom.xml could not be located on one line", [GA, "app/pom.xml"]),
    ("declares_vs", f"app/pom.xml declares {GA} 1.0, Maven resolves 1.1", ["app/pom.xml", GA, "1.0", "1.1"]),
    ("prop_not_located", "property lib.version in pom.xml could not be located on one line", ["lib.version", "pom.xml"]),
    ("prop_value_vs", "property lib.version in pom.xml is 1.0, Maven resolves 1.1", ["lib.version", "1.0", "1.1"]),
    ("prop_not_defined", "version property ${a} → ${b} is not defined in the project poms", ["${a}", "${b}"]),
    ("builtin", f"version of {GA} comes from the built-in property ${{project.version}}", [GA, "${project.version}"]),
    ("expression", f"version of {GA} is the expression '${{a}}-${{b}}'; manual review", [GA, "${a}-${b}"]),
    ("also_resolved_inv", "also resolved in app-a, app-b (transitive via org.example:x:1 → org.example:y:2)",
     ["app-a", "app-b", "org.example:x:1", "org.example:y:2"]),
    ("variant_paren", "type/classifier variant (test-jar/tests)", ["test-jar/tests"]),
    ("variant_in", "type/classifier variant in app-a, app-b", ["app-a", "app-b"]),
    ("module_pom_unreadable", "module app-x: pom not readable (missing)", ["app-x"]),
    ("vendored_dep", "file in the tree (not a Maven dependency); not changed automatically", []),
    ("vendored_unresolved", "file in the tree (not resolved by Maven); not changed automatically", []),
    ("file_component", "file component in the tree; not changed automatically", []),
    ("kind_unsupported", "finding kind sw is not supported by the remediation", ["sw"]),
    ("ident_not_ga", "identifier 'jquery.js' is not group:artifact", ["jquery.js"]),
    ("plugin", ("build plugin or plugin dependency (declared 3.1.0); plugin versions are declared, not resolved; not changed "
     "automatically"), ["3.1.0"]),
    ("ivas_not_resolved", f"IVAS reports {GA} 1.0, Maven does not resolve {GA} in any module (resolution incomplete)",
     [GA, "1.0"]),
    ("ivas_no_version", f"IVAS reports {GA} without a version; Maven resolves 1.0, 1.1", [GA, "1.0", "1.1"]),
    ("ivas_vs_maven", "IVAS reports 1.0, Maven resolves 1.1", ["1.0", "1.1"]),
    ("cannot_read", "cannot read app/pom.xml: PermissionError", ["app/pom.xml", "PermissionError"]),
    ("cannot_parse", "cannot parse app/pom.xml: XMLSyntaxError line 3", ["app/pom.xml"]),
    ("target_no_pom", "build target svc-a: no pom.xml", ["svc-a"]),
    ("module_other_pom", "pom.xml: module 'app/other.xml' names a pom file other than pom.xml — not read", ["app/other.xml"]),
    ("module_outside", "pom.xml: module '../x' points outside the project — not read", ["../x"]),
    ("module_no_pom", "pom.xml: module 'app-x' has no pom.xml", ["app-x"]),
    ("maven_incomplete_note", "Maven resolution is incomplete", []),
    ("res_incomplete", "resolution after the change is incomplete: module app-x: pom not readable (missing)", ["app-x"]),
    ("still_resolves", f"{GA} still resolves at 1.0 (p-1-1)", [GA, "1.0", "p-1-1"]),
    ("module_missing", "module app-x is missing from the resolution after the change (p-1-1)", ["app-x", "p-1-1"]),
    ("resolves_at", f"{GA} in module app-x resolves at nothing, planned 1.1 (p-1-1)", [GA, "app-x", "1.1", "p-1-1"]),
    ("info_not_governed", (f"info: {GA} in module app-x still resolves at 1.0 — not governed by the edited version text "
     "(p-1-1); left for the next round"), [GA, "app-x", "1.0", "p-1-1"]),
    ("changed", f"info: {GA} in module app-x changed 1.0 → 1.1 (moved with a property / parent / BOM change)",
     [GA, "app-x", "1.0", "1.1"]),
    ("info_packages", (f"info: app/target/app.war still packages WEB-INF/lib/lib-1.0.jar ({GA}:1.0) — its module is not "
     "governed by p-1-1"), ["app/target/app.war", "WEB-INF/lib/lib-1.0.jar", f"{GA}:1.0", "p-1-1"]),
    ("packages", f"app/target/app.war still packages WEB-INF/lib/lib-1.0.jar ({GA}:1.0, p-1-1)",
     ["app/target/app.war", f"{GA}:1.0", "p-1-1"]),
    ("edited_not_listed", "edited file app/pom.xml is not in the original file list", ["app/pom.xml"]),
    ("missing_original", "app/pom.xml: missing in the original tree", ["app/pom.xml"]),
    ("missing_candidate", "app/pom.xml: missing in the candidate", ["app/pom.xml"]),
    ("unreadable", "app/pom.xml: unreadable (OSError)", ["app/pom.xml", "OSError"]),
    ("changed_unplanned", "app/src/A.java: changed but no planned edit names it", ["app/src/A.java"]),
    ("edits_not_apply", "pom.xml: planned edits do not apply to the original (pom.xml:3: line does not exist (2 lines))",
     ["pom.xml"]),
    ("differs_more", "pom.xml: differs from the original by more than the planned version replacements and override entries",
     ["pom.xml"]),
    ("override_not_one", "pom.xml: the planned override entries are not in one top-level <dependencyManagement>", ["pom.xml"]),
    ("override_presence", "pom.xml: <dependencyManagement> presence before / after does not match the planned override",
     ["pom.xml"]),
    ("outside_differs", "a file outside the project folder differs from the accepted tree", []),
    ("upload_unknown", ("the upload of scan 'baseline' was sent but its result is unknown; check IVAS and confirm before "
     "resuming"), ["baseline"]),
    ("upload_unknown_e", "the upload of scan 'final-r1' has an unknown result: ReadTimeout", ["final-r1", "ReadTimeout"]),
    ("stop_scan", ("stop requested during scan 'final-r2'; the remote IVAS analysis keeps running and resume continues "
     "polling it without uploading again"), ["final-r2"]),
    ("stop_before_scan", "stop requested before scan 'baseline' uploaded anything", ["baseline"]),
    ("stop_baseline", "stop requested during the baseline build", []),
    ("stop_round", "stop requested during the round 2 candidate build", ["2"]),
    ("stop_node", "stop requested before node rescan", ["rescan"]),
    ("zip_changed_intake", "the original ZIP no longer has the sha256 recorded at intake", []),
    ("ivas_bytes_differ", "IVAS analysed bytes whose sha256 differs from the original ZIP", []),
    ("ivas_scanned_orig", "IVAS scanned a ZIP whose sha256 differs from the original", []),
    ("ivas_scanned_delivery", "IVAS scanned a ZIP whose sha256 differs from the delivery ZIP", []),
    ("scan_incomplete", "final scan incomplete: page_missing, total_mismatch", ["결과 페이지 누락", "전체 개수가 바뀜"]),
    ("maven_incomplete", "Maven resolution incomplete: build target svc-a: no pom.xml", ["svc-a"]),
    ("scan_missing", "the scan this round plans from is missing or incomplete", []),
    ("claim_held", ("another process is already working on this run: pid 41 on host sbmig-run-rem2 since "
     "2026-11-17T10:00:00Z (attempt 2)"), ["41", "sbmig-run-rem2", "2026-11-17T10:00:00Z", "시도 2"]),
    ("candidate_not_vouched", ("the round 2 candidate was not delivered: the files on disk are not the complete change set "
     "this state records"), ["라운드 2"]),
    ("failed_included", ("delivery/result.zip contains the round 2 change set whose candidate build FAILED (proposals: "
     "p-2-1, p-2-2; build log rounds/2/build.log); it was not rescanned; the original is preserved as original/source.zip; "
     "the verified delivery of an earlier round is kept as delivery/result-r1-verified.zip"),
     ["p-2-1", "p-2-2", "rounds/2/build.log", "delivery/result-r1-verified.zip"]),
    ("discarded", ("round 2 candidate discarded; delivery/result.zip is the verified state of round 1, without the failed "
     "change"), ["2", "1", "delivery/result.zip"]),
    ("delivery_missing", "the delivery ZIP is missing", []),
    ("delivery_changed", "the delivery ZIP changed after it was frozen", []),
    ("chain_budget", ("the chained remediation used its time budget of 90 minute(s) in this pod; resume continues from the "
     "stopped node (the remote IVAS analysis keeps running and is polled again without a new upload)"), ["90"]),
    ("baseline_differs", ("the migration's final build passed on this same tree, but the remediation's baseline build failed "
     "(build targets: svc-a, svc-b) with the same Maven command base, settings.xml and local repository (only IVAS_* / "
     "LLM_* variables are removed from its environment) — compare remediation/baseline/build.log with the migration's "
     "final build log (logs/*-build-verify*.log); the remediation builds a copy (remediation/baseline/tree) without .git/, "
     ".sbmig/, target/ build output"), ["svc-a", "svc-b", "remediation/baseline/build.log"]),
    ("baseline_tolerated", ("the project did not build before the remediation either (3 error(s) in baseline/build.log); "
     "the changed versions were scanned again, but no build confirmed them: the candidate build stopped at the same "
     "errors as the baseline build (3 error(s), none new)"), ["3건"]),
    ("candidate_new_errors", ("the candidate build failed with 1 error(s) the baseline build did not have: "
     "pom.xml dependency_resolution: dependency_resolution|example:lib"), ["1건", "example:lib"]),
    ("candidate_differs", ("the candidate build failed differently from the baseline build: the candidate build stopped in "
     "module core, the baseline build in web"), ["core", "web"]),
    ("restarted", ("the server restarted while this remediation was RUNNING; nothing is re-sent automatically — check the "
     "state and resume it explicitly"), ["진행 중"]),
    ("shutdown", ("the server shut down while this remediation was running; nothing is re-sent automatically — resume it "
     "explicitly"), []),
    ("build_output_left_out", "build output left out: 3 file(s) under core/target, target", ["core/target"]),
    ("cannot_start", "cannot start: RuntimeError: boom", ["RuntimeError"]),
    ("no_root_pom", "no readable root pom.xml", []),
    ("no_target_pom", "no readable pom.xml in the build targets", []),
    ("deptree_not_run", "dependency:tree not run: build.mvn points at a Maven wrapper (mvnw); refused", []),
    ("deptree_failed", "dependency:tree failed for build target svc-a (see deptree-svc-a-*.log)",
     ["svc-a", "deptree-svc-a-*.log"]),
    ("deptree_unreadable", "dependency:tree output unreadable: OSError", ["OSError"]),
    ("module_no_tree", f"module app-x ({GA}): no dependency tree", ["app-x", GA]),
    ("plugins_declared", "plugin versions are declared in the poms, not resolved", []),
    ("ready_mvnw", "build.mvn points at a Maven wrapper (mvnw); the server Maven installation is required", []),
    ("ready_mvnw_refused", "build.mvn points at a Maven wrapper (mvnw); refused", []),
    ("ready_no_maven", "Maven not available (FileNotFoundError)", ["FileNotFoundError"]),
    ("ready_no_jdk", ("JDK 21 not found (build.java_home / JAVA_HOME has no release file and Maven did not report a Java "
     "version)"), ["21"]),
    ("ready_jdk_wrong", "JDK 21 required, found Java 17", ["21", "17"]),
    ("ready_unsafe_args", maven_mod._unsafe_problem(["-Dmaven.wagon.http.ssl.insecure", "-s"]),
     ["-Dmaven.wagon.http.ssl.insecure", "-s"]),
    ("ready_settings_unset", "server settings.xml not configured (build.settings_file)", []),
    ("ready_settings_missing", "server settings.xml not found (/etc/maven/settings.xml)", ["/etc/maven/settings.xml"]),
    ("ready_settings_unreadable", "server settings.xml cannot be read (/etc/maven/settings.xml)", ["/etc/maven/settings.xml"]),
    ("ready_no_hosts", ("no approved repository host (remediation.allowed_repository_hosts is empty and the server "
     "settings.xml declares no mirror / repository)"), []),
    ("components_unbuilt", "remediation components cannot be built (RuntimeError)", ["RuntimeError"]),
    ("boundary_symlink", "symbolic link in the project", []),
    ("boundary_settings", "the server's settings.xml is the only one used", []),
    ("boundary_project_settings_ignored", ("names its own Maven settings (-s .mvn/settings.nexus.xml), not used: every "
     "build used the server's settings"), ["-s .mvn/settings.nexus.xml"]),
    ("boundary_project_settings_used", ("names its own Maven settings (-gs .mvn/g.xml), read by Maven as its global "
     "settings together with the server's"), ["-gs .mvn/g.xml"]),
    ("boundary_project_offline", "also turns on offline mode (-o): Maven downloads nothing", ["-o"]),
    ("boundary_wrapper", "the server's Maven is used; the wrapper is never run", []),
    ("boundary_mvn_not_used", "not used: the build did not read the project's .mvn folder", []),
    ("boundary_mvn_carried", ("not used: the build did not read the project's .mvn folder; carried over to the command "
     "line: -Drevision=1.2.3 -Pci"), ["-Drevision=1.2.3 -Pci"]),
    ("boundary_host", "host repo.example.invalid not approved: https://repo.example.invalid/maven2",
     ["repo.example.invalid", "https://repo.example.invalid/maven2"]),
    ("boundary_unresolved", "unresolved property in url ${repo.url}/maven2", ["${repo.url}/maven2"]),
    ("boundary_non_https", "non-https (http)", ["http"]),
    ("boundary_no_url", "no url", []),
    ("boundary_bad_url", "unparsable url", []),
    ("boundary_repo_id", "id=company-snapshots", ["company-snapshots"]),
    ("boundary_refused_pom", "unparsable_pom: app/pom.xml: not well-formed XML (line 3): Premature end of data",
     ["app/pom.xml", "XML 문법 오류(줄 3)"]),
    ("pom_xml_syntax", "not well-formed XML (line 3): Premature end of data in tag project", ["줄 3"]),
    ("pom_cannot_open", "cannot be opened (permission denied)", ["권한"]),
    ("pom_encoding_unknown", "character encoding unknown (the bytes do not decode as utf-16-le)", []),
    ("report_not_written", "report not written: OSError: disk full", ["OSError: disk full"]),
    ("stale_run", ("the migration run has ended while this remediation was RUNNING (its build pod is gone); "
     "nothing is re-sent automatically"), ["진행 중"]),
    # the suffix the graph appends to the state message when the build fix loop did not fix the candidate build
    ("auto_fix_left", "automatic build fix: not fixed: 3 error(s) left", ["남은 오류 3건", "되돌렸습니다"]),
    ("auto_fix_llm", "automatic build fix: llm unavailable: LLM_API_KEY is not set",
     ["LLM을 사용할 수 없음", "LLM_API_KEY is not set"]),
    ("auto_fix_stop", "automatic build fix: stop requested", ["중지 요청"]),
    ("auto_fix_time", "automatic build fix: time budget used up", ["제한 시간 초과"]),
    ("auto_fix_rejected", "automatic build fix: rejected: pom.xml must not be edited: app/pom.xml",
     ["고칠 수 없는 파일: app/pom.xml"]),
    ("auto_fix_confirm", "automatic build fix: error: confirming build failed", ["고친 뒤 다시 한 빌드가 실패함"]),
    ("auto_fix_error", "automatic build fix: error: RuntimeError: boom", ["내부 오류", "RuntimeError: boom"]),
    ("auto_fix_other", "automatic build fix: something new", ["something new"]),
    ("auto_fix_discarded", "automatic build fix: discarded: the candidate was remade",
     ["수정본을 다시 만들어 이전 수정은 버림"]),
    # the suffix when the loop was not started (attempted: false); it never says the sources were reverted
    ("auto_fix_not_started_stop", "automatic build fix not started: stop requested",
     ["빌드 오류 자동 수정은 시작하지 않았습니다(중지 요청)."]),
    ("auto_fix_not_started_time", "automatic build fix not started: time budget used up",
     ["빌드 오류 자동 수정은 시작하지 않았습니다(남은 시간 부족)."]),
    ("auto_fix_not_started", "automatic build fix not started: something new", ["something new"]),
]


@pytest.mark.parametrize("key,sentence,ids", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_every_template_turns_korean_and_keeps_identifiers(key, sentence, ids):
    out = i18n.ko_text(sentence)
    assert HANGUL.search(out), out
    for ident in ids:
        assert ident in out, (ident, out)
    rule = next(r for r in i18n.RULES if r.rx.fullmatch(sentence))
    assert rule.key == key
    phrase = i18n.KEY_PHRASES.get(key)
    if phrase:
        assert phrase not in out


def test_every_rule_has_a_sample():
    assert {r.key for r in i18n.RULES} == {s[0] for s in SAMPLES}


def test_unknown_text_is_returned_unchanged_and_pieces_are_translated_separately():
    assert i18n.ko_text("something only Maven says") == "something only Maven says"
    assert i18n.ko_text("") == "" and i18n.ko_text(None) is None
    mixed = i18n.ko_text("something unknown; managed by org.example:bom:5")
    assert mixed.startswith("something unknown; ") and "org.example:bom:5에서 버전을 지정합니다." in mixed
    # Maven's own build summary after " | " stays as it is
    out = i18n.ko_text("stop requested during the baseline build | [ERROR] Failed to execute goal")
    assert out.endswith(" | [ERROR] Failed to execute goal") and "조치 전 빌드" in out
    # the report shows an unknown piece once, after the Korean, as the original text in backticks
    assert i18n.ko_display("something only Maven says") == "원문: `something only Maven says`"
    assert i18n.ko_display("something unknown; managed by org.example:bom:5") == (
        "org.example:bom:5에서 버전을 지정합니다. 원문: `something unknown`")
    assert i18n.ko_display("") == "" and i18n.ko_display(None) == ""


def test_real_verify_lines_translate():
    """Lines produced by apply.verify_resolution itself (cheap inputs), including the new info: lines."""
    def model(mods: dict[str, dict[str, str]]) -> ResolvedModel:
        return ResolvedModel(modules=[ModuleResolution(dir=m, group_id="g", artifact_id=m, version="1",
                                                       dependencies=[ResolvedDep(group_id=ga.split(":")[0],
                                                                                 artifact_id=ga.split(":")[1], version=v,
                                                                                 direct=ga == GA)
                                                                     for ga, v in deps.items()])
                                     for m, deps in mods.items()])
    p = Proposal(id="p-1-1", round=1, ga=GA, from_version="1.0", to_version="1.1", kind="direct_version",
                 modules=["app-a"])
    before = model({"app-a": {GA: "1.0", "org.example:t": "2"}, "app-b": {GA: "1.0"}})
    after = model({"app-a": {GA: "1.0", "org.example:t": "3"}, "app-b": {GA: "1.0"}})
    packaged = [PackagedLib(artifact="app-b/target/app-b.war", entry="WEB-INF/lib/lib-1.0.jar", file_name="lib-1.0.jar",
                            ga=GA, version="1.0")]
    lines = apply_mod.verify_resolution([p], before, after, packaged)
    assert lines and any(x.startswith("info:") for x in lines)
    for line in lines:
        out = i18n.ko_text(line)
        assert HANGUL.search(out) and out != line, line
        assert "p-1-1" in out or "org.example:t" in out


def test_detail_group_has_a_stable_key_and_one_explanation():
    a = "module(s) a-boot, a-domain do not inherit from the root pom.xml: an override entry there would not reach them"
    b = "module(s) b-store do not inherit from the root pom.xml: an override entry there would not reach them"
    ka, ta = i18n.detail_group(a)
    kb, tb = i18n.detail_group(b)
    assert ka == kb == "not_inheriting_root" and ta == tb
    assert "a-boot" not in ta and HANGUL.search(ta)
    assert i18n.group_meta(ka) == ("모듈 pom에 직접 지정해야 하는 버전", False)
    assert "회사 부모 pom" in ta and "최상위 pom.xml" in ta and "상속" not in ta and "모듈의 pom.xml" in ta
    k, t = i18n.detail_group("version property ${x} is not defined in the project poms; managed by g:p:1")
    assert k == "external_property+external_manager" and "g:p:1" not in t
    assert i18n.group_meta(k)[1] is True
    raw = "an unknown detail"
    assert i18n.detail_group(raw) == (raw, "원문: `an unknown detail`") and i18n.group_meta(raw) == ("", False)


def test_inhouse_gas_reads_old_details():
    assert i18n.inhouse_gas(f"{GA2} is an in-house artifact: its owner publishes the fixed version") == [GA2]
    assert i18n.inhouse_gas(f"brought in by the in-house artifact(s) {GA2}, g:b: only their owner can change it") == [GA2, "g:b"]
    assert i18n.inhouse_gas("inventory incomplete") == []


def test_labels():
    assert i18n.label(i18n.ORIGIN_KO, "transitive") == "간접 의존성"
    assert i18n.label(i18n.REASON_KO, "inhouse_artifact") == "사내 라이브러리 (담당자 요청)"
    assert i18n.label(i18n.REASON_KO, "manual_review") == "직접 확인 필요"
    assert i18n.label(i18n.KIND_KO, "unknown_kind") == "unknown_kind"
    assert i18n.evidence_ko("policy:pins") == "서버 정책에 지정한 버전"
    assert i18n.loc_ko("app/pom.xml:41") == "app/pom.xml 41행" and i18n.loc_ko("pom.xml") == "pom.xml"
    assert i18n.evidence_ko("repository:nexus.invalid") == "저장소 nexus.invalid"
    from sbmig.remediations import models
    codes = {v for k, v in vars(models).items() if k.startswith("R_") and isinstance(v, str)} | {"chain_budget"}
    assert codes <= set(i18n.REASON_KO)


def test_variant_reason_is_said_once():
    out = i18n.ko_text(f"transitive dependency {GA} resolves as a non-jar type or with a classifier (type/classifier "
                       "variant (jar/linux-x86_64)); a plain override entry would not govern it")
    assert out == (f"간접 의존성 {GA}의 형식이 jar가 아니거나 classifier가 붙어 있습니다(jar/linux-x86_64). "
                   "dependencyManagement의 버전 지정으로는 조치할 수 없습니다.")
    out = i18n.ko_text(f"managed version {GA} resolves as a non-jar type or with a classifier (managed by g:p:1; "
                       "type/classifier variant in app-a); a plain override entry would not govern it")
    assert "(app-a 모듈)" in out and "g:p:1에서 버전을 지정합니다." in out and out.count("classifier") == 1
    detail = (f"transitive dependency {GA} resolves as a non-jar type or with a classifier (type/classifier variant "
              "(jar/linux-x86_64, test-jar/tests, pom/)); a plain override entry would not govern it")
    assert i18n.variant_label(detail) == "linux-x86_64, test-jar, tests, pom"
    assert i18n.row_memo(detail) == ""                       # the variant fact goes next to the coordinates
    assert i18n.row_memo(f"managed version {GA} resolves as a non-jar type or with a classifier (managed by g:p:1; "
                         "type/classifier variant in app-a); a plain override entry would not govern it") == (
        "g:p:1에서 버전을 지정합니다.")


def test_nested_reasons_are_not_wrapped_in_parentheses_twice():
    out = i18n.ko_text("inventory incomplete: nothing planned (module app: pom not readable (missing))")
    assert out == "의존성 목록을 다 읽지 못해 조치 계획을 세우지 않았습니다. 이유: app 모듈의 pom을 읽을 수 없습니다(missing)."
    assert i18n.ko_text("Maven resolution incomplete: Maven resolution is incomplete") == (
        "Maven 의존성 분석이 완전하지 않습니다. 이유: Maven 의존성 분석이 완전하지 않습니다.")
    # an inner reason no rule knows is shown as the original text, not as half-translated Korean
    assert i18n.ko_text("inventory incomplete: something new") == (
        "의존성 목록을 다 읽지 못했습니다. 원문: `something new`")


DETACHED = re.compile(r"[A-Za-z0-9`)\]] (은|는|이|가|을|를|과|와|로|으로|의|에|에서|도|만|이라|라)(?=[\s,.)]|$)")


@pytest.mark.parametrize("key,sentence,ids", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_every_rule_attaches_its_particles(key, sentence, ids):
    for out in (i18n.ko_text(sentence), i18n.ko_display(sentence)):
        assert not DETACHED.search(out), out


def test_labels_and_groups_attach_their_particles():
    texts = [g.title + " " + g.text for g in i18n.GROUPS.values()]
    for name in ("ORIGIN_KO", "ORIGIN_ALL_KO", "KIND_KO", "RISK_KO", "DECISION_KO", "REASON_KO", "COUNT_KO", "SCAN_REASON_KO",
                 "BOUNDARY_KO"):
        texts += list(getattr(i18n, name).values())
    assert [t for t in texts if DETACHED.search(t)] == []


def test_not_inheriting_root_and_variant_speak_to_a_project_owner():
    """The user did not understand "루트 pom을 상속하지 않는 모듈" (2026-11-17): the words say what the tool can do, why
    it does not reach these modules and what to do by hand, without Maven inheritance vocabulary."""
    out = i18n.ko_text("module(s) app-boot, app-domain do not inherit from the root pom.xml: an override entry there "
                       "would not reach them")
    assert out.startswith("app-boot, app-domain 모듈은 프로젝트 최상위 pom.xml이 아니라 회사 부모 pom에서 설정을 물려받습니다.")
    assert "모듈의 pom.xml에 조치된 버전을 지정해야 합니다." in out and "상속" not in out
    g = i18n.GROUPS["not_inheriting_root"]
    assert g.title == "모듈 pom에 직접 지정해야 하는 버전"
    assert "모듈 pom에 버전 지정을 제안하는 설정이 꺼져 있어" in g.text and "dependencyManagement" in g.text
    assert "classifier는 같은 버전의 jar를 용도별로 구분하는 이름입니다(예: jakarta)." in i18n.GROUPS["variant"].text


def test_variant_override_sentences():
    """Variant overrides (one proposal per management key): the note, the residual that cannot be written, the
    apply / structure-check messages with the full key, and the existing rules with the new key shapes."""
    note = i18n.ko_text("the entry names classifier jakarta like the resolved dependency: an entry without it would not "
                        "govern that dependency")
    assert note == ("추가하는 항목에 해석된 의존성과 같은 값을 적습니다(classifier jakarta). 이 값이 없는 항목은 그 "
                    "의존성에 적용되지 않습니다.")
    assert "(형식 pom)" in i18n.ko_text("the entry names type pom like the resolved dependency: an entry without it "
                                       "would not govern that dependency")
    detail = f"transitive dependency {GA} resolves as jar/a b, which cannot be written as plain override coordinates"
    key, text = i18n.detail_group(detail)
    assert key == "variant" and text == i18n.GROUPS["variant"].text
    assert i18n.variant_label(detail) == "a b" and i18n.row_memo(detail) == ""
    assert i18n.ko_text("cannot place an override entry safely: the root pom declares a DTD / entities; not edited"
                        ).startswith("루트 pom.xml 파일에 DTD나 엔티티 선언이 있어")
    assert i18n.ko_text("cannot place an override entry safely: override type / classifier 'jar' / \"it's\" is not plain "
                        "coordinate text").startswith("dependencyManagement 항목의 형식('jar')이나 classifier(\"it's\")")
    for ga in (GA, f"{GA}:jar:jakarta", f"{GA}:test-jar"):
        assert i18n.rule_keys(f"the root pom.xml already has <dependencyManagement> entries for {ga} (top level, profile "
                              "dev); a second entry is never added") == ["root_entries_many"]
        assert i18n.rule_keys(f"the root pom.xml already has a <dependencyManagement> entry for {ga} whose version (1.0-x) "
                              "is not the resolved 1.0 as plain text; a second entry is never added") == [
            "root_entry_mismatch"]
        assert i18n.rule_keys(f"{ga} is declared with a version in app/pom.xml (1.0): a declared version wins over "
                              "<dependencyManagement>, so an override entry would not change it") == ["declared_wins"]
        out = i18n.ko_text(f"pom.xml: inserted <dependency> {ga} is not exactly groupId / artifactId / version 1.1")
        assert out == f"pom.xml: 추가한 {ga} 항목의 내용이 계획(groupId, artifactId, version 1.1)과 다릅니다."


def test_variant_label_skips_the_plain_jar():
    assert i18n.variant_label("type/classifier variant (jar/, jar/jakarta)") == "jakarta"
    assert i18n.variant_label("type/classifier variant (jar/jdk8, test-jar/tests)") == "jdk8, test-jar, tests"


def test_module_pom_overrides_name_their_pom():
    """Override sentences name the module pom they edit (v2.1z-an); the root keeps its old wording."""
    pom = "app-back/pom.xml"
    cases = {
        f"adds a <dependencyManagement> entry {GA}:1.1 to {pom}: pins the transitive dependency brought by "
        "org.example:a:1 → org.example:b:2 (1.0 → 1.1)": "override_transitive",
        f"adds a <dependencyManagement> entry {GA}:1.1 to {pom}: overrides the version managed by org.example:bom:5 "
        "(1.0 → 1.1)": "override_managed",
        f"uses the existing <dependencyManagement> entry of {pom}": "uses_existing",
        f"{pom} already has <dependencyManagement> entries for {GA} (top level, profile dev); a second entry is never "
        "added": "root_entries_many",
        f"{pom} already has a <dependencyManagement> entry for {GA} whose version (${{v}}) is not the resolved 1.0 as "
        "plain text; a second entry is never added": "root_entry_mismatch",
    }
    for sentence, key in cases.items():
        assert i18n.rule_keys(sentence) == [key], sentence
        out = i18n.ko_text(sentence)
        assert out.startswith(f"{pom}의 dependencyManagement에") and "루트" not in out, out
        assert not DETACHED.search(out), out
    assert i18n.ko_text("uses the existing <dependencyManagement> entry of the root pom.xml").startswith("루트 pom.xml의")


def test_an_ineffective_override_entry():
    one = (f"the override entry in app-back/pom.xml did not change the resolved version of {GA} (1.0 stays): a dependency "
           "declared with a version, here or in a parent, wins over <dependencyManagement>; the entry was removed again")
    key, text = i18n.detail_group(one)
    assert key == "override_no_effect" and i18n.group_meta(key) == ("버전 지정을 넣어도 바뀌지 않는 버전", False)
    assert "다시 뺐습니다" in text and "그 선언을 찾아 버전을 직접 올려 주세요." in text
    assert i18n.ko_text(one).startswith(f"app-back/pom.xml에 {GA}의 버전 지정을 넣었지만 실제 버전이 바뀌지 않아(1.0 그대로)")
    assert i18n.label(i18n.DECISION_KO, "ineffective") == "효과가 없어 다시 뺌"
    msg = f"{GA} still resolves at 1.0 (p-1-1) | after removing the ineffective override entr(ies) p-1-2"
    assert i18n.rule_keys(msg) == ["still_resolves", "after_removing_ineffective"]


def test_three_pods_review_sentences():
    """v2.1z-ap: the claim refusal and the unvouched candidate read in Korean (the re-plan reason about approvals
    went with the approval step)."""
    from sbmig.remediations import chain, graph
    assert i18n.ko_text(chain.claim_refusal(None)) == "다른 프로세스가 이미 이 실행을 진행하고 있습니다."
    assert i18n.ko_text(chain.CLAIM_HELD) == "다른 프로세스가 이미 이 실행을 진행하고 있습니다."
    assert i18n.ko_text(chain.claim_refusal({"pid": 7, "host": "pod-a", "started": "2026-11-17T00:00:00Z"})) == (
        "다른 프로세스가 이미 이 실행을 진행하고 있습니다(pid 7, 호스트 pod-a, 시작 2026-11-17T00:00:00Z).")
    assert "라운드 3의 수정본" in i18n.ko_text(graph.CANDIDATE_NOT_VOUCHED.format(r=3))


def test_structure_violation_sentences_reach_the_report_in_korean():
    """Review finding M6: the structure-check sentences of apply.verify_text_only had no rule (shown raw)."""
    before = b"<project><groupId>a</groupId><version>1</version></project>"
    cases = [b"<project><groupId>a</groupId><version>1</version><x/></project>",       # structure changed
             b"<project><groupId>b</groupId><version>1</version></project>",            # text of <groupId>
             b"<project><groupId>a</groupId>x<version>1</version></project>",           # text between elements
             b"<project><groupId>a</groupId>"]                                          # unparsable
    for after in cases:
        (problem,) = apply_mod._structure_problems("app/pom.xml", before, after)
        out = i18n.ko_display(problem)
        assert HANGUL.search(out) and "원문" not in out and "app/pom.xml" in out, (problem, out)


def test_unreadable_pom_reasons_read_as_words():
    assert i18n.ko_text("unparsable_pom: app/pom.xml") == "pom 파일을 읽을 수 없습니다. app/pom.xml."
    assert i18n.ko_text("cannot be opened (missing)") == "파일이 없습니다."
    assert i18n.ko_text("cannot be opened (a folder, not a file)") == "파일이 아니라 폴더입니다."
    assert i18n.ko_text("cannot read app/pom.xml: cannot be opened (permission denied)") == (
        "app/pom.xml 파일을 읽을 수 없습니다. 파일을 열 수 없습니다(권한).")
    assert i18n.ko_text("module app: pom not readable (cannot read app/pom.xml: not well-formed XML (line 7): x)") == (
        "app 모듈의 pom을 읽을 수 없습니다. app/pom.xml: XML 문법 오류(줄 7).")


def test_fix_reasons_of_the_work_copy_fixer():
    """Reasons the fixer writes since INTERFACES "v2.1z-bj": bare `llm unavailable` (the old form still reads), the
    guard reason `<p> must not be edited`, a discarded fix; a loop that never started is never said to be reverted."""
    assert i18n.fix_reason_ko("llm unavailable") == ("LLM을 사용할 수 없음", "")
    assert i18n.fix_reason_ko("llm unavailable: gateway 503") == ("LLM을 사용할 수 없음", "원문: `gateway 503`")
    assert i18n.fix_reason_ko("rejected: app/mvnw must not be edited") == ("고칠 수 없는 파일: app/mvnw", "")
    assert i18n.fix_reason_ko("rejected: pom.xml must not be edited: app/pom.xml") == ("고칠 수 없는 파일: app/pom.xml", "")
    assert i18n.fix_reason_ko("discarded: the candidate was remade") == ("수정본을 다시 만들어 이전 수정은 버림", "")
    assert i18n.ko_text("automatic build fix: llm unavailable") == (
        "빌드 오류 자동 수정을 시도했지만 고치지 못했습니다(LLM을 사용할 수 없음). 소스 수정은 되돌렸습니다.")
    assert i18n.rule_keys("automatic build fix: llm unavailable") == ["auto_fix_llm"]
    for reason, why in (("stop requested", "중지 요청"), ("time budget used up", "남은 시간 부족")):
        line = i18n.fix_not_started_ko(reason)
        assert line == f"빌드 오류 자동 수정은 시작하지 않았습니다({why})."
        out = i18n.ko_text(f"the candidate build failed | automatic build fix not started: {reason}")
        assert line in out and "되돌렸" not in out
