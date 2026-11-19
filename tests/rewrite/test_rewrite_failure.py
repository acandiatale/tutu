"""`sbmig.rewrite.failure`: the words of a failed OpenRewrite run (INTERFACES "v2.1z-ay", part B)."""
from __future__ import annotations

from sbmig.rewrite.failure import (
    FLAG,
    HANDOVER_PREFIX,
    clean_reason,
    recipe_set_names,
    rewrite_failed_followup,
)

J21 = "org.openrewrite.java.migrate.UpgradeToJava21"
JAKARTA = "org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta"


def test_flag_and_prefix():
    assert FLAG == "rewrite-failed"
    assert rewrite_failed_followup(["jdk21"], "x").startswith(HANDOVER_PREFIX)


def test_recipe_set_names_from_the_configured_sets(cfg):
    assert recipe_set_names([J21, JAKARTA], cfg) == ["jdk21", "jakarta"]
    assert recipe_set_names(["com.example.Unlisted"], cfg) == ["Unlisted"]


def test_clean_reason_drops_paths_secrets_and_extra_lines():
    root = "/srv/out/run-1/project"
    err = (f"Failed to parse pom: Invalid UTF-8 middle byte 0xce (at char #421, byte #37) in {root}/app/pom.xml "
           "via https://deploy:s3cr3t@nexus.example.invalid/repository/public and /home/builder/.m2/settings.xml\n"
           "[ERROR] more lines")
    out = clean_reason(err, roots=[root])
    assert out == ("Failed to parse pom: Invalid UTF-8 middle byte 0xce (at char #421, byte #37) in app/pom.xml via "
                   "https://***@nexus.example.invalid/repository/public and …/settings.xml")
    assert "s3cr3t" not in out and "/home/" not in out
    assert clean_reason("C:\\Users\\kim\\work\\pom.xml broke", roots=[]) == "…\\pom.xml broke"
    assert clean_reason("x" * 900, roots=[]).endswith("…") and len(clean_reason("x" * 900, roots=[])) <= 300
    assert clean_reason("", roots=[]) == "OpenRewrite run failed"


def test_followup_line():
    assert rewrite_failed_followup(["jdk21", "jakarta"], "Failed to parse pom: Invalid UTF-8 middle byte 0xce") == (
        "the automatic conversion (OpenRewrite) failed — recipe sets not applied: jdk21, jakarta; reason: Failed to "
        "parse pom: Invalid UTF-8 middle byte 0xce — the files were not converted and the build ran on the "
        "unconverted tree; fix the cause and run the migration again")
