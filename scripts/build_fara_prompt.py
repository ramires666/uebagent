"""Build the checked-in Fara-1.5 prompt and computer-use schema.

Source: microsoft/fara, commit a675d6d61c41c47ae87bacefeab22caad18e3e84,
src/fara/agents/fara/_prompts.py. The source is parsed as Python syntax and
only literal values are extracted; importing its training/runtime dependencies
is intentionally avoided.

The original NousFnCallPrompt formatter is not vendored. This builder renders
its function signature as a JSON array containing the same name, description,
and parameters fields in the source schema. The deployed parser accepts the
trained <tool_call> JSON form; do not infer native-tool compatibility from this
rendered prompt alone.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any


SOURCE_RELATIVE = Path("vendor/fara-reference/_prompts.py")
SOURCE_SHA256 = "727c6c7d3111d28b3508089eb5645020f6ea145c008549bbdf974e78f0a16854"
SOURCE_COMMIT = "a675d6d61c41c47ae87bacefeab22caad18e3e84"
DISPLAY_WIDTH_PX = 1000
DISPLAY_HEIGHT_PX = 1000


def _resolve(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_resolve(item, values) for item in node.elts]
        return items if isinstance(node, ast.List) else tuple(items)
    if isinstance(node, ast.Dict):
        return {
            _resolve(key, values): _resolve(value, values)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.JoinedStr):
        return "".join(_resolve(part, values) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        value = node.value
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
            if value.attr == "display_width_px":
                return str(DISPLAY_WIDTH_PX)
            if value.attr == "display_height_px":
                return str(DISPLAY_HEIGHT_PX)
        raise ValueError("unsupported dynamic expression in pinned description")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _resolve(node.left, values) + _resolve(node.right, values)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "strip"
        and not node.args
        and isinstance(node.func.value, (ast.Constant, ast.JoinedStr))
    ):
        return _resolve(node.func.value, values).strip()
    raise ValueError(f"unsupported non-literal source expression: {type(node).__name__}")


def _source_values(source_path: Path) -> tuple[str, str, str, str, dict[str, Any]]:
    raw = source_path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != SOURCE_SHA256:
        raise ValueError(f"pinned Fara prompt source hash mismatch: {actual_sha}")
    tree = ast.parse(raw, filename=str(source_path))
    constants: dict[str, Any] = {}
    computer_class: ast.ClassDef | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            names = [t.id for t in node.targets]
            if any(name in {"FARA_QWEN35_IDENTITY", "CRITICAL_POINTS_FARA_1_5", "FN_CALL_FORMAT"} for name in names):
                resolved = _resolve(node.value, constants)
                constants.update({name: resolved for name in names})
        elif isinstance(node, ast.ClassDef) and node.name == "FaraBrowserComputerUse":
            computer_class = node
    if computer_class is None:
        raise ValueError("pinned source is missing FaraBrowserComputerUse")
    parameters = None
    description = None
    for node in computer_class.body:
        if isinstance(node, ast.FunctionDef) and node.name == "description":
            return_node = next((part for part in ast.walk(node) if isinstance(part, ast.Return)), None)
            if return_node is not None:
                description = _resolve(return_node.value, {})
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "parameters" for t in node.targets):
            parameters = _resolve(node.value, constants)
    if not isinstance(parameters, dict) or not isinstance(description, str):
        raise ValueError("pinned Fara tool parameters are missing")
    return (
        constants["FARA_QWEN35_IDENTITY"],
        constants["CRITICAL_POINTS_FARA_1_5"],
        constants["FN_CALL_FORMAT"],
        description,
        parameters,
    )


def build(root: Path | None = None) -> tuple[str, dict[str, Any]]:
    root = root or Path(__file__).resolve().parents[1]
    source = root / SOURCE_RELATIVE
    identity, critical_points, fn_call_format, description, parameters = _source_values(source)
    tool = {
        "name": "computer_use",
        "description": description,
        "parameters": parameters,
    }
    tool_descs = json.dumps([tool], ensure_ascii=False, indent=2)
    prompt = identity + "\n\n" + critical_points + "\n\n" + fn_call_format.format(tool_descs=tool_descs)
    return prompt, tool


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    prompt, tool = build(root)
    (root / "config/fara-system-prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
    (root / "config/fara-tool-schema.json").write_text(
        json.dumps(tool, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
