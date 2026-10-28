from __future__ import annotations

import base64
import json

import httpx
import pytest

from browser_mcp.model_client import ModelClient, ModelError, ModelSettings, _history_fara_arguments


PNG = b"\x89PNG\r\n\x1a\nfixture"


def xml_call(action: dict[str, object]) -> dict[str, object]:
    call = {"name": "computer_use", "arguments": action}
    return {"choices": [{"message": {"role": "assistant", "content": f"Reasoning ignored.\n<tool_call>{json.dumps(call)}</tool_call>"}}]}


def client(handler, *, prompt: str = "Pinned Fara prompt", api_key: str = "test-secret") -> ModelClient:
    transport = httpx.MockTransport(handler)
    return ModelClient(ModelSettings("http://127.0.0.1:8080/v1", api_key=api_key, system_prompt=prompt), transport=transport)


@pytest.mark.asyncio
async def test_request_sends_image_sensitive_message_and_bearer_auth() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=xml_call({"action": "left_click", "coordinate": [12, 34]}))

    result = await client(handler).next_action("Open the catalog", PNG, [{"step": 1, "action": "wait"}])
    assert result == {"kind": "click", "x": 12.0, "y": 34.0, "button": "left", "count": 1}
    assert seen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert seen["auth"] == "Bearer test-secret"
    body = seen["body"]
    assert body["model"] == "fara-browser"
    assert body["temperature"] == 0 and body["max_tokens"] == 2048 and body["stream"] is False
    messages = body["messages"]
    assert messages[0] == {"role": "system", "content": "Pinned Fara prompt"}
    assert messages[1] == {"role": "user", "content": "Goal:\nOpen the catalog"}
    user_parts = messages[-1]["content"]
    assert user_parts[0]["text"] == "Goal and current page context:\nOpen the catalog"
    data_url = user_parts[1]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == PNG


@pytest.mark.asyncio
async def test_reconstructs_only_applied_fara_tool_turns_and_preserves_model_coordinates() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=xml_call({"action": "history_back"}))

    history = [
        {
            "model_action": {"kind": "click", "x": 20, "y": 35, "button": "left", "count": 1},
            "outcome": "applied",
            "url": "https://shop.example/",
            "debug_secret": "NEVER_SERIALIZE_THIS_FIELD",
        },
        {"model_action": {"kind": "type_text", "text": "typed value"}, "outcome": "applied", "url": "https://shop.example/"},
        {"model_action": {"kind": "click", "x": 2, "y": 3, "button": "left", "count": 1}, "outcome": "unknown"},
        {"reasoning": "NEVER_SERIALIZE_PRIVATE_REASONING"},
    ]
    model = client(handler, prompt="VERBATIM SYSTEM PROMPT")
    await model.next_action("Goal\nCurrent page URL: https://shop.example/", PNG, history)

    messages = seen["body"]["messages"]
    assert messages[0] == {"role": "system", "content": "VERBATIM SYSTEM PROMPT"}
    assert messages[1] == {"role": "user", "content": "Goal:\nGoal\nCurrent page URL: https://shop.example/"}
    assert messages[2] == {
        "role": "assistant",
        "content": '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[20.0,35.0]}}</tool_call>',
    }
    assert messages[3] == {"role": "user", "content": "Action applied."}
    assert messages[4] == {
        "role": "assistant",
        "content": '<tool_call>{"name":"computer_use","arguments":{"action":"type","text":"typed value"}}</tool_call>',
    }
    assert messages[5] == {"role": "user", "content": "Action applied."}
    latest = messages[-1]["content"]
    assert latest[0]["text"] == "Goal and current page context:\nGoal\nCurrent page URL: https://shop.example/"
    assert "NEVER_SERIALIZE_THIS_FIELD" not in json.dumps(messages)
    assert "NEVER_SERIALIZE_PRIVATE_REASONING" not in json.dumps(messages)
    assert all("unknown" not in message.get("content", "") for message in messages if isinstance(message.get("content"), str))


@pytest.mark.parametrize(("action", "expected"), [
    ({"kind": "click", "x": 4, "y": 5, "button": "left", "count": 2}, {"action": "double_click", "coordinate": [4.0, 5.0]}),
    ({"kind": "press_key", "key": "Control+a"}, {"action": "key", "keys": ["Control", "a"]}),
    ({"kind": "navigate", "url": "https://shop.example/"}, {"action": "visit_url", "url": "https://shop.example/"}),
    ({"kind": "scroll", "dx": 0, "dy": 120}, {"action": "scroll", "pixels": -120.0}),
    ({"kind": "scroll", "dx": 40, "dy": 0}, {"action": "hscroll", "pixels": 40.0}),
    ({"kind": "mouse_move", "x": 12, "y": 14}, {"action": "mouse_move", "coordinate": [12.0, 14.0]}),
    ({"kind": "drag", "x": 12, "y": 14}, {"action": "left_click_drag", "coordinate": [12.0, 14.0]}),
    ({"kind": "history_back"}, {"action": "history_back"}),
    ({"kind": "wait", "seconds": 2}, {"action": "wait", "time": 2.0}),
])
def test_history_action_maps_back_to_official_fara_arguments(action, expected) -> None:
    assert _history_fara_arguments(action) == expected


@pytest.mark.asyncio
async def test_normalizes_supported_fara_actions_and_aliases() -> None:
    actions = [
        ({"action": "right_click", "coordinate": [5, 8]}, {"kind": "click", "x": 5.0, "y": 8.0, "button": "right", "count": 1}),
        ({"action": "triple_click", "coordinate": [5, 8]}, {"kind": "click", "x": 5.0, "y": 8.0, "button": "left", "count": 3}),
        ({"action": "type", "text": "Привет"}, {"kind": "type_text", "text": "Привет"}),
        ({"action": "type", "coordinate": [0, 0], "text": "FARA LIVE DEMO"}, {"kind": "type_text", "text": "FARA LIVE DEMO"}),
        ({"action": "key", "coordinate": [0, 0], "keys": ["Tab"]}, {"kind": "press_key", "key": "Tab"}),
        ({"action": "key", "keys": ["Control", "a"]}, {"kind": "press_key", "key": "Control+a"}),
        # Fara positive pixels means up; Playwright positive delta_y means down.
        ({"action": "scroll", "pixels": -120}, {"kind": "scroll", "dx": 0.0, "dy": 120.0}),
        ({"action": "scroll", "pixels": 120}, {"kind": "scroll", "dx": 0.0, "dy": -120.0}),
        # Fara documents the vertical sign only; horizontal wheel deltas keep
        # their signed pixel value (positive right, per wheel delta_x convention).
        ({"action": "hscroll", "pixels": 30}, {"kind": "scroll", "dx": 30.0, "dy": 0.0}),
        ({"action": "hscroll", "pixels": -30}, {"kind": "scroll", "dx": -30.0, "dy": 0.0}),
        ({"action": "mouse_move", "coordinate": [23, 42]}, {"kind": "mouse_move", "x": 23.0, "y": 42.0}),
        ({"action": "left_click_drag", "coordinate": [23, 42]}, {"kind": "drag", "x": 23.0, "y": 42.0}),
        ({"action": "web_search", "query": "weather today"}, {"kind": "search", "query": "weather today"}),
        ({"action": "visit_url", "url": "https://shop.example/list"}, {"kind": "navigate", "url": "https://shop.example/list"}),
        ({"action": "history_back"}, {"kind": "history_back"}),
        ({"action": "wait", "time": 2}, {"kind": "wait", "seconds": 2.0}),
        ({"action": "terminate", "answer": "Done"}, {"kind": "finish", "summary": "Done"}),
        ({"action": "ask_user_question", "question": "Which size?"}, {"kind": "ask_user", "question": "Which size?"}),
        ({"action": "pause_and_memorize_fact", "fact": "Blue is preferred"}, {"kind": "remember_fact", "text": "Blue is preferred"}),
    ]
    for action, expected in actions:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=xml_call(action)))
        result = await ModelClient(ModelSettings("http://local/v1", system_prompt="prompt"), transport).next_action("Goal", PNG)
        assert result == expected


@pytest.mark.asyncio
async def test_health_checks_llama_server_and_prompt_is_required_for_action() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"status": "ok"})

    model = client(handler, prompt="")
    assert await model.health() == {"configured": True, "ready": True, "detail": "llama-server is ready"}
    assert seen == ["http://127.0.0.1:8080/health"]
    with pytest.raises(ModelError) as exc:
        await model.next_action("Goal", PNG)
    assert exc.value.code == "PROMPT_NOT_CONFIGURED"
    absent = ModelClient(ModelSettings(""), httpx.MockTransport(handler))
    assert (await absent.health())["configured"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    {"content": "No action"},
    {"content": "<tool_call>{bad}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"click_me\"}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"coordinate\":[1001,3]}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"coordinate\":[1000,3]}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"coordinate\":[NaN,3]}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"x\":3,\"y\":3}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"coordinate\":[3,3],\"extra\":true}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"coordinate\":[3,3]}}</tool_call><tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"coordinate\":[4,4]}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click_drag\"}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click_drag\",\"coordinate\":[1000,0]}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click_drag\",\"coordinate\":[NaN,0]}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click_drag\",\"coordinate\":[0,0],\"extra\":true}}</tool_call>"},
    {"content": "<tool_call>{\"name\":\"computer_use\",\"arguments\":{\"action\":\"left_click\",\"coordinate\":[3,3]}}</tool_call>", "tool_calls": [{"function": {"name": "computer_use", "arguments": "{\"action\":\"left_click\",\"coordinate\":[4,4]}"}}]},
])
async def test_rejects_malformed_ambiguous_or_unsupported_model_outputs(message) -> None:
    response = {"choices": [{"message": message}]}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    model = ModelClient(ModelSettings("http://local/v1", system_prompt="prompt"), transport)
    with pytest.raises(ModelError):
        await model.next_action("Goal", PNG)


@pytest.mark.asyncio
async def test_native_tool_call_is_supported_once_and_multiple_calls_rejected() -> None:
    native = {"choices": [{"message": {"content": None, "tool_calls": [{"type": "function", "function": {"name": "computer_use", "arguments": {"action": "double_click", "coordinate": [10, 20]}}}]}}]}
    model = ModelClient(ModelSettings("http://local/v1", system_prompt="prompt"), httpx.MockTransport(lambda _: httpx.Response(200, json=native)))
    assert await model.next_action("Goal", PNG) == {"kind": "click", "x": 10.0, "y": 20.0, "button": "left", "count": 2}
    native["choices"][0]["message"]["tool_calls"].append(native["choices"][0]["message"]["tool_calls"][0])
    model = ModelClient(ModelSettings("http://local/v1", system_prompt="prompt"), httpx.MockTransport(lambda _: httpx.Response(200, json=native)))
    with pytest.raises(ModelError) as exc:
        await model.next_action("Goal", PNG)
    assert exc.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.asyncio
async def test_response_size_is_bounded_before_parsing() -> None:
    oversized = b"{" + b" " * (1024 * 1024) + b"}"
    model = ModelClient(
        ModelSettings("http://local/v1", system_prompt="prompt"),
        httpx.MockTransport(lambda _: httpx.Response(200, content=oversized)),
    )
    with pytest.raises(ModelError) as exc:
        await model.next_action("Goal", PNG)
    assert exc.value.code == "MODEL_RESPONSE_TOO_LARGE"
