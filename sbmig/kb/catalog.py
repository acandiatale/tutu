"""케이스 ID 함수 ``case_id_for`` / ``case_id_of``를 이 경로로 import하는 코드를 위해 다시 내보내는 모듈.
``sbmig.triage.cluster``, 테스트, scripts/e2e.py가 여기서 가져간다. 실제 정의는 :mod:`sbmig.errors.common`에 있다.

``case_id_for(l2)`` = ``sha1(l2)[:10]``; the cases themselves live in ``<kb>/cases/<case_id>/``
(:mod:`sbmig.errors.cases`)."""
from sbmig.errors.common import case_id_for, case_id_of

__all__ = ["case_id_for", "case_id_of"]
