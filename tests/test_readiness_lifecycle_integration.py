"""Local safety guards for the opt-in lifecycle integration command."""
from __future__ import annotations

import importlib.util
import asyncio
import httpx
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "readiness_lifecycle_integration.py"
spec = importlib.util.spec_from_file_location("readiness_lifecycle_integration", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(module)


class LifecycleIntegrationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Path(self.temp.name) / "worker-key"
        self.key.write_text("disposable-key\n", encoding="utf-8")
        self.env = {
            "APP_ENV": "test", "READINESS_DISPOSABLE": "1",
            "READINESS_RUNTIME_INTEGRATION": "1", "READINESS_ACTION_TRACE": "1",
            "BROWSER_WORKER_URL": module.WORKER_URL, "WORKER_API_KEY_FILE": str(self.key),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(module.IntegrationGuardError, "explicit_disposable_confirmation_required"):
            module.validate_environment(confirmed=False, environ=self.env, docker_marker=True)

    def test_requires_all_disposable_markers(self):
        env = dict(self.env)
        env.pop("READINESS_ACTION_TRACE")
        with self.assertRaisesRegex(module.IntegrationGuardError, "integration_and_action_trace_opt_in_required"):
            module.validate_environment(confirmed=True, environ=env, docker_marker=True)

    def test_requires_fixed_worker_and_docker(self):
        env = dict(self.env, BROWSER_WORKER_URL="http://other:7070")
        with self.assertRaisesRegex(module.IntegrationGuardError, "worker_url_must_be_exact_disposable_service"):
            module.validate_environment(confirmed=True, environ=env, docker_marker=True)
        with self.assertRaisesRegex(module.IntegrationGuardError, "container_only_execution_required"):
            module.validate_environment(confirmed=True, environ=self.env, docker_marker=False)

    def test_accepts_only_guarded_disposable_context(self):
        self.assertEqual(module.validate_environment(confirmed=True, environ=self.env,
                                                     docker_marker=True), self.key)


class ReusableTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_client_closing_each_call_does_not_close_reusable_proxy(self):
        forwarded = []

        async def handler(request):
            forwarded.append(request.url.path)
            body = {"outcome": "applied"} if request.url.path == "/execute" else {"ready": True}
            return httpx.Response(200, json=body, request=request)

        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            key.write_text("test-key", encoding="utf-8")
            settings = module.Settings(data_dir=Path(temporary), worker_url=module.WORKER_URL,
                                       worker_key_file=str(key), model_prompt_file="", test_mode=True)
            proxy = module.DropFirstExecuteReply(upstream_handler=handler)
            client = module.WorkerClient(settings, transport=proxy)
            with self.assertRaises(module.DomainError) as caught:
                await client.call("POST", "/execute", {"action": {"kind": "click"}})
            self.assertEqual(caught.exception.code, "WORKER_UNAVAILABLE")
            state = await client.call("GET", "/status")
            self.assertEqual(state, {"ready": True})
            self.assertEqual(proxy.execute_forward_count, 1)
            self.assertEqual(forwarded, ["/execute", "/status"])
            await proxy.aclose()

    async def test_held_execute_can_be_cancelled_without_a_second_forward(self):
        forwarded = []

        async def handler(request):
            forwarded.append(request.url.path)
            return httpx.Response(200, json={"outcome": "applied"}, request=request)

        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            key.write_text("test-key", encoding="utf-8")
            settings = module.Settings(data_dir=Path(temporary), worker_url=module.WORKER_URL,
                                       worker_key_file=str(key), model_prompt_file="", test_mode=True)
            proxy = module.HoldFirstExecuteReply(upstream_handler=handler)
            client = module.WorkerClient(settings, transport=proxy)
            request_task = asyncio.create_task(client.call("POST", "/execute", {"action": {"kind": "click"}}))
            await asyncio.wait_for(proxy.forwarded.wait(), timeout=1)
            self.assertEqual(proxy.execute_forward_count, 1)
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
            await client.call("GET", "/status")
            self.assertEqual(proxy.execute_forward_count, 1)
            self.assertEqual(forwarded, ["/execute", "/status"])
            await proxy.aclose()


if __name__ == "__main__":
    unittest.main()
