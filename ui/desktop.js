(() => {
  'use strict';

  const API = '/api/v1';
  const embedMode = new URLSearchParams(location.search).get('embed') === '1';
  if (embedMode) document.documentElement.classList.add('desktop-embed');
  const VIEWER_POLL_MS = 500;
  const HUMAN_POLL_MS = 180;
  const TASK_POLL_MS = 2000;
  const SERVICE_POLL_MS = 5000;
  const REQUEST_TIMEOUT_MS = 8000;
  const MAX_SCROLL = 600;
  const POINTER_INTERVAL_MS = 50;
  const $ = (id) => document.getElementById(id);
  const ui = {
    csrf: '',
    current: null,
    polling: false,
    hidden: document.hidden,
    timer: null,
    taskTimer: null,
    serviceTimer: null,
    taskPolling: false,
    taskEventsLoading: false,
    servicePolling: false,
    lastTaskPoll: 0,
    currentTask: null,
    currentTaskKey: '',
    taskControlPending: false,
    taskEventKey: '',
    taskEventSummary: [],
    taskEventCursor: 0,
    screenshotAbort: null,
    screenshotBusy: false,
    screenshotGeneration: 0,
    lastFrameAt: 0,
    imageUrl: null,
    mutationPending: false,
    actionQueue: [], queueBytes: 0, draining: false, inputError: false,
    pendingPointer: null, pendingPointerClient: null, pendingPointerGeometry: null, lastPointer: null, lastPointerClient: null, lastPointerGeometry: null, lastPointerSentAt: 0, pendingWheel: null, pointerFlushTimer: null,
    captureBackoffMs: 0, captureRetryAt: 0, controlError: false,
    taskSubmitting: false, taskSubmitAttempt: null
  };

  function setText(id, value) {
    const node = $(id);
    if (node) node.textContent = value == null ? '' : String(value);
  }

  function updateTaskComposer() {
    const command = $('task-command')?.value || '';
    const attemptedOrigin = ui.taskSubmitAttempt?.payload?.goal === command ? ui.taskSubmitAttempt.payload.allowed_origins[0] : '';
    if (attemptedOrigin) { setText('task-site-hint', `Повторная отправка для сайта: ${attemptedOrigin}`); return; }
    const origin = safeOrigin(ui.current?.url);
    setText('task-site-hint', origin ? `Сайт: ${origin}` : 'Откройте HTTPS-сайт в браузере перед запуском');
  }

  function showNotice(message, type = '') {
    const notice = $('notice');
    notice.className = `notice${type ? ` ${type}` : ''}`;
    notice.textContent = message;
    notice.dataset.source = '';
  }

  function timeoutSignal(parentSignal) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error('Превышено время ожидания ответа сервера.')), REQUEST_TIMEOUT_MS);
    if (parentSignal) {
      if (parentSignal.aborted) controller.abort(parentSignal.reason);
      else parentSignal.addEventListener('abort', () => controller.abort(parentSignal.reason), { once: true });
    }
    return { signal: controller.signal, clear: () => clearTimeout(timer) };
  }

  async function fetchBounded(path, options = {}, retried = false) {
    const timed = timeoutSignal(options.signal);
    const headers = new Headers(options.headers || {});
    if (ui.csrf && !['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase())) headers.set('X-CSRF-Token', ui.csrf);
    try {
      const response = await fetch(`${API}${path}`, {
        ...options,
        headers,
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'follow',
        signal: timed.signal
      });
      if (response.redirected) throw new Error('Сессия оператора истекла. Войдите в панель и откройте экран снова.');
      const renewedCsrf = response.headers.get('X-CSRF-Token');
      if (renewedCsrf) ui.csrf = renewedCsrf;
      if (!retried && path !== '/operator/session' && (response.status === 401 || response.status === 403)) {
        const errorBody = await response.clone().json().catch(() => ({}));
        if (['UNAUTHORIZED', 'CSRF_INVALID'].includes(errorBody?.error?.code)) {
          const sessionResponse = await fetch(`${API}/operator/session`, {credentials: 'same-origin', cache: 'no-store', signal: timed.signal});
          const session = sessionResponse.ok ? await sessionResponse.json() : {};
          if (session.local_ui === true && session.csrf_token) {
            ui.csrf = session.csrf_token;
            return fetchBounded(path, options, true);
          }
        }
      }
      if (!response.ok) {
        let message = `Ошибка сервера (${response.status}).`;
        let code = '';
        if ((response.headers.get('content-type') || '').includes('application/json')) {
          try {
            const body = await response.json();
            message = body?.error?.message || body?.detail || message;
            code = body?.error?.code || '';
          } catch { /* Keep the readable status fallback. */ }
        }
        const error = new Error(message);
        error.code = code;
        error.status = response.status;
        throw error;
      }
      return response;
    } finally {
      timed.clear();
    }
  }

  async function getJson(path, signal) {
    const response = await fetchBounded(path, { method: 'GET', signal });
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) throw new Error('Сервер вернул неожиданный формат ответа.');
    return response.json();
  }

  function setOwnerState(data) {
    const previous = ui.current;
    const sameIdentity = previous && ['worker_instance_id', 'session_id', 'page_id'].every(key => previous[key] === data[key]);
    if (sameIdentity && Number.isFinite(previous.control_epoch) && Number.isFinite(data.control_epoch) && data.control_epoch < previous.control_epoch) return;
    if (previous && (!sameIdentity || previous.width !== data.width || previous.height !== data.height || previous.url !== data.url)) discardQueue('Страница или браузер изменились. Неотправленный ввод удалён; выберите поле снова.');
    const changed = !previous || previous.control_owner !== data.control_owner || previous.control_epoch !== data.control_epoch ||
      previous.available !== data.available || previous.width !== data.width || previous.height !== data.height || previous.url !== data.url;
    ui.current = data;
    updateTaskComposer();
    if (changed) {
      ui.screenshotGeneration += 1;
      ui.lastFrameAt = 0;
      ui.screenshotAbort?.abort();
    }
    const human = data.control_owner === 'human';
    if ((!human || !data.available) && previous?.control_owner === 'human') discardQueue('Управление изменилось. Неотправленный ввод удалён; передайте управление и повторите ввод.');
    $('remote-keyboard').disabled = !human || !data.available || !ui.csrf;
    const status = $('owner-status');
    status.className = 'status warning';
    setText('owner-label', human ? 'Вы управляете' : 'Просмотр');
    setText('control-owner-label', data.control_owner === 'human' ? '\u0420\u0443\u0447\u043d\u043e\u0435 \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435' : data.control_owner === 'agent' ? '\u0417\u0430\u0434\u0430\u0447\u0430 \u0443\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u0442 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u043e\u043c' : data.control_owner === 'none' ? '\u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0441\u0432\u043e\u0431\u043e\u0434\u043d\u043e' : '\u0412\u043b\u0430\u0434\u0435\u043b\u0435\u0446 \u043d\u0435 \u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d');
    updateViewerPill();
    updateControlButtons();
    const allowed = Boolean(data.available && human && ui.csrf && !ui.mutationPending);
    document.querySelectorAll('[data-mutate]').forEach((control) => { control.disabled = !allowed; });
    $('browser-image').classList.toggle('interactive', allowed);
    const safeUrl = safeOrigin(data.url);
    setText('browser-runtime', data.runtime_environment === 'docker' ? `${data.backend || 'Браузер'} · Docker / WSL2` : (data.backend || ''));
    setText('site-label', data.available ? (safeUrl || 'Браузер доступен') : 'Браузер недоступен');
    if (!ui.csrf && !ui.controlError) showNotice('Нет CSRF-токена операторской сессии. Обновите страницу и войдите в панель снова.', 'error');
    else if (!data.available && !ui.inputError && !ui.controlError) {
      showNotice(data.detail || 'Содержимое браузера сейчас недоступно.', 'warning');
      $('notice').dataset.source = 'state';
    } else if ($('notice').dataset.source === 'state') showNotice('');
  }

  function updateViewerPill(force = '') {
    const status = $('owner-status');
    if (force === 'bad' || !ui.current?.available) { status.className = 'status bad'; return; }
    status.className = ui.lastFrameAt && Date.now() - ui.lastFrameAt <= 4000 ? 'status good' : 'status warning';
  }

  function safeOrigin(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password ? url.origin : null;
    } catch { return null; }
  }

  function releaseScreenshot() {
    ui.screenshotAbort?.abort();
    ui.screenshotAbort = null;
    ui.screenshotBusy = false;
    ui.captureBackoffMs = 0;
    ui.captureRetryAt = 0;
    ui.screenshotGeneration += 1;
  }

  async function loadScreenshot() {
    if (!ui.current?.available || ui.hidden || ui.screenshotBusy || Date.now() < ui.captureRetryAt) return;
    ui.screenshotBusy = true;
    const generation = ui.screenshotGeneration;
    const controller = new AbortController();
    ui.screenshotAbort = controller;
    try {
      const response = await fetchBounded('/desktop/screenshot', { method: 'GET', signal: controller.signal });
      if (generation !== ui.screenshotGeneration || ui.hidden || !ui.current?.available) return;
      const type = response.headers.get('content-type') || '';
      if (!type.toLowerCase().startsWith('image/png')) throw new Error('Сервер не вернул снимок PNG.');
      const statedSize = Number(response.headers.get('content-length'));
      if (statedSize > 16 * 1024 * 1024) throw new Error('Снимок экрана превышает допустимый размер.');
      const blob = await response.blob();
      if (generation !== ui.screenshotGeneration || ui.hidden || !ui.current?.available) return;
      if (!blob.size) throw new Error('Получен пустой снимок экрана.');
      ui.captureBackoffMs = 0;
      ui.captureRetryAt = 0;
      if (blob.size > 16 * 1024 * 1024) throw new Error('Снимок экрана превышает допустимый размер.');
      const nextUrl = URL.createObjectURL(blob);
      const image = $('browser-image');
      const oldUrl = ui.imageUrl;
      image.onload = () => {
        if (generation !== ui.screenshotGeneration || ui.imageUrl !== nextUrl) return;
        ui.lastFrameAt = Date.now();
        updateViewerPill();
        setText('image-size', `${image.naturalWidth} × ${image.naturalHeight}`);
      };
      image.onerror = () => {
        if (ui.imageUrl !== nextUrl) return;
        URL.revokeObjectURL(nextUrl);
        ui.imageUrl = null;
        ui.lastFrameAt = 0;
        updateViewerPill('bad');
        showNotice('Не удалось показать снимок браузера.', 'error');
      };
      ui.imageUrl = nextUrl;
      image.src = nextUrl;
      if (oldUrl) URL.revokeObjectURL(oldUrl);
      image.hidden = false;
      $('empty-state').hidden = true;
    } catch (error) {
      if (!controller.signal.aborted && error.code === 'CAPTURE_BUSY') {
        ui.captureBackoffMs = Math.min(ui.captureBackoffMs ? ui.captureBackoffMs * 2 : 150, 1000);
        ui.captureRetryAt = Date.now() + ui.captureBackoffMs;
      } else if (!controller.signal.aborted) {
        ui.lastFrameAt = 0;
        updateViewerPill('bad');
        showNotice(error.message || '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0441\u043d\u0438\u043c\u043e\u043a \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.', 'error');
      }
    } finally {
      if (ui.screenshotAbort === controller) ui.screenshotAbort = null;
      ui.screenshotBusy = false;
    }
  }

  async function refreshState() {
    if (ui.polling || ui.hidden) return;
    ui.polling = true;
    try {
      const data = await getJson('/desktop/state');
      if (ui.hidden) return;
      if (!data || typeof data !== 'object') throw new Error('Сервер вернул некорректное состояние браузера.');
      setOwnerState(data);
      if (data.available) await loadScreenshot();
      else showEmpty('Браузер пока недоступен', data.detail || 'Ожидается запуск браузера.');
    } catch (error) {
      if (!ui.hidden) {
        setText('owner-label', 'Нет связи');
        $('owner-status').className = 'status bad';
        ui.lastFrameAt = 0;
        setText('control-owner-label', '\u041d\u0435\u0442 \u0441\u0432\u044f\u0437\u0438 \u0441 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u043e\u043c');
        showNotice(error.message || 'Не удалось получить состояние браузера.', 'error');
      }
    } finally {
      ui.polling = false;
      schedulePoll(ui.current?.control_owner === 'human' ? HUMAN_POLL_MS : VIEWER_POLL_MS);
    }
  }

  function showEmpty(title, detail) {
    const image = $('browser-image');
    image.hidden = true;
    image.onload = null;
    image.onerror = null;
    image.removeAttribute('src');
    if (ui.imageUrl) URL.revokeObjectURL(ui.imageUrl);
    ui.imageUrl = null;
    setText('image-size', '');
    const empty = $('empty-state');
    empty.replaceChildren();
    const strong = document.createElement('strong');
    strong.textContent = title;
    const copy = document.createElement('span');
    copy.textContent = detail;
    empty.append(strong, copy);
    empty.hidden = false;
  }

  function schedulePoll(delay) {
    updateViewerPill();
    clearTimeout(ui.timer);
    if (!ui.hidden) ui.timer = setTimeout(refreshState, delay);
  }

  function updateControlButtons() {
    const owner = ui.current?.control_owner;
    const known = ['agent', 'human', 'none'].includes(owner);
    const ready = Boolean(ui.current?.available && known && ui.csrf && !ui.mutationPending);
    $('takeover').disabled = !ready || owner === 'human';
    $('release-control').disabled = !ready || owner !== 'human';
  }

  function setMutationPending(value) {
    ui.mutationPending = value;
    updateControlButtons();
    const allowed = Boolean(ui.current?.available && ui.current.control_owner === 'human' && ui.csrf && !value);
    document.querySelectorAll('[data-mutate]').forEach((control) => { control.disabled = !allowed; });
    $('browser-image').classList.toggle('interactive', allowed);
  }

  async function mutate(action) {
    if (ui.mutationPending || !ui.current?.available || ui.current.control_owner !== 'human' || !ui.csrf) return false;
    const epoch = ui.current.control_epoch;
    if (epoch == null) { showNotice('Нет актуальной версии управления. Обновите состояние браузера.', 'error'); return false; }
    setMutationPending(true);
    let succeeded = false;
    try {
      const response = await fetchBounded('/desktop/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ control_epoch: epoch, action })
      });
      const type = response.headers.get('content-type') || '';
      const result = type.includes('application/json') ? await response.json() : {};
      if (result?.ok === false || result?.error) throw new Error(result.error?.message || result.error || 'Действие отклонено сервером.');
      if (result?.outcome === 'unknown') throw new Error('Результат действия неизвестен. Проверьте поле перед повтором.');
      if (ui.current && ['worker_instance_id', 'session_id', 'page_id'].some(key => result[key] != null && ui.current[key] != null && result[key] !== ui.current[key])) {
        ui.current = null; discardQueue(); throw new Error('Браузер изменился. Выберите поле снова.');
      }
      if (Number.isFinite(result.control_epoch) && ui.current && result.control_epoch >= ui.current.control_epoch) ui.current = { ...ui.current, control_epoch: result.control_epoch };
      showNotice(result.detail || 'Действие отправлено браузеру.');
      succeeded = true;
    } catch (error) {
      ui.inputError = true;
      showNotice(action.kind === 'type_text' ? 'Не удалось отправить текст. Проверьте поле браузера и повторите ввод.' : (error.message || 'Не удалось выполнить действие.'), 'error');
    } finally {
      setMutationPending(false);
      schedulePoll(0);
      if (!ui.draining && hasPendingInput()) void drainQueue();
    }
    return succeeded;
  }

  function discardQueue(message) {
    const hadQueued = ui.actionQueue.length > 0 || Boolean($('remote-keyboard').value) ||
      Boolean(ui.pendingWheel && (ui.pendingWheel.dx || ui.pendingWheel.dy));
    ui.actionQueue = []; ui.queueBytes = 0;
    ui.pendingPointer = null; ui.pendingPointerClient = null; ui.pendingPointerGeometry = null; ui.pendingWheel = null;
    clearTimeout(ui.pointerFlushTimer); ui.pointerFlushTimer = null;
    const input = $('remote-keyboard'); input.value = '';
    if (hadQueued && message) { ui.inputError = true; showNotice(message, 'error'); }
  }
  function queueAction(action) {
    if (ui.inputError || !ui.current?.available || ui.current.control_owner !== 'human' || !ui.csrf || ui.hidden) return;
    const bytes = action.kind === 'type_text' ? new TextEncoder().encode(action.text).length : 0;
    const last = ui.actionQueue[ui.actionQueue.length - 1];
    const combine = action.kind === 'type_text' && last?.kind === 'type_text';
    if ((!combine && ui.actionQueue.length >= 64) || ui.queueBytes + bytes > 8192) {
      discardQueue(); ui.inputError = true; showNotice('Очередь ввода переполнена. Проверьте поле браузера и повторите неотправленный текст.', 'error'); return;
    }
    if (combine) last.text += action.text; else ui.actionQueue.push(action);
    ui.queueBytes += bytes;
    if (!ui.draining && !ui.mutationPending) void drainQueue();
  }
  function hasPendingInput() {
    return ui.actionQueue.length > 0 || ui.pendingPointer !== null ||
      Boolean(ui.pendingWheel && (ui.pendingWheel.dx || ui.pendingWheel.dy));
  }
  function boundedDelta(value) {
    return Math.max(-MAX_SCROLL, Math.min(MAX_SCROLL, Math.round(Number(value) || 0)));
  }
  function takeWheelAction() {
    const pending = ui.pendingWheel;
    if (!pending || (!pending.dx && !pending.dy)) { ui.pendingWheel = null; return null; }
    const dx = boundedDelta(pending.dx), dy = boundedDelta(pending.dy);
    pending.dx -= dx; pending.dy -= dy;
    const action = { kind: 'scroll', dx, dy, x: pending.x, y: pending.y };
    if (!pending.dx && !pending.dy) ui.pendingWheel = null;
    return action;
  }
  async function drainQueue() {
    if (ui.draining || ui.mutationPending) return;
    ui.draining = true;
    try {
      while (hasPendingInput()) {
        if (!ui.current?.available || ui.current.control_owner !== 'human' || ui.hidden) { discardQueue('Управление изменилось. Неотправленный ввод удалён.'); break; }
        let action = null;
        if (ui.actionQueue.length) {
          action = ui.actionQueue.shift();
          if (action.kind === 'type_text') ui.queueBytes -= new TextEncoder().encode(action.text).length;
        } else if (ui.pendingWheel && (ui.pendingWheel.dx || ui.pendingWheel.dy)) {
          action = takeWheelAction();
        } else if (ui.pendingPointer) {
          const waitMs = Math.max(0, POINTER_INTERVAL_MS - (Date.now() - ui.lastPointerSentAt));
          if (waitMs) await new Promise(resolve => setTimeout(resolve, waitMs));
          if (!ui.current?.available || ui.current.control_owner !== 'human' || ui.hidden) continue;
          action = { kind: 'mouse_move', ...ui.pendingPointer };
          ui.pendingPointer = null;
          ui.lastPointerClient = ui.pendingPointerClient;
          ui.lastPointerGeometry = ui.pendingPointerGeometry;
          ui.pendingPointerClient = null;
          ui.pendingPointerGeometry = null;
          if (ui.lastPointer && ui.lastPointer.x === action.x && ui.lastPointer.y === action.y) continue;
          ui.lastPointer = { x: action.x, y: action.y };
          ui.lastPointerSentAt = Date.now();
        }
        if (!action) break;
        if (!await mutate(action)) { discardQueue(); break; }
        if (action.kind === 'click') {
          ui.lastPointer = { x: action.x, y: action.y };
          if (ui.pendingPointer?.x === action.x && ui.pendingPointer?.y === action.y) ui.pendingPointer = null;
        }
      }
    } finally { ui.draining = false; }
  }

  async function changeControl(operation) {
    const owner = ui.current?.control_owner;
    if (ui.mutationPending || !ui.current?.available || !ui.csrf ||
        !['agent', 'human', 'none'].includes(owner) ||
        (operation === 'takeover' ? owner === 'human' : owner !== 'human')) return;
    discardQueue('Неотправленный ввод удалён при смене управления.');
    setMutationPending(true);
    try {
      await fetchBounded(`/profiles/default/${operation}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
      const confirmed = await getJson('/desktop/state');
      setOwnerState(confirmed);
      const expectedOwner = operation === 'takeover' ? 'human' : 'none';
      if (confirmed.available !== true || confirmed.control_owner !== expectedOwner) {
        throw new Error(`Сервис не подтвердил «${expectedOwner}». Проверьте состояние браузера и повторите действие.`);
      }
      ui.controlError = false;
      ui.inputError = false;
      showNotice(operation === 'takeover' ? 'Управление передано вам.' : 'Управление возвращено.');
    } catch (error) {
      ui.current = null; discardQueue();
      ui.controlError = true;
      const hint = location.port === '8080' ? ' Откройте панель по адресу http://localhost:8090.' : '';
      showNotice((error.message || 'Не удалось изменить управление.') + hint, 'error');
    } finally {
      setMutationPending(false);
      schedulePoll(0);
    }
  }

  const taskLabels = {
    queued: 'В очереди', running: 'Выполняется', pause_requested: 'Пауза запрошена',
    paused: 'Приостановлена', waiting_human: 'Нужно внимание', waiting_approval: 'Ожидает подтверждения',
    recovering: 'Нужно проверить состояние', succeeded: 'Завершена', failed: 'Ошибка',
    cancelled: 'Отменена', expired: 'Истекло время'
  };
  const terminalTasks = new Set(['succeeded', 'done', 'completed', 'failed', 'cancelled', 'canceled', 'expired']);

  const phaseLabels = {
    checking_dependencies: '\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0441\u0435\u0440\u0432\u0438\u0441\u043e\u0432',
    submitting_prompt: '\u041f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0430 \u0437\u0430\u043f\u0440\u043e\u0441\u0430',
    cookie_sync_required: '\u041f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0430 cookies',
    authentication_required: '\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0432\u0445\u043e\u0434\u0430',
    capability_unavailable: '\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0433\u043e \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u044f \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u044b',
    action_outcome_unknown: '\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f',
    verification_required: '\u041e\u0436\u0438\u0434\u0430\u0435\u0442 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430',
    completed_unverified: '\u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430 \u0431\u0435\u0437 \u043d\u0435\u0437\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0439 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438',
    submitted: '\u0417\u0430\u043f\u0440\u043e\u0441 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d',
    task_deadline_exceeded: '\u0421\u0440\u043e\u043a \u0437\u0430\u0434\u0430\u0447\u0438 \u0438\u0441\u0442\u0451\u043a',
    restart_recovery: '\u0412\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u043f\u043e\u0441\u043b\u0435 \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u043a\u0430',
    recovery_reviewed: '\u0421\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d\u043e',
    answer_required: '\u041e\u0436\u0438\u0434\u0430\u0435\u0442 \u0432\u0430\u0448\u0435\u0433\u043e \u043e\u0442\u0432\u0435\u0442\u0430',
    autonomy_no_progress: '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c \u0431\u0435\u0437 \u0443\u0442\u043e\u0447\u043d\u0435\u043d\u0438\u044f',
  };

  const activityLabels = {
    checking_dependencies: '\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0441\u0435\u0440\u0432\u0438\u0441\u043e\u0432',
    syncing_cookies: '\u041f\u0435\u0440\u0435\u043d\u043e\u0441 cookies',
    recovering_browser: '\u0410\u0432\u0442\u043e\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430',
    observing: '\u0410\u043d\u0430\u043b\u0438\u0437 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u044b',
    thinking: '\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u0437\u0430\u0434\u0430\u0447\u0438 \u043c\u043e\u0434\u0435\u043b\u044c\u044e',
    executing: '\u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f',
    verifying: '\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430',
  };

  function safeCode(value) {
    return typeof value === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(value) ? value : '';
  }

  function phaseText(phase) {
    if (!phase) return '\u042d\u0442\u0430\u043f \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d';
    return phaseLabels[phase] || safeCode(phase) || '\u042d\u0442\u0430\u043f \u043d\u0435 \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u043d';
  }

  function lastFailedActionKind(events, code) {
    for (const event of [...(events || [])].reverse()) {
      if (event.type === 'action.failed' && (!code || event.code === code)) return event.kind || '';
    }
    return '';
  }

  function stopReason(task, events = []) {
    return window.taskStatusCopy?.reason(task, events) || '';
  }

  function recoveryActivity(worker) {
    if (worker.recovery_state === 'recovering') return ['warning', '\u0412\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u043d\u043e\u0435 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.'];
    if (worker.recovery_state === 'backoff') {
      const seconds = Number.isFinite(worker.recovery_retry_after_seconds) ? Math.ceil(worker.recovery_retry_after_seconds) : null;
      return ['warning', seconds === null ? '\u0410\u0432\u0442\u043e\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u0436\u0434\u0451\u0442 \u043f\u0435\u0440\u0435\u0434 \u043d\u043e\u0432\u043e\u0439 \u043f\u043e\u043f\u044b\u0442\u043a\u043e\u0439.' : `\u0410\u0432\u0442\u043e\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435: \u043f\u043e\u0432\u0442\u043e\u0440 \u0447\u0435\u0440\u0435\u0437 ${seconds} \u0441.`];
    }
    if (worker.recovery_state === 'exhausted') return ['bad', '\u0410\u0432\u0442\u043e\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u043e: \u043b\u0438\u043c\u0438\u0442 \u043f\u043e\u043f\u044b\u0442\u043e\u043a \u0438\u0441\u0447\u0435\u0440\u043f\u0430\u043d.'];
    if (worker.recovery_state === 'blocked' || worker.recovery_needed === true) {
      const why = {
        canonical_page_closed: '\u041e\u0441\u043d\u043e\u0432\u043d\u0430\u044f \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0430 \u0437\u0430\u043a\u0440\u044b\u043b\u0430\u0441\u044c.',
        browser_disconnected: '\u041f\u043e\u0442\u0435\u0440\u044f\u043d\u043e \u0441\u043e\u0435\u0434\u0438\u043d\u0435\u043d\u0438\u0435 \u0441 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u043e\u043c.',
        operation_in_progress: '\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442 \u0434\u0440\u0443\u0433\u0443\u044e \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044e.',
        resource_violation: '\u041f\u0440\u0435\u0432\u044b\u0448\u0435\u043d \u043b\u0438\u043c\u0438\u0442 \u0440\u0435\u0441\u0443\u0440\u0441\u043e\u0432 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.',
        egress_unavailable: '\u041d\u0435 \u0433\u043e\u0442\u043e\u0432 \u0441\u0435\u0442\u0435\u0432\u043e\u0439 \u043c\u0430\u0440\u0448\u0440\u0443\u0442.',
        attempts_exhausted: '\u041b\u0438\u043c\u0438\u0442 \u043f\u043e\u043f\u044b\u0442\u043e\u043a \u0438\u0441\u0447\u0435\u0440\u043f\u0430\u043d.',
        teardown_unconfirmed: '\u0421\u0435\u0440\u0432\u0438\u0441 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u043b \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u0435 \u0441\u0442\u0430\u0440\u043e\u0433\u043e \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.',
        launch_failed: '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c \u0431\u0440\u0430\u0443\u0437\u0435\u0440.',
        not_recoverable: '\u042d\u0442\u043e \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435 \u043d\u0435\u043b\u044c\u0437\u044f \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438.',
        context_close_failed: '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u0435 \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\u0430 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.',
        context_close_timeout: '\u0418\u0441\u0442\u0451\u043a\u043b\u043e \u0432\u0440\u0435\u043c\u044f \u043e\u0436\u0438\u0434\u0430\u043d\u0438\u044f \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442\u0430.',
        driver_stop_failed: '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c \u0434\u0440\u0430\u0439\u0432\u0435\u0440 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430.',
        driver_stop_timeout: '\u0418\u0441\u0442\u0451\u043a\u043b\u043e \u0432\u0440\u0435\u043c\u044f \u043e\u0436\u0438\u0434\u0430\u043d\u0438\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f \u0434\u0440\u0430\u0439\u0432\u0435\u0440\u0430.',
        pending_page_close: '\u0417\u0430\u043a\u0440\u044b\u0442\u0438\u0435 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u044b \u0435\u0449\u0451 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u043e.',
        browser_process_alive: '\u0421\u0442\u0430\u0440\u044b\u0439 \u043f\u0440\u043e\u0446\u0435\u0441\u0441 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u0430 \u0432\u0441\u0451 \u0435\u0449\u0451 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442.',
      }[worker.recovery_reason] || '\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u043e\u0436\u0438\u0434\u0430\u0435\u0442 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f.';
      return ['warning', `\u0410\u0432\u0442\u043e\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435: ${why}`];
    }
    return worker.ready === true
      ? ['good', '\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u0433\u043e\u0442\u043e\u0432; \u0430\u0432\u0442\u043e\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u043d\u0435 \u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f.']
      : ['bad', '\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u043d\u0435 \u0433\u043e\u0442\u043e\u0432; \u0441\u0435\u0440\u0432\u0438\u0441 \u043f\u043e\u043a\u0430 \u043d\u0435 \u043f\u0435\u0440\u0435\u0434\u0430\u043b \u0441\u0442\u0430\u0442\u0443\u0441 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f.'];
  }

  function renderServiceStatus(data) {
    const model = data?.model || {};
    const worker = data?.worker || {};
    const [recoveryState, recoveryText] = recoveryActivity(worker);
    const modelText = model.ready === true
      ? '\u041c\u043e\u0434\u0435\u043b\u044c: \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430.'
      : '\u041c\u043e\u0434\u0435\u043b\u044c: \u043d\u0435 \u0433\u043e\u0442\u043e\u0432\u0430.';
    const workerText = worker.ready === true
      ? '\u0411\u0440\u0430\u0443\u0437\u0435\u0440: \u0433\u043e\u0442\u043e\u0432.'
      : '\u0411\u0440\u0430\u0443\u0437\u0435\u0440: \u043d\u0435 \u0433\u043e\u0442\u043e\u0432.';
    const node = $('service-status');
    node.dataset.state = model.ready === true && worker.ready === true ? recoveryState : (recoveryState === 'warning' ? 'warning' : 'bad');
    node.textContent = `${modelText} ${workerText} ${recoveryText}`;
  }

  function safeSummary(value, limit = 240) {
    if (typeof value !== 'string') return '';
    return value.replace(/[\u0000-\u001f\u007f]/g, ' ')
      .replace(/https?:\/\/[^\s]+/gi, '[ссылка]')
      .replace(/\b(?:authorization|cookie|token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+/gi, '[скрыто]')
      .replace(/\s+/g, ' ').trim().slice(0, limit);
  }

  function selectCurrentTask(tasks) {
    return tasks.find((item) => item && !terminalTasks.has(item.status)) || tasks[0] || null;
  }

  function taskKey(task) {
    return task ? `${task.task_id || ''}:${task.version ?? ''}` : '';
  }

  function safeTaskEvents(data) {
    const events = Array.isArray(data?.events) ? data.events : [];
    const eventTypes = new Set(['action.planned', 'action.applied', 'action.failed', 'cookie.sync_completed', 'task.activity']);
    return events.slice(-100).map((event) => {
      const payload = event?.payload && typeof event.payload === 'object' ? event.payload : {};
      const counts = payload.counts && typeof payload.counts === 'object' ? payload.counts : {};
      return {
        eventId: Number.isInteger(event?.event_id) && event.event_id > 0 ? event.event_id : null,
        type: eventTypes.has(event?.event_type) ? event.event_type : '',
        id: safeCode(payload.action_id),
        kind: safeCode(payload.kind),
        code: safeCode(payload.code),
        reason: safeCode(payload.reason),
        outcome: safeCode(payload.outcome),
        stage: safeCode(payload.stage),
        step: Number.isInteger(payload.step) && payload.step >= 0 ? payload.step : null,
        counts: {
          total: Number.isInteger(counts.total) ? counts.total : null,
          applied: Number.isInteger(counts.applied) ? counts.applied : null,
          failed: Number.isInteger(counts.failed) ? counts.failed : null,
        },
      };
    });
  }

  async function loadTaskEvents(task) {
    if (!task || !task.task_id) return;
    const key = String(task.task_id);
    if (ui.taskEventsLoading) return;
    if (ui.taskEventKey !== key) {
      ui.taskEventKey = key;
      ui.taskEventSummary = [];
      ui.taskEventCursor = 0;
    }
    ui.taskEventsLoading = true;
    try {
      let pages = 0;
      while (pages < 5) {
        const data = await getJson(`/tasks/${encodeURIComponent(task.task_id)}/events?after_event_id=${ui.taskEventCursor}&limit=100`);
        if (ui.currentTask?.task_id !== key || ui.taskEventKey !== key) return;
        const events = Array.isArray(data?.events) ? data.events : [];
        const summaries = safeTaskEvents(data);
        ui.taskEventSummary.push(...summaries);
        if (ui.taskEventSummary.length > 100) ui.taskEventSummary.splice(0, ui.taskEventSummary.length - 100);
        const lastId = events.length ? events[events.length - 1]?.event_id : null;
        if (Number.isInteger(lastId) && lastId > ui.taskEventCursor) ui.taskEventCursor = lastId;
        pages += 1;
        if (events.length < 100 || !Number.isInteger(lastId)) break;
      }
      renderTask({ tasks: [ui.currentTask] });
    } catch {
      // The stable error code still has a localized fallback if event details are unavailable.
    } finally {
      ui.taskEventsLoading = false;
    }
  }

  function actionLabel(kind) {
    const labels = {
      navigate: '\u043f\u0435\u0440\u0435\u0445\u043e\u0434 \u043f\u043e \u0430\u0434\u0440\u0435\u0441\u0443',
      click: '\u043d\u0430\u0436\u0430\u0442\u0438\u0435',
      type_text: '\u0432\u0432\u043e\u0434 \u0442\u0435\u043a\u0441\u0442\u0430',
      press_key: '\u043d\u0430\u0436\u0430\u0442\u0438\u0435 \u043a\u043b\u0430\u0432\u0438\u0448\u0438',
      gemini_refresh_session: '\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u044f Gemini',
      gemini_submit_prompt: '\u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u0437\u0430\u043f\u0440\u043e\u0441\u0430 Gemini',
      history_back: '\u0432\u043e\u0437\u0432\u0440\u0430\u0442 \u043d\u0430 \u043f\u0440\u0435\u0434\u044b\u0434\u0443\u0449\u0443\u044e \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0443',
      scroll: '\u043f\u0440\u043e\u043a\u0440\u0443\u0442\u043a\u0430',
      wait: '\u043e\u0436\u0438\u0434\u0430\u043d\u0438\u0435',
    };
    return labels[kind] || '';
  }

  function taskEvidence(task, events) {
    const activity = [...events].reverse().find((event) => event.type === 'task.activity' && activityLabels[event.stage]);
    if (task.status === 'running' && !task.error && activity) {
      const step = activity.step === null ? '' : ` (\u0448\u0430\u0433 ${activity.step})`;
      return `\u0421\u0435\u0440\u0432\u0438\u0441 \u0441\u0435\u0439\u0447\u0430\u0441: ${activityLabels[activity.stage]}${step}.`;
    }
    const applied = [...events].reverse().find((event) => event.type === 'action.applied');
    const planned = [...events].reverse().find((event) => event.type === 'action.planned');
    const failed = [...events].reverse().find((event) => event.type === 'action.failed');
    const settledIds = new Set(events.filter((event) => ['action.applied', 'action.failed'].includes(event.type) && event.id).map((event) => event.id));
    if (planned && planned.id && !settledIds.has(planned.id) && task.status === 'running') {
      const label = actionLabel(planned.kind);
      if (label) return `\u0421\u0435\u0439\u0447\u0430\u0441 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f: ${label}.`;
    }
    const label = actionLabel(applied?.kind);
    if (label) return `\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u043e\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435: ${label}.`;
    if (failed && actionLabel(failed.kind)) return `\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043d\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043e: ${actionLabel(failed.kind)}.`;
    const sync = [...events].reverse().find((event) => event.type === 'cookie.sync_completed');
    if (sync?.counts?.total !== null && sync?.counts?.total !== undefined) {
      return `\u0421\u043e\u0431\u044b\u0442\u0438\u0435: cookies \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043b\u0435\u043d\u044b (${sync.counts.applied ?? 0} \u0438\u0437 ${sync.counts.total}, \u043e\u0448\u0438\u0431\u043e\u043a: ${sync.counts.failed ?? 0}).`;
    }
    if (task.status === 'running') return '\u0417\u0430\u0434\u0430\u0447\u0430 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f; \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0445 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0439 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442.';
    return '\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0445 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0439 \u043d\u0435\u0442.';
  }

  function renderTask(data) {
    const tasks = Array.isArray(data?.tasks) ? data.tasks : [];
    const task = selectCurrentTask(tasks);
    setText('task-heading', '\u0422\u0435\u043a\u0443\u0449\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430');
    const reasonNode = $('task-stop-reason');
    if (!task) {
      ui.currentTask = null;
      ui.currentTaskKey = '';
      ui.taskEventKey = '';
      ui.taskEventSummary = [];
      ui.taskEventCursor = 0;
      setText('task-status', '\u0417\u0430\u0434\u0430\u0447 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442');
      setText('task-phase', '\u042d\u0442\u0430\u043f \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d');
      setText('task-progress-count', '');
      setText('task-goal', '');
      setText('task-detail', '');
      reasonNode.hidden = true;
      reasonNode.querySelector('span').textContent = '';
      renderTaskControls(null);
      return;
    }
    ui.currentTask = task;
    ui.currentTaskKey = taskKey(task);
    setText('task-status', taskLabels[task.status] || '\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e\u0435 \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435');
    const events = ui.taskEventKey === String(task.task_id || '') ? ui.taskEventSummary : [];
    const activity = [...events].reverse().find((event) => event.type === 'task.activity' && activityLabels[event.stage]);
    setText('task-phase', task.status === 'running' && !task.error && activity
      ? `\u042d\u0442\u0430\u043f: ${activityLabels[activity.stage]}`
      : `\u042d\u0442\u0430\u043f: ${phaseText(task.phase)}`);
    const steps = Number.isInteger(task.step_count) && task.step_count >= 0 ? task.step_count : 0;
    const maxSteps = Number.isInteger(task.spec?.limits?.max_steps) && task.spec.limits.max_steps > 0
      ? task.spec.limits.max_steps : null;
    setText('task-progress-count', maxSteps === null
      ? `\u0428\u0430\u0433\u043e\u0432 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043e: ${steps}`
      : `\u0428\u0430\u0433\u0438: ${steps} \u0438\u0437 ${maxSteps}`);
    setText('task-goal', safeSummary(task.spec?.goal, 180) || '\u0426\u0435\u043b\u044c \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u0430');
    const resultSummary = terminalTasks.has(task.status) ? safeSummary(task.result?.summary) : '';
    setText('task-detail', resultSummary || taskEvidence(task, events));
    const reason = stopReason(task, events);
    reasonNode.hidden = !reason;
    reasonNode.querySelector('span').textContent = reason;
    renderTaskControls(task);
  }

  function renderTaskControls(task) {
    const container = $('task-actions');
    if (!container) return;
    container.replaceChildren();
    if (!task?.task_id || ui.taskControlPending) return;
    const add = (label, action, danger = false) => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = `task-action${danger ? ' danger' : ''}`; button.textContent = label;
      button.addEventListener('click', () => { void controlTask(task, action, label); });
      container.append(button);
    };
    if (task.status === 'running') add('Пауза', 'pause');
    if (task.status === 'paused' || (task.status === 'waiting_human' && task.phase !== 'answer_required')) add('Продолжить', 'resume');
    if (task.status === 'recovering') add('Проверить восстановление', 'review-recovery');
    if (['queued', 'running', 'pause_requested', 'paused', 'waiting_human', 'waiting_approval', 'waiting_pause'].includes(task.status)) add('Отменить', 'cancel', true);
  }

  async function controlTask(task, action, label) {
    if (ui.taskControlPending || !task?.task_id || terminalTasks.has(task.status) || task.status === 'recovering' && action !== 'review-recovery') return;
    ui.taskControlPending = true;
    renderTaskControls(null);
    try {
      const id = encodeURIComponent(task.task_id);
      const latestResponse = await getJson(`/tasks/${id}`);
      const latest = latestResponse.task || latestResponse;
      if (!latest || latest.task_id !== task.task_id || terminalTasks.has(latest.status) || latest.status === 'expired') throw new Error('Статус задачи изменился; обновите экран и проверьте доступные действия.');
      const allowed = action === 'pause' ? latest.status === 'running'
        : action === 'resume' ? latest.status === 'paused' || (latest.status === 'waiting_human' && latest.phase !== 'answer_required')
        : action === 'review-recovery' ? latest.status === 'recovering'
        : action === 'takeover' ? ['running', 'pause_requested', 'paused', 'waiting_human', 'waiting_approval'].includes(latest.status)
        : action === 'cancel' ? ['queued', 'running', 'pause_requested', 'paused', 'waiting_human', 'waiting_approval', 'waiting_pause'].includes(latest.status)
        : false;
      if (!allowed) throw new Error('Для текущего статуса задачи эта команда недоступна.');
      const expectedVersion = latest.version ?? latest.expected_version;
      if (!Number.isInteger(expectedVersion)) throw new Error('Версия задачи недоступна. Обновите экран перед действием.');
      await fetchBounded(`/tasks/${id}/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_version: expectedVersion }) });
      showNotice(`Команда «${label}» отправлена задаче.`);
      ui.lastTaskPoll = 0;
      await refreshTask();
    } catch (error) {
      showNotice(error.message || 'Не удалось выполнить команду задачи.', 'error');
    } finally {
      ui.taskControlPending = false;
      renderTaskControls(ui.currentTask);
    }
  }

  async function refreshTask() {
    if (ui.taskPolling || ui.hidden) return;
    const wait = TASK_POLL_MS - (Date.now() - ui.lastTaskPoll);
    if (ui.lastTaskPoll && wait > 0) {
      clearTimeout(ui.taskTimer);
      ui.taskTimer = setTimeout(refreshTask, wait);
      return;
    }
    ui.taskTimer = null;
    ui.taskPolling = true;
    ui.lastTaskPoll = Date.now();
    try {
      const data = await getJson('/tasks');
      renderTask(data);
      void loadTaskEvents(ui.currentTask);
    } catch {
      setText('task-status', '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435 \u0437\u0430\u0434\u0430\u0447\u0438');
      setText('task-phase', '');
      setText('task-progress-count', '');
      setText('task-goal', '');
      setText('task-detail', '');
      $('task-stop-reason').hidden = true;
      $('task-stop-reason').querySelector('span').textContent = '';
    } finally {
      ui.taskPolling = false;
      clearTimeout(ui.taskTimer);
      if (!ui.hidden) ui.taskTimer = setTimeout(refreshTask, TASK_POLL_MS);
    }
  }

  async function refreshServices() {
    if (ui.servicePolling || ui.hidden) return;
    ui.servicePolling = true;
    try {
      renderServiceStatus(await getJson('/status'));
    } catch {
      const node = $('service-status');
      node.dataset.state = 'bad';
      node.textContent = '\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043c\u043e\u0434\u0435\u043b\u044c \u0438 \u0431\u0440\u0430\u0443\u0437\u0435\u0440. \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435.';
    } finally {
      ui.servicePolling = false;
      clearTimeout(ui.serviceTimer);
      if (!ui.hidden) ui.serviceTimer = setTimeout(refreshServices, SERVICE_POLL_MS);
    }
  }

  $('takeover').addEventListener('click', () => { void changeControl('takeover'); });
  $('release-control').addEventListener('click', () => { void changeControl('release-control'); });

  function mapPointer(event) {
    const image = $('browser-image');
    const rect = image.getBoundingClientRect();
    const width = Number(ui.current?.width) || image.naturalWidth;
    const height = Number(ui.current?.height) || image.naturalHeight;
    if (!rect.width || !rect.height || !width || !height) return null;
    const x = Math.max(0, Math.min(width - 1, Math.round((event.clientX - rect.left) * width / rect.width)));
    const y = Math.max(0, Math.min(height - 1, Math.round((event.clientY - rect.top) * height / rect.height)));
    return { x, y };
  }
  function pointerGeometry() {
    const rect = $('browser-image').getBoundingClientRect();
    return { left: rect.left, top: rect.top, width: rect.width, height: rect.height, screenWidth: Number(ui.current?.width) || 0, screenHeight: Number(ui.current?.height) || 0 };
  }
  function samePointerGeometry(first, second) {
    if (!first || !second) return false;
    return ['left', 'top', 'width', 'height'].every(key => Math.abs(first[key] - second[key]) < 0.25) &&
      first.screenWidth === second.screenWidth && first.screenHeight === second.screenHeight;
  }
  function mapWheelPointer(event) {
    const geometry = pointerGeometry();
    const candidates = [
      [ui.pendingPointer, ui.pendingPointerClient, ui.pendingPointerGeometry],
      [ui.lastPointer, ui.lastPointerClient, ui.lastPointerGeometry],
    ];
    for (const [point, client, priorGeometry] of candidates) {
      if (point && client && samePointerGeometry(geometry, priorGeometry) && Math.abs(event.clientX - client.x) <= 2 && Math.abs(event.clientY - client.y) <= 2) return point;
    }
    return mapPointer(event);
  }
  function schedulePointerFlush() {
    if (ui.pointerFlushTimer !== null) return;
    const delay = Math.max(16, POINTER_INTERVAL_MS - (Date.now() - ui.lastPointerSentAt));
    ui.pointerFlushTimer = setTimeout(() => {
      ui.pointerFlushTimer = null;
      if (!ui.draining && !ui.mutationPending) void drainQueue();
    }, delay);
  }

  $('browser-image').addEventListener('click', (event) => {
    if (!ui.current?.available || ui.current.control_owner !== 'human') return;
    const point = mapPointer(event);
    if (point) { ui.inputError = false; $('remote-keyboard').focus({ preventScroll: true }); queueAction({ kind: 'click', ...point, button: 'left', count: 1 }); }
  });

  $('browser-image').addEventListener('pointermove', (event) => {
    if (ui.inputError || !ui.current?.available || ui.current.control_owner !== 'human' || ui.hidden) return;
    const point = mapPointer(event);
    if (!point) return;
    ui.pendingPointer = point;
    ui.pendingPointerClient = { x: event.clientX, y: event.clientY };
    ui.pendingPointerGeometry = pointerGeometry();
    schedulePointerFlush();
  });

  const keyboard = $('remote-keyboard');
  function flushKeyboard() {
    const text = keyboard.value; keyboard.value = '';
    if (text) queueAction({ kind: 'type_text', text });
  }
  keyboard.addEventListener('input', (event) => { if (!event.isComposing) flushKeyboard(); });
  keyboard.addEventListener('paste', (event) => {
    const text = event.clipboardData?.getData('text/plain');
    if (text != null) { event.preventDefault(); keyboard.value = ''; if (text) queueAction({ kind: 'type_text', text }); }
  });
  keyboard.addEventListener('compositionend', () => { setTimeout(flushKeyboard, 0); });
  keyboard.addEventListener('blur', () => discardQueue('Фокус ввода потерян. Неотправленный ввод удалён; выберите поле и повторите его.'));
  keyboard.addEventListener('keydown', (event) => {
    if (event.isComposing || event.key === 'Process') return;
    const control = event.ctrlKey || event.metaKey;
    if (control && event.key.toLowerCase() === 'v') return; // Paste into the local capture field; input forwards it once.
    if (control && event.key.toLowerCase() === 'a') { event.preventDefault(); queueAction({ kind: 'press_key', key: 'Control+a' }); return; }
    const keys = new Set(['Escape', 'Tab', 'Backspace', 'Delete', 'Home', 'End', 'PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Enter']);
    if (!control && !event.altKey && keys.has(event.key)) {
      event.preventDefault(); queueAction({ kind: 'press_key', key: `${event.shiftKey ? 'Shift+' : ''}${event.key}` });
    }
  });

  $('browser-image').addEventListener('wheel', (event) => {
    if (ui.inputError || !ui.current?.available || ui.current.control_owner !== 'human' || ui.hidden) return;
    event.preventDefault();
    const point = mapWheelPointer(event);
    if (!point) return;
    const scale = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16 : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? (Number(ui.current?.height) || 900) : 1;
    const dx = Math.round(event.deltaX * scale), dy = Math.round(event.deltaY * scale);
    if (!dx && !dy) return;
    ui.pendingWheel = ui.pendingWheel
      ? { dx: ui.pendingWheel.dx + dx, dy: ui.pendingWheel.dy + dy, x: point.x, y: point.y }
      : { dx, dy, x: point.x, y: point.y };
    if (!ui.draining && !ui.mutationPending) void drainQueue();
  }, { passive: false });

  document.querySelectorAll('[data-key]').forEach((button) => button.addEventListener('click', () => {
    void mutate({ kind: 'press_key', key: button.dataset.key });
  }));
  document.querySelectorAll('[data-scroll]').forEach((button) => button.addEventListener('click', () => {
    void mutate({ kind: 'scroll', dx: 0, dy: Number(button.dataset.scroll) });
  }));

  $('text-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('text-input');
    const text = input.value;
    if (!text || text.length > 4000) return;
    void mutate({ kind: 'type_text', text }).then((succeeded) => { if (succeeded) input.value = ''; });
  });

  $('navigate-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('navigate-url');
    try {
      const url = new URL(input.value.trim());
      if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Введите HTTPS-адрес без имени пользователя и пароля.');
      void mutate({ kind: 'navigate', url: url.href });
    } catch (error) {
      showNotice(error.message || 'Введите корректный HTTPS-адрес.', 'error');
    }
  });

  $('task-command-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (ui.taskSubmitting) return;
    const command = $('task-command').value;
    const origin = safeOrigin(ui.current?.url);
    const status = $('task-command-status');
    status.className = 'task-composer-status';
    if (!command.trim()) { status.classList.add('error'); status.textContent = 'Введите команду модели.'; return; }
    const sameAttempt = ui.taskSubmitAttempt?.payload?.goal === command;
    if ((!ui.current?.available || !origin) && !sameAttempt) { status.classList.add('error'); status.textContent = 'Откройте HTTPS-сайт в браузере перед запуском.'; return; }
    if (!ui.csrf) { status.classList.add('error'); status.textContent = 'Нет операторской сессии. Обновите страницу панели и войдите снова.'; return; }
    const fingerprint = JSON.stringify({ goal: command, origin: sameAttempt ? ui.taskSubmitAttempt.payload.allowed_origins[0] : origin });
    if (!ui.taskSubmitAttempt || ui.taskSubmitAttempt.fingerprint !== fingerprint) {
      ui.taskSubmitAttempt = {
        fingerprint,
        payload: {
          goal_language: 'ru', success_conditions: [], user_inputs: {}, secret_refs: [], policy_grant_id: null,
          sync_cookies: true, site_capabilities: [], limits: { timeout_s: 900, max_steps: 80 },
          profile_id: 'default', goal: command, workflow: 'agent', verbatim_text: null,
          allowed_origins: [origin], execution_mode: 'autonomous',
          idempotency_key: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`
        }
      };
    }
    updateTaskComposer();
    ui.taskSubmitting = true;
    $('task-command-submit').disabled = true;
    $('task-command').disabled = true;
    status.textContent = 'Создаём задачу…';
    try {
      const response = await fetchBounded('/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(ui.taskSubmitAttempt.payload) });
      const task = await response.json();
      if (!task || typeof task !== 'object' || typeof task.task_id !== 'string' || !task.task_id) throw new Error('Ответ сервера не подтвердил создание задачи.');
      ui.taskSubmitAttempt = null;
      updateTaskComposer();
      status.className = 'task-composer-status success';
      status.textContent = `Задача создана: ${task.task_id || 'ID не получен'}.`;
      ui.lastTaskPoll = 0;
      void refreshTask();
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && error.status !== 408) {
        ui.taskSubmitAttempt = null;
      }
      updateTaskComposer();
      status.className = 'task-composer-status error';
      if (error.status >= 400 && error.status < 500 && error.message) status.textContent = error.message;
      else status.textContent = `Не удалось подтвердить создание задачи (${error.message || 'сбой запроса'}). Проверьте текущую задачу перед повторной отправкой.`;
    } finally {
      ui.taskSubmitting = false;
      $('task-command-submit').disabled = false;
      $('task-command').disabled = false;
    }
  });
  $('task-command').addEventListener('input', updateTaskComposer);

  $('refresh').addEventListener('click', () => { releaseScreenshot(); void refreshState(); });

  document.querySelectorAll('[data-mutate]').forEach((control) => { control.disabled = true; });

  document.addEventListener('visibilitychange', () => {
    ui.hidden = document.hidden;
    if (ui.hidden) {
      discardQueue('Страница скрыта. Неотправленный ввод удалён; вернитесь к полю и повторите ввод.');
      keyboard.blur();
      clearTimeout(ui.timer);
      clearTimeout(ui.taskTimer);
      clearTimeout(ui.serviceTimer);
      ui.taskTimer = null;
      ui.serviceTimer = null;
      releaseScreenshot();
    } else {
      void refreshState();
      if (!embedMode && !ui.taskPolling && !ui.taskTimer) void refreshTask();
      if (!embedMode && !ui.servicePolling && !ui.serviceTimer) void refreshServices();
    }
  });

  window.addEventListener('unload', () => {
    clearTimeout(ui.timer);
    clearTimeout(ui.taskTimer);
    clearTimeout(ui.serviceTimer);
    releaseScreenshot();
    $('browser-image').removeAttribute('src');
    if (ui.imageUrl) URL.revokeObjectURL(ui.imageUrl);
    ui.imageUrl = null;
  });

  async function start() {
    try {
      const session = await getJson('/operator/session');
      ui.csrf = session.csrf_token || session.csrf || '';
      if (!(session.authenticated || session.operator_authenticated || session.active)) {
        showNotice('Войдите в операторскую панель, затем откройте этот экран снова.', 'error');
        setText('owner-label', 'Требуется вход');
        return;
      }
      if (!ui.csrf) showNotice('Операторская сессия не вернула CSRF-токен. Действия браузера останутся отключены.', 'error');
      if (!embedMode) {
        void refreshTask();
        void refreshServices();
      }
      await refreshState();
    } catch (error) {
      showNotice(error.message || 'Не удалось проверить сессию оператора.', 'error');
      setText('owner-label', 'Нет связи');
    }
  }

  void start();
})();
