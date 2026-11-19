"""v2.1z-bf: the structure review describes a single-module Spring Boot app split by `layout.boot_single_module:
split` as the company layout of a non-Boot project (boot / back / front modules), not as "directories kept"."""
from __future__ import annotations

from sbmig.core.models import CaseType, PomInfo, ProjectProfile, SourceStats
from sbmig.review.inputs import rule_lines


def _profile(modules: list[str]) -> ProjectProfile:
    return ProjectProfile(root="/x", name="shop", case_type=CaseType.SPRING_BOOT, packaging="jar",
                          root_pom=PomInfo(path="pom.xml", artifact_id="shop", group_id="com.example", version="1.0",
                                           is_root=True, modules=modules),
                          source=SourceStats(web_static_count=1))


def _company(cfg):
    lay = cfg.layout
    lay.boot_as_is, lay.module_dir = "in-place", ""
    lay.boot_module, lay.comp_module, lay.front_module = "{name}-boot", "{name}-back", "{name}-front"
    return cfg


def test_split_single_boot_app_is_described_as_the_layout(cfg) -> None:
    _company(cfg)
    lines, _ = rule_lines(cfg, _profile([]), "shop")
    text = "\n".join(lines)
    assert "- layout (case `spring_boot`): the root is a `pom` aggregator" in text
    assert "`shop-front`: web resources only" in text
    assert "directories are kept" not in text


def test_wrap_and_multi_module_keep_the_in_place_text(cfg) -> None:
    _company(cfg)
    multi = "\n".join(rule_lines(cfg, _profile(["shop-boot", "shop-core"]), "shop")[0])
    assert "directories are kept" in multi and "split like the other cases" in multi
    cfg.layout.boot_single_module = "wrap"
    wrap = "\n".join(rule_lines(cfg, _profile([]), "shop")[0])
    assert "directories are kept" in wrap and "a single-module Boot app is wrapped into `shop-boot`" in wrap


def test_split_single_boot_app_names_the_real_jsp_place(cfg) -> None:
    """In a Boot split the JSPs keep their web path inside the front jar's META-INF/resources (the application's own
    view prefix stays valid); the review must not be told they sit under `layout.front_jsp_dir`."""
    _company(cfg)
    p = _profile([])
    p.source.jsp_count = 2
    text = "\n".join(rule_lines(cfg, p, "shop")[0])
    front = next(ln for ln in text.splitlines() if "`shop-front`: web resources only" in ln)
    assert cfg.layout.front_jsp_dir not in front
    assert "JSPs at their as-is web path under `src/main/resources/META-INF/resources`" in front
    # a non-Boot project keeps the configured JSP folder in that line
    p.case_type = CaseType.SPRING_LEGACY
    legacy = "\n".join(rule_lines(cfg, p, "shop")[0])
    assert cfg.layout.front_jsp_dir in next(ln for ln in legacy.splitlines() if "`shop-front`: web resources only" in ln)
