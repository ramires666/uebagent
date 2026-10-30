# P0: диагностика двух live отказов

**Текущий статус:** P0 criteria закрыты в bounded disposable scope: checkbox — 3/3 PASS, web_table — 3/3 PASS; full G3 — 8/8 PASS с независимым verifier при лимитах 120 секунд / 30 шагов. Hints включались только тестовой конфигурацией, production остаётся default-off. Свежий полный suite на ffa80c1: 486 passed, 8 skipped, 0 failed за 93,14 с; [JUnit-derived summary](evidence/unit-suite-gc-final.json). Guarded GC diagnostic покрыт тестами. Live GC contrast завершился: cgroup memory снизилась на 559 685 632 B, anon — на 554 995 712 B, file memory не изменилась. Причинность не установлена без matched idle-only контроля; production memory improvement не доказано. [Allowlisted evidence](evidence/memory-gc4gb1-summary.json). Lifecycle/cookie bridge bounded run завершился PASS; cookie bridge использовал synthetic extension protocol, не реальный Chrome E2E. Оба capacity soak завершились FAIL: soak15m1 при VM 4 GiB / browser 2,75 GiB — 359,75/900 с и 99,88%; soak8gvm4gb1 при VM 8 GiB / browser 4 GiB — 670,56/900 с и 99,998%, `MANUAL_ACTION_NOT_APPLIED:navigate` и cleanup/idle failure. Рост бюджетов продлил run, но не устранил failure. Production browser limit остаётся 2816 MiB; postflight после диагностики подтвердил восстановление production и 29 задач (одна suspended/waiting_human), profile fingerprints не изменились. [Soak evidence](evidence/soak-8gvm-4gb-failed.json) · [GC evidence](evidence/memory-gc4gb1-summary.json). Документ ниже сохраняет исторические traces исходного P0 провала; suite 457/8/0 в его старых разделах — исторический baseline.

**Важно:** первоначальные checkbox/web_table failures далее — архивная диагностика, а не текущее состояние P0.

**Архивный статус исходной диагностики:** checkbox и web_table тогда завершились FAIL. Этот снимок объясняет исходный дефект; актуальное bounded состояние P0 указано выше.

## Ранний снимок checkbox

В раннем снимке задача ещё была `running/checking_dependencies`. Worker trace содержал пять click actions при viewport 1440×900. Первые четыре завершились с `outcome=applied`, но попали в `other`; body оставался в focus, Home и Desktop оставались unchecked. Пятый click попал в checkbox Home (`x=295.2, y=236.7`); Home изменился с unchecked на checked, Desktop остался unchecked. Trace показал checkbox bounds `x=283.6, y=230.5, width=13, height=13` и expander bounds `x=262.6, y=227.5, width=16, height=16`. `elapsed_ms` измеряет browser action, а не ожидание модели. Итоговый отчёт ниже показывает, что более поздний клик повторно снял Home, а Desktop так и не был checked.

Санитизированный снимок: [early-trace.json](../../runtime/demoqa/p0trace1/early-trace.json). Он содержит координаты и геометрию целей, Boolean checked state и категории hit/focus/outcome; сырых model output и typed values там нет.

## Результат исходного P0-прогона

Оригинальные задачи прошли полный MCP/TaskRunner/browser worker путь с независимой проверкой; лимиты оставались 180 секунд и 40 шагов. Оба результата — **FAIL**:

| Сценарий | Исход и проверка | Worker trace | Интервалы между запланированными действиями |
|---|---|---|---|
| `checkbox` | `expired`, `TASK_EXPIRED`; 14 шагов, 187.790 с scenario elapsed; независимые `element_checked` и result checks оба false. | 13 clicks: 11 попали в `other`, 2 в Home checkbox. Home был включён, затем повторным кликом выключен; Desktop остался unchecked. Все actions `applied`; длительность browser action 21–52 мс. | Между action planned событиями 6.075–31.142 с. Это wall-clock gap, включающий model и orchestration, а не чистая model latency. |
| `web_table` | `failed`, `EDITABLE_FOCUS_RETRIES_EXHAUSTED`; 9 шагов, 65.346 с scenario elapsed; независимая проверка строки не пройдена. | 6 clicks попали в `other`; 3 `type_text` были `rejected`, focus до/после оставался `other`, все шесть полей имели `filled=false`. Browser actions заняли 12–58 мс. | Между action planned событиями 3.872–12.928 с. Это также не чистая model latency. |

Полный отчёт содержит только allowlisted trace и Boolean verifier outcomes: без task IDs, task specification, raw model output или typed values. Его SHA-256 при копировании из run output совпал с исходным файлом. Артефакты: [sanitized full trace report](evidence/p0-initial-trace.json), [summary](evidence/p0-initial-summary.json). Исходный путь был `runtime/demoqa/p0trace1/readiness-p0-report.json`; SHA-256 `f05fb68fc3ed2e45abb8d1871a2d346b298037384695acb3941cf8d2fc493aee`.

После прогона parent подтвердил прямым production preflight: production восстановлен, worker idle/readiness true с одним context/page, исходная waiting-human task осталась в `waiting_human`, phase `model_output_invalid`, version 15; task status и version не изменились. Её ID и timestamps в evidence не сохраняются. Отдельный полный unit run дал **432 passed, 6 skipped, 0 failed** за 88.61 с ([JUnit](../../runtime/readiness-2026-10-30/pytest-p0-baseline.xml)); это не меняет два live FAIL.

## Зафиксированная база

- До P0-фазы последний полный pytest на `b8c319a` завершился **418 passed, 6 skipped, 0 failed**. Это историческая база, а не повторный полный запуск после P0-изменений.
- Коммит `9a2365e` меняет историю generic model: предыдущий verbatim-текст сериализуется как канонический generic JSON placeholder. Для этого изменения пройдены **52 focused checks**. Оно меняет transcript format и retention; оно **не является причинным объяснением** timeout-ов DemoQA checkbox и web_table, в которых не было verbatim payload.
- Коммит `52fc6e0` добавляет opt-in action trace для тестового worker. Trace выключен по умолчанию, доступен только в test-конфигурации, не записывает model output, chain of thought или введённые значения; allowlist включает известные типы действий, координаты и viewport, категории focus/hit/outcome, фиксированные target labels, ограниченные bounding boxes и Boolean states.
- Статические synthetic tree/form fixtures проверены отдельными headless Chromium тестами без LLM, Camoufox и Docker. **87 combined checks прошли** в заявленном объединённом прогоне. Это не live-stack readiness evidence и не доказывает успешность DemoQA действий.

P0 runner и allowlisted сценарии находятся в [readiness_p0.py](../../scripts/readiness_p0.py), worker trace contract — в [readiness_trace.py](../../worker/readiness_trace.py), offline fixture checks — в [test_readiness_p0.py](../../tests/test_readiness_p0.py).

## Что ещё измеряем

Первый диагностический прогон использует исходные задачи и исходные лимиты: **180 секунд и 40 шагов** на сценарий. Для checkbox сохранена точная задача «Expand Home and leave its children visible. Select the Desktop checkbox and finish when the selected result appears.» Для web_table проверяются все шесть ожидаемых значений строки: Testperson, Example, test@example.com, 30, 12345 и Testing. Результат каждого сценария сверяется независимым worker verifier.

Цель trace — увидеть последовательность действия и результат hit/focus рядом с интервалами между запланированными шагами. Trace не содержит сырого ответа модели или введённых значений. Он поможет отличить долгие интервалы до следующего действия от времени выполнения самого browser action; сам по себе успешный action receipt не считается подтверждением результата.

## Гипотезы для дальнейшей проверки

- **Повторное масштабирование координат:** read-review пока не выявил второй scaling pass в полном пути координат. Это проверка кода, не live proof; trace покажет viewport и фактическую точку действия.
- **Focus и попадание в цель:** для checkbox сравним категории focus до/после, hit target и факт независимого `checked=true`. Планируется отдельно исследовать, помогает ли видимый focus/target feedback устранить расхождение между кликом и наблюдаемым состоянием.
- **Vision и длительность:** сопоставим trace action duration с интервалами между `action.planned`; большой интервал до следующего действия включает ожидание модели и orchestration и не равен чистой model latency. В текущем trace нет сырого model content, chain of thought или typed values.
- **Форма и таблица:** проверим, на каких шагах возникают задержки или несовпадение результата. Уменьшение требований, автоматизация отдельных полей или повторные blind retries не считаются исправлением исходного сценария.

## Команды повторения

Оригинальные два DemoQA сценария, с неизменными лимитами:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-demoqa-tests.ps1 -ModelBaseUrl http://host.docker.internal:8080/v1 -ModelAlias auto -ModelProtocol generic -AllowSuspendedTasks -P0Diagnostics -P0Scenarios 'checkbox,web_table' -RunLabel p0trace
```

Synthetic fixtures запускаются отдельно тем же полным MCP/TaskRunner/worker verifier путём:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-demoqa-tests.ps1 -ModelBaseUrl http://host.docker.internal:8080/v1 -ModelAlias auto -ModelProtocol generic -AllowSuspendedTasks -P0Diagnostics -P0Scenarios 'fixture_tree,fixture_form' -RunLabel p0fixtures
```

`-AllowSuspendedTasks` требуется только потому, что preflight обнаружил ранее ожидающую human-review задачу и оператор решил сохранить её. Этот флаг не разрешает активные задачи, recovery или небезопасное состояние worker.

## Критерий результата

Критерий воспроизводимости этих двух P0-сценариев — **три свежих независимых PASS запуска checkbox и три web_table**, каждый с успешным TaskRunner завершением и независимой проверкой конечного состояния. Этот критерий теперь выполнен для opt-in hints prototype. Это закрывает только повторяемость двух P0-сценариев; общий release gate остаётся **NO-GO** до прохождения остальных обязательных gates.

Первый прогон указывает на ошибки наведения/focus: большинство кликов не попадали в интерактивный target; для web_table worker отклонял ввод без editable focus. Он не доказывает конкретный внутренний механизм модели, не разделяет чистую model latency и orchestration wait, и не устанавливает, что изменения prompt или интерфейса исправят задачи.

Исходные live-отказы сохранены как историческая база для сравнения с opt-in generic interaction hints при тех же задачах и лимитах.

## Opt-in hints prototype

Прототип добавляет только к generic model текущего screenshot-turn ограниченные metadata видимых интерактивных controls на HTTPS-страницах: максимум 32 элемента и 4 KiB, normalized-1000 center/bounds, tag/role и label до 80 символов. Password controls исключены; значения полей не передаются, допускается только Boolean `value_present` и безопасные состояния вроде `checked`/`expanded`. Labels являются недоверенными данными страницы. Hints сверяются с размерами PNG того же observation, не сохраняются в следующих ходах; feature по умолчанию выключен (`BROWSER_OBSERVATION_HINTS=0`).

Hints лишь помогают модели выбрать target: runtime не перемещает и не snap-ит координаты, не меняет policy, число попыток, лимиты или verifier. Исходный P0-результат остаётся историческим **FAIL**. Hints остаются opt-in и выключены по умолчанию (`BROWSER_OBSERVATION_HINTS=0`); в production не развёрнуты.

Повторить исходные checkbox и web_table с prototype можно в disposable P0 runner, включив `-ObservationHints`; лимиты остаются 180 секунд и 40 шагов:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-demoqa-tests.ps1 -ModelBaseUrl http://host.docker.internal:8080/v1 -ModelAlias auto -ModelProtocol generic -AllowSuspendedTasks -P0Diagnostics -ObservationHints -P0Scenarios 'checkbox,web_table' -RunLabel p0hints1
```

### Live hints runs 1–3

Три opt-in прогона завершили оба сценария **PASS** при тех же лимитах 180 секунд и 40 шагов. Во всех шести сценарных запусках `baseline_verified=false`, `independent_verified=true`, `task_events_truncated=false`. Продуктовый код во всех трёх запусках соответствовал коммиту `7f5151d`; образы между прогонами не пересобирались (`-SkipBuild`). Отличие harness третьего запуска не меняло продуктовый код.

| Сценарий | Исходная база без hints | Hints run 1 | Hints run 2 | Hints run 3 | Наблюдение по trace |
|---|---|---|---|---|---|
| `checkbox` | `expired`, 14 шагов, 187.790 с | `succeeded`, 3 шага, 31.286 с | `succeeded`, 3 шага, 29.247 с | `succeeded`, 3 шага, 29.249 с | Во всех трёх hint runs первый click промахнулся; второй попал в expander Home, третий выбрал Desktop. Независимые проверки checkbox и результата прошли. |
| `web_table` | `failed`, 9 шагов, 65.346 с, editable-focus retries exhausted | `succeeded`, 15 шагов, 113.373 с | `succeeded`, 15 шагов, 111.380 с | `succeeded`, 15 шагов, 115.385 с | Во всех трёх hint runs все 6 `type_text` применены с focus на соответствующих полях; все 6 условий таблицы прошли независимую проверку. |

Успех трёх запусков на каждый сценарий — **3/3 PASS** по критерию воспроизводимости P0-сценариев. Web table завершился медленнее исходного отказа; intervals между действиями не являются чистой model latency, и по этим прогонам нельзя заявлять её снижение. Checkbox trace во всех трёх запусках всё ещё показывает начальный промах. Общий release **NO-GO** сохраняется до остальных gates; hints остаются выключены по умолчанию и не развёрнуты в production.

После третьего прогона parent подтвердил production worker `ready=true`, `idle` с 29 задачами; ранее ожидавшая human-review задача осталась в прежних status/version/phase/timestamps.

Сохранены полные sanitized allowlisted traces: [run 1](evidence/p0-hints-1.json) ([summary](evidence/p0-hints-1-summary.json)), [run 2](evidence/p0-hints-2.json) ([summary](evidence/p0-hints-2-summary.json)) и [run 3](evidence/p0-hints-3.json) ([summary](evidence/p0-hints-3-summary.json)). SHA-256: run 1 `ee31bf26b6cda84534e3a8bff29e50f3a2855f5ad30a069cfc5020b6a76816f2`; run 2 `8dd4ee9accac5069644027c74d3c9f7e13a46e543735664c982709be0a95be7f`; run 3 `b3cf4de7cb914b701731d0b2fb13db2bc0f7061f6946836200d7779de799eb57`. В artifacts отсутствуют task IDs, page labels, selectors, typed values, credentials и raw model output.

Проверено локально focused-набором model/runtime/P0 tests: **71 passed**. Полный suite на исходном коммите 7bc09cf: **475 passed, 8 skipped, 0 failed за 88,30 секунды** ([сводка](evidence/unit-cookie-probe-summary.json)); 6 сфокусированных тестов harness прошли на a0cc278; полный suite на этой ревизии не запускался. Из восьми пропусков шесть — environment-bound тесты: 2 требуют isolated Camoufox/X11 для cookie persistence, 2 — Camoufox/X11 для fingerprint migration, 1 — isolated X11 для native input, 1 — disposable browser container для runtime audit. Два opt-in real-worker integration check исполняются отдельными CLI и проверены отдельно. Полный G3 suite `hints-fullg3` завершён: 8/8 сценариев PASS с независимой проверкой при лимитах 120 секунд / 30 шагов. Текущий итог P0 — 3/3 свежих PASS для checkbox и 3/3 для web_table; полный G3 прошёл без setup blocks. Сводка G3: [allowlisted evidence](evidence/p0-hints-full-g3.json). Это закрывает воспроизводимость критичных P0-сценариев, но не общий release gate: opt-in hints выключены по умолчанию и не развернуты в production. P1 runtime integration checks завершились: queued cancellation дала `CANCELLED`, in-flight cancellation — `RECOVERING` после наблюдаемого действия; overlay завершился `waiting_human/verification_required` после одного клика и одной verification repair, без подтверждённого целевого эффекта. [Allowlisted summary](evidence/runtime-integration.json). Capacity/soak, concurrency и unsupported scenarios остаются вне этого evidence. Сценарии upload, browser dialog, frame targeting и multiple windows не запускались.
