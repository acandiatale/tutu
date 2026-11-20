"""The upload page shows neither the advanced settings nor the list of other run files (the admin page serves those)."""
import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[2] / "sbmig" / "web" / "static" / "index.html").read_text(encoding="utf-8")


def test_advanced_settings_are_hidden_but_their_fields_stay_for_the_start_request():
    m = re.search(r"<details class=\"adv\" id=\"adv\"([^>]*)>", PAGE)
    assert m and "hidden" in m.group(1)
    for field in ('id="cfgs"', 'id="llm"', 'id="stop_after"'):
        assert field in PAGE
    assert "고급 설정에서 바꿈" not in PAGE
    # no style gives the block a display value that would beat the hidden attribute
    assert not re.search(r"details\.adv\s*\{[^}]*display", PAGE)


def test_other_files_are_not_listed_on_the_page():
    assert "<span>그 밖의 파일</span>" not in PAGE
    assert "$('otherfiles').textContent = '';" in PAGE
    # the notice about a missing result ZIP stays
    assert "변환된 프로젝트 ZIP이 없습니다" in PAGE
