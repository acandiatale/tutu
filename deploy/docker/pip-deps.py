"""pyproject.toml 의 런타임 의존성을 한 줄에 하나씩 출력한다 — 이미지는 프로젝트가 선언한 것을 그대로 설치한다
(의존성 목록을 두 곳에 두고 맞춰 가는 일을 없애기 위해). 사용: python pip-deps.py pyproject.toml | xargs pip install"""
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    for dep in tomllib.load(f)["project"]["dependencies"]:
        print(dep)
