# План проверки готовности Browser MCP — 2026-10-30

## Назначение и статусы

План проверяет полный локальный путь UI/MCP → control → модель → Camoufox worker → действие → независимый verifier, а также безопасность, надёжность, ресурсы и deploy/rollback. Это план, не отчёт: все gates сейчас **NOT RUN**. Исторические результаты README/docs не считаются текущим evidence.

- **PASS** — запуск завершён, все критерии выполнены, команда/окружение/результат и артефакты записаны.
- **FAIL** — тест выполнен и критерий нарушен; сохранить безопасные диагностики, не скрывать повтором.
- **BLOCKED** — не выполнен из-за явной внешней предпосылки; указать причину, не засчитывать как PASS.
- **NOT RUN** — запуск ещё не произведён.

Секреты, cookies, личные данные и приватные диалоги не включать в отчёт. Для live использовать синтетические данные и разрешённые origin. У каждого результата фиксировать дату, commit/source revision, версии, команду, exit code, counts/timing, окружение и evidence path.

## Runtime и предпосылки

Windows Python 3.12–3.13; Node.js для extension tests; Docker Desktop Linux/WSL2; control image (Python/FastAPI/MCP); browser image (Camoufox, Playwright, Xvfb, системные зависимости); отдельный Windows llama-server с моделью и vision projector. Сверить локальные пути и ревизию с `docs/03-running-fara.md`; health модели не доказывает качество текстового/визуального вызова или VRAM headroom.

Базовый Compose закрывает egress worker. `compose.direct.yaml` включает прямой development egress без proxy. Не объявлять его проверкой прокси или production-маршрута. На рабочий профиль допускается один worker, один context и одна основная page; повторно использовать существующий viewer, viewport 1440×900. Перед сайтами, требующими входа, проверить авторизацию и именно нужный аккаунт; cookie наличие этого не доказывает. Для публичных страниц вход не нужен.

Безопасные проверки конфигурации:

```powershell
python --version
node --version
docker info --format '{{.OSType}}'
.\scripts\start-services.ps1 -Direct -CheckOnly
.\scripts\start-fara.ps1 -CheckOnly
```

Fara CheckOnly зависит от файлов, заданных скрипту; при их отсутствии real model gate BLOCKED. Не запускать credential bootstrap или контейнеры, пока не проверены конфигурация и целевые volumes.

## Порядок и gates

| Gate | Область | Измеримый PASS | Статус |
|---|---|---|---|
| G0 | Baseline | Полный pytest и extension contract exit 0; версии/счётчики сохранены. Compose/Fara CheckOnly валидны либо внешняя предпосылка явно BLOCKED. | NOT RUN |
| G1 | Security API/MCP | В live test stack правильная UI session + Host/Origin/CSRF принимается; неверные/отсутствующие credentials, CSRF, Origin и worker key отклоняются; UI session не даёт MCP доступа; запрещённые resource/origin/cookie операции fail closed; секретов в events/logs нет. Есть positive и negative cases. | NOT RUN |
| G2 | Component/integration | Control↔worker API контракты, task verifier, safe receipts, UI/viewer, one-page policy проходят в изолированной среде. | NOT RUN |
| G3 | Real stack: browser + model | UI/MCP задача реально проходит через текущую модель, control и Camoufox; verifier независимо подтверждает точный outcome; submit разрешён только для синтетической тестовой формы, когда submit явно входит в сценарий и verifier проверяет результат; реальные отправки, покупки и действия с аккаунтами запрещены; после задачи все сервисы ready, worker/task idle, один context/page. | NOT RUN |
| G4 | Cancel/restart/concurrency | Cancel прекращает последующие действия; restart восстанавливает согласованное состояние; uncertain dispatch никогда не replay; конкурентность соблюдает лимиты, не создавая второй context/page; после каждого сценария idle/ready. | NOT RUN |
| G5 | Load/resources | Ступенчатая нагрузка не даёт OOM, неконтролируемой очереди/роста страниц или неограниченной задержки; лимиты дают контролируемый отказ; после нагрузки ready/idle. Зафиксированы memory/CPU/PID/cgroup. | NOT RUN |
| G6 | Deploy/rollback | Сверены candidate image/source IDs; deploy и возврат к зафиксированному image/config дают ready/idle, исходный профиль/volumes сохранены. Только изолированный candidate; никаких удаления production volumes. | NOT RUN |
| G7 | Приёмка | Production GO только если каждый обязательный gate G0–G6 имеет статус PASS и RESULTS содержит evidence. Любой обязательный BLOCKED или FAIL означает NO-GO; исключения требуют явного пересмотра состава обязательных gates до запуска, не постфактум. | NOT RUN |

### G0 — unit/component baseline

```powershell
python -m pytest -q
node --test chrome_extension/tests/contract.test.js
.\scripts\start-services.ps1 -Direct -CheckOnly
.\scripts\start-fara.ps1 -CheckOnly
```

Исторические числа в README/docs расходятся по датам и не являются текущим baseline. Не придумывать счётчики. Сфокусированные наборы для диагностики:

- API/policy: `tests/test_api.py tests/test_resource_policy.py tests/test_domain_storage.py tests/test_key_normalization.py`
- Worker: `tests/test_worker_browser.py tests/test_worker_gate.py tests/test_worker_verification.py tests/test_site_capabilities.py tests/test_native_input.py`
- Модель/task/UI: `tests/test_model_client.py tests/test_fara_prompt.py tests/test_fara_conversation.py tests/test_task_ui.py tests/test_verification_repair.py tests/test_verbatim_inputs.py`
- Cookie/extension: `tests/test_cookie_sync.py tests/test_cookie_ui.py tests/test_cookie_camoufox.py tests/test_cookie_extension_e2e.py` и JS команда выше.
- Recovery/resources: `tests/test_browser_recovery_contract.py tests/test_runtime.py tests/test_runtime_audit.py tests/test_worker_resources.py tests/test_resource_retention.py tests/test_desktop_viewer.py tests/test_demo_reuse.py`

`test_runtime_audit.py` требует disposable browser container и `RUNTIME_AUDIT=1`; `smoke_browser_recovery.py` только disposable Camoufox. Не запускать их на production profile. `model_generic_live_smoke.py` проверяет generic client на синтетической локальной странице, но не реальную полную связку.

### G1 — безопасность API/MCP и данных

В отдельном test stack проверить: (1) локальная UI session и Host/Origin/CSRF; (2) MCP Bearer отдельно от UI cookie; (3) worker credential; (4) resource/origin allowlist, redirect/frame и невалидный ввод; (5) synthetic cookie export/import только для exact разрешённых HTTPS origins, partitioned/ambiguous/unknown outcome fail closed. Проверить task event, log, response на отсутствие cookie values и приватного model text. PASS только при положительных и отрицательных примерах в реально поднятой конфигурации; unit tests сами по себе недостаточны.

### G2 — интеграция

`tests/manual_desktop_real_integration.py` по docstring использует временные профили, synthetic HTML и fixture credentials. Перед запуском проверить wrapper, режим и отсутствие production volume mounts. Если безопасный runner отсутствует — не запускать вручную на текущем профиле; отметить BLOCKED и подготовить disposable stack. Проверить screenshot/viewer, API flow, receipts и idle после работы.

### G3 — критические live browser пути

До старта проверить health/live, health/ready, model health и worker health из реальных network namespaces; маршрут egress; один idle context/page; отсутствие активных task/action/import/manual ownership. Не использовать пользовательские аккаунты. Порядок:

1. Read-only навигация по публичным Python.org и Node.js docs; факт/версия независимо сверяются с конкретной страницей/источником.
2. DemoQA text_box: синтетические поля, submit тестовой формы по сценарию, exact output verifier;
3. invalid_email: форма показывает ожидаемую ошибку, успешного submit нет.
4. checkbox, radio, dynamic button, single/double click: состояние подтверждено независимым ARIA/DOM verifier.
5. web_table: только новая synthetic row; все поля независимо сопоставлены.
6. Delayed render/dynamic IDs/overlap — UI Testing Playground или disposable fixture. iframe/alert/upload/drag — отдельными тестами, если worker реально поддерживает операцию; иначе зафиксировать как непроверенное ограничение.

Из приложения пользователя извлечены идеи тестовых площадок: WebArena/VisualWebArena, the-internet.herokuapp.com, UI Testing Playground, DemoQA, Books/Quotes to Scrape, SannySoft, Browserleaks, CreepJS, Turnstile demo, Amazon/eBay. Это лишь список предложений. Использовать безопасные публичные/собственные стенды. WAF/CAPTCHA/fingerprint страницы — наблюдать и проверять корректную остановку/эскалацию; не вводить «обход», stealth или anti-bot score как критерий PASS. Не использовать покупки, отправку сообщений или реальные credentials.

Изолированный DemoQA runner:

```powershell
python scripts/demoqa_preflight.py
.\scripts\run-demoqa-tests.ps1 -Cases 'text_box,invalid_email,checkbox,web_table,notify_then_click' -ExtendedComplex
```

Обязательно до выполнения прочитать актуальный runner: он останавливает точный production browser container, поднимает disposable stack и восстанавливает сервис. Нужна подтверждённая idle проверка и допустимое окно недоступности браузера. Не включать удаление test volume и не запускать на пользовательском профиле. `-ProbeOnly` — read-only диагностика, не тест модели. Для настоящего full-stack gate нужен сценарий через UI/MCP и реальную model; если готового безопасного runner нет, обозначить BLOCKED, а не считать synthetic smoke достаточным.

Read-only live security/health/observation/load probe для уже поднятого disposable сервиса на loopback:18080:

```powershell
python scripts/readiness_live.py --i-confirm-disposable --output docs/readiness-2026-10-30/live-report.json
```

Probe ограничивает target loopback:18080, concurrency максимум 4, итерации максимум 10; делает GET, read-only MCP `tools/list` auth probes и DELETE только собственной throwaway UI session для CSRF-проверки. Он не создаёт task и не выполняет browser action. Для проверки валидного MCP bearer передать test-only token file через `--mcp-token-file` или `READINESS_MCP_TOKEN_FILE`; значение token не включается в JSON. Без файла MCP positive check будет BLOCKED. Probe не реализует delay/dynamic-ID/overlay/iframe/dialog fixtures: текущий control API не предоставляет read-only интерфейс для их ввода.

Если harness запущен внутри disposable control container, разрешён loopback:8080 только с `--allow-disposable-internal`, `APP_ENV=test` и `READINESS_DISPOSABLE=1`; origin остаётся `http://localhost:18080`. Без этих двух marker-переменных внутренний URL отвергается до сетевого запроса.

```sh
APP_ENV=test READINESS_DISPOSABLE=1 READINESS_MCP_TOKEN_FILE=/app/runtime/control-data/mcp-token.txt \
  python scripts/readiness_live.py --url http://127.0.0.1:8080 --origin http://localhost:18080 \
  --allow-disposable-internal --i-confirm-disposable --output /app/runtime/control-data/readiness-live-report.json
```

Отдельный UI edge audit запускается только после остановки disposable browser worker, в одноразовом Camoufox контейнере с network namespace `none`, Xvfb и только временным профилем:

```sh
APP_ENV=test READINESS_DISPOSABLE=1 READINESS_BROWSER_AUDIT=1 READINESS_NETWORK_DISABLED=1 \
  xvfb-run -a python scripts/readiness_browser_audit.py --output /tmp/readiness-browser-audit.json
```

Он маршрутизирует exact synthetic fixture через worker и прерывает остальные page requests; проверяет delay/dynamic ID, coordinate click/type, overlay, open shadow DOM и synthetic iframe. Overlay, native dialog, file upload и повторную cancel idempotency возвращает BLOCKED как непроверенные capability/сценарии, не PASS. Queued/in-flight cancel и restart/crash recovery проверять существующими suites; повторный cancel live API здесь не выполняется, поскольку потребовал бы запуск TaskRunner/model task. Никогда не запускать audit рядом со вторым worker или на пользовательском профиле.

### G4 — cancel/restart/concurrency

На disposable stack и synthetic page: отменить задачу после начала действия и проверить отсутствие новых действий после ack; перезапустить control в idle и проверить task/API; перезапустить/crash browser и проверить bounded recovery, generation, новая observation/lease, одна page/context. Сверить action dispatch count, чтобы неизвестное внешнее действие не повторилось. Послать конкурентные API/MCP requests и проверить idempotency, task locking, concurrency limits, finite queues и корректные 4xx/5xx/timeout. По завершении каждого теста подтвердить task terminal state, idle worker и отсутствие import/action in progress.

### G5 — нагрузка/ресурсы

Только disposable stack: idle baseline → одна задача → ограниченная API конкуренция (без параллельного браузерного worker) → повторная задача → проверка контролируемого отказа при лимите. Снимать memory.current/peak/events, CPU, PID, worker context/page, queue depth, рестарты и длительности. PASS — в пределах Compose limits, нет OOM, неконтролируемой очереди/утечки страниц; при лимите отказ конечен и после восстановления ready/idle. Если метрики недоступны — BLOCKED.

Учитывать два прошлых OOM при лимите 2 GiB; текущая граница browser — 2816 MiB, control — 768 MiB, но тяжёлые сайты всё ещё могут исчерпать Docker VM. Не менять allocation Docker/WSL в рамках readiness проверки.

### G6 — deploy/rollback

Сохранить source revision, Compose config и image IDs безопасно (без секретов), собрать candidate и развернуть только в изолированном Compose project/profile. Сверить ready/health, auth boundary и безопасный read-only smoke. Выполнить rollback на сохранённые candidate baseline images/config и повторить ready/idle/profile checks. Запрещено применять `down -v`, удалять production volumes либо запускать второй worker на исходном профиле. Если безопасная изоляция и согласованное окно недоступны — BLOCKED. Успешная сборка или один health endpoint сами по себе не доказывают rollback.

## Evidence ledger

Заполнять отдельный `RESULTS.md` только после запусков; не менять статусы в этом плане на основании исторических заметок.

| Gate | Команда/сценарий | Commit/runtime | Результат, exit code, timing | Evidence path | Статус |
|---|---|---|---|---|---|
| G0 Python | `python -m pytest -q` |  |  |  | NOT RUN |
| G0 extension | `node --test chrome_extension/tests/contract.test.js` |  |  |  | NOT RUN |
| G0 config | start-services/Fara CheckOnly |  |  |  | NOT RUN |
| G1 security | API/MCP positive + negative cases | disposable |  |  | NOT RUN |
| G2 integration | control↔worker/viewer | disposable |  |  | NOT RUN |
| G3 full stack | real model + Camoufox + UI/MCP | isolated |  |  | NOT RUN |
| G4 reliability | cancel/restart/concurrency | disposable |  |  | NOT RUN |
| G5 resources | staged load + cgroup metrics | disposable |  |  | NOT RUN |
| G6 deploy | candidate/rollback IDs + health | isolated |  |  | NOT RUN |

Вкладывать только безопасные логи, receipts, synthetic screenshots и метрики; указывать артефактные пути. Исключить bearer, API key, cookie values, личные данные и приватные ответы модели.

## Остаточные риски и точные источники

- DemoQA suite документирует text/checkbox/table/click сценарии, но upload, browser dialogs, iframe и multiple windows не доказаны.
- MVP имеет один профиль/worker; popup/SSO ограничены; нагрузочный PASS на одной странице не равен multi-worker readiness.
- Прямой egress не proxy; реальная сетевой маршрут должен проверяться в worker namespace.
- OOM риск остаётся при тяжёлых страницах и малой Docker VM.
- `browser-use-undetected` и заявленные stealth/proxy/CAPTCHA свойства не интегрированы/не проверены; это вне PASS gate.
- Health и cookie presence не доказывают корректность model result, аккаунтную идентичность или завершение задачи; нужен независимый verifier.
- Chrome extension interactive permission/pairing UX не полностью покрывается isolated Chromium tests.
- Исторические отчёты за 28 октября не являются текущим run.

Ключевые файлы: `AGENTS.md`, `README.md`, `pyproject.toml`, `.env.example`; `src/browser_mcp/{app,auth,config,model_client,runtime,storage}.py`; `worker/{app,site_capabilities,native_input}.py`; `ui/{app,desktop,task-status}.js`; `config/{fara-system-prompt.txt,fara-tool-schema.json,artifacts.lock.json}`; `deploy/{compose.yaml,compose.direct.yaml,compose.demoqa-test.yaml,Dockerfile.control,Dockerfile.browser}`; `scripts/{start-services.ps1,start-fara.ps1,run-demoqa-tests.ps1,demoqa_preflight.py,demoqa_live_probe.py}`; docs 03/04/05/06/09/10/12/13/14; тесты перечислены в G0, плюс `tests/model_generic_live_smoke.py`, `tests/smoke_browser_recovery.py`, `tests/manual_desktop_real_integration.py`.

### Системный промпт и границы действий

`config/fara-system-prompt.txt` требует остановиться при недостающих персональных данных, неоднозначной задаче или необратимом действии без явного разрешения; заполнение формы не даёт разрешения на submit. Tool schema задаёт действия мыши/клавиатуры и screenshot-driven координаты; 1000×1000 в Fara prompt — нормализованная система координат, runtime переводит её в viewport 1440×900. Это не дефект само по себе; live visual gate сверяет фактическое попадание координаты на цель. TaskSpec/MCP default execution_mode — autonomous, UI default — supervised; post-action verification и максимум одна попытка repair; неизвестный исход внешней операции требует проверки человеком, не слепого retry. Загрузку, диалоги, iframe и multi-window не считать поддержанными без отдельного подтверждения.
