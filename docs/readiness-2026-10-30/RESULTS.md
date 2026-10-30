# Readiness evidence — 2026-10-30

Эта ведомость отражает проверенные локальные артефакты и переданные оператором результаты. Секреты и модельные пути не сохраняются. Сводный PASS отдельного сценария не подменяет gate полного стека.

## Evidence

| Gate | Команда / сценарий | Статус | Измеренный результат | Evidence |
|---|---|---|---|---|
| G0 | `python -m pytest -q` на `f6af7ed` | PASS | 403 passed, 6 skipped, 84.71 с. Включает контракт `test_finish_without_conditions_is_marked_unverified`: успешная задача без условий завершается как `completed_unverified` с `verified=false`; отдельный API consumer проверяет это состояние. | Результат baseline; полный лог не сохранён. |
| G0 | `node --test chrome_extension/tests/contract.test.js` | PASS | 29/29. | Результат baseline; отдельный лог не сохранён. |
| G0 | `start-fara.ps1 -CheckOnly`; `start-services.ps1 -Direct -CheckOnly` | PASS | Оба CheckOnly прошли; service check без мутаций. | Результаты baseline; отдельные логи не сохранены. |
| G1 | Readiness live security summary | PASS для 16 security checks; общий G1 не PASS | Отчёт summary: `overall_status=PASS`, `PASS=16`, `FAIL=0`, `BLOCKED=0`. Полный JSON export завершился `report_export_failed`, поэтому case-level детали и полный отчёт не доступны для аудита. Считать подтверждёнными только эти 16 summary checks. | [readiness1 JSONL](../../runtime/demoqa/results-readiness1.jsonl) |
| G1 / G2 / G3 | DemoQA: `text_box`, `invalid_email`, `checkbox`, `web_table`, `notify_then_click` | BLOCKED до browser/model действий; не security failures | Все 5 сценариев завершились до первого шага: `steps=0`, `COOKIE_SOURCE_UNAVAILABLE`, `task_status=waiting_human`; `model_action_observed=false`. Это не доказывает поломку действий worker, поскольку модель и browser action не запускались. Артефакт фиксирует `test_profile_fingerprint` неизменным (`sha256_before == sha256_after`); отдельная строка фиксирует `readiness_live` export failure. | [результаты сценариев и контроль профиля](../../runtime/demoqa/results-readiness1.jsonl); [screenshots](../../runtime/demoqa/readiness1/screens/) |
| G3 (ограниченное evidence) | `tests/model_generic_live_smoke.py` на локальной synthetic Chromium fixture, hardened run | PASS только для model/Chromium smoke; полный G3 NOT RUN | `result.json`: success, 4 шага, 18.288 с, High выбран, exact input match, `goal_reached`, vision `accepted`, 0 wrong clicks, 0 invalid outputs, submit attempts 0. Приватный браузер и внешняя сеть не использовались. Фактическая версия system prompt `43b3dad`; source revision самого smoke отдельно не фиксировался. Не проверяет Camoufox, control↔worker, MCP/UI либо deployment. | [result.json](../../runtime/readiness-2026-10-30/model-smoke-hardened/result.json), [final.png](../../runtime/readiness-2026-10-30/model-smoke-hardened/final.png) |
| G2 (native worker) | Network-disabled synthetic edge audit на Camoufox worker | PARTIAL: 8 PASS, 0 FAIL, 4 BLOCKED | Readiness audit применил worker navigation/click/type и независимую проверку delayed render, dynamic ID, shadow DOM и iframe. Overlay перехватил клик; worker receipt был `applied`, target не активировался, поэтому сценарий BLOCKED. Dialog, upload и worker-only cancel capability также BLOCKED как не поддерживаемые/не проверенные. Audit image собран с source `f6af7ed`. Это не полный control↔worker/model G2. | [sanitized edge report](evidence/browser-fault-edge-report.json), [sanitized fault summary](evidence/browser-fault-summary.json) |
| G4 (runtime) | `tests/test_runtime_audit.py` в network-disabled disposable browser image | PASS | 1 test, 0 failure records: runtime restart fencing, capture/input, extra-page enforcement и fingerprint persistence. | [fault summary](evidence/browser-fault-summary.json); [test source](../../tests/test_runtime_audit.py) |
| G4 (recovery) | `tests/smoke_browser_recovery.py` в isolated temporary profile | PASS | Recovery прошёл после canonical-page close и Camoufox parent SIGKILL; тестовые assertions подтвердили сохранение synthetic cookie, action receipt и capability-use marker и возврат к одному ready context/page. Не является TaskRunner cancel/restart/concurrency evidence. | [fault summary](evidence/browser-fault-summary.json); [smoke source](../../tests/smoke_browser_recovery.py) |
| Pre/postcondition | Read-only контроль исходного профиля вокруг fault tests | PASS для наблюдаемых инвариантов | `task_unchanged=true`; до прогона worker available/browser_started, owner none, action/import idle, 1 context/page. После прогона worker ready, owner none, action/import idle, 1 context/page, recovery idle, `resource_violation=false`. IDs и timestamps исключены. | [sanitized fault summary](evidence/browser-fault-summary.json) |
| G3 / G5 setup | Network-disabled audit container, попытка без Docker `--init` | NOT RUN — invocation issue | Контейнер оставался на `xvfb-run`/`Xvfb`; Python/audit не стартовали. Причина: ручной запуск пропустил `--init`, в отличие от Compose `init: true`. Отдельный короткий `--init` smoke завершился за 2.25 с, но audit сценарии этим не проверены. Следующий запуск должен использовать `--init`. | [browser start diagnostics](../../runtime/readiness-2026-10-30/browser-start-diagnostics-session65141.json) |
| G5 | cgroup snapshot контейнера до запуска Python | OBSERVED, не PASS G5 | CPU 0.00%, RAM 23.69 MiB / 2.75 GiB; `memory.current=32,948,224`, `memory.peak=33,800,192`, OOM counters 0, `pids.current=3`, `pids.peak=8`. Были только Xvfb процессы; это не измерение браузерной нагрузки. | [container snapshot](../../runtime/readiness-2026-10-30/container-observations-session65141-structured.json) |

## Gate status

| Gate | Статус | Причина |
|---|---|---|
| G0 baseline | PASS | Python, extension и CheckOnly evidence выше; тестовый контракт `completed_unverified` покрыт. |
| G1 API/MCP security | PARTIAL / IN PROGRESS | Первый summary: 16 security checks PASS, но full JSON export failure лишил case-level evidence. Live2 повторил 16 PASS; full report export ждёт cleanup. Пять DemoQA `COOKIE_SOURCE_UNAVAILABLE` остаются setup отказами task сценариев, не security failures. |
| G2 native worker/control integration | PARTIAL | Native worker edge audit: 8 PASS, 0 FAIL, 4 BLOCKED. Сценарии не покрывают полный control↔worker↔model путь; отдельный live TaskRunner smoke ещё выполняется. |
| G3 real stack browser + model | NOT RUN | Model smoke прошёл только на Chromium/synthetic fixture. Настоящий Camoufox + control + UI/MCP + model сценарий ещё не подтверждён. |
| G4 cancel/restart/concurrency | PARTIAL / IN PROGRESS | Runtime restart audit и SIGKILL recovery smoke PASS с preservation assertions. Live TaskRunner cancel/idempotency ещё pending; конкурентность и uncertain-dispatch outcome не закрыты. |
| G5 resources/load | NOT RUN | Есть только pre-browser-start snapshot, нагрузочного прогона нет. |
| G6 deploy/rollback | IN PROGRESS | Отдельный isolated rollback run подготавливается; к production target не применялся. |
| G7 production GO | IN PROGRESS — решение не вынесено | Текущие обязательные gates не закрыты. Эта ведомость предварительная; финальный GO/NO-GO после live2, cancel smoke и rollback evidence. До этого production GO не подтверждён. |

## Ограничения

- Пять DemoQA сценариев блокированы на источнике cookies до действий; screenshots и profile digest подтверждают контекст и отсутствие изменения профиля, но не проверяют UI capability.
- 16 security PASS доступны только в summary, потому что выгрузка полного отчёта не удалась.
- Hardened model smoke — локальная synthetic Chromium fixture с локальной generic model; он не проверяет Camoufox или полный UI/MCP путь.
- Xvfb audit попытка без `--init` не является результатом browser test. Cgroup snapshot также снят до старта Python.
- Native edge audit подтвердил iframe только на синтетическом fixture; dialog/upload BLOCKED, multi-window не проверялся.
- Live TaskRunner cancel, параллельность, uncertain-dispatch recovery и isolated deploy/rollback ещё не имеют завершённого evidence.
