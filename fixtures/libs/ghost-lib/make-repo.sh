#!/usr/bin/env bash
# Lay out com.company.nonexistent:ghost-lib:1.2.3 as a minimal Maven repository in <dir> (NOT ~/.m2), so the
# missing-artifact fixture can be built as-is with  mvn -Dmaven.repo.local.tail=<dir> … .
# The artifact simulates a library that existed only in a decommissioned repository; sbmig must report it as
# blocked (artifact_not_found), so it must never end up in the local repository that sbmig's Maven uses.
#   fixtures/libs/ghost-lib/make-repo.sh /tmp/ghost-repo
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:?usage: make-repo.sh <repo dir>}"
JAVA8_HOME="${JAVA8_HOME:-/Users/acandiatale/Library/Java/JavaVirtualMachines/corretto-1.8.0_362/Contents/Home}"
DEST="$OUT/com/company/nonexistent/ghost-lib/1.2.3"
mkdir -p "$DEST"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ghost-lib.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/classes"
"$JAVA8_HOME/bin/javac" -nowarn -d "$TMP/classes" $(find "$HERE/src/main/java" -name '*.java')
"$JAVA8_HOME/bin/jar" cf "$DEST/ghost-lib-1.2.3.jar" -C "$TMP/classes" .
cp "$HERE/pom.xml" "$DEST/ghost-lib-1.2.3.pom"
echo "ghost-lib 1.2.3 → $DEST"
