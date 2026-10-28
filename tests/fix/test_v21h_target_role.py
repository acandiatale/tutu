"""v2.1h fixer fixes (docs/v2/HANDOVER-FINDINGS-2026-10-28.md): A20 the prompt's migration target comes from
`cfg.target` (not a constant), A21 `verify_guide` is a member of `LLMRole` — adapters receive it as is and the replay
provider answers it with a `replay_role: verify_guide` entry (a `replay_role: fix` entry never does)."""
from __future__ import annotations

import json
from typing import get_args

from sbmig.core.interfaces import LLMResponse, LLMRole
from sbmig.core.models import CaseType, PomInfo, ProjectProfile
from sbmig.fix import ContextPacker, FakeLLMClient, LLMFixer, make_llm_client, profile_summary
from sbmig.fix.client import NO_FIX_RESPONSE, ReplayEntry, ReplayResponder, load_replay_entries
from sbmig.fix.context import TARGET_MARK, target_text, with_target
from sbmig.llm import AdapterClient, LLMAdapter

from .test_fix_script_context import case, errs_for, sctx
from .test_v21d_verify import GUIDE, cluster, ctx, major, proj  # noqa: F401 - proj is a fixture

PROFILE = ProjectProfile(root="/x", name="demo", case_type=CaseType.SPRING_BOOT, java_version="1.8",
                         poms=[PomInfo(path="pom.xml", artifact_id="demo")])


def retargeted(cfg):
    c = cfg.model_copy(deep=True)
    c.target.java_version, c.target.spring_boot_version = "25", "4.2.0"
    return c


# ---------------------------------------------------------------------------------------------- A20
def test_profile_summary_target_follows_the_config(cfg):
    assert profile_summary(PROFILE).endswith(TARGET_MARK + "JDK 21, Spring Boot 4.1.0")      # TargetConfig defaults
    assert profile_summary(PROFILE, retargeted(cfg)).endswith(" | target: JDK 25, Spring Boot 4.2.0")
    assert target_text(None) == target_text(cfg) == "JDK 21, Spring Boot 4.1.0"


def test_with_target_rewrites_only_the_tail(cfg):
    c = retargeted(cfg)
    s = profile_summary(PROFILE)                         # built without the config (the loop does this)
    assert with_target(s, c) == s.rsplit(TARGET_MARK, 1)[0] + " | target: JDK 25, Spring Boot 4.2.0"
    assert with_target("case=spring_boot java=17", c) == "case=spring_boot java=17"
    assert with_target("", c) == ""


def test_the_script_prompt_and_the_verify_prompt_carry_the_configured_target(cfg, ws, put, proj):  # noqa: F811
    c = retargeted(cfg)
    f = "app/src/main/java/com/example/web/C.java"
    put(f, "package com.example.web;\nclass C {}\n")
    summary = profile_summary(PROFILE)
    text = ContextPacker(c).pack_script(ws, case(errs_for(f, [2])), sctx(profile_summary=summary)).render()
    assert "target: JDK 25, Spring Boot 4.2.0" in text and "Spring Boot 4.1.0" not in text
    msgs = LLMFixer(c, FakeLLMClient([GUIDE])).build_verify_messages(cluster(), ctx(profile_summary=summary),
                                                                      [major()], "")
    assert "## Project\n" in msgs[1].content and "target: JDK 25, Spring Boot 4.2.0" in msgs[1].content


# ---------------------------------------------------------------------------------------------- A21
class Capture(LLMAdapter):
    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return LLMResponse(content=f"<verify>{GUIDE}</verify>", model="m")


def test_verify_guide_is_an_llm_role_adapters_receive_as_is(cfg, proj):  # noqa: F811
    assert "verify_guide" in get_args(LLMRole)
    a = Capture()
    out = LLMFixer(cfg, AdapterClient(a)).verify_guide(proj, cluster(), ctx(), [major()], "")
    assert out == GUIDE
    [req] = a.requests
    assert req.role == "verify_guide" and "role_detail" not in req.metadata
    assert req.metadata["purpose"] == "verify_guide"


def test_replay_answers_verify_guide_only_with_its_own_role():
    fix = ReplayEntry("<analysis>fix</analysis>", role="fix", name="llm-001.json")
    guide = ReplayEntry(GUIDE, role="verify_guide", name="llm-002.json")
    r = ReplayResponder([fix])
    assert r([], role="verify_guide") == NO_FIX_RESPONSE and r([], role="fix_escalation") == fix.response
    r = ReplayResponder([fix, guide])
    assert r([], role="verify_guide") == GUIDE and r([], role="fix") == fix.response
    assert r.served == ["llm-002.json", "llm-001.json"]


def test_replay_dir_with_a_verify_guide_entry_drives_the_fixer(cfg, tmp_path, proj):  # noqa: F811
    d = tmp_path / "replay"
    d.mkdir()
    (d / "llm-001.json").write_text(json.dumps({"response": "<analysis>x</analysis>", "replay_role": "fix",
                                                "match": "case: abc123"}), encoding="utf-8")
    (d / "llm-002.json").write_text(json.dumps({"response": f"<verify>{GUIDE}</verify>",
                                                "replay_role": "verify_guide"}), encoding="utf-8")
    assert [e.role for e in load_replay_entries(d)] == ["fix", "verify_guide"]
    c = cfg.model_copy(deep=True)
    c.llm.provider, c.llm.base_url = "fake", f"replay:{d}"
    assert LLMFixer(c, make_llm_client(c)).verify_guide(proj, cluster(), ctx(), [major()], "") == GUIDE
