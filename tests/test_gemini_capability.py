import pytest
import httpx
from pydantic import ValidationError

from worker.app import AgentExecuteBody, BrowserRuntime, WorkerError, create_app
from worker.site_capabilities import GEMINI_AUTH_STATUS, is_gemini_submit


class Adapter:
    async def url(self): return "https://gemini.google.com/app"


class Mouse:
    def __init__(self): self.calls = 0
    async def click(self, *args, **kwargs): self.calls += 1


class Keyboard:
    def __init__(self): self.calls = 0
    async def press(self, key): self.calls += 1


class Page:
    def __init__(self): self.mouse, self.keyboard, self.allowed = Mouse(), Keyboard(), True
    async def evaluate(self, expression, arg=None):
        if expression == GEMINI_AUTH_STATUS:
            return {'status': 'signed_in', 'reason_code': 'ACCOUNT_CONTROLS_PRESENT'}
        if 'location.origin' in expression: return self.allowed
        return {'tag': 'button', 'role': '', 'type': ''}


def body(action_id, epoch, observation, *, grant="grant_1", action=None):
    return AgentExecuteBody(action_id=action_id, control_epoch=epoch, observation_id=observation,
        allowed_origins=['https://gemini.google.com'],
        action=action or {'kind': 'press_key', 'key': 'Enter'},
        site_capability={'kind': 'gemini_prompt_submit', 'grant_id': grant})


@pytest.mark.asyncio
async def test_capability_one_use_persists_and_action_replays(tmp_path):
    page = Page()
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1, page=page, adapter=Adapter(), test_mode=True)
    gate = runtime.gate('agent'); runtime._last_observation_id = 'obs'
    first = body('first', gate['control_epoch'], 'obs')
    result = await runtime.execute_agent(first)
    assert result['outcome'] == 'applied' and page.keyboard.calls == 1
    assert await runtime.execute_agent(first) == result and page.keyboard.calls == 1
    with pytest.raises(WorkerError, match='different arguments'):
        await runtime.execute_agent(body('first', gate['control_epoch'], 'obs', grant='other'))
    await runtime.close()
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1, page=page, adapter=Adapter(), test_mode=True)
    gate = runtime.gate('agent'); runtime._last_observation_id = 'obs2'
    with pytest.raises(WorkerError) as used:
        await runtime.execute_agent(body('second', gate['control_epoch'], 'obs2'))
    assert used.value.code == 'CAPABILITY_ALREADY_USED' and page.keyboard.calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_rejected_or_stale_does_not_consume_grant(tmp_path):
    page = Page(); page.allowed = False
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1, page=page, adapter=Adapter(), test_mode=True)
    gate = runtime.gate('agent'); runtime._last_observation_id = 'obs'
    with pytest.raises(WorkerError) as rejected:
        await runtime.execute_agent(body('one', gate['control_epoch'], 'obs'))
    assert rejected.value.code == 'APPROVAL_REQUIRED'
    assert runtime._db.execute('select count(*) from capability_uses').fetchone()[0] == 0
    runtime._last_observation_id = 'next'
    with pytest.raises(WorkerError) as stale:
        await runtime.execute_agent(body('two', gate['control_epoch'], 'stale'))
    assert stale.value.code == 'STALE_OBSERVATION'
    await runtime.close()


@pytest.mark.asyncio
async def test_unknown_outcome_cannot_be_retried(tmp_path):
    page = Page()
    async def interrupted(_key): raise RuntimeError('synthetic input interruption')
    page.keyboard.press = interrupted
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1, page=page, adapter=Adapter(), test_mode=True)
    gate = runtime.gate('agent'); runtime._last_observation_id = 'obs'
    request = body('uncertain', gate['control_epoch'], 'obs')
    result = await runtime.execute_agent(request)
    assert result['outcome'] == 'unknown'
    assert await runtime.execute_agent(request) == result
    runtime._last_observation_id = 'obs2'
    with pytest.raises(WorkerError) as used:
        await runtime.execute_agent(body('retry', gate['control_epoch'], 'obs2'))
    assert used.value.code == 'CAPABILITY_ALREADY_USED'
    await runtime.close()


def test_schema_rejects_untrusted_shapes():
    with pytest.raises(ValidationError):
        body('x', 1, 'o', grant='invalid space')
    with pytest.raises(ValidationError):
        AgentExecuteBody(action_id='x', control_epoch=1, observation_id='o', action={'kind': 'wait'},
                         site_capability={'kind': 'other', 'grant_id': 'x'})


@pytest.mark.asyncio
async def test_diagnostics_auth_and_rejects_unexpected_sensitive_data(tmp_path):
    page = Page()
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1, page=page, adapter=Adapter(), test_mode=True)
    app = create_app(runtime=runtime, token='fixture')
    async def evaluate(*args):
        return {'gemini_main_frame': True, 'input_area_count': 1, 'input_container_count': 1,
                'composer_count': 1, 'send_button_count': 1, 'active_composer': False,
                'dialog_count': 0, 'known_send_label': True, 'unexpected': 'private-data'}
    page.evaluate = evaluate
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://worker') as client:
        path = '/capabilities/gemini-prompt-submit/diagnostics'
        assert (await client.get(path)).status_code == 401
        response = await client.get(path, headers={'Authorization':'Bearer fixture'})
        assert response.status_code == 503 and 'private-data' not in response.text
    await runtime.close()


@pytest.mark.asyncio
async def test_real_dom_recognition():
    import os
    from pathlib import Path
    playwright_async = pytest.importorskip('playwright.async_api')
    chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    if not chrome.is_file(): pytest.skip('local Chrome unavailable')
    async with playwright_async.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=str(chrome), headless=True)
        try:
            page = await browser.new_page(viewport={'width': 800, 'height': 600})
            await page.route('https://gemini.google.com/**', lambda route: route.fulfill(body='''
              <div class="input-area"><rich-textarea><div class="ql-editor" contenteditable="true"></div></rich-textarea>
              <button class="send-button" aria-label="Send message">send</button></div>
              <button id="other" class="send-button" aria-label="Send message">other</button>''', content_type='text/html'))
            await page.goto('https://gemini.google.com/app')
            await page.locator('.ql-editor').focus()
            assert await is_gemini_submit(page, {'kind':'press_key','key':'Enter'})
            assert not await is_gemini_submit(page, {'kind':'press_key','key':'Shift+Enter'})
            for selector, expected in [('.input-area button', True), ('#other', False)]:
                r = await page.locator(selector).bounding_box()
                assert await is_gemini_submit(page, {'kind':'click','x':r['x']+2,'y':r['y']+2}) is expected
            await page.evaluate("document.querySelector('.input-area button').disabled=true")
            r = await page.locator('.input-area button').bounding_box()
            assert not await is_gemini_submit(page, {'kind':'click','x':r['x']+2,'y':r['y']+2})
            await page.evaluate("document.body.insertAdjacentHTML('beforeend','<dialog open>Confirm</dialog>')")
            assert not await is_gemini_submit(page, {'kind':'press_key','key':'Enter'})
        finally:
            await browser.close()
