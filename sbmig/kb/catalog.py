"""케이스 ID 함수를 예전 위치에서 import하는 코드를 위해 남겨 둔 호환 모듈.
``sbmig.triage.cluster``, 테스트, scripts/e2e.py가 여기서 ``case_id_for`` / ``case_id_of``를 가져간다.
실제 정의는 :mod:`sbmig.errors.common`에 있고 이 모듈은 다시 내보내기만 한다.

Kept for the importers of the v1/v2 case catalog id (``sbmig.triage.cluster``, tests, scripts/e2e.py): the
catalog itself is gone in v2.1 (docs/v2/PLAN-v2.1.md §3.4) — cases live in ``<kb>/cases/<case_id>/``
(:mod:`sbmig.errors.cases`). ``case_id_for(l2)`` = ``sha1(l2)[:10]``, defined in :mod:`sbmig.errors.common`."""
from sbmig.errors.common import case_id_for, case_id_of

__all__ = ["case_id_for", "case_id_of"]
