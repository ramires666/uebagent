"""Async llama-server transport and strict Fara action parser.

This client only proposes normalized actions; it never executes them.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class ModelError(Exception):
    """Expected model transport/configuration/output failure."""

    def __init__(self, code: str, message: str, *, category: str | None = None) -> None:
        self.code = code
        self.message = message
        self.category = category
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ModelSettings:
    base_url: str
    alias: str = "fara-browser"
    api_key: str = ""
    timeout_s: float = 90
    system_prompt: str = ""


_MAX_RESPONSE = 1024 * 1024
_MAX_SCREENSHOT = 8 * 1024 * 1024
_MAX_HISTORY_BYTES = 48 * 1024
_MAX_HISTORY_ITEMS = 12
_MAX_CONVERSATION_ASSISTANT_TEXT = 24 * 1024
_MAX_CONVERSATION_IMAGES = 3
_MAX_CONVERSATION_MESSAGES = 24
_TOOL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_ACTION_NAMES = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}
_KEY_ALIASES = {
    "CTRL": "Control", "CONTROL": "Control",
    "ESC": "Escape", "RETURN": "Enter",
    "DEL": "Delete", "BACKSPACE": "Backspace",
    "LEFT": "ArrowLeft", "RIGHT": "ArrowRight",
    "UP": "ArrowUp", "DOWN": "ArrowDown",
    "SPACE": "Space", "SPACEBAR": "Space",
}
_KEY_MODIFIERS = {"Control", "Alt", "Shift"}
_KEY_NAMES = {
    "Escape", "Tab", "Backspace", "Delete", "Home", "End", "PageUp", "PageDown",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", "Space",
    "a", "c", "v", "x", "z", "A", "C", "V", "X", "Z",
}


def _normalize_key_sequence(value: Any) -> str:
    """Validate and canonicalize one key plus optional modifier chord."""
    if isinstance(value, str):
        parts = value.split("+")
    elif isinstance(value, list):
        if not 1 <= len(value) <= 8 or any(not isinstance(item, str) for item in value):
            raise ModelError("MODEL_OUTPUT_INVALID", "keys must contain 1 to 8 string key names")
        # Accept a modifier chord expressed in one list item as well as the
        # official ordered list form (for example ["Control", "a"]).
        parts = [part for item in value for part in item.split("+")]
    else:
        raise ModelError("MODEL_OUTPUT_INVALID", "keys must be a key name or an array of key names")

    if not 1 <= len(parts) <= 8:
        raise ModelError("MODEL_OUTPUT_INVALID", "key chord must contain 1 to 8 key names")
    normalized: list[str] = []
    for part in parts:
        if not part or len(part) > 100 or any(ch in part for ch in "\r\n\t") or part != part.strip():
            raise ModelError("MODEL_OUTPUT_INVALID", "key names must be non-empty single-line values")
        alias = _KEY_ALIASES.get(part.upper())
        if alias:
            name = alias
        elif part in {"a", "c", "v", "x", "z", "A", "C", "V", "X", "Z"}:
            name = part
        else:
            name = next((known for known in _KEY_NAMES | _KEY_MODIFIERS if known.lower() == part.lower()), "")
        if not name:
            raise ModelError("MODEL_OUTPUT_INVALID", "unknown or unsupported key name")
        normalized.append(name)

    modifiers = [key for key in normalized if key in _KEY_MODIFIERS]
    physical = [key for key in normalized if key not in _KEY_MODIFIERS]
    if len(set(modifiers)) != len(modifiers) or len(physical) > 1:
        raise ModelError("MODEL_OUTPUT_INVALID", "key chord must contain unique modifiers and at most one physical key",
                         category="invalid_key_chord")
    # The worker accepts modifier prefixes followed by at most one key. Put
    # modifiers first even if the model returned the same chord in another order.
    return "+".join(modifiers + physical)


def _strict_json(raw: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _numeric(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ModelError("MODEL_OUTPUT_INVALID", f"{field} must be a finite number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ModelError("MODEL_OUTPUT_INVALID", f"{field} is outside the supported range")
    return number


def _coordinate(args: dict[str, Any]) -> tuple[float, float]:
    coordinate = args.get("coordinate")
    if not isinstance(coordinate, list) or len(coordinate) != 2:
        raise ModelError("MODEL_OUTPUT_INVALID", "coordinate must be a two-number [x, y] array")
    x = _numeric(coordinate[0], "coordinate[0]", 0, 1000)
    y = _numeric(coordinate[1], "coordinate[1]", 0, 1000)
    if x >= 1000 or y >= 1000:
        raise ModelError("MODEL_OUTPUT_INVALID", "coordinate must fall inside the 1000 by 1000 Fara display space")
    return x, y


def _only(args: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    # The official computer_use schema exposes coordinate as an optional
    # property for every action. Fara can emit a placeholder on keyboard
    # actions. Validate it, then ignore it; it must never cause a mouse input.
    if "coordinate" in args and "coordinate" not in allowed:
        _coordinate(args)
        allowed = allowed | {"coordinate"}
    unknown = set(args) - allowed
    missing = required - set(args)
    if unknown or missing:
        raise ModelError("MODEL_OUTPUT_INVALID", "action arguments do not match the Fara schema")


def _normalize_tool_call(call: Any) -> dict[str, Any]:
    if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
        raise ModelError("MODEL_OUTPUT_INVALID", "tool call must contain only name and arguments")
    if call["name"] != "computer_use" or not isinstance(call["arguments"], dict):
        raise ModelError("MODEL_OUTPUT_INVALID", "expected a computer_use tool call")
    args = call["arguments"]
    action = args.get("action")
    if not isinstance(action, str):
        raise ModelError("MODEL_OUTPUT_INVALID", "action must be a string")

    if action in _ACTION_NAMES:
        _only(args, {"action", "coordinate"}, {"action", "coordinate"})
        x, y = _coordinate(args)
        button, count = _ACTION_NAMES[action]
        return {"kind": "click", "x": x, "y": y, "button": button, "count": count}
    if action == "type":
        _only(args, {"action", "text"}, {"action", "text"})
        if not isinstance(args["text"], str) or len(args["text"]) > 100_000:
            raise ModelError("MODEL_OUTPUT_INVALID", "text must be a string of at most 100000 characters")
        return {"kind": "type_text", "text": args["text"]}
    if action == "key":
        _only(args, {"action", "keys"}, {"action", "keys"})
        return {"kind": "press_key", "key": _normalize_key_sequence(args["keys"])}
    if action in {"scroll", "hscroll"}:
        _only(args, {"action", "pixels"}, {"action", "pixels"})
        pixels = _numeric(args["pixels"], "pixels", -10_000, 10_000)
        # Fara's schema defines positive pixels as upward scrolling. Browser
        # wheel APIs use positive dy for downward scrolling, so invert y.
        # The source does not distinguish hscroll's sign convention; retain
        # the signed pixel amount as a horizontal wheel delta.
        return {"kind": "scroll", "dx": pixels if action == "hscroll" else 0.0,
                "dy": -pixels if action == "scroll" else 0.0}
    if action in {"mouse_move", "left_click_drag"}:
        _only(args, {"action", "coordinate"}, {"action", "coordinate"})
        x, y = _coordinate(args)
        return {"kind": "drag" if action == "left_click_drag" else "mouse_move", "x": x, "y": y}
    if action == "web_search":
        _only(args, {"action", "query"}, {"action", "query"})
        query = args["query"]
        if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
            raise ModelError("MODEL_OUTPUT_INVALID", "query must be a non-empty string of at most 10000 characters")
        return {"kind": "search", "query": query}
    if action == "visit_url":
        _only(args, {"action", "url"}, {"action", "url"})
        url = args["url"]
        if not isinstance(url, str) or len(url) > 4096:
            raise ModelError("MODEL_OUTPUT_INVALID", "url must be a string of at most 4096 characters")
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise ValueError
            _ = parsed.port
        except ValueError as exc:
            raise ModelError("MODEL_OUTPUT_INVALID", "url must be an absolute HTTP or HTTPS URL without credentials") from exc
        return {"kind": "navigate", "url": url}
    if action == "history_back":
        _only(args, {"action"}, {"action"})
        return {"kind": "history_back"}
    if action == "wait":
        _only(args, {"action", "time"}, {"action", "time"})
        seconds = _numeric(args["time"], "time", 0, 10)
        return {"kind": "wait", "seconds": seconds}
    if action == "terminate":
        _only(args, {"action", "answer"}, {"action", "answer"})
        summary = args["answer"]
        if not isinstance(summary, str) or len(summary) > 10_000:
            raise ModelError("MODEL_OUTPUT_INVALID", "summary must be a string of at most 10000 characters")
        return {"kind": "finish", "summary": summary}
    if action == "ask_user_question":
        _only(args, {"action", "question"}, {"action", "question"})
        question = args["question"]
        if not isinstance(question, str) or not question or len(question) > 10_000:
            raise ModelError("MODEL_OUTPUT_INVALID", "question must be a non-empty string of at most 10000 characters")
        return {"kind": "ask_user", "question": question}
    if action == "pause_and_memorize_fact":
        _only(args, {"action", "fact"}, {"action", "fact"})
        text = args["fact"]
        if not isinstance(text, str) or not text or len(text) > 10_000:
            raise ModelError("MODEL_OUTPUT_INVALID", "text must be a non-empty string of at most 10000 characters")
        return {"kind": "remember_fact", "text": text}
    if action == "read_page_answer_question":
        raise ModelError("ACTION_UNSUPPORTED", "this runtime does not support that Fara action")
    raise ModelError("MODEL_OUTPUT_INVALID", "unknown or unsupported Fara action")


def _parse_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ModelError("MODEL_OUTPUT_INVALID", "assistant message is malformed")
    content = message.get("content")
    calls = message.get("tool_calls")
    content_call: dict[str, Any] | None = None
    if content is not None:
        if not isinstance(content, str):
            raise ModelError("MODEL_OUTPUT_INVALID", "assistant content must be a string")
        matches = list(_TOOL_RE.finditer(content))
        if "<tool_call" in content or "</tool_call>" in content:
            if len(matches) != 1 or content.count("<tool_call>") != 1 or content.count("</tool_call>") != 1:
                raise ModelError("MODEL_OUTPUT_INVALID", "content must contain exactly one complete tool_call block")
            try:
                content_call = _strict_json(matches[0].group(1).strip())
            except (ValueError, json.JSONDecodeError) as exc:
                raise ModelError("MODEL_OUTPUT_INVALID", "tool_call contains invalid JSON") from exc
    native_call: dict[str, Any] | None = None
    if calls is not None:
        if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
            raise ModelError("MODEL_OUTPUT_INVALID", "exactly one native tool call is required")
        native = calls[0]
        function = native.get("function")
        if not isinstance(function, dict) or function.get("name") != "computer_use":
            raise ModelError("MODEL_OUTPUT_INVALID", "expected one native computer_use tool call")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = _strict_json(arguments)
            except (ValueError, json.JSONDecodeError) as exc:
                raise ModelError("MODEL_OUTPUT_INVALID", "native tool call contains invalid JSON") from exc
        native_call = {"name": function["name"], "arguments": arguments}
    if content_call is None and native_call is None:
        raise ModelError("MODEL_OUTPUT_INVALID", "model response did not contain a Fara tool call")
    if content_call is not None and native_call is not None and content_call != native_call:
        raise ModelError("MODEL_OUTPUT_INVALID", "content and native tool call disagree")
    return _normalize_tool_call(content_call if content_call is not None else native_call)


def _history_number(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    number = float(value)
    return number if low <= number <= high else None


def _history_fara_arguments(action: Any) -> dict[str, Any] | None:
    """Map a recorded normalized action back to Fara's trained action schema."""
    if not isinstance(action, dict):
        return None
    kind = action.get("kind")
    if kind == "click":
        x = _history_number(action.get("x"), 0, 999.999999)
        y = _history_number(action.get("y"), 0, 999.999999)
        button, count = action.get("button"), action.get("count")
        if x is None or y is None or button not in {"left", "right"} or isinstance(count, bool) or not isinstance(count, int):
            return None
        names = {("left", 1): "left_click", ("right", 1): "right_click", ("left", 2): "double_click", ("left", 3): "triple_click"}
        name = names.get((button, count))
        return {"action": name, "coordinate": [x, y]} if name else None
    if kind == "type_text":
        text = action.get("text")
        return {"action": "type", "text": text} if isinstance(text, str) and len(text) <= 100_000 else None
    if kind == "press_key":
        key = action.get("key")
        try:
            normalized = _normalize_key_sequence(key)
        except ModelError:
            return None
        return {"action": "key", "keys": normalized.split("+")}
    if kind == "navigate":
        url = action.get("url")
        return {"action": "visit_url", "url": url} if isinstance(url, str) and len(url) <= 4096 else None
    if kind == "search":
        query = action.get("query")
        return {"action": "web_search", "query": query} if isinstance(query, str) and query.strip() and len(query) <= 10_000 else None
    if kind == "scroll":
        dx = _history_number(action.get("dx"), -10_000, 10_000)
        dy = _history_number(action.get("dy"), -10_000, 10_000)
        if dx is None or dy is None or (dx != 0 and dy != 0) or (dx == 0 and dy == 0):
            return None
        # Fara positive vertical pixels means up; normalized browser wheel dy
        # has the opposite sign. Horizontal deltas retain their signed value.
        if dy:
            return {"action": "scroll", "pixels": -dy}
        return {"action": "hscroll", "pixels": dx}
    if kind in {"mouse_move", "drag"}:
        x = _history_number(action.get("x"), 0, 999.999999)
        y = _history_number(action.get("y"), 0, 999.999999)
        return {"action": "left_click_drag" if kind == "drag" else "mouse_move", "coordinate": [x, y]} if x is not None and y is not None else None
    if kind == "history_back":
        return {"action": "history_back"}
    if kind == "wait":
        seconds = _history_number(action.get("seconds"), 0, 10)
        return {"action": "wait", "time": seconds} if seconds is not None else None
    if kind == "remember_fact":
        text = action.get("text")
        return {"action": "pause_and_memorize_fact", "fact": text} if isinstance(text, str) and text and len(text) <= 10_000 else None
    return None


def _history_messages(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in history[-_MAX_HISTORY_ITEMS:]:
        # Only applied, typed normalized actions can be replayed as assistant
        # calls. Unconfirmed/planned actions are never upgraded into outcomes.
        if item.get("outcome") == "applied":
            arguments = _history_fara_arguments(item.get("model_action"))
            if arguments is not None:
                call = {"name": "computer_use", "arguments": arguments}
                encoded = json.dumps(call, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                messages.append({"role": "assistant", "content": f"<tool_call>{encoded}</tool_call>"})
                messages.append({"role": "user", "content": "Action applied."})
                continue
        # Explicit remembered facts are user-visible task context, not model
        # reasoning or inferred tool outcomes. Other history fields are omitted.
        fact = item.get("fact")
        if isinstance(fact, str) and fact and len(fact) <= 10_000:
            messages.append({"role": "user", "content": f"Previously remembered fact: {fact}"})
    return messages


class ModelClient:
    def __init__(self, settings: ModelSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        # This private, process-local transcript is never logged or persisted.
        self._conversation_task_id: str | None = None
        self._conversation: list[dict[str, Any]] = []
        self._conversation_pending_outcome = False
        self._conversation_feedback: str | None = None

    def begin_task(self, task_id: str) -> None:
        """Start or resume one task's private Fara conversation."""
        if self._conversation_task_id != task_id:
            self._conversation_task_id = task_id
            self._conversation = []
            self._conversation_pending_outcome = False
            self._conversation_feedback = None

    def discard_task(self, task_id: str) -> None:
        if self._conversation_task_id == task_id:
            self.clear_conversation()

    def clear_conversation(self) -> None:
        self._conversation_task_id = None
        self._conversation.clear()
        self._conversation_pending_outcome = False
        self._conversation_feedback = None

    def record_outcome(self, outcome: str, code: str | None = None) -> None:
        """Record execution status for the next screenshot turn, without action text."""
        if not self._conversation_task_id or not self._conversation_pending_outcome:
            return
        self._conversation_pending_outcome = False
        if outcome == "unknown":
            self._conversation_feedback = "The previous action outcome is unknown. Inspect the current page before deciding what to do next."
        elif outcome != "applied":
            safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code) else "ACTION_FAILED"
            self._conversation_feedback = f"The previous action was rejected and not executed: {safe_code}."
        else:
            self._conversation_feedback = None

    def record_verification_failure(self, failed: list[dict[str, Any]]) -> None:
        """Keep verification feedback private and separate from action outcomes."""
        encoded = json.dumps(failed, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 8192:
            raise ModelError("MODEL_INPUT_TOO_LARGE", "verification feedback exceeds 8 KiB")
        self._conversation_pending_outcome = False
        self._conversation_feedback = ("Independent verification found these unmet success conditions: "
                                       + encoded + ". Inspect the current page and correct them.")

    def record_autonomy_notice(self) -> None:
        """Give private bounded feedback without inventing absent required data."""
        self._conversation_pending_outcome = False
        self._conversation_feedback = ('The user was notified. Continue the authorized task using available information and permitted actions. '
                                       'Do not invent required information, credentials or bypass authentication or challenges. '
                                       'If essential information is unavailable, say what is needed.')

    def record_action_rejection(self, reason: str) -> None:
        if reason == 'verbatim_text_mismatch':
            self._conversation_pending_outcome = False
            self._conversation_feedback = 'Use the exact supplied text; rephrasing is not allowed.'
            return
        if reason != 'editable_not_focused':
            raise ModelError('MODEL_INPUT_INVALID', 'unsupported action rejection reason')
        self._conversation_pending_outcome = False
        self._conversation_feedback = ('Typing was not performed: no focused editable field. '
                                       'Inspect the screenshot, focus the intended field, then retry.')

    def record_model_output_rejection(self, category: str) -> None:
        if category != "invalid_key_chord":
            raise ModelError("MODEL_INPUT_INVALID", "unsupported model output rejection category")
        self._conversation_pending_outcome = False
        self._conversation_feedback = (
            "The previous keyboard action had an invalid key chord. Use unique modifiers and at most one physical key. "
            "Do not repeat a multi-key sequence."
        )

    def record_verbatim_text_action(self) -> None:
        """Replace the latest managed type action with a payload-free placeholder."""
        if not self._conversation or self._conversation[-1].get("role") != "assistant":
            return
        arguments = {"action": "type", "text": "__VERBATIM_TEXT__"}
        encoded = json.dumps({"name": "computer_use", "arguments": arguments},
                             ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self._conversation[-1]["content"] = f"<tool_call>{encoded}</tool_call>"

    @staticmethod
    def _conversation_api_messages(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": item["role"], "content": item["content"]} for item in items]

    @staticmethod
    def _conversation_text_size(items: list[dict[str, Any]]) -> int:
        total = 0
        for item in items:
            content = item["content"]
            if isinstance(content, str):
                total += len(content.encode("utf-8"))
            elif isinstance(content, list):
                total += sum(len(part.get("text", "").encode("utf-8")) for part in content
                             if isinstance(part, dict) and isinstance(part.get("text"), str))
        return total

    @staticmethod
    def _assistant_text_size(items: list[dict[str, Any]]) -> int:
        return sum(len(item["content"].encode("utf-8")) for item in items
                   if item.get("role") == "assistant" and isinstance(item.get("content"), str))

    @staticmethod
    def _trim_conversation(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Keep the original task message and the newest three screenshot turns.
        image_count = 0
        for item in reversed(items):
            content = item["content"]
            if not isinstance(content, list):
                continue
            image_parts = [part for part in content if isinstance(part, dict) and part.get("type") == "image_url"]
            if image_parts:
                image_count += len(image_parts)
                if image_count > _MAX_CONVERSATION_IMAGES:
                    item["content"] = [part for part in content if not (isinstance(part, dict) and part.get("type") == "image_url")]

        # Remove complete oldest user/assistant turns; never slice a tool call.
        while (ModelClient._assistant_text_size(items) > _MAX_CONVERSATION_ASSISTANT_TEXT
               or len(items) > _MAX_CONVERSATION_MESSAGES):
            # Index 0 is the immutable original task message. The assistant's
            # first reply can be dropped as a whole once a later turn exists.
            assistant_idx = next((i for i, item in enumerate(items[1:], 1) if item.get("role") == "assistant"), None)
            if assistant_idx is None:
                break
            if assistant_idx == 1 and len(items) > 2:
                del items[assistant_idx]
                continue
            user_idx = assistant_idx - 1 if items[assistant_idx - 1].get("role") == "user" else None
            if user_idx is None or assistant_idx == len(items) - 1:
                # The newest assistant call is kept whole, even if it alone exceeds
                # a message-count limit; truncating it could corrupt its tool_call.
                break
            del items[user_idx:assistant_idx + 1]
        return items

    @staticmethod
    def _raw_assistant_content(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        # Some OpenAI-compatible servers return native calls without textual
        # content. Reconstruct the canonical Fara form for future turns.
        calls = message.get("tool_calls")
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict):
            function = calls[0].get("function")
            if isinstance(function, dict):
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = _strict_json(arguments)
                    except (ValueError, json.JSONDecodeError):
                        arguments = None
                if isinstance(arguments, dict):
                    call = {"name": function.get("name"), "arguments": arguments}
                    encoded = json.dumps(call, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    return f"<tool_call>{encoded}</tool_call>"
        raise ModelError("MODEL_OUTPUT_INVALID", "assistant response has no reusable text or tool call")

    @property
    def _configured(self) -> bool:
        if not self.settings.base_url or not self.settings.alias:
            return False
        try:
            parsed = urlsplit(self.settings.base_url)
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and parsed.path.rstrip("/").endswith("/v1") and not parsed.query and not parsed.fragment
        except ValueError:
            return False

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.settings.timeout_s), transport=self._transport, trust_env=False)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key}"} if self.settings.api_key else {}

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > _MAX_RESPONSE:
                raise ModelError("MODEL_RESPONSE_TOO_LARGE", "model response exceeded 1 MiB")
        return bytes(data)

    async def health(self) -> dict[str, Any]:
        if not self._configured:
            return {"configured": False, "ready": False, "detail": "model base_url must be an HTTP(S) /v1 endpoint and alias must be set"}
        health_url = self.settings.base_url.rstrip("/")[:-3] + "/health"
        try:
            async with self._client() as client:
                async with client.stream("GET", health_url, headers=self._auth(), timeout=5.0) as response:
                    body = await self._read_bounded(response)
                    ready = response.is_success
                    detail = "llama-server is ready" if ready else f"llama-server health returned HTTP {response.status_code}"
                    if not ready and body:
                        detail = f"llama-server health returned HTTP {response.status_code}"
                    return {"configured": True, "ready": ready, "detail": detail}
        except ModelError as exc:
            return {"configured": True, "ready": False, "detail": exc.code}
        except httpx.HTTPError:
            return {"configured": True, "ready": False, "detail": "llama-server is unreachable"}

    async def next_action(self, goal: str, screenshot_png: bytes, history: list[dict[str, Any]] | None = None,
                         *, current_url: str | None = None) -> dict[str, Any]:
        if not self._configured:
            raise ModelError("MODEL_NOT_CONFIGURED", "model base_url and alias are required")
        if not self.settings.system_prompt.strip():
            raise ModelError("PROMPT_NOT_CONFIGURED", "the pinned Fara system prompt is required")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 20_000:
            raise ModelError("MODEL_INPUT_INVALID", "goal must be a non-empty string of at most 20000 characters")
        if not isinstance(screenshot_png, bytes) or not screenshot_png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ModelError("MODEL_INPUT_INVALID", "screenshot_png must contain PNG bytes")
        if len(screenshot_png) > _MAX_SCREENSHOT:
            raise ModelError("MODEL_INPUT_TOO_LARGE", "screenshot PNG exceeds 8 MiB")
        bounded_history = history or []
        if not isinstance(bounded_history, list) or any(not isinstance(item, dict) for item in bounded_history):
            raise ModelError("MODEL_INPUT_INVALID", "history must be a list of objects")
        try:
            history_json = json.dumps(bounded_history[-_MAX_HISTORY_ITEMS:], ensure_ascii=False,
                                      separators=(",", ":"), allow_nan=False)
            history_size = len(history_json.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ModelError("MODEL_INPUT_INVALID", "history must contain JSON-compatible values") from exc
        if history_size > _MAX_HISTORY_BYTES:
            raise ModelError("MODEL_INPUT_TOO_LARGE", "history exceeds 48 KiB")
        image_url = "data:image/png;base64," + base64.b64encode(screenshot_png).decode("ascii")
        managed = self._conversation_task_id is not None
        candidate_conversation: list[dict[str, Any]] | None = None
        if managed:
            candidate_conversation = copy.deepcopy(self._conversation)
            if not candidate_conversation:
                feedback = f"\n{self._conversation_feedback}" if self._conversation_feedback else ""
                current = {"role": "user", "image_bytes": len(screenshot_png), "content": [
                    {"type": "text", "text": goal + feedback},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}
            else:
                url_text = f"Current URL: {current_url[:100]}\n" if isinstance(current_url, str) and current_url else ""
                feedback = f"{self._conversation_feedback}\n" if self._conversation_feedback else ""
                current = {"role": "user", "image_bytes": len(screenshot_png), "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": f"{url_text}{feedback}Here is the next screenshot. Think about what to do next."},
                ]}
            candidate_conversation.append(current)
            candidate_conversation = self._trim_conversation(candidate_conversation)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self.settings.system_prompt},
                *self._conversation_api_messages(candidate_conversation),
            ]
        else:
            # Backwards-compatible stateless mode for direct callers.
            messages = [
                {"role": "system", "content": self.settings.system_prompt},
                {"role": "user", "content": f"Goal:\n{goal}"},
            ]
            messages.extend(_history_messages(bounded_history))
            messages.append({"role": "user", "content": [
                {"type": "text", "text": f"Goal and current page context:\n{goal}"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]})
        payload = {
            "model": self.settings.alias,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 2048,
            "stream": False,
        }
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        try:
            async with self._client() as client:
                async with client.stream("POST", url, headers=self._auth(), json=payload) as response:
                    raw = await self._read_bounded(response)
                    if not response.is_success:
                        raise ModelError("MODEL_UNAVAILABLE", f"model endpoint returned HTTP {response.status_code}")
        except ModelError:
            raise
        except httpx.HTTPError as exc:
            raise ModelError("MODEL_UNAVAILABLE", "model endpoint request failed") from exc
        try:
            body = _strict_json(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelError("MODEL_OUTPUT_INVALID", "model response is not valid JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or len(body["choices"]) != 1:
            raise ModelError("MODEL_OUTPUT_INVALID", "model response must contain exactly one choice")
        choice = body["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ModelError("MODEL_OUTPUT_INVALID", "model response choice is malformed")
        message = choice["message"]
        action = _parse_message(message)
        if managed:
            assert candidate_conversation is not None
            raw_assistant = self._raw_assistant_content(message)
            if len(raw_assistant.encode("utf-8")) > _MAX_CONVERSATION_ASSISTANT_TEXT:
                raise ModelError("MODEL_CONTEXT_LIMIT", "assistant turn exceeds the managed conversation text budget")
            candidate_conversation.append({"role": "assistant", "content": raw_assistant})
            self._conversation = self._trim_conversation(candidate_conversation)
            self._conversation_feedback = None
            self._conversation_pending_outcome = True
        return action
