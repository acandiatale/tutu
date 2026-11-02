"""Rule 14 of the script prompt stays as v2.1l / v2.1s / v2.1t / v2.1u decided (this guard used to live in the deleted
v2.1i edits-mode test file): the facts decide in the resolution order (new parent chain → target Boot BOM → as-is →
repository → null), an unknown version is written null and sbmig fills it, never a reason to refuse, the parent chain's
properties as `${name}`, no "look releases up yourself"; ≤ 1.4K chars."""
from __future__ import annotations

from sbmig.fix.fixer import PROMPTS_DIR


def test_rule_14_leaves_unknown_versions_to_sbmig():
    raw = (PROMPTS_DIR / "script_system_v1.md").read_text(encoding="utf-8")
    rule = raw[raw.index("14. Dependency versions"):raw.index("## Edits (`<edits>`)")].strip()
    text = " ".join(rule.split())
    assert len(text) <= 1400, len(text)
    order = [text.index(x) for x in ("`new parent chain: manages g:a at v`", "`target Boot BOM: manages g:a at v`",
                                     "`as-is build: g:a v`")]
    assert order == sorted(order) and "in this order" in text
    assert ('Without a fact write `"version": null` (sbmig fills it: none when managed, else the as-is version, else '
            "the newest release in the repository)") in text
    assert "`parent chain property: name=value` → `${name}`, never the literal (in-house artifacts too)" in text
    assert "`managed artifacts under <group> …` → add the one holding the missing package, version-less" in text
    assert "look releases up" not in text and "If you can look" not in text
    assert "never refuse, answer `<no_script>` or leave an error unfixed because a version is unknown" in text
