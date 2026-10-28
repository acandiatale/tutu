"""옛 케이스 카탈로그 ID 를 import 하던 코드를 위해 남긴 호환 모듈.
``sbmig.triage.cluster``, 테스트, scripts/e2e.py 가 여기서 ``case_id_for`` / ``case_id_of`` 를 가져간다.
카탈로그 자체는 v2.1 에서 없어졌고 케이스는 ``<kb>/cases/<case_id>/``(:mod:`sbmig.errors.cases`)에 있다.
``case_id_for(l2)`` = ``sha1(l2)[:10]`` 의 정의는
:mod:`sbmig.errors.common` 하나뿐이고 여기서는 다시 내보내기만 한다.

Kept for the importers of the v1/v2 case catalog id (``sbmig.triage.cluster``, tests, scripts/e2e.py): the
catalog itself is gone in v2.1 (docs/v2/PLAN-v2.1.md §3.4) — cases live in ``<kb>/cases/<case_id>/``
(:mod:`sbmig.errors.cases`). ``case_id_for(l2)`` = ``sha1(l2)[:10]``, defined in :mod:`sbmig.errors.common`."""
from sbmig.errors.common import case_id_for, case_id_of

__all__ = ["case_id_for", "case_id_of"]
