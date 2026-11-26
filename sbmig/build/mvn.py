"""Maven을 실행하는 공용 함수를 모은 모듈. sbmig가 실행하는 mvn 명령은 모두 이 모듈을 거친다.

* ``base_command(cfg)``: ``[mvn, *extra_args, -s settings, -gs <maven home>/conf/settings.xml,
  -Dmaven.repo.local=…, -o]``
* ``mvn_env(cfg)``: inherited env + ``cfg.build.env`` + ``JAVA_HOME`` / ``PATH`` when ``java_home`` is set,
  ``MAVEN_OPTS`` += ``-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8`` (unless already set there),
  ``MAVEN_BASEDIR`` = an empty folder of this process (``build.ignore_project_mvn``; never inherited)
* ``with_project_mvn(cfg, cmd, cwd, top)``: the `-D` / `-P` of the project's ``.mvn/maven.config`` and
  ``-Dmaven.multiModuleProjectDirectory=<root>`` right after the executable
* ``run_logged(...)``: runs a command in its own process group, streams merged stdout/stderr (ANSI stripped)
  to a log file as it arrives, enforces a timeout by killing the whole group.
* ``maven_available(cfg)``: the preflight ``mvn -v``.

The build (MavenRunner) and the conversion (OpenRewriteRunner) share command, environment and logging through
this module. The project's ``.mvn/`` folder and settings are not read; only the `-D` / `-P` of its
``maven.config`` reach the command line. Maven output is taken as UTF-8 because a Korean Windows MS949 console
garbles Korean paths in javac errors. The whole process tree is always killed, so a ``java.exe`` started by
``mvn.cmd`` never lingers.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sbmig.core.config import Config

from .log_parser import strip_ansi

log = logging.getLogger("sbmig.build")

HEARTBEAT_S = 60.0      # mvn, rewrite 실행 중 이 간격으로 INFO를 남긴다. 느린 것과 멈춘 것을 구분할 수 있다


def _blank(v: str | None) -> bool:
    return v is None or str(v).strip() == ""


# 파이프로 출력을 받으면 Maven JVM이 콘솔 코드 페이지(MS949)로 출력해 한글 경로가 깨진다. UTF-8 속성을 준다
# JDK 18 이상만 System.out, System.err에 이 속성을 따르고 이전 JDK는 무시한다
UTF8_OUTPUT_PROPS: tuple[str, ...] = ("stdout.encoding", "stderr.encoding")


def with_utf8_output(maven_opts: str | None) -> str:
    """MAVEN_OPTS 끝에 UTF-8 출력 속성을 붙여 반환한다. ``build.env`` 등에서 이미 지정한 속성은 그대로 둔다."""
    opts = (maven_opts or "").strip()
    for prop in UTF8_OUTPUT_PROPS:
        if f"-D{prop}=" not in opts:
            opts = f"{opts} -D{prop}=UTF-8".strip()
    return opts


def ignore_project_mvn(cfg: Any) -> bool:
    """Maven이 프로젝트의 ``.mvn/`` 폴더를 읽지 않게 할지 반환한다.
    설정 ``build.ignore_project_mvn``을 따르고 기본값은 True다."""
    return bool(getattr(getattr(cfg, "build", None), "ignore_project_mvn", True))


# 프로세스마다 시스템 임시 폴더에 빈 폴더 하나를 만들어 모든 Maven 호출이 같이 쓰고, 종료할 때 지운다
# Maven은 이 폴더에서 .mvn/jvm.config, maven.config, extensions.xml을 찾는다. 빈 폴더라 아무것도 읽지 않는다
_BASEDIR: Optional[Path] = None
_BASEDIR_LOCK = threading.Lock()


def _remove_basedir() -> None:
    if _BASEDIR is not None:
        shutil.rmtree(_BASEDIR, ignore_errors=True)


def maven_basedir() -> Path:
    """``MAVEN_BASEDIR``로 넘길 빈 폴더를 반환한다. 없거나 누가 지웠으면 시스템 임시 폴더에 새로 만든다."""
    global _BASEDIR
    import tempfile
    with _BASEDIR_LOCK:
        if _BASEDIR is not None and _BASEDIR.is_dir() and not (_BASEDIR / ".mvn").exists():
            return _BASEDIR
        first = _BASEDIR is None
        _BASEDIR = Path(tempfile.mkdtemp(prefix="sbmig-maven-base-"))
        if first:
            atexit.register(_remove_basedir)
        log.info("Maven base directory: %s (MAVEN_BASEDIR, empty: Maven reads no .mvn/ folder of a project; the -D / -P "
                 "options of its .mvn/maven.config are passed on the command line)", _BASEDIR)
        return _BASEDIR


def mvn_env(cfg: Config, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Maven을 실행할 환경 변수를 만든다.
    현재 환경 변수에 ``build.env``, JAVA_HOME과 PATH, ``extra``, UTF-8 출력 속성을 더한다.
    ``build.ignore_project_mvn``이면 ``MAVEN_BASEDIR``에 빈 폴더를 준다."""
    env = dict(os.environ)
    # sbmig를 실행한 환경의 MAVEN_BASEDIR가 빌드에 섞이지 않게 뺀다
    env.pop("MAVEN_BASEDIR", None)
    env.update({k: str(v) for k, v in (cfg.build.env or {}).items()})
    if not _blank(cfg.build.java_home):
        jh = str(Path(cfg.build.java_home).expanduser().absolute())
        env["JAVA_HOME"] = jh
        env["PATH"] = os.pathsep.join([str(Path(jh) / "bin"), env.get("PATH", "")])
    if extra:
        env.update(extra)
    env["MAVEN_OPTS"] = with_utf8_output(env.get("MAVEN_OPTS"))
    if ignore_project_mvn(cfg):
        env["MAVEN_BASEDIR"] = str(maven_basedir())
    return env


def mvn_executable(cfg: Config) -> str:
    """`build.mvn`을 셸 없이 `subprocess`로 실행할 수 있는 경로로 바꾼다.
    Windows의 Maven은 `mvn.cmd`라서 `Popen`이 찾지 못한다. 그래서 `shutil.which`로 찾는다.
    찾지 못하면 설정값을 그대로 반환한다. 오류 메시지에 설정한 이름이 나온다.

    `Popen(["mvn", ...])` ignores PATHEXT and fails with `WinError 2`. A bare name is looked up in PATH plus
    `MAVEN_HOME`/`M2_HOME`/bin, with `.cmd` / `.bat` on Windows; a path with a directory part is used as
    given."""
    name = (cfg.build.mvn or "mvn").strip()
    if any(sep in name for sep in ("/", "\\")):
        return str(Path(name).expanduser())
    env = mvn_env(cfg)
    search = env.get("PATH", "")
    for home_var in ("MAVEN_HOME", "M2_HOME"):
        home = env.get(home_var)
        if home:
            search = os.pathsep.join([str(Path(home).expanduser() / "bin"), search])
    candidates = [name]
    if os.name == "nt" and not name.lower().endswith((".cmd", ".bat", ".exe")):
        candidates = [name + ".cmd", name + ".bat", name]
    for c in candidates:
        hit = shutil.which(c, path=search)
        if hit:
            return hit
    return name


def base_command(cfg: Config) -> list[str]:
    """모든 mvn 명령의 앞부분을 만든다. ``-Dmaven.test.skip=true``는 ``MavenRunner.command``가 붙인다."""
    b = cfg.build
    cmd = [mvn_executable(cfg), *b.extra_args]
    # settings는 절대경로로 넘긴다. Maven은 모듈 폴더에서 실행돼서, 상대경로면 조용히 ~/.m2/settings.xml을 쓴다
    # /tmp 표기가 바뀌지 않도록 심볼릭 링크는 풀지 않는다
    if not _blank(b.settings_file):
        cmd += ["-s", str(Path(b.settings_file).expanduser().absolute())]
        # 설치본의 전역 settings를 -gs로 준다. 프로젝트 .mvn/maven.config의 -gs가 대신 읽히지 않는다
        if getattr(b, "ignore_project_settings", True):
            gs = maven_global_settings(cfg)
            _log_global_settings(cmd[0], gs)
            if gs is not None:
                cmd += ["-gs", str(gs)]
    if not _blank(b.local_repo):
        cmd.append(f"-Dmaven.repo.local={Path(b.local_repo).expanduser().absolute()}")
    if b.offline:
        cmd.append("-o")
    return cmd


# Maven 설치 폴더는 실행 파일마다 프로세스에서 한 번만 찾는다. None이면 못 찾은 것이고 -gs를 넘기지 않는다
# `maven_available`이 `mvn -v`의 `Maven home:`으로 채운다. 없으면 실행 파일 옆에서 찾는다
_MAVEN_HOMES: dict[str, Optional[Path]] = {}
_MAVEN_HOME_LOGGED: set[str] = set()
_MAVEN_HOME_RE = re.compile(r"^Maven home:\s*(.+?)\s*$", re.MULTILINE)


def _home_beside(exe: Path) -> Optional[Path]:
    """실행 파일이 속한 Maven 설치 폴더를 반환한다. ``bin/m2.conf``와 ``conf/settings.xml``이 모두 있어야 한다.

    `<home>/bin/mvn` (symlinks followed) or a wrapper whose installation is `<dir>/libexec` (Homebrew)."""
    real = exe.resolve()
    for cand in (real.parent.parent, real.parent.parent / "libexec"):
        if (cand / "bin" / "m2.conf").is_file() and (cand / "conf" / "settings.xml").is_file():
            return cand
    return None


def remember_maven_home(exe: str, version_text: str) -> Optional[Path]:
    """``mvn -v`` 출력의 ``Maven home:`` 경로를 실행 파일별로 기억하고 반환한다.
    그 폴더에 ``conf/settings.xml``이 없으면 기억하지 않고 None을 반환한다."""
    m = _MAVEN_HOME_RE.search(version_text or "")
    if not m:
        return None
    home = Path(m.group(1))
    try:
        if not (home / "conf" / "settings.xml").is_file():
            return None
    except OSError:
        return None
    _MAVEN_HOMES[exe] = home
    return home


def maven_home(cfg: Config) -> Optional[Path]:
    """``build.mvn``이 실행하는 Maven의 설치 폴더를 반환한다. 찾지 못하면 None을 반환한다.
    사전 점검의 ``Maven home:``을 먼저 쓰고, 없으면 실행 파일 옆에서 찾는다. 결과는 프로세스 동안 캐시한다."""
    exe = mvn_executable(cfg)
    if exe in _MAVEN_HOMES:
        return _MAVEN_HOMES[exe]
    home: Optional[Path] = None
    try:
        p = Path(exe)
        if p.is_absolute() and p.is_file():
            home = _home_beside(p)
    except OSError:
        home = None
    _MAVEN_HOMES[exe] = home
    return home


def maven_global_settings(cfg: Config) -> Optional[Path]:
    """Maven 설치본의 전역 settings(``<maven home>/conf/settings.xml``)를 반환한다. 없으면 None을 반환한다."""
    home = maven_home(cfg)
    if home is None:
        return None
    f = home / "conf" / "settings.xml"
    try:
        return f if f.is_file() else None
    except OSError:
        return None


def _log_global_settings(exe: str, gs: Optional[Path]) -> None:
    if exe in _MAVEN_HOME_LOGGED:
        return
    _MAVEN_HOME_LOGGED.add(exe)
    if gs is not None:
        log.info("Maven global settings: %s (passed as -gs with -s, so a project's .mvn/maven.config cannot replace "
                 "the server's settings)", gs)
    else:
        log.warning("Maven home of %s not found (no 'Maven home:' from mvn -v, no Maven installation beside the "
                    "executable): builds pass -s only, a -gs in a project's .mvn/maven.config would still be read", exe)


@dataclass
class ProjectMavenSettings:
    """프로젝트 ``.mvn/maven.config``의 옵션을 담는 클래스.
    settings, 오프라인, ``-D``, ``-P`` 옵션을 파일에 쓰인 그대로 값까지 담는다."""
    path: str
    settings: list[str]
    global_settings: list[str]
    offline: list[str]
    properties: list[str] = field(default_factory=list)   # `-Dname=value` 또는 `-Dname`으로 정리한 값
    profiles: list[str] = field(default_factory=list)     # `-P<ids>`로 정리한 값
    options: list[str] = field(default_factory=list)      # 모든 옵션을 값과 함께 쓰인 그대로

    def carried(self, exclude: Iterable[str] = ()) -> list[str]:
        """Maven이 이 파일을 읽지 않을 때 명령줄로 옮겨 넘길 ``-D``, ``-P`` 옵션을 반환한다.
        ``RESERVED_PROPERTIES``와 ``exclude``에 있는 이름은 넘기지 않는다."""
        skip = set(RESERVED_PROPERTIES) | set(exclude)
        return [p for p in self.properties if _prop_name(p) not in skip] + list(self.profiles)

    @property
    def names_settings(self) -> bool:
        return bool(self.settings or self.global_settings)

    def shown(self) -> str:
        return ", ".join([*self.settings, *self.global_settings])


def _option_kind(tok: str) -> tuple[Optional[str], bool]:
    """maven.config 토큰 하나가 어떤 옵션인지 (종류, 다음 토큰이 값인지)로 반환한다. 종류는 s, gs, o, None이다.

    Mirrors commons-cli as Maven uses it: `-s x`, `-s=x`, `-sx`, `--settings[=x]`, unique long prefixes
    (`--sett`), single-dash long names."""
    if tok.startswith("--"):
        name, eq, _ = tok[2:].partition("=")
        if len(name) >= 2 and "settings".startswith(name):
            return "s", not eq
        if len(name) >= 8 and "global-settings".startswith(name):
            return "gs", not eq
        if len(name) >= 2 and "offline".startswith(name) and not eq:
            return "o", False
        return None, False
    if not tok.startswith("-") or len(tok) < 2:
        return None, False
    name, eq, _ = tok[1:].partition("=")
    if name in ("s", "gs"):
        return name, not eq
    if len(name) >= 8 and "global-settings".startswith(name):
        return "gs", not eq
    if name.startswith("gs"):
        return "gs", False
    if name.startswith("s"):
        return "s", not eq and len(name) >= 2 and "settings".startswith(name)
    if name.startswith("o") and name.isalpha() and not eq:
        return "o", False
    return None, False


def project_maven_settings(root: Path) -> Optional[ProjectMavenSettings]:
    """프로젝트 루트의 ``.mvn/maven.config``에서 옵션을 읽는다. settings, 오프라인, ``-D``, ``-P``를 본다.
    파일이 없거나 읽지 못하면 None을 반환한다. 파일은 바꾸지 않는다.

    Maven 3.9 reads one argument per line and skips `#` lines; the words of a line are split too, as older
    Maven did (`_config_line_words`)."""
    p = Path(root) / ".mvn" / "maven.config"
    try:
        if not p.is_file():
            return None
        with open(p, "rb") as fh:
            text = fh.read(256 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None
    # Maven 3.9는 한 줄을 인자 하나로 읽고 '#'으로 시작하는 줄은 건너뛴다
    toks = [w for ln in text.splitlines() if not ln.strip().startswith("#") for w in _config_line_words(ln)]
    out = ProjectMavenSettings(path=".mvn/maven.config", settings=[], global_settings=[], offline=[])
    i = 0
    while i < len(toks):
        tok = toks[i]
        define, profile = _define_value(tok), _profile_value(tok)
        if define is not None or profile is not None:
            value = define if define is not None else profile
            shown = tok
            if value == "" and i + 1 < len(toks):          # `-D name=value`처럼 값이 다음 토큰
                value = toks[i + 1]
                shown = f"{tok} {value}"
                i += 1
            if value:
                out.options.append(shown)
                (out.properties if define is not None else out.profiles).append(
                    ("-D" if define is not None else "-P") + value)
            i += 1
            continue
        kind, takes_next = _option_kind(tok)
        takes_next = takes_next or (kind is None and tok in _VALUE_OPTIONS)
        shown = tok
        if takes_next and i + 1 < len(toks):
            shown = f"{tok} {toks[i + 1]}"
            i += 1
        if kind == "s":
            out.settings.append(shown)
        elif kind == "gs":
            out.global_settings.append(shown)
        elif kind == "o":
            out.offline.append(shown)
        out.options.append(shown)
        i += 1
    return out


# sbmig가 직접 정하는 속성은 프로젝트 maven.config에서 옮겨 오지 않는다
RESERVED_PROPERTIES = frozenset({"maven.repo.local", "maven.multiModuleProjectDirectory", "maven.test.skip",
                                 "skipTests"})
# 다음 인자를 값으로 받는 Maven 옵션(설정 파일, 스레드, 모듈 선택 등)이다
_VALUE_OPTIONS = frozenset({"-t", "--toolchains", "-gt", "--global-toolchains", "-f", "--file", "-l", "--log-file",
                            "-T", "--threads", "-pl", "--projects", "-rf", "--resume-from", "-b", "--builder",
                            "--color"})


def _config_line_words(line: str) -> list[str]:
    """maven.config 한 줄을 공백으로 나눈 단어 목록을 반환한다.
    ``-Dname=value`` 하나만 있고 값에 공백이 있는 줄은 줄 전체를 인자 하나로 본다. Maven 3.9와 같다."""
    words = line.split()
    if len(words) > 1 and _define_value(words[0]) and "=" in words[0] and not any(w.startswith("-") for w in words[1:]):
        return [line.strip()]
    return words


def _define_value(tok: str) -> Optional[str]:
    """``-D`` 옵션의 속성 부분을 반환한다. 값이 다음 토큰이면 빈 문자열, ``-D``가 아니면 None을 반환한다."""
    if tok.startswith("--"):
        name, eq, val = tok[2:].partition("=")
        if len(name) >= 3 and "define".startswith(name):
            return val if eq else ""
        return None
    if tok.startswith("-D"):
        return tok[2:]
    return None


def _profile_value(tok: str) -> Optional[str]:
    """``-P`` 옵션의 프로필 id를 반환한다. 값이 다음 토큰이면 빈 문자열, ``-P``가 아니면 None을 반환한다."""
    if tok.startswith("--"):
        name, eq, val = tok[2:].partition("=")
        if len(name) >= 2 and "activate-profiles".startswith(name):
            return val if eq else ""
        return None
    if tok.startswith("-P"):
        return tok[2:].removeprefix("=")
    return None


def _prop_name(opt: str) -> str:
    """``-Dname=value``에서 ``name``을 반환한다."""
    return opt[2:].split("=", 1)[0] if opt.startswith("-D") else ""


def project_mvn_dir(start: Path, top: Optional[Path] = None) -> Optional[Path]:
    """``start``부터 ``top``까지 올라가며 ``.mvn`` 폴더가 있는 첫 폴더를 반환한다. 없으면 None을 반환한다.
    Maven 런처가 찾았을 폴더와 같다. ``top`` 위로는 올라가지 않는다.

    When `start` is not inside `top`, only `start` itself is checked."""
    start = Path(start).absolute()
    chain = [start]
    if top is not None:
        top = Path(top).absolute()
        if top == start or top in start.parents:
            chain = [start, *[d for d in start.parents if d == top or top in d.parents]]
    for d in chain:
        try:
            if (d / ".mvn").is_dir():
                return d
        except OSError:
            return None
    return None


def _file_arg(cmd: Sequence[str]) -> Optional[str]:
    for i, a in enumerate(cmd):
        if a in ("-f", "--file") and i + 1 < len(cmd):
            return cmd[i + 1]
        if a.startswith("--file="):
            return a.split("=", 1)[1]
    return None


def with_project_mvn(cfg: Any, cmd: Sequence[str], cwd: Path, top: Optional[Path] = None) -> list[str]:
    """Maven이 읽지 않는 프로젝트 ``maven.config``의 ``-D``, ``-P``를 명령줄로 옮긴다.
    실행 파일 바로 뒤에 그 옵션과 프로젝트 루트(``-Dmaven.multiModuleProjectDirectory``)를 넣는다.
    ``build.ignore_project_mvn``이 꺼져 있거나 루트가 이미 들어 있으면(재시도) 명령을 그대로 반환한다.

    Maven gets the empty `MAVEN_BASEDIR` (`mvn_env`), so this restores what the project's `.mvn/maven.config`
    said. The file is the one Maven would have found from where the call starts (`cwd`, or the directory of
    `-f`; first ancestor with `.mvn`, never above `top`); properties the command already sets are dropped.
    Then `-Dmaven.multiModuleProjectDirectory=<that directory, else the start>` so a pom's
    `${maven.multiModuleProjectDirectory}` is the project's root again (measured with Maven 3.9.10: the
    property on the command line does not make Maven read the project's `.mvn/`). Everything sbmig passes
    comes later and wins."""
    cmd = list(cmd)
    if not ignore_project_mvn(cfg) or not cmd or any(a.startswith(_MMPD) for a in cmd):
        return cmd
    start = Path(cwd).absolute()
    f = _file_arg(cmd)
    if f:
        fp = Path(f) if Path(f).is_absolute() else start / f
        start = fp if fp.is_dir() else fp.parent
    root = project_mvn_dir(start, top if top is not None else start)
    carried: list[str] = []
    if root is not None:
        found = project_maven_settings(root)
        if found is not None:
            own = {_prop_name(a) for a in cmd if a.startswith("-D")}
            carried = found.carried(exclude=own)
    return [cmd[0], *carried, f"{_MMPD}{root or start}", *cmd[1:]]


_MMPD = "-Dmaven.multiModuleProjectDirectory="


def project_settings_ignored(cfg: Config, found: ProjectMavenSettings) -> bool:
    """프로젝트가 지정한 settings를 이번 실행의 Maven이 무시하면 True를 반환한다.
    ``build.settings_file``이 있어야 한다. 프로젝트가 ``-gs``도 지정했으면 sbmig도 ``-gs``를 넘길 수 있어야 한다."""
    b = cfg.build
    if _blank(b.settings_file):
        return False
    if not found.global_settings:
        return True
    return bool(getattr(b, "ignore_project_settings", True)) and maven_global_settings(cfg) is not None


def project_settings_note(cfg: Config, found: Optional[ProjectMavenSettings]) -> Optional[tuple[str, str]]:
    """프로젝트 ``.mvn/maven.config``가 settings를 지정했으면 플래그와 인수인계 줄을 반환한다.
    플래그는 ``project-maven-settings-ignored``나 ``project-maven-settings-used``다.
    ``build.settings_file``이 없거나 settings 옵션이 없으면 None을 반환한다."""
    if found is None or not found.names_settings or _blank(cfg.build.settings_file):
        return None
    head = f"the project's .mvn/maven.config points Maven at its own settings ({found.shown()}): "
    if project_settings_ignored(cfg, found):
        flag, line = ("project-maven-settings-ignored",
                      head + "ignored here — every build of this run used the server's settings")
    else:
        why = ("build.ignore_project_settings is false" if not getattr(cfg.build, "ignore_project_settings", True)
               else "Maven home not found, so no -gs was passed")
        flag, line = ("project-maven-settings-used",
                      head + f"Maven read them in this run as its global settings, merged with the server's ({why})")
    if found.offline and not cfg.build.offline:
        line += (f". The same file also turns on offline mode ({', '.join(found.offline)}), which the command line "
                 "cannot switch off: Maven downloads nothing in this run and builds only from what the local "
                 "repository already holds")
    return flag, line


@dataclass
class ProjectMvnFolder:
    """프로젝트 안의 ``.mvn`` 폴더 하나에 무엇이 들어 있는지 담는 클래스. 폴더는 읽기만 한다."""
    rel: str                                      # ".mvn" 또는 "svc-a/.mvn"
    maven_config: Optional[ProjectMavenSettings]
    has_jvm_config: bool
    jvm_options: list[str]
    has_extensions: bool
    extensions: list[str]                         # g:a:v
    wrapper: bool
    other_files: list[str]

    def carried(self) -> list[str]:
        return self.maven_config.carried() if self.maven_config is not None else []

    def names(self) -> list[str]:
        out = [n for n, on in (("maven.config", self.maven_config is not None), ("jvm.config", self.has_jvm_config),
                               ("extensions.xml", self.has_extensions)) if on]
        if self.wrapper:
            out.append("Maven wrapper")
        if self.other_files:
            out.append("other files: " + ", ".join(self.other_files))
        return out

    def holds(self) -> str:
        parts: list[str] = []
        if self.maven_config is not None:
            parts.append("maven.config: " + (_capped(self.maven_config.options, ", ") or "empty"))
        if self.has_jvm_config:
            parts.append("jvm.config: " + (_capped(self.jvm_options, " ") or "empty"))
        if self.has_extensions:
            parts.append("extensions.xml: " + (_capped(self.extensions, ", ") or "no extension"))
        if self.wrapper:
            parts.append("Maven wrapper")
        if self.other_files:
            parts.append("other files: " + _capped(self.other_files, ", "))
        return "; ".join(parts)


def _capped(items: Sequence[str], sep: str, limit: int = 8) -> str:
    shown = sep.join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


_EXTENSION_RE = re.compile(r"<extension>(.*?)</extension>", re.S)
_SKIP_DIRS = frozenset({".git", "target", "node_modules", ".svn", ".idea"})


def _read_text(p: Path, limit: int = 256 * 1024) -> str:
    try:
        with open(p, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _mvn_folder(root: Path, d: Path) -> Optional[ProjectMvnFolder]:
    mvn = d / ".mvn"
    try:
        entries = sorted(p.name for p in mvn.iterdir())
    except OSError:
        return None
    if not entries:
        return None                               # 빈 .mvn은 루트 표시일 뿐이다. 알릴 내용이 없다
    jvm = mvn / "jvm.config"
    jvm_opts = [w for ln in _read_text(jvm).splitlines() for w in ln.split("#", 1)[0].split()] if jvm.is_file() else []
    ext = mvn / "extensions.xml"
    exts: list[str] = []
    if ext.is_file():
        text = re.sub(r"<!--.*?-->", "", _read_text(ext), flags=re.S)
        for block in _EXTENSION_RE.findall(text):
            gav = [(re.search(rf"<{t}>\s*([^<]*?)\s*</{t}>", block) or [None, ""])[1]
                   for t in ("groupId", "artifactId", "version")]
            exts.append(":".join(x for x in gav if x))
    rel = (d.relative_to(root).as_posix() + "/.mvn") if d != root else ".mvn"
    return ProjectMvnFolder(rel=rel, maven_config=project_maven_settings(d), has_jvm_config=jvm.is_file(),
                            jvm_options=jvm_opts, has_extensions=ext.is_file(), extensions=exts,
                            wrapper=(mvn / "wrapper").is_dir(),
                            other_files=[n for n in entries
                                         if n not in ("maven.config", "jvm.config", "extensions.xml", "wrapper")])


def project_mvn_folders(root: Path, skip: Iterable[str] = (), max_depth: int = 3) -> list[ProjectMvnFolder]:
    """프로젝트 안의 ``.mvn`` 폴더를 ``max_depth`` 깊이까지 찾아 내용을 정리한다. 빈 폴더는 뺀다.
    ``.git``, ``target``, ``node_modules``, ``skip`` 폴더 아래는 보지 않는다. 파일은 바꾸지 않는다."""
    root = Path(root)
    skipped = {s.strip("/") for s in skip if s}
    out: list[ProjectMvnFolder] = []
    for dirpath, dirnames, _files in os.walk(root, followlinks=False):
        d = Path(dirpath)
        rel = "" if d == root else d.relative_to(root).as_posix()
        if ".mvn" in dirnames:
            f = _mvn_folder(root, d)
            if f is not None:
                out.append(f)
        depth = 0 if not rel else rel.count("/") + 1
        dirnames[:] = sorted(n for n in dirnames if depth < max_depth and not n.startswith(".")
                             and n not in _SKIP_DIRS and (f"{rel}/{n}" if rel else n) not in skipped)
    return out


def project_mvn_note(cfg: Any, folders: Sequence[ProjectMvnFolder]) -> Optional[tuple[str, str]]:
    """프로젝트의 ``.mvn`` 폴더를 쓰지 않았으면 플래그 ``project-mvn-ignored``와 인수인계 줄을 반환한다.
    ``build.ignore_project_mvn``이 꺼져 있거나 내용이 있는 ``.mvn`` 폴더가 없으면 None을 반환한다."""
    if not ignore_project_mvn(cfg) or not folders:
        return None
    jdk = int(getattr(getattr(cfg, "build", None), "require_java_major", 21) or 21)
    if len(folders) == 1:
        f = folders[0]
        holds, carried = f.holds(), " ".join(f.carried()) or "nothing"
        subject, it, them = "folder was", "the folder is", "it"
    else:
        holds = "; ".join(f"{f.rel}: {', '.join(f.names())}" for f in folders)
        carried = ", ".join(f"{' '.join(f.carried())} ({f.rel})" for f in folders if f.carried()) or "nothing"
        subject, it, them = "folders were", "the folders are", "them"
    line = (f"the project's .mvn/ {subject} not used for the builds of this run ({holds}); carried over to the command "
            f"line: {carried}; {it} delivered unchanged — check {them} against JDK {jdk} and the Maven version in use "
            f"before building with {them}")
    return "project-mvn-ignored", line


_CATCH_ALL_MIRROR_RE = re.compile(r"<mirrorOf>\s*(\*|external:\*|external:http:\*)\s*</mirrorOf>")


def settings_catch_all_mirror(cfg: Config) -> Optional[bool]:
    """``build.settings_file``에 모든 저장소 요청을 받는 미러(``<mirrorOf>*</mirrorOf>`` 등)가 있는지 확인한다.
    설정한 파일이 없거나 읽지 못하면 None을 반환한다.
    이런 미러가 없으면 Maven이 pom에 선언된 저장소 URL에 직접 접속한다. 폐쇄망에서는 실패한다.

    `<mirrorOf>` values that count: `*`, `external:*`, `external:http:*`. See RUNBOOK §0.1."""
    if _blank(cfg.build.settings_file):
        return None
    p = Path(cfg.build.settings_file).expanduser()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return bool(_CATCH_ALL_MIRROR_RE.search(text))


def repository_notes(poms: Iterable[Any], cfg: Config) -> list[str]:
    """프로젝트 pom이 선언한 ``<repositories>``를 운영자에게 알리는 문구 하나를 만든다.
    Maven이 그 URL에 직접 접속하게 되면 문구가 ``followup:``으로 시작한다.
    선언이 없으면 빈 목록을 반환한다."""
    urls: list[str] = []
    for p in poms:
        for u in getattr(p, "repositories", []) or []:
            if u not in urls:
                urls.append(u)
    if not urls:
        return []
    shown = ", ".join(urls[:5]) + (f" (+{len(urls) - 5} more)" if len(urls) > 5 else "")
    mirror = settings_catch_all_mirror(cfg)
    if mirror:
        return [(f"poms declare {len(urls)} repository URL(s) ({shown}); routed through the settings mirror "
                 f"({cfg.build.settings_file})")]
    where = ("no build.settings_file configured" if mirror is None else
             f"{cfg.build.settings_file} has no catch-all <mirrorOf>*</mirrorOf> / external:* mirror")
    return [(f"followup: poms declare {len(urls)} repository URL(s) ({shown}) and {where}: Maven contacts them directly "
             "(unreachable → resolution failures / long timeouts). Copy config/settings.nexus.example.xml, set the Nexus "
             "group URL and point build.settings_file at it")]


def local_repo_path(cfg: Config) -> Path:
    """로컬 Maven 저장소 경로를 반환한다.
    ``build.local_repo`` -> settings.xml의 ``<localRepository>`` -> ``~/.m2/repository`` 순서로 찾는다.
    settings.xml은 설정한 파일을 먼저 보고, 다음에 사용자 settings를 본다."""
    if not _blank(cfg.build.local_repo):
        return Path(cfg.build.local_repo).expanduser()
    for settings in ([cfg.build.settings_file] if not _blank(cfg.build.settings_file) else []) + \
            [str(Path.home() / ".m2" / "settings.xml")]:
        p = Path(settings).expanduser()
        if p.is_file():
            m = re.search(r"<localRepository>\s*([^<]+?)\s*</localRepository>",
                          p.read_text(encoding="utf-8", errors="replace"))
            if m and "${" not in m.group(1):
                return Path(m.group(1)).expanduser()
    return Path.home() / ".m2" / "repository"


@dataclass
class ProcessResult:
    """``run_logged``의 실행 결과를 담는 클래스.
    종료 코드, 전체 출력, 걸린 시간, 타임아웃이 났는지, 시작하지 못한 이유를 담는다."""
    exit_code: int
    text: str
    duration_s: float
    timed_out: bool = False
    error: str | None = None          # 실행 파일을 찾지 못한 경우 등


def _popen(*args, **kwargs) -> subprocess.Popen:
    """Maven 프로세스를 띄우는 래퍼. 테스트가 이 함수만 가짜로 바꾼다.
    git도 subprocess를 쓰기 때문에 subprocess 전체를 바꿀 수는 없다."""
    return subprocess.Popen(*args, **kwargs)


IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000


def _popen_kwargs() -> dict[str, Any]:
    """Windows에서 Maven, OpenRewrite 프로세스에 숨긴 별도 콘솔(``CREATE_NO_WINDOW``)을 주는 옵션을 반환한다.
    운영자가 누른 Ctrl+Break, Ctrl+C가 ``mvn.cmd``에 가면 "Terminate batch job (Y/N)?"에서 멈춘다.

    Ctrl+Break is the thread dump of `sbmig.core.diag`. sbmig still kills the whole tree itself (`taskkill /T
    /F`)."""
    if not IS_WINDOWS:
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)}


def _kill_tree_windows(pid: int) -> None:
    """Windows에서 자식 프로세스까지 모두 `taskkill /T /F`로 종료한다.
    Windows에는 프로세스 그룹이 없다. ``mvn.cmd``만 끝내면 ``java.exe``가 남아 ``target/``과 소스 파일을 잠근다."""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30,
                       encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 실패해도 계속 진행한다
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    """프로세스를 자식까지 모두 종료한다.
    SIGTERM을 보내고 5초 기다린다. 그래도 남아 있으면 SIGKILL을 보내고 5초 더 기다린다. 오류는 무시한다."""
    try:
        if IS_WINDOWS:
            _kill_tree_windows(proc.pid)
        elif hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:                             # pragma: no cover
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:                             # pragma: no cover
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:     # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# 실행 중인 Maven, OpenRewrite 프로세스 관리
# ---------------------------------------------------------------------------
# run_logged가 띄운 자식 프로세스를 모아 둔다. 웹 서버나 인터프리터가 끝날 때 실행 중인 mvn도 함께 종료한다
# 그러지 않으면 Windows에서 mvn.cmd가 띄운 java.exe가 uvicorn 종료 뒤에도 남아 파일을 잠근다
_LIVE: "set[subprocess.Popen]" = set()
_LIVE_LOCK = threading.Lock()


def _register(proc: subprocess.Popen) -> None:
    with _LIVE_LOCK:
        _LIVE.add(proc)


def _unregister(proc: subprocess.Popen) -> None:
    with _LIVE_LOCK:
        _LIVE.discard(proc)


def live_processes() -> list[subprocess.Popen]:
    """``run_logged``로 시작한 자식 프로세스 중 아직 끝나지 않은 것을 반환한다."""
    with _LIVE_LOCK:
        return [p for p in _LIVE if p.poll() is None]


def kill_live_processes(reason: str = "shutdown") -> int:
    """실행 중인 자식 프로세스를 그 자식까지 모두 종료하고, 종료한 개수를 반환한다.
    ``atexit``와 웹 앱이 종료될 때 호출된다. Windows는 ``taskkill /T /F``로, 나머지는 프로세스 그룹으로 종료한다."""
    procs = live_processes()
    for proc in procs:
        name = Path(str(proc.args[0])).name if isinstance(proc.args, (list, tuple)) and proc.args else "?"
        log.warning("%s: killing %s (pid %s) and its process tree", reason, name, proc.pid)
        _kill_group(proc)
    return len(procs)


atexit.register(lambda: kill_live_processes("interpreter exit"))


def run_logged(cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str], log_path: Path,
               timeout_s: float | None) -> ProcessResult:
    """``cmd``를 별도 프로세스 그룹에서 실행하고 출력을 줄 단위로 ``log_path``에 기록한다.
    ``HEARTBEAT_S``마다 "still running" 줄을 남긴다. 타임아웃이 되면 자식 프로세스까지 모두 종료한다.
    실패해도 예외를 던지지 않고 ProcessResult를 반환한다. 실행 파일이 없으면 종료 코드는 127, 타임아웃이면 -1이다.
    출력의 ANSI 코드는 지우고 기록한다."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"# cwd: {cwd}\n# cmd: {' '.join(cmd)}\n")
        fh.flush()
        try:
            proc = _popen(list(cmd), cwd=str(cwd), env=dict(env), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                          **_popen_kwargs())
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            msg = f"cannot start {cmd[0]!r}: {exc}"
            fh.write(f"[ERROR] {msg}\n")
            return ProcessResult(exit_code=127, text=f"[ERROR] {msg}\n", duration_s=time.monotonic() - t0,
                                 error=msg)

        def pump() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                # 깨진 바이트는 U+FFFD로 바꾼다. 읽기 스레드가 디코딩 오류로 멈추면 파이프가 가득 차서 Maven도 멈춘다
                line = strip_ansi(raw.decode("utf-8", errors="replace"))
                chunks.append(line)
                fh.write(line)
                fh.flush()

        reader = threading.Thread(target=pump, name="mvn-log-pump", daemon=True)
        reader.start()
        timed_out = False
        _register(proc)
        try:
            while True:
                elapsed = time.monotonic() - t0
                remaining = None if timeout_s is None else max(0.0, timeout_s - elapsed)
                slot = HEARTBEAT_S if remaining is None else min(HEARTBEAT_S, remaining)
                try:
                    proc.wait(timeout=slot if slot > 0 else 0.01)
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - t0
                    if timeout_s is not None and elapsed >= timeout_s:
                        timed_out = True
                        log.warning("timeout after %.0fs, killing process group of %s", timeout_s or 0, cmd[0])
                        _kill_group(proc)
                        break
                    log.info("%s still running: %.0fs elapsed%s — %d output line(s) so far, log: %s",
                             Path(cmd[0]).name, elapsed, f" (timeout {timeout_s:.0f}s)" if timeout_s else "",
                             len(chunks), log_path)
        # Ctrl+C, SystemExit에도 JVM이 남지 않도록 먼저 종료하고 예외를 다시 던진다
        except BaseException:
            _kill_group(proc)
            raise
        finally:
            _unregister(proc)
        reader.join(timeout=10)
        if timed_out:
            fh.write(f"\n[ERROR] sbmig: killed after timeout of {timeout_s:.0f}s\n")
        try:
            proc.stdout.close()           # type: ignore[union-attr]
        except Exception:                 # pragma: no cover
            pass
    code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
    return ProcessResult(exit_code=code, text="".join(chunks), duration_s=time.monotonic() - t0, timed_out=timed_out)


def maven_available(cfg: Config) -> tuple[bool, str]:
    """사전 점검으로 ``mvn -v``를 실행해 성공했는지와 버전 문구를 반환한다.
    버전 문구에 runtime 부분을 남긴다. JDK 21 확인이 실패하면 Maven이 실제로 쓴 JDK를 보여 줄 수 있다.
    성공하면 설치 폴더도 기억해 둔다.

    Text shape: 'Apache Maven x.y.z; Java version: 21.0.1, runtime: …'."""
    cmd = [mvn_executable(cfg), "-v"]
    try:
        cp = subprocess.run(cmd, env=mvn_env(cfg), capture_output=True, timeout=120,
                            stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError) as exc:
        return False, f"mvn not found ({cfg.build.mvn}): {exc}"
    except subprocess.TimeoutExpired:
        return False, "mvn -v timed out"
    out = strip_ansi(cp.stdout + cp.stderr)
    if cp.returncode != 0:
        return False, out.strip().splitlines()[-1] if out.strip() else f"mvn -v exited {cp.returncode}"
    remember_maven_home(cmd[0], out)       # 빌드마다 mvn -v를 다시 실행하지 않도록 설치 폴더를 기억한다
    maven = next((ln.strip() for ln in out.splitlines() if ln.startswith("Apache Maven")), out.strip()[:80])
    line = next((ln.strip() for ln in out.splitlines() if ln.startswith("Java version")), "")
    # "Java version: 21.0.1, vendor: …, runtime: C:\jdk-21"에서 버전과 runtime만 남긴다. jdk-not-21 중단 문구에 들어간다
    parts = [x.strip() for x in line.split(",")]
    java = ", ".join([parts[0], *[x for x in parts[1:] if x.startswith("runtime:")]]) if line else ""
    return True, f"{maven}; {java}" if java else maven
