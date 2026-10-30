"""Opt-in, in-memory, privacy-filtered action diagnostics for readiness runs."""
from __future__ import annotations

import os
from collections import deque
from typing import Any


class ReadinessActionTrace:
    LIMIT = 200

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("APP_ENV", "production").lower() == "test" and os.environ.get("READINESS_ACTION_TRACE") == "1"

    @staticmethod
    def capture_enabled() -> bool:
        """Whether action-level DOM snapshots are enabled for this test run."""
        return ReadinessActionTrace.enabled() and os.environ.get("READINESS_SKIP_ACTION_SNAPSHOTS") != "1"

    def __init__(self) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=self.LIMIT)

    def append(self, record: dict[str, Any]) -> None:
        # Construct a strict allowlist rather than attempting to scrub arbitrary payloads.
        clean: dict[str, Any] = {}
        for key in ("kind", "elapsed_ms", "x", "y", "viewport", "focus_before", "focus_after", "target_before", "target_after", "hit", "target", "bbox", "target_boxes", "checked_before", "checked_after", "filled_before", "filled_after", "outcome"):
            if key not in record:
                continue
            value = record[key]
            if key == "kind" and value in {"click", "type_text", "press_key", "scroll", "wait", "navigate", "drag", "mouse_move"}:
                clean[key] = value
            elif key == "elapsed_ms" and type(value) in (int, float):
                clean[key] = max(0, min(600000, int(value)))
            elif key in {"x", "y"} and type(value) in (int, float):
                clean[key] = round(max(-10000, min(10000, float(value))), 2)
            elif key == "viewport" and isinstance(value, dict) and set(value) == {"width", "height"}:
                if all(type(value[k]) is int and 0 <= value[k] <= 10000 for k in value): clean[key] = dict(value)
            elif key in {"focus_before", "focus_after"} and value in {"body", "input", "textarea", "button", "select", "checkbox", "other", "none", "unknown"}:
                clean[key] = value
            elif key in {"target_before", "target_after", "target"} and value in {"home_expander", "home_checkbox", "home_label", "desktop_expander", "desktop_checkbox", "desktop_label", "root_expander", "first_name", "last_name", "email", "age", "salary", "department", "other", "none", "unknown"}:
                clean[key] = value
            elif key == "hit" and value in {"checkbox", "button", "input", "textarea", "select", "link", "other", "none", "unknown"}:
                clean[key] = value
            elif key == "bbox" and isinstance(value, dict) and set(value) == {"x", "y", "width", "height"}:
                if all(type(value[k]) in (int, float) and -10000 <= value[k] <= 10000 for k in value):
                    clean[key] = {k: round(float(value[k]), 2) for k in ("x", "y", "width", "height")}
            elif key == "target_boxes" and isinstance(value, dict) and len(value) <= 12:
                safe = {}
                for name, box in value.items():
                    if (name in {"first_name", "last_name", "email", "age", "salary", "department", "home_checkbox", "desktop_checkbox", "home_expander", "desktop_expander", "root_expander"}
                            and isinstance(box, dict) and set(box) == {"x", "y", "width", "height"}
                            and all(type(box[k]) in (int, float) and -10000 <= box[k] <= 10000 for k in box)):
                        safe[name] = {k: round(float(box[k]), 2) for k in ("x", "y", "width", "height")}
                clean[key] = safe
            elif key in {"checked_before", "checked_after", "filled_before", "filled_after"} and isinstance(value, dict):
                if len(value) <= 12 and all(k in {"home", "desktop", "first_name", "last_name", "email", "age", "salary", "department"} and type(v) is bool for k, v in value.items()):
                    clean[key] = dict(value)
            elif key == "outcome" and value in {"applied", "rejected", "unknown", "error"}:
                clean[key] = value
        if clean:
            self._records.append(clean)

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._records]

    def reset(self) -> None:
        self._records.clear()
