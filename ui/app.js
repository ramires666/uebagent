(() => {
  'use strict';

  const API = '/api/v1';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { csrf: '', authenticated: false, status: null, tasks: [], sources: [], sourceId: '', discovery: null, pendingCatalogId: '', selectedSites: new Set(), catalog: null, cookies: [], selectedCookies: new Set(), importJob: null, activeTaskPolls: new Set(), autoPolledTasks: new Set(), desktopInfo: null, controlOwner: 'none', sitesPolling: false, catalogPolling: false, repeatSpec: null, taskSubmitAttempt: null };
  const terminalJobs = new Set(['done', 'partial', 'failed', 'expired', 'selection_stale']);
  state.catalogGeneration = 0; state.catalogTimer = null; state.importing = false;

  function setText(el, value) { if (el) el.textContent = value == null ? '' : String(value); }
  function show(el, visible = true) { if (el) el.classList.toggle('hidden', !visible); }
  function safe(value, fallback = '—') { return value == null || value === '' ? fallback : String(value); }
  function displayError(value, fallback = 'Не удалось выполнить запрос.') {
    if (value?.code === 'CHROME_COOKIE_READ_FAILED') return 'Chrome не дал прочитать cookies выбранного сайта. Проверьте разрешения расширения и загрузите каталог заново.';
    if (value && typeof value === 'object') return value.message || value.detail || value.code || fallback;
    return value || fallback;
  }
  function randomKey() { return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`; }
  function toast(message, type = '') {
    const node = $('#toast'); if (!node) return;
    node.textContent = message; node.className = `toast visible ${type}`;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => node.className = 'toast', 4200);
  }
  function errorMessage(error) { return error?.message || 'Не удалось выполнить запрос. Проверьте подключение к локальному сервису.'; }
  async function request(path, options = {}, retried = false) {
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (state.csrf && !['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase())) headers.set('X-CSRF-Token', state.csrf);
    let response;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    const abort = () => controller.abort();
    if (options.signal) { if (options.signal.aborted) abort(); else options.signal.addEventListener('abort', abort, { once: true }); }
    try {
    try { response = await fetch(`${API}${path}`, { ...options, signal: controller.signal, headers, credentials: 'same-origin', body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
    catch { throw new Error('Сервис недоступен или не ответил вовремя. Проверьте соединение и повторите запрос.'); }
    const contentType = response.headers.get('content-type') || '';
    const renewedCsrf = response.headers.get('X-CSRF-Token');
    if (renewedCsrf) state.csrf = renewedCsrf;
    let data = null;
    if (contentType.includes('application/json')) { try { data = await response.json(); } catch { if (controller.signal.aborted) throw new Error('Сервис не ответил вовремя. Повторите запрос.'); data = null; } }
    if (!response.ok && !retried && path !== '/operator/session' && ['UNAUTHORIZED', 'CSRF_INVALID'].includes(data?.error?.code)) {
      clearTimeout(timer);
      const session = await request('/operator/session');
      if (session.local_ui === true && session.csrf_token) {
        state.csrf = session.csrf_token;
        return request(path, options, true);
      }
    }
    if (!response.ok) { const error = new Error(data?.error?.message || `Ошибка запроса (${response.status})`); error.code = data?.error?.code; throw error; }
    return data || {};
    } finally { clearTimeout(timer); options.signal?.removeEventListener('abort', abort); }
  }
  const post = (path, body) => request(path, { method: 'POST', body });

  function pill(el, status, label) {
    if (!el) return;
    el.className = `status-pill ${status || 'neutral'}`;
    setText(el, label);
  }
  let geminiAuthState = null;
  let geminiAuthRequest = null;
  function renderGeminiAuthStatus() {
    const data = geminiAuthState || { status: 'unknown' };
    const states = {
      signed_in: ['good', 'Подтверждён', 'Сессионный вход подтверждён.'],
      signed_out: ['warning', 'Нужно войти', 'Нужно войти в Gemini. Задача остановится до входа.'],
      unknown: ['pending', 'Не подтверждён', 'Вход не подтверждён — выполнение остановлено до проверки состояния.'],
    };
    const [kind, label, detail] = states[data.status] || states.unknown;
    pill($('#task-gemini-auth-pill'), kind, label);
    setText($('#task-gemini-auth-detail'), detail);
  }
  async function loadGeminiAuthStatus(force = false) {
    if (!state.authenticated || (!force && geminiAuthState)) return;
    if (geminiAuthRequest) return geminiAuthRequest;
    geminiAuthState = { status: 'unknown' };
    renderGeminiAuthStatus();
    geminiAuthRequest = request('/capabilities/gemini-auth/status')
      .then(data => { geminiAuthState = ['signed_in', 'signed_out', 'unknown'].includes(data.status) ? data : { status: 'unknown' }; })
      .catch(() => { geminiAuthState = { status: 'unknown' }; })
      .finally(() => { geminiAuthRequest = null; renderGeminiAuthStatus(); });
    return geminiAuthRequest;
  }
  function componentState(component) {
    if (!component?.configured) return ['neutral', 'Не настроено'];
    if (component.ready) return ['good', 'Готово'];
    return ['warning', 'Ожидание'];
  }
  function modelStatusText(model) {
    const id = typeof model.model_id === 'string' ? model.model_id.slice(0, 160) : '';
    const code = typeof model.error_code === 'string' ? model.error_code : '';
    const message = {
      MODEL_VISION_UNSUPPORTED: `Модель ${id || '(не выбрана)'} не поддерживает анализ изображений. Выберите модель с поддержкой vision.`,
      MODEL_SELECTION_REQUIRED: `Сервер предлагает несколько моделей${Array.isArray(model.available_models) && model.available_models.length ? `: ${model.available_models.slice(0, 8).map(value => String(value).slice(0, 80)).join(', ')}` : ''}. Требуется выбрать одну модель.`,
      MODEL_DISCOVERY_FAILED: 'Не удалось получить список моделей с endpoint /v1/models. Проверьте доступность сервера модели.',
      MODEL_NOT_SERVED: `Настроенная модель${id ? ` «${id}»` : ''} отсутствует в списке моделей, опубликованном сервером.`,
      MODEL_UNAVAILABLE: `Сервер модели${id ? ` «${id}»` : ''} не готов принимать запросы.`,
      MODEL_NOT_CONFIGURED: 'Не задан endpoint или модель для подключения.'
    };
    const fallback = typeof model.detail === 'string' ? model.detail.slice(0, 240) : (model.ready ? 'Модель готова к работе' : 'Ожидает готовности модели');
    const primary = message[code] || fallback;
    const selected = id ? `Модель: ${id}. ` : '';
    const vision = model.vision_status === 'accepted'
      ? ' Сервер принял запрос с изображением; это не подтверждает качество визуального анализа.'
      : model.vision_status === 'unsupported' || code === 'MODEL_VISION_UNSUPPORTED'
        ? ''
        : ' Поддержка изображений проверяется при первом запросе с изображением.';
    return { detail: `${selected}${primary}${vision}`.slice(0, 500), protocol: model.protocol === 'fara' ? 'Fara' : 'OpenAI API', code };
  }
  function renderStatus(data) {
    state.status = data;
    const model = data.model || {}, worker = data.worker || {};
    const [mClass, mLabel] = componentState(model), [wClass, wLabel] = componentState(worker);
    pill($('#model-pill'), mClass, mLabel); pill($('#worker-pill'), wClass, wLabel);
    pill($('#settings-model'), mClass, mLabel); pill($('#settings-worker'), wClass, wLabel);
    const modelStatus = modelStatusText(model);
    setText($('#model-detail'), modelStatus.detail);
    setText($('#settings-model-detail'), modelStatus.detail);
    setText($('#model-protocol'), modelStatus.protocol);
    setText($('#worker-detail'), safe(worker.detail, worker.ready ? 'Браузер готов к работе' : 'Ожидает запуска исполнителя'));
    setText($('#model-config'), model.model_id ? String(model.model_id).slice(0, 80) : (model.configured ? 'Настроена' : 'Не настроена'));
    setText($('#worker-config'), worker.configured ? 'Настроен' : 'Не настроен');
    const profiles = Array.isArray(data.profiles) ? data.profiles : [];
    setText($('#profile-count'), `${profiles.length} ${plural(profiles.length, 'профиль', 'профиля', 'профилей')}`);
    setText($('#profile-summary'), profiles.length ? profiles.map(p => p.label || p.profile_id).join(' · ') : 'Нет доступных профилей');
    setText($('#source-count'), `${Number(data.sources_count || 0)} подключено`);
    const proxy = data.proxy || {};
    const proxyMode = proxy.mode;
    const proxyTitle = proxyMode === 'direct' ? 'Прямой тестовый маршрут' : proxyMode === 'external' ? 'Внешний маршрут' : 'Маршрут неизвестен';
    const proxyDetail = safe(proxy.detail, proxyMode === 'direct' ? 'Трафик идёт напрямую.' : proxyMode === 'external' ? 'Используется внешний маршрут.' : 'Статус маршрута недоступен.');
    setText($('#proxy-sidebar-title'), proxyTitle);
    setText($('#proxy-sidebar-detail'), proxyDetail);
    setText($('#proxy-settings-title'), proxyTitle);
    setText($('#proxy-settings-detail'), proxyDetail);
    setText($('#proxy-settings-mode'), proxyMode === 'direct' ? 'Прямой' : proxyMode === 'external' ? 'Внешний' : 'Неизвестно');
    $('#sidebar-health')?.classList.toggle('healthy', !!(model.ready && worker.ready));
    const banner = $('#service-banner');
    const ready = model.ready && worker.ready;
    banner.className = `notice ${ready ? 'notice-good' : 'notice-warn'}`;
    setText(banner, ready ? 'Все компоненты готовы. Можно создавать задачи.' : 'Сервис запущен, но некоторые компоненты ещё не готовы. Проверьте состояние ниже.');
    setText($('#last-updated'), `Обновлено ${new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`);
    const select = $('#task-profile');
    if (select) {
      const old = select.value;
      select.replaceChildren();
      (profiles.length ? profiles : [{ profile_id: 'default', label: 'Профиль по умолчанию' }]).forEach(profile => {
        const option = document.createElement('option'); option.value = safe(profile.profile_id, 'default'); option.textContent = safe(profile.label, profile.profile_id); select.append(option);
      });
      if ([...select.options].some(o => o.value === old)) select.value = old;
    }
    const cookieSelect = $('#cookie-profile');
    if (cookieSelect) {
      const old = cookieSelect.value;
      cookieSelect.replaceChildren();
      (profiles.length ? profiles : [{ profile_id: 'default', label: 'Профиль по умолчанию' }]).forEach(profile => {
        const option = document.createElement('option'); option.value = safe(profile.profile_id, 'default'); option.textContent = safe(profile.label, profile.profile_id); cookieSelect.append(option);
      });
      if ([...cookieSelect.options].some(o => o.value === old)) cookieSelect.value = old;
    }
    const settings = $('#settings-profiles');
    if (settings) {
      settings.replaceChildren();
      if (!profiles.length) { const empty = document.createElement('span'); empty.className = 'muted'; empty.textContent = 'Нет доступных профилей'; settings.append(empty); }
      profiles.forEach(profile => {
        const row = document.createElement('div'); row.className = 'profile-row';
        const title = document.createElement('b'); title.textContent = safe(profile.label, profile.profile_id);
        const id = document.createElement('code'); id.textContent = safe(profile.profile_id);
        row.append(title, id); settings.append(row);
      });
    }
    show(banner, true);
  }
  function plural(n, one, few, many) { const x = Math.abs(n) % 100, y = x % 10; return x > 10 && x < 20 ? many : y > 1 && y < 5 ? few : y === 1 ? one : many; }

  async function loadStatus() {
    try { renderStatus(await request('/status')); }
    catch (e) { const b = $('#service-banner'); b.className = 'notice notice-error'; setText(b, errorMessage(e)); show(b, true); $('#sidebar-health')?.classList.remove('healthy'); }
  }
  async function loadTasks() {
    try {
      const data = await request('/tasks'); state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
      renderTasks();
    } catch (e) { $('#tasks-list').replaceChildren(); const err = document.createElement('div'); err.className = 'inline-error'; err.textContent = errorMessage(e); $('#tasks-list').append(err); setText($('#task-list-count'), 'Не удалось загрузить'); }
  }
  function statusLabel(status) {
    const labels = { queued: 'В очереди', running: 'Выполняется', pause_requested: 'Пауза запрошена', paused: 'Приостановлена', waiting_human: 'Нужно внимание', waiting_approval: 'Ожидает подтверждения', recovering: 'Нужно проверить состояние', succeeded: 'Завершена', done: 'Завершена', completed: 'Завершена', failed: 'Ошибка', cancelled: 'Отменена', expired: 'Истекло время', canceled: 'Отменена', needs_review: 'Требует внимания', waiting_permission: 'Ожидает разрешения', waiting_pause: 'Ожидает паузы' };
    return labels[status] || safe(status, 'Неизвестно');
  }
  function taskClass(status) { return ['succeeded', 'done', 'completed'].includes(status) ? 'good' : ['failed', 'cancelled', 'canceled', 'expired'].includes(status) ? 'bad' : ['running', 'pause_requested', 'waiting_human', 'waiting_approval', 'recovering', 'waiting', 'waiting_permission', 'waiting_pause'].includes(status) ? 'warning' : 'neutral'; }
  function taskPreflightError(error) {
    const code = typeof error === 'object' ? error?.code : error;
    if (code === 'AUTH_REQUIRED') return { code, text: 'Нужно войти в Gemini в окне браузера. После входа нажмите «Продолжить» у этой задачи.' };
    if (code === 'AUTH_STATE_UNKNOWN') return { code, text: 'Вход в Gemini не подтверждён. Проверьте состояние в окне браузера; задача остановлена до подтверждения.' };
    if (code === 'CHROME_PERMISSION_REQUIRED') return { code, text: 'Разрешите доступ к cookies сайта через значок расширения Chrome, затем повторите задачу.' };
    if (code === 'COOKIE_SOURCE_UNAVAILABLE') return { code, text: 'Подключите расширение Chrome и повторите задачу.' };
    if (code === 'COOKIE_SOURCE_AMBIGUOUS') return { code, text: 'Не удалось определить обычный профиль cookies Chrome. Оставьте открытой обычную вкладку в подключённом профиле и повторите задачу.' };
    if (code === 'COOKIE_SYNC_FAILED') return { code, text: 'Cookies не перенесены; задача не запущена.' };
    if (code === 'COOKIE_SYNC_UNSUPPORTED') return { code, text: 'Часть cookies этого сайта пока не поддерживается; задача не запущена.' };
    return null;
  }
  const MASKED_INPUT_VALUE = '[\u0441\u043a\u0440\u044b\u0442\u043e]';
  function isSensitiveInputKey(key) {
    const words = String(key).replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
    const sensitive = new Set(['password', 'passwd', 'secret', 'token', 'cookie', 'authorization', 'credential', 'auth', 'otp', 'totp', 'mfa', 'pin', 'passcode']);
    return words.some(word => sensitive.has(word))
      || ((words.includes('api') || words.includes('access') || words.includes('private')) && words.includes('key'))
      || (words.includes('session') && words.includes('id'))
      || (words.includes('verification') && words.some(word => ['code', 'token', 'otp'].includes(word)));
  }
  function safeInputPreview(value) {
    if (Array.isArray(value)) return value.map(item => safeInputPreview(item));
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, isSensitiveInputKey(key) ? MASKED_INPUT_VALUE : safeInputPreview(item)]));
  }
  function redactedSensitiveInputPaths(value, prefix = '') {
    if (Array.isArray(value)) return value.flatMap((item, index) => redactedSensitiveInputPaths(item, `${prefix}[${index}]`));
    if (!value || typeof value !== 'object') return [];
    const paths = [];
    for (const [key, item] of Object.entries(value)) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (isSensitiveInputKey(key) && item === '[REDACTED]') {
        paths.push(path);
      } else {
        paths.push(...redactedSensitiveInputPaths(item, path));
      }
    }
    return paths;
  }
  function restoreMaskedInputs(original, edited, key = '') {
    if (isSensitiveInputKey(key) && edited === MASKED_INPUT_VALUE) return original;
    if (Array.isArray(edited)) return edited.map((value, index) => restoreMaskedInputs(Array.isArray(original) ? original[index] : undefined, value));
    if (!edited || typeof edited !== 'object') return edited;
    const restored = {};
    for (const [name, value] of Object.entries(edited)) {
      restored[name] = Object.hasOwn(original || {}, name) ? restoreMaskedInputs(original[name], value, name) : value;
    }
    return restored;
  }
  function specPreviewValue(key, value) {
    if (key === 'workflow') return value === 'gemini_prompt_submit' ? '\u041e\u0434\u043d\u043e\u0440\u0430\u0437\u043e\u0432\u0430\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u0437\u0430\u043f\u0440\u043e\u0441\u0430 \u0432 Gemini' : value === 'agent' || value == null ? '\u0417\u0430\u0434\u0430\u0447\u0430 \u0432 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0435' : value;
    if (key === 'verbatim_text') return value == null || value === '' ? 'Дословный ввод: нет' : `Дословный ввод: Да · ${String(value).length} символов`;
    if (key === 'secret_refs') return Array.isArray(value) ? value.map(ref => `ссылка: ${String(ref)}`) : value;
    if (key === 'user_inputs') return safeInputPreview(value);
    return value;
  }
  function renderTaskSpec(spec) {
    const details = document.createElement('details'); details.className = 'task-spec'; details.open = true;
    const summary = document.createElement('summary'); summary.textContent = 'Параметры запуска'; details.append(summary);
    const list = document.createElement('dl'); list.className = 'task-spec-grid';
    const entries = Object.entries(spec || {});
    for (const [key, rawValue] of entries) {
      const term = document.createElement('dt'); term.textContent = key;
      const value = document.createElement('dd'); const preview = specPreviewValue(key, rawValue);
      value.textContent = preview == null ? 'null' : typeof preview === 'string' ? preview : JSON.stringify(preview);
      if (key === 'verbatim_text' && typeof rawValue === 'string' && rawValue.length) {
        const full = document.createElement('details'); full.className = 'task-verbatim-full';
        const fullSummary = document.createElement('summary'); fullSummary.textContent = rawValue.length > 160 ? `Показать точный текст (${rawValue.length} символов)` : 'Точный текст';
        const text = document.createElement('pre'); text.textContent = rawValue; full.append(fullSummary, text); value.append(full);
      }
      list.append(term, value);
    }
    if (!entries.length) { const value = document.createElement('dd'); value.textContent = 'Параметры недоступны'; list.append(value); }
    details.append(list); return details;
  }
  function specOptionsForEditor(spec) {
    const options = { ...(spec || {}) };
    for (const key of ['profile_id', 'goal', 'allowed_origins', 'execution_mode', 'idempotency_key', 'verbatim_text', 'workflow', 'sync_cookies']) delete options[key];
    if (options.user_inputs && typeof options.user_inputs === 'object' && !Array.isArray(options.user_inputs)) options.user_inputs = safeInputPreview(options.user_inputs);
    return options;
  }
  function openRepeatTask(task) {
    state.taskSubmitAttempt = null;
    state.repeatSpec = JSON.parse(JSON.stringify(task.spec || {}));
    const spec = state.repeatSpec;
    const redactedInputs = redactedSensitiveInputPaths(spec.user_inputs);
    if (redactedInputs.length) {
      showMessage('task-notice', `\u0421\u0435\u043a\u0440\u0435\u0442\u043d\u044b\u0435 \u043f\u043e\u043b\u044f \u043d\u0435 \u0432\u043e\u0437\u0432\u0440\u0430\u0449\u0430\u044e\u0442\u0441\u044f: ${redactedInputs.slice(0, 8).join(', ')}. \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e \u0438\u043b\u0438 \u0443\u0434\u0430\u043b\u0438\u0442\u0435 \u0438\u0437 JSON \u043f\u0435\u0440\u0435\u0434 \u043f\u043e\u0432\u0442\u043e\u0440\u043e\u043c.`, 'warn');
    }
    $('#task-goal').value = spec.goal ?? task.goal ?? '';
    $('#task-verbatim-text').value = spec.verbatim_text ?? '';
    $('#task-gemini-prompt').value = spec.workflow === 'gemini_prompt_submit' ? (spec.verbatim_text ?? '') : '';
    $('#task-workflow').value = spec.workflow === 'gemini_prompt_submit' ? 'gemini_prompt_submit' : 'agent';
    $('#task-sync-cookies').checked = spec.sync_cookies !== false;
    const profileId = spec.profile_id || 'default';
    if (![...$('#task-profile').options].some(option => option.value === profileId)) {
      const option = document.createElement('option'); option.value = profileId; option.textContent = `Профиль ${profileId}`; $('#task-profile').append(option);
    }
    $('#task-profile').value = profileId;
    $('#task-origins').value = Array.isArray(spec.allowed_origins) ? spec.allowed_origins.join(', ') : '';
    $('#task-execution-mode').value = spec.execution_mode || 'supervised';
    $('#task-advanced-options').value = JSON.stringify(specOptionsForEditor(spec), null, 2);
    show($('#task-advanced-wrap'), spec.workflow !== 'gemini_prompt_submit'); show($('#task-success-field'), false);
    setText($('#task-form-heading'), `Повтор задачи ${safe(task.task_id)}`);
    setText($('#task-form-submit'), 'Сохранить и отправить повторно →');
    workflowWasFast = false;
    show($('#task-create-card'), true); syncTaskWorkflow(); $('#task-workflow').disabled = false;
    if (spec.workflow === 'gemini_prompt_submit') $('#task-gemini-prompt').focus(); else $('#task-goal').focus();
  }
  function resetTaskForm() {
    state.repeatSpec = null; state.taskSubmitAttempt = null; workflowWasFast = false; $('#task-form').reset(); $('#task-advanced-options').value = '';
    show($('#task-advanced-wrap'), false); show($('#task-success-field'), true);
    setText($('#task-form-heading'), 'Новая задача'); setText($('#task-form-submit'), 'Создать задачу →');
    $('#task-workflow').disabled = false; savedGenericExecutionMode = $('#task-execution-mode').value || 'supervised'; syncTaskWorkflow();
  }
  function renderTasks() {
    const list = $('#tasks-list'); list.replaceChildren();
    setText($('#task-list-count'), `${state.tasks.length} ${plural(state.tasks.length, 'задача', 'задачи', 'задач')}`);
    if (!state.tasks.length) { list.innerHTML = '<div class="empty-state"><span class="empty-mark">◷</span><b>Задач пока нет</b><span>Создайте первую задачу, чтобы начать работу.</span></div>'; renderRecent(); renderDesktopTaskStrip(); return; }
    state.tasks.forEach(task => {
      const card = document.createElement('article'); card.className = 'task-row';
      const main = document.createElement('div'); main.className = 'task-main';
      const top = document.createElement('div'); top.className = 'task-topline';
      const id = document.createElement('code'); id.textContent = safe(task.task_id);
      const preflightError = taskPreflightError(task.error);
      const badge = document.createElement('span'); badge.className = `status-pill ${taskClass(task.status)}`; badge.textContent = task.spec?.workflow === 'gemini_prompt_submit' && task.phase === 'submitted' ? '\u0417\u0430\u043f\u0440\u043e\u0441 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d' : task.phase === 'completed_unverified' ? 'Завершено, без независимой проверки' : statusLabel(task.status);
      top.append(id, badge);
      const reasonText = window.taskStatusCopy?.reason(task) || '';
      const reason = document.createElement('p'); reason.className = 'task-stop-reason'; reason.setAttribute('role', 'status');
      reason.textContent = reasonText ? `\u041f\u0440\u0438\u0447\u0438\u043d\u0430 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0438: ${reasonText}` : ''; reason.hidden = !reasonText;
      const goal = document.createElement('p'); goal.className = 'task-goal'; goal.textContent = safe(task.spec?.goal || task.goal, 'Цель не указана');
      const meta = document.createElement('div'); meta.className = 'task-meta';
      [task.spec?.execution_mode === 'autonomous' ? 'Автономно от моего имени' : 'С подтверждениями', task.spec?.workflow === 'gemini_prompt_submit' ? '\u041e\u0434\u043d\u043e\u0440\u0430\u0437\u043e\u0432\u0430\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430; \u043e\u0442\u0432\u0435\u0442 \u043d\u0435 \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442\u0441\u044f' : '', task.phase === 'completed_unverified' ? 'Независимые критерии проверки не заданы; результат по сообщению модели.' : task.phase === 'authentication_required' ? 'Ожидает подтверждения входа в Gemini.' : task.phase && task.phase !== 'submitted' && `Этап: ${task.phase}`, task.step_count != null && `Шагов: ${task.step_count}`, ''].filter(Boolean).forEach(v => { const span = document.createElement('span'); span.textContent = String(v); meta.append(span); });
      if (task.status === 'expired') {
        const expiredNote = document.createElement('p'); expiredNote.className = 'task-expired-note';
        expiredNote.textContent = 'Срок задачи истёк. «Повторить…» откроет исходные параметры для редактирования; задача начнётся только после отправки формы.';
        main.append(top, reason, goal, meta, expiredNote, renderTaskSpec(task.spec));
      } else {
        main.append(top, reason, goal, meta, renderTaskSpec(task.spec));
      }
      card.append(main);
      if (task.spec?.execution_mode === 'autonomous') {
        void request(`/tasks/${encodeURIComponent(task.task_id)}/events`).then(data => {
          if (!card.isConnected) return;
          (data.events || []).filter(e => e.event_type === 'attention.notice').slice(-8).forEach(e => {
            const notice = document.createElement('p'); notice.className = 'task-goal'; notice.textContent = `Уведомление: ${safe(e.payload?.message, 'Нужны дополнительные данные')}`; main.append(notice);
          });
        }).catch(() => {});
      }
      const controls = document.createElement('div'); controls.className = 'task-controls';
      const repeat = document.createElement('button'); repeat.type = 'button'; repeat.className = 'small-button'; repeat.textContent = 'Повторить…'; repeat.addEventListener('click', () => openRepeatTask(task)); controls.append(repeat);
      if (preflightError && ['AUTH_REQUIRED', 'AUTH_STATE_UNKNOWN'].includes(preflightError.code)) {
        const openAuthBrowser = document.createElement('button'); openAuthBrowser.type = 'button'; openAuthBrowser.className = 'small-button'; openAuthBrowser.textContent = 'Открыть браузер для входа'; openAuthBrowser.addEventListener('click', openDesktop); controls.append(openAuthBrowser);
      }
      const idPath = encodeURIComponent(task.task_id || '');
      if (task.status === 'running') addTaskButton(controls, 'Пауза', 'pause', idPath, task);
      if (task.status === 'paused' || (task.status === 'waiting_human' && task.phase !== 'answer_required')) addTaskButton(controls, 'Продолжить', 'resume', idPath, task);
      if (task.status === 'recovering') addTaskButton(controls, 'Проверил состояние', 'review-recovery', idPath, task);
      if (['running', 'pause_requested', 'paused', 'waiting_human', 'waiting_approval', 'recovering'].includes(task.status)) addTaskButton(controls, 'Забрать управление', 'takeover', idPath, task);
      if (!['succeeded', 'failed', 'cancelled', 'expired'].includes(task.status)) addTaskButton(controls, 'Отменить', 'cancel', idPath, task, true);
      card.append(controls); list.append(card);
      if (['running', 'queued', 'pause_requested'].includes(task.status) && !state.autoPolledTasks.has(task.task_id)) pollTask(task.task_id);
    });
    renderRecent(); renderDesktopTaskStrip();
  }
  function renderDesktopTaskStrip() {
    const status = $('#desktop-task-status'), goal = $('#desktop-task-goal'), phase = $('#desktop-task-phase'), reason = $('#desktop-task-reason'), controls = $('#desktop-task-controls');
    if (!status || !controls) return;
    const terminal = new Set(['succeeded', 'done', 'completed', 'failed', 'cancelled', 'canceled', 'expired']);
    const task = state.tasks.find(item => !terminal.has(item.status)) || state.tasks[0];
    controls.replaceChildren();
    if (!task) { setText(status, 'Текущая задача'); setText(goal, 'Задачи пока нет'); setText(phase, ''); setText(reason, ''); reason.hidden = true; return; }
    setText(status, statusLabel(task.status)); setText(goal, safe(task.spec?.goal || task.goal, 'Цель не указана'));
    const showPhase = task.phase && !(task.status === 'running' && Number(task.step_count) > 0);
    setText(phase, [showPhase && `Этап: ${task.phase}`, task.step_count != null && `Шагов: ${task.step_count}`].filter(Boolean).join(' · '));
    const why = window.taskStatusCopy?.reason(task) || '';
    setText(reason, why ? `Причина остановки: ${why}` : ''); reason.hidden = !why;
    const idPath = encodeURIComponent(task.task_id || '');
    if (task.status === 'running') addTaskButton(controls, 'Пауза', 'pause', idPath, task);
    if (task.status === 'paused' || (task.status === 'waiting_human' && task.phase !== 'answer_required')) addTaskButton(controls, 'Продолжить', 'resume', idPath, task);
    if (task.status === 'recovering') addTaskButton(controls, 'Проверить', 'review-recovery', idPath, task);
    if (['running', 'pause_requested', 'paused', 'waiting_human', 'waiting_approval', 'recovering'].includes(task.status)) addTaskButton(controls, 'Забрать управление', 'takeover', idPath, task);
    if (!terminal.has(task.status) && !['recovering', 'expired'].includes(task.status)) addTaskButton(controls, 'Отменить', 'cancel', idPath, task, true);
    const repeat = document.createElement('button'); repeat.type = 'button'; repeat.className = 'small-button'; repeat.textContent = 'Изменить и повторить…';
    repeat.addEventListener('click', () => { activateTab('tasks'); openRepeatTask(task); }); controls.append(repeat);
  }
  function addTaskButton(container, label, action, idPath, task, danger = false) {
    const button = document.createElement('button'); button.type = 'button'; button.className = danger ? 'small-button danger-text' : 'small-button'; button.textContent = label;
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const latest = await request(`/tasks/${idPath}`); const current = latest.task || latest;
        await post(`/tasks/${idPath}/${action}`, { expected_version: current.version ?? current.expected_version ?? task.version ?? task.expected_version ?? 0 });
        state.autoPolledTasks.delete(task.task_id);
        toast(`Команда «${label.toLowerCase()}» отправлена`); await loadTasks();
      }
      catch (e) {
        if (e.code === 'TASK_EXPIRED' && action === 'resume') {
          toast('Срок задачи истёк. Нажмите «Повторить…», чтобы открыть её параметры и начать новый запуск.', 'error');
          await loadTasks();
          return;
        }
        toast(errorMessage(e), 'error'); button.disabled = false;
      }
    }); container.append(button);
  }
  async function pollTask(id) {
    if (state.activeTaskPolls.has(id) || state.autoPolledTasks.has(id)) return; state.activeTaskPolls.add(id); state.autoPolledTasks.add(id);
    try {
      for (let i = 0; i < 20; i++) {
        await new Promise(resolve => setTimeout(resolve, 2500));
        const data = await request(`/tasks/${encodeURIComponent(id)}`);
        const task = data.task || data;
        const index = state.tasks.findIndex(t => t.task_id === id);
        if (index >= 0) state.tasks[index] = { ...state.tasks[index], ...task };
        if (!['running', 'queued', 'pause_requested'].includes(task.status)) break;
      }
    } catch { /* Refresh remains available if the service is offline. */ }
    finally {
      state.activeTaskPolls.delete(id);
      const task = state.tasks.find(t => t.task_id === id);
      if (task && ['succeeded', 'failed', 'cancelled', 'expired'].includes(task.status)) state.autoPolledTasks.delete(id);
      renderTasks();
    }
  }
  function renderRecent() {
    const list = $('#recent-tasks'); if (!list) return; list.replaceChildren();
    if (!state.tasks.length) { list.innerHTML = '<div class="empty-state compact"><span class="empty-mark">◷</span><b>Задач пока нет</b><span>Новая задача появится здесь</span></div>'; return; }
    state.tasks.slice(0, 3).forEach(task => {
      const row = document.createElement('div'); row.className = 'recent-row';
      const dot = document.createElement('span'); dot.className = `recent-dot ${taskClass(task.status)}`;
      const copy = document.createElement('div'); copy.className = 'recent-copy';
      const goal = document.createElement('b'); goal.textContent = safe(task.spec?.goal || task.goal, 'Задача');
      const meta = document.createElement('span'); meta.textContent = `${statusLabel(task.status)} · ${safe(task.task_id)}`;
      copy.append(goal, meta); row.append(dot, copy); list.append(row);
    });
  }

  function activateTab(name) {
    $$('.nav-link').forEach(btn => { const selected = btn.dataset.tab === name; btn.classList.toggle('active', selected); btn.setAttribute('aria-selected', String(selected)); btn.tabIndex = selected ? 0 : -1; });
    $$('.tab-panel').forEach(panel => show(panel, panel.id === `panel-${name}`));
    setText($('#crumb-current'), ({ overview: 'Обзор', tasks: 'Задачи', cookies: 'Cookies', settings: 'Настройки' })[name] || 'Обзор');
    if (name === 'tasks') loadTasks();
    if (name === 'cookies') loadSources();
  }

  async function refreshBrowserControls() {
    if (!state.authenticated) return;
    const openButton = $('#toolbar-open-browser'), takeoverButton = $('#toolbar-takeover');
    const generation = state.browserRefreshGeneration = (state.browserRefreshGeneration || 0) + 1;
    const infoRequest = request('/desktop/info?profile_id=default').then(info => {
      if (generation !== state.browserRefreshGeneration) return;
      state.desktopInfo = info;
      const canOpen = !!(state.desktopInfo.available && state.desktopInfo.browser_started === true && state.desktopInfo.url);
      openButton.disabled = !canOpen;
      setText($('#desktop-viewer-detail'), displayError(state.desktopInfo.detail, canOpen ? 'Браузер готов к ручному управлению.' : 'Окно браузера пока недоступно.'));
    }).catch(() => {
      if (generation !== state.browserRefreshGeneration) return;
      state.desktopInfo = null; openButton.disabled = true;
      setText($('#desktop-viewer-detail'), 'Не удалось проверить окно браузера.');
    });
    const stateRequest = request('/desktop/state').then(browserState => {
      if (generation !== state.browserRefreshGeneration) return;
      state.controlOwner = browserState.control_owner || 'none';
      const active = state.controlOwner === 'human';
      setText($('#browser-control-state'), active ? 'Вы управляете браузером' : (browserState.available === false ? displayError(browserState.detail, 'Браузер не запущен') : 'Управление свободно'));
      show($('#toolbar-release'), active); show(takeoverButton, !active);
      takeoverButton.disabled = browserState.available === false;
      $('#browser-toolbar').dataset.owner = state.controlOwner;
    }).catch(error => {
      if (generation !== state.browserRefreshGeneration) return;
      state.controlOwner = 'unknown';
      setText($('#browser-control-state'), displayError(error?.message, 'Состояние браузера недоступно'));
      show($('#toolbar-release'), false); show(takeoverButton, true); takeoverButton.disabled = true;
    });
    await Promise.allSettled([infoRequest, stateRequest]);
  }

  async function openDesktop() {
    try {
      const info = await request('/desktop/info?profile_id=default'); state.desktopInfo = info;
      if (!(info.available && info.browser_started === true && info.url)) {
        throw new Error(displayError(info.detail, 'Окно браузера пока недоступно.'));
      }
      const url = new URL(info.url, location.href);
      if (url.origin !== location.origin || url.pathname !== '/desktop') throw new Error('Сервис вернул недопустимый адрес окна браузера.');
      url.searchParams.set('embed', '1');
      const wrap = $('#desktop-frame-wrap');
      let frame = wrap.querySelector('iframe');
      if (!frame) { frame = document.createElement('iframe'); frame.title = 'Окно браузера'; frame.src = url.href; wrap.append(frame); }
      show($('#desktop-viewer'), true); renderDesktopTaskStrip();
      $('#toolbar-open-browser').disabled = false;
    } catch (error) { toast(errorMessage(error), 'error'); await refreshBrowserControls(); }
  }

  async function changeProfileControl(operation) {
    const button = operation === 'takeover' ? $('#toolbar-takeover') : $('#toolbar-release');
    button.disabled = true;
    const expectedOwner = operation === 'takeover' ? 'human' : 'none';
    try {
      await post(`/profiles/default/${operation === 'takeover' ? 'takeover' : 'release-control'}`, {});
      await refreshBrowserControls();
      if (state.controlOwner !== expectedOwner) {
        throw new Error(`Сервис не подтвердил «${expectedOwner}». Проверьте состояние браузера и повторите действие.`);
      }
      await loadTasks();
      toast(operation === 'takeover' ? 'Управление браузером передано вам' : 'Управление браузером освобождено');
    } catch (error) {
      const hint = location.port === '8080' ? ' Панель браузера находится по адресу http://localhost:8090.' : '';
      toast(errorMessage(error) + hint, 'error');
    }
    finally { button.disabled = false; }
  }

  async function loadSources() {
    try {
      const data = await request('/chrome/sources'); state.sources = Array.isArray(data.sources) ? data.sources : [];
      const sourceSelect = $('#source-select');
      sourceSelect.replaceChildren();
      state.sources.forEach(s => { const option = document.createElement('option'); option.value = safe(s.source_id, ''); option.textContent = `${safe(s.label, s.source_id)}${s.online ? '' : ' · не в сети'}`; sourceSelect.append(option); });
      const source = state.sources.find(s => s.source_id === state.sourceId) || state.sources.find(s => s.online) || state.sources[0];
      if (!source) {
        state.sourceId = ''; setText($('#source-name'), 'Источник Chrome не найден'); setText($('#source-state'), 'Установите и подключите расширение Chrome.'); pill($('#source-pill'), 'bad', 'Нет связи');
        show($('#sites-empty'), true); setText($('#sites-empty b'), 'Нет подключённых профилей Chrome'); setText($('#sites-empty span:last-child'), 'Создайте код привязки и подключите расширение.'); show($('#sites-list'), false); show($('#sites-footer'), false); return;
      }
      state.sourceId = source.source_id;
      sourceSelect.value = state.sourceId; show(sourceSelect, state.sources.length > 1);
      setText($('#source-name'), safe(source.label, 'Профиль Chrome'));
      const seen = formatLastSeen(source.last_seen);
      setText($('#source-state'), source.online ? `В сети${seen ? ` · ${seen}` : ''}` : `Не в сети${seen ? ` · последний контакт ${seen}` : ''}`);
      pill($('#source-pill'), source.online ? 'good' : 'bad', source.online ? 'В сети' : 'Не в сети');
      if (!source.online) showMessage('sites-message', 'Расширение Chrome не в сети. Откройте Chrome и проверьте, что расширение включено.', 'warn');
      else { show($('#sites-message'), false); }
    } catch (e) { showMessage('sites-message', errorMessage(e), 'error'); }
  }
  function formatLastSeen(value) {
    if (value == null || value === '') return '';
    if (typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value))) return 'недавно';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'последний контакт был';
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  function showMessage(id, message, type = 'warn') { const el = $(`#${id}`); el.className = `notice notice-${type}`; setText(el, message); show(el, true); }
  async function refreshSites() {
    if (!state.sourceId) { await loadSources(); if (!state.sourceId) return; }
    const button = $('#refresh-sites'); button.disabled = true; button.textContent = 'Обновляем…';
    try {
      const started = await post(`/chrome/sources/${encodeURIComponent(state.sourceId)}/open-sites/refresh`, {});
      state.discovery = { status: 'pending', discovery_revision: started.request_id, sites: [] };
      state.selectedSites.clear(); invalidateCatalog(); show($('#catalog-message'), false);
      await pollOpenSites();
    } catch (e) { showMessage('sites-message', errorMessage(e), 'error'); }
    finally { button.disabled = false; button.innerHTML = '↻ Обновить список'; }
  }
  async function pollOpenSites() {
    if (!state.sourceId || state.sitesPolling) return;
    state.sitesPolling = true; $('#poll-sites-status').disabled = true; $('#poll-sites-status').textContent = 'Проверяем…';
    try {
      const started = Date.now(); let result = state.discovery;
      while (Date.now() - started < 46_000) {
        result = await request(`/chrome/sources/${encodeURIComponent(state.sourceId)}/open-sites`);
        state.discovery = result;
        if (!['pending', 'requested', 'running', 'refreshing'].includes(result.status)) break;
        await new Promise(resolve => setTimeout(resolve, 900));
      }
      const isPending = ['pending', 'requested', 'running', 'refreshing'].includes(result?.status);
      if (isPending) {
        result.sites = []; show($('#sites-empty'), true); show($('#sites-list'), false); show($('#sites-footer'), false);
        setText($('#sites-empty b'), 'Запрос списка ещё выполняется'); setText($('#sites-empty span:last-child'), 'Нажмите «Обновить», чтобы снова проверить этот запрос.');
        showMessage('sites-message', 'Расширение пока формирует список сайтов. Запрос остаётся активным.', 'warn'); show($('#poll-sites-status'), true); return;
      }
      const sites = Array.isArray(result?.sites) ? result.sites : [];
      state.selectedSites.clear(); renderSites(sites);
      if (result?.error) showMessage('sites-message', displayError(result.error), 'warn'); else show($('#sites-message'), false);
      show($('#poll-sites-status'), false);
    } catch (e) { showMessage('sites-message', errorMessage(e), 'error'); show($('#poll-sites-status'), true); }
    finally { state.sitesPolling = false; $('#poll-sites-status').disabled = false; $('#poll-sites-status').textContent = 'Обновить'; }
  }
  function renderSites(sites) {
    const list = $('#sites-list'); list.replaceChildren();
    show($('#sites-empty'), !sites.length); show(list, !!sites.length); show($('#sites-footer'), !!sites.length);
    if (!sites.length) { setText($('#sites-empty b'), 'Подходящих сайтов не найдено'); setText($('#sites-empty span:last-child'), 'Откройте обычные вкладки сайтов в подключённом Chrome и обновите список.'); }
    sites.forEach(site => {
      const row = document.createElement('label'); row.className = 'site-row';
      const check = document.createElement('input'); check.type = 'checkbox'; check.checked = false; check.value = safe(site.site_id, ''); check.addEventListener('change', () => { if (check.checked) state.selectedSites.add(check.value); else state.selectedSites.delete(check.value); updateSiteSelection(); });
      const icon = document.createElement('span'); icon.className = 'site-favicon'; icon.textContent = safe(site.origin, '?').replace(/^https?:\/\//, '').slice(0, 1).toUpperCase();
      const info = document.createElement('span'); info.className = 'site-info';
      const origin = document.createElement('b'); origin.textContent = safe(site.origin);
      const meta = document.createElement('small'); meta.textContent = `${Number(site.tab_count || 0)} ${plural(Number(site.tab_count || 0), 'вкладка', 'вкладки', 'вкладок')} · ${safe(site.store_id, 'Хранилище по умолчанию')}`;
      info.append(origin, meta);
      const arrow = document.createElement('span'); arrow.className = 'site-arrow'; arrow.textContent = '›';
      row.append(check, icon, info, arrow); list.append(row);
    }); updateSiteSelection();
  }
  function updateSiteSelection() {
    setText($('#site-selection-count'), `Выбрано: ${state.selectedSites.size} ${plural(state.selectedSites.size, 'сайт', 'сайта', 'сайтов')}`);
    $('#load-cookies').disabled = state.selectedSites.size === 0;
    invalidateCatalog(); show($('#catalog-message'), false);
  }
  function invalidateCatalog() {
    state.catalogGeneration++; clearInterval(state.catalogTimer); state.catalogTimer = null;
    state.pendingCatalogId = ''; state.catalog = null; state.cookies = []; state.selectedCookies.clear();
    show($('#catalog-panel'), false); show($('#poll-catalog-status'), false); updateCookieSelection();
  }
  function catalogError(error, fallback) {
    if (['HOST_PERMISSION_REQUIRED', 'PERMISSION_REQUIRED', 'CHROME_PERMISSION_REQUIRED'].includes(error?.code) || /permission is required/i.test(error?.message || '')) return 'Откройте значок нашего расширения в Chrome, нажмите «Grant selected site access» и подтвердите разрешение Chrome. Вернитесь сюда и нажмите «Повторить загрузку cookies». Выбранные сайты сохранены.';
    return displayError(error, fallback);
  }
  async function checkCatalogExpiry(id, generation) {
    try {
      const current = await request(`/chrome/cookie-catalogs/${encodeURIComponent(id)}`);
      if (generation !== state.catalogGeneration || state.catalog?.catalog_id !== id) return;
      if (current.status !== 'ready') { invalidateCatalog(); showMessage('catalog-message', 'Срок списка cookies истёк. Загрузите список снова и отметьте нужные записи.', 'warn'); }
    } catch { if (generation === state.catalogGeneration) { invalidateCatalog(); showMessage('catalog-message', 'Не удалось проверить список cookies. Загрузите его снова.', 'error'); } }
  }
  async function loadCatalog() {
    if (!state.discovery || !state.selectedSites.size) return;
    invalidateCatalog(); const generation = state.catalogGeneration;
    showMessage('catalog-message', 'Загружаем список cookies. Затем отметьте нужные записи.', 'warn');
    const button = $('#load-cookies'); button.disabled = true; button.textContent = 'Загружаем…';
    try {
      const started = await post(`/chrome/sources/${encodeURIComponent(state.sourceId)}/cookie-catalogs`, { site_ids: [...state.selectedSites], discovery_revision: state.discovery.discovery_revision });
      if (generation !== state.catalogGeneration) return;
      state.pendingCatalogId = started.catalog_id; await pollCookieCatalog();
    } catch (e) { if (generation === state.catalogGeneration) showMessage('catalog-message', catalogError(e, errorMessage(e)), 'error'); }
    finally { button.disabled = state.selectedSites.size === 0; button.textContent = 'Повторить загрузку cookies'; }
  }
  async function pollCookieCatalog() {
    if (!state.pendingCatalogId || state.catalogPolling) return;
    const generation = state.catalogGeneration; const catalogId = state.pendingCatalogId;
    state.catalogPolling = true; $('#poll-catalog-status').disabled = true; $('#poll-catalog-status').textContent = 'Проверяем…';
    try {
      const started = Date.now(); let catalog = null;
      while (Date.now() - started < 46_000) {
        catalog = await request(`/chrome/cookie-catalogs/${encodeURIComponent(catalogId)}`);
        if (generation !== state.catalogGeneration) return;
        if (!['pending', 'requested', 'running', 'collecting'].includes(catalog.status)) break;
        await new Promise(resolve => setTimeout(resolve, 900));
      }
      if (['pending', 'requested', 'running', 'collecting'].includes(catalog?.status)) {
        showMessage('catalog-message', 'Каталог ещё собирается. Нажмите «Обновить каталог», чтобы проверить тот же запрос.', 'warn'); show($('#poll-catalog-status'), true); return;
      }
      if (catalog.status !== 'ready') {
        showMessage('catalog-message', catalogError(catalog.error, `Каталог недоступен: ${statusLabel(catalog.status)}`), 'warn'); show($('#poll-catalog-status'), false); return;
      }
      state.catalog = catalog; state.cookies = Array.isArray(catalog.cookies) ? catalog.cookies : []; state.selectedCookies.clear();
      show($('#catalog-message'), false); show($('#poll-catalog-status'), false); renderCatalog();
      state.catalogTimer = setInterval(() => checkCatalogExpiry(catalogId, generation), 5000);
    } catch (e) { if (generation === state.catalogGeneration) { invalidateCatalog(); showMessage('catalog-message', catalogError(e, errorMessage(e)), 'error'); } }
    finally { state.catalogPolling = false; $('#poll-catalog-status').disabled = false; $('#poll-catalog-status').textContent = 'Обновить каталог'; }
  }
  function renderCatalog() {
    const groups = $('#cookie-groups'); groups.replaceChildren();
    setText($('#catalog-revision'), `Ревизия ${safe(state.catalog?.revision)}`);
    if (!state.cookies.length) { const empty = document.createElement('div'); empty.className = 'empty-state compact'; empty.textContent = 'Для выбранных сайтов cookies не найдены.'; groups.append(empty); }
    const siteById = new Map((state.discovery?.sites || []).map(s => [s.site_id, s]));
    const cookiesBySite = new Map();
    state.cookies.forEach(cookie => (cookie.site_ids || []).forEach(id => { if (!state.selectedSites.has(id)) return; if (!cookiesBySite.has(id)) cookiesBySite.set(id, []); cookiesBySite.get(id).push(cookie); }));
    for (const siteId of state.selectedSites) {
      const site = siteById.get(siteId); const cookies = cookiesBySite.get(siteId) || [];
      const group = document.createElement('section'); group.className = 'cookie-group';
      const head = document.createElement('div'); head.className = 'cookie-group-head';
      const siteTitle = document.createElement('div'); siteTitle.className = 'cookie-site-title';
      const availableCookies = cookies.filter(c => !c.unsupported_reason);
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = availableCookies.length > 0 && availableCookies.every(c => state.selectedCookies.has(c.cookie_id)); checkbox.setAttribute('aria-label', `Выбрать все доступные cookies сайта ${safe(site?.origin)}`);
      checkbox.addEventListener('change', () => { cookies.filter(c => !c.unsupported_reason).forEach(c => checkbox.checked ? state.selectedCookies.add(c.cookie_id) : state.selectedCookies.delete(c.cookie_id)); renderCatalog(); });
      const title = document.createElement('b'); title.textContent = safe(site?.origin, siteId);
      const count = document.createElement('span'); count.className = 'muted'; count.textContent = `${cookies.length} ${plural(cookies.length, 'cookie', 'cookies', 'cookies')}`;
      siteTitle.append(checkbox, title, count);
      const pickAll = document.createElement('button'); pickAll.type = 'button'; pickAll.className = 'text-button'; pickAll.textContent = 'Выбрать все доступные'; pickAll.addEventListener('click', () => { availableCookies.forEach(c => state.selectedCookies.add(c.cookie_id)); renderCatalog(); });
      head.append(siteTitle, pickAll); group.append(head);
      if (!cookies.length) { const none = document.createElement('div'); none.className = 'cookie-none'; none.textContent = 'Для этого сайта нет записей в каталоге.'; group.append(none); }
      cookies.forEach(cookie => group.append(createCookieRow(cookie)));
      groups.append(group);
    }
    show($('#catalog-panel'), true); updateCookieSelection();
  }
  function createCookieRow(cookie) {
    const row = document.createElement('div'); row.className = `cookie-row ${cookie.unsupported_reason ? 'unsupported' : ''}`;
    const check = document.createElement('input'); check.type = 'checkbox'; check.checked = state.selectedCookies.has(cookie.cookie_id); check.disabled = !!cookie.unsupported_reason; check.setAttribute('aria-label', `Выбрать cookie ${safe(cookie.name)}`);
    check.addEventListener('change', () => { if (check.checked) state.selectedCookies.add(cookie.cookie_id); else state.selectedCookies.delete(cookie.cookie_id); updateCookieSelection(); });
    const data = document.createElement('div'); data.className = 'cookie-data';
    const name = document.createElement('b'); name.textContent = safe(cookie.name);
    const domain = document.createElement('span'); domain.textContent = `${safe(cookie.domain)}${safe(cookie.path, '/')}`;
    const attrs = document.createElement('div'); attrs.className = 'cookie-attrs';
    [['Secure', cookie.secure], ['HttpOnly', cookie.http_only], ['SameSite', cookie.same_site]].forEach(([label, value]) => { if (value) { const tag = document.createElement('span'); tag.textContent = label === 'SameSite' ? `SameSite ${value}` : label; attrs.append(tag); } });
    if (cookie.expires_at) { const expires = document.createElement('span'); expires.textContent = `До ${safe(cookie.expires_at)}`; attrs.append(expires); } else { const session = document.createElement('span'); session.textContent = 'Сессионная'; attrs.append(session); }
    data.append(name, domain, attrs);
    if (cookie.unsupported_reason) { const reason = document.createElement('small'); reason.className = 'unsupported-reason'; reason.textContent = safe(cookie.unsupported_reason); data.append(reason); }
    row.append(check, data); return row;
  }
  function updateCookieSelection() {
    setText($('#selection-total'), `${state.selectedCookies.size} ${plural(state.selectedCookies.size, 'cookie выбрана', 'cookies выбраны', 'cookies выбрано')}`);
    setText($('#selection-sites'), `Для ${state.selectedSites.size} ${plural(state.selectedSites.size, 'сайта', 'сайтов', 'сайтов')}`);
    $('#import-cookies').disabled = state.importing || (state.importJob && !terminalJobs.has(state.importJob.status)) || state.catalog?.status !== 'ready' || state.selectedCookies.size === 0;
    setText($('#selection-hint'), state.catalog?.status === 'ready' ? (state.selectedCookies.size ? 'Можно перенести отмеченные записи.' : 'Отметьте хотя бы одну cookie в списке.') : 'Выберите сайты, загрузите список и отметьте cookies.');
  }
  async function startImport() {
    if (state.importing || (state.importJob && !terminalJobs.has(state.importJob.status)) || state.catalog?.status !== 'ready' || !state.selectedCookies.size) return;
    state.importing = true; const generation = state.catalogGeneration; const catalogId = state.catalog.catalog_id;
    const button = $('#import-cookies'); button.disabled = true; button.textContent = 'Создаём задание…';
    try {
      await checkCatalogExpiry(catalogId, generation);
      if (generation !== state.catalogGeneration || state.catalog?.status !== 'ready') return;
      const profileId = $('#cookie-profile')?.value || state.status?.profiles?.[0]?.profile_id || 'default';
      const job = await post(`/profiles/${encodeURIComponent(profileId)}/cookie-import-jobs`, {
        source_id: state.sourceId,
        discovery_revision: state.discovery?.discovery_revision,
        catalog_id: state.catalog.catalog_id,
        catalog_revision: state.catalog.revision,
        selected_site_ids: [...state.selectedSites],
        selected_cookie_ids: [...state.selectedCookies],
        conflict_mode: 'replace_selected_matches',
        idempotency_key: randomKey()
      });
      state.importJob = { ...job, profileId }; show($('#import-panel'), true); show($('#import-manual'), false); show($('#refresh-import'), true); show($('#import-progress'), true);
      await pollImport(profileId, job.job_id);
    } catch (e) { invalidateCatalog(); showMessage('catalog-message', catalogError(e, errorMessage(e)), 'error'); }
    finally { state.importing = false; updateCookieSelection(); button.innerHTML = 'Перенести выбранные cookies в Camoufox <span aria-hidden="true">→</span>'; }
  }
  async function pollImport(profileId, jobId) {
    if (state.importPolling) return;
    state.importPolling = true;
    const started = Date.now(); let data = state.importJob || {};
    try {
      while (Date.now() - started < 120000) {
        data = await request(`/profiles/${encodeURIComponent(profileId)}/cookie-import-jobs/${encodeURIComponent(jobId)}`);
        renderImport(data);
        if (terminalJobs.has(data.status)) break;
        await new Promise(resolve => setTimeout(resolve, 1800));
      }
      if (!terminalJobs.has(data.status)) { setText($('#import-detail'), data.status === 'waiting_pause' ? 'Браузер занят. Нажмите «Вернуть управление» при ручном управлении или дождитесь завершения задачи, затем обновите статус. Список cookies действует ограниченное время.' : 'Проверка остановлена по таймеру. Обновите статус, чтобы продолжить.'); show($('#refresh-import'), true); }
    } catch (e) { setText($('#import-detail'), errorMessage(e)); show($('#refresh-import'), true); }
    finally { state.importPolling = false; updateCookieSelection(); }
  }
  function reportValue(data, keys) { for (const key of keys) if (data[key] != null) return data[key]; return null; }
  function renderImport(data) {
    state.importJob = { ...state.importJob, ...data };
    const labels = { validating: 'Проверка', waiting_permission: 'Ожидает разрешения', waiting_pause: 'Ожидает паузы', reading_selected: 'Чтение выбранного', applying: 'Применение', verifying: 'Проверка входа', done: 'Завершено', partial: 'Частично', failed: 'Ошибка', expired: 'Истёк срок', selection_stale: 'Выбор устарел' };
    const kind = ['done'].includes(data.status) ? 'good' : ['partial', 'waiting_permission', 'waiting_pause', 'selection_stale'].includes(data.status) ? 'warning' : ['failed', 'expired'].includes(data.status) ? 'bad' : 'pending';
    pill($('#import-status'), kind, labels[data.status] || safe(data.status, 'В очереди'));
    const detail = data.status === 'waiting_pause' ? (data.detail || 'Браузер занят. При ручном управлении нажмите «Вернуть управление»; если выполняется задача, дождитесь её паузы или завершения. Список cookies действует ограниченное время.') : data.status === 'partial' ? 'Перенос выполнен частично. Проверьте счётчики ниже.' : data.status === 'failed' ? displayError(data.error, 'Перенос не выполнен. Проверьте счётчики ниже.') : displayError(data.error, data.detail || data.message || 'Задание выполняется…');
    setText($('#import-detail'), detail);
    let release = $('#import-release-control');
    if (!release) {
      release = document.createElement('button'); release.id = 'import-release-control'; release.type = 'button';
      release.className = 'secondary-button hidden'; release.textContent = 'Освободить управление и перенести';
      release.addEventListener('click', async () => {
        release.disabled = true;
        try {
          const job = state.importJob;
          const current = await request(`/profiles/${encodeURIComponent(job.profileId)}/cookie-import-jobs/${encodeURIComponent(job.job_id)}`);
          if (current.status !== 'waiting_pause') { renderImport(current); return; }
          const browser = await request('/desktop/state');
          if (browser.control_owner !== 'human') { show(release, false); return; }
          await post(`/profiles/${encodeURIComponent(job.profileId)}/release-control`, {});
          show(release, false); setText($('#import-detail'), 'Управление возвращено. Проверяем состояние переноса…');
          await refreshBrowserControls(); void pollImport(job.profileId, job.job_id);
        } catch (error) { setText($('#import-detail'), errorMessage(error)); }
        finally { release.disabled = false; }
      });
      $('#import-detail').after(release);
    }
    show(release, false);
    if (data.status === 'waiting_pause') {
      const jobId = state.importJob.job_id;
      void request('/desktop/state').then(browser => {
        if (state.importJob?.job_id === jobId && state.importJob?.status === 'waiting_pause') show(release, browser.control_owner === 'human');
      }).catch(() => {});
    }
    const report = $('#import-report'); report.replaceChildren();
    const counts = data.counts || data.report || data;
    const items = [
      ['Выбрано', reportValue(counts, ['selected', 'selected_count', 'requested_count'])],
      ['Перенесено', reportValue(counts, ['copied', 'imported', 'imported_count', 'applied', 'applied_count', 'copied_count'])],
      ['Пропущено', reportValue(counts, ['skipped', 'skipped_count'])],
      ['Ошибки', reportValue(counts, ['failed', 'failed_count', 'error_count'])]
    ];
    items.filter(item => item[1] != null).forEach(([label, value]) => { const cell = document.createElement('div'); cell.className = 'report-cell'; const number = document.createElement('b'); number.textContent = String(value); const caption = document.createElement('span'); caption.textContent = label; cell.append(number, caption); report.append(cell); });
    if (data.outcomes_truncated) { const line = document.createElement('div'); line.className = 'report-error'; line.textContent = 'Показаны не все подробности результатов. Счётчики отражают полный итог.'; report.append(line); }
    show($('#import-progress'), !terminalJobs.has(data.status)); show($('#refresh-import'), !terminalJobs.has(data.status));
    if (terminalJobs.has(data.status)) {
      show($('#import-manual'), true);
      const errors = Array.isArray(data.errors) ? data.errors : [];
      if (errors.length) errors.forEach(item => { const line = document.createElement('div'); line.className = 'report-error'; line.textContent = typeof item === 'string' ? item : (item.message || item.reason || 'Ошибка одной из записей'); report.append(line); });
      checkDesktopAvailability(data.profile_id || state.importJob?.profileId || 'default');
    }
  }
  async function checkDesktopAvailability() { await refreshBrowserControls(); }

  async function createPairing() {
    const button = $('#pairing-button'); button.disabled = true;
    try {
      const result = await post('/extension/pairing-codes', {});
      setText($('#pairing-code'), safe(result.code));
      setText($('#pairing-expiry'), `Действует ${safe(result.expires_in_s, '?')} сек.`);
      show($('#pairing-code-wrap'), true); setText($('#pairing-message'), 'Введите этот код в установленном расширении Chrome.');
    } catch (e) { setText($('#pairing-message'), errorMessage(e)); }
    finally { button.disabled = false; }
  }

  async function authCheck() {
    show($('#workspace'), false); show($('#auth-gate'), false); show($('#logout'), false);
    try {
      const session = await request('/operator/session');
      state.localUI = session.local_ui === true;
      state.csrf = session.csrf_token || session.csrf || '';
      if (session.authenticated || session.operator_authenticated || session.active) { state.authenticated = true; enterWorkspace(); }
    } catch (e) {
      show($('#auth-gate'), true);
      // An unauthenticated response is expected; a service outage is shown on the sign-in panel.
      if (e.message.includes('недоступен')) setText($('#login-error'), e.message);
    }
  }
  function enterWorkspace() { state.authenticated = true; show($('#auth-gate'), false); show($('#workspace'), true); show($('#browser-toolbar'), true); show($('#logout'), !state.localUI); loadStatus(); loadTasks(); refreshBrowserControls(); }

  $('#login-form').addEventListener('submit', async event => {
    event.preventDefault(); setText($('#login-error'), '');
    const token = $('#login-token').value; const button = $('button[type="submit"]', event.currentTarget); button.disabled = true;
    try {
      const result = await post('/operator/session', { token });
      state.localUI = result.local_ui === true;
      state.csrf = result.csrf_token || result.csrf || '';
      $('#login-token').value = ''; enterWorkspace();
    } catch (e) { setText($('#login-error'), errorMessage(e)); }
    finally { button.disabled = false; }
  });
  $('#logout').addEventListener('click', async () => {
    try { await request('/operator/session', { method: 'DELETE' }); } catch { /* Local cookie expires server-side if the service is offline. */ }
    state.csrf = ''; state.authenticated = false; show($('#workspace'), false); show($('#browser-toolbar'), false); show($('#desktop-viewer'), false); $('#desktop-frame-wrap').replaceChildren(); show($('#auth-gate'), true); show($('#logout'), false); activateTab('overview');
  });
  $$('.nav-link').forEach(button => button.addEventListener('click', () => activateTab(button.dataset.tab)));
  document.addEventListener('click', event => { const go = event.target.closest('[data-go]'); if (go) activateTab(go.dataset.go); });
  $('#refresh-all').addEventListener('click', () => { if (state.authenticated) { loadStatus(); loadTasks(); refreshBrowserControls(); if ($('#panel-cookies:not(.hidden)')) loadSources(); } else authCheck(); });
  $('#refresh-tasks').addEventListener('click', loadTasks);
  $('#toolbar-open-browser').addEventListener('click', openDesktop);
  $('#toolbar-takeover').addEventListener('click', () => changeProfileControl('takeover'));
  $('#toolbar-release').addEventListener('click', () => changeProfileControl('release-control'));
  $('#close-desktop-viewer').addEventListener('click', () => { show($('#desktop-viewer'), false); $('#desktop-frame-wrap').replaceChildren(); });
  $('#new-task-button').addEventListener('click', () => { resetTaskForm(); show($('#task-create-card'), true); $('#task-goal').focus(); });
  $('#close-task-form').addEventListener('click', () => { resetTaskForm(); show($('#task-create-card'), false); });
  let geminiAuthPanelWasVisible = false;
  function taskUsesGemini() {
    if ($('#task-workflow').value === 'gemini_prompt_submit') return true;
    return $('#task-origins').value.split(',').some(value => {
      try { const url = new URL(value.trim()); return url.origin === 'https://gemini.google.com' && url.pathname === '/' && !url.search && !url.hash && !url.username && !url.password; } catch { return false; }
    });
  }
  function updateGeminiAuthPanel() {
    const visible = !$('#task-create-card').classList.contains('hidden') && taskUsesGemini();
    show($('#task-gemini-auth-panel'), visible);
    if (visible) {
      renderGeminiAuthStatus();
      if (!geminiAuthPanelWasVisible) void loadGeminiAuthStatus(true);
    }
    geminiAuthPanelWasVisible = visible;
  }
  function updateGeminiCapability() {
    const allowed = $('#task-origins').value.split(',').some(value => {
      try { const url = new URL(value.trim()); return url.origin === 'https://gemini.google.com' && url.pathname === '/' && !url.search && !url.hash && !url.username && !url.password; } catch { return false; }
    });
    const checkbox = $('#task-gemini-capability');
    if (!allowed) checkbox.checked = false;
    checkbox.disabled = !allowed; show($('#gemini-capability-wrap'), allowed);
    updateGeminiAuthPanel();
  }
  let savedGenericExecutionMode = 'autonomous';
  let workflowWasFast = false;
  function syncTaskWorkflow() {
    const fast = $('#task-workflow').value === 'gemini_prompt_submit';
    if (fast && !workflowWasFast) savedGenericExecutionMode = $('#task-execution-mode').value || 'supervised';
    if (!fast && workflowWasFast) $('#task-execution-mode').value = savedGenericExecutionMode;
    workflowWasFast = fast;
    show($('#task-execution-field'), !fast);
    show($('#task-goal-field'), !fast);
    show($('#task-origins-field'), !fast);
    show($('#task-verbatim-field'), !fast);
    show($('#task-success-field'), !fast && !state.repeatSpec);
    show($('#task-advanced-wrap'), !!state.repeatSpec && !fast);
    show($('#task-gemini-prompt-field'), fast);
    $('#task-goal').required = !fast;
    $('#task-origins').required = !fast;
    $('#task-gemini-prompt').required = fast;
    if (fast) $('#task-execution-mode').value = 'autonomous';
    setText($('#task-form-limit'), fast ? '\u041e\u0434\u0438\u043d \u0437\u0430\u043f\u0440\u043e\u0441 \xb7 \u0431\u0435\u0437 \u043e\u0436\u0438\u0434\u0430\u043d\u0438\u044f \u043e\u0442\u0432\u0435\u0442\u0430' : '\u041b\u0438\u043c\u0438\u0442: 80 \u0448\u0430\u0433\u043e\u0432 \xb7 15 \u043c\u0438\u043d\u0443\u0442');
    updateGeminiCapability();
    updateGeminiAuthPanel();
    if (fast) show($('#gemini-capability-wrap'), false);
  }
  $('#task-workflow').addEventListener('change', syncTaskWorkflow);
  $('#task-origins').addEventListener('input', updateGeminiCapability);
  $('#task-gemini-auth-open-browser').addEventListener('click', openDesktop);
  $('#task-gemini-auth-refresh').addEventListener('click', () => { geminiAuthState = null; void loadGeminiAuthStatus(true); });
  $('#task-form').addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget, button = $('button[type="submit"]', form); button.disabled = true;
    const fastWorkflow = $('#task-workflow').value === 'gemini_prompt_submit';
    const originValues = fastWorkflow ? ['https://gemini.google.com'] : $('#task-origins').value.split(',').map(value => value.trim()).filter(Boolean);
    const origins = [];
    if (!originValues.length) {
      showMessage('task-notice', 'Укажите хотя бы один HTTPS-origin, например https://example.com.', 'error');
      button.disabled = false;
      return;
    }
    for (const value of originValues) {
      try {
        const url = new URL(value);
        if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error();
        origins.push(url.origin);
      } catch {
        showMessage('task-notice', `Укажите корректный HTTPS-origin без пути, логина или пароля: ${value}`, 'error');
        button.disabled = false;
        return;
      }
    }
    const allowedOrigins = [...new Set(origins)];
    const geminiCapability = !fastWorkflow && $('#task-gemini-capability').checked;
    if (geminiCapability && !allowedOrigins.includes('https://gemini.google.com')) {
      showMessage('task-notice', 'Для отправки запроса добавьте https://gemini.google.com в разрешённые сайты.', 'error'); button.disabled = false; return;
    }
    const successText = $('#task-success-text').value.trim();
    const goal = fastWorkflow ? (state.repeatSpec?.goal ?? 'Submit the exact supplied prompt to Gemini once.') : $('#task-goal').value;
    const fastPrompt = $('#task-gemini-prompt').value;
    if (fastWorkflow && !fastPrompt.trim()) { showMessage('task-notice', '\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0442\u0435\u043a\u0441\u0442 \u0437\u0430\u043f\u0440\u043e\u0441\u0430 \u0434\u043b\u044f Gemini.', 'error'); button.disabled = false; return; }
    if (!fastWorkflow && !goal.trim()) { showMessage('task-notice', 'Укажите цель задачи.', 'error'); button.disabled = false; return; }
    let advanced = {};
    const repeatWorkflowChanged = state.repeatSpec && (state.repeatSpec.workflow === 'gemini_prompt_submit') !== fastWorkflow;
    if (state.repeatSpec && (!fastWorkflow || repeatWorkflowChanged)) {
      try { advanced = JSON.parse($('#task-advanced-options').value || '{}'); }
      catch { showMessage('task-notice', 'Дополнительные параметры должны быть корректным JSON.', 'error'); button.disabled = false; return; }
      if (!advanced || typeof advanced !== 'object' || Array.isArray(advanced)) { showMessage('task-notice', 'Дополнительные параметры должны быть JSON-объектом.', 'error'); button.disabled = false; return; }
      if (state.repeatSpec.user_inputs && advanced.user_inputs && typeof advanced.user_inputs === 'object') advanced.user_inputs = restoreMaskedInputs(state.repeatSpec.user_inputs, advanced.user_inputs);
    }
    try {
      const base = state.repeatSpec ? { ...state.repeatSpec, ...advanced } : { goal_language: 'ru', success_conditions: successText ? [{ kind: 'text_present', text: successText }] : [], user_inputs: {}, secret_refs: [], policy_grant_id: null, sync_cookies: true, site_capabilities: geminiCapability ? ['gemini_prompt_submit'] : [], limits: { timeout_s: 900, max_steps: 80 } };
      for (const key of ['task_id', 'status', 'version', 'result', 'error', 'principal_id', 'created_at', 'updated_at', 'idempotency_key']) delete base[key];
      const requestSpec = { ...base, profile_id: $('#task-profile').value || 'default', goal, workflow: fastWorkflow ? 'gemini_prompt_submit' : 'agent', verbatim_text: fastWorkflow ? fastPrompt : ($('#task-verbatim-text').value || null), allowed_origins: allowedOrigins, execution_mode: fastWorkflow ? 'autonomous' : $('#task-execution-mode').value, sync_cookies: $('#task-sync-cookies').checked };
      if (fastWorkflow) { requestSpec.user_inputs = {}; requestSpec.secret_refs = []; requestSpec.policy_grant_id = null; requestSpec.success_conditions = []; requestSpec.site_capabilities = ['gemini_prompt_submit']; }
      const unreentered = state.repeatSpec
        ? redactedSensitiveInputPaths(requestSpec.user_inputs)
        : [];
      if (unreentered.length) {
        showMessage('task-notice', `\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e \u0438\u043b\u0438 \u0443\u0434\u0430\u043b\u0438\u0442\u0435 \u0441\u0435\u043a\u0440\u0435\u0442\u043d\u044b\u0435 \u043f\u043e\u043b\u044f \u043f\u0435\u0440\u0435\u0434 \u043f\u043e\u0432\u0442\u043e\u0440\u043e\u043c: ${unreentered.slice(0, 8).join(', ')}.`, 'error');
        button.disabled = false;
        return;
      }
      if (!state.repeatSpec && !requestSpec.site_capabilities?.length && geminiCapability) requestSpec.site_capabilities = ['gemini_prompt_submit'];
      const fingerprint = JSON.stringify(requestSpec);
      if (!state.taskSubmitAttempt || state.taskSubmitAttempt.fingerprint !== fingerprint) state.taskSubmitAttempt = { fingerprint, key: randomKey() };
      requestSpec.idempotency_key = state.taskSubmitAttempt.key;
      const task = await post('/tasks', requestSpec);
      const hasVerification = Array.isArray(requestSpec.success_conditions) && requestSpec.success_conditions.length > 0;
      state.taskSubmitAttempt = null; resetTaskForm(); show($('#task-create-card'), false);
      const verificationNote = fastWorkflow ? ' Запрос отправлен; ответ не проверялся.' : hasVerification ? '' : ' Условие проверки не задано — результат будет отмечен как не проверенный независимо.';
      showMessage('task-notice', fastWorkflow ? `\u0421\u043e\u0437\u0434\u0430\u043d\u0430 \u0437\u0430\u0434\u0430\u0447\u0430 \u043e\u0434\u043d\u043e\u0440\u0430\u0437\u043e\u0432\u043e\u0439 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438: ${safe(task.task_id)}. \u041e\u0442\u0432\u0435\u0442 \u043d\u0435 \u043e\u0436\u0438\u0434\u0430\u0435\u0442\u0441\u044f \u0438 \u043d\u0435 \u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442\u0441\u044f.` : `Задача создана: ${safe(task.task_id)}.${verificationNote}`, 'good'); await loadTasks();
    } catch (e) { showMessage('task-notice', errorMessage(e), 'error'); }
    finally { button.disabled = false; }
  });
  $('#pairing-button').addEventListener('click', createPairing);
  $('#refresh-sites').addEventListener('click', refreshSites);
  $('#poll-sites-status').addEventListener('click', pollOpenSites);
  $('#source-select').addEventListener('change', event => {
    state.sourceId = event.currentTarget.value; state.discovery = null; state.selectedSites.clear(); invalidateCatalog();
    state.pendingCatalogId = ''; show($('#catalog-panel'), false); show($('#catalog-message'), false); show($('#poll-catalog-status'), false); show($('#poll-sites-status'), false); show($('#sites-message'), false); show($('#sites-footer'), false); show($('#sites-list'), false); show($('#sites-empty'), true);
    setText($('#sites-empty b'), 'Обновите список сайтов'); setText($('#sites-empty span:last-child'), 'Нажмите «Обновить список», чтобы загрузить вкладки выбранного профиля Chrome.');
    const source = state.sources.find(s => s.source_id === state.sourceId);
    if (source) { setText($('#source-name'), safe(source.label, source.source_id)); pill($('#source-pill'), source.online ? 'good' : 'bad', source.online ? 'В сети' : 'Не в сети'); setText($('#source-state'), source.online ? 'Профиль Chrome подключён' : 'Откройте Chrome и проверьте расширение'); }
  });
  $('#load-cookies').addEventListener('click', loadCatalog);
  $('#cookie-profile').addEventListener('change', () => { invalidateCatalog(); showMessage('catalog-message', 'Профиль изменён. Загрузите список cookies снова.', 'warn'); });
  $('#poll-catalog-status').addEventListener('click', pollCookieCatalog);
  $('#import-cookies').addEventListener('click', startImport);
  $('#refresh-import').addEventListener('click', () => { if (state.importJob) pollImport(state.importJob.profileId, state.importJob.job_id); });
  authCheck();
})();
