"""Print the runtime dependencies of pyproject.toml, one per line — the image installs exactly what the project declares
(no second list to keep in sync). Usage: python pip-deps.py /path/to/pyproject.toml | xargs pip install"""
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    for dep in tomllib.load(f)["project"]["dependencies"]:
        print(dep)
