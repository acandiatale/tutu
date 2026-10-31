"""sbmig.review building blocks: the answer parser (tolerant JSON), pom cutting to the prompt budget, the module
tree / facts / rules shown to the LLM, encodings, the system prompt."""
from __future__ import annotations

from pathlib import Path

from sbmig.core.config import load_config
from sbmig.core.models import CaseType, ProjectProfile, SourceStats
from sbmig.core.workspace import Workspace
from sbmig.review import collect_review_input, parse_review_answer, system_prompt
from sbmig.review.inputs import allot, compatible, cut_text, java_charset
from sbmig.review.reviewer import FALLBACK_HELPER_API, helper_api_doc

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------- parsing
def test_parse_plain_review_and_script():
    p = parse_review_answer('<review>{"verdict":"issues","issues":[{"severity":"critical","file":"pom.xml",'
                            '"problem":"p","fix_hint":"h"}],"analysis":"a"}</review>\n'
                            "<script>\ndef fix(p):\n    p.log('x')\n</script>")
    assert p.ok and p.verdict == "issues" and p.analysis == "a"
    assert [(i.severity, i.file, i.problem, i.fix_hint) for i in p.issues] == [("critical", "pom.xml", "p", "h")]
    assert p.script == "def fix(p):\n    p.log('x')\n"


def test_parse_fences_trailing_text_and_commas():
    text = ("Here is my review:\n```json\n{\"verdict\": \"issues\", \"issues\": [{\"severity\": \"Critical\", "
            "\"path\": \"a\\\\pom.xml\", \"issue\": \"x\", \"fix\": \"y\"},],}\n```\nThanks!")
    p = parse_review_answer(text)
    assert p.ok and p.issues[0].severity == "critical" and p.issues[0].file == "a/pom.xml"
    assert p.issues[0].problem == "x" and p.issues[0].fix_hint == "y"
    p = parse_review_answer('<review>\n{"verdict": "ok", "issues": []}\nI hope this helps.</review>')
    assert p.ok and p.verdict == "ok" and p.issues == []


def test_parse_single_object_unknown_severity_and_contradictions():
    p = parse_review_answer('<review>{"severity": "high", "file": "pom.xml", "problem": "dup jackson"}</review>')
    assert p.ok and p.issues[0].severity == "warning" and any("unknown severity" in w for w in p.warnings)
    p = parse_review_answer('{"verdict": "ok", "issues": {"severity": "critical", "problem": "z"}}')
    assert p.verdict == "issues" and p.critical[0].problem == "z"
    p = parse_review_answer('<review>{"verdict": "issues", "issues": []}</review>')
    assert p.ok and p.verdict == "ok"
    p = parse_review_answer('[{"severity": "warning", "problem": "w"}, {"problem": ""}]')
    assert p.ok and [i.problem for i in p.issues] == ["w"]


def test_parse_failures_and_scripts_without_fix():
    assert not parse_review_answer("").ok and parse_review_answer("").error == "empty answer"
    bad = parse_review_answer("<analysis>no fix</analysis><patches></patches>")
    assert not bad.ok and bad.error == "no review JSON in the answer"
    p = parse_review_answer('<review>{"verdict":"ok","issues":[]}</review><script>print("x")</script>')
    assert p.ok and p.script == ""                                    # no def fix(p) → no script
    p = parse_review_answer('<analysis>the analysis</analysis>\n<review>{"verdict": "ok"}</review>')
    assert p.analysis == "the analysis"
    p = parse_review_answer('{"verdict": "issues", "issues": [{"severity": "critical", "problem": "c"}], '
                            '"script": "```python\\ndef fix(p):\\n    pass\\n```"}')
    assert p.script == "def fix(p):\n    pass\n"


# ---------------------------------------------------------------------------- pom budget
def test_allot_and_cut():
    assert allot([100, 5000, 200], 1000) == [100, 700, 200]
    assert sum(allot([900, 900, 900], 1000)) <= 1000
    text = "".join(f"<line{i}>value {i}</line{i}>\n" for i in range(400))
    cut, was_cut = cut_text(text, 1200)
    assert was_cut and len(cut) <= 1200 and cut.startswith("<line0>") and "<line399>" in cut
    assert "characters cut here (prompt budget)" in cut
    assert cut_text(text, len(text)) == (text, False)
    tiny, was_cut = cut_text(text, 150)
    assert was_cut and "not shown" in tiny and len(tiny) <= 150


def _tree(tmp_path: Path, n_modules: int, pom_padding: int = 0) -> Workspace:
    root = tmp_path / "p"
    root.mkdir()
    mods = "".join(f"<module>m{i}</module>" for i in range(n_modules))
    (root / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0'
                                  "</modelVersion><groupId>g</groupId><artifactId>demo</artifactId><version>1"
                                  f"</version><packaging>pom</packaging><modules>{mods}<module>gone</module>"
                                  "</modules></project>\n")
    pad = "".join(f"\n  <!-- padding line {j} -->" for j in range(pom_padding))
    for i in range(n_modules):
        (root / f"m{i}").mkdir()
        (root / f"m{i}/pom.xml").write_text(
            f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>{pad}\n'
            f"<parent><groupId>g</groupId><artifactId>demo</artifactId><version>1</version></parent>"
            f"<artifactId>m{i}</artifactId><dependencies>"
            "<dependency><groupId>javax.servlet</groupId><artifactId>javax.servlet-api</artifactId>"
            "<version>4.0.1</version><scope>provided</scope></dependency>"
            "<dependency><groupId>jakarta.servlet</groupId><artifactId>jakarta.servlet-api</artifactId>"
            "</dependency><dependency><groupId>org.x</groupId><artifactId>dup</artifactId><version>1</version>"
            "</dependency><dependency><groupId>org.x</groupId><artifactId>dup</artifactId><version>2</version>"
            "</dependency></dependencies></project>\n")
    (root / "orphan").mkdir()
    (root / "orphan/pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0'
                                         "</modelVersion><groupId>g</groupId><artifactId>orphan</artifactId>"
                                         "<version>1</version></project>\n")
    return Workspace(root, tmp_path / "run", "r")


def test_pom_budget_and_max_poms(tmp_path, cfg):
    ws = _tree(tmp_path, 4, pom_padding=300)
    small = cfg.model_copy(update={"review": cfg.review.model_copy(update={"max_pom_chars": 9000, "max_poms": 4})})
    inp = collect_review_input(ws, None, small)
    assert [p.path for p in inp.poms] == ["pom.xml", "m0/pom.xml", "m1/pom.xml", "m2/pom.xml"]
    assert inp.omitted_poms == ["m3/pom.xml", "orphan/pom.xml"]
    assert inp.pom_chars <= 9000 and inp.poms[0].cut is False and all(p.cut for p in inp.poms[1:])
    text = inp.render()
    assert "characters, cut)" in text and "Not shown (review.max_poms): `m3/pom.xml`, `orphan/pom.xml`" in text
    assert all("<artifactId>" in p.text for p in inp.poms)            # head kept
    assert all("</project>" in p.text for p in inp.poms)              # tail kept


def test_module_tree_and_facts(tmp_path, cfg):
    ws = _tree(tmp_path, 2)
    text = collect_review_input(ws, None, cfg).render()
    assert "`gone` (**MISSING: no pom.xml**)" in text
    assert "Poms no aggregator lists as a module: `orphan/pom.xml`" in text
    assert "| `m0/pom.xml` | `g:m0` | jar | `g:demo:1` (in project) | - |" in text
    assert "javax.* API dependencies declared: `javax.servlet:javax.servlet-api:4.0.1` (provided) in `m0/pom.xml`" in text
    assert "jakarta.* API dependencies declared:" in text
    assert "- `m0/pom.xml` declares `org.x:dup` 2 times in dependencies: 1, 2" in text
    assert "- mandatory `org.springframework.boot:spring-boot-starter-validation`: **not declared in any pom**" in text


def test_binary_files_under_a_filtered_resource_are_a_fact(tmp_path, cfg):
    """v1 kept webfonts / Excel / media byte-identical with a pre-emptive handler (E12); v2 has no handlers, so the
    review is told which binaries a filtered <resource> would rewrite (2026-10-26 e2e: servlet-nonstandard-dirs)."""
    root = tmp_path / "p"
    (root / "back/STATIC/resources/media").mkdir(parents=True)
    (root / "back/pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0'
                                       "</modelVersion><groupId>g</groupId><artifactId>back</artifactId><version>1"
                                       "</version><build><resources><resource><directory>STATIC/resources</directory>"
                                       "<filtering>true</filtering></resource></resources></build></project>\n")
    (root / "back/STATIC/resources/sample.xlsx").write_bytes(b"PK\x03\x04\x00\x00binary\xff\xfe")
    (root / "back/STATIC/resources/media/intro.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\xff")
    (root / "back/STATIC/resources/messages.properties").write_text("title=${project.name}\n")
    ws = Workspace(root, tmp_path / "run", "r")
    text = collect_review_input(ws, None, cfg).render()
    line = next(ln for ln in text.splitlines() if ln.startswith("- binary files under a filtered `<resource>`"))
    assert "`back/STATIC/resources/sample.xlsx`" in line and "`back/STATIC/resources/media/intro.mp4`" in line
    assert "messages.properties" not in line and line.endswith("extensions: mp4, xlsx")
    assert "nonFilteredFileExtensions" in system_prompt(cfg)
    # no filtering → no fact
    (root / "back/pom.xml").write_text((root / "back/pom.xml").read_text().replace("<filtering>true</filtering>", ""))
    assert "binary files under a filtered" not in collect_review_input(ws, None, cfg).render()


def test_company_rules_come_from_the_config(tmp_path):
    company = load_config(REPO / "config" / "artifacts.example.yml", repo_root=REPO)
    ws = _tree(tmp_path, 1)
    prof = ProjectProfile(root="/x", name="demo", case_type=CaseType.SERVLET_TOMCAT,
                          source=SourceStats(java_files=3, jsp_count=2, has_web_xml=True, web_xml_spec="2.5"))
    text = collect_review_input(ws, prof, company).render()
    a = company.artifacts
    assert f"`{a.parent.key}:{a.parent.version}`" in text and f"`{a.boot_parent.key}:{a.boot_parent.version}`" in text
    assert f"`{a.comp_parent.key}:{a.comp_parent.version}`" in text
    assert all(s.key in text for s in a.mandatory) and all(s.key in text for s in a.optional)
    assert "`demo-boot`: Boot configuration only" in text and "packaging war (executable WAR: `java -jar`" in text
    assert "external Tomcat)" not in text and "stay `provided`" in text            # v2.1u (D-H4)
    assert "`demo-back`: every source" in text and "`demo-front`: web resources only" in text
    assert "case `servlet_tomcat`" in text and "web: web.xml 2.5, 2 JSP(s)" in text


def test_encoding_helpers():
    assert java_charset("cp949") == "MS949" and java_charset("euc_kr") == "EUC-KR"
    assert compatible("cp949", "MS949") and compatible("cp949", "EUC-KR") and compatible("euc_kr", "x-windows-949")
    assert not compatible("cp949", None) and not compatible("cp949", "UTF-8") and compatible("utf-8", None)
    assert compatible("ascii", "MS949") and not compatible("shift_jis", "MS949")


# ---------------------------------------------------------------------------- prompt
def test_system_prompt(cfg):
    extra = cfg.model_copy(update={"script": cfg.script.model_copy(update={"extra_allowed_imports": ["xml.dom"]}),
                                   "review": cfg.review.model_copy(update={"ask_script": True})})
    text = system_prompt(extra)
    assert "math, xml.dom." in text and "## Answer format" in text and "critical" in text
    assert "p.pom(" in text and "{{" not in text and "<!--" not in text
    assert "`<changes>` is\nREQUIRED" in text and "No critical issue → no `<edits>` and no `<script>`." in text
    # v2.1y (decision 2): the default is edits-only — the marked script parts leave the prompt
    plain = system_prompt(cfg)
    assert "p.pom(" not in plain and "<script>" not in plain and "Script rules" not in plain and "<!--" not in plain
    assert "No critical issue → no `<edits>`." in plain and "{{" not in plain and len(plain) < len(text) - 3000
    doc = helper_api_doc()
    assert doc.lstrip().startswith("|") and "p.pom(" in doc            # the table, without the fix-loop answer format
    assert FALLBACK_HELPER_API.startswith("| API |")
