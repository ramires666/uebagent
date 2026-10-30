import asyncio
import json
import time

import pytest
from playwright.async_api import async_playwright

from worker.app import BrowserRuntime
from worker.observation_hints import (
    INTERACTION_HINTS_JS,
    MAX_SERIALIZED_BYTES,
    collect_interaction_hints,
    empty_hints,
    enabled,
    sanitize_hints,
)
from src.browser_mcp.model_client import _sanitize_observation_hints


def test_opt_in_is_explicit_and_defaults_off(monkeypatch):
    monkeypatch.delenv("BROWSER_OBSERVATION_HINTS", raising=False)
    assert enabled() is False
    monkeypatch.setenv("BROWSER_OBSERVATION_HINTS", "true")
    assert enabled() is False
    monkeypatch.setenv("BROWSER_OBSERVATION_HINTS", "1")
    assert enabled() is True


def test_sanitizer_drops_untrusted_fields_and_enforces_serialized_byte_cap():
    raw_element = {
        "tag": "button", "role": "button", "label": "x" * 80,
        "center": {"x": 500, "y": 500}, "bounds": {"x": 400, "y": 400, "width": 200, "height": 200},
        "kind": "control", "id": "private-id", "href": "https://secret.invalid/?token=x",
        "value": "password", "text": "private contents", "password": "secret",
    }
    hints = sanitize_hints({"version": 1, "coordinate_space": "normalized_1000", "elements": [dict(raw_element) for _ in range(32)]})
    encoded = json.dumps(hints, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(hints["elements"]) <= 32 and len(encoded) <= MAX_SERIALIZED_BYTES
    assert "private-id" not in encoded.decode() and "secret.invalid" not in encoded.decode()
    assert "private contents" not in encoded.decode() and "password" not in encoded.decode()
    assert all(set(item) <= {"tag", "role", "label", "center", "bounds", "checked", "expanded", "value_present", "kind"}
               for item in hints["elements"])
    assert sanitize_hints({"version": 1, "coordinate_space": "normalized_1000", "elements": [{"tag": {}, "role": "button"}]}) == empty_hints()


def test_sanitizer_output_matches_model_client_at_decimal_boundaries_and_keeps_useful_labels():
    def item(label, bounds, center):
        return {"tag": "button", "role": "button", "label": label,
                "bounds": bounds, "center": center}

    raw = {"version": 1, "coordinate_space": "normalized_1000", "elements": [
        item("Right edge", {"x": 999.994, "y": 0, "width": 0.006, "height": 1},
             {"x": 999.999, "y": 0.5}),
        item("Bottom edge", {"x": 0, "y": 999.994, "width": 1, "height": 0.006},
             {"x": 0.5, "y": 999.999}),
        *[item(f"Useful control {i:02d}", {"x": 400, "y": 400, "width": 200, "height": 200},
               {"x": 500, "y": 500}) for i in range(32)],
    ]}
    hints = sanitize_hints(raw)
    assert len(hints["elements"]) <= 32
    assert len(json.dumps(hints, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MAX_SERIALIZED_BYTES
    assert all(element["label"] for element in hints["elements"])
    assert json.loads(_sanitize_observation_hints(hints)) == hints


@pytest.mark.asyncio
async def test_offline_chromium_hints_are_bounded_private_and_coordinate_aligned():
    html = """<!doctype html><style>
      body{margin:0} .rc-tree-treenode{position:relative;height:64px}
      .rc-tree-switcher{position:absolute;left:20px;top:20px;width:20px;height:20px}
      .rc-tree-checkbox,.rct-checkbox{position:absolute;top:20px;width:20px;height:20px}
      .rc-tree-checkbox{left:100px}.rct-checkbox{left:220px}
      [role=checkbox]{position:absolute;left:60px;top:20px;width:20px;height:20px}
      #cover{position:absolute;left:120px;top:20px;width:80px;height:32px;z-index:5;background:#888}
      #covered{position:absolute;left:120px;top:20px;width:80px;height:32px}
      #far{position:absolute;left:1600px;top:0}
      #edge{position:absolute;left:1430px;top:100px;width:50px;height:24px}
    </style><main>
      <div class="rc-tree-treenode" role="treeitem" aria-label="Projects" aria-expanded="false">
        <span class="rc-tree-title">Projects</span>
        <span class="rc-tree-switcher" aria-hidden="true">+</span>
        <input type="checkbox" id="native" aria-label="Native box">
        <span role="checkbox" aria-label="Select Desktop" aria-checked="false"></span>
        <span class="rc-tree-checkbox rc-tree-checkbox-unchecked"></span>
        <span class="rct-checkbox rct-checkbox-check"></span>
      </div>
      <input id="firstName" placeholder="First name" value="sensitive-value">
      <input type="password" value="secret-password">
      <button hidden>Hidden button</button><button disabled>Disabled button</button>
      <button id="covered">Occluded button</button><div id="cover"></div>
      <button id="far">Offscreen button</button>
      <button id="edge">Viewport edge</button>
      <button aria-label='x&quot; onmouseover=&quot;alert(1)'>Safe label injection</button>
    </main>"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.route("https://hints.test/**", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        await page.goto("https://hints.test/", wait_until="domcontentloaded")
        raw = await page.evaluate(INTERACTION_HINTS_JS, {"width": 1440, "height": 900})
        assert raw["version"] == 1 and isinstance(raw["elements"], list)
        hints = await collect_interaction_hints(page, 1440, 900)
        assert json.loads(_sanitize_observation_hints(hints)) == hints
        serialized = json.dumps(hints, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(serialized) <= MAX_SERIALIZED_BYTES and len(hints["elements"]) <= 32
        desktop = next(item for item in hints["elements"] if item["label"] == "Select Desktop")
        expander = next(item for item in hints["elements"] if item["kind"] == "expander")
        assert desktop["role"] == "checkbox" and desktop["checked"] is False
        tree_checks = [item for item in hints["elements"] if item["role"] == "checkbox" and item["label"] == "Projects"]
        assert len(tree_checks) == 2 and all(item["checked"] is not None for item in tree_checks)
        assert expander["label"] == "Projects" and expander["expanded"] is False
        pixel_box = await page.locator('[aria-label="Select Desktop"]').bounding_box()
        expected_x = round((pixel_box["x"] + pixel_box["width"] / 2) / 1440 * 1000, 2)
        expected_y = round((pixel_box["y"] + pixel_box["height"] / 2) / 900 * 1000, 2)
        assert desktop["center"] == {"x": expected_x, "y": expected_y}
        assert desktop["bounds"]["width"] > 0 and desktop["bounds"]["height"] > 0
        edge = next(item for item in hints["elements"] if item["label"] == "Viewport edge")
        assert edge["center"]["x"] < 1000
        assert edge["bounds"]["x"] + edge["bounds"]["width"] <= 1000
        field = next(item for item in hints["elements"] if item["label"] == "First name")
        assert field["value_present"] is True
        labels = [item["label"] for item in hints["elements"]]
        assert "Hidden button" not in labels and "Disabled button" not in labels
        assert "Occluded button" not in labels and "Offscreen button" not in labels
        assert "sensitive-value" not in serialized.decode() and "secret-password" not in serialized.decode()
        injected = next(item for item in hints["elements"] if item["label"].startswith("x\" onmouseover"))
        assert injected["label"].startswith("x\" onmouseover")
        assert await page.locator("body").locator("script").count() == 0
        await browser.close()


def test_probe_failure_returns_empty_hints():
    class Page:
        async def evaluate(self, script, args):
            assert script == INTERACTION_HINTS_JS
            raise RuntimeError("private browser failure")

    hints = asyncio.run(collect_interaction_hints(Page(), 1, 1))
    assert hints == empty_hints()


def test_probe_timeout_is_bounded_and_returns_empty_hints():
    class SlowPage:
        async def evaluate(self, *args): await asyncio.sleep(3)
    started = time.monotonic()
    hints = asyncio.run(collect_interaction_hints(SlowPage(), 1440, 900))
    assert hints == empty_hints()
    assert time.monotonic() - started < 1.5


@pytest.mark.asyncio
async def test_about_blank_page_returns_no_hints():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.set_content('<button>Not a public HTTPS page</button>')
        assert await collect_interaction_hints(page, 1440, 900) == empty_hints()
        await browser.close()


@pytest.mark.asyncio
async def test_observe_includes_hints_only_when_opted_in(monkeypatch, tmp_path):
    class Adapter:
        async def screenshot(self, **kwargs): return b"png"
        async def url(self): return "https://hints.test/"
    class Page:
        calls = 0
        async def evaluate(self, script, *args):
            self.calls += 1
            if script == "() => ({width: innerWidth, height: innerHeight})":
                return {"width": 1440, "height": 900}
            return {"version": 1, "coordinate_space": "normalized_1000", "elements": []}
    page = Page()
    runtime = BrowserRuntime(tmp_path, adapter=Adapter(), page=page, test_mode=True)
    monkeypatch.delenv("BROWSER_OBSERVATION_HINTS", raising=False)
    off = await runtime.observe()
    assert "interaction_hints" not in off and page.calls == 1
    monkeypatch.setenv("BROWSER_OBSERVATION_HINTS", "1")
    on = await runtime.observe()
    assert on["interaction_hints"] == empty_hints() and on["width"] == 1440 and on["height"] == 900
    assert page.calls == 3
