"""IVAS client + scan procedure: no network, no sleeping (fake session, injected sleep / clock)."""
from __future__ import annotations

import hashlib
import json
import logging
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError

from sbmig.core.config import RemediationConfig
from sbmig.remediations import interfaces
from sbmig.remediations.ivas import (
    IvasHttpClient,
    IvasSettings,
    ivas_settings,
    project_name,
    readiness,
    run_scan,
)
from sbmig.remediations.models import IvasError, IvasUploadUncertain, UploadReceipt

BASE = "https://ivas.invalid/api"
TOKEN = "tok-SECRET-9f8e7d"
PASSWORD = "pw-SECRET-1234"
P, A = "proj-1", "anl-1"


def settings(**kw) -> IvasSettings:
    base = {"base_url": BASE, "user_id": "svc-user", "password": PASSWORD, "timeout_seconds": 5, "upload_timeout_seconds": 50,
                "poll_seconds": 10, "analysis_timeout_seconds": 100, "page_size": 2, "project_prefix": "sbmig"}
    base.update(kw)
    return IvasSettings(**base)


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            return json.loads(self.text)       # raises ValueError for non-JSON
        return self._body


class FakeSession:
    """Routes (method, path) to handlers. A handler is a Resp, an exception, or a callable(call) → Resp."""

    def __init__(self):
        self.calls: list[dict] = []
        self.routes: dict[tuple[str, str], list] = {}
        self.login_token = TOKEN

    def on(self, method, path, *responses):
        self.routes.setdefault((method, path), []).extend(responses)
        return self

    def request(self, method, url, **kw):
        assert url.startswith(BASE)
        path = url[len(BASE):]
        call = {"method": method, "path": path, **kw}
        data = kw.get("data")
        if data is not None and hasattr(data, "read"):
            call["body"] = None                 # filled by the handler / below
        self.calls.append(call)
        if method == "POST" and path == "/session" and (method, path) not in self.routes:
            return Resp(200, {"token": self.login_token})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {path} {kw.get('params')}")
        h = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(h) and not isinstance(h, Resp):
            h = h(call)
        if isinstance(h, BaseException):
            raise h
        if data is not None and hasattr(data, "read") and call["body"] is None:
            call["body"] = data.read()
        return h

    def count(self, method, path):
        return sum(1 for c in self.calls if c["method"] == method and c["path"] == path)


@pytest.fixture()
def zip_file(tmp_path) -> Path:
    p = tmp_path / "app.zip"
    p.write_bytes(b"PK\x03\x04" + b"x" * 5000)
    return p


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def status_path(p=P, a=A):
    return f"/projects/SELF/{p}/analysis/{a}/status"


def res(path):
    return f"/projects/SELF/{P}/analysis/{A}/result/{path}"


def summary_body(**totals):
    cats = ["components", "swComponents", "fileComponents", "codeSnippets", "file", "functionComponents",
            "ospkgComponents", "binaryComponents"]
    return {"summary": {"resultCount": {"vulnerabilities": {c: {"total": totals.get(c, 0)} for c in cats}}}}


def page(content, total):
    return Resp(200, {"content": content, "totalElements": total})


def lib_item(cve="CVE-2026-12345", ident="example:lib", version="1.0.0", score=8.0):
    return {"id": cve, "library": {"identifier": ident, "version": version, "info": {"patchedVersion": "1.0.1"}},
            "detectedLocations": [{"filePath": "pom.xml"}], "action": {"actionDivCd": "NE"},
            "rating": [{"version": "v2", "score": 5.0}, {"version": "v3", "score": score}]}


def file_item(cve="CVE-2026-76543", comp="comp-1"):
    return {"id": cve, "fileComponent": {"id": comp, "version": "1.0.0", "info": {}}, "action": {"actionDivCd": "NE"},
            "rating": []}


def parse_multipart(call) -> dict:
    ctype = call["headers"]["Content-Type"]
    raw = b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + call["body"]
    msg = BytesParser(policy=HTTP).parsebytes(raw)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = {"ctype": part.get_content_type(), "filename": part.get_filename(),
                       "payload": part.get_payload(decode=True)}
    return parts


def results_session(s: FakeSession, *, summary=None, library=None, sw=None, main=None, potential=None, matched=None):
    s.on("GET", res("summary"), Resp(200, summary or summary_body()))
    s.on("GET", res("libraryvulnerabilities"), *(library or [page([], 0)]))
    s.on("GET", res("swvulnerabilities"), *(sw or [page([], 0)]))
    s.on("GET", res("filecomponentvulnerabilities/main"), *(main or [page([], 0)]))
    s.on("GET", res("filecomponentvulnerabilities/potential"), *(potential or [page([], 0)]))
    for comp, pages in (matched or {}).items():
        s.on("GET", res(f"filecomponent/{comp}/matchedfiles"), *pages)
    return s


def receipt_for(zip_file):
    return UploadReceipt(project_id=P, analysis_id=A, zip_sha256=sha(zip_file), project_name="n")


def scan(s, zip_file, *, rec="auto", stop=None, clock=None, journal=None, **kw):
    client = IvasHttpClient(settings(), session=s, sleep=lambda _x: None)
    if rec == "auto":
        rec = receipt_for(zip_file)
    return run_scan(client, zip_file, sha(zip_file), "baseline", "sbmig-r1-baseline", receipt=rec, stop=stop,
                    journal=journal, settings=settings(**kw), sleep=lambda _x: None,
                    clock=clock or (lambda: 0.0))


# ---------------------------------------------------------------------------- settings / readiness
def test_settings_from_env_and_config():
    cfg = SimpleNamespace(remediation=RemediationConfig(ivas_poll_seconds=7, ivas_page_size=50,
                                                        ivas_analysis_timeout_minutes=2, ivas_project_prefix="x"))
    st = ivas_settings(cfg, env={"IVAS_BASE_URL": "https://ivas.invalid/", "IVAS_USER_ID": "u", "IVAS_PASSWORD": PASSWORD})
    assert st.base_url == "https://ivas.invalid" and st.poll_seconds == 7 and st.page_size == 50
    assert st.analysis_timeout_seconds == 120 and st.project_prefix == "x"
    assert PASSWORD not in repr(st)
    assert readiness(st) == []
    assert project_name(st, "r1", "baseline") == "x-r1-baseline"


def test_readiness_names_missing_values_only():
    probs = readiness(ivas_settings(None, env={}))
    assert probs == ["IVAS_BASE_URL is not set", "IVAS_USER_ID is not set", "IVAS_PASSWORD is not set"]
    st = ivas_settings(None, env={"IVAS_BASE_URL": "http://ivas.invalid", "IVAS_USER_ID": "u", "IVAS_PASSWORD": PASSWORD})
    probs = readiness(st)
    assert len(probs) == 1 and "https" in probs[0] and PASSWORD not in probs[0]
    assert readiness(settings(base_url="http://127.0.0.1:8080")) == []
    assert readiness(settings(base_url="http://localhost:9")) == []
    assert any("credentials" in p for p in readiness(settings(base_url="https://a:b@ivas.invalid")))


def test_client_satisfies_protocol():
    assert isinstance(IvasHttpClient(settings(), session=FakeSession()), interfaces.IvasClient)


# ---------------------------------------------------------------------------- login / token handling
def test_login_then_header_and_lazy():
    s = FakeSession().on("GET", status_path(), Resp(200, {"projectId": P, "lbrdrAnlId": A, "anlStatusDivCd": "DONE"}))
    c = IvasHttpClient(settings(), session=s)
    assert s.calls == []                                  # lazy
    assert c.status(P, A) == "DONE"
    assert c.status(P, A) == "DONE"
    login = s.calls[0]
    assert login["path"] == "/session" and login["json"] == {"userId": "svc-user", "password": PASSWORD}
    assert s.count("POST", "/session") == 1
    gets = [x for x in s.calls if x["method"] == "GET"]
    assert all(x["headers"]["X-API-KEY"] == TOKEN for x in gets)
    assert all(x["allow_redirects"] is False for x in s.calls)
    assert all("verify" not in x for x in s.calls)
    assert TOKEN not in repr(c) and PASSWORD not in repr(c)


def test_relogin_once_on_401(caplog):
    caplog.set_level(logging.DEBUG, logger="sbmig.remediations.ivas")
    s = FakeSession().on("GET", status_path(), Resp(401, text=f"expired {TOKEN}"),
                         Resp(200, {"anlStatusDivCd": "WAITING"}))
    c = IvasHttpClient(settings(), session=s)
    assert c.status(P, A) == "WAITING"
    assert s.count("POST", "/session") == 2
    assert TOKEN not in caplog.text and PASSWORD not in caplog.text


def test_second_401_is_error_without_secret(caplog):
    caplog.set_level(logging.DEBUG, logger="sbmig.remediations.ivas")
    s = FakeSession().on("GET", status_path(), Resp(403, text=f"denied token={TOKEN} pw={PASSWORD} " + "y" * 900))
    c = IvasHttpClient(settings(), session=s)
    with pytest.raises(IvasError) as ei:
        c.status(P, A)
    msg = str(ei.value)
    assert TOKEN not in msg and PASSWORD not in msg and "***" in msg and len(msg) < 500
    assert s.count("POST", "/session") == 2
    assert TOKEN not in caplog.text and PASSWORD not in caplog.text


def test_login_rejected_scrubs_password():
    s = FakeSession().on("POST", "/session", Resp(401, text=f"bad password {PASSWORD}"))
    c = IvasHttpClient(settings(), session=s)
    with pytest.raises(IvasError) as ei:
        c.status(P, A)
    assert ei.value.code == "login_failed" and PASSWORD not in str(ei.value)


def test_get_retries_transient_then_gives_up():
    sleeps = []
    s = FakeSession().on("GET", status_path(), Resp(503), requests.exceptions.ReadTimeout("t"),
                         Resp(200, {"anlStatusDivCd": "ANALYSIS"}))
    c = IvasHttpClient(settings(), session=s, sleep=sleeps.append)
    assert c.status(P, A) == "ANALYSIS"
    assert sleeps == [1.0, 2.0]
    s2 = FakeSession().on("GET", status_path(), Resp(429))
    with pytest.raises(IvasError) as ei:
        IvasHttpClient(settings(), session=s2, sleep=lambda _x: None).status(P, A)
    assert s2.count("GET", status_path()) == 3 and ei.value.code == "http_error"


def test_ids_are_quoted_path_segments():
    s = FakeSession().on("GET", "/projects/SELF/a%2Fb/analysis/c%20d/status", Resp(200, {"anlStatusDivCd": "DONE"}))
    assert IvasHttpClient(settings(), session=s).status("a/b", "c d") == "DONE"
    assert unquote("a%2Fb") == "a/b"


# ---------------------------------------------------------------------------- upload
def upload_ok(call):
    call["body"] = call["data"].read()
    return Resp(200, {"projectId": P, "lbrdAnlId": A})


def test_upload_multipart_shape_and_receipt(zip_file):
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", upload_ok)
    c = IvasHttpClient(settings(), session=s)
    r = c.upload(zip_file, "sbmig-r1-baseline")
    assert r == UploadReceipt(project_id=P, analysis_id=A, zip_sha256=sha(zip_file), project_name="sbmig-r1-baseline")
    post = [x for x in s.calls if x["path"] == "/projects/SELF/sourcecode"]
    assert len(post) == 1
    call = post[0]
    assert call["headers"]["X-API-KEY"] == TOKEN
    assert int(call["headers"]["Content-Length"]) == len(call["body"])
    assert not isinstance(call["data"], (bytes, str))      # streamed object, not a loaded body
    assert call["timeout"] == (5, 50)
    parts = parse_multipart(call)
    assert json.loads(parts["analysisRequest"]["payload"]) == {"projectNm": "sbmig-r1-baseline",
                                                               "source": {"type": "ZIP_FILE"}}
    assert parts["analysisRequest"]["ctype"] == "application/json"
    assert parts["file"]["filename"] == "app.zip" and parts["file"]["payload"] == zip_file.read_bytes()


def test_upload_logs_in_fresh_each_time(zip_file):
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", upload_ok)
    s.on("GET", status_path(), Resp(200, {"anlStatusDivCd": "DONE"}))
    c = IvasHttpClient(settings(), session=s)
    c.status(P, A)
    c.upload(zip_file, "n")
    assert s.count("POST", "/session") == 2


def test_upload_analysis_id_alias(zip_file):
    s = FakeSession().on("POST", "/projects/SELF/sourcecode",
                         lambda call: (call.__setitem__("body", call["data"].read()),
                                       Resp(200, {"projectId": 7, "analysisId": 9}))[1])
    r = IvasHttpClient(settings(), session=s).upload(zip_file, "n")
    assert (r.project_id, r.analysis_id) == ("7", "9")


def partial_then(exc):
    def h(call):
        call["data"].read(100)
        raise exc
    return h


def connect_refused():
    nce = NewConnectionError(None, "refused")
    return requests.exceptions.ConnectionError(MaxRetryError(None, "/x", reason=nce))


@pytest.mark.parametrize("handler", [
    requests.exceptions.ReadTimeout("read timed out"),
    partial_then(requests.exceptions.ConnectionError("Connection reset by peer")),
    requests.exceptions.ConnectionError("ambiguous"),           # no connect-failure cause → uncertain
    partial_then(requests.exceptions.ChunkedEncodingError("x")),
    Resp(500, text="boom"),
    Resp(502),
    Resp(302),
    Resp(200, text="<html>ok</html>"),
    Resp(200, {"projectId": P}),
    Resp(201, {"lbrdAnlId": A}),
    Resp(200, ["not", "an", "object"]),
], ids=["read-timeout", "reset", "conn-ambiguous", "chunked", "500", "502", "302", "2xx-html", "no-analysis-id",
        "no-project-id", "2xx-list"])
def test_upload_uncertain_paths_never_retry(zip_file, handler, caplog):
    caplog.set_level(logging.DEBUG, logger="sbmig.remediations.ivas")
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", handler)
    c = IvasHttpClient(settings(), session=s, sleep=lambda _x: None)
    with pytest.raises(IvasUploadUncertain) as ei:
        c.upload(zip_file, "proj-name")
    assert s.count("POST", "/projects/SELF/sourcecode") == 1
    assert ei.value.details["project_name"] == "proj-name"
    assert TOKEN not in str(ei.value) and TOKEN not in caplog.text


@pytest.mark.parametrize("handler,code", [
    (requests.exceptions.ConnectTimeout("connect timed out"), "upload_not_sent"),
    (connect_refused(), "upload_not_sent"),
    (Resp(400, text="bad request"), "upload_rejected"),
    (Resp(401, text=f"token {TOKEN} invalid"), "upload_rejected"),
    (Resp(413), "upload_rejected"),
], ids=["connect-timeout", "refused", "400", "401", "413"])
def test_upload_clear_failures_are_ivas_error(zip_file, handler, code):
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", handler)
    c = IvasHttpClient(settings(), session=s)
    with pytest.raises(IvasError) as ei:
        c.upload(zip_file, "n")
    assert not isinstance(ei.value, IvasUploadUncertain)
    assert ei.value.code == code and TOKEN not in str(ei.value)
    assert s.count("POST", "/projects/SELF/sourcecode") == 1
    assert s.count("POST", "/session") == 1                  # no re-login + retry for the upload


def test_upload_unreadable_file_and_login_failure(tmp_path, zip_file):
    s = FakeSession()
    with pytest.raises(IvasError) as ei:
        IvasHttpClient(settings(), session=s).upload(tmp_path / "missing.zip", "n")
    assert ei.value.code == "upload_not_sent" and s.calls == []
    s2 = FakeSession().on("POST", "/session", Resp(401))
    with pytest.raises(IvasError) as ei:
        IvasHttpClient(settings(), session=s2).upload(zip_file, "n")
    assert not isinstance(ei.value, IvasUploadUncertain) and s2.count("POST", "/projects/SELF/sourcecode") == 0


# ---------------------------------------------------------------------------- status / summary / page
def test_status_id_mismatch():
    for body in ({"projectId": "other", "anlStatusDivCd": "DONE"}, {"lbrdrAnlId": "other", "anlStatusDivCd": "DONE"},
                 {"lbrdAnlId": "x", "anlStatusDivCd": "DONE"}):
        s = FakeSession().on("GET", status_path(), Resp(200, body))
        with pytest.raises(IvasError) as ei:
            IvasHttpClient(settings(), session=s).status(P, A)
        assert ei.value.code == "id_mismatch"


def test_summary_flattens_and_rejects_missing():
    s = FakeSession().on("GET", res("summary"), Resp(200, summary_body(components=1)))
    out = IvasHttpClient(settings(), session=s).summary(P, A)
    assert out["components"] == 1 and len(out) == 8 and sum(out.values()) == 1
    bad = summary_body()
    del bad["summary"]["resultCount"]["vulnerabilities"]["binaryComponents"]
    s2 = FakeSession().on("GET", res("summary"), Resp(200, bad))
    with pytest.raises(IvasError):
        IvasHttpClient(settings(), session=s2).summary(P, A)


def test_page_shapes():
    s = FakeSession().on("GET", res("filecomponent/comp-1/matchedfiles"), page([{"file": {"fullPath": "a.js"}}], 1))
    s.on("GET", res("libraryvulnerabilities"), Resp(200, {"content": [], "totalElements": "0"}))
    c = IvasHttpClient(settings(), session=s)
    pg = c.page(P, A, "matched_files", 0, 100, component_id="comp-1")
    assert pg == interfaces.Page(content=[{"file": {"fullPath": "a.js"}}], total=1)
    assert s.calls[-1]["params"] == {"page": 0, "size": 100}
    with pytest.raises(IvasError):
        c.page(P, A, "library", 0, 100)
    with pytest.raises(IvasError):
        c.page(P, A, "matched_files", 0, 100)


# ---------------------------------------------------------------------------- run_scan
def test_complete_zero_finding_scan_with_upload(zip_file):
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", upload_ok)
    s.on("GET", status_path(), Resp(200, {"anlStatusDivCd": "PREPARING"}), Resp(200, {"anlStatusDivCd": "WAITING"}),
         Resp(200, {"anlStatusDivCd": "ANALYSIS"}), Resp(200, {"anlStatusDivCd": "DONE"}))
    results_session(s)
    receipts, events, sleeps = [], [], []
    client = IvasHttpClient(settings(), session=s, sleep=lambda _x: None)
    r = run_scan(client, zip_file, sha(zip_file), "baseline", "sbmig-r1-baseline", on_receipt=receipts.append,
                 journal=events.append, settings=settings(), sleep=sleeps.append, clock=lambda: 0.0)
    assert r.complete is True and r.incomplete_reasons == [] and r.findings == []
    assert r.status_code == "DONE" and r.receipt.analysis_id == A and len(receipts) == 1
    assert sleeps == [10, 10, 10]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "ivas_upload" and kinds.count("ivas_status") == 4 and kinds[-1] == "ivas_scan_done"
    assert TOKEN not in json.dumps(events)
    assert s.count("POST", "/projects/SELF/sourcecode") == 1


def test_resume_with_receipt_uploads_nothing(zip_file):
    s = results_session(FakeSession().on("GET", status_path(), Resp(200, {"anlStatusDivCd": "DONE"})))
    r = scan(s, zip_file)
    assert r.complete and s.count("POST", "/projects/SELF/sourcecode") == 0


def test_zip_hash_mismatch_makes_no_remote_call(zip_file):
    s = FakeSession()
    rec = UploadReceipt(project_id=P, analysis_id=A, zip_sha256="0" * 64)
    r = scan(s, zip_file, rec=rec)
    assert not r.complete and r.incomplete_reasons == ["zip_hash_mismatch"] and s.calls == []


def test_upload_uncertain_propagates_once(zip_file):
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", requests.exceptions.ReadTimeout("t"))
    receipts = []
    with pytest.raises(IvasUploadUncertain):
        run_scan(IvasHttpClient(settings(), session=s), zip_file, sha(zip_file), "baseline", "n", on_receipt=receipts.append,
            settings=settings(), sleep=lambda _x: None, clock=lambda: 0.0)
    assert receipts == [] and s.count("POST", "/projects/SELF/sourcecode") == 1


def test_clear_upload_failure_is_incomplete_result(zip_file):
    s = FakeSession().on("POST", "/projects/SELF/sourcecode", Resp(400))
    r = run_scan(IvasHttpClient(settings(), session=s), zip_file, sha(zip_file), "baseline", "n", settings=settings(),
                 sleep=lambda _x: None, clock=lambda: 0.0)
    assert not r.complete and r.incomplete_reasons == ["http_error"]


@pytest.mark.parametrize("state", ["FAILED", "ERROR", "CANCELED", "CANCELLED"])
def test_failed_states(zip_file, state):
    s = FakeSession().on("GET", status_path(), Resp(200, {"anlStatusDivCd": "ANALYSIS"}), Resp(200, {"anlStatusDivCd": state}))
    r = scan(s, zip_file)
    assert not r.complete and r.incomplete_reasons == ["analysis_failed"] and r.status_code == state


def test_analysis_timeout(zip_file):
    t = iter(range(0, 1000, 30))
    s = FakeSession().on("GET", status_path(), Resp(200, {"anlStatusDivCd": "ANALYSIS"}))
    r = scan(s, zip_file, clock=lambda: float(next(t)))
    assert r.incomplete_reasons == ["analysis_timeout"] and not r.complete
    assert "not cancelled" in " ".join(r.notes)


def test_stop_keeps_remote_running(zip_file):
    flags = iter([False, False, True])
    s = FakeSession().on("GET", status_path(), Resp(200, {"anlStatusDivCd": "WAITING"}))
    r = scan(s, zip_file, stop=lambda: next(flags))
    assert r.incomplete_reasons == ["stop_requested"]
    text = " ".join(r.notes)
    assert "keeps running" in text and "not cancelled" in text
    assert s.count("GET", status_path()) == 2


def test_unknown_status_kept_polling(zip_file):
    s = results_session(FakeSession().on("GET", status_path(), Resp(200, {"anlStatusDivCd": "QUEUEING"}),
                                         Resp(200, {"anlStatusDivCd": "DONE"})))
    r = scan(s, zip_file)
    assert r.complete and any("QUEUEING" in n for n in r.notes)


def test_status_id_mismatch_in_scan(zip_file):
    s = FakeSession().on("GET", status_path(), Resp(200, {"projectId": "zzz", "anlStatusDivCd": "DONE"}))
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["id_mismatch"]


def done(s):
    return s.on("GET", status_path(), Resp(200, {"anlStatusDivCd": "DONE"}))


def test_multi_page_library(zip_file):
    items = [lib_item(f"CVE-2026-{i}") for i in range(5)]
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[page(items[0:2], 5), page(items[2:4], 5), page(items[4:], 5)])
    r = scan(s, zip_file)
    assert r.complete, r.notes
    assert [f.id for f in r.findings] == sorted(f"CVE-2026-{i}" for i in range(5))
    f = r.findings[0]
    assert (f.kind, f.identifier, f.version, f.patched_version, f.action, f.score, f.locations) == \
        ("library", "example:lib", "1.0.0", "1.0.1", "NE", 8.0, ["pom.xml"])
    params = [c["params"]["page"] for c in s.calls if c["path"] == res("libraryvulnerabilities")]
    assert params == [0, 1, 2]
    assert r.counts["components"] == 1


def test_file_components_with_matched_files_pagination(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(fileComponents=2),
                        main=[page([file_item("CVE-1", "comp-1"), file_item("CVE-2", "comp-1")], 2)],
                        potential=[page([file_item("CVE-3", "comp 2")], 1)],
                        matched={"comp-1": [page([{"file": {"fullPath": "one/vendor.js"}},
                                                  {"file": {"fullPath": "two/vendor.js"}}], 3),
                                            page([{"file": {"fullPath": "three/vendor.js"}}], 3)],
                                 "comp%202": [page([{"file": {"fullPath": "x.js"}}], 1)]})
    r = scan(s, zip_file)
    assert r.complete, r.notes
    by = {f.id: f for f in r.findings}
    assert by["CVE-1"].locations == ["one/vendor.js", "two/vendor.js", "three/vendor.js"]
    assert by["CVE-1"].kind == "file_component" and by["CVE-1"].component_id == "comp-1"
    assert by["CVE-1"].identifier == "comp-1"
    assert by["CVE-3"].kind == "file_component_potential" and by["CVE-3"].locations == ["x.js"]
    # matched files fetched once per component
    assert s.count("GET", res("filecomponent/comp-1/matchedfiles")) == 2


def test_sw_items_are_unsupported(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(swComponents=1),
                        sw=[page([{"id": "CVE-SW-1"}], 1)])
    r = scan(s, zip_file)
    assert not r.complete and r.incomplete_reasons == ["unsupported_sw"] and r.findings == []


def test_page_missing_on_error(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[page([lib_item("A"), lib_item("B")], 3), Resp(404)])
    r = scan(s, zip_file)
    assert not r.complete
    assert "page_missing" in r.incomplete_reasons and "http_error" in r.incomplete_reasons
    assert len(r.findings) == 2                               # kept, but not "complete"


def test_page_missing_on_early_empty_page(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[page([lib_item("A"), lib_item("B")], 4), page([], 4)])
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["page_missing"]


def test_total_mismatch_when_total_changes(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[page([lib_item("A"), lib_item("B")], 3), page([lib_item("C")], 4), page([], 4)])
    r = scan(s, zip_file)
    assert "total_mismatch" in r.incomplete_reasons


def test_total_mismatch_when_more_items_than_total(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[page([lib_item("A"), lib_item("B")], 1)])
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["total_mismatch"]


def test_duplicate_result_across_pages(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[page([lib_item("A"), lib_item("B")], 3), page([lib_item("A")], 3)])
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["duplicate_result"] and len(r.findings) == 2


def test_duplicate_matched_file(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(fileComponents=1), main=[page([file_item()], 1)],
                        matched={"comp-1": [page([{"file": {"fullPath": "a.js"}}, {"file": {"fullPath": "a.js"}}], 2)]})
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["duplicate_result"]


def test_page_safety_cap(zip_file):
    # every page is short (1 item for size 2) → the safety cap stops the walk
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[lambda call: page([lib_item(f"X{call['params']['page']}")], 1000)])
    r = scan(s, zip_file, page_size=2)
    assert "page_missing" in r.incomplete_reasons
    assert s.count("GET", res("libraryvulnerabilities")) == 502   # 1000/2 + 2


def test_summary_mismatch(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(components=0), library=[page([lib_item()], 1)])
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["summary_mismatch"]
    assert any("compared zero / non-zero only" in n for n in r.notes)


def test_summary_category_without_endpoint(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(binaryComponents=2))
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["summary_mismatch"]


def test_summary_unreadable_is_http_error(zip_file):
    s = results_session(done(FakeSession()))
    s.routes[("GET", res("summary"))] = [Resp(200, {"summary": {}})]
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["http_error"] and not r.complete


def test_malformed_item_is_not_dropped_silently(zip_file):
    s = results_session(done(FakeSession()), summary=summary_body(components=1),
                        library=[page([lib_item("A"), {"library": {"identifier": "x:y"}}], 2)])
    r = scan(s, zip_file)
    assert r.incomplete_reasons == ["http_error"] and any("malformed" in n for n in r.notes)


def test_stop_before_upload_sends_nothing(zip_file):
    s = FakeSession()
    r = run_scan(IvasHttpClient(settings(), session=s), zip_file, sha(zip_file), "baseline", "n", stop=lambda: True,
                 settings=settings(), sleep=lambda _x: None, clock=lambda: 0.0)
    assert r.incomplete_reasons == ["stop_requested"] and s.calls == []


def test_request_errors_before_sending_are_ivas_errors_not_crashes():
    class Broken(FakeSession):
        def request(self, method, url, **kw):
            self.calls.append({"method": method, "url": url})
            raise requests.exceptions.MissingSchema("Invalid URL '/session': No scheme supplied")

    c = IvasHttpClient(settings(), session=Broken())
    with pytest.raises(IvasError) as e:
        c.status(P, A)
    assert e.value.code == "login_failed" and PASSWORD not in str(e.value)

    s = FakeSession()

    def invalid(call):
        raise requests.exceptions.InvalidURL("bad url")
    s.on("GET", status_path(), invalid)
    with pytest.raises(IvasError) as e:
        IvasHttpClient(settings(), session=s).status(P, A)
    assert e.value.code == "http_error"
