"""Static check of fix scripts (sbmig.script.check): imports, refused names / modules / attributes, fix(p)."""
from __future__ import annotations

import pytest

from sbmig.script import ALLOWED_IMPORTS, HELPER_API_DOC, check_script, helper_api_doc
from sbmig.script.check import REFUSED_MODULES, REFUSED_NAMES, import_allowed, module_allowed

OK = "def fix(p):\n    pass\n"


def _one(code: str, **kw) -> str:
    problems = check_script(code, **kw)
    assert problems, f"expected a problem for:\n{code}"
    return problems[0]


def test_minimal_script_is_allowed():
    assert check_script(OK) == []
    code = ("import re\nimport json, textwrap\nfrom collections import defaultdict\nimport collections.abc\n"
            "from functools import reduce\nimport itertools as it\nimport fnmatch, posixpath, string, math\n"
            "PAT = re.compile(r'javax\\.(\\w+)')\n\nclass Edit:\n    pass\n\n"
            "def helper(text):\n    return PAT.sub(r'jakarta.\\1', text)\n\n"
            "def fix(p):\n    for path in p.files('**/*.java'):\n        p.write(path, helper(p.read(path)))\n"
            "    if __name__ == '__main__':\n        pass\n")
    assert check_script(code) == []


@pytest.mark.parametrize("module", REFUSED_MODULES)
def test_refused_modules(module):
    assert _one(f"import {module}\n{OK}") == f"line 1: import of '{module}' is not allowed"
    assert _one(f"from {module} import x\n{OK}").startswith(f"line 1: import of '{module}' is not allowed")


def test_refused_module_via_alias_submodule_and_function_body():
    assert _one(f"import os as o\n{OK}") == "line 1: import of 'os' is not allowed"
    assert _one(f"import os.path as osp\n{OK}") == "line 1: import of 'os.path' is not allowed"
    assert _one(f"from os import path as re\n{OK}") == "line 1: import of 'os' is not allowed"
    assert _one("def fix(p):\n    import subprocess\n") == "line 2: import of 'subprocess' is not allowed"


def test_not_whitelisted_module_lists_the_whitelist():
    msg = _one(f"import requests\n{OK}")
    assert msg.startswith("line 1: import of 'requests' is not allowed (allowed: re, json")
    assert _one(f"import sbmig.core\n{OK}").startswith("line 1: import of 'sbmig.core' is not allowed")
    assert _one(f"from . import x\n{OK}").startswith("line 1: relative import")
    assert _one(f"from json import _default_decoder\n{OK}") == \
        "line 1: import of private name '_default_decoder' from 'json' is not allowed"


@pytest.mark.parametrize("name", REFUSED_NAMES)
def test_refused_names(name):
    msg = _one(f"def fix(p):\n    x = {name}\n")
    assert msg.startswith(f"line 2: '{name}' is not allowed")


def test_refused_name_in_a_call_and_as_store():
    assert _one("def fix(p):\n    open('x').read()\n") == \
        "line 2: 'open' is not allowed (use p.read / p.write / p.create)"
    assert _one("def fix(p):\n    eval = 1\n") == "line 2: 'eval' is not allowed"
    assert _one(f"import json as eval\n{OK}") == "line 1: import alias 'eval' is not allowed"
    assert _one("def fix(p):\n    return __builtins__\n") == "line 2: name '__builtins__' is not allowed"


@pytest.mark.parametrize("attr", ["__class__", "__globals__", "__subclasses__", "_root", "_ed", "__dict__"])
def test_underscore_attributes_are_refused(attr):
    assert _one(f"def fix(p):\n    return p.{attr}\n") == \
        f"line 2: attribute '{attr}' is not allowed (names starting with '_' are refused)"


def test_module_reached_through_an_allowed_module_is_refused():
    assert _one("import posixpath\ndef fix(p):\n    posixpath.os.remove('x')\n") == \
        "line 3: attribute '.os' is not allowed"
    assert set(check_script("import json\ndef fix(p):\n    json.codecs.open('x')\n")) == {
        "line 3: attribute '.open' is not allowed", "line 3: attribute '.codecs' is not allowed"}


def test_re_compile_and_format_are_fine():
    assert check_script("import re\ndef fix(p):\n    re.compile('x').sub('y', p.read('a'))\n"
                        "    p.log('{}: {}'.format(1, 2))\n") == []


@pytest.mark.parametrize("code, expected", [
    ("def other(p):\n    pass\n", "no module-level 'def fix(p)' found"),
    ("class X:\n    def fix(self, p):\n        pass\n", "no module-level 'def fix(p)' found"),
    ("def fix():\n    pass\n", "line 1: 'fix' must take exactly one positional parameter (the helper p), found 0"),
    ("def fix(p, q):\n    pass\n", "line 1: 'fix' must take exactly one positional parameter (the helper p), found 2"),
    ("def fix(*args):\n    pass\n", "line 1: 'fix' must not take *args; define it as 'def fix(p)'"),
    ("def fix(p, *, dry):\n    pass\n", "line 1: keyword-only parameter 'dry' of 'fix' needs a default"),
    ("async def fix(p):\n    pass\n", "line 1: 'fix' must be a plain function, not 'async def'"),
])
def test_fix_signature(code, expected):
    assert expected in check_script(code)


def test_fix_signature_variants_that_are_fine():
    assert check_script("def fix(p, *, dry=False):\n    pass\n") == []
    assert check_script("def fix(p=None):\n    pass\n") == []
    assert check_script("def fix(p, **kw):\n    pass\n") == []


def test_syntax_error_length_and_empty():
    assert _one("def fix(p):\n    x = (\n").startswith("line 2: syntax error")
    assert _one("x" * 31 + "\n" + OK, max_chars=30).startswith("the script is ")
    assert "limit 30" in _one("x" * 31 + "\n" + OK, max_chars=30)
    assert _one("") == "the script is empty; define 'def fix(p)'"
    assert _one(None) == "the script is not text (NoneType)"            # type: ignore[arg-type]
    assert "null bytes" in _one("def fix(p):\n    x = 1\x00\n")
    assert _one("return 1\n" + OK) == "line 1: syntax error: 'return' outside function"


def test_problems_are_capped_and_deduplicated():
    code = "def fix(p):\n" + "".join(f"    open('{i}')\n" for i in range(30))
    problems = check_script(code)
    assert len(problems) == 21 and problems[-1] == "… 10 more problem(s)"
    assert check_script("import os\nimport os\n" + OK) == ["line 1: import of 'os' is not allowed",
                                                          "line 2: import of 'os' is not allowed"]


def test_extra_imports():
    assert check_script(f"import datetime\n{OK}", extra_imports=["datetime"]) == []
    assert check_script(f"import xml.etree.ElementTree as ET\n{OK}", extra_imports=["xml.etree.ElementTree"]) == []
    assert check_script(f"from xml.etree import ElementTree\n{OK}", extra_imports=["xml.etree.ElementTree"]) == []
    assert check_script(f"import xml.dom\n{OK}", extra_imports=["xml.etree.ElementTree"])
    assert check_script(f"import os\n{OK}", extra_imports=["os"]) == ["line 1: import of 'os' is not allowed"]


def test_import_rule_helpers():
    assert all(module_allowed(m) for m in ALLOWED_IMPORTS)
    assert module_allowed("collections.abc") and not module_allowed("os.path") and not module_allowed("sbmig")
    assert not module_allowed("") and not module_allowed(".x")
    assert import_allowed("collections", ("abc",))
    assert not import_allowed("os", ("path",))
    assert import_allowed("xml.etree", ("ElementTree",), ("xml.etree.ElementTree",))


def test_helper_api_doc_names_the_api_and_rules():
    for needle in ("p.errors", "p.files(", "p.read(", "p.write(", "p.replace(", "p.sub(", "p.create(", "p.delete(",
                   "p.move(", "p.pom(", "add_exclusion", "set_plugin_version", "p.log(", "`re`", "`math`", "`open`",
                   "`subprocess`", "PathRefused", "def fix(p)"):
        assert needle in HELPER_API_DOC, needle
    doc = helper_api_doc(["datetime"], timeout_s=60)
    assert "`datetime`" in doc and "60 s" in doc
