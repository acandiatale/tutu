"""v2.1d dependency version policy — what the repository has for a library (docs/INTERFACES.md "v2.1d").

`RepositoryLookup(cfg)` reads `maven-metadata.xml` (and candidate poms) from ONE base URL: `build.repository_url`,
else the catch-all `<mirror>` of `build.settings_file` (with the matching `<server>` credentials, `${env.X}`
resolved; an encrypted password → no auth, logged once), else Maven Central unless `build.offline`. Every HTTP call
has `build.repository_timeout_seconds`; the local repository (`<local repo>/<g>/<a>/`) is the fallback when the URL
gives nothing. Results are cached per instance (one run). Never raises: a failed lookup is a `RepositoryVersions`
with `error` set.

`candidates(key, current)` → `RepositoryVersions`: the same-major releases newer than `current` (oldest → newest,
prereleases only with `libraries.include_prerelease`), the newest release of every higher major, and for up to
`libraries.max_pom_checks` candidate poms (newest same-major first, then one per higher major) whether the release is
a jakarta build (its pom declares a `jakarta.*` dependency or a `jakarta` classifier / artifact) and the minimum Java
it states. `hint_lines(rv, entry)` renders the prompt lines ("Dependency versions" section).

STUB (contract only, 2026-10-27): the signatures below are the contract; the implementation is agent V's.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from sbmig.core.config import Config
from sbmig.core.models import RepositoryVersions

Fetch = Callable[[str, Optional[tuple[str, str]], float], Optional[str]]   # (url, (user, password) | None, timeout) → text


def parse_version(v: str) -> tuple:
    """A sortable key for Maven versions (numeric parts, qualifiers lower than a release; never raises)."""
    raise NotImplementedError


def is_prerelease(v: str) -> bool:
    """milestone / RC / alpha / beta / SNAPSHOT / preview spellings."""
    raise NotImplementedError


def mirror_from_settings(path: str) -> Optional[tuple[str, Optional[tuple[str, str]]]]:
    """(url, credentials) of the catch-all mirror (`<mirrorOf>*</mirrorOf>` / `external:*`, else the first mirror) of
    a settings.xml; credentials from the `<server>` with the mirror's id (`${env.X}` resolved; encrypted → None).
    None when the file has no mirror / cannot be read."""
    raise NotImplementedError


class RepositoryLookup:
    def __init__(self, cfg: Config, *, fetch: Optional[Fetch] = None) -> None:
        raise NotImplementedError

    def repository_url(self) -> Optional[str]:
        """The base URL used (trailing `/`), None when none (offline without a mirror)."""
        raise NotImplementedError

    def versions(self, group_id: str, artifact_id: str) -> list[str]:
        """Every release of the artifact the repository / local repository knows, sorted (oldest → newest)."""
        raise NotImplementedError

    def pom_text(self, group_id: str, artifact_id: str, version: str) -> Optional[str]:
        raise NotImplementedError

    def candidates(self, key: str, current: Optional[str]) -> RepositoryVersions:
        """See the module docstring. `key` = groupId:artifactId."""
        raise NotImplementedError


def hint_lines(rv: RepositoryVersions, entry: Any = None) -> list[str]:
    """Prompt lines for one library: the table entry first (`entry` = JakartaLibrary or None: "approved: raise to
    2.6.0 (jakarta since 2.6.0; <note>)" / "approved: 3.3.0 may be used (a person allowed the major upgrade)"), then
    what the repository has ("same major 2.x newer than 2.4.2: 2.5.0, 2.6.0 (jakarta ✓)"; "higher majors: 3.3.0
    (jakarta ✓, first jakarta release 3.2.0)"), or the lookup error in one line. Never raises."""
    raise NotImplementedError
