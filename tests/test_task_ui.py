"""Task history and repeat form using a synthetic UI backend."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


def test_task_spec_is_visible_and_repeat_requires_explicit_submit():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    original = {
        'task_id': 'old-task', 'status': 'failed', 'version': 4,
        'error': {'code': 'NEW_SAFE_CODE', 'message': 'Refresh cannot continue <img src=x onerror=alert(1)> token=secret'},
        'spec': {
            'profile_id': 'research',
            'goal': 'Сделай точно для Алексей Ильин\n«Кавычки и ё»',
            'goal_language': 'ru',
            'verbatim_text': 'Алексей Ильин\n«Кавычки и ё»',
            'allowed_origins': ['https://example.test', 'https://shop.example.test'],
            'user_inputs': {'first_name': 'Алексей', 'password': 'private-password-value', 'nested': {'token': 'private-nested-token', 'region': 'north'}},
            'secret_refs': ['vault://demo/customer-token'],
            'success_conditions': [{'kind': 'text_present', 'text': 'Заказ готов'}],
            'policy_grant_id': 'grant-demo',
            'execution_mode': 'supervised',
            'site_capabilities': [],
            'limits': {'timeout_s': 321, 'max_steps': 17},
            'idempotency_key': 'old-idempotency-key',
        },
    }
    refresh_failure = {
        'task_id': 'refresh-task', 'status': 'waiting_human', 'version': 1,
        'error': {'code': 'CAPABILITY_UNAVAILABLE', 'message': 'Gemini page is not in a safe state to refresh'},
        'spec': {'profile_id': 'default', 'goal': 'Refresh the session', 'execution_mode': 'autonomous'},
    }
    posted = []
    attempts = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/status': data = {'profiles': [{'profile_id': 'default', 'label': 'Default'}]}
                elif path == '/api/v1/tasks' and req.method == 'POST':
                    body = json.loads(req.post_data); attempts.append(body)
                    if len(attempts) == 1:
                        route.fulfill(status=503, json={'error': {'message': 'temporary'}})
                        return
                    posted.append(body); data = {'task_id': 'new-task'}
                elif path == '/api/v1/tasks': data = {'tasks': [original, refresh_failure]}
                elif path == '/api/v1/desktop/info': data = {}
                else: data = {}
                route.fulfill(json=data)
                return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-tasks').click()
        card = page.locator('.task-row').filter(has_text='old-task')
        expect(card.locator('.task-stop-reason')).to_be_visible()
        expect(card.locator('.task-stop-reason')).to_contain_text('Причина остановки')
        expect(card.locator('.task-stop-reason')).to_contain_text('Refresh cannot continue')
        expect(card.locator('.task-stop-reason')).to_contain_text('NEW_SAFE_CODE')
        assert card.locator('.task-stop-reason img').count() == 0
        expect(card.locator('.task-stop-reason')).not_to_contain_text('token=secret')
        refresh_card = page.locator('.task-row').filter(has_text='refresh-task')
        expect(refresh_card.locator('.task-stop-reason')).to_be_visible()
        expect(refresh_card.locator('.task-stop-reason')).to_contain_text('Проверка безопасности страницы Gemini')
        expect(refresh_card.locator('.task-stop-reason')).to_contain_text('Gemini page is not in a safe state to refresh')
        expect(card).to_contain_text(original['spec']['goal'])
        for value in ['timeout_s', '321', 'max_steps', '17', 'policy_grant_id', 'grant-demo', 'vault://demo/customer-token', 'first_name', 'Алексей', 'Дословный ввод', 'Да']:
            expect(card.locator('.task-spec')).to_contain_text(value)
        expect(card.locator('.task-spec')).not_to_contain_text('private-password-value')
        expect(card.locator('.task-spec')).not_to_contain_text('private-nested-token')
        assert page.locator('.task-spec img').count() == 0

        card.get_by_role('button', name='Повторить…').click()
        expect(page.locator('#task-create-card')).to_be_visible()
        expect(page.locator('#task-sync-cookies')).to_be_checked()
        assert posted == []  # Opening repeat is an edit only.
        assert page.locator('#task-goal').input_value() == original['spec']['goal']
        assert page.locator('#task-verbatim-text').input_value() == original['spec']['verbatim_text']
        assert page.locator('#task-profile').input_value() == 'research'
        assert page.locator('#task-origins').input_value() == ', '.join(original['spec']['allowed_origins'])
        options = json.loads(page.locator('#task-advanced-options').input_value())
        assert options['limits'] == original['spec']['limits']
        assert options['success_conditions'] == original['spec']['success_conditions']
        assert options['user_inputs']['password'] == '[скрыто]'
        assert options['user_inputs']['nested']['token'] == '[скрыто]'
        assert options['user_inputs']['nested']['region'] == 'north'

        page.locator('#task-goal').fill('  Выполнить для Алексей Ильин\n«Оставить как есть»  ')
        page.locator('#task-verbatim-text').fill('  Алексей Ильин\n«Точно так»  ')
        options['limits']['max_steps'] = 19
        options['user_inputs']['nested']['region'] = 'south'
        page.locator('#task-advanced-options').fill(json.dumps(options, ensure_ascii=False))
        page.locator('#task-form-submit').click()
        expect(page.locator('#task-notice')).to_contain_text('temporary')
        page.locator('#task-form-submit').click()
        expect(page.locator('#task-create-card')).to_be_hidden()
        assert len(attempts) == 2
        assert attempts[0]['idempotency_key'] == attempts[1]['idempotency_key']
        assert len(posted) == 1
        repeated = posted[0]
        assert repeated['goal'] == '  Выполнить для Алексей Ильин\n«Оставить как есть»  '
        assert repeated['verbatim_text'] == '  Алексей Ильин\n«Точно так»  '
        assert repeated['profile_id'] == 'research'
        assert repeated['allowed_origins'] == original['spec']['allowed_origins']
        assert repeated['execution_mode'] == 'supervised'
        assert repeated['sync_cookies'] is True
        assert repeated['limits'] == {'timeout_s': 321, 'max_steps': 19}
        assert repeated['user_inputs']['password'] == 'private-password-value'
        assert repeated['user_inputs']['nested'] == {'token': 'private-nested-token', 'region': 'south'}
        assert repeated['secret_refs'] == original['spec']['secret_refs']
        assert repeated['idempotency_key'] != original['spec']['idempotency_key']
        assert original['spec']['goal'] == 'Сделай точно для Алексей Ильин\n«Кавычки и ё»'

        card = page.locator('.task-row').filter(has_text='old-task')
        card.get_by_role('button', name='Повторить…').click()
        options = json.loads(page.locator('#task-advanced-options').input_value())
        options['user_inputs']['password'] = 'rotated-password'
        del options['user_inputs']['nested']['token']
        page.locator('#task-advanced-options').fill(json.dumps(options, ensure_ascii=False))
        page.locator('#task-form-submit').click()
        expect(page.locator('#task-create-card')).to_be_hidden()
        assert len(posted) == 2
        assert posted[1]['user_inputs']['password'] == 'rotated-password'
        assert posted[1]['user_inputs']['nested'] == {'region': 'north'}
        browser.close()


def test_expired_task_only_restarts_after_explicit_repeat_submission():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    expired = {
        'task_id': 'expired-task', 'status': 'expired', 'version': 8,
        'spec': {
            'profile_id': 'default',
            'goal': 'Сделай точно для Алексей Ильин\n«Кавычки»',
            'goal_language': 'ru',
            'verbatim_text': 'Алексей Ильин\n«Кавычки»',
            'allowed_origins': ['https://example.test'],
            'user_inputs': {'given_name': 'Алексей'},
            'secret_refs': [],
            'success_conditions': [{'kind': 'text_present', 'text': 'Готово'}],
            'policy_grant_id': None,
            'execution_mode': 'supervised',
            'site_capabilities': [],
            'limits': {'timeout_s': 30, 'max_steps': 2},
            'idempotency_key': 'expired-idempotency-key',
        },
    }
    posted = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/status': data = {'profiles': [{'profile_id': 'default', 'label': 'Default'}]}
                elif path == '/api/v1/tasks' and req.method == 'POST':
                    posted.append(json.loads(req.post_data)); data = {'task_id': 'repeated-task'}
                elif path == '/api/v1/tasks': data = {'tasks': [expired]}
                else: data = {}
                route.fulfill(json=data)
                return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-tasks').click()
        card = page.locator('.task-row').filter(has_text='expired-task')
        expect(card).to_contain_text('Срок задачи истёк')
        expect(card.get_by_role('button', name='Продолжить')).to_have_count(0)
        card.get_by_role('button', name='Повторить…').click()
        expect(page.locator('#task-create-card')).to_be_visible()
        assert posted == []
        assert page.locator('#task-goal').input_value() == expired['spec']['goal']
        assert page.locator('#task-verbatim-text').input_value() == expired['spec']['verbatim_text']
        options = json.loads(page.locator('#task-advanced-options').input_value())
        assert options['limits'] == expired['spec']['limits']
        assert options['success_conditions'] == expired['spec']['success_conditions']

        page.locator('#task-form-submit').click()
        expect(page.locator('#task-create-card')).to_be_hidden()
        assert len(posted) == 1
        assert posted[0]['goal'] == expired['spec']['goal']
        assert posted[0]['verbatim_text'] == expired['spec']['verbatim_text']
        assert posted[0]['limits'] == expired['spec']['limits']
        assert posted[0]['idempotency_key'] != expired['spec']['idempotency_key']
        browser.close()


def test_compact_browser_operator_reuses_frame_and_controls_current_task():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    task = {'task_id': 'live-task', 'version': 3, 'status': 'running', 'phase': 'checking_dependencies', 'step_count': 2, 'spec': {'goal': 'Inspect the current page safely', 'execution_mode': 'autonomous'}}
    calls = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            calls.append((req.method, path, json.loads(req.post_data) if req.post_data else None))
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/status': data = {'profiles': [{'profile_id': 'default', 'label': 'Default'}]}
                elif path == '/api/v1/tasks': data = {'tasks': [task]}
                elif path == '/api/v1/tasks/live-task': data = {'task': task}
                elif path == '/api/v1/desktop/info': data = {'available': True, 'browser_started': True, 'url': '/desktop'}
                elif path == '/api/v1/desktop/state': data = {'available': True, 'control_owner': 'agent'}
                elif path == '/api/v1/tasks/live-task/pause': data = {'status': 'pause_requested'}
                else: data = {}
                route.fulfill(json=data); return
            if path == '/desktop': route.fulfill(body=(ui / 'desktop.html').read_bytes(), content_type='text/html; charset=utf-8'); return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file(): route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#toolbar-open-browser').click()
        frame = page.locator('#desktop-frame-wrap iframe')
        expect(frame).to_have_count(1)
        expect(page.locator('#desktop-task-status')).to_contain_text('Выполняется')
        expect(page.locator('#desktop-task-goal')).to_have_text(task['spec']['goal'])
        expect(page.locator('#desktop-task-phase')).to_contain_text('Шагов: 2')
        expect(page.locator('#desktop-task-phase')).not_to_contain_text('checking_dependencies')
        page.evaluate("window.originalBrowserFrame = document.querySelector('#desktop-frame-wrap iframe')")
        page.locator('#toolbar-open-browser').click()
        expect(page.locator('#desktop-frame-wrap iframe')).to_have_count(1)
        assert page.evaluate("window.originalBrowserFrame === document.querySelector('#desktop-frame-wrap iframe')")
        page.locator('#desktop-task-controls').get_by_role('button', name='Пауза').click()
        expect(page.locator('#toast')).to_contain_text('отправлена')
        pause = [call for call in calls if call[0] == 'POST' and call[1] == '/api/v1/tasks/live-task/pause']
        assert len(pause) == 1 and pause[0][2] == {'expected_version': 3}
        assert not [call for call in calls if call[0] == 'POST' and call[1] == '/api/v1/tasks']
        browser.close()


def test_resume_expired_waiting_human_refreshes_card_without_creating_repeat():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    waiting = {
        'task_id': 'old-waiting-task', 'status': 'waiting_human', 'phase': 'waiting_human', 'version': 5,
        'spec': {
            'profile_id': 'default',
            'goal': 'Повторить дословно: Алексей Ильин\n«Текст»',
            'goal_language': 'ru',
            'verbatim_text': 'Алексей Ильин\n«Текст»',
            'allowed_origins': ['https://example.test'],
            'user_inputs': {}, 'secret_refs': [],
            'success_conditions': [{'kind': 'text_present', 'text': 'Готово'}],
            'policy_grant_id': None, 'execution_mode': 'supervised', 'site_capabilities': [],
            'limits': {'timeout_s': 30, 'max_steps': 2},
            'idempotency_key': 'old-waiting-idempotency',
        },
    }
    expired = {**waiting, 'status': 'expired', 'phase': None, 'version': 6}
    commands = []
    new_tasks = []
    listing = {'waiting': True}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/status': data = {'profiles': [{'profile_id': 'default', 'label': 'Default'}]}
                elif path == '/api/v1/tasks' and req.method == 'POST':
                    new_tasks.append(json.loads(req.post_data)); data = {'task_id': 'unexpected-new-task'}
                elif path == '/api/v1/tasks': data = {'tasks': [waiting if listing['waiting'] else expired]}
                elif path == '/api/v1/tasks/old-waiting-task' and req.method == 'GET': data = {'task': waiting}
                elif path == '/api/v1/tasks/old-waiting-task/resume':
                    commands.append(path)
                    listing['waiting'] = False
                    route.fulfill(status=409, json={'error': {'code': 'TASK_EXPIRED', 'message': 'Task deadline passed'}})
                    return
                else: data = {}
                route.fulfill(json=data)
                return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-tasks').click()
        card = page.locator('.task-row').filter(has_text='old-waiting-task')
        expect(card.get_by_role('button', name='Продолжить')).to_be_visible()
        card.get_by_role('button', name='Продолжить').click()
        expect(page.locator('#toast')).to_contain_text('Срок задачи истёк')
        card = page.locator('.task-row').filter(has_text='old-waiting-task')
        expect(card).to_contain_text('Срок задачи истёк')
        expect(card.get_by_role('button', name='Продолжить')).to_have_count(0)
        assert commands == ['/api/v1/tasks/old-waiting-task/resume']
        assert new_tasks == []

        card.get_by_role('button', name='Повторить…').click()
        expect(page.locator('#task-create-card')).to_be_visible()
        assert page.locator('#task-goal').input_value() == waiting['spec']['goal']
        assert page.locator('#task-verbatim-text').input_value() == waiting['spec']['verbatim_text']
        assert json.loads(page.locator('#task-advanced-options').input_value())['limits'] == waiting['spec']['limits']
        assert new_tasks == []
        browser.close()



def test_gemini_fast_workflow_sends_exact_prompt_and_repeat_is_explicit():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    attempts = []
    current_tasks = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/status': data = {'profiles': [{'profile_id': 'default', 'label': 'Default'}]}
                elif path == '/api/v1/tasks' and req.method == 'POST':
                    body = json.loads(req.post_data); attempts.append(body)
                    if len(attempts) == 1:
                        route.fulfill(status=503, json={'error': {'message': 'temporary'}})
                        return
                    record = {'task_id': f'fast-{len(attempts)}', 'status': 'succeeded', 'phase': 'submitted', 'version': 1, 'spec': body, 'goal': body['goal']}
                    current_tasks[:] = [record]
                    route.fulfill(json={'task_id': record['task_id']})
                    return
                elif path == '/api/v1/tasks': data = {'tasks': current_tasks}
                elif path.endswith('/events'): data = {'events': []}
                else: data = {}
                route.fulfill(json=data)
                return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-tasks').click()
        page.locator('#new-task-button').click()
        prompt = '  \u041f\u0440\u0438\u0432\u0435\u0442, Gemini!\nKeep   spaces.  \n'
        page.locator('#task-goal').fill('generic browser goal')
        page.locator('#task-origins').fill('https://example.test')
        page.locator('#task-verbatim-text').fill('generic exact value')
        page.locator('#task-execution-mode').select_option('supervised')
        assert page.locator('#task-workflow option[value=gemini_prompt_submit]').inner_text() == '\u0411\u044b\u0441\u0442\u0440\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u043f\u0440\u043e\u0441 \u0432 Gemini'
        page.locator('#task-workflow').select_option('gemini_prompt_submit')
        expect(page.locator('#task-gemini-prompt-field')).to_be_visible()
        expect(page.locator('#task-goal-field')).to_be_hidden()
        page.locator('#task-gemini-prompt').fill(prompt)
        page.locator('#task-workflow').select_option('agent')
        assert page.locator('#task-goal').input_value() == 'generic browser goal'
        assert page.locator('#task-origins').input_value() == 'https://example.test'
        assert page.locator('#task-verbatim-text').input_value() == 'generic exact value'
        assert page.locator('#task-execution-mode').input_value() == 'supervised'
        page.locator('#task-workflow').select_option('gemini_prompt_submit')

        page.locator('#task-gemini-prompt').fill('   ')
        page.locator('#task-form-submit').click()
        assert attempts == []
        page.locator('#task-gemini-prompt').fill(prompt)
        page.locator('#task-form-submit').click()
        expect(page.locator('#task-notice')).to_contain_text('temporary')
        page.locator('#task-form-submit').click()
        expect(page.locator('#task-create-card')).to_be_hidden()
        assert len(attempts) == 2
        first = attempts[0]
        assert attempts[1]['idempotency_key'] == first['idempotency_key']
        assert first['workflow'] == 'gemini_prompt_submit'
        assert first['sync_cookies'] is True
        assert first['verbatim_text'] == prompt
        assert first['goal'] == 'Submit the exact supplied prompt to Gemini once.'
        assert first['execution_mode'] == 'autonomous'
        assert first['allowed_origins'] == ['https://gemini.google.com']
        assert first['site_capabilities'] == ['gemini_prompt_submit']
        assert first['success_conditions'] == []
        row_text = page.locator('.task-row').inner_text()
        assert 'workflow' in row_text
        assert '\u041e\u0434\u043d\u043e\u0440\u0430\u0437\u043e\u0432\u0430\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u0437\u0430\u043f\u0440\u043e\u0441\u0430 \u0432 Gemini' in row_text
        assert '\ufffd' not in row_text

        card = page.locator('.task-row').filter(has_text='fast-2')
        card.locator('.task-controls button').first.click()
        expect(page.locator('#task-create-card')).to_be_visible()
        assert len(attempts) == 2  # Repeat only opens the exact spec for editing.
        assert page.locator('#task-workflow').input_value() == 'gemini_prompt_submit'
        assert page.locator('#task-gemini-prompt').input_value() == first['verbatim_text']
        assert not page.locator('#task-workflow').is_disabled()
        page.locator('#task-workflow').select_option('agent')
        assert page.locator('#task-goal').input_value() == first['goal']
        assert page.locator('#task-verbatim-text').input_value() == first['verbatim_text']
        assert page.locator('#task-origins').input_value() == 'https://gemini.google.com'
        assert page.locator('#task-execution-mode').input_value() == 'autonomous'
        page.locator('#task-workflow').select_option('gemini_prompt_submit')
        assert page.locator('#task-gemini-prompt').input_value() == first['verbatim_text']
        page.locator('#task-gemini-prompt').fill('New exact line\n  ??????')
        page.locator('#task-form-submit').click()
        expect(page.locator('#task-create-card')).to_be_hidden()
        assert len(attempts) == 3
        assert attempts[2]['verbatim_text'] == 'New exact line\n  ??????'
        assert attempts[2]['goal'] == first['goal']
        assert attempts[2]['idempotency_key'] != first['idempotency_key']
        assert attempts[2]['allowed_origins'] == ['https://gemini.google.com']
        browser.close()



def test_repeating_generic_task_can_switch_workflow_without_losing_original_options():
    ui = Path(__file__).resolve().parents[1] / 'ui'
    original = {
        'task_id': 'generic-with-private-options', 'status': 'failed', 'version': 2,
        'spec': {
            'profile_id': 'default', 'goal': 'Keep this original goal unchanged', 'goal_language': 'ru',
            'verbatim_text': 'generic exact text', 'allowed_origins': ['https://example.test'],
            'user_inputs': {'name': 'Alice', 'password': 'original-secret'},
            'secret_refs': ['vault://demo/reference'], 'success_conditions': [{'kind': 'text_present', 'text': 'done'}],
            'policy_grant_id': 'grant-original', 'execution_mode': 'supervised',
            'site_capabilities': [], 'limits': {'timeout_s': 321, 'max_steps': 17}, 'sync_cookies': False, 'idempotency_key': 'original-key-1234',
        },
    }
    posted = []
    tasks = [original]
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/status': data = {'profiles': [{'profile_id': 'default', 'label': 'Default'}]}
                elif path == '/api/v1/tasks' and req.method == 'POST':
                    body = json.loads(req.post_data); posted.append(body)
                    created = {'task_id': 'fast-conversion', 'status': 'succeeded', 'phase': 'submitted', 'version': 1, 'spec': body, 'goal': body['goal']}
                    tasks.append(created)
                    route.fulfill(json={'task_id': created['task_id']}); return
                elif path == '/api/v1/tasks': data = {'tasks': tasks}
                elif path.endswith('/events'): data = {'events': []}
                else: data = {}
                route.fulfill(json=data); return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-tasks').click()
        card = page.locator('.task-row').filter(has_text='generic-with-private-options')
        card.locator('.task-controls button').first.click()
        expect(page.locator('#task-sync-cookies')).not_to_be_checked()
        options_before = json.loads(page.locator('#task-advanced-options').input_value())
        assert options_before['user_inputs']['password'] != original['spec']['user_inputs']['password']
        page.locator('#task-workflow').select_option('gemini_prompt_submit')
        expect(page.locator('#task-sync-cookies')).not_to_be_checked()
        expect(page.locator('#task-advanced-wrap')).to_be_hidden()
        assert page.locator('#task-gemini-prompt').input_value() == ''  # Do not infer a prompt from the goal.
        assert posted == []
        page.locator('#task-gemini-prompt').fill('Exact \u03c0rompt\nsecond line')
        page.locator('#task-form-submit').click()
        assert len(posted) == 1
        fast = posted[0]
        assert fast['workflow'] == 'gemini_prompt_submit'
        assert fast['goal'] == original['spec']['goal']
        assert fast['verbatim_text'] == 'Exact \u03c0rompt\nsecond line'
        assert fast['user_inputs'] == {}
        assert fast['secret_refs'] == []
        assert fast['policy_grant_id'] is None
        assert fast['success_conditions'] == []
        assert fast['site_capabilities'] == ['gemini_prompt_submit']
        assert fast['sync_cookies'] is False
        assert original['spec']['user_inputs']['password'] == 'original-secret'

        fast_card = page.locator('.task-row').filter(has_text='fast-conversion')
        expect(fast_card.locator('.status-pill')).to_have_text('\u0417\u0430\u043f\u0440\u043e\u0441 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d')
        old_card = page.locator('.task-row').filter(has_text='generic-with-private-options')
        old_card.locator('.task-controls button').first.click()
        assert posted == [fast]
        page.locator('#task-workflow').select_option('gemini_prompt_submit')
        page.locator('#task-workflow').select_option('agent')
        expect(page.locator('#task-sync-cookies')).not_to_be_checked()
        options_after = json.loads(page.locator('#task-advanced-options').input_value())
        assert options_after['user_inputs'] == options_before['user_inputs']
        assert options_after['secret_refs'] == options_before['secret_refs']
        assert options_after['policy_grant_id'] == options_before['policy_grant_id']
        assert options_after['success_conditions'] == options_before['success_conditions']
        assert posted == [fast]
        browser.close()


@pytest.mark.parametrize(('auth_status', 'pill_text', 'detail_text'), [
    ('signed_in', 'Подтверждён', 'Сессионный вход подтверждён.'),
    ('signed_out', 'Нужно войти', 'Нужно войти в Gemini.'),
    ('unknown', 'Не подтверждён', 'Вход не подтверждён — выполнение остановлено'),
])
@pytest.mark.parametrize(('error_code', 'error_text'), [
    ('AUTH_REQUIRED', 'Нужно войти в Gemini в окне браузера'),
    ('AUTH_STATE_UNKNOWN', '\u0412\u0445\u043e\u0434 \u0432 Gemini \u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c'),
])
def test_gemini_auth_status_is_independent_of_worker_health_and_auth_errors_are_actionable(auth_status, pill_text, detail_text, error_code, error_text):
    ui = Path(__file__).resolve().parents[1] / 'ui'
    waiting_task = {
        'task_id': 'gemini-auth-task', 'status': 'waiting_human', 'phase': 'authentication_required',
        'version': 2, 'error': {'code': error_code},
        'spec': {'profile_id': 'default', 'goal': 'Send the supplied prompt', 'workflow': 'gemini_prompt_submit', 'execution_mode': 'autonomous', 'allowed_origins': ['https://gemini.google.com']},
    }
    auth_calls = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/status': data = {'model': {'configured': True, 'ready': True}, 'worker': {'configured': True, 'ready': True}, 'profiles': [{'profile_id': 'default', 'label': 'Default'}]}
                elif path == '/api/v1/capabilities/gemini-auth/status':
                    auth_calls.append(path); data = {'status': auth_status, 'reason_code': 'TEST'}
                elif path == '/api/v1/tasks': data = {'tasks': [waiting_task]}
                elif path == '/api/v1/desktop/info': data = {'available': False, 'browser_started': False}
                else: data = {}
                route.fulfill(json=data)
                return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-tasks').click()
        card = page.locator('.task-row').filter(has_text='gemini-auth-task')
        expect(card).to_contain_text(error_text)
        expect(card.get_by_role('button', name='Открыть браузер для входа')).to_be_visible()
        expect(card.get_by_role('button', name='Продолжить')).to_be_visible()
        page.locator('#new-task-button').click()
        page.locator('#task-workflow').select_option('gemini_prompt_submit')
        expect(page.locator('#task-gemini-auth-pill')).to_contain_text(pill_text)
        expect(page.locator('#task-gemini-auth-detail')).to_contain_text(detail_text)
        assert auth_calls == ['/api/v1/capabilities/gemini-auth/status']
        browser.close()


@pytest.mark.parametrize(('error_code', 'error_text'), [
    ('CHROME_PERMISSION_REQUIRED', '\u041f\u0435\u0440\u0435\u0434 \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u043e\u043c cookies \u0434\u0430\u0439\u0442\u0435 \u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u0435 Chrome'),
    ('COOKIE_SOURCE_UNAVAILABLE', '\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a cookies Chrome \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d'),
    ('COOKIE_SOURCE_AMBIGUOUS', '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0434\u043d\u043e\u0437\u043d\u0430\u0447\u043d\u043e \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a cookies Chrome'),
    ('COOKIE_SYNC_FAILED', 'Cookies \u043d\u0435 \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0435\u043d\u044b; \u0437\u0430\u0434\u0430\u0447\u0430 \u043d\u0435 \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u0430'),
    ('COOKIE_SYNC_UNSUPPORTED', '\u0427\u0430\u0441\u0442\u044c cookies \u044d\u0442\u043e\u0433\u043e \u0441\u0430\u0439\u0442\u0430 \u043f\u043e\u043a\u0430 \u043d\u0435 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0435\u0442\u0441\u044f'),
])
def test_cookie_sync_task_errors_explain_how_to_resolve(error_code, error_text):
    ui = Path(__file__).resolve().parents[1] / 'ui'
    task = {'task_id': 'cookie-sync-task', 'status': 'failed', 'version': 2, 'error': {'code': error_code}, 'spec': {'goal': 'Open the allowed site', 'allowed_origins': ['https://example.test'], 'sync_cookies': True}}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path.startswith('/api/v1/'):
                if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
                elif path == '/api/v1/tasks': data = {'tasks': [task]}
                else: data = {}
                route.fulfill(json=data)
                return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        page.locator('#tab-tasks').click()
        card = page.locator('.task-row').filter(has_text='cookie-sync-task')
        expect(card).to_contain_text(error_text)
        expect(card).to_contain_text(error_code)
        expect(card.get_by_role('button', name='Открыть браузер для входа')).to_have_count(0)
        browser.close()

@pytest.mark.parametrize(('model', 'expected'), [
    ({'configured': True, 'ready': False, 'protocol': 'generic', 'model_id': 'vision-model-7b', 'error_code': 'MODEL_VISION_UNSUPPORTED'},
     ['vision-model-7b', 'не поддерживает анализ изображений']),
    ({'configured': True, 'ready': False, 'protocol': 'generic', 'model_id': None, 'available_models': ['chat-a', 'vision-b'], 'error_code': 'MODEL_SELECTION_REQUIRED'},
     ['chat-a, vision-b', 'Требуется выбрать одну модель']),
    ({'configured': False, 'ready': False, 'protocol': 'generic', 'model_id': None, 'error_code': 'MODEL_DISCOVERY_FAILED'},
     ['/v1/models', 'Проверьте доступность сервера модели']),
    ({'configured': True, 'ready': False, 'protocol': 'generic', 'model_id': 'served-model', 'error_code': 'MODEL_UNAVAILABLE'},
     ['served-model', 'не готов принимать запросы']),
    ({'configured': True, 'ready': False, 'protocol': 'generic', 'model_id': None, 'error_code': 'MODEL_NOT_SERVED'},
     ['отсутствует в списке моделей']),
    ({'configured': False, 'ready': False, 'protocol': 'generic', 'model_id': None, 'error_code': 'MODEL_NOT_CONFIGURED'},
     ['Не задан endpoint или модель']),
    ({'configured': True, 'ready': True, 'protocol': 'generic', 'model_id': 'served-model', 'vision_status': 'accepted'},
     ['Сервер принял запрос с изображением']),
])
def test_model_status_shows_served_id_and_actionable_generic_errors(model, expected):
    ui = Path(__file__).resolve().parents[1] / 'ui'
    status = {'model': model, 'worker': {'configured': True, 'ready': True}, 'profiles': []}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Playwright Chromium unavailable: {exc}')
        page = browser.new_page(viewport={'width': 1440, 'height': 900})

        def respond(route):
            req = route.request
            path = req.url.split('test.local', 1)[-1].split('?', 1)[0]
            if path == '/api/v1/operator/session': data = {'authenticated': True, 'local_ui': True, 'csrf_token': 'fixture'}
            elif path == '/api/v1/status': data = status
            else: data = {}
            if path.startswith('/api/v1/'):
                route.fulfill(json=data)
                return
            file = ui / ('index.html' if path == '/' else path.lstrip('/'))
            if file.is_file():
                route.fulfill(body=file.read_bytes(), content_type={'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(file.suffix[1:], 'text/plain'))
            else: route.fulfill(status=404)

        page.route('http://test.local/**', respond)
        page.goto('http://test.local/')
        detail = page.locator('#model-detail')
        for text in expected:
            expect(detail).to_contain_text(text)
        expect(page.locator('#model-protocol')).to_contain_text('OpenAI API')
        expect(page.locator('#model-config')).to_contain_text(model.get('model_id') or ('Настроена' if model.get('configured') else 'Не настроена'))
        browser.close()
