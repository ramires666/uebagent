"""Isolated Camoufox recovery smoke; never uses the configured user profile."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import time
from pathlib import Path

from worker.app import BrowserRuntime, CookieImportBody, WorkerError


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


async def wait_for_recovery(runtime: BrowserRuntime, generation: int, timeout_s: float = 45.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        status = await runtime.health()
        if (status.get("ready") is True and status.get("recovery_generation", 0) >= generation
                and status.get("context_count") == 1 and status.get("page_count") == 1):
            return status
        if status.get("recovery_state") in {"blocked", "exhausted"}:
            keys = ("recovery_state", "recovery_reason", "recovery_generation", "recovery_attempts",
                    "recovery_retry_after_seconds", "browser_started", "context_count", "page_count", "resource_violation")
            summary = ", ".join(f"{key}={status.get(key)!r}" for key in keys)
            teardown_type = getattr(runtime, "_recovery_teardown_error_type", None)
            raise AssertionError(f"browser recovery entered a terminal state: {summary}; teardown_type={teardown_type!r}")
        await asyncio.sleep(0.2)
    status = await runtime.health()
    keys = ("recovery_state", "recovery_reason", "recovery_generation", "recovery_attempts",
            "recovery_retry_after_seconds", "browser_started", "context_count", "page_count", "resource_violation")
    summary = ", ".join(f"{key}={status.get(key)!r}" for key in keys)
    teardown_type = getattr(runtime, "_recovery_teardown_error_type", None)
    raise AssertionError(f"browser did not recover to one ready context and page: {summary}; teardown_type={teardown_type!r}")


def camoufox_parent_pid(profile_path: Path) -> int:
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / "comm").read_text(encoding="utf-8").strip() != "camoufox-bin":
                continue
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if b"-wait-for-browser" not in argv or b"-profile" not in argv:
                continue
            profile_index = argv.index(b"-profile") + 1
            if profile_index < len(argv) and argv[profile_index].decode("utf-8") == str(profile_path):
                return int(process.name)
        except (FileNotFoundError, PermissionError, ValueError, UnicodeDecodeError):
            continue
    raise AssertionError("isolated Camoufox parent process was not found")


def camoufox_descendants(root_pid: int) -> set[int]:
    parent_by_pid = {}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            stat = (process / "stat").read_text(encoding="utf-8")
            rest = stat[stat.rfind(")") + 2:].split()
            parent_by_pid[int(process.name)] = int(rest[1])
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    descendants = {root_pid}
    while True:
        children = {pid for pid, parent in parent_by_pid.items() if parent in descendants}
        expanded = descendants | children
        if expanded == descendants:
            return descendants
        descendants = expanded


def process_is_live(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat[stat.rfind(")") + 2:].split()[0] != "Z"
    except (FileNotFoundError, PermissionError, ValueError):
        return False


async def run_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="uebagent-recovery-smoke-") as scratch:
        runtime = BrowserRuntime(
            Path(scratch), backend="camoufox", headless=env_bool("BROWSER_HEADLESS", True),
            egress_ready=True, proxy_assignment_id="direct-development", route_generation=1,
        )
        watchdog: asyncio.Task | None = None
        try:
            await runtime.start()
            before = await runtime.health()
            assert before["ready"] is True
            assert before["context_count"] == before["page_count"] == 1
            assert (await runtime._adapter.url()) == "about:blank"
            profile_path = runtime.data_dir / "profiles" / "default"
            assert profile_path.is_dir()

            cookie_expiry = int(time.time()) + 86_400
            cookie_result = await runtime.import_cookies(CookieImportBody(job_id="smoke-cookie-job", cookies=[{
                "name": "recovery_marker", "value": "synthetic-smoke-value",
                "domain": "example.test", "path": "/", "secure": False, "httpOnly": False,
                "expires": cookie_expiry,
            }]))
            assert cookie_result["applied"] == 1 and cookie_result["failed"] == 0

            action_id = "smoke-ledger-action"
            fingerprint = "synthetic-smoke-fingerprint"
            assert runtime._ledger_claim(action_id, fingerprint) is None
            receipt = {"outcome": "applied", "action_id": action_id, "result": {"kind": "fixture"}}
            runtime._ledger_finish(action_id, receipt)
            runtime._claim_capability("smoke-capability", action_id)

            context_before = runtime._context
            page_before = runtime._page
            generation_before = before["recovery_generation"]
            browser_instance_id = before["browser_instance_id"]
            watchdog = asyncio.create_task(runtime.recovery_watchdog())
            page_close_pid = camoufox_parent_pid(profile_path)
            await runtime._page.close()
            after = await wait_for_recovery(runtime, generation_before + 1)

            assert after["recovery_generation"] == generation_before + 1
            assert after["browser_instance_id"] == browser_instance_id
            assert after["context_count"] == after["page_count"] == 1
            assert runtime._context is not context_before
            assert runtime._page is not page_before
            assert (runtime.data_dir / "profiles" / "default") == profile_path
            assert profile_path.is_dir()
            assert (await runtime._adapter.url()) == "about:blank"

            cookies = await runtime._adapter.get_cookies(["https://example.test/"])
            marker = [item for item in cookies if item.get("name") == "recovery_marker"]
            assert (len(marker) == 1 and marker[0].get("value") == "synthetic-smoke-value"
                    and abs(marker[0].get("expires", 0) - cookie_expiry) <= 1)
            assert runtime._ledger_lookup(action_id, fingerprint) == receipt
            try:
                runtime._claim_capability("smoke-capability", "another-action")
            except WorkerError as exc:
                assert exc.code == "CAPABILITY_ALREADY_USED"
            else:
                raise AssertionError("capability-use receipt was not preserved")

            stable_context, stable_page = runtime._context, runtime._page
            repeated = await runtime.recover()
            assert repeated["outcome"] == "already_ready"
            assert repeated["recovery_generation"] == after["recovery_generation"]
            assert runtime._context is stable_context and runtime._page is stable_page

            dead_browser_pid = camoufox_parent_pid(profile_path)
            old_browser_processes = camoufox_descendants(dead_browser_pid)
            os.kill(dead_browser_pid, signal.SIGKILL)
            after_process_death = await wait_for_recovery(runtime, generation_before + 2)
            assert after_process_death["recovery_generation"] == generation_before + 2
            assert after_process_death["browser_instance_id"] == browser_instance_id
            assert after_process_death["context_count"] == after_process_death["page_count"] == 1
            assert runtime._context is not stable_context and runtime._page is not stable_page
            assert all(not process_is_live(pid) for pid in old_browser_processes)
            assert await runtime._adapter.url() == "about:blank"
            cookies_after_process_death = await runtime._adapter.get_cookies(["https://example.test/"])
            assert any(item.get("name") == "recovery_marker" and
                       item.get("value") == "synthetic-smoke-value" and
                       abs(item.get("expires", 0) - cookie_expiry) <= 1 for item in cookies_after_process_death)

            print("PASS: recovered after canonical-page close and Camoufox parent SIGKILL; isolated persistent cookie, action receipt, and capability use survived")
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            await runtime.close()


if __name__ == "__main__":
    asyncio.run(run_smoke())
