"""케이스 ID 함수 ``case_id_for`` / ``case_id_of``를 다시 내보내는 모듈. 실제 정의는 :mod:`sbmig.errors.common`에 있다.

``sbmig.triage.cluster``, tests and scripts/e2e.py import them from this path.

``case_id_for(l2)`` = ``sha1(l2)[:10]``; the cases themselves live in ``<kb>/cases/<case_id>/``
(:mod:`sbmig.errors.cases`)."""
from sbmig.errors.common import case_id_for, case_id_of

__all__ = ["case_id_for", "case_id_of"]
