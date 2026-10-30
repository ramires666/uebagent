# P0: диагностика двух live отказов

**Статус:** исходный диагностический прогон checkbox и web_table завершён; оба сценария остались FAIL. Эта заметка фиксирует дополнительное evidence после readiness-аудита; она не меняет исторические выводы в [REPORT.md](REPORT.md) и [RESULTS.md](RESULTS.md).

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

Исходный прогон — диагностика, а не приёмка исправления. Для признания обоих путей исправленными нужны **три свежих независимых PASS запуска checkbox и три web_table**, каждый с успешным TaskRunner завершением и независимой проверкой конечного состояния. Таких результатов нет; release gate остаётся прежним **NO-GO** до закрытия подтверждённых отказов и остальных обязательных gates.

Первый прогон указывает на ошибки наведения/focus: большинство кликов не попадали в интерактивный target; для web_table worker отклонял ввод без editable focus. Он не доказывает конкретный внутренний механизм модели, не разделяет чистую model latency и orchestration wait, и не устанавливает, что изменения prompt или интерфейса исправят задачи.

Следующий эксперимент сравнит исходный путь с opt-in generic interaction hints при тех же задачах и лимитах. Это прототип, а не уже проверенное исправление; не считать его PASS без новых независимых live результатов.

## Opt-in hints prototype

Прототип добавляет только к generic model текущего screenshot-turn ограниченные metadata видимых интерактивных controls на HTTPS-страницах: максимум 32 элемента и 4 KiB, normalized-1000 center/bounds, tag/role и label до 80 символов. Password controls исключены; значения полей не передаются, допускается только Boolean `value_present` и безопасные состояния вроде `checked`/`expanded`. Labels являются недоверенными данными страницы. Hints сверяются с размерами PNG того же observation, не сохраняются в следующих ходах; feature по умолчанию выключен (`BROWSER_OBSERVATION_HINTS=0`).

Hints лишь помогают модели выбрать target: runtime не перемещает и не snap-ит координаты, не меняет policy, число попыток, лимиты или verifier. Исходный P0-результат остаётся **FAIL**, а release gate — **NO-GO** до критериев выше; для hints пока нет новых live результатов.

Повторить исходные checkbox и web_table с prototype можно в disposable P0 runner, включив `-ObservationHints`; лимиты остаются 180 секунд и 40 шагов:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-demoqa-tests.ps1 -ModelBaseUrl http://host.docker.internal:8080/v1 -ModelAlias auto -ModelProtocol generic -AllowSuspendedTasks -P0Diagnostics -ObservationHints -P0Scenarios 'checkbox,web_table' -RunLabel p0hints1
```

Проверено локально focused-набором model/runtime/P0 tests: **71 passed**. Полный стабильный unit suite: **455 passed, 6 skipped, 0 failed за 79.55 секунды** ([JUnit](../../runtime/readiness-2026-10-30/pytest-p0-hints.xml)). Этот результат не включает live A/B и не меняет исторический live **FAIL** или release **NO-GO**.
