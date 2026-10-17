#!/usr/bin/env bash
# Install the simulated in-house libraries (fixtures/libs/*) into a local Maven repository. Idempotent.
#
#   fixtures/libs/install.sh                 # default local repo (~/.m2/repository)
#   fixtures/libs/install.sh /path/to/repo   # -Dmaven.repo.local=/path/to/repo
#
# Each library is built in a temporary copy (no target/ left in the fixture tree) with the JDK it targets:
#   legacy-web-lib 1.0.0 → JDK 8  (JAVA8_HOME)   javax.servlet
#   legacy-web-lib 2.0.0 → JDK 21 (JAVA21_HOME)  jakarta.servlet, release 17
set -euo pipefail
LIBS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JVM_DIR=/Users/acandiatale/Library/Java/JavaVirtualMachines
JAVA8_HOME="${JAVA8_HOME:-$JVM_DIR/corretto-1.8.0_362/Contents/Home}"
JAVA21_HOME="${JAVA21_HOME:-$JVM_DIR/corretto-21.0.1/Contents/Home}"
MVN="${MVN:-$(command -v mvn || echo /opt/homebrew/bin/mvn)}"

REPO_ARGS=()
if [[ $# -ge 1 && -n "$1" ]]; then
  mkdir -p "$1"
  REPO_ARGS=("-Dmaven.repo.local=$(cd "$1" && pwd)")
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/sbmig-libs.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

install_lib() {   # <source dir> <java home>
  local src="$1" jh="$2" name
  name="$(basename "$(dirname "$src")")-$(basename "$src")"
  if [[ ! -x "$jh/bin/java" ]]; then
    echo "install.sh: no JDK at $jh (needed for $name)" >&2
    exit 1
  fi
  cp -R "$src" "$TMP/$name"
  rm -rf "$TMP/$name/target"
  echo "installing $name (JAVA_HOME=$jh)"
  (cd "$TMP/$name" && JAVA_HOME="$jh" "$MVN" -q -B -DskipTests ${REPO_ARGS[@]+"${REPO_ARGS[@]}"} install)
}

install_lib "$LIBS/legacy-web-lib/v1.0.0" "$JAVA8_HOME"
install_lib "$LIBS/legacy-web-lib/v2.0.0" "$JAVA21_HOME"
echo "fixtures/libs installed${1:+ into $1}"
