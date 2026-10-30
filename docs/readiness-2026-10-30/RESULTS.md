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
| G3 (ограниченное evidence) | `tests/model_generic_live_smoke.py` на локальной synthetic Chromium fixture, hardened run | PASS только для model/Chromium smoke; полный G3 NOT RUN | `result.json`: success, 4 шага, 18.288 с, High выбран, exact input match, `goal_reached`, vision `accepted`, 0 wrong clicks, 0 invalid outputs, submit attempts 0. Приватный браузер и внешняя сеть не использовались. Не проверяет Camoufox, control↔worker, MCP/UI либо deployment. | [result.json](../../runtime/readiness-2026-10-30/model-smoke-hardened/result.json), [final.png](../../runtime/readiness-2026-10-30/model-smoke-hardened/final.png) |
| G3 / G5 setup | Network-disabled audit container, попытка без Docker `--init` | NOT RUN — invocation issue | Контейнер оставался на `xvfb-run`/`Xvfb`; Python/audit не стартовали. Причина: ручной запуск пропустил `--init`, в отличие от Compose `init: true`. Отдельный короткий `--init` smoke завершился за 2.25 с, но audit сценарии этим не проверены. Следующий запуск должен использовать `--init`. | [browser start diagnostics](../../runtime/readiness-2026-10-30/browser-start-diagnostics-session65141.json) |
| G5 | cgroup snapshot контейнера до запуска Python | OBSERVED, не PASS G5 | CPU 0.00%, RAM 23.69 MiB / 2.75 GiB; `memory.current=32,948,224`, `memory.peak=33,800,192`, OOM counters 0, `pids.current=3`, `pids.peak=8`. Были только Xvfb процессы; это не измерение браузерной нагрузки. | [container snapshot](../../runtime/readiness-2026-10-30/container-observations-session65141-structured.json) |

## Gate status

| Gate | Статус | Причина |
|---|---|---|
| G0 baseline | PASS | Python, extension и CheckOnly evidence выше; тестовый контракт `completed_unverified` покрыт. |
| G1 API/MCP security | PARTIAL | Налицо 16 security checks summary PASS, но JSON export failure лишил case-level evidence; полный G1 нельзя подтвердить. Пять DemoQA `COOKIE_SOURCE_UNAVAILABLE` — setup отказ task сценариев, не security failures. |
| G2 control↔worker integration | NOT RUN | DemoQA сценарии не дошли до worker actions. |
| G3 real stack browser + model | NOT RUN | Synthetic Chromium smoke ограничен; Camoufox audit не стартовал. |
| G4 cancel/restart/concurrency | NOT RUN | Нет live результата. |
| G5 resources/load | NOT RUN | Есть только pre-browser-start snapshot, нагрузочного прогона нет. |
| G6 deploy/rollback | NOT RUN | Нет live результата. |
| G7 production GO | NO-GO / NOT READY | Обязательные G1–G6 не все PASS. |

## Ограничения

- Пять DemoQA сценариев блокированы на источнике cookies до действий; screenshots и profile digest подтверждают контекст и отсутствие изменения профиля, но не проверяют UI capability.
- 16 security PASS доступны только в summary, потому что выгрузка полного отчёта не удалась.
- Hardened model smoke — локальная synthetic Chromium fixture с локальной generic model; он не проверяет Camoufox или полный UI/MCP путь.
- Xvfb audit попытка без `--init` не является результатом browser test. Cgroup snapshot также снят до старта Python.
- Upload, browser dialogs, iframe и multi-window не подтверждены реальным worker экспериментом.
