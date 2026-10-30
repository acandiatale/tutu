"""v2.1r: a run that ended with a `file_lock` BLOCKED item gets `infra:file-lock` in `SBMIG-RESULT … attn=`, the way
repository failures get `infra:repository`; a green run never does."""
from __future__ import annotations

from sbmig.report.result_line import attention_codes


def test_file_lock_is_an_infra_attention_code():
    codes = attention_codes(exit_code=2, blocked_reasons=["file_lock"], unresolved=1)
    assert codes[0] == "infra:file-lock" and "blocked:file_lock" in codes and "unresolved:1" in codes
    assert attention_codes(flags=["infra:file-lock"], exit_code=2)[0] == "infra:file-lock"
    assert "infra:file-lock" not in attention_codes(exit_code=0, blocked_reasons=["file_lock"])
    # another infra kind already named keeps the first place
    assert attention_codes(flags=["infra:repository"], exit_code=2, blocked_reasons=["file_lock"])[0] == \
        "infra:repository"
