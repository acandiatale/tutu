from __future__ import annotations

from sbmig.web.redact import redact_file, redact_run_dir, redact_text


def test_longest_value_first():
    # "abcdef" is a prefix of the longer value: replacing it first would leave "123456" behind
    assert redact_text("x=abcdef123456 y=abcdef", ["abcdef", "abcdef123456"]) == "x=*** y=***"


def test_url_userinfo_masked_any_scheme():
    t = "clone https://user:pa55@git.example/r.git and ssh://bob@host/x and jdbc:mysql://u:p@db:3306/a"
    got = redact_text(t, [])
    assert "https://***@git.example/r.git" in got
    assert "ssh://***@host/x" in got
    assert "mysql://***@db:3306/a" in got
    assert "pa55" not in got


def test_plain_urls_untouched():
    t = "see https://example.org/a@b and mailto x@y.z"
    assert redact_text(t, []) == t


def test_redact_file_only_rewrites_on_change(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("nothing secret\n", encoding="utf-8")
    assert redact_file(p, ["abcdef123456"]) is False
    p.write_text("token abcdef123456\n", encoding="utf-8")
    assert redact_file(p, ["abcdef123456"]) is True
    assert p.read_text(encoding="utf-8") == "token ***\n"


def test_redact_file_missing_does_not_raise(tmp_path):
    assert redact_file(tmp_path / "nope.log", ["abcdef123456"]) is False


def test_non_utf8_bytes_do_not_crash(tmp_path):
    p = tmp_path / "b.log"
    p.write_bytes(b"\xff\xfe cp949 \xb0\xa1 abcdef123456\n")
    assert redact_file(p, ["abcdef123456"]) is True
    assert b"abcdef123456" not in p.read_bytes()


def test_run_dir_scope_and_count(tmp_path):
    rd = tmp_path / "run"
    (rd / "logs" / "deep").mkdir(parents=True)
    (rd / "control").mkdir()
    (rd / "project").mkdir()
    secret = "abcdef123456"
    (rd / "sbmig.log").write_text(f"k={secret}\n", encoding="utf-8")
    (rd / "report.md").write_text(f"# r {secret}\n", encoding="utf-8")
    (rd / "report.json").write_text('{"ok": true}\n', encoding="utf-8")        # unchanged → not counted
    (rd / "logs" / "deep" / "mvn.out").write_text(f"{secret}\n", encoding="utf-8")
    (rd / "control" / "request.json").write_text(f'{{"x": "{secret}"}}', encoding="utf-8")
    (rd / "project" / "app.properties").write_text(f"p={secret}\n", encoding="utf-8")
    (rd / "source.zip").write_bytes(b"PK" + secret.encode())
    (rd / "result.zip").write_bytes(b"PK" + secret.encode())

    assert redact_run_dir(rd, [secret]) == 3
    assert secret not in (rd / "sbmig.log").read_text(encoding="utf-8")
    assert secret not in (rd / "logs" / "deep" / "mvn.out").read_text(encoding="utf-8")
    assert secret in (rd / "control" / "request.json").read_text(encoding="utf-8")
    assert secret in (rd / "project" / "app.properties").read_text(encoding="utf-8")
    assert (rd / "source.zip").read_bytes() == b"PK" + secret.encode()
    assert (rd / "result.zip").read_bytes() == b"PK" + secret.encode()


def test_empty_values_returns_zero(tmp_path):
    (tmp_path / "a.log").write_text("https://u:p@h/x\n", encoding="utf-8")
    assert redact_run_dir(tmp_path, []) == 0


def test_values_none_reads_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("SBMIG_TEST_API_KEY", "zzzz-secret-zzzz")
    (tmp_path / "a.log").write_text("v=zzzz-secret-zzzz\n", encoding="utf-8")
    assert redact_run_dir(tmp_path) == 1
    assert "zzzz-secret-zzzz" not in (tmp_path / "a.log").read_text(encoding="utf-8")
