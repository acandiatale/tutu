"""권고 목록(카탈로그) — ``knowledge/libraries/advisories.yml``
(+ ``advisories.extra_files``)을 id 기준으로 합친다. 흐름상 위치: ``evaluate_advisories`` /
``verify_advisories`` / ``sbmig advisories`` 가 ``load_advisories`` 로 읽는다. 나중 파일이 같은 id 를
이긴다(회사 파일이 덮어씀). 없는 파일은 건너뛴다(info 로그). 회사 고유 내용은 YAML 에만 둔다. 권고는 안내만
한다: 항목은 라이브러리·아티팩트·대체품·후속 작업을 적을 뿐 레시피를 돌리지 않는다 (회사 파일에 남은
``policy`` / ``recipes`` 키는 무시).

Advisory catalog: `knowledge/libraries/advisories.yml` (+ `advisories.extra_files`), merged by id.

File schema (version 1): `{version: 1, advisories: [LibraryAdvisory …]}` — see `sbmig.core.models.LibraryAdvisory`
and docs/ADVISORIES.md. Advisories are guide-only (LLM hints + Handover): an entry names the library / artifacts, the
replacement and the follow-up; it never runs a recipe (a `policy` / `recipes` key left in a company file is ignored).
Later files win per id (company overrides); missing files are ignored (logged at info).


"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import yaml
from pydantic import ValidationError

from sbmig.core.config import Config
from sbmig.core.models import CaseType, LibraryAdvisory

log = logging.getLogger("sbmig.advisory")

_CASE_VALUES = {c.value for c in CaseType}


class AdvisoryCatalog:
    """권고 파일들을 id 로 합친 메모리 보기(처음 본 순서 유지). 읽다 생긴 문제는 ``load_problems`` 에 모은다.

    In-memory, id-merged view of the advisory files (entries in first-seen order)."""

    def __init__(self, entries: Optional[Iterable[LibraryAdvisory]] = None, *, sources: Optional[list[Path]] = None,
                 problems: Optional[list[str]] = None):
        self._by_id: dict[str, LibraryAdvisory] = {}
        for e in entries or []:
            self._by_id[e.id] = e
        self.sources: list[Path] = list(sources or [])
        self.load_problems: list[str] = list(problems or [])   # duplicate ids, invalid entries (see check())

    # ---- loading -------------------------------------------------------
    @classmethod
    def from_files(cls, paths: Iterable[str | Path]) -> "AdvisoryCatalog":
        """파일들을 순서대로 합친 카탈로그(뒤 파일이 같은 id 를 이김)."""
        cat = cls()
        for p in paths:
            cat._merge_file(Path(p))
        return cat

    @classmethod
    def from_dict(cls, data: dict, source: str = "<dict>") -> "AdvisoryCatalog":
        """dict(파일 내용과 같은 꼴)에서 카탈로그를 만든다(테스트용)."""
        cat = cls()
        cat._merge_data(data, source)
        return cat

    def _merge_file(self, path: Path) -> None:
        """파일 하나를 합친다. 없으면 건너뛰고, 못 읽으면 문제로 기록 — 예외는 올리지 않는다."""
        if not path.exists():
            log.info("advisory file %s not found; skipped", path)
            return
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("cannot read advisory file %s: %s", path, e)
            self.load_problems.append(f"{path}: unreadable ({type(e).__name__}: {e})")
            return
        self.sources.append(path)
        self._merge_data(data, str(path))

    def _merge_data(self, data: Any, source: str) -> None:
        """항목들을 검증해 합친다. 잘못된 ``applies_to`` 값은 빼고 문제로 남기며, 검증 실패 항목은 건너뛴다
        (항목 하나가 틀렸다고 다른 권고까지 잃지 않게)."""
        if not isinstance(data, dict):
            self.load_problems.append(f"{source}: top level must be a mapping with 'advisories'")
            return
        seen: set[str] = set()
        for i, raw in enumerate(data.get("advisories") or []):
            if isinstance(raw, dict) and raw.get("applies_to") is not None:
                bad = [str(v) for v in raw.get("applies_to") or [] if str(v) not in _CASE_VALUES]
                if bad:
                    self.load_problems.append(f"{source}: advisory {raw.get('id', i)}: bad applies_to "
                                              f"{', '.join(bad)} (known: {', '.join(sorted(_CASE_VALUES))})")
                    raw = {**raw, "applies_to": [v for v in raw["applies_to"] if str(v) in _CASE_VALUES]}
            try:
                entry = LibraryAdvisory.model_validate(raw)
            except ValidationError as e:
                rid = raw.get("id", f"#{i}") if isinstance(raw, dict) else f"#{i}"
                msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
                self.load_problems.append(f"{source}: advisory {rid} invalid: {msg}")
                log.warning("advisory %s in %s invalid: %s", rid, source, msg)
                continue
            if entry.id in seen:
                self.load_problems.append(f"{source}: duplicate advisory id {entry.id}")
            seen.add(entry.id)
            self._by_id[entry.id] = entry           # later wins (also across files)

    # ---- lookups -------------------------------------------------------
    @property
    def entries(self) -> list[LibraryAdvisory]:
        """모든 권고 항목(합쳐진 결과)."""
        return list(self._by_id.values())

    def get(self, advisory_id: str) -> Optional[LibraryAdvisory]:
        """id 로 권고 하나(없으면 None)."""
        return self._by_id.get(advisory_id)

    def ids(self) -> list[str]:
        """권고 id 목록."""
        return list(self._by_id)

    def __contains__(self, advisory_id: object) -> bool:
        return advisory_id in self._by_id

    def __iter__(self) -> Iterator[LibraryAdvisory]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self._by_id)

    # ---- validation ----------------------------------------------------
    def check(self, cfg: Config) -> list[str]:
        """``sbmig advisories check`` 의 검사: 모르는 라이브러리 이름, 잘못된 아티팩트 패턴, 없는 노트 파일,
        중복 id, 잘못된 applies_to. [] 이면 일관됨.

        Problems ([] = consistent): unknown library, bad artifact patterns, missing notes files, duplicate ids,
        bad applies_to values."""
        from sbmig.analyze.libraries import LIBRARY_NAMES

        problems = list(self.load_problems)
        for e in self._by_id.values():
            if e.library is None and not e.artifacts:
                problems.append(f"advisory {e.id}: needs a library or artifacts")
            elif e.library is not None and e.library not in LIBRARY_NAMES and not e.artifacts:
                problems.append(f"advisory {e.id}: unknown library {e.library!r} (not an analyzer rule name; "
                                "add artifacts or a rule in sbmig/analyze/libraries.py)")
            for a in e.artifacts:
                if a.count(":") != 1:
                    problems.append(f"advisory {e.id}: artifact pattern {a!r} must be 'group:artifact-glob'")
            if e.notes and not cfg.resolve_path(e.notes).is_file():
                problems.append(f"advisory {e.id}: notes file {e.notes} does not exist")
        return problems


def load_advisories(cfg: Config) -> AdvisoryCatalog:
    """설정의 권고 파일들(``cfg.advisory_files``)을 id 로 합쳐 읽는다(뒤가 이김). 예외를 내지 않는다 —
    파일이 없으면 권고가 적거나 없을 뿐이다.

    `cfg.advisory_files` merged by id (later wins). Never raises; missing files → fewer / no entries."""
    try:
        files = list(cfg.advisory_files)
    except Exception as e:  # pragma: no cover - config without advisories section
        log.info("no advisory files configured: %s", e)
        return AdvisoryCatalog()
    return AdvisoryCatalog.from_files(files)
