"""A reactor module whose pom declares EUC-KR must still be found by the log parser's module walk.

The pre-conversion correction writes `encoding="EUC-KR"` into poms whose Korean text is saved that way; the standard
library's XML parser refuses multi-byte declarations when it is given bytes, so the walk has to decode first."""
from __future__ import annotations

from sbmig.build.log_parser import reactor_artifact_ids_from_poms


def _pom(artifact: str, encoding: str, declared: str | None) -> bytes:
    decl = f'<?xml version="1.0" encoding="{declared}"?>\n' if declared else ""
    text = (f"{decl}<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n  <modelVersion>4.0.0</modelVersion>\n"
            f"  <groupId>com.example</groupId>\n  <artifactId>{artifact}</artifactId>\n  <version>1</version>\n"
            "  <name>인사말 모듈</name>\n</project>\n")
    return text.encode(encoding)


def test_module_names_are_read_from_poms_in_korean_encodings(tmp_path):
    cases = {"utf8": ("utf-8", "UTF-8"), "euckr": ("euc-kr", "EUC-KR"), "mismatch": ("cp949", "UTF-8"),
             "nodecl": ("cp949", None)}
    for name, (enc, declared) in cases.items():
        d = tmp_path / name
        d.mkdir()
        (d / "pom.xml").write_bytes(_pom(f"mod-{name}", enc, declared))
    assert reactor_artifact_ids_from_poms(tmp_path) == sorted(f"mod-{n}" for n in cases)
