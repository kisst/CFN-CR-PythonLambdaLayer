"""
Verify that the Python embedded as ZipFile inside CFN-CR-PythonLambdaLayer.yaml
is the same program as lambda_function.py — for BOTH builder functions
(x86_64 and arm64).

CloudFormation rejects YAML aliases at deploy time, so the two functions each
carry their own literal copy of the inline source. This script:

  1. Verifies each function's ZipFile is semantically equal to
     lambda_function.py (via ast.unparse, which strips docstrings/comments and
     rewrites whitespace deterministically).
  2. Verifies both ZipFiles are byte-identical to each other — catches the
     case where someone edits one copy but forgets the other.
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACKED = ROOT / "lambda_function.py"
TEMPLATE = ROOT / "CFN-CR-PythonLambdaLayer.yaml"
FUNCTIONS = ("LayerBuilderX8664Function", "LayerBuilderArm64Function")


def strip_docstrings(tree: ast.Module) -> ast.Module:
    """Remove module and function docstrings in-place."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
    return tree


def normalise(source: str) -> str:
    return ast.unparse(strip_docstrings(ast.parse(source)))


def extract_inline(function_name: str) -> str:
    query = f".Resources.{function_name}.Properties.Code.ZipFile"
    result = subprocess.run(
        ["yq", query, str(TEMPLATE)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def diff(label_a: str, a: str, label_b: str, b: str) -> None:
    import difflib

    sys.stdout.writelines(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=label_a,
            tofile=label_b,
        )
    )


def main() -> int:
    tracked_norm = normalise(TRACKED.read_text())
    failures = 0
    inlines = {}
    for fn in FUNCTIONS:
        raw = extract_inline(fn)
        inlines[fn] = raw
        if normalise(raw) == tracked_norm:
            print(f"OK: {fn} ZipFile matches lambda_function.py")
        else:
            print(f"FAIL: {fn} ZipFile has diverged from lambda_function.py", file=sys.stderr)
            diff(
                "lambda_function.py (normalised)",
                tracked_norm,
                f"{fn} (normalised)",
                normalise(raw),
            )
            failures += 1

    if len(set(inlines.values())) == 1:
        print(f"OK: all {len(FUNCTIONS)} ZipFile copies are byte-identical")
    else:
        print("FAIL: ZipFile copies drifted from each other", file=sys.stderr)
        fn_a, fn_b = FUNCTIONS[0], FUNCTIONS[1]
        diff(fn_a, inlines[fn_a], fn_b, inlines[fn_b])
        failures += 1

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
