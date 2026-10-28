"""Standalone synthetic viewer-to-control-to-worker test inside an isolated Camoufox container.

The wrapper supplies VIEWER_HTML and VIEWER_JS as base64 environment variables.
No network, production profile, or real credentials are used.
"""
import asyncio
import base64
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from camoufox.async_api import AsyncCamoufox
from worker.app import BrowserRuntime, create_app as worker_app, _adapter_class
from browser_mcp.app import create_app as control_app
from browser_mcp.config import Settings
from browser_mcp.runtime import WorkerClient


async def exercise():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        async with AsyncCamoufox(headless=False) as browser:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            target = await context.new_page()
            viewer_context = await browser.new_context(viewport={"width": 1440, "height": 900})
            viewer = await viewer_context.new_page()
            await target.set_content('''<input id="one" style="display:block;margin:20px;width:300px;height:40px">
              <input id="two" type="password" style="display:block;margin:20px;width:300px;height:40px">
              <textarea id="three" style="display:block;margin:20px;width:300px;height:40px"></textarea>
              <div id="four" contenteditable="true" style="margin:20px;width:300px;height:40px;border:1px solid"></div>''')
            runtime = BrowserRuntime(root / "worker", egress_ready=True, route_generation=1,
                                     adapter=_adapter_class()(target), page=target, context=context)
            worker = worker_app(runtime=runtime, token="fixture")
            key = root / "key"
            key.write_text("fixture")
            settings = Settings(data_dir=root / "control", ui_dir=root / "no-ui", test_mode=True,
                                operator_auth_mode="local", operator_token="fixture", mcp_token="fixture",
                                worker_url="http://worker", worker_key_file=str(key))
            control = control_app(settings, worker=WorkerClient(settings, transport=httpx.ASGITransport(app=worker)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=control), base_url="http://testserver", timeout=15) as client:
                action_diagnostics = []
                async def intercept(route):
                    request = route.request
                    path = urlsplit(request.url).path
                    if path in {"/desktop.html", "/desktop.js"}:
                        data = os.environ["VIEWER_HTML" if path.endswith("html") else "VIEWER_JS"]
                        await route.fulfill(body=base64.b64decode(data), content_type="text/html; charset=utf-8" if path.endswith("html") else "text/javascript; charset=utf-8")
                    elif path.startswith("/api/v1/"):
                        headers = {k: v for k, v in request.headers.items() if k.lower() in {"content-type", "x-csrf-token"}}
                        response = await client.request(request.method, path, headers=headers, content=request.post_data_buffer)
                        if path == "/api/v1/desktop/action":
                            action = request.post_data_json.get("action", {})
                            action_diagnostics.append((action.get("kind"), len(action.get("text", "")), response.status_code))
                        await route.fulfill(status=response.status_code, body=response.content,
                                            headers={"content-type": response.headers.get("content-type", "application/octet-stream")})
                    else:
                        await route.fulfill(status=404)

                await viewer.route("http://testserver/**", intercept)
                await viewer.goto("http://testserver/desktop.html")
                assert await viewer.evaluate("fetch('/desktop.js').then(r => r.text()).then(s => s.includes('function queueAction'))"), "fixture_loaded_stale_script"
                source = await viewer.evaluate("fetch('/desktop.js').then(r => r.text())")
                print('fixture_js_bytes_equal', hashlib.sha256(source.encode()).digest() == hashlib.sha256(base64.b64decode(os.environ['VIEWER_JS'])).digest(), flush=True)
                await viewer.locator("#takeover").click()
                await viewer.wait_for_function("!document.getElementById('remote-keyboard').disabled")
                await viewer.locator("#browser-image").wait_for(state="visible")
                await viewer.wait_for_function("document.getElementById('browser-image').naturalWidth === 1440")

                async def click_field(selector):
                    rect = await target.locator(selector).bounding_box()
                    image = await viewer.locator("#browser-image").bounding_box()
                    await viewer.mouse.click(image["x"] + (rect["x"] + rect["width"] / 2) * image["width"] / 1440,
                                             image["y"] + (rect["y"] + rect["height"] / 2) * image["height"] / 900)

                sample = "AbПривет😀"
                for selector in ("#one", "#two", "#three", "#four"):
                    await click_field(selector)
                    await viewer.keyboard.insert_text(sample)
                    await asyncio.sleep(1)
                    value = await (target.locator(selector).inner_text() if selector == "#four" else target.locator(selector).input_value())
                    if value != sample:
                        print("synthetic_field_diagnostic", selector, "length", len(value), "focus", await target.evaluate("document.activeElement.id"), "actions", action_diagnostics[-5:], flush=True)
                        raise AssertionError("field_mismatch")
                await click_field("#two")
                await viewer.keyboard.press("Control+a")
                await viewer.keyboard.press("Backspace")
                await asyncio.sleep(1)
                assert await target.locator("#two").input_value() == "", "key_mismatch"
                await click_field("#one")
                await viewer.keyboard.press("Control+a")
                await viewer.keyboard.insert_text("burst")
                clipboard = r'''import select,sys,time
from Xlib import X,Xatom,display,protocol
d=display.Display(); w=d.screen().root.create_window(0,0,1,1,0,0,X.InputOnly,X.CopyFromParent)
c=d.intern_atom('CLIPBOARD'); u=d.intern_atom('UTF8_STRING'); t=d.intern_atom('TARGETS'); w.set_selection_owner(c,X.CurrentTime);d.sync()
sys.stdout.write('R');sys.stdout.flush();end=time.monotonic()+5
while time.monotonic()<end:
 if not d.pending_events(): select.select([d.fileno()],[],[],.1);continue
 e=d.next_event()
 if e.type!=X.SelectionRequest:continue
 p=e.property or e.target
 if e.target==t:e.requestor.change_property(p,Xatom.ATOM,32,[t,u,Xatom.STRING])
 elif e.target in (u,Xatom.STRING):e.requestor.change_property(p,e.target,8,b'paste')
 else:p=X.NONE
 e.requestor.send_event(protocol.event.SelectionNotify(time=e.time,requestor=e.requestor,selection=e.selection,target=e.target,property=p),propagate=False);d.sync()
 if p and e.target in (u,Xatom.STRING):sys.stdout.write('T');sys.stdout.flush();break
w.destroy();d.close()
'''
                owner = await asyncio.create_subprocess_exec(sys.executable, '-c', clipboard, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    assert await asyncio.wait_for(owner.stdout.readexactly(1), 3) == b'R'
                    await viewer.keyboard.press('Control+v')
                    assert await asyncio.wait_for(owner.stdout.readexactly(1), 3) == b'T'
                finally:
                    if owner.returncode is None:
                        owner.kill()
                    await owner.wait()
                await asyncio.sleep(1)
                actual = await target.locator("#one").input_value()
                if actual != "burstpaste":
                    print("synthetic_paste_diagnostic", "length", len(actual), "burst_present", "burst" in actual, "paste_present", "paste" in actual, "actions", action_diagnostics[-6:], "hidden", await viewer.evaluate("document.hidden"), "error", await viewer.evaluate("document.querySelector('#notice').classList.contains('error')"), flush=True)
                    raise AssertionError("paste_mismatch")
                await viewer.keyboard.press("Tab")
                await asyncio.sleep(.5)
                assert await target.evaluate("document.activeElement.id") == "two", "tab_mismatch"
                print("REAL_CAMOUFOX_FIXTURE_OK fields=4 control_a=ok backspace=ok tab=ok trusted_paste=ok", flush=True)
            await viewer_context.close()
            await context.close()


if __name__ == "__main__":
    asyncio.run(exercise())
