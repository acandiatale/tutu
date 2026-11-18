"""Review 2026-11-18, the deploy/ side: W7 (the image's command line must not beat SBMIG_MAX_UPLOAD_MB from the ConfigMap),
the ConfigMap comment on what three build pods request, and the operator text for an image pushed with the same tag."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from sbmig.core.config import PACKAGE_ROOT

DEPLOY = PACKAGE_ROOT / "deploy"
K8S = DEPLOY / "k8s"
DOCKERFILE = DEPLOY / "docker" / "Dockerfile"


def _cmd() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    m = re.search(r"^CMD (\[.*?\])\s*$", text.replace("\\\n", " "), re.M | re.S)
    assert m, "Dockerfile has no exec-form CMD"
    return json.loads(m.group(1))


def _sbmig_env() -> dict:
    docs = [d for d in yaml.safe_load_all((K8S / "20-configmap.yaml").read_text(encoding="utf-8")) if d]
    return next(d for d in docs if d["metadata"]["name"] == "sbmig-env")["data"]


def test_the_upload_limit_of_the_configmap_takes_effect_in_the_pod(monkeypatch):
    """W7: `sbmig serve --max-upload-mb` wins over $SBMIG_MAX_UPLOAD_MB, so a CMD carrying it made the ConfigMap key dead."""
    from sbmig.web.app import resolve_max_upload

    cmd = _cmd()
    assert "--max-upload-mb" not in cmd, cmd
    assert _sbmig_env()["SBMIG_MAX_UPLOAD_MB"] == "1024"
    monkeypatch.setenv("SBMIG_MAX_UPLOAD_MB", "2048")              # what an operator raising the limit does
    assert resolve_max_upload(None) == 2048 * 1024 * 1024


def test_the_image_keeps_its_1024_mb_default_without_the_configmap():
    """`docker run` without the ConfigMap keeps the limit the CMD used to give (ENV, which a pod's env overrides)."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ENV SBMIG_MAX_UPLOAD_MB=1024\s*$", text, re.M)


def _memory_gi(value: str) -> int:
    assert value.endswith("Gi"), value
    return int(value[:-2])


def test_the_configmap_comment_on_build_pod_requests_matches_the_job_template():
    job = yaml.safe_load((K8S / "81-web-job.template.yaml").read_text(encoding="utf-8"))
    req = job["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    cpu, mem = int(req["cpu"]), _memory_gi(req["memory"])
    slots = int(_sbmig_env()["SBMIG_JOB_MAX_PARALLEL"])
    text = (K8S / "20-configmap.yaml").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "81 의 resources" in ln)
    assert f"CPU {cpu} / 메모리 {mem}Gi" in line, line
    assert f"CPU {cpu * slots} / 메모리 {mem * slots}Gi" in line, line


README = DEPLOY / "README.md"


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


def test_readme_section_1_says_what_to_do_after_pushing_the_same_tag():
    """W2 / R1 item 4: restart the web pod; build pods pull at start; the first time after switching the pull policy apply
    the Deployment; the Job template travels in the image (no ConfigMap step); a run uploaded in between is refused and
    uploaded again."""
    from sbmig.web.contract import IMAGE_DIFFERS_HINT

    s1 = _section(README.read_text(encoding="utf-8"), "## 1. 이미지 만들기", "### 1.1")
    assert "kubectl -n agent rollout restart deploy/sbmig" in s1
    assert "create configmap sbmig-job-template" not in s1 and "kubectl apply -f deploy/k8s/40-deployment.yaml" in s1
    assert "81-web-job.template.yaml" in s1
    assert "처음 한 번만" in s1 and "다시 올린다" in s1
    flat = " ".join(s1.split())
    assert " ".join(IMAGE_DIFFERS_HINT.split()) in flat
    for manifest in ("40-deployment.yaml", "81-web-job.template.yaml"):
        assert "README §1" in (K8S / manifest).read_text(encoding="utf-8")


def test_readme_says_what_the_admin_password_protects_in_open_mode():
    """W9: in open mode the admin password guards the aggregated views only; the per-run files are open."""
    admin = _section(README.read_text(encoding="utf-8"), "### 관리자 페이지", "### env 목록")
    assert "열린 모드" in admin and "result.zip" in admin and "sbmig.log" in admin and "SBMIG_WEB_TOKEN" in admin


def test_every_deploy_text_on_the_disk_floor_says_what_really_happens():
    """W4: `fleet.min_free_gb` is checked by the web pod before an upload (507), not by a build pod before a run."""
    texts = {p: p.read_text(encoding="utf-8") for p in (README, DEPLOY / "ENV.md", K8S / "20-configmap.yaml")}
    for p, text in texts.items():
        lines = [ln for ln in text.splitlines() if "MIN_FREE_GB" in ln and "configMapKeyRef" not in ln]
        assert lines, p
        joined = " ".join(lines) + " " + " ".join(text.splitlines()[text.splitlines().index(lines[-1]) + 1:][:1])
        assert "507" in joined and "실행 전 디스크 여유 하한" not in joined, (p, joined)
