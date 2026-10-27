from __future__ import annotations

import pytest

from sbmig.core.models import CaseType, PomInfo, ProjectProfile


def make_profile(poms=None, libs=(), boot="2.7.18", case=CaseType.SPRING_BOOT, root="/x") -> ProjectProfile:
    from sbmig.core.models import LibraryUsage
    poms = poms or [PomInfo(path="pom.xml", artifact_id="demo", is_root=True)]
    return ProjectProfile(root=root, name="demo", case_type=case, poms=poms, root_pom=poms[0],
                          libraries=[LibraryUsage(name=n, version=v) for n, v in libs], spring_boot_version=boot)


@pytest.fixture()
def profile() -> ProjectProfile:
    return make_profile()
