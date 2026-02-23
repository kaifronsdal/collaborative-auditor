from __future__ import annotations

import ast
from textwrap import dedent

from inspect_ai.tool import ToolDef


def parse_function_code(function_code: str) -> ToolDef:
    """Parse Python function code and build a safe ToolDef."""
    function_code = dedent(function_code)
    parsed = ast.parse(function_code.strip())

    if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.FunctionDef):
        raise ValueError("Code must contain exactly one function definition and nothing else")

    func_def = parsed.body[0]
    docstring = ast.get_docstring(func_def)
    if docstring is None:
        raise ValueError("Function must have a docstring")

    func_def.body = [ast.Expr(value=ast.Constant(value=docstring))]
    func_def.body.append(
        ast.Raise(
            exc=ast.Call(
                func=ast.Name(id="ValueError", ctx=ast.Load()),
                args=[ast.Constant(value="This tool should have never been called!")],
                keywords=[],
            )
        )
    )

    for arg in func_def.args.defaults:
        if not isinstance(arg, ast.Constant):
            raise ValueError(f"Argument defaults must be constants, found: {type(arg)}")

    processed_code = ast.unparse(func_def)
    namespace: dict[str, object] = {}
    exec(processed_code, {}, namespace)
    synthetic_tool_func = namespace[func_def.name]
    return ToolDef(synthetic_tool_func)
