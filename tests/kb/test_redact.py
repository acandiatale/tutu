"""sbmig.kb.redact (round 10, C1): secret VALUES masked by context, code never mangled, company rules from yml, and
what the KB does with masked text (pairs dropped, never a rule candidate, patch cases hint-only)."""
from __future__ import annotations

import pytest

from sbmig.core.models import FileChange
from sbmig.kb import prepare_error_cases, sanitize_case
from sbmig.kb.changes import classify_diff_ex
from sbmig.kb.harvest import NO_REPLAY_TAG, redact
from sbmig.kb.redact import Redactor, get_redactor, key_words, load_rules

from .helpers import EC_FILE, ec_attempt, ec_error, make_attempt, with_kb

MASKED = [
    ("<password>s3cr3t</password>", "<password>***</password>"),
    ('<property name="password" value="s3cr3t"/>', '<property name="password" value="***"/>'),
    ('<entry key="db.password" value="s3cr3t"/>', '<entry key="db.password" value="***"/>'),
    ('"password": "s3cr3t"', '"password": "***"'),
    ("{'apiKey': 'abc123'}", "{'apiKey': '***'}"),
    ("Authorization: Bearer eyJhbGciOi.abc.def", "Authorization: ***"),
    ("curl -H 'X: y' Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig", "curl -H 'X: y' Bearer ***"),
    ("jdbc:oracle:thin:scott/tiger@db:1521:ORCL", "jdbc:oracle:thin:scott/***@db:1521:ORCL"),
    ("jdbc:sqlserver://h;databaseName=d;user=sa;password=Secret1;", "jdbc:sqlserver://h;databaseName=d;user=sa;password=***;"),
    ("jdbc:mysql://h/db?user=x&password=y&ssl=true", "jdbc:mysql://h/db?user=x&password=***&ssl=true"),
    ("https://deploy:Pa55w0rd@nexus.corp/repo", "https://deploy:***@nexus.corp/repo"),
    ("spring.datasource.password=s3cr3t", "spring.datasource.password=***"),
    ("db.pw=s3cr3t", "db.pw=***"),
    ("passwd=s3cr3t", "passwd=***"),
    ("user_pw=abc def ghi", "user_pw=***"),
    ("credentials: hunter2", "credentials: ***"),
    ("+  password: hunter2 # set by ops", "+  password: *** # set by ops"),
    ("-spring.datasource.password=old", "-spring.datasource.password=***"),
    ("[ERROR] /x/A.java password=hunter2 rest", "[ERROR] /x/A.java password=*** rest"),
    ("mvn -Dspring.datasource.password=abc -Dfoo=bar", "mvn -Dspring.datasource.password=*** -Dfoo=bar"),
    ('    private static final String PASSWORD = "tiger";', '    private static final String PASSWORD = "***";'),
    ('<Resource name="jdbc/x" username="u" password="tiger" />', '<Resource name="jdbc/x" username="u" password="***" />'),
    ('String s = "password=abc";', 'String s = "password=***";'),
    ("X-Api-Key: abcdef123", "X-Api-Key: ***"),
    ("password=P@ss(w0rd)!", "password=***"),
    ("<param-name>db.password</param-name>\n+  <param-value>tiger</param-value>",
     "<param-name>db.password</param-name>\n+  <param-value>***</param-value>"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
     "-----BEGIN RSA PRIVATE KEY-----\n***\n-----END RSA PRIVATE KEY-----"),
]

UNCHANGED = [
    "    if (secret == null) return;",
    "    if (token != null && password.equals(x)) {",
    "    String token = helper.clientIp();",
    '    String token = request.getHeader("X-Token");',
    "    password = getPassword();",
    "    this.password = password;",
    "    token = other;",
    "    password = new String(chars);",
    "    password = p1 + p2;",
    "    for (String token : tokens) {",
    "    x = flag ? token : other;",
    '    private static final String API_KEY_HEADER = "X-Api-Key";',
    "    int tokenCount = 5;",
    '    log.info("password={}", pw);',
    'secret += "x";',
    "  symbol:   variable token",
    "Basic authentication is used",
    "+  password: ${DB_PASSWORD}",
    "spring.datasource.password=__SET_ME__",
    '<property name="password" value="${jdbc.password}"/>',
    '"https://x/api?token=" + tok',
]


@pytest.mark.parametrize("text,want", MASKED)
def test_secret_values_are_masked_by_context(text, want):
    assert redact(text) == want


@pytest.mark.parametrize("text", UNCHANGED)
def test_code_comparisons_and_expressions_are_never_mangled(text):
    assert redact(text) == text


def test_key_words_and_secret_keys():
    assert key_words("spring.datasource.password") == ["spring", "datasource", "password"]
    assert key_words("userPw") == ["user", "pw"] and key_words("API_KEY_HEADER") == ["api", "key", "header"]
    assert key_words("PASSWORD2") == ["password"]
    r = Redactor()
    assert r.is_secret_key("DB_PASSWORD") and r.is_secret_key("clientSecret") and r.is_secret_key("x-api-key")
    assert not r.is_secret_key("API_KEY_HEADER") and not r.is_secret_key("password.encoder")
    assert not r.is_secret_key("tokenCount") and not r.is_secret_key("bypass")


def test_redact_ex_reports_the_masked_values():
    text, values = Redactor().redact_ex("a\npassword=one\ntoken: two\n")
    assert text == "a\npassword=***\ntoken: ***\n" and values == ["one", "two"]
    assert Redactor().redact_ex(None) == (None, [])


def test_company_rules_from_the_redact_yml(tmp_path, kb_cfg):
    rules = tmp_path / "redact.yml"
    rules.write_text(
        "key_words: [otp]\n"
        "key_patterns: ['acme\\.sso\\.client-ref']\n"
        "patterns:\n"
        "  - name: acme-token\n"
        "    regex: 'ACME-[A-Z0-9]{8}'\n"
        "  - {name: cookie, regex: 'ACME_SSO=(?P<value>[^;\\s]+)'}\n"
        "  - {name: broken, regex: '([unclosed'}\n"
        "allow_keys: [csrf.header.token]\n", encoding="utf-8")
    red = Redactor(load_rules(rules))
    assert red.redact("login.otp=123456") == "login.otp=***"
    assert red.redact("acme.sso.client-ref=xyz") == "acme.sso.client-ref=***"
    assert red.redact("header ACME-AB12CD34 sent") == "header *** sent"
    assert red.redact("Cookie: ACME_SSO=deadbeef; path=/") == "Cookie: ACME_SSO=***; path=/"
    assert red.redact("csrf.header.token=X-CSRF") == "csrf.header.token=X-CSRF"
    assert Redactor().redact("login.otp=123456") == "login.otp=123456"          # not a built-in word
    cfg = with_kb(kb_cfg, redact_file=str(rules))
    assert redact("login.otp=123456", cfg) == "login.otp=***"
    assert get_redactor(cfg) is get_redactor(cfg)                               # cached by the file stamp


def test_the_shipped_redact_yml_loads_and_adds_nothing_by_default(cfg):
    path = cfg.resolve_path(cfg.kb.redact_file)
    assert path.is_file()
    extra = load_rules(path)
    assert not extra.words and not extra.patterns and not extra.key_patterns


def test_classification_drops_pairs_that_touch_a_secret():
    diff = ("diff --git a/src/main/resources/app.properties b/src/main/resources/app.properties\n"
            "--- a/src/main/resources/app.properties\n+++ b/src/main/resources/app.properties\n@@ -1,2 +1,2 @@\n"
            "-spring.datasource.password=old-pw\n+spring.datasource.password=new-pw\n"
            "-server.port=8080\n+server.port=9090\n")
    c, dropped = classify_diff_ex(diff)
    assert dropped == 1 and c.change_kind == "text_replace"
    assert [(p.old, p.new) for p in c.replace_pairs] == [("8080", "9090")]
    c2, dropped2 = classify_diff_ex(diff, None)                                 # no redactor: every pair
    assert dropped2 == 0 and len(c2.replace_pairs) == 2


def test_a_secret_resolution_is_never_a_rule_candidate(kb_cfg, ws, profile):
    diff = (f"diff --git a/{EC_FILE} b/{EC_FILE}\n--- a/{EC_FILE}\n+++ b/{EC_FILE}\n@@ -3,1 +3,1 @@\n"
            '-        String apiKey = "old-key-123";\n+        String apiKey = "new-key-456";\n')
    errors, attempts = prepare_error_cases([ec_error()], [ec_attempt(diff=diff)], profile, ws, kb_cfg)
    a, e = attempts[0], errors[0]
    assert "old-key-123" not in a.diff and "new-key-456" not in a.diff and a.diff.count("***") == 2
    assert a.change.change_kind == "text_replace" and a.change.replace_pairs == []
    assert e.replace_rule_candidate is False and e.replace_pairs == []
    assert any(n.startswith("redacted:") for n in a.notes) and any(n.startswith("redacted:") for n in e.notes)
    assert "old-key-123" not in e.model_dump_json() + a.model_dump_json()


def test_a_multiline_secret_context_drops_the_pair_too(kb_cfg, ws, profile):
    """`<param-name>` on the line before the changed value: the line-local check misses it, the masked value of the
    whole diff catches it."""
    diff = ("diff --git a/src/main/webapp/WEB-INF/web.xml b/src/main/webapp/WEB-INF/web.xml\n"
            "--- a/src/main/webapp/WEB-INF/web.xml\n+++ b/src/main/webapp/WEB-INF/web.xml\n@@ -1,2 +1,2 @@\n"
            " <param-name>db.password</param-name>\n-<param-value>tiger</param-value>\n"
            "+<param-value>lion</param-value>\n")
    _, attempts = prepare_error_cases([], [ec_attempt(diff=diff)], profile, ws, kb_cfg)
    a = attempts[0]
    assert "tiger" not in a.model_dump_json() and "lion" not in a.model_dump_json()
    assert a.change.replace_pairs == []


def test_patch_case_with_a_masked_secret_is_hint_only(kb, ws, profile):
    changes = [FileChange(path="src/main/resources/application.yml", op="modify",
                          search="spring.datasource.password=s3cr3t", replace="spring.datasource.password=n3w")]
    case = sanitize_case(make_attempt(changes=changes), profile, ws)
    assert NO_REPLAY_TAG in case.tags and "s3cr3t" not in case.model_dump_json()
    clean = sanitize_case(make_attempt(), profile, ws)
    assert NO_REPLAY_TAG not in clean.tags
    cid = kb.record_case(case)
    stored = kb.get_case(cid)
    ws.write("src/main/resources/application.yml", "spring.datasource.password=***\n")
    assert kb.replay_view(stored, ws) is None                                  # never replayed
    assert kb.catalog().kb() is kb


def test_prepare_failure_stores_a_minimal_record_never_the_original(kb_cfg, ws, profile, monkeypatch):
    import sbmig.kb.error_cases as ec

    def boom(*a, **k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(ec, "classify_diff_ex", boom)
    raw = f"[ERROR] {ws.root}/{EC_FILE} password=hunter2"
    diff = ec_attempt().diff + "+spring.datasource.password=hunter2\n"
    errors, attempts = prepare_error_cases([ec_error(raw=raw, message=f"{ws.root}/X.java password=hunter2")],
                                           [ec_attempt(diff=diff, description="password=hunter2")], profile, ws,
                                           kb_cfg)
    a, e = attempts[0], errors[0]
    assert a.diff == "" and a.description == "" and a.change.change_kind == "none"
    assert a.notes and a.notes[0].startswith("prepare failed (RuntimeError)") and a.project == "demo"
    assert "hunter2" not in a.model_dump_json()
    # the error itself prepared fine (its resolver attempt is the minimal one)
    assert "hunter2" not in e.model_dump_json() and str(ws.root) not in e.model_dump_json()

    monkeypatch.setattr(ec, "_prepare_error", boom)
    errors, _ = prepare_error_cases([ec_error(raw=raw, message=f"{ws.root}/X.java password=hunter2")], [],
                                    profile, ws, kb_cfg)
    e = errors[0]
    assert e.raw == "" and e.diff_excerpt == "" and e.notes[0].startswith("prepare failed")
    assert "hunter2" not in e.model_dump_json() and str(ws.root) not in e.message and e.case_id
