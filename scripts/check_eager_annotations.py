#!/usr/bin/env python3
"""Find NameError and stdlib AttributeError from eager Python 3.13 annotations.
Run on deployment Python, not 3.14; skip strings/future modules and non-runtime imports."""
from __future__ import annotations

import ast
import builtins
import importlib
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "__pycache__", "aur-deckard-git", ".claude"}


def module_level_runtime_bindings(tree: ast.Module) -> set[str]:
    """Names bound at module scope at runtime, TYPE_CHECKING blocks excluded."""
    names: set[str] = set()

    def is_type_checking(test: ast.expr) -> bool:
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"

    for node in tree.body:
        if isinstance(node, ast.If) and is_type_checking(node.test):
            continue                      # bound for the checker only
        for sub in ast.walk(node):
            if isinstance(sub, ast.Import):
                for a in sub.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(sub, ast.ImportFrom):
                for a in sub.names:
                    names.add(a.asname or a.name)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(sub.name)
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
    return names


def annotations_of(tree: ast.Module) -> list[tuple[int, ast.expr]]:
    """Only the annotations the interpreter actually evaluates.
    Include parameters, returns, and module or class AnnAssign nodes; exclude
    function-body AnnAssign nodes under PEP 526.
    """
    out: list[tuple[int, ast.expr]] = []

    def walk_body(body: list[ast.stmt], in_function: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                    if arg is not None and arg.annotation is not None:
                        out.append((arg.lineno, arg.annotation))
                if node.returns is not None:
                    out.append((node.lineno, node.returns))
                walk_body(node.body, True)
            elif isinstance(node, ast.ClassDef):
                walk_body(node.body, in_function)
            elif isinstance(node, ast.AnnAssign):
                if not in_function and node.annotation is not None:
                    out.append((node.lineno, node.annotation))
            else:
                for field in ("body", "orelse", "finalbody"):
                    inner = getattr(node, field, None)
                    if isinstance(inner, list):
                        walk_body([n for n in inner if isinstance(n, ast.stmt)], in_function)
                for handler in getattr(node, "handlers", []):
                    walk_body(handler.body, in_function)
    walk_body(tree.body, False)
    return out


def root_and_path(expr: ast.expr) -> tuple[str, list[str]] | None:
    """("sys", ["UnraisableHookArgs"]) for sys.UnraisableHookArgs."""
    parts: list[str] = []
    cur = expr
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id, list(reversed(parts))
    return None


def check_expr(expr: ast.expr, bound: set[str], report) -> None:
    """Walk one annotation, reporting each name that would not evaluate."""
    if isinstance(expr, ast.Constant):
        return                            # a string annotation is never evaluated
    if isinstance(expr, ast.Subscript):
        check_expr(expr.value, bound, report)
        sl = expr.slice
        for part in (sl.elts if isinstance(sl, ast.Tuple) else [sl]):
            check_expr(part, bound, report)
        return
    if isinstance(expr, ast.BinOp):       # X | Y
        check_expr(expr.left, bound, report)
        check_expr(expr.right, bound, report)
        return
    if isinstance(expr, ast.Tuple):
        for e in expr.elts:
            check_expr(e, bound, report)
        return
    if isinstance(expr, ast.List):
        for e in expr.elts:
            check_expr(e, bound, report)
        return

    rp = root_and_path(expr)
    if rp is None:
        return
    root, attrs = rp
    if root not in bound and not hasattr(builtins, root):
        report(f"NameError: {ast.unparse(expr)} -- {root!r} has no runtime binding")
        return
    if attrs and root in sys.stdlib_module_names:
        try:
            obj = importlib.import_module(root)
        except Exception:
            return
        for a in attrs:
            if not hasattr(obj, a):
                report(f"AttributeError: {ast.unparse(expr)} -- {root} has no {a!r} at runtime")
                return
            obj = getattr(obj, a)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    failures = 0
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        # Match skip names on root-relative parts.
        # An absolute checkout path can contain a skip name and hide the whole tree.
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            src = path.read_text()
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               and any(a.name == "annotations" for a in n.names) for n in tree.body):
            continue                      # deferred, so nothing evaluates
        scanned += 1
        bound = module_level_runtime_bindings(tree)
        for lineno, expr in annotations_of(tree):
            msgs: list[str] = []
            check_expr(expr, bound, msgs.append)
            for m in msgs:
                print(f"{path}:{lineno}: {m}")
                failures += 1
    print(f"\nscanned {scanned} eagerly-annotated modules under Python {sys.version.split()[0]}; "
          f"{failures} annotation(s) would raise at import")
    if scanned == 0:
        print(f"eager-annotation check: found no modules to scan under {root}. "
              "A check that scans nothing reports success and covers nothing.",
              file=sys.stderr)
        return 1
    if sys.version_info >= (3, 14):
        print(f"eager-annotation check: running on {sys.version.split()[0]}, which defers "
              "annotation evaluation (PEP 649). The point of this check is the 3.13 floor, "
              "so run it with python3.13.", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
