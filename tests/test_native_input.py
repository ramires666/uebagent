"""Run in the isolated native image with xvfb-run and no network."""
import asyncio
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch


from worker.native_input import NativeInputError, NativePage


@unittest.skipUnless(os.environ.get("NATIVE_INPUT_INTEGRATION") == "1", "requires isolated X11 container")
class NativeIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_native_input(self):
        from camoufox.async_api import AsyncCamoufox
        with tempfile.TemporaryDirectory() as profile:
            async with AsyncCamoufox(headless=False, persistent_context=True, user_data_dir=profile,
                                     viewport={"width": 1440, "height": 900}, window=(1440, 1000), geoip=False) as context:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.set_content('''<style>body { margin:0; height:2200px } input { position:absolute; left:100px; top:100px; width:400px; height:40px } #drag { position:absolute; left:100px; top:200px; width:150px; height:100px; background:red }</style><canvas id="pattern" width="1440" height="900" style="position:absolute;top:0;left:0;z-index:-1"></canvas><input id="text"><div id="drag"></div><script>let c=document.querySelector('canvas').getContext('2d'); let a=c.createImageData(1440,900); let n=1; for(let i=0;i<a.data.length;i+=4){n=(1664525*n+1013904223)>>>0;a.data[i]=n&255;a.data[i+1]=(n>>>8)&255;a.data[i+2]=(n>>>16)&255;a.data[i+3]=255} c.putImageData(a,0,0); window.events=[]; for (let t of ['mousedown','mouseup','mousemove']) document.addEventListener(t, e=>events.push(t));</script>''')
                await asyncio.sleep(1)
                await page.evaluate("""() => document.addEventListener('mouseup', () => document.body.dataset.released='yes')""")
                from Xlib import display, X
                d = display.Display()
                pid_atom = d.intern_atom('_NET_WM_PID')
                candidates = []
                for window in d.screen().root.query_tree().children:
                    prop = window.get_full_property(pid_atom, X.AnyPropertyType)
                    cls = window.get_wm_class() or ()
                    if window.get_attributes().map_state == X.IsViewable and not window.get_attributes().override_redirect and prop is not None and any('camoufox' in v.lower() or 'firefox' in v.lower() for v in cls):
                        candidates.append(int(prop.value[0]))
                d.close()
                self.assertEqual(len(candidates), 1)
                browser_pid = candidates[0]
                self.assertNotEqual(browser_pid, os.getpid())
                active = True
                def epoch():
                    if not active: raise RuntimeError('stale epoch')
                native = NativePage(page, browser_pid, epoch)
                try:
                    win = native._window(); geom = win.get_geometry(); pos = native._root.translate_coords(win, 0, 0); print("GEOMETRY", geom.width, geom.height, pos.x, pos.y, await page.evaluate("() => ({x:screenX,y:screenY,ow:outerWidth,oh:outerHeight,iw:innerWidth,ih:innerHeight,dpr:devicePixelRatio})"), flush=True)
                    box = await native._prepare()
                    self.assertEqual(box[2:], (1440, 900))
                    await native.mouse.click(150, 120)
                    self.assertEqual(await page.evaluate('document.activeElement.id'), 'text')
                    await native.keyboard.insert_text('Hello, РјРёСЂ рџЊЌ')
                    await asyncio.sleep(.2)
                    self.assertEqual(await page.locator('#text').input_value(), 'Hello, РјРёСЂ рџЊЌ')
                    await native.keyboard.press('Control+a')
                    await native.keyboard.press('Backspace')
                    self.assertEqual(await page.locator('#text').input_value(), '')
                    await native.mouse.move(120, 220)
                    await native.mouse.down()
                    async with asyncio.timeout(1):
                        for step in range(1, 11):
                            await native.mouse.move(120 + 4*step, 220 + 4*step)
                    active = False
                    with self.assertRaises(RuntimeError): await native.mouse.move(180, 280)
                    self.assertFalse(native._pressed_buttons)
                    active = True
                    await native.mouse.wheel(0, 400)
                    await asyncio.sleep(.2)
                    self.assertGreater(await page.evaluate('window.scrollY'), 0)
                    self.assertEqual(await page.evaluate('document.body.dataset.released'), 'yes')
                    self.assertFalse(native._display.screen().root.query_pointer().mask & X.Button1Mask)
                    await native.mouse.move(120, 220)
                    await native.mouse.down()
                    task = asyncio.create_task(native.keyboard.press("a"))
                    await asyncio.sleep(0)
                    task.cancel()
                    try: await task
                    except asyncio.CancelledError: pass
                    self.assertFalse(native._display.screen().root.query_pointer().mask & X.Button1Mask)
                    await page.set_content('<body style="background:white"></body>')
                    with self.assertRaises(NativeInputError):
                        await native._prepare()
                finally:
                    await native.aclose()

class ReleaseTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_hung_helper_is_killed_and_outcome_unknown(self):
        class Hung:
            returncode = None
            def __init__(self): self.event = asyncio.Event(); self.killed = False
            async def wait(self):
                await self.event.wait()
                return self.returncode
            def kill(self):
                self.killed = True
                self.returncode = -9
                self.event.set()
        hung = Hung()
        async def spawn(*args, **kwargs): return hung
        native = object.__new__(NativePage)
        native._pressed_buttons, native._pressed_keys = {'left'}, set()
        native._drag_snapshot = None
        with patch('asyncio.create_subprocess_exec', spawn):
            with self.assertRaisesRegex(NativeInputError, 'outcome unknown'):
                await native._release()
        self.assertTrue(hung.killed)
        self.assertIn('left', native._pressed_buttons)

if __name__ == '__main__': unittest.main()





