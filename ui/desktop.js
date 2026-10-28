(() => {
  'use strict';

  const API = '/api/v1';
  const STATE_POLL_MS = 1000;
  const TASK_POLL_MS = 2000;
  const REQUEST_TIMEOUT_MS = 8000;
  const MAX_SCROLL = 600;
  const $ = (id) => document.getElementById(id);
  const ui = {
    csrf: '',
    current: null,
    polling: false,
    hidden: document.hidden,
    timer: null,
    taskTimer: null,
    taskPolling: false,
    lastTaskPoll: 0,
    screenshotAbort: null,
    screenshotBusy: false,
    screenshotGeneration: 0,
    imageUrl: null,
    mutationPending: false,
    actionQueue: [], queueBytes: 0, draining: false, inputError: false
  };

  function setText(id, value) {
    const node = $(id);
    if (node) node.textContent = value == null ? '' : String(value);
  }

  function showNotice(message, type = '') {
    const notice = $('notice');
    notice.className = `notice${type ? ` ${type}` : ''}`;
    notice.textContent = message;
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
    if (changed) {
      ui.screenshotGeneration += 1;
      ui.screenshotAbort?.abort();
    }
    const human = data.control_owner === 'human';
    if ((!human || !data.available) && previous?.control_owner === 'human') discardQueue('Управление изменилось. Неотправленный ввод удалён; передайте управление и повторите ввод.');
    $('remote-keyboard').disabled = !human || !data.available || !ui.csrf;
    const status = $('owner-status');
    status.className = `status ${human ? 'good' : data.available ? '' : 'bad'}`;
    setText('owner-label', human ? 'Вы управляете' : 'Просмотр');
    updateControlButtons();
    const allowed = Boolean(data.available && human && ui.csrf && !ui.mutationPending);
    document.querySelectorAll('[data-mutate]').forEach((control) => { control.disabled = !allowed; });
    $('browser-image').classList.toggle('interactive', allowed);
    const safeUrl = safeOrigin(data.url);
    setText('browser-runtime', data.runtime_environment === 'docker' ? `${data.backend || 'Браузер'} · Docker / WSL2` : (data.backend || ''));
    setText('site-label', data.available ? (safeUrl || 'Браузер доступен') : 'Браузер недоступен');
    if (!data.available && !ui.inputError) showNotice(data.detail || 'Содержимое браузера сейчас недоступно.', 'warning');
    else if (!human && !ui.inputError) showNotice(data.detail || 'Сейчас доступен только просмотр. Передайте управление оператору, чтобы вводить текст или нажимать кнопки.');
    else if (!ui.csrf) showNotice('Нет CSRF-токена операторской сессии. Обновите страницу и войдите в панель снова.', 'error');
    else if (!ui.inputError) showNotice(data.detail || 'Вы управляете браузером. Нажмите на снимок или используйте ручные действия.');
  }

  function safeOrigin(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' ? url.origin : null;
    } catch { return null; }
  }

  function releaseScreenshot() {
    ui.screenshotAbort?.abort();
    ui.screenshotAbort = null;
    ui.screenshotBusy = false;
    ui.screenshotGeneration += 1;
  }

  async function loadScreenshot() {
    if (!ui.current?.available || ui.hidden || ui.screenshotBusy) return;
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
      if (blob.size > 16 * 1024 * 1024) throw new Error('Снимок экрана превышает допустимый размер.');
      const nextUrl = URL.createObjectURL(blob);
      const image = $('browser-image');
      const oldUrl = ui.imageUrl;
      image.onload = () => {
        setText('image-size', `${image.naturalWidth} × ${image.naturalHeight}`);
      };
      image.onerror = () => {
        if (ui.imageUrl !== nextUrl) return;
        URL.revokeObjectURL(nextUrl);
        ui.imageUrl = null;
        showNotice('Не удалось показать снимок браузера.', 'error');
      };
      ui.imageUrl = nextUrl;
      image.src = nextUrl;
      if (oldUrl) URL.revokeObjectURL(oldUrl);
      image.hidden = false;
      $('empty-state').hidden = true;
    } catch (error) {
      if (!controller.signal.aborted && error.code !== 'CAPTURE_BUSY') showNotice(error.message || 'Не удалось получить снимок браузера.', 'error');
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
        showNotice(error.message || 'Не удалось получить состояние браузера.', 'error');
      }
    } finally {
      ui.polling = false;
      schedulePoll(STATE_POLL_MS);
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
      if (!ui.draining && ui.actionQueue.length) void drainQueue();
    }
    return succeeded;
  }

  function discardQueue(message) {
    const hadQueued = ui.actionQueue.length > 0 || Boolean($('remote-keyboard').value);
    ui.actionQueue = []; ui.queueBytes = 0;
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
  async function drainQueue() {
    if (ui.draining || ui.mutationPending) return;
    ui.draining = true;
    try {
      while (ui.actionQueue.length) {
        if (!ui.current?.available || ui.current.control_owner !== 'human' || ui.hidden) { discardQueue('Управление изменилось. Неотправленный ввод удалён.'); break; }
        const action = ui.actionQueue.shift();
        if (action.kind === 'type_text') ui.queueBytes -= new TextEncoder().encode(action.text).length;
        if (!await mutate(action)) { discardQueue(); break; }
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
      // Invalidate stale ownership until the server confirms the new gate state.
      ui.current = null;
      showNotice(operation === 'takeover' ? 'Управление передано вам.' : 'Управление возвращено.');
    } catch (error) {
      ui.current = null;
      showNotice(error.message || 'Не удалось изменить управление.', 'error');
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
  const terminalTasks = new Set(['succeeded', 'failed', 'cancelled', 'expired']);

  function safeSummary(value, limit = 240) {
    if (typeof value !== 'string') return '';
    return value.replace(/[\u0000-\u001f\u007f]/g, ' ')
      .replace(/https?:\/\/[^\s]+/gi, '[ссылка]')
      .replace(/\b(?:authorization|cookie|token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+/gi, '[скрыто]')
      .replace(/\s+/g, ' ').trim().slice(0, limit);
  }

  function renderTask(data) {
    const tasks = Array.isArray(data?.tasks) ? data.tasks : [];
    // Match the server's takeover selection; repository order is newest first.
    const task = tasks.find((item) => item && !terminalTasks.has(item.status)) || tasks[0];
    if (!task) {
      setText('task-status', 'Задач пока нет');
      setText('task-goal', '');
      setText('task-detail', '');
      return;
    }
    setText('task-status', taskLabels[task.status] || 'Неизвестное состояние');
    setText('task-goal', safeSummary(task.spec?.goal, 180) || 'Цель не указана');
    const steps = Number.isInteger(task.step_count) && task.step_count >= 0 ? task.step_count : 0;
    const summary = safeSummary(task.error?.message || task.result?.summary);
    setText('task-detail', `Шагов: ${steps}${summary ? ` · ${summary}` : ''}`);
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
      renderTask(await getJson('/tasks'));
    } catch {
      setText('task-status', 'Не удалось получить состояние задачи');
      setText('task-goal', '');
      setText('task-detail', '');
    } finally {
      ui.taskPolling = false;
      clearTimeout(ui.taskTimer);
      if (!ui.hidden) ui.taskTimer = setTimeout(refreshTask, TASK_POLL_MS);
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

  $('browser-image').addEventListener('click', (event) => {
    if (!ui.current?.available || ui.current.control_owner !== 'human') return;
    const point = mapPointer(event);
    if (point) { ui.inputError = false; $('remote-keyboard').focus({ preventScroll: true }); queueAction({ kind: 'click', ...point, button: 'left', count: 1 }); }
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
    if (!ui.current?.available || ui.current.control_owner !== 'human' || ui.mutationPending) return;
    event.preventDefault();
    const dy = Math.max(-MAX_SCROLL, Math.min(MAX_SCROLL, Math.round(event.deltaY)));
    if (dy) void mutate({ kind: 'scroll', dx: 0, dy });
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

  $('refresh').addEventListener('click', () => { releaseScreenshot(); void refreshState(); });

  document.querySelectorAll('[data-mutate]').forEach((control) => { control.disabled = true; });

  document.addEventListener('visibilitychange', () => {
    ui.hidden = document.hidden;
    if (ui.hidden) {
      discardQueue('Страница скрыта. Неотправленный ввод удалён; вернитесь к полю и повторите ввод.');
      keyboard.blur();
      clearTimeout(ui.timer);
      clearTimeout(ui.taskTimer);
      ui.taskTimer = null;
      releaseScreenshot();
    } else {
      void refreshState();
      if (!ui.taskPolling && !ui.taskTimer) void refreshTask();
    }
  });

  window.addEventListener('unload', () => {
    clearTimeout(ui.timer);
    clearTimeout(ui.taskTimer);
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
      void refreshTask();
      await refreshState();
    } catch (error) {
      showNotice(error.message || 'Не удалось проверить сессию оператора.', 'error');
      setText('owner-label', 'Нет связи');
    }
  }

  void start();
})();
