# Проверка текущего стенда

Срез после исправлений: 28 октября 2026 года. Живые сценарии запускались последовательно в изолированном DemoQA worker; таблица ниже объединяет результаты отдельных регрессионных прогонов, это не один общий запуск.

| Проверка | Результат |
|---|---|
| Основной набор | 215 тестов прошли, 6 пропущены за 33,8 секунды. |
| Production | `/health/live` и `/health/ready` отвечают успешно; модель и worker готовы. Прямой маршрут задан существующим `compose.direct.yaml`: `direct-development`, generation 1. Панель использует автоматическую локальную operator-сессию. |
| Браузер | Один контекст, одна страница, нет действий или импорта cookies в процессе финальной проверки. Исходный профиль и cookie volumes сохранены. |
| Образы | Развёрнуты проверенные browser `bfedd35c74f5` и control `2dd179966567`. Старые образы сохранены как `browser-mcp-local-browser:pre-fontfix` и `browser-mcp-local-control:pre-fontfix`. |
| Fingerprint | Сохранённый digest остался `958ee3c18bd1870a48fdf4d4bb3f88d93217c54f4844a59cc671ca2521f9b3d3`; значения исходной identity-конфигурации не менялись. macOS launch preferences направляют `system-ui` в Arial для western и unicode групп, не записывая эти значения в fingerprint. |
| Шрифты | После deploy естественный DemoQA заголовок при исходном CSS weight 100 читается правильно. PNG: `runtime/demoqa/production-font-fixed.png` (129115 bytes, SHA-256 `b3b5d620a0e49f4bc2d229d26c42ebe374a92ab01af78800d43ebaa385e0db07`). Body/sidebar отображаются заметно жирнее прежнего снимка; это ожидаемый визуальный компромисс выбранного семейства. Системные шрифты в целом не копировались и fingerprint/OS persona не подменялись. |
| Текстовая форма | `text_box` прошёл с точными синтетическими значениями `test@example.com`; отдельный `invalid_email` оставил `test@example.test` без отправленного результата, страница показала `field-error`. |
| Checkbox | PASS: задача выбрала Desktop; независимая проверка уникального `[role="checkbox"][aria-label="Select Desktop"]` подтвердила `checked=true`, ARIA `true` и результат `desktop`. |
| Web table | PASS: добавленная строка с `test@example.com` независимо проверена по пяти значениям в нативной таблице. |
| Остальные интерактивные элементы | Radio, double-click, single-click и dynamic properties прошли в прежних серийных прогонах. `notify_then_click` прошёл с одним уведомлением и подтверждённым click. |
| Автономный режим | `execution_mode=autonomous` остаётся доступен для новых задач; supervised остаётся значением по умолчанию. Подтверждённые автономные действия выполнены через TaskRunner/Fara и независимый worker verifier. |
| Не проверялись | Загрузка файла, браузерные диалоги, iframe и несколько окон не входят в этот набор. |

Сводки и исходные результаты отдельных прогонов сохранены в `runtime/demoqa/results-v*.jsonl`; визуальные сравнения шрифта — в `runtime/demoqa/v6styles/`, `v6thin/`, `v6geckopref/` и `production-font-fixed.png`. Сводное описание сценариев и исправлений: `docs/10-demoqa-tests.md`.

Контрольное состояние после deploy: production UI ready, worker idle, `context_count=1`, `page_count=1`, `control_owner=none`; тестовый compose stack остановлен. Профиль `browser-mcp-demoqa-test-worker-state` остаётся отдельным disposable volume и не подключался к production.

## Latest verification update - 2026-10-28

The latest full test suite completed with **228 passed, 6 skipped** in 36.43 seconds. The focused Google resource and frame policy suite completed with **7 passed**. These results supersede the earlier test count above; they do not combine the separate DemoQA browser runs into one run.

After the final worker restart, a metadata-only snapshot confirmed all 16 imported authentication-cookie entries were present, unexpired, unpartitioned, and in the normal cookie context; no re-import was performed. An independent read-only control-to-worker verifier confirmed the visible account control and absence of ServiceLogin/sign-in links and exact sign-in buttons. This confirms signed-in UI state, but does not reveal or verify the account email identity and does not prove agent task success. The narrow Google resource/frame policy patch was live; the previously observed account/ogs blocks were absent in that check. See [cookie integration incident record](06-cookie-integration.md) for scope and limits.


### Follow-up verification - 2026-10-28

The newest full suite completed with **236 passed, 6 skipped** in 37.11 seconds, superseding the earlier 228-test summary. The focused resource-policy suite passed **7 tests**, and the task UI suite passed **4 tests**. These are separate runs.

Regression tests confirmed that an overdue resume returns `TASK_EXPIRED` before model dispatch; the UI updates the old task and offers its repeat editor, with no automatic task creation. A fresh run used the existing specification with a new idempotency key and deadline and ended `completed_unverified` because no independent success conditions were supplied. Its duration covers the complete agent workflow, not Gemini alone. The narrow Gemini resource-policy check passed live with the prior API blocks absent. Exact-text correctness and account identity remain unverified. See [cookie integration incident record](06-cookie-integration.md) for the live run details and limits.

### Fast Gemini workflow implementation - 2026-10-28

The latest full suite completed with **258 passed, 6 skipped** in 45.95 seconds. Separate focused runs completed with **33 worker tests**, **62 control tests**, and **5 task UI tests**. These are separate runs and are not additional counts to add to the full suite.

The UI now exposes an explicit one-prompt Gemini workflow, preserves exact prompt text, and shows submission-only status. Repeat tasks remain editable and do not submit until the user confirms. Tests cover exact text, repeat conversion, one-use workflow fields, and idempotency behavior. No live browser timing run was performed for this UI update; the fast-workflow benchmark remains pending. See [fast Gemini workflow guide](11-fast-gemini.md) for the API contract and result limits.

### Native-click fast-workflow rollout - 2026-10-28

The latest full suite passed **258 tests, 6 skipped** in 43.10 seconds; the focused worker, fast-runtime, and site-capability suite passed **24 tests**. Native click receipts distinguish `submit_dispatched=true`, `submit_method=click`, and `enter_dispatched=false`; durable receipts from the prior Enter implementation remain accepted.


### Mandatory Gemini authentication and task cookie sync - 2026-10-28

The latest full suite passed **290 tests, 6 skipped** in 52.03 seconds. Separate focused runs passed **27 extension contract tests**, **3 isolated Chromium extension end-to-end tests**, and **19 task/cookie UI tests**. The isolated extension test closes the requested site tab, identifies the ordinary cookie store from a different normal tab, and confirms that applicable host and parent-domain cookies are exported while sibling-domain cookies are excluded. These targeted runs are separate from the full suite.

Task cookie sync is enabled by default for new tasks and may be opted out with `sync_cookies=false`. It exports only cookies applicable to the task's exact allowed HTTPS origins, imports them through the task-scoped worker gate, and verifies the result before task browser actions. Cookies and values are not retained in task events; events record only safe outcome codes and counts. Resuming the same task does not resync. Partitioned/unsupported cookies, ambiguous profile selection, and unknown import outcomes stop the task without partial success or automatic retry. See [task cookie sync guide](12-task-cookie-sync.md).

The unpacked Chrome extension is version **0.1.2**; the user must reload it once for the new task export job. Existing pairing and grants remain in place; Chrome may request a user gesture to grant access to a newly allowed site. Production control and UI deployment and the task source SHA were verified. At the final check, Gemini authentication status was `unknown` with `PROOF_INCOMPLETE`; no claim is made that a live Google sign-in or end-to-end Gemini task was verified. A positive UI check alone does not verify account identity or task success.
