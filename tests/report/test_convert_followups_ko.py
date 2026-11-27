"""The two hand-over lines the conversion step writes (`sbmig/loop/convert.py`: nothing to convert, duplicate request
mappings in the generated controllers) have Korean. The English lines are built by the real code."""
from __future__ import annotations

from sbmig.loop import convert as cv
from sbmig.report.i18n import tr_line

DUPLICATES = ["GET /api/order: claimed by OrderController.list and Order2Controller.list",
              "GET /api/a and GET /api/A differ only in case: AController.x, A2Controller.y"]
MANY = [f"POST /api/x{i}: claimed by X.a{i} and Y.b{i}" for i in range(7)]


def lines() -> list[str]:
    """Real follow-ups of the conversion step (also fed to tests/report/test_report_i18n.py)."""
    return [cv.SKIPPED_LINE, cv.duplicate_followup(DUPLICATES), cv.duplicate_followup(MANY)]


def test_nothing_to_convert_line():
    ko = tr_line("ko", cv.SKIPPED_LINE)
    assert ko == ("사내 규정 전환은 하지 않았습니다. 이 프로젝트에는 사내 프레임워크의 서비스 XML도 SQL 파일도 없어서, 올린 "
                  "그대로 두고 JDK 21, Jakarta만 적용하는 방법과 같이 처리했습니다.")
    assert tr_line("en", cv.SKIPPED_LINE) == cv.SKIPPED_LINE


def test_duplicate_mappings_line_names_every_address_and_handler():
    ko = tr_line("ko", cv.duplicate_followup(DUPLICATES))
    assert ko.startswith("사내 규정 전환으로 만든 컨트롤러에 여러 메서드가 함께 받는 주소가 2건 있습니다. 메서드를 하나만 "
                         "남기기 전에는 애플리케이션이 시작하지 않으니")
    assert "`GET /api/order`(`OrderController.list`, `Order2Controller.list`)" in ko
    assert "대소문자만 다른 `GET /api/a`, `GET /api/A`(AController.x, A2Controller.y)" in ko
    assert "claimed by" not in ko and "differ only" not in ko


def test_duplicate_mappings_line_cap_reads_in_korean():
    ko = tr_line("ko", cv.duplicate_followup(MANY))
    assert "주소가 7건 있습니다" in ko and ko.endswith("; 외 2건.") and "more" not in ko
    assert "`POST /api/x4`(`X.a4`, `Y.b4`)" in ko and "x5" not in ko
