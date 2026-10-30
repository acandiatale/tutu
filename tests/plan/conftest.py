"""계획 테스트 공통: v2.1o 부터 재구성 전 컴파일(``transform.compile``)은 기본으로 꺼져 있다. 이 디렉토리의 기존 테스트는
v2.1 순서(변환 → 컴파일 → 재구성 → 검토 → 검증)를 그대로 단언하므로 자동 픽스처가 ``plan.compile_after_rewrite`` 를 켠다;
기본값 자체는 ``test_planner.py::test_default_plans_no_compile_before_restructure`` 가 본다.

Plan tests: since v2.1o the compile between the rewrite and the restructure is off by default; the existing tests here
assert the v2.1 order, so an autouse fixture switches `plan.compile_after_rewrite` on. The default is covered by
`test_default_plans_no_compile_before_restructure`."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _v21_compile_step(cfg):
    """v2.1 순서를 단언하는 기존 계획 테스트를 위해 컴파일 단계를 켠다."""
    cfg.plan.compile_after_rewrite = True
    return cfg
