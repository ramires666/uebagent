"""Isolated, network-free Camoufox cookie persistence integration test.

Opt in with RUN_CAMOUFOX_COOKIE_TEST=1 inside a disposable Xvfb container.
All cookies and profiles in this test are synthetic.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get("RUN_CAMOUFOX_COOKIE_TEST") == "1", "requires isolated Camoufox and X11")
class CamoufoxCookiePersistenceTest(unittest.TestCase):
    def test_import_readback_scope_and_restart(self):
        from worker.app import BrowserRuntime, CookieImportBody

        async def run():
            with tempfile.TemporaryDirectory(prefix="synthetic-cookie-test-") as directory:
                expiry = int(time.time()) + 86400
                entries = [
                    {"name": "__Host-fixture", "value": "fixture-host-secret", "domain": "app.example.test",
                     "path": "/", "host_only": True, "secure": True, "http_only": True,
                     "same_site": "strict", "expires_at": expiry},
                    {"name": "__Secure-fixture", "value": "fixture-domain-secret", "domain": ".example.test",
                     "path": "/account", "host_only": False, "secure": True, "http_only": False,
                     "same_site": "no_restriction", "expires_at": expiry},
                    {"name": "unicode-fixture", "value": "\u0442\u0435\u0441\u0442-\u503c", "domain": "app.example.test",
                     "path": "/", "host_only": True, "secure": True, "http_only": False,
                     "same_site": "lax", "expires_at": expiry},
                ]
                runtime = BrowserRuntime(Path(directory), backend="camoufox", headless=False, test_mode=True)
                try:
                    await runtime.start()
                    receipt = await runtime.import_cookies(CookieImportBody(job_id="synthetic-import", cookies=entries))
                    self.assertEqual(receipt["total"], len(entries))
                    self.assertEqual(receipt["failed"], 0, "synthetic import unexpectedly failed")
                    self.assertEqual(receipt["applied"], len(entries))
                    self.assertTrue(all(item["value"] not in json.dumps(receipt, ensure_ascii=False) for item in entries),
                                    "receipt exposed synthetic cookie value")

                    async def verify(context):
                        rows = {row["name"]: row for row in await context.cookies()}
                        for original in entries:
                            self.assertIn(original["name"], rows, "applied cookie absent on readback")
                            actual = rows[original["name"]]
                            self.assertTrue(actual["value"] == original["value"], "cookie value changed")
                            self.assertEqual(actual["domain"], original["domain"])
                            self.assertEqual(actual["path"], original["path"])
                            self.assertEqual(actual["secure"], original["secure"])
                            self.assertEqual(actual["httpOnly"], original["http_only"])
                            self.assertEqual(actual["sameSite"], {"strict": "Strict", "lax": "Lax", "no_restriction": "None"}[original["same_site"]])
                            self.assertLessEqual(abs(actual["expires"] - expiry), 1)
                        sibling = {row["name"] for row in await context.cookies(["https://other.example.test/account"])}
                        self.assertIn("__Secure-fixture", sibling)
                        self.assertNotIn("__Host-fixture", sibling)
                        wrong_path = {row["name"] for row in await context.cookies(["https://other.example.test/other"])}
                        self.assertNotIn("__Secure-fixture", wrong_path)

                    await verify(runtime._context)
                finally:
                    await runtime.close()
                restarted = BrowserRuntime(Path(directory), backend="camoufox", headless=False, test_mode=True)
                try:
                    await restarted.start()
                    await verify(restarted._context)
                finally:
                    await restarted.close()
        asyncio.run(run())

    def test_negative_inputs_do_not_report_unstored_cookie_as_applied(self):
        from worker.app import BrowserRuntime, CookieImportBody, WorkerError

        async def run():
            with tempfile.TemporaryDirectory(prefix="synthetic-cookie-negative-") as directory:
                runtime = BrowserRuntime(Path(directory), backend="camoufox", headless=False, test_mode=True)
                try:
                    await runtime.start()
                    base = {"value": "synthetic-negative-secret", "domain": "app.example.test", "path": "/",
                            "host_only": True, "secure": True, "http_only": True, "same_site": "lax"}
                    partitioned = {**base, "name": "partitioned-fixture", "partition_key": {"topLevelSite": "https://example.test"}}
                    with self.assertRaises(WorkerError) as rejected:
                        await runtime.import_cookies(CookieImportBody(job_id="partitioned", cookies=[partitioned]))
                    self.assertEqual(rejected.exception.code, "INVALID_COOKIE")
                    self.assertFalse(await runtime._context.cookies())
                    variants = [
                        {**base, "name": "__Host-invalid-domain", "domain": ".example.test", "host_only": False},
                        {**base, "name": "__Host-invalid-path", "path": "/account"},
                        {**base, "name": "__Secure-invalid", "secure": False},
                        {**base, "name": "\u0438\u043c\u044f-fixture"},
                    ]
                    for index, entry in enumerate(variants):
                        with self.assertRaises(WorkerError) as invalid:
                            await runtime.import_cookies(CookieImportBody(job_id=f"negative-{index}", cookies=[entry]))
                        self.assertEqual(invalid.exception.code, "INVALID_COOKIE")
                        self.assertFalse(await runtime._context.cookies())
                finally:
                    await runtime.close()
        asyncio.run(run())
