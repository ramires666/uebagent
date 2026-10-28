from __future__ import annotations

import base64
from pathlib import Path

import pytest

from worker.app import (
    AgentExecuteBody,
    BrowserRuntime,
    CookieImportBody,
    ManualExecuteBody,
    WorkerError,
    _adapter_class,
)


CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


@pytest.mark.asyncio
async def test_real_chromium_worker_observe_manual_gate_agent_guard_cookie_import_and_verify(tmp_path):
    playwright_async = pytest.importorskip("playwright.async_api", reason="Playwright is not installed")
    if not CHROME.is_file():
        pytest.skip("the local Chrome executable is unavailable")

    playwright = await playwright_async.async_playwright().start()
    browser = None
    runtime = None
    try:
        try:
            browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        except Exception as exc:
            pytest.skip(f"local Chrome could not launch in headless mode: {type(exc).__name__}")
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        await context.add_cookies([
            {"name": "unrelated", "value": "keep", "domain": "shop.test", "path": "/other",
             "secure": True, "httpOnly": False, "sameSite": "Lax"},
        ])
        page = await context.new_page()
        await page.set_content(
            """<!doctype html><html><body>
              <h1>Local worker fixture</h1>
              <label for="field">Search</label><input id="field" type="text">
              <button id="submit" type="button" onclick="document.body.dataset.clicked='yes'">Submit</button>
            </body></html>"""
        )
        adapter = _adapter_class()(page)
        runtime = BrowserRuntime(
            tmp_path, backend="chromium", headless=True, egress_ready=True, route_generation=1,
            adapter=adapter, page=page, context=context, test_mode=True,
        )

        observation = await runtime.observe()
        screenshot = base64.b64decode(observation["screenshot_png_b64"])
        assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
        assert observation["width"] == 1440 and observation["height"] == 900
        assert observation["browser_id"] == "browser-001" and observation["page_id"] == "main"

        human = runtime.gate("human")
        await page.locator("#field").focus()
        manual = await runtime.execute_manual(ManualExecuteBody(
            control_epoch=human["control_epoch"],
            action={"kind": "type_text", "text": "typed by the human path"},
            allowed_origins=[],
        ))
        assert manual["outcome"] == "applied"
        field = await runtime.verify([{"kind": "field_equals", "selector": "#field", "value": "typed by the human path"}])
        assert field == {"verified": True, "checks": [{"kind": "field_equals", "passed": True}]}

        agent = runtime.gate("agent")
        current = await runtime.observe()
        button_box = await page.locator("#submit").bounding_box()
        assert button_box is not None
        with pytest.raises(WorkerError, match="button-like target") as approval:
            await runtime.execute_agent(AgentExecuteBody(
                action_id="button-action", control_epoch=agent["control_epoch"],
                observation_id=current["observation_id"], allowed_origins=["https://shop.test"],
                action={"kind": "click", "x": button_box["x"] + button_box["width"] / 2,
                        "y": button_box["y"] + button_box["height"] / 2, "button": "left", "count": 1},
            ))
        assert approval.value.code == "APPROVAL_REQUIRED"
        assert await page.locator("body").get_attribute("data-clicked") is None

        stale_epoch = runtime.gate("agent")
        stale_observation = await runtime.observe()
        runtime.gate("human")
        with pytest.raises(WorkerError) as stale:
            await runtime.execute_agent(AgentExecuteBody(
                action_id="stale-action", control_epoch=stale_epoch["control_epoch"],
                observation_id=stale_observation["observation_id"],
                action={"kind": "history_back"},
            ))
        assert stale.value.code == "STALE_CONTROL_EPOCH"

        runtime.gate("none")
        imported = await runtime.import_cookies(CookieImportBody(job_id="selected-cookie-job", cookies=[{
            "source_key": "selected-cookie-1", "name": "selected", "value": "only-this-value",
            "domain": "shop.test", "path": "/selected", "store_id": "1", "host_only": True,
            "secure": True, "http_only": True, "same_site": "strict", "expires_at": None,
            "partition_key": None, "unsupported_reason": "", "site_ids": ["shop-site"],
        }]))
        assert imported["applied"] == 1 and "only-this-value" not in str(imported)
        cookies = await context.cookies()
        by_name = {cookie["name"]: cookie for cookie in cookies}
        assert by_name["unrelated"]["value"] == "keep"
        assert by_name["unrelated"]["path"] == "/other"
        assert by_name["selected"]["value"] == "only-this-value"
        assert by_name["selected"]["path"] == "/selected"
        assert by_name["selected"]["secure"] is True and by_name["selected"]["httpOnly"] is True
        assert by_name["selected"]["sameSite"] == "Strict"
        text = await runtime.verify([{"kind": "text_present", "text": "Local worker fixture"}])
        assert text["verified"] is True
    finally:
        if runtime is not None:
            await runtime.close()
        if browser is not None:
            await browser.close()
        await playwright.stop()
