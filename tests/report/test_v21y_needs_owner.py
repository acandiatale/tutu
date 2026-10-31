"""v2.1y (prompt review E-M1): a case the LLM declined is BLOCKED `needs_owner` — the attention code `owner-decision`
(before the blocked codes, so `fleet status --attention` groups such projects) and the Korean rows of its suggestion
(the sibling "same cause as <case>: …" and the fallback of `compose_suggestion`)."""
from __future__ import annotations

from sbmig.core.models import BlockedItem, BlockReason, Phase
from sbmig.loop.blocked_registry import compose_suggestion
from sbmig.loop.fixloop import NEEDS_OWNER_KO
from sbmig.report.i18n import tr_line
from sbmig.report.result_line import OWNER_DECISION, attention_codes


def test_needs_owner_attention_code():
    codes = attention_codes(exit_code=2, blocked_reasons=["needs_owner", "needs_owner", "external_javax_api"],
                            unresolved=3)
    assert codes == [OWNER_DECISION, "blocked:needs_owner", "blocked:external_javax_api", "unresolved:3"]
    assert OWNER_DECISION == "owner-decision" and OWNER_DECISION not in attention_codes(exit_code=2,
                                                                                        blocked_reasons=["oscillation"])
    # an infra reason keeps the first place
    assert attention_codes(flags=["infra:repository"], exit_code=2, blocked_reasons=["needs_owner"])[:2] == \
        ["infra:repository", OWNER_DECISION]


def test_needs_owner_suggestions_in_korean():
    sentence = "iBATIS 2 has no Spring 7 support; the owning team must choose MyBatis or plain JDBC"
    sibling = f"same cause as 70cf5d17c7: {sentence} — {NEEDS_OWNER_KO}"
    assert tr_line("ko", sibling) == f"70cf5d17c7 와 같은 원인: {sentence} — 소유팀 결정 필요"
    assert tr_line("en", sibling) == sibling
    fallback = compose_suggestion(BlockedItem(id="b1", reason=BlockReason.NEEDS_OWNER, phase=Phase.VERIFY, cluster_id="c1"))
    ko = tr_line("ko", fallback)
    assert fallback.endswith("— 소유팀 결정 필요") and ko != fallback and ko.startswith("LLM 이 프로젝트 밖의 차단 사유")
    # the declined case's own suggestion is the LLM's sentence: left as it is
    own = f"{sentence} — {NEEDS_OWNER_KO}"
    assert tr_line("ko", own) == own
