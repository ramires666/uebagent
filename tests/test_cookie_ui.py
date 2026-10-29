"""Cookie transfer UI against a synthetic backend, never a real browser profile."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


def test_cookie_flow():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    posted = []
    attempts = []
    released = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.add_init_script("""(() => { const original = window.fetch; window.fetch = (url, options = {}) => String(url).includes('/desktop/info?') ? new Promise((resolve, reject) => { options.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))); }) : original(url, options); })();""")

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if not path.startswith('/api/v1/'):
                name = 'index.html' if path == '/' else path.lstrip('/')
                file = ui / name
                if file.is_file():
                    route.fulfill(status=200, body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
                else:
                    route.fulfill(status=404)
                return
            endpoint = path[len('/api/v1'):]
            data = {}
            if endpoint == '/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'test'}
            elif endpoint == '/desktop/state': data = {'available': True, 'control_owner': 'none' if released else 'human'}
            elif endpoint == '/chrome/sources': data = {'sources': [{'source_id': 'src', 'label': 'Chrome', 'online': True}]}
            elif endpoint.endswith('/open-sites/refresh'): data = {'request_id': 'rev'}
            elif endpoint.endswith('/open-sites'): data = {'status': 'ready', 'discovery_revision': 'rev', 'sites': [{'site_id': 'site', 'origin': 'https://gemini.example', 'tab_count': 1}]}
            elif endpoint.endswith('/cookie-catalogs'):
                attempts.append(1); data = {'catalog_id': str(len(attempts))}
            elif '/chrome/cookie-catalogs/' in endpoint:
                if endpoint.endswith('/1'): data = {'status': 'failed', 'error': {'code': 'CHROME_PERMISSION_REQUIRED', 'message': 'Access required'}}
                else: data = {'status': 'ready', 'catalog_id': '2', 'revision': 'cr', 'cookies': [{'cookie_id': 'one', 'name': 'first', 'domain': 'gemini.example', 'site_ids': ['site'], 'value': 'secret-value'}, {'cookie_id': 'two', 'name': 'second', 'domain': 'gemini.example', 'site_ids': ['site'], 'value': 'secret-value'}]}
            elif endpoint.endswith('/cookie-import-jobs'):
                posted.append(json.loads(req.post_data)); data = {'job_id': 'job'}
            elif endpoint.endswith('/cookie-import-jobs/job'): data = {'job_id': 'job', 'status': 'done' if released else 'waiting_pause', 'counts': {'selected': 1, **({'copied': 1} if released else {})}}
            elif endpoint.endswith('/release-control'): released.append(True); data = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        expect(page.locator('#toolbar-release')).to_be_visible(timeout=3000)
        page.locator('#tab-cookies').click()
        copy = page.locator('#import-cookies')
        assert copy.is_visible() and copy.is_disabled()
        page.set_viewport_size({'width': 375, 'height': 812})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.locator('#refresh-sites').click()
        page.locator('.site-row input').check()
        page.locator('#load-cookies').click()
        expect(page.locator('#catalog-message')).to_contain_text('Grant selected site access')
        assert copy.is_visible() and copy.is_disabled()
        assert page.locator('.site-row input').is_checked()
        page.locator('#load-cookies').click()
        page.locator('#catalog-panel').wait_for(state='visible')
        assert copy.is_disabled()
        assert not page.locator('.cookie-row input').nth(1).is_checked()
        page.locator('.cookie-row input').first.check()
        assert copy.is_enabled()
        copy.click()
        expect(page.locator('#import-status')).to_have_text('Ожидает паузы')
        expect(page.locator('#import-detail')).to_contain_text('Вернуть управление')
        expect(page.locator('#import-release-control')).to_be_visible()
        assert copy.is_disabled()
        page.locator('#import-release-control').click()
        page.locator('#import-status').get_by_text('Завершено').wait_for()
        assert posted[0]['selected_cookie_ids'] == ['one']
        assert len(posted) == 1 and len(released) == 1
        assert 'secret-value' not in page.locator('body').inner_text()
        browser.close()


def test_hanging_response_body_is_bounded_without_blocking_tabs():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page()
        page.add_init_script("""(() => {
          const originalTimer = window.setTimeout;
          window.setTimeout = (fn, ms, ...args) => originalTimer(fn, ms === 8000 ? 100 : ms, ...args);
          const original = window.fetch;
          window.fetch = (url, options = {}) => {
            if (!String(url).endsWith('/chrome/sources')) return original(url, options);
            const response = new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}});
            response.json = () => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))));
            return Promise.resolve(response);
          };
        })();""")
        def respond(route):
            path = route.request.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'test'} if path.endswith('/operator/session') else {}
                route.fulfill(json=data)
            else:
                file = ui / ('index.html' if path == '/' else path.lstrip('/'))
                if file.is_file(): route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
                else: route.fulfill(status=404)
        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-cookies').click()
        expect(page.locator('#sites-message')).to_contain_text('вовремя')
        page.locator('#tab-tasks').click()
        assert page.locator('#panel-tasks').is_visible()
        browser.close()


def test_task_mode_is_explicit_and_defaults_to_autonomous():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    posted = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page()
        def respond(route):
            req = route.request; path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'test'} if path.endswith('/operator/session') else {}
                if path == '/api/v1/tasks' and req.method == 'POST': posted.append(json.loads(req.post_data)); data = {'task_id': 'fixture'}
                elif path == '/api/v1/tasks': data = {'tasks': []}
                route.fulfill(json=data)
            else:
                file = ui / ('index.html' if path == '/' else path.lstrip('/'))
                if file.is_file(): route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
                else: route.fulfill(status=404)
        page.route('http://test.local/**', respond); page.goto('http://test.local/'); page.locator('#tab-tasks').click()
        for mode in ('autonomous', 'supervised'):
            page.locator('#new-task-button').click()
            assert page.locator('#task-execution-mode').input_value() == 'autonomous'
            page.locator('#task-goal').fill('fixture')
            page.locator('#task-origins').fill('https://demoqa.com')
            page.locator('#task-execution-mode').select_option(mode)
            page.locator('#task-form button[type=submit]').click()
            expect(page.locator('#task-create-card')).to_be_hidden()
        assert [x['execution_mode'] for x in posted] == ['autonomous', 'supervised']
        browser.close()
