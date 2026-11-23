"""v2.1z-bt: the name a result ZIP is received under (`sbmig.web.contract.zip_download_name`): `<project>-migration.zip`
and `<project>-remediation.zip` instead of `result.zip`; the run id stands in when no usable project name is known."""
from __future__ import annotations

import pytest

from sbmig.web.contract import ZIP_KINDS, zip_download_name

WEB_ID = "shop-api-20261123-101010-abc123"


@pytest.mark.parametrize("kind", ZIP_KINDS)
def test_project_name_first(kind):
    assert zip_download_name(WEB_ID, kind, "Shop_Api-2") == f"Shop_Api-2-{kind}.zip"


def test_migration_and_remediation_names():
    assert zip_download_name(WEB_ID, "migration", "shop-api") == "shop-api-migration.zip"
    assert zip_download_name(WEB_ID, "remediation", "shop-api") == "shop-api-remediation.zip"


@pytest.mark.parametrize("project", [None, "", "   ", "shop api", "../etc", "-x", "a\nb", "프로젝트", "a" * 82, 7])
def test_run_id_stands_in_for_a_missing_or_unsafe_project(project):
    assert zip_download_name(WEB_ID, "migration", project) == f"{WEB_ID}-migration.zip"


def test_cli_and_standalone_ids():
    assert zip_download_name("run-20261123-101010", "migration") == "run-20261123-101010-migration.zip"
    assert zip_download_name("r20261123-101010-0123456789ab", "remediation") == "r20261123-101010-0123456789ab-remediation.zip"


@pytest.mark.parametrize("run_id", ["", "../x", "a b", None])
def test_result_zip_when_nothing_fits(run_id):
    assert zip_download_name(run_id, "remediation", None) == "result.zip"


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        zip_download_name(WEB_ID, "result", "shop-api")
