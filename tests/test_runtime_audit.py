"""Run only in a disposable, network-disabled container with Xvfb."""
import asyncio
import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from worker.app import BrowserRuntime, ManualExecuteBody, WorkerError


@unittest.skipUnless(os.environ.get("RUNTIME_AUDIT") == "1", "requires isolated disposable browser container")
class RuntimeAudit(unittest.IsolatedAsyncioTestCase):
    async def test_camoufox_restart_fencing_capture_and_extra_page(self):
        with tempfile.TemporaryDirectory(prefix="runtime-audit-") as directory:
            fingerprint_file = Path(directory) / "profiles" / "default.camoufox-fingerprint.json"
            first = BrowserRuntime(directory, backend="camoufox", headless=False,
                                   egress_ready=True, route_generation=1)
            second = None
            try:
                await first.start()
                state = first.status_base()
                self.assertEqual((state["context_count"], state["page_count"]), (1, 1))
                ua1 = await first._page.evaluate("navigator.userAgent")
                fingerprint_hash = hashlib.sha256(fingerprint_file.read_bytes()).hexdigest()
                await first._page.set_content('<input id="input" style="position:absolute;left:50px;top:50px;width:200px;height:50px">')
                observation = await first.observe()
                self.assertEqual((observation["width"], observation["height"]), (1440, 900))
                self.assertTrue(base64.b64decode(observation["screenshot_png_b64"]).startswith(b'\x89PNG\r\n\x1a\n'))
                human = first.gate("human")
                click = await first.execute_manual(ManualExecuteBody(control_epoch=human["control_epoch"],
                    action={"kind": "click", "x": 100, "y": 70}, allowed_origins=[]))
                self.assertEqual(click["outcome"], "applied")
                self.assertEqual(await first._page.evaluate("document.activeElement.id"), "input")
                typed = await first.execute_manual(ManualExecuteBody(control_epoch=click["control_epoch"],
                    action={"kind": "type_text", "text": "Audit текст"}, allowed_origins=[]))
                self.assertEqual(typed["outcome"], "applied")
                self.assertEqual(await first._page.locator("#input").input_value(), "Audit текст")
                agent = first.gate("agent")
                first._lease_deadline = 0
                with self.assertRaises(WorkerError) as expired:
                    first._check_agent_gate(agent["control_epoch"])
                self.assertEqual(expired.exception.code, "AGENT_LEASE_EXPIRED")
                with self.assertRaises(WorkerError) as stale:
                    first._check_epoch(agent["control_epoch"])
                self.assertEqual(stale.exception.code, "STALE_CONTROL_EPOCH")
                try:
                    await first._context.new_page()
                except Exception:
                    pass
                for _ in range(40):
                    if first.status_base()["page_count"] == 1 and first.status_base()["closed_extra_pages"] >= 1:
                        break
                    await asyncio.sleep(.1)
                state = first.status_base()
                self.assertEqual(state["page_count"], 1)
                self.assertGreaterEqual(state["closed_extra_pages"], 1)
                self.assertFalse(state["resource_violation"])
                await first.close()
                second = BrowserRuntime(directory, backend="camoufox", headless=False,
                                        egress_ready=True, route_generation=1)
                await second.start()
                self.assertEqual(await second._page.evaluate("navigator.userAgent"), ua1)
                self.assertEqual(hashlib.sha256(fingerprint_file.read_bytes()).hexdigest(), fingerprint_hash)
                state = second.status_base()
                self.assertEqual((state["context_count"], state["page_count"]), (1, 1))
            finally:
                if second is not None:
                    await second.close()
                await first.close()

if __name__ == "__main__":
    unittest.main()
