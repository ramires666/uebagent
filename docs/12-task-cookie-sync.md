# Автоматическая синхронизация cookies перед задачей

Новые задачи по умолчанию копируют применимые cookies из подключённого обычного профиля Chrome в профиль браузерного исполнителя перед началом действий. В форме флажок **«Копировать cookies сайта из моего Chrome перед запуском»** включён. В MCP поле `sync_cookies` также по умолчанию `true`; укажите `false`, чтобы отказаться от копирования. При отказе исполнитель использует уже имеющиеся cookies своего профиля. Обязательная проверка входа в Gemini остаётся включённой.

## Какие cookies копируются

- Только для точных HTTPS-origin из `allowed_origins` задачи; принимается от одного до десяти origin.
- Учитываются применимые cookies самого хоста и родительских доменов по Public Suffix List. Cookies соседних доменов не выбираются; учитываются все пути.
- Используется обычный профиль Chrome, выбранный подключённым расширением. Для определения хранилища целевую вкладку открывать не нужно, но в подключённом обычном профиле должна оставаться открытой хотя бы одна обычная вкладка. Режим инкогнито не используется. Если профиль нельзя определить однозначно, задача останавливается с ошибкой.
- Перед чтением Chrome может запросить разрешение на доступ к новому сайту. Подтвердите точные origin во всплывающем окне расширения после действия пользователя, затем повторите задачу.
- Передаются только поддерживаемые cookies. Если обнаружена partitioned cookie, превышен предел размера/количества или результат копирования неизвестен, задача останавливается целиком; набор не обрезается и частичный успех не выдаётся за полный.

Расширение получает список только из разрешённых origin для текущей задачи и читает значения через Chrome Cookies API. Значения не показываются в интерфейсе, журналах или событиях задачи. В событиях сохраняются код результата и счётчики; успешная синхронизация учитывается один раз для задачи. При возобновлении той же задачи она не отправляется повторно. Новая задача получает отдельную синхронизацию.

## Порядок запуска и вход

Сначала источник Chrome экспортирует cookies, затем исполнитель дожидается возможности безопасного импорта, устанавливает их и проверяет результат. Только после успешной синхронизации продолжаются переход к каноническому адресу сайта и действия задачи. Для Gemini отдельно проверяется подтверждённое состояние входа. Отсутствующий вход и неизвестное состояние блокируют ввод и отправку; наличие cookies само по себе не подтверждает вход. Обычные публичные сайты могут не требовать авторизации.

Синхронизация не продлевает срок cookies и не меняет их срок действия. Пустой набор cookies допустим, но для Gemini всё равно требуется независимое подтверждение входа.

## Обновление расширения и ошибки

Для автоматической синхронизации нужна версия расширения **0.1.2**. После установки обновления перезагрузите уже подключённое распакованное расширение в `chrome://extensions` один раз. Существующая привязка и ранее выданные разрешения сохраняются. Для ранее не разрешённого сайта Chrome всё равно требует отдельного подтверждения через значок расширения.

- `CHROME_PERMISSION_REQUIRED`: подтвердите доступ к указанному сайту через расширение и повторите задачу.
- `COOKIE_SOURCE_UNAVAILABLE`: откройте и подключите расширение Chrome.
- `COOKIE_SOURCE_AMBIGUOUS`: оставьте обычную вкладку в подключённом профиле и убедитесь, что подключён ровно один источник Chrome.
- `COOKIE_SYNC_UNSUPPORTED`: обновите расширение или проверьте, не использует ли сайт неподдерживаемый тип cookies.
- `COOKIE_SYNC_FAILED`: cookies не перенесены, задача не начинает действия.
- Неизвестный исход импорта требует проверки; автоматическая повторная отправка не выполняется.

Автоматическая синхронизация сейчас поддерживает профиль `default`. Для исходного задания, ожидающего вход в Gemini, после входа нажмите **«Продолжить»** у этой же задачи: уже выполненная синхронизация не повторяется. Создание новой задачи запускает отдельную синхронизацию.

## Live integration note (2026-10-28)

- Synthetic run `c7d0a96f-8363-4114-bd29-1dc8ae915b01` is superseded and is not a valid Unicode benchmark. Its PowerShell-to-Python request transport replaced the expected Cyrillic prompt with question marks before the API received it. The immutable task record was left untouched.
- Corrected run `4c8e53c1-0da4-4799-b65f-e7bf18c5083b` used an ASCII-only smoke script with Python Unicode escapes. The stored goal and prompt matched their expected strings exactly; the prompt had 28 code points, 23 Cyrillic letters, and zero question marks.
- The corrected run copied 28 cookies (28 applied, 0 failed), refreshed the Gemini session, navigated to the canonical app, and completed one native click submission. The task receipt reported `enter_dispatched=false`, `input_verified=true`, `model_calls=0`, and `response_verified=false`. Final auth state was `signed_in/ACCOUNT_CONTROLS_PRESENT`.
- Timings: sync 406 ms; sync completion to refresh receipt 809 ms; refresh receipt to task terminal 3,653 ms; total 5,091 ms. A read-only worker verification returned `true` for the exact known test prompt; no chat text was returned or inspected.

## Current task defaults

- New tasks run in autonomous mode by default. Choose supervised mode when the task should pause for approval before each action.
- Cookie synchronization remains enabled by default and can be disabled per task. Gemini authentication checks remain required before entering or submitting a prompt.
- After browser recovery creates a new browser generation, the task re-syncs cookies once for that generation before resuming. It does not replay an action with an unknown outcome.

## Recovery validation (2026-10-29)

- The final automated suite passed: 326 passed, 6 skipped.
- The desktop and task-status assets returned HTTP 200 after the control image update. No task was resumed or cloned during deployment validation.
- The previously configured direct route was restored after explicit approval. The browser worker is ready with one context/page and route generation 1; the persisted Camoufox config/preferences fingerprint matches its expected digest.
- The recovery script's dry run confirmed one applied navigation and one failed refresh with no unresolved action. The task was not resumed, cloned, or submitted during validation.
