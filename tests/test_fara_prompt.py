from __future__ import annotations

import json
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BUILDER_SPEC = importlib.util.spec_from_file_location("build_fara_prompt", ROOT / "scripts/build_fara_prompt.py")
assert _BUILDER_SPEC and _BUILDER_SPEC.loader
_BUILDER = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(_BUILDER)
SOURCE_COMMIT = _BUILDER.SOURCE_COMMIT
SOURCE_SHA256 = _BUILDER.SOURCE_SHA256
build = _BUILDER.build


def test_generated_fara_prompt_and_schema_match_pinned_source() -> None:
    prompt, tool = build(ROOT)
    persisted_prompt = (ROOT / "config/fara-system-prompt.txt").read_text(encoding="utf-8")
    persisted_tool = json.loads((ROOT / "config/fara-tool-schema.json").read_text(encoding="utf-8"))

    assert persisted_prompt == prompt
    assert persisted_tool == tool
    assert "You are Fara, a computer use agent" in prompt
    assert "Only stop at a critical point" in prompt
    assert "<tools>" in prompt and "<tool_call>" in prompt
    assert "source hash" not in prompt.lower()
    assert tool["name"] == "computer_use"
    assert tool["parameters"]["type"] == "object"
    actions = tool["parameters"]["properties"]["action"]["enum"]
    assert {"mouse_move", "web_search", "left_click_drag", "read_page_answer_question"}.issubset(actions)
    assert "1000x1000" in tool["description"]


def test_source_pin_provenance_matches_checked_in_reference() -> None:
    source = ROOT / "vendor/fara-reference/_prompts.py"
    import hashlib

    assert hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA256
    metadata = json.loads((ROOT / "vendor/fara-reference/source.json").read_text(encoding="utf-8"))
    assert metadata["commit"] == SOURCE_COMMIT

