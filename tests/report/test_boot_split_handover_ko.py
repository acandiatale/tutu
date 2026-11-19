"""v2.1z-bf: the hand-over lines of a split single-module Spring Boot app read as Korean in the requester's guide."""
from __future__ import annotations

import pytest

from sbmig.report.i18n import tr_line

LINES = {
    "users": ("boot split: 2 class(es) that use the application class ShopApplication moved with it to shop-boot "
              "(com.acme.shop.support.Banner, com.acme.shop.support.BannerUser) because shop-back cannot depend on the "
              "boot module — check that they belong there",
              "애플리케이션 클래스(ShopApplication)를 사용하는 클래스 2개를 함께 shop-boot 모듈로 옮겼습니다"
              "(com.acme.shop.support.Banner, com.acme.shop.support.BannerUser). shop-back 모듈은 boot 모듈을 참조할 수 "
              "없기 때문입니다. 이 클래스들이 그 모듈에 있어도 되는지 확인해 주세요."),
    "scan": ("boot split: the component scan of com.acme.shop (the package of com.acme.shop.ShopApplication) does not "
             "reach 1 package(s) of shop-back (com.other.lib) — check how the application registered their beans before "
             "the split (it was the same scan) and that it still does",
             "shop-back 모듈의 패키지 1개(com.other.lib)는 애플리케이션 클래스(com.acme.shop.ShopApplication)가 있는 "
             "com.acme.shop 패키지 밖에 있어 기본 컴포넌트 스캔에 들어가지 않습니다. 나누기 전에도 스캔 범위는 같았으니, "
             "이 클래스들을 어떻게 빈으로 등록했는지와 지금도 그대로 등록되는지 확인해 주세요."),
    "realpath": ("boot split: 1 source(s) read web files by a file path (getRealPath / src/main/webapp: "
                 "shop-back/src/main/java/com/acme/shop/web/Download.java); the web files now come from the shop-front "
                 "jar, where no such path exists — read them as classpath resources",
                 "소스 1개가 웹 파일을 파일 경로로 읽습니다(getRealPath / src/main/webapp: "
                 "shop-back/src/main/java/com/acme/shop/web/Download.java). 웹 파일은 이제 shop-front jar 안에 있어 그런 "
                 "경로가 없으니 클래스패스 자원으로 읽도록 바꿔 주세요."),
}


@pytest.mark.parametrize("key", sorted(LINES))
def test_boot_split_lines_in_korean(key: str) -> None:
    en, ko = LINES[key]
    assert tr_line("ko", en) == ko
    assert tr_line("en", en) == en
