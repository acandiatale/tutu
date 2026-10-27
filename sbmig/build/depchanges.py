"""v2.1d dependency version policy — what a fix changed in the poms (docs/INTERFACES.md "v2.1d").

`dependency_changes(before, after, path)` reads two versions of ONE pom and lists every coordinate change as
`DependencyChange` (dependencies, dependencyManagement, plugins, pluginManagement, `<parent>`): kind `major` when the
major number of the version changed, `replaced` when one library was removed and another added that stands in for it
(same artifactId, or one artifactId contains the other), `minor` / `added` / `removed` otherwise. `${property}` versions
are resolved from the same pom's `<properties>` where possible. Pure text in, never raises (a pom that does not parse
→ []).

`changes_in_commit(ws, pre, sha)` does it for every `pom.xml` changed between two commits; `apply_table(changes,
libraries)` marks the changes a person's table entry allows; `still_applied(ws, change)` says whether the final tree
still carries it (a rolled-back try is not reported); `synthesized_guide(changes, lang)` is the operator guide sbmig
writes when the LLM gave no `<verify>`.

STUB (contract only, 2026-10-27): the signatures below are the contract; the implementation is agent V's.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sbmig.core.models import DependencyChange


def major_of(version: Optional[str]) -> Optional[int]:
    """Leading number of a Maven version (`2.4.2` → 2, `3.3.0-RC1` → 3, `${x}` / "" / None → None)."""
    raise NotImplementedError


def dependency_changes(before: str, after: str, path: str) -> list[DependencyChange]:
    """Coordinate changes between two texts of the pom at `path` (project-relative posix). Never raises."""
    raise NotImplementedError


def changes_in_commit(ws: Any, pre: str, sha: str) -> list[DependencyChange]:
    """`dependency_changes` over every pom.xml changed between commits `pre` and `sha` (`ws.file_at(sha, path)`).
    Never raises; [] when git cannot tell."""
    raise NotImplementedError


def apply_table(changes: Iterable[DependencyChange], libraries: Iterable[Any]) -> None:
    """Set `approved` / `note` on the changes a table entry (`JakartaLibrary`) allows: `allow_major` on the entry of
    `key` or `replaced_by`, or the entry's `version` == `after`."""
    raise NotImplementedError


def still_applied(ws: Any, change: DependencyChange) -> bool:
    """Does the working tree's pom at `change.path` still carry the change (the `after` version of `key` /
    `replaced_by`)? Used at report time to drop rolled-back tries. Never raises (unreadable → False)."""
    raise NotImplementedError


def synthesized_guide(changes: Iterable[DependencyChange], lang: str = "ko") -> str:
    """The 운영자 확인 사항 sbmig writes when the LLM gave none: one line per change naming the coordinates, the
    major line crossed and the generic checks (configuration files / registrations / renamed packages / runtime)."""
    raise NotImplementedError
