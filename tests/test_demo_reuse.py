import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "show_demo.py"
SPEC = importlib.util.spec_from_file_location("show_demo_under_test", SCRIPT)
show_demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(show_demo)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, *, active_tasks=None, owner="none", auth_status=200, task_status="succeeded"):
        self.active_tasks = active_tasks or []
        self.owner = owner
        self.auth_status = auth_status
        self.task_status = task_status
        self.calls = []
        self.base_url = httpx.URL("http://localhost:8080")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, path):
        self.calls.append(("GET", path, None))
        if path == "/api/v1/operator/session":
            return FakeResponse(self.auth_status, {"csrf_token": "csrf-fixture"})
        if path == "/api/v1/tasks":
            return FakeResponse(payload={"tasks": self.active_tasks})
        if path == "/api/v1/desktop/state":
            return FakeResponse(payload={"available": True, "control_owner": self.owner, "control_epoch": 9})
        if path.startswith("/api/v1/tasks/"):
            return FakeResponse(payload={"task_id": "task-fixture", "status": self.task_status})
        if path == "/api/v1/desktop/screenshot":
            return FakeResponse(content=b"png-frame")
        raise AssertionError(f"Unexpected GET {path}")

    async def request(self, method, path, *, headers=None, json=None):
        if method == "GET":
            return await self.get(path)
        if method == "POST":
            return await self.post(path, headers=headers, json=json)
        raise AssertionError(f"Unexpected {method} {path}")

    async def post(self, path, *, headers=None, json=None):
        self.calls.append(("POST", path, json))
        if path == "/api/v1/operator/session":
            return FakeResponse(payload={"csrf_token": "csrf-fixture"})
        if path == "/api/v1/profiles/default/takeover":
            self.owner = "human"
            return FakeResponse()
        if path == "/api/v1/profiles/default/release-control":
            self.owner = "none"
            return FakeResponse()
        if path == "/api/v1/desktop/action":
            return FakeResponse()
        if path == "/api/v1/tasks":
            return FakeResponse(payload={"task_id": "task-fixture", "status": "queued"})
        raise AssertionError(f"Unexpected POST {path}")


def install_fake_client(monkeypatch, client):
    def factory(**kwargs):
        client.async_client_options = kwargs
        return client
    monkeypatch.setattr(show_demo.httpx, "AsyncClient", factory)


def test_demo_reuses_http_viewer_and_runs_bounded_task(tmp_path, monkeypatch):
    client = FakeClient()
    install_fake_client(monkeypatch, client)

    result = asyncio.run(show_demo.run_demo(evidence=tmp_path))

    assert result["status"] == "succeeded"
    assert client.async_client_options["trust_env"] is False
    paths = [path for _, path, _ in client.calls]
    assert "/api/v1/profiles/default/takeover" in paths
    assert "/api/v1/desktop/action" in paths
    assert "/api/v1/profiles/default/release-control" in paths
    assert "/api/v1/tasks" in paths
    assert not any("/cancel" in path for path in paths)
    assert (tmp_path / "task-id.txt").read_text(encoding="utf-8") == "task-fixture"
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["status"] == "succeeded"
    assert (tmp_path / "result.png").read_bytes() == b"png-frame"
    task_spec = next(body for method, path, body in client.calls if method == "POST" and path == "/api/v1/tasks")
    assert task_spec["success_conditions"] == [
        {"kind": "field_equals", "selector": "input[name='my-text']", "value": "FARA LIVE DEMO"}
    ]
    assert task_spec["limits"] == {"timeout_s": 180, "max_steps": 20}


def test_active_task_blocks_before_browser_setup_and_is_never_cancelled(tmp_path, monkeypatch):
    client = FakeClient(active_tasks=[{"task_id": "existing", "status": "running"}])
    install_fake_client(monkeypatch, client)

    with pytest.raises(RuntimeError, match="Another task is active"):
        asyncio.run(show_demo.run_demo(evidence=tmp_path))

    paths = [path for _, path, _ in client.calls]
    assert paths == ["/api/v1/operator/session", "/api/v1/tasks"]
    assert not any("cancel" in path or "takeover" in path for path in paths)


def test_current_human_owner_is_preserved(tmp_path, monkeypatch):
    client = FakeClient(owner="human")
    install_fake_client(monkeypatch, client)

    with pytest.raises(RuntimeError, match="currently owned"):
        asyncio.run(show_demo.run_demo(evidence=tmp_path))

    assert not any(path.endswith("/takeover") or path.endswith("/desktop/action") for _, path, _ in client.calls)


def test_demo_lock_rejects_a_simultaneous_run(tmp_path):
    lock_path = tmp_path / "runtime" / "demo.lock"
    with show_demo.DemoRunLock(lock_path):
        with pytest.raises(RuntimeError, match="already holds the lock"):
            with show_demo.DemoRunLock(lock_path):
                pass


def test_token_mode_reads_operator_key_file_without_exposing_it(tmp_path, monkeypatch):
    client = FakeClient(auth_status=401)
    install_fake_client(monkeypatch, client)
    key_file = tmp_path / "operator-token.txt"
    key_file.write_text("private-test-key", encoding="utf-8")

    headers = asyncio.run(show_demo._operator_headers(client, key_file))

    assert headers["X-CSRF-Token"] == "csrf-fixture"
    login = next(body for method, path, body in client.calls if method == "POST" and path == "/api/v1/operator/session")
    assert login == {"token": "private-test-key"}


def test_nonrunning_nonterminal_state_returns_immediately(tmp_path, monkeypatch):
    client = FakeClient(task_status="recovering")
    install_fake_client(monkeypatch, client)

    result = asyncio.run(show_demo.run_demo(evidence=tmp_path))

    assert result["status"] == "recovering"
    assert len([1 for method, path, _ in client.calls if method == "GET" and path == "/api/v1/tasks/task-fixture"]) == 1


def test_poll_timeout_reports_uncertain_task_without_cancelling(tmp_path, monkeypatch):
    client = FakeClient()
    install_fake_client(monkeypatch, client)
    monkeypatch.setattr(show_demo, "POLL_TIMEOUT_S", 0)

    with pytest.raises(RuntimeError, match="still running.*not cancelled"):
        asyncio.run(show_demo.run_demo(evidence=tmp_path))

    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["status"] == "queued"
    assert not any("/cancel" in path for _, path, _ in client.calls)


def test_gif_frames_are_resized_before_writing(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    import io
    frames = []
    for color in ((1, 2, 3), (3, 2, 1)):
        frame_buffer = io.BytesIO()
        Image.new("RGB", (1440, 900), color).save(frame_buffer, format="PNG")
        frames.append(frame_buffer.getvalue())

    show_demo._write_gif(tmp_path, frames)

    with Image.open(tmp_path / "live-demo.gif") as gif:
        assert gif.size == (720, 450)
        assert gif.n_frames == 2


def test_script_contains_no_browser_launch_or_new_window_code():
    source = Path(show_demo.__file__).read_text(encoding="utf-8").lower()
    assert "playwright" not in source
    assert "webbrowser.open" not in source
    assert ".launch(" not in source
    assert "new_page(" not in source
