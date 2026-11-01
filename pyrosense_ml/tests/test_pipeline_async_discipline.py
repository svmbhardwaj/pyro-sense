"""Pipeline async-discipline regression tests (plan §6).

The §6 production bug was `session.execute(...)` used WITHOUT await inside
aggregate_day — `.all()` on a coroutine raises AttributeError only when the
stage actually runs against data, which is why it slipped past smoke tests.

These tests are static (AST-based), need no DB, and fail on the BUG CLASS,
not just the one line: any Call on `.execute(` that is not immediately
wrapped in an Await anywhere under app/pipeline/ is rejected.
"""

from __future__ import annotations

import ast
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "app" / "pipeline"


def _unawaited_execute_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Find `<anything>.execute(...)` calls not wrapped in `await`."""
    offenders: list[tuple[str, int]] = []

    # First pass: collect line numbers of every Await that wraps an execute call.
    awaited_lines: set[int] = set()

    class AwaitVisitor(ast.NodeVisitor):
        def visit_Await(self, node: ast.Await) -> None:  # noqa: N802
            # await session.execute(...) — record the inner call's line
            if isinstance(node.value, ast.Call):
                awaited_lines.add(node.value.lineno)
            self.generic_visit(node)

    AwaitVisitor().visit(tree)

    class ExecuteVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "execute":
                receiver = ast.dump(func.value)
                if "session" in receiver and node.lineno not in awaited_lines:
                    offenders.append((receiver, node.lineno))
            self.generic_visit(node)

    ExecuteVisitor().visit(tree)
    return offenders


def test_no_unawaited_session_execute_in_pipeline():
    offenders: list[str] = []
    for path in sorted(PIPELINE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for receiver, lineno in _unawaited_execute_calls(tree):
            offenders.append(f"{path.name}:{lineno} un-awaited {receiver}.execute()")
    assert offenders == [], "un-awaited session.execute calls found:\n" + "\n".join(offenders)


def test_aggregate_day_regression_specifically():
    """The §6 line: aggregate_day's first select must be awaited."""
    src = (PIPELINE_DIR / "h3_aggregate.py").read_text()
    assert "await session.execute(\n            select(LiveDetection.latitude" in src, (
        "aggregate_day's detection-position query lost its await — §6 bug reintroduced"
    )


def test_execute_call_sites_are_awaits_in_api_layer_too():
    """Cheap extension: the same bug class in app/api and app/data."""
    for subdir in ("api", "data"):
        base = Path(__file__).resolve().parent.parent / "app" / subdir
        for path in sorted(base.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            offenders = _unawaited_execute_calls(tree)
            assert offenders == [], f"{path.name}: {offenders}"
