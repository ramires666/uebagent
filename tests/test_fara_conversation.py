from __future__ import annotations

import json

import httpx
import pytest

from browser_mcp.model_client import ModelClient, ModelError, ModelSettings


PNG = b"\x89PNG\r\n\x1a\nfixture"


def response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.mark.asyncio
async def test_managed_turns_preserve_raw_assistant_content_goal_once_and_latest_three_images() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        step = len(seen)
        call = {"name": "computer_use", "arguments": {"action": "left_click", "coordinate": [step, step]}}
        raw = f"private thought {step}\n<tool_call>{json.dumps(call)}</tool_call>"
        return httpx.Response(200, json=response(raw))

    model = ModelClient(ModelSettings("http://local/v1", system_prompt="PINNED PROMPT"), httpx.MockTransport(handler))
    model.begin_task("one")
    goal = "Fill the form\nTask allowed origins: [shop.example]\nUser data: {name: Ada}"
    await model.next_action(goal, PNG, current_url="https://shop.example/start")
    model.record_outcome("applied")
    for index in range(3):
        await model.next_action(goal, PNG, current_url=f"https://shop.example/step-{index}")
        model.record_outcome("applied")

    last = seen[-1]["messages"]
    assert last[0] == {"role": "system", "content": "PINNED PROMPT"}
    assert sum("Fill the form" in part.get("text", "")
               for message in last if isinstance(message.get("content"), list)
               for part in message["content"]) == 1
    assistant_turns = [message["content"] for message in last if message["role"] == "assistant"]
    assert len(assistant_turns) == 3
    assert all(f"private thought {step}" in assistant_turns[index] for index, step in enumerate((1, 2, 3)))
    assert all("Action applied." not in message.get("content", "")
               for message in last if isinstance(message.get("content"), str))
    assert sum(1 for message in last if isinstance(message.get("content"), list)
               for part in message["content"] if part.get("type") == "image_url") == 3
    assert any(part.get("text", "").startswith("Current URL: https://shop.example/step-2\n")
               for part in last[-1]["content"])


@pytest.mark.asyncio
async def test_managed_conversation_task_isolation_failure_feedback_and_cleanup() -> None:
    seen: list[dict] = []
    call = {"name": "computer_use", "arguments": {"action": "type", "text": "private typed value"}}
    raw = f"reasoning retained\n<tool_call>{json.dumps(call)}</tool_call>"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response(raw))

    model = ModelClient(ModelSettings("http://local/v1", system_prompt="PINNED"), httpx.MockTransport(handler))
    model.begin_task("first")
    await model.next_action("first task goal", PNG, current_url="https://one.example")
    model.record_outcome("failed", "WORKER_PREFLIGHT_REJECTED")
    await model.next_action("first task goal", PNG, current_url="https://one.example/form")
    messages = seen[-1]["messages"]
    assert any("previous action was rejected and not executed: WORKER_PREFLIGHT_REJECTED" in part.get("text", "")
               for part in messages[-1]["content"])
    assert any("reasoning retained" in message.get("content", "")
               for message in messages if message["role"] == "assistant")
    assert "private typed value" in json.dumps(messages)

    model.begin_task("second")
    await model.next_action("second task goal", PNG, current_url="https://two.example")
    second_messages = seen[-1]["messages"]
    assert all("first task goal" not in json.dumps(message) for message in second_messages)
    assert sum(1 for message in second_messages if message["role"] == "assistant") == 0
    model.discard_task("second")
    assert model._conversation == []
    assert model._conversation_task_id is None


@pytest.mark.asyncio
async def test_managed_assistant_turn_hard_limit_rejects_without_corrupting_tool_call() -> None:
    call = {"name": "computer_use", "arguments": {"action": "wait", "time": 1}}
    raw = "x" * (24 * 1024) + f"\n<tool_call>{json.dumps(call)}</tool_call>"
    model = ModelClient(
        ModelSettings("http://local/v1", system_prompt="PINNED"),
        httpx.MockTransport(lambda _: httpx.Response(200, json=response(raw))),
    )
    model.begin_task("bounded")
    with pytest.raises(ModelError) as exc:
        await model.next_action("bounded goal", PNG)
    assert exc.value.code == "MODEL_CONTEXT_LIMIT"
    assert model._conversation == []


@pytest.mark.asyncio
async def test_assistant_context_budget_drops_whole_old_turns_but_keeps_goal_and_latest_call() -> None:
    seen: list[dict] = []
    call = {"name": "computer_use", "arguments": {"action": "wait", "time": 1}}
    counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal counter
        counter += 1
        seen.append(json.loads(request.content))
        raw = f"thought-{counter}:" + ("x" * 19_000) + f"\n<tool_call>{json.dumps(call)}</tool_call>"
        return httpx.Response(200, json=response(raw))

    model = ModelClient(ModelSettings("http://local/v1", system_prompt="PINNED"), httpx.MockTransport(handler))
    model.begin_task("large-turns")
    await model.next_action("THE ORIGINAL GOAL", PNG)
    model.record_outcome("applied")
    await model.next_action("THE ORIGINAL GOAL", PNG)
    assert model._assistant_text_size(model._conversation) <= 24 * 1024
    assert len(model._conversation) <= 24
    assert model._conversation[0]["content"][0]["text"] == "THE ORIGINAL GOAL"
    assert model._conversation[-1]["content"].startswith("thought-2:")


@pytest.mark.asyncio
async def test_managed_message_count_stays_bounded() -> None:
    seen: dict = {}
    call = {"name": "computer_use", "arguments": {"action": "wait", "time": 1}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json=response(f"<tool_call>{json.dumps(call)}</tool_call>"))

    model = ModelClient(ModelSettings("http://local/v1", system_prompt="PINNED"), httpx.MockTransport(handler))
    model.begin_task("many")
    for _ in range(30):
        await model.next_action("repeat", PNG)
        model.record_outcome("applied")
    assert len(model._conversation) <= 24
    assert len(seen["messages"]) <= 25
    assert model._conversation[0]["content"][0]["text"] == "repeat"
