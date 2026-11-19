"""v2.1z-ax: flag `pomless-library` has an explanation in both languages, consistent with its hand-over line."""
from __future__ import annotations

from sbmig.report.markdown import FLAG_HELP, flag_help


def test_pomless_library_flag_has_help_in_both_languages():
    en = flag_help("pomless-library", "en")
    assert en == FLAG_HELP["pomless-library"]
    assert "minimal pom" in en and "no pom" in en and "Manual follow-ups" in en
    ko = flag_help("pomless-library", "ko")
    assert ko != en and "최소한의 pom" in ko and "「확인해 주세요」" in ko
    assert "—" not in ko and ko.rstrip().endswith(("니다.", "주세요."))
