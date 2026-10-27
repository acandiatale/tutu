"""Kept for the importers of the v1/v2 case catalog id (``sbmig.triage.cluster``, tests, scripts/e2e.py): the
catalog itself is gone in v2.1 (docs/v2/PLAN-v2.1.md §3.4) — cases live in ``<kb>/cases/<case_id>/``
(:mod:`sbmig.errors.cases`). ``case_id_for(l2)`` = ``sha1(l2)[:10]``, defined in :mod:`sbmig.errors.common`."""
from sbmig.errors.common import case_id_for, case_id_of

__all__ = ["case_id_for", "case_id_of"]
