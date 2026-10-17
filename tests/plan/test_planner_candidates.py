from types import SimpleNamespace

from sbmig.core.models import CaseType
from sbmig.plan import build_plan
from tests.analyze.helpers import *  # noqa: F401,F403  (project builders)


class _KB:
    def __init__(self, active=(), candidates=()):
        self._a, self._c = list(active), list(candidates)
    def active_recipe_ids(self): return [i for i, _ in self._a]
    def active_recipe_files(self): return [f for _, f in self._a]
    def list_recipes(self, status=None):
        rows = [SimpleNamespace(id=i, path=f, status="active") for i, f in self._a]
        if status in (None, "candidate"):
            rows += [SimpleNamespace(id=i, path=f, status="candidate") for i, f in self._c]
        return [r for r in rows if status is None or r.status == status]


def _boot_profile():
    from sbmig.core.models import PomInfo, ProjectProfile, SourceStats
    return ProjectProfile(root="/x", name="p", case_type=CaseType.SPRING_BOOT, spring_boot_version="3.3.13",
                          root_pom=PomInfo(path="pom.xml", artifact_id="p", is_root=True), source=SourceStats(java_files=1),
                          fingerprint="abc")


def test_candidates_are_tried_in_plan_and_can_be_disabled(cfg):
    kb = _KB(active=[("com.company.kb.Active", "/kb/a.yml")], candidates=[("com.company.kb.Cand", "/kb/c.yml")])
    plan = build_plan(_boot_profile(), cfg, kb)
    spring = next(s for s in plan.steps if s.id == "spring.rewrite")
    ids = [r.id for r in spring.recipes]
    assert "com.company.kb.Active" in ids and "com.company.kb.Cand" in ids
    assert ids.index("com.company.kb.Active") < ids.index("com.company.kb.Cand")
    cand = next(r for r in spring.recipes if r.id == "com.company.kb.Cand")
    assert cand.source == "kb" and cand.file == "/kb/c.yml"
    assert any("candidate" in n for n in plan.notes)
    cfg.kb.try_candidates_in_plan = False
    plan2 = build_plan(_boot_profile(), cfg, kb)
    ids2 = [r.id for r in next(s for s in plan2.steps if s.id == "spring.rewrite").recipes]
    assert "com.company.kb.Cand" not in ids2 and "com.company.kb.Active" in ids2
