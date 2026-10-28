"""Opt-in, fail-closed X11 input for a single headed browser page.

The caller must hold its existing input lock for the whole action and supply a
synchronous control-epoch check. This module is not enabled by worker.app.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path


class NativeInputError(RuntimeError):
    """Native input failed or its outcome could not be confirmed."""


def _descends_from(pid: int, expected: int) -> bool:
    seen = set()
    while pid > 1 and pid not in seen:
        if pid == expected:
            return True
        seen.add(pid)
        try:
            # The executable name in /proc/PID/stat can contain spaces and ')'.
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return pid == expected


class NativePage:
    """Read-only page access and X11 mouse/keyboard surrogates.

    expected_pid must identify the browser process or an ancestor dedicated to
    exactly one browser. check_epoch must raise whenever ownership is stale.
    Construct only inside a private X11 display, never on the host display.
    """
    def __init__(self, page, expected_pid: int, check_epoch):
        if os.name != "posix" or not os.environ.get("DISPLAY"):
            raise NativeInputError("native input requires an isolated X11 display")
        if not isinstance(expected_pid, int) or expected_pid <= 1 or not callable(check_epoch):
            raise NativeInputError("a browser process and epoch check are required")
        from Xlib import display
        import pyautogui
        self._display = display.Display()
        self._root = self._display.screen().root
        self._page, self._pid, self._check = page, expected_pid, check_epoch
        self._gui = pyautogui
        self._pressed_buttons = set()
        self._pressed_keys = set()
        self._drag_snapshot = None
        self.mouse = _Mouse(self)
        self.keyboard = _Keyboard(self)

    async def evaluate(self, *args, **kwargs):
        return await self._page.evaluate(*args, **kwargs)

    def locator(self, *args, **kwargs):
        return self._page.locator(*args, **kwargs)

    def _window(self):
        from Xlib import X
        pid_atom = self._display.intern_atom("_NET_WM_PID")
        matches = []
        for win in self._root.query_tree().children:
            try:
                attrs = win.get_attributes()
                if attrs.map_state != X.IsViewable or attrs.override_redirect:
                    continue
                prop = win.get_full_property(pid_atom, X.AnyPropertyType)
                cls = win.get_wm_class() or ()
                if prop is not None and prop.value and _descends_from(int(prop.value[0]), self._pid) and any("camoufox" in x.lower() or "firefox" in x.lower() for x in cls):
                    matches.append(win)
            except Exception:
                continue
        if len(matches) != 1:
            raise NativeInputError("expected exactly one visible owned Camoufox window")
        return matches[0]

    def _focus(self, win):
        from Xlib import X
        self._check()
        win.set_input_focus(X.RevertToParent, X.CurrentTime)
        self._display.sync()
        focused = self._display.get_input_focus().focus
        for _ in range(64):
            if getattr(focused, "id", None) == win.id:
                self._check()
                return
            try:
                focused = focused.query_tree().parent
            except Exception:
                break
        raise NativeInputError("owned browser did not receive X11 focus")

    async def _prepare(self):
        """Calibrate against rendered pixels, never spoofable screenX/outerHeight."""
        import asyncio
        import cv2
        import numpy as np
        from Xlib import X
        self._check()
        win = self._window()
        win.configure(stack_mode=X.Above)
        self._focus(win)
        geom = win.get_geometry()
        pos = self._root.translate_coords(win, 0, 0)
        screen = self._display.screen()
        sw, sh = screen.width_in_pixels, screen.height_in_pixels
        if not (0 < sw <= 4096 and 0 < sh <= 2160):
            raise NativeInputError("unsupported X11 screen dimensions")
        # A full window larger than the display is unsafe. The caller must launch
        # with a fitting window; resizing here could change page layout.
        if pos.x < 0 or pos.y < 0 or pos.x + geom.width > sw or pos.y + geom.height > sh:
            raise NativeInputError("browser window does not fit the display")
        metrics = await self._page.evaluate("() => ({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})")
        w, h, dpr = metrics["width"], metrics["height"], metrics["dpr"]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (w, h, dpr)) or dpr != 1 or not (64 <= w <= sw and 64 <= h <= sh) or int(w) != w or int(h) != h:
            raise NativeInputError("unsupported viewport dimensions or pixel scale")
        png = await asyncio.wait_for(self._page.screenshot(type="png", timeout=2000), 2.5)
        self._check()
        self._focus(win)
        shot = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if shot is None or shot.shape[:2] != (int(h), int(w)):
            raise NativeInputError("screenshot dimensions do not match viewport")
        raw = self._root.get_image(0, 0, sw, sh, X.ZPixmap, 0xffffffff)
        data = np.frombuffer(raw.data, dtype=np.uint8)
        if data.size != sw * sh * 4:
            raise NativeInputError("unsupported X11 pixel format")
        desktop = data.reshape(sh, sw, 4)[:, :, :3].copy()
        # Spread candidates across the viewport, reject plain patches, and require
        # three independently located patches to agree on a unique translation.
        candidates = []
        side = 48
        for gy in range(1, 5):
            for gx in range(1, 7):
                px = int((w - side) * gx / 7)
                py = int((h - side) * gy / 5)
                patch = shot[py:py + side, px:px + side]
                score = float(np.std(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)))
                if score >= 20:
                    candidates.append((score, px, py, patch))
        candidates.sort(key=lambda c: c[0], reverse=True)
        chosen = []
        for item in candidates:
            if all(abs(item[1] - prev[1]) >= side * 2 or abs(item[2] - prev[2]) >= side * 2 for prev in chosen):
                chosen.append(item)
            if len(chosen) == 3: break
        if len(chosen) < 3:
            raise NativeInputError("viewport lacks enough distinct image detail to calibrate")
        offsets = []
        for _, px, py, patch in chosen:
            matches = cv2.matchTemplate(desktop, patch, cv2.TM_CCOEFF_NORMED)
            _, best, _, at = cv2.minMaxLoc(matches)
            x, y = at
            matches[max(0, y - side):min(matches.shape[0], y + side + 1), max(0, x - side):min(matches.shape[1], x + side + 1)] = -1
            _, second, _, _ = cv2.minMaxLoc(matches)
            if best < 0.97 or best - second < 0.04:
                continue
            offsets.append((x - px, y - py))
        if len(offsets) < 2 or not any(offsets.count(o) >= 2 for o in offsets):
            raise NativeInputError("viewport image could not be uniquely calibrated")
        x, y = next(o for o in offsets if offsets.count(o) >= 2)
        if not (pos.x <= x and pos.y <= y and x + w <= pos.x + geom.width and y + h <= pos.y + geom.height and x + w <= sw and y + h <= sh):
            raise NativeInputError("calibrated viewport is outside visible browser window")
        self._focus(win)
        return x, y, w, h

    def _drag_box(self):
        if self._drag_snapshot is None:
            raise NativeInputError("drag geometry unavailable")
        box, wid, signature = self._drag_snapshot
        win = self._window()
        geom = win.get_geometry()
        pos = self._root.translate_coords(win, 0, 0)
        if win.id != wid or (pos.x, pos.y, geom.width, geom.height) != signature:
            raise NativeInputError("browser window changed during drag")
        self._focus(win)
        return box

    def _point(self, box, x, y):
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)) or not (0 <= x < box[2] and 0 <= y < box[3]):
            raise NativeInputError("point outside viewport")
        return round(box[0] + x), round(box[1] + y)

    async def _release(self):
        """Release tracked input in a killable, time-bounded X11 helper."""
        import asyncio
        buttons, keys = tuple(self._pressed_buttons), tuple(self._pressed_keys)
        self._drag_snapshot = None
        if not buttons and not keys:
            return
        script = r"""
import sys
from Xlib import X, XK, display
from Xlib.ext import xtest
d = display.Display()
keys = {'ctrl':'Control_L','shift':'Shift_L','alt':'Alt_L','win':'Super_L',
 'enter':'Return','esc':'Escape','up':'Up','down':'Down','left':'Left','right':'Right',
 'backspace':'BackSpace','delete':'Delete','tab':'Tab','home':'Home','end':'End',
 'pageup':'Page_Up','pagedown':'Page_Down','space':'space'}
buttons = {'left':1,'middle':2,'right':3}
for item in sys.argv[1:]:
    kind, value = item.split(':', 1)
    if kind == 'button': xtest.fake_input(d, X.ButtonRelease, buttons[value])
    else:
        code = d.keysym_to_keycode(XK.string_to_keysym(keys.get(value,value)))
        if not code: raise RuntimeError('unmapped key')
        xtest.fake_input(d, X.KeyRelease, code)
d.sync()
d.close()
"""
        args = ["button:" + b for b in buttons] + ["key:" + k for k in sorted(keys, key=lambda k: k in {"ctrl", "alt", "shift", "win"})]
        async def run():
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, *args,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), 0.4)
            except asyncio.TimeoutError as exc:
                try: proc.kill()
                except ProcessLookupError: pass
                await proc.wait()
                raise NativeInputError("input release timed out; outcome unknown") from exc
            if proc.returncode:
                raise NativeInputError("input release failed; outcome unknown")
            self._pressed_buttons.difference_update(buttons)
            self._pressed_keys.difference_update(keys)
        task = asyncio.create_task(run())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def aclose(self):
        try:
            await self._release()
        finally:
            self._display.close()


class _Mouse:
    def __init__(self, owner): self.o = owner

    async def move(self, x, y, **kwargs):
        try:
            box = self.o._drag_box() if self.o._pressed_buttons else await self.o._prepare()
            self.o._check()
            self.o._gui.moveTo(*self.o._point(box, x, y), _pause=False)
        except BaseException:
            await self.o._release()
            raise

    async def down(self, button="left", **kwargs):
        if button not in {"left", "middle", "right"}: raise NativeInputError("unsupported mouse button")
        try:
            box = await self.o._prepare()
            win = self.o._window()
            geom = win.get_geometry()
            pos = self.o._root.translate_coords(win, 0, 0)
            self.o._drag_snapshot = (box, win.id, (pos.x, pos.y, geom.width, geom.height))
            self.o._check()
            self.o._pressed_buttons.add(button)
            self.o._gui.mouseDown(button=button, _pause=False)
        except BaseException:
            await self.o._release()
            raise

    async def up(self, button="left", **kwargs):
        if button not in {"left", "middle", "right"}:
            raise NativeInputError("unsupported mouse button")
        # A down request may have reached X even when the caller was cancelled.
        self.o._pressed_buttons.add(button)
        await self.o._release()

    async def click(self, x, y, button="left", click_count=1, **kwargs):
        if click_count not in (1, 2) or button not in {"left", "middle", "right"}: raise NativeInputError("unsupported click")
        try:
            await self.move(x, y)
            for _ in range(click_count):
                await self.down(button=button)
                self.o._check()
                await self.up(button=button)
        finally:
            await self.o._release()

    async def wheel(self, delta_x, delta_y):
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 2000 for v in (delta_x, delta_y)):
            raise NativeInputError("invalid wheel delta")
        try:
            await self.o._prepare()
            # X11 wheel events are discrete detents, not pixel-accurate deltas.
            for value, fn in ((delta_x, self.o._gui.hscroll), (delta_y, self.o._gui.scroll)):
                steps = math.ceil(abs(value) / 100) if value else 0
                for _ in range(steps):
                    self.o._check()
                    fn(-1 if value > 0 else 1, _pause=False)
        except BaseException:
            await self.o._release()
            raise


class _Keyboard:
    def __init__(self, owner): self.o = owner

    async def press(self, key, **kwargs):
        aliases = {"Control": "ctrl", "Meta": "win", "Alt": "alt", "Shift": "shift", "Enter": "enter", "Escape": "esc", "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right", "Backspace": "backspace", "Delete": "delete", "Tab": "tab", "Home": "home", "End": "end", "PageUp": "pageup", "PageDown": "pagedown", "Space": "space"}
        parts = [aliases.get(p, p.lower()) for p in key.split("+")]
        if not parts or any(p not in self.o._gui.KEYBOARD_KEYS for p in parts): raise NativeInputError("unsupported key")
        try:
            await self.o._prepare()
            for part in parts:
                self.o._check()
                self.o._pressed_keys.add(part)
                self.o._gui.keyDown(part, _pause=False)
        finally:
            await self.o._release()

    async def insert_text(self, text):
        import asyncio
        if not isinstance(text, str) or len(text) > 10000 or "\x00" in text:
            raise NativeInputError("invalid text")
        # This helper owns only the isolated X11 CLIPBOARD. It acknowledges only
        # a request for actual text, never a TARGETS metadata request.
        script = r"""
import select, sys, time
from Xlib import X, Xatom, display, protocol
data = sys.stdin.buffer.read()
d = display.Display()
w = d.screen().root.create_window(0,0,1,1,0,0,X.InputOnly, X.CopyFromParent)
clip = d.intern_atom('CLIPBOARD')
utf8 = d.intern_atom('UTF8_STRING')
targets = d.intern_atom('TARGETS')
text = d.intern_atom('text/plain;charset=utf-8')
w.set_selection_owner(clip, X.CurrentTime)
d.sync()
if getattr(d.get_selection_owner(clip),'id',0) != w.id: sys.exit(2)
sys.stdout.buffer.write(b'R'); sys.stdout.buffer.flush()
end = time.monotonic() + 4
while time.monotonic() < end:
    if not d.pending_events():
        select.select([d.fileno()], [], [], 0.1)
        continue
    e = d.next_event()
    if e.type == X.SelectionClear: sys.exit(3)
    if e.type != X.SelectionRequest: continue
    prop = e.property or e.target
    if e.target == targets:
        e.requestor.change_property(prop, Xatom.ATOM, 32, [targets, utf8, text, Xatom.STRING])
    elif e.target in (utf8, text, Xatom.STRING):
        e.requestor.change_property(prop, e.target, 8, data)
    else: prop = X.NONE
    e.requestor.send_event(protocol.event.SelectionNotify(time=e.time, requestor=e.requestor,
        selection=e.selection, target=e.target, property=prop), propagate=False)
    d.sync()
    if prop and e.target in (utf8, text, Xatom.STRING):
        sys.stdout.buffer.write(b'T'); sys.stdout.buffer.flush()
        break
w.destroy()
d.close()
"""
        proc = None
        try:
            await self.o._prepare()
            self.o._check()
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            ready = await asyncio.wait_for(proc.stdout.readexactly(1), 2)
            if ready != b'R': raise NativeInputError("clipboard owner not ready")
            self.o._check()
            await self.press("Control+v")
            # Confirmation means the X server received a text SelectionRequest;
            # it is not proof that the page accepted or retained the text.
            delivered = await asyncio.wait_for(proc.stdout.readexactly(1), 2)
            if delivered != b'T': raise NativeInputError("clipboard request not confirmed; outcome unknown")
            self.o._check()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            raise NativeInputError("clipboard transfer unconfirmed; outcome unknown") from exc
        finally:
            try:
                await self.o._release()
            finally:
                if proc is not None and proc.returncode is None:
                    try: proc.kill()
                    except ProcessLookupError: pass
                    try:
                        await asyncio.wait_for(proc.wait(), 0.5)
                    except asyncio.TimeoutError as exc:
                        raise NativeInputError("clipboard owner did not exit; retire isolated display") from exc
