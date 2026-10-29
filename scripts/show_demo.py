"""Run the bounded Fara demo against the existing local browser viewer."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

if os.name != "nt":
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://localhost:8090"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
MAX_FRAMES = 64
MAX_FRAME_BYTES = 32 * 1024 * 1024
POLL_INTERVAL_S = 0.6
POLL_TIMEOUT_S = 190
RUNNING_STATUSES = {"queued", "running", "pause_requested"}


class DemoRunLock:
    """A nonblocking, cross-process lock held for the whole demo run."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "DemoRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another demo run already holds the lock") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _ensure_ok(response: httpx.Response, message: str) -> None:
    if not response.is_success:
        raise RuntimeError(message)


async def _operator_headers(client: httpx.AsyncClient, operator_key_file: Path | None) -> dict[str, str]:
    response = await client.get("/api/v1/operator/session")
    if response.is_success:
        session = response.json()
    elif operator_key_file is not None:
        try:
            operator_key = operator_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("Could not read the operator key file") from exc
        if not operator_key:
            raise RuntimeError("The operator key file is empty")
        response = await client.post("/api/v1/operator/session", json={"token": operator_key})
        if not response.is_success:
            raise RuntimeError("Operator authentication failed")
        session = response.json()
    else:
        raise RuntimeError("Local operator session unavailable; provide --operator-key-file for token mode")
    csrf_token = session.get("csrf_token")
    if not csrf_token:
        raise RuntimeError("Operator session did not provide a CSRF token")
    return {"X-CSRF-Token": csrf_token, "Origin": str(client.base_url).rstrip("/")}


async def _json(client: httpx.AsyncClient, method: str, path: str, *, headers=None, json_body=None) -> dict[str, Any]:
    response = await client.request(method, path, headers=headers, json=json_body)
    _ensure_ok(response, f"Local API request failed: {method} {path}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Local API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Local API returned an invalid response")
    return result


def _active_tasks(tasks: dict[str, Any]) -> list[dict[str, Any]]:
    records = tasks.get("tasks")
    if not isinstance(records, list):
        raise RuntimeError("Local API returned an invalid task list")
    return [task for task in records if isinstance(task, dict) and task.get("status") not in TERMINAL_STATUSES]


async def _reset_demo_page(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    tasks = await _json(client, "GET", "/api/v1/tasks")
    if _active_tasks(tasks):
        raise RuntimeError("Another task is active; the demo will not replace it")
    state = await _json(client, "GET", "/api/v1/desktop/state")
    if not state.get("available"):
        raise RuntimeError("The existing local browser viewer is unavailable")
    if state.get("control_owner") != "none":
        raise RuntimeError("Browser control is currently owned; the demo will not take it")

    takeover = await client.post("/api/v1/profiles/default/takeover", json={}, headers=headers)
    _ensure_ok(takeover, "Demo setup could not acquire browser control")
    try:
        owned_state = await _json(client, "GET", "/api/v1/desktop/state")
        if owned_state.get("control_owner") != "human":
            raise RuntimeError("Demo setup did not acquire browser control")
        reset = await client.post("/api/v1/desktop/action", json={
            "control_epoch": owned_state["control_epoch"],
            "action": {"kind": "navigate", "url": "https://www.selenium.dev/selenium/web/web-form.html"},
        }, headers=headers)
        _ensure_ok(reset, "Demo page could not open")
    finally:
        released = await client.post("/api/v1/profiles/default/release-control", json={}, headers=headers)
        _ensure_ok(released, "Demo setup could not release browser control")


def _demo_spec() -> dict[str, Any]:
    return {
        "goal": "Fill the field labeled Text input with exactly FARA LIVE DEMO. The empty Selenium demonstration form is already open. Do not change other fields. Do not click Submit or send the form. Finish once Text input contains exactly FARA LIVE DEMO.",
        "goal_language": "en",
        "allowed_origins": ["https://www.selenium.dev"],
        "success_conditions": [
            {"kind": "field_equals", "selector": "input[name='my-text']", "value": "FARA LIVE DEMO"},
        ],
        "limits": {"timeout_s": 180, "max_steps": 20},
        "idempotency_key": "visible-demo-" + secrets.token_hex(8),
    }


async def _record_frames(client: httpx.AsyncClient, evidence: Path, frames: list[bytes], total_bytes: int) -> tuple[list[bytes], int]:
    if len(frames) >= MAX_FRAMES or total_bytes >= MAX_FRAME_BYTES:
        return frames, total_bytes
    response = await client.get("/api/v1/desktop/screenshot")
    if not response.is_success:
        return frames, total_bytes
    frame = response.content
    if not frame or total_bytes + len(frame) > MAX_FRAME_BYTES:
        return frames, total_bytes
    frames.append(frame)
    total_bytes += len(frame)
    (evidence / "result.png").write_bytes(frame)
    return frames, total_bytes


def _write_gif(evidence: Path, frames: list[bytes]) -> None:
    if not frames:
        return
    try:
        import io
        from PIL import Image
        images = []
        try:
            for frame in frames:
                with Image.open(io.BytesIO(frame)) as source:
                    source.thumbnail((720, 450))
                    images.append(source.convert("RGB"))
            images[0].save(evidence / "live-demo.gif", save_all=True, append_images=images[1:], duration=650, loop=0)
        finally:
            for image in images:
                image.close()
    except (ImportError, OSError, ValueError):
        return


async def run_demo(*, base_url: str = ORIGIN, operator_key_file: Path | None = None, evidence: Path | None = None) -> dict[str, Any]:
    evidence = evidence or ROOT / "runtime" / "demo"
    evidence.mkdir(parents=True, exist_ok=True)
    with DemoRunLock(evidence / "demo.lock"):
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0, trust_env=False) as client:
            headers = await _operator_headers(client, operator_key_file)
            await _reset_demo_page(client, headers)
            started = await _json(client, "POST", "/api/v1/tasks", headers=headers, json_body=_demo_spec())
            task_id = started.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError("Local API did not return a task ID")
            (evidence / "task-id.txt").write_text(task_id, encoding="utf-8")

            task = started
            frames: list[bytes] = []
            total_bytes = 0
            deadline = time.monotonic() + POLL_TIMEOUT_S
            while time.monotonic() < deadline:
                task = await _json(client, "GET", f"/api/v1/tasks/{task_id}")
                frames, total_bytes = await _record_frames(client, evidence, frames, total_bytes)
                if task.get("status") not in RUNNING_STATUSES:
                    break
                await asyncio.sleep(POLL_INTERVAL_S)
            (evidence / "result.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            _write_gif(evidence, frames)
            if task.get("status") in RUNNING_STATUSES:
                raise RuntimeError("Demo task is still running at the local poll timeout; it was not cancelled. Check the viewer before rerunning.")
            return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-key-file", type=Path, help="Operator key file for token-authenticated deployments")
    parser.add_argument("--base-url", default=ORIGIN, help=argparse.SUPPRESS)
    args = parser.parse_args()
    task = asyncio.run(run_demo(base_url=args.base_url, operator_key_file=args.operator_key_file))
    print(f"Task state: {task.get('status', 'unknown')}")
    print(f"Viewer: {args.base_url.rstrip('/')}/desktop")


if __name__ == "__main__":
    main()
