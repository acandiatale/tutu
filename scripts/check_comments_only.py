"""Fails when a changed .py file differs from HEAD in anything but comments and docstrings.

Usage: python check_comments_only.py <file> [<file> ...]   (paths relative to the repository root)
For each file: the AST of HEAD's version and of the working tree's version are compared after every docstring
(module, class, function) is removed. Comments are not part of the AST, so only code and non-docstring strings count.
"""
import ast
import subprocess
import sys


def strip(tree: ast.AST) -> ast.AST:
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                n.body = b[1:] or [ast.Pass()]
    return tree


def main(paths: list[str]) -> int:
    bad = 0
    for p in paths:
        old = subprocess.run(["git", "show", f"HEAD:{p}"], capture_output=True, text=True, check=True).stdout
        new = open(p, encoding="utf-8").read()
        a = ast.dump(strip(ast.parse(old)))
        b = ast.dump(strip(ast.parse(new)))
        if a != b:
            bad += 1
            print(f"CODE CHANGED: {p}")
        else:
            print(f"ok (comments / docstrings only): {p}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
