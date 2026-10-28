from __future__ import annotations

import pytest

from browser_mcp.model_client import ModelError, _history_fara_arguments, _normalize_tool_call


def call(keys):
    return {"name": "computer_use", "arguments": {"action": "key", "keys": keys}}


@pytest.mark.parametrize(("keys", "expected"), [
    (["CTRL", "a"], "Control+a"),
    (["ESC"], "Escape"),
    (["RETURN"], "Enter"),
    (["DELETE"], "Delete"),
    (["BACKSPACE"], "Backspace"),
    (["LEFT"], "ArrowLeft"),
    (["SPACEBAR"], "Space"),
    (["Control", "Shift", "ArrowLeft"], "Control+Shift+ArrowLeft"),
    (["CTRL+SHIFT+LEFT"], "Control+Shift+ArrowLeft"),
    (["a", "CTRL"], "Control+a"),
])
def test_fara_key_names_and_modifier_chords_normalize(keys, expected):
    assert _normalize_tool_call(call(keys)) == {"kind": "press_key", "key": expected}


@pytest.mark.parametrize("keys", [
    ["DefinitelyNotAKey"],
    ["b"],
    ["Meta", "v"],
    ["Enter\nTab"],
    ["Enter", "Tab"],
    ["Control", "CTRL", "a"],
    ["Control++a"],
    ["PageDown\r"],
    ["F13"],
])
def test_unknown_multikey_and_malformed_sequences_are_rejected(keys):
    with pytest.raises(ModelError) as error:
        _normalize_tool_call(call(keys))
    assert error.value.code == "MODEL_OUTPUT_INVALID"


def test_duplicate_modifier_or_multiple_physical_key_failure_is_typed_for_bounded_recovery():
    with pytest.raises(ModelError) as error:
        _normalize_tool_call(call(["Control", "a", "c"]))
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert error.value.category == "invalid_key_chord"


def test_applied_key_history_replay_uses_canonical_fara_key_names():
    assert _history_fara_arguments({"kind": "press_key", "key": "CTRL+LEFT"}) == {
        "action": "key", "keys": ["Control", "ArrowLeft"]
    }
    assert _history_fara_arguments({"kind": "press_key", "key": "Enter+Tab"}) is None
