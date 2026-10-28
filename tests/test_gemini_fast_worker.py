import json

import pytest

from worker.app import AgentExecuteBody, BrowserRuntime, WorkerError
from worker.site_capabilities import (
    GEMINI_ORIGIN,
    GEMINI_AUTH_STATUS,
    GEMINI_PROMPT_ACK,
    GEMINI_PROMPT_FOCUS_VERIFY,
    GEMINI_PROMPT_PREFLIGHT,
    GEMINI_PROMPT_READY,
    GEMINI_PROMPT_SEND_TARGET,
    GEMINI_PROMPT_VERIFY,
    GEMINI_SUBMIT_CHECK,
    GEMINI_SESSION_REFRESH_PREFLIGHT,
)


class Adapter:
    def __init__(self, url=f"{GEMINI_ORIGIN}/app"):
        self.current_url = url
        self.goto_calls = []

    async def url(self):
        return self.current_url

    async def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.current_url = url
        return {"url": url, "status": 200}


class Keyboard:
    def __init__(self, page):
        self.page = page
        self.insert_calls = []
        self.enter_calls = 0

    async def insert_text(self, text):
        self.insert_calls.append(text)
        self.page.draft += text
        if self.page.auth_after_insert is not None:
            self.page.auth_status = self.page.auth_after_insert

    async def press(self, key):
        assert key == "Enter"
        self.enter_calls += 1
        if self.page.fail_enter:
            raise RuntimeError("synthetic key dispatch interruption")
        if self.page.clear_on_enter:
            self.page.ack_polls_remaining = self.page.clear_after_polls


class Mouse:
    def __init__(self, page):
        self.page = page
        self.clicks = []

    async def click(self, x, y, **kwargs):
        self.clicks.append((x, y, kwargs))
        if y < 15:
            self.page.focused = True
        else:
            self.page.submit_calls += 1
            if self.page.fail_click:
                raise RuntimeError("synthetic native click interruption")
            if self.page.clear_on_click:
                self.page.ack_polls_remaining = self.page.clear_after_polls


class Page:
    def __init__(self, draft=""):
        self.draft = draft
        self.allowed = True
        self.focused = False
        self.ready = True
        self.clear_on_enter = True
        self.clear_after_polls = 0
        self.ack_polls_remaining = None
        self.ack_seen = False
        self.fail_enter = False
        self.fail_click = False
        self.clear_on_click = True
        self.submit_calls = 0
        self.auth_status = {"status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT"}
        self.auth_after_insert = None
        self.refresh_modal = False
        self.keyboard = Keyboard(self)
        self.mouse = Mouse(self)

    async def evaluate(self, expression, arg=None):
        if expression == GEMINI_AUTH_STATUS:
            return self.auth_status
        if expression == GEMINI_SESSION_REFRESH_PREFLIGHT:
            if self.refresh_modal:
                return {"safe_to_refresh": False, "reason_code": "MODAL_PRESENT"}
            if self.draft:
                return {"safe_to_refresh": False, "reason_code": "DRAFT_NOT_EMPTY"}
            return {"safe_to_refresh": True, "reason_code": "EMPTY_COMPOSER"}
        if expression == GEMINI_PROMPT_PREFLIGHT:
            if not self.allowed:
                return "blocked"
            if self.draft and self.draft != arg["text"]:
                return "draft_mismatch"
            return {"state": "same" if self.draft == arg["text"] else "empty", "x": 10, "y": 10}
        if expression == GEMINI_PROMPT_VERIFY:
            return self.allowed and self.draft == arg["text"]
        if expression == GEMINI_PROMPT_FOCUS_VERIFY:
            return self.allowed and self.focused
        if expression == GEMINI_PROMPT_READY:
            return self.allowed and self.ready and self.focused
        if expression == GEMINI_PROMPT_SEND_TARGET:
            return {"x": 10, "y": 20} if self.allowed and self.ready and self.focused else None
        if expression == GEMINI_SUBMIT_CHECK:
            return self.allowed and self.ready and self.focused
        if expression == "() => { const e = document.activeElement; return !!e && (e.isContentEditable || /^(INPUT|TEXTAREA)$/.test(e.tagName)); }":
            return self.focused
        if expression == GEMINI_PROMPT_ACK:
            if not self.allowed or self.ack_polls_remaining is None:
                return False
            if self.ack_polls_remaining > 0:
                self.ack_polls_remaining -= 1
                return False
            self.draft = ""
            self.ack_seen = True
            return True
        raise AssertionError("unexpected page script")


def request(page, action_id="fast-1", *, action=None, allow_write=True, grant="grant_1",
            origins=None, epoch=None, observation="obs"):
    return AgentExecuteBody(
        action_id=action_id,
        control_epoch=epoch,
        observation_id=observation,
        allowed_origins=origins or [GEMINI_ORIGIN],
        allow_write=allow_write,
        action=action or {"kind": "gemini_submit_prompt", "text": "Привет, Gemini — exact."},
        site_capability={"kind": "gemini_prompt_submit", "grant_id": grant},
    )


async def runtime_for(tmp_path, page=None, adapter=None):
    page = page or Page()
    adapter = adapter or Adapter()
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1, page=page,
                             adapter=adapter, test_mode=True)
    gate = runtime.gate("agent")
    runtime._last_observation_id = "obs"
    return runtime, page, gate


@pytest.mark.asyncio
async def test_fast_submit_is_idempotent_exact_and_payload_free(tmp_path):
    prompt = "Привет, Gemini — exact.\nKeep this line too."
    runtime, page, gate = await runtime_for(tmp_path)
    action = {"kind": "gemini_submit_prompt", "text": prompt}
    body = request(page, action=action, epoch=gate["control_epoch"])
    result = await runtime.execute_agent(body)
    assert result["outcome"] == "applied"
    assert result["result"]["submitted"] is True
    assert result["result"]["submission_acknowledged"] is True
    assert result["result"]["submit_dispatched"] is True
    assert result["result"]["submit_method"] == "click"
    assert result["result"]["enter_dispatched"] is False
    assert result["result"]["response_verified"] is False
    assert result["result"]["ack_kind"] == "composer_cleared"
    assert result["result"]["input_verified"] is True
    assert result["result"]["characters"] == len(prompt)
    assert set(result["result"]["timings_ms"]) == {"preflight", "insert", "verify", "submit", "ack", "total"}
    assert page.keyboard.insert_calls == [prompt]
    assert page.keyboard.enter_calls == 0 and page.submit_calls == 1
    assert prompt not in json.dumps(result, ensure_ascii=False)
    assert await runtime.execute_agent(body) == result
    assert page.keyboard.insert_calls == [prompt] and page.submit_calls == 1
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "reason", "error"), [
    ("signed_out", "SIGN_IN_CONTROL_PRESENT", "AUTH_REQUIRED"),
    ("unknown", "PROOF_INCOMPLETE", "AUTH_STATE_UNKNOWN"),
])
async def test_fast_submit_fails_closed_before_prompt_mutation(tmp_path, status, reason, error):
    prompt = "must remain untouched"
    page = Page()
    page.auth_status = {"status": status, "reason_code": reason}
    runtime, page, gate = await runtime_for(tmp_path, page)
    with pytest.raises(WorkerError) as rejected:
        await runtime.execute_agent(request(page, action={"kind": "gemini_submit_prompt", "text": prompt},
                                            epoch=gate["control_epoch"]))
    assert rejected.value.code == error
    assert page.keyboard.insert_calls == [] and page.mouse.clicks == [] and page.submit_calls == 0
    assert runtime._db.execute("select count(*) from capability_uses").fetchone()[0] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_auth_loss_after_prompt_entry_prevents_submit_click(tmp_path):
    page = Page()
    page.auth_after_insert = {"status": "unknown", "reason_code": "PROOF_INCOMPLETE"}
    runtime, page, gate = await runtime_for(tmp_path, page)
    with pytest.raises(WorkerError) as rejected:
        await runtime.execute_agent(request(page,
            action={"kind": "gemini_submit_prompt", "text": "exact draft remains"},
            epoch=gate["control_epoch"]))
    assert rejected.value.code == "AUTH_STATE_UNKNOWN"
    assert page.keyboard.insert_calls == ["exact draft remains"] and page.submit_calls == 0
    assert runtime._db.execute("select count(*) from capability_uses").fetchone()[0] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_gemini_session_refresh_is_readonly_and_preserves_current_url(tmp_path):
    adapter = Adapter(f"{GEMINI_ORIGIN}/app?keep=this")
    runtime, page, gate = await runtime_for(tmp_path, adapter=adapter)
    request = AgentExecuteBody(action_id="refresh-readonly-1", control_epoch=gate["control_epoch"],
        observation_id="obs", allowed_origins=[GEMINI_ORIGIN], allow_write=False,
        action={"kind": "gemini_refresh_session"})
    result = await runtime.execute_agent(request)
    assert result["outcome"] == "applied"
    assert result["result"] == {"origin": GEMINI_ORIGIN, "session_refreshed": True}
    assert adapter.goto_calls == [f"{GEMINI_ORIGIN}/app?keep=this"]
    assert page.keyboard.insert_calls == [] and page.keyboard.enter_calls == 0 and page.submit_calls == 0
    assert await runtime.execute_agent(request) == result and len(adapter.goto_calls) == 1
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("draft", "modal", "code"), [
    ("keep this user text", False, "DRAFT_NOT_EMPTY"),
    ("", True, "CAPABILITY_UNAVAILABLE"),
])
async def test_gemini_session_refresh_refuses_draft_or_modal(tmp_path, draft, modal, code):
    page = Page(draft=draft); page.refresh_modal = modal
    adapter = Adapter(f"{GEMINI_ORIGIN}/app")
    runtime, page, gate = await runtime_for(tmp_path, page=page, adapter=adapter)
    request = AgentExecuteBody(action_id="refresh-refused-1", control_epoch=gate["control_epoch"],
        observation_id="obs", allowed_origins=[GEMINI_ORIGIN], allow_write=False,
        action={"kind": "gemini_refresh_session"})
    with pytest.raises(WorkerError) as refused:
        await runtime.execute_agent(request)
    assert refused.value.code == code
    assert adapter.goto_calls == [] and page.draft == draft
    assert page.keyboard.insert_calls == [] and page.submit_calls == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_auth_probe_is_read_only_and_unknown_off_gemini(tmp_path):
    runtime, page, _ = await runtime_for(tmp_path)
    assert await runtime.gemini_auth_status() == {"status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT"}
    runtime._adapter.current_url = "https://example.test/"
    assert await runtime.gemini_auth_status() == {"status": "unknown", "reason_code": "NOT_GEMINI"}
    assert page.keyboard.insert_calls == [] and page.mouse.clicks == []
    await runtime.close()


@pytest.mark.asyncio
async def test_agent_gemini_writes_require_auth_but_manual_login_remains_available(tmp_path):
    page = Page()
    page.focused = True
    page.auth_status = {"status": "signed_out", "reason_code": "SIGN_IN_CONTROL_PRESENT"}
    runtime, page, gate = await runtime_for(tmp_path, page)
    with pytest.raises(WorkerError) as rejected:
        await runtime.execute_agent(request(page, action={"kind": "type_text", "text": "blocked"},
                                            epoch=gate["control_epoch"]))
    assert rejected.value.code == "AUTH_REQUIRED"
    assert page.keyboard.insert_calls == []
    human_gate = runtime.gate("human")
    result = await runtime._execute_action({"kind": "type_text", "text": "manual sign in"},
        frozenset({GEMINI_ORIGIN}), manual=True, allow_write=True,
        expected_epoch=human_gate["control_epoch"])
    assert result["characters"] == len("manual sign in")
    assert page.keyboard.insert_calls == ["manual sign in"]
    await runtime.close()


@pytest.mark.asyncio
async def test_matching_existing_draft_is_not_retyped_but_mismatch_is_refused(tmp_path):
    prompt = "exact draft"
    runtime, page, gate = await runtime_for(tmp_path, Page(draft=prompt))
    result = await runtime.execute_agent(request(page, action={"kind": "gemini_submit_prompt", "text": prompt},
                                                epoch=gate["control_epoch"]))
    assert result["outcome"] == "applied" and result["result"]["submission_acknowledged"]
    assert page.keyboard.insert_calls == [] and page.submit_calls == 1
    await runtime.close()

    runtime, page, gate = await runtime_for(tmp_path / "mismatch", Page(draft="unrelated draft"))
    body = request(page, action={"kind": "gemini_submit_prompt", "text": prompt}, epoch=gate["control_epoch"])
    with pytest.raises(WorkerError) as rejected:
        await runtime.execute_agent(body)
    assert rejected.value.code == "DRAFT_NOT_EMPTY"
    assert page.keyboard.insert_calls == [] and page.keyboard.enter_calls == 0
    assert runtime._db.execute("select count(*) from capability_uses").fetchone()[0] == 0
    assert prompt not in str(rejected.value)
    await runtime.close()


@pytest.mark.asyncio
async def test_fast_submit_requires_exact_gemini_write_capability_and_safe_dom(tmp_path):
    prompt = "send exactly"
    runtime, page, gate = await runtime_for(tmp_path)
    with pytest.raises(WorkerError) as denied:
        await runtime.execute_agent(request(page, action={"kind": "gemini_submit_prompt", "text": prompt},
                                            allow_write=False, epoch=gate["control_epoch"]))
    assert denied.value.code == "POLICY_DENIED"
    assert page.keyboard.insert_calls == [] and page.keyboard.enter_calls == 0
    runtime._last_observation_id = "obs-2"
    with pytest.raises(WorkerError) as wrong_origin_set:
        await runtime.execute_agent(request(page, action_id="wrong-origin-set",
            action={"kind": "gemini_submit_prompt", "text": prompt},
            origins=[GEMINI_ORIGIN, "https://example.test"], epoch=gate["control_epoch"], observation="obs-2"))
    assert wrong_origin_set.value.code == "POLICY_DENIED"

    runtime._last_observation_id = "obs-3"
    page.allowed = False
    with pytest.raises(WorkerError) as unsafe:
        await runtime.execute_agent(request(page, action_id="unsafe-page",
            action={"kind": "gemini_submit_prompt", "text": prompt}, epoch=gate["control_epoch"], observation="obs-3"))
    assert unsafe.value.code == "CAPABILITY_UNAVAILABLE"
    assert page.keyboard.insert_calls == [] and page.keyboard.enter_calls == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_fast_submit_rejects_wrong_origin_missing_grant_and_empty_text(tmp_path):
    prompt = "exact"
    runtime, page, gate = await runtime_for(tmp_path)
    page_adapter = Adapter("https://gemini.google.com.evil.test/app")
    runtime._adapter = page_adapter
    with pytest.raises(WorkerError) as wrong_origin:
        await runtime.execute_agent(request(page, action={"kind": "gemini_submit_prompt", "text": prompt},
                                            epoch=gate["control_epoch"]))
    assert wrong_origin.value.code == "POLICY_DENIED"
    assert page.keyboard.insert_calls == [] and page.keyboard.enter_calls == 0

    runtime._adapter = Adapter()
    runtime._last_observation_id = "obs-2"
    body = AgentExecuteBody(action_id="no-grant", control_epoch=gate["control_epoch"], observation_id="obs-2",
        allowed_origins=[GEMINI_ORIGIN], allow_write=True,
        action={"kind": "gemini_submit_prompt", "text": prompt})
    with pytest.raises(WorkerError) as missing_grant:
        await runtime.execute_agent(body)
    assert missing_grant.value.code == "POLICY_DENIED"

    with pytest.raises(WorkerError) as empty:
        runtime._validate_action({"kind": "gemini_submit_prompt", "text": ""})
    assert empty.value.code == "INVALID_ACTION"
    assert page.keyboard.insert_calls == [] and page.keyboard.enter_calls == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_submission_without_ack_is_unknown_and_never_replayed(tmp_path, monkeypatch):
    import worker.app as worker_app

    monkeypatch.setattr(worker_app, "GEMINI_PROMPT_ACK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(worker_app, "GEMINI_PROMPT_ACK_POLL_SECONDS", 0.005)
    page = Page()
    page.clear_on_click = False
    runtime, page, gate = await runtime_for(tmp_path, page)
    body = request(page, action={"kind": "gemini_submit_prompt", "text": "possibly sent"},
                   epoch=gate["control_epoch"])
    result = await runtime.execute_agent(body)
    assert result["outcome"] == "unknown" and result["error"] == "ACTION_OUTCOME_UNKNOWN"
    assert page.keyboard.enter_calls == 0 and page.submit_calls == 1
    assert "possibly sent" not in json.dumps(result)
    runtime._last_observation_id = "obs-2"
    with pytest.raises(WorkerError) as used:
        await runtime.execute_agent(request(page, action_id="second-attempt",
            action={"kind": "gemini_submit_prompt", "text": "possibly sent"},
            epoch=gate["control_epoch"], observation="obs-2"))
    assert used.value.code == "CAPABILITY_ALREADY_USED"
    assert page.keyboard.enter_calls == 0 and page.submit_calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_click_exception_after_capability_claim_is_unknown(tmp_path):
    page = Page()
    page.fail_click = True
    runtime, page, gate = await runtime_for(tmp_path, page)
    body = request(page, action={"kind": "gemini_submit_prompt", "text": "dispatch may have happened"},
                   epoch=gate["control_epoch"])
    result = await runtime.execute_agent(body)
    assert result["outcome"] == "unknown" and result["error"] == "ACTION_OUTCOME_UNKNOWN"
    assert page.keyboard.enter_calls == 0 and page.submit_calls == 1
    assert "dispatch may have happened" not in json.dumps(result)
    await runtime.close()


@pytest.mark.asyncio
async def test_post_enter_control_loss_stays_unknown_even_if_ack_was_seen(tmp_path):
    runtime, page, gate = await runtime_for(tmp_path)
    check = runtime._check_agent_gate

    def lose_control_after_ack(epoch):
        if page.ack_seen:
            raise WorkerError("AGENT_LEASE_EXPIRED", "synthetic post-submit lease loss")
        return check(epoch)

    runtime._check_agent_gate = lose_control_after_ack
    result = await runtime.execute_agent(request(page,
        action={"kind": "gemini_submit_prompt", "text": "control fence"},
        epoch=gate["control_epoch"]))
    assert result["outcome"] == "unknown" and result["error"] == "ACTION_OUTCOME_UNKNOWN"
    assert page.keyboard.enter_calls == 0 and page.submit_calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_delayed_composer_clear_ack_is_observed_without_reading_reply(tmp_path):
    page = Page()
    page.clear_after_polls = 2
    runtime, page, gate = await runtime_for(tmp_path, page)
    result = await runtime.execute_agent(request(page, action={"kind": "gemini_submit_prompt", "text": "ack me"},
                                                epoch=gate["control_epoch"]))
    assert result["outcome"] == "applied"
    assert result["result"]["ack_kind"] == "composer_cleared"
    assert result["result"]["submission_acknowledged"] is True
    assert result["result"]["timings_ms"]["ack"] > 0
    assert page.keyboard.enter_calls == 0 and page.submit_calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_real_dom_scripts_handle_quill_blank_multiline_unicode_and_delayed_button():
    from pathlib import Path

    playwright_async = pytest.importorskip("playwright.async_api")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file():
        pytest.skip("local Chrome unavailable")
    from worker.site_capabilities import (
        GEMINI_AUTH_STATUS,
        GEMINI_DIAGNOSTICS,
        GEMINI_SUBMIT_CHECK,
        GEMINI_PROMPT_ACK,
        GEMINI_PROMPT_FOCUS_VERIFY,
        GEMINI_PROMPT_PREFLIGHT,
        GEMINI_PROMPT_READY,
        GEMINI_PROMPT_VERIFY,
    )

    html = """<!doctype html><html><head><meta charset="utf-8"><style>
      .ql-editor { white-space: pre-wrap; }
      .ql-editor p { margin: 0; padding: 0; }
    </style></head><body>
      <div class="input-area"><rich-textarea>
        <div class="ql-editor" contenteditable="true"><p><br></p></div>
      </rich-textarea></div>
      <script>
        const area=document.querySelector('.input-area');
        const editor=document.querySelector('.ql-editor');
        editor.addEventListener('input',()=>{
          if(editor.textContent!=='' && !area.querySelector('button[aria-label]')){
            const b=document.createElement('button');
            b.setAttribute('aria-label','\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435'); b.textContent='send'; area.append(b);
          }
        });
        editor.addEventListener('keydown',e=>{
          if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();editor.innerHTML='<p><br></p>';}
        });
      </script>
    </body></html>"""

    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=str(chrome), headless=True)
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            await page.route("https://gemini.google.com/**", lambda route: route.fulfill(
                status=200, body=html, content_type="text/html; charset=utf-8"))
            await page.goto(f"{GEMINI_ORIGIN}/app")
            prompt = "  \u041f\u0440\u0438\u0432\u0435\u0442, Gemini\u00a0\u2014 exact.\nSecond line.  "
            await page.evaluate("() => { const img=document.createElement('img'); document.querySelector('.input-area').append(img); }")
            assert await page.evaluate(GEMINI_PROMPT_PREFLIGHT, {"text": prompt}) == "blocked"
            await page.evaluate("() => document.querySelector('.input-area img').remove()")
            preflight = await page.evaluate(GEMINI_PROMPT_PREFLIGHT, {"text": prompt})
            assert preflight["state"] == "empty"
            assert await page.evaluate(GEMINI_PROMPT_FOCUS_VERIFY) is False
            await page.mouse.click(preflight["x"], preflight["y"])
            assert await page.evaluate(GEMINI_PROMPT_FOCUS_VERIFY) is True
            assert await page.evaluate(GEMINI_PROMPT_READY) is False
            await page.keyboard.insert_text(prompt)
            assert await page.evaluate(GEMINI_PROMPT_VERIFY, {"text": prompt}) is True
            assert await page.evaluate(GEMINI_PROMPT_FOCUS_VERIFY) is True
            assert await page.evaluate(GEMINI_PROMPT_READY) is True
            diagnostic = await page.evaluate(GEMINI_DIAGNOSTICS)
            assert diagnostic["send_button_count"] == 1 and diagnostic["known_send_label"] is True
            button_info = await page.locator('.input-area button').bounding_box()
            click_args = {"kind": "click", "x": button_info["x"] + button_info["width"] / 2,
                          "y": button_info["y"] + button_info["height"] / 2}
            assert await page.evaluate(GEMINI_SUBMIT_CHECK, click_args) is True
            await page.evaluate("() => { document.querySelector('.input-area button').disabled=true; }")
            assert await page.evaluate(GEMINI_PROMPT_READY) is False
            assert await page.evaluate(GEMINI_SUBMIT_CHECK, click_args) is False
            await page.evaluate("() => { document.querySelector('.input-area button').disabled=false; }")
            await page.evaluate("() => { const b=document.querySelector('.input-area button'); const a=document.createElement('div'); a.id='foreign-area'; a.className='input-area'; a.append(b); document.body.append(a); }")
            assert await page.evaluate(GEMINI_PROMPT_READY) is False
            assert await page.evaluate(GEMINI_SUBMIT_CHECK, click_args) is False
            await page.evaluate("() => { const b=document.querySelector('#foreign-area button'); document.querySelector('#foreign-area').remove(); document.querySelector('.input-area').append(b); }")
            button_info = await page.locator('.input-area button').bounding_box()
            click_args = {"kind": "click", "x": button_info["x"] + button_info["width"] / 2,
                          "y": button_info["y"] + button_info["height"] / 2}
            await page.evaluate("() => { const b=document.createElement('button'); b.setAttribute('aria-label','Send prompt'); b.textContent='send'; document.querySelector('.input-area').append(b); }")
            assert await page.evaluate(GEMINI_PROMPT_READY) is False
            await page.evaluate("() => document.querySelectorAll('.input-area button')[1].remove()")
            assert await page.evaluate(GEMINI_PROMPT_READY) is True
            await page.keyboard.press("Enter")
            assert await page.evaluate(GEMINI_PROMPT_ACK) is True
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_real_dom_auth_probe_requires_strong_visible_chrome_proof():
    from pathlib import Path

    playwright_async = pytest.importorskip("playwright.async_api")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file():
        pytest.skip("local Chrome unavailable")
    html = """<!doctype html><html><head><meta charset="utf-8"></head><body>
      <header id="chrome"></header><main><user-query id="chat"></user-query></main>
    </body></html>"""
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=str(chrome), headless=True)
        try:
            page = await browser.new_page()
            await page.route("https://gemini.google.com/**", lambda route: route.fulfill(
                status=200, body=html, content_type="text/html; charset=utf-8"))
            await page.goto(f"{GEMINI_ORIGIN}/app")
            assert await page.evaluate(GEMINI_AUTH_STATUS) == {
                "status": "unknown", "reason_code": "PROOF_INCOMPLETE"}
            await page.evaluate("""() => {
              const a=document.createElement('a'); a.href='https://accounts.google.com/ServiceLogin';
              a.textContent='Sign in'; document.querySelector('#chrome').append(a);
            }""")
            assert await page.evaluate(GEMINI_AUTH_STATUS) == {
                "status": "signed_out", "reason_code": "SIGN_IN_CONTROL_PRESENT"}
            await page.evaluate("""() => {
              document.querySelector('#chrome').replaceChildren();
              const b=document.createElement('button');
              b.setAttribute('aria-label','Google Account: Fixture (fixture@example.test)');
              const i=document.createElement('img'); i.width=24; i.height=24;
              i.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
              b.append(i); document.querySelector('#chrome').append(b);
            }""")
            signed_in = await page.evaluate(GEMINI_AUTH_STATUS)
            assert signed_in == {"status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT"}
            assert "fixture@example.test" not in json.dumps(signed_in)
            await page.evaluate("""() => {
              const a=document.createElement('a'); a.href='https://accounts.google.com/ServiceLogin';
              a.textContent='Sign in'; document.querySelector('#chrome').append(a);
            }""")
            assert await page.evaluate(GEMINI_AUTH_STATUS) == {
                "status": "unknown", "reason_code": "PROOF_INCOMPLETE"}
            await page.evaluate("""() => {
              document.querySelector('#chrome').replaceChildren();
              const q=document.querySelector('#chat');
              const b=document.createElement('button');
              b.setAttribute('aria-label','Google Account: Fixture (fixture@example.test)');
              const i=document.createElement('img'); i.width=24; i.height=24;
              i.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
              b.append(i); q.append(b);
              const a=document.createElement('a'); a.href='https://accounts.google.com/ServiceLogin';
              a.textContent='Sign in'; q.append(a);
            }""")
            assert await page.evaluate(GEMINI_AUTH_STATUS) == {
                "status": "unknown", "reason_code": "PROOF_INCOMPLETE"}
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_real_dom_session_refresh_guard_detects_draft_and_modal():
    from pathlib import Path

    playwright_async = pytest.importorskip("playwright.async_api")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file():
        pytest.skip("local Chrome unavailable")
    html = """<!doctype html><html><head><meta charset="utf-8"></head><body>
      <div class="input-area"><rich-textarea><div class="ql-editor" contenteditable="true"><br></div></rich-textarea></div>
      <dialog id="modal">Modal</dialog>
    </body></html>"""
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=str(chrome), headless=True)
        try:
            page = await browser.new_page()
            await page.route("https://gemini.google.com/**", lambda route: route.fulfill(
                status=200, body=html, content_type="text/html; charset=utf-8"))
            await page.goto(f"{GEMINI_ORIGIN}/app?keep=this")
            assert await page.evaluate(GEMINI_SESSION_REFRESH_PREFLIGHT) == {
                "safe_to_refresh": True, "reason_code": "EMPTY_COMPOSER"}
            await page.locator(".ql-editor").evaluate("e => e.textContent='unsent draft'")
            assert await page.evaluate(GEMINI_SESSION_REFRESH_PREFLIGHT) == {
                "safe_to_refresh": False, "reason_code": "DRAFT_NOT_EMPTY"}
            await page.locator(".ql-editor").evaluate("e => e.innerHTML='<br>'")
            await page.locator("#modal").evaluate("e => e.showModal()")
            assert await page.evaluate(GEMINI_SESSION_REFRESH_PREFLIGHT) == {
                "safe_to_refresh": False, "reason_code": "MODAL_PRESENT"}
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_worker_fast_path_waits_boundedly_for_cold_composer(tmp_path):
    from pathlib import Path

    playwright_async = pytest.importorskip("playwright.async_api")
    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.is_file():
        pytest.skip("local Chrome unavailable")

    html = """<!doctype html><html><head><meta charset="utf-8"><style>
      .ql-editor { white-space: pre-wrap; }
      .ql-editor p { margin: 0; padding: 0; }
    </style></head><body><header><button aria-label="Google Account: Fixture (fixture@example.test)"><img width="24" height="24" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></button></header><main id="mount"></main><script>
      setTimeout(()=>{
        const area=document.createElement('div'); area.className='input-area';
        area.innerHTML='<rich-textarea><div class="ql-editor" contenteditable="true"><p><br></p></div></rich-textarea>';
        document.querySelector('#mount').append(area);
        const editor=area.querySelector('.ql-editor');
        editor.addEventListener('input',()=>{
          if(editor.textContent!=='' && !area.querySelector('button[aria-label]')){
            setTimeout(()=>{
              if(area.querySelector('button[aria-label]')) return;
              const b=document.createElement('button');
              b.setAttribute('aria-label','\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435'); b.textContent='send';
              b.addEventListener('click',()=>{editor.innerHTML='<p><br></p>';}); area.append(b);
            }, 200);
          }
        });
        editor.addEventListener('keydown',e=>{
          if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();editor.innerHTML='<p><br></p>';}
        });
      }, 250);
    </script></body></html>"""

    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=str(chrome), headless=True)
        runtime = None
        try:
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            await page.route("https://gemini.google.com/**", lambda route: route.fulfill(
                status=200, body=html, content_type="text/html; charset=utf-8"))
            await page.goto(f"{GEMINI_ORIGIN}/app")

            class RealAdapter:
                async def url(self):
                    return page.url

            runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1,
                                     page=page, adapter=RealAdapter(), test_mode=True)
            gate = runtime.gate("agent")
            runtime._last_observation_id = "obs"
            prompt = "  \u041f\u0440\u0438\u0432\u0435\u0442, Gemini\u00a0\u2014 exact.\nSecond line.  "
            result = await runtime.execute_agent(request(page,
                action={"kind": "gemini_submit_prompt", "text": prompt},
                epoch=gate["control_epoch"]))
            assert result["outcome"] == "applied"
            assert result["result"]["submitted"] is True
            assert result["result"]["submit_dispatched"] is True
            assert result["result"]["submit_method"] == "click"
            assert result["result"]["enter_dispatched"] is False
            assert result["result"]["submission_acknowledged"] is True
            assert result["result"]["response_verified"] is False
            assert result["result"]["characters"] == len(prompt)
            assert result["result"]["timings_ms"]["preflight"] >= 200
        finally:
            if runtime is not None:
                await runtime.close()
            await browser.close()
