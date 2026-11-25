"""v2.1z-cg: the image's JDK is Amazon Corretto 21 at a pinned version, its checksum checked before it is installed.

The image cannot be built in the test suite, so these tests read the Dockerfile as text: continuation lines joined and
comments dropped the way Docker does (the approach of test_v21zbr_image_context.py)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"

CONTRACT_ARGS = {
    "CORRETTO_VERSION": "21.0.13.1.2",
    "CORRETTO_BASE_URL": "https://corretto.aws/downloads/resources",
    "CORRETTO_SHA256_AMD64": "c365469853d9296728bb19e629225fef04b45e595e3467d8a0778450367381e0",
    "CORRETTO_SHA256_ARM64": "d251fb09557ccf7992d36b810a96df859531821679d21d0ab1eddc2b8e8c1ba0",
    "CORRETTO_TLS_VERIFY": "1",
}
CORRETTO_HOME = "/usr/lib/jvm/java-21-amazon-corretto"


def _instructions() -> list[str]:
    """Dockerfile instructions with continuations joined and comment lines dropped."""
    out, buf = [], ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        out.append(buf + s)
        buf = ""
    return out


def _after_from() -> list[str]:
    ins = _instructions()
    i = next(n for n, ln in enumerate(ins) if ln.upper().startswith("FROM "))
    return ins[i + 1:]


def _jdk_run() -> str:
    runs = [ln for ln in _after_from() if ln.startswith("RUN ") and "apt-get install" in ln]
    assert len(runs) == 1, runs
    return runs[0]


def _steps(run: str) -> list[str]:
    return [s.strip() for s in run[len("RUN "):].split("&&")]


def test_the_five_arguments_have_the_contract_defaults_and_are_declared_after_from():
    args = {}
    for ln in _after_from():
        m = re.match(r"ARG (\w+)=(.*)$", ln)
        if m and m.group(1).startswith("CORRETTO_"):
            assert m.group(1) not in args, f"{m.group(1)} declared twice"
            args[m.group(1)] = m.group(2).strip()
    assert args == CONTRACT_ARGS


def test_the_version_is_written_once_everything_else_derives_from_the_argument():
    text = "\n".join(_instructions())
    assert text.count(CONTRACT_ARGS["CORRETTO_VERSION"]) == 1
    run = _jdk_run()
    assert "${CORRETTO_BASE_URL%/}/${CORRETTO_VERSION}/$deb" in run
    assert 'deb="java-21-amazon-corretto-jdk_${CORRETTO_VERSION}-1_${arch}.deb"' in run
    assert 'arch="$(dpkg --print-architecture)"' in run


def test_no_apt_get_install_line_installs_openjdk():
    for ln in _instructions():
        for step in ln.split("&&"):
            if "apt-get install" in step:
                assert "openjdk" not in step, step


def test_checksum_before_install_install_before_maven():
    steps = _steps(_jdk_run())

    def first(pred) -> int:
        return next(i for i, s in enumerate(steps) if pred(s))

    sed = first(lambda s: "sed -i" in s and "APT_MIRROR" in s)
    download = first(lambda s: s.startswith("curl "))
    checksum = first(lambda s: "sha256sum" in s)
    compare = first(lambda s: '"$sha_got" != "$sha_want"' in s)
    install = first(lambda s: s.startswith("apt-get install") and '"/tmp/$deb"' in s)
    maven = first(lambda s: s.startswith("apt-get install") and " maven" in s)
    openjdk_check = first(lambda s: "'openjdk-*'" in s)
    version_check = first(lambda s: "grep -qF \"$CORRETTO_VERSION\"" in s)
    lists = first(lambda s: s == "rm -rf /var/lib/apt/lists/*")
    link = first(lambda s: s.startswith("ln -s "))
    assert sed == 0
    assert sed < download < checksum < compare < install < maven < openjdk_check < version_check < lists < link
    assert "exit 1" in steps[compare] and "$sha_arg" in steps[compare]
    # everything chained: one failing step stops the build
    assert all(";" not in s or s.startswith(("if ", "case ", "tls_opt")) for s in steps), steps


def test_the_download_and_tls_switch():
    run = _jdk_run()
    curl = next(s for s in _steps(run) if s.startswith("curl "))
    assert curl.startswith("curl -fSL") and "--retry 3" in curl and "--no-progress-meter" in curl
    assert "$tls_opt" in curl and "--insecure" not in curl
    assert 'if [ "$CORRETTO_TLS_VERIFY" = "0" ]; then tls_opt="--insecure"; fi' in run
    assert run.count("--insecure") == 1


def test_unsupported_architecture_stops_the_build():
    run = _jdk_run()
    assert "amd64) sha_arg=CORRETTO_SHA256_AMD64; sha_want=\"$CORRETTO_SHA256_AMD64\"" in run
    assert "arm64) sha_arg=CORRETTO_SHA256_ARM64; sha_want=\"$CORRETTO_SHA256_ARM64\"" in run
    assert re.search(r"\*\) echo \"ERROR: unsupported architecture[^\"]*\" >&2; exit 1 ;;", run)


def test_opt_java21_points_at_corretto_and_java_home_is_unchanged():
    steps = _steps(_jdk_run())
    assert steps[-1] == f"ln -s {CORRETTO_HOME} /opt/java21"
    env = " ".join(ln for ln in _after_from() if ln.startswith("ENV "))
    assert "JAVA_HOME=/opt/java21" in env and "JAVA21_HOME=/opt/java21" in env


def test_build_messages_are_english_one_line_each():
    run = _jdk_run()
    messages = re.findall(r'echo "ERROR: .*?>&2', run)
    # unsupported architecture, checksum mismatch, OpenJDK found, java -version not the pinned build
    assert len(messages) == 4, messages
    for msg, arg in zip(messages, ("--platform", "$sha_arg", "CORRETTO_VERSION", "CORRETTO_VERSION")):
        assert arg in msg, msg
        assert not re.search(r"[ㄱ-힝]", msg), msg
    assert "expected $sha_want, got $sha_got" in messages[1]
