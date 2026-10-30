# Инженерный отчёт о готовности к выпуску

**Дата проверки:** 30 октября 2026, Asia/Qyzylorda

**Решение:** **NO-GO для production release**

**Объём:** API/MCP, UI, модель, browser worker, управление задачами, восстановление, ограниченная нагрузка и candidate rollback.

## Краткое резюме

Аудит завершён, однако выпуск по текущим критериям не готов. В восьми различных live UI сценариях **6 завершились PASS с независимой проверкой и 2 завершились FAIL**: checkbox остался выключен, а web_table истёк по времени. Эти два дефекта сохраняются и после targeted retest. Prompt-smoke, контроль доступа и rollback дали полезные положительные результаты, но каждый в ограниченном объёме; они не отменяют два подтверждённых отказа полного пользовательского пути.

| Показатель | Результат | Как читать |
|---|---:|---|
| Итог readiness | **NO-GO** | G3 — FAIL; G2/G4/G5 — PARTIAL; полный release evidence не закрыт |
| Последний полный pytest (`b8c319a`) | **418 PASS / 6 SKIPPED / 0 FAIL / 0 errors**, 424 всего | 82.605 с по JUnit; 82.63 с по консольному измерению |
| Расширение, Node contract suite | **29/29 PASS** | Отдельный запуск, не часть pytest-числа |
| Live UI сценарии | **6 PASS / 2 FAIL** | Всего 8 различных сценариев; таблица ниже |
| Control/API/MCP security | **16/16 PASS** | Auth/CSRF матрица; worker bearer positive/negative — отдельные task-control checks |
| Ограниченный cancel smoke | **5/5 checks PASS** | Остановился в `RECOVERING`; не доказывает отмену in-flight dispatch |
| Synthetic model smoke | **4 шага PASS за 17.82 с** | Chromium fixture; не полный Camoufox/control/worker стек |
| Browser peak в короткой выборке | **1,522,884,608 / 2,952,790,016 B (51.58%)** | 189/512 PID, OOM counters = 0; это не capacity test |
| Candidate → baseline → candidate | **PASS с ограничением** | Production восстановлен; control DB migration marker не проверялся |

### Итог по воротам

| Gate | Статус | Вывод |
|---|---|---|
| G0 — базовые тесты | **PASS** | Полный pytest, extension suite и CheckOnly прошли в зафиксированном объёме; в pytest 6 пропусков. |
| G1 — API/MCP security | **PASS** | Реализованная auth/CSRF матрица 16/16. Worker bearer positive/negative проверен отдельно в task-control suite (5 checks всего), не включён в эти 16. Это не полная оценка всех security/data policy. |
| G2 — worker/control integration | **PARTIAL** | Synthetic worker edge audit: 8 PASS, 0 FAIL, 4 BLOCKED; полный control↔worker/model путь не закрыт. |
| G3 — live browser + model | **FAIL** | Восемь сценариев: шесть PASS и два FAIL; checkbox и web_table не прошли. |
| G4 — cancel/restart/concurrency | **PARTIAL** | Runtime/SIGKILL recovery и bounded cancel smoke прошли; in-flight cancellation и concurrency не доказаны. |
| G5 — ресурсы/нагрузка | **PARTIAL** | Короткий read-only прогон прошёл без OOM; soak и capacity не проверены. |
| G6 — deploy/rollback | **PASS с ограничением** | Изолированный цикл прошёл, профиль не менялся, production восстановлен; миграционная совместимость БД не проверялась. |
| G7 — production release | **NO-GO** | Обязательный G3 содержит два FAIL; дополнительные незакрытые ограничения G2/G4/G5 остаются. |

## Как результат проходит через систему

Запрос приходит через UI или MCP, управление задачей формирует предложение модели и передаёт разрешённое действие worker-у. Worker воздействует на сайт. Отдельный verifier сравнивает наблюдаемое состояние с условием успеха. Три уровня результата нельзя смешивать: **proposal** — что модель предлагает; **action** — что worker получил/применил; **verified result** — что независимая проверка действительно увидела на странице.

```mermaid
flowchart LR
    U[UI или MCP] --> C[Control и TaskRunner]
    C --> M[Model: предложение]
    M -->|proposal| C
    C -->|разрешённое действие| W[Browser worker]
    W -->|action receipt| C
    W --> S[Сайт]
    S -->|новое наблюдаемое состояние| V[Независимый verifier]
    V -->|verified result| C
    C --> R[Статус задачи и ответ]
    C -.->|схема разрешённых действий| W
```

Положительный receipt сам по себе не равен результату: synthetic overlay test зафиксировал receipt `applied`, хотя overlay перехватил клик и целевой элемент не активировался. Именно поэтому итоговые UI PASS опираются на независимое подтверждение состояния.

## Восемь live UI сценариев

Для каждого PASS был указан независимый verifier. Время — зафиксированное wall time конкретного запуска, не прогноз производительности. Сценарии synthetic и не включают реальные аккаунты, покупки или отправку сообщений.

| Сценарий | Результат | Шаги | Время | Проверка / основание |
|---|---:|---:|---:|---|
| `text_box` | **PASS** | 9 | 57.71 с | Поле заполнено; независимая проверка прошла |
| `invalid_email` | **PASS** | 14 | 98.05 с | Невалидный email обработан ожидаемым UI путём; проверка прошла |
| `notify_then_click` | **PASS** | 10 | 102.15 с | Действие после уведомления подтверждено verifier-ом |
| `radio` | **PASS** | 4 | 47.20 с | Выбор radio подтверждён независимо |
| `double_click` | **PASS** | 7 | 99.24 с | Двойной клик подтвердил ожидаемое состояние |
| `dynamic` | **PASS** | 1 | 15.21 с | Динамический элемент найден и проверен |
| `checkbox` | **FAIL** | 16 | 191.98 с | `TASK_EXPIRED`; Home раскрылся и 4 checkbox role были видны, но Desktop остался unchecked. Предыдущая ошибка формы вывода `MODEL_OUTPUT_INVALID` устранена, однако end-to-end условие не достигнуто. |
| `web_table` | **FAIL** | 20 | 186.27 с | `TASK_EXPIRED`; требуемая строка не была независимо подтверждена до deadline. |

История отказов сохранена: в readiness2 checkbox завершился `MODEL_OUTPUT_INVALID` после 2 шагов, а web_table — `TASK_EXPIRED` после 23 шагов/185.48 с. Исправление prompt убрало именно ранний shape error checkbox, но targeted4 всё ещё завершился с checkbox unchecked и повторным timeout web_table. Оба отказа учитываются как FAIL, а не переименовываются в setup block.

## Исправления в коде и тестовой инфраструктуре

| Область | Было | Стало | Проверка | Ограничение |
|---|---|---|---|---|
| Продукт: приватность API/MCP | Чувствительные пользовательские значения могли попадать в ответы/повторное использование задачи. | Значения редактируются в API/MCP ответах; при повторе секрет требуется ввести заново, он не копируется из прошлой задачи. | Полный pytest на `b8c319a`; commit `953bf8d` добавил secret re-entry. | Не доказывает отсутствие всех классов утечки во всех интеграциях. |
| Продукт: транспорт и отмена | Chunked HTTP body мог читаться без достаточной границы; неизвестный исход действия мог выглядеть как завершённая отмена. | Чтение body ограничено; неопределённый outcome остаётся `RECOVERING` и требует review, без автоматического replay. | Regression suite; cancel smoke: после 2 с число `action.planned` не выросло, worker вернулся idle. | Это не доказывает abort уже отправленного действия; bounded smoke не создал/не подтвердил in-flight dispatch. |
| Продукт: инструкции модели и формы действий | Общая guidance недостаточно чётко задавала доверие к источникам, аргументы действий, деревья и формы. | Уточнены generic prompt и требования к shape/аргументам действий, работа с деревьями и формами (`43b3dad`, `c895246`, `ac8a7bf`). | 40 focused prompt cases PASS; synthetic Chromium smoke PASS; live targeted retest. | Не все ошибки модели исправлены: checkbox и web_table остаются FAIL. |
| Тестовая инфраструктура: запуск и конфигурация | Неоднозначные test URLs/cookie sync, нестабильный startup extension worker и некорректная обработка завершения/отчёта. | Уточнены URLs, sync отключён явно; стабилизирован generic auto setup/startup; поправлены failed-exit и evidence export. | Соответствующие regression cases входят в полный pytest; итоговые live security/cancel summary экспортированы. | Инфраструктурный PASS не является продуктовой capability проверкой. |
| Тестовая инфраструктура: очистка и владение | Остановленные fixtures от предыдущих запусков учитывались как leftovers текущего прогона и давали ложный failure. | Cleanup отличает pre-existing stopped fixture IDs от ресурсов текущего запуска; проверяются новые leftovers, поправлен per-run ownership/exit/report путь. | Regression suite и проверки cleanup/restore в control3. | Targeted4 runner не дошёл до собственного profile digest postcheck после сценарных FAIL; отдельный read-only postflight подтвердил digest. |
| Тестовая инфраструктура: Windows/Docker | Rollback harness некорректно читал Docker labels из-за quoting; ручной запуск без init зависал на Xvfb. | Исправлен Windows quoting (`b8c319a`); audit повторён корректным способом с init. | Shim guard tests входят в 418 pytest PASS; candidate → baseline → candidate прошёл. | Ранний запуск без `--init` — NOT RUN, а не browser PASS. |

Тестовая дисциплина: шесть native runtime wrapper тестов отмечены как **SKIPPED** в полном host pytest и не выполнялись там. Отдельный `test_runtime_audit` прошёл в disposable browser image; эти две группы не следует смешивать.

## Неопределённый исход отмены

Если отмена приходит после передачи действия, система не может считать, что сайт его не выполнил: ответ мог потеряться после эффекта на странице. В этом случае задача остаётся в `RECOVERING`; автоматический повтор запрещён, поскольку он способен применить внешнее действие дважды. Следующий шаг — наблюдение/review и сверка результата. Схема ниже концептуальная: подписи переходов описывают условия, а не точные enum/API статусы; verifier не переводит задачу в `COMPLETED` автоматически.

```mermaid
flowchart TD
    R[Задача выполняется] --> Q{Исход действия при отмене}
    Q -->|Очередь: dispatch не было| C[Терминальная отмена]
    Q -->|Receipt подтверждает завершённое действие: остановить следующие шаги| C
    Q -->|Итог отправленного действия неизвестен| U[RECOVERING]
    U --> I[Осмотр состояния и human review]
    I --> P[PAUSED: решение не принято]
    P -->|Явное решение продолжить| R
    P -->|Явное решение отменить| C
    P -.->|Автоматический replay запрещён| P
```

Фактический control smoke достиг стабильного `RECOVERING`, выдержал 2 секунды без дополнительных planned actions и оставил worker ready/idle с одним context/page. Это подтверждает bounded поведение control smoke, но **не** отмену уже dispatched/in-flight действия, рестарт TaskRunner или concurrency.

## Изолированный deployment rollback

Rollback проверялся отдельным Compose project и раздельными volumes control/worker; host profiles не монтировались. Fingerprint профиля оставался неизменным, production task state совпал до и после, временные контейнеры удалены, production восстановлен.

```mermaid
flowchart LR
    A[Production: состояние зафиксировано] --> B[Изолированный candidate]
    B -->|ready, 1 context/page| C[Точный baseline rollback]
    C -->|ready, fingerprint без изменений| D[Повторный candidate]
    D -->|ready, production восстановлен| E[Проверка task state и очистки]
```

Все три фазы прошли health/operator session/browser checks. **Control DB migration marker не проверялся**, поэтому совместимость миграции и гарантия rollback для схемы БД не доказаны. Этот результат — deployment evidence с ограничением, не доказательство полного production rollout.

## Ресурсы и границы измерений

В bounded Live2 read-only прогоне было 6/6 HTTP 200 при concurrency 2 и 3 итерациях; p95 — 14.23 мс. Browser container достиг peak **1,522,884,608 B из 2,952,790,016 B (51.58%)**, PID peak **189/512**, OOM/OOM-kill/high counters — 0; отмечено 6 throttled CPU periods.

Это короткая выборка, а не soak/capacity test. Низкий peak в ней не задаёт безопасную ёмкость и не описывает тяжёлые страницы, длительную нагрузку, несколько workers или конкурирующие задачи. До выполнения длительного bounded run G5 остаётся PARTIAL.

## Остаточные риски

- Checkbox и web_table — повторяемые live path failures, подтверждённые targeted retest.
- Нативные dialog и file upload не поддержаны/не проверены; overlay test показал корректный BLOCKED при перехвате клика. Worker edge audit: 8 PASS, 0 FAIL, 4 BLOCKED, в том числе overlay, dialog, upload и worker-only cancel. Iframe был подтверждён только synthetic fixture.
- Runtime audit и восстановление после закрытия страницы/SIGKILL прошли, но это не заменяет TaskRunner in-flight cancellation, uncertain-dispatch recovery или concurrency evidence.
- Readiness1 security export имел ошибку; его можно считать только summary 16 checks. Поздний control3 export успешно подтвердил реализованную матрицу 16/16.
- Успешный model smoke был локальной synthetic Chromium fixture, 4 шага за 17.82 с; он не включал Camoufox и полный control↔worker/MCP/UI путь. Отдельный smoke 18.288 с также не является полным G3.
- G6 не проверил migration marker control DB; миграционная совместимость и DB rollback не доказаны.
- Внешний egress/proxy, stealth/CAPTCHA и multi-worker readiness не входят в подтверждённые PASS.

## План доделок и выпускной критерий

Аудит завершён, работа над продуктовой готовностью — нет. По имеющимся evidence unrestricted production release рекомендовать нельзя. Сначала нужно закрыть два доказанных live отказа. Ограниченный пилот допустимо рассматривать только для явно описанного поддерживаемого потока, после отдельного решения владельца выпуска и проверки его критериев; текущие результаты сами по себе не являются рекомендацией запускать production или пилот.

### P0 — обязательно для любого пересмотра G3 / GO

| Задача | Владелец по роли | Зависимости | Критерий приёмки |
|---|---|---|---|
| Установить причину checkbox и web_table FAIL до изменения prompt/поведения. Снять минимальную санитизированную трассу: тип действия (`kind`), координаты, задержки, имена полей схемы, наблюдения до/после и ожидаемая/фактическая цель. Не включать введённый текст, секреты или raw reasoning модели. | Владелец browser automation; совместно с тестированием | Повторяемый disposable stack и synthetic fixtures | Для каждого шага можно сопоставить proposal/action receipt с фактической целью и следующим наблюдением; причины различают ошибку выбора модели/vision от неверного ввода/координаты или недостатка наблюдений. Трасса не содержит пользовательского ввода, секретов или chain-of-thought. |
| Добавить маленькие детерминированные fixtures: раскрытие tree expander с отдельным checkbox target; отдельно — небольшую таблицу с фокусом/целью на конкретной строке. Сравнить ожидаемый hit target с фактическим hit target/координатой и проверяемым состоянием страницы. | Владелец тестовой инфраструктуры | P0 трассировка; доступные verifier assertions | Fixture стабильно создаёт исходное состояние; verifier отдельно проверяет, что раскрылась нужная ветка и checkbox checked, а в таблице изменилась нужная строка; тест отличает неверную цель/координату от правильного действия с неверным наблюдением/верификацией. |
| Исправить установленную первопричину ограниченным изменением и повторить исходные сценарии с прежним пределом 180 с / 40 шагов. Не проводить слепые prompt retries: каждый новый прогон должен проверять конкретную гипотезу о причине. | Владелец browser automation; ревью тестирования | Причина установлена; fixture и acceptance assertions готовы | Checkbox действительно checked, строка внесена; оба условия подтверждены независимым verifier до deadline. Никаких ложных PASS по receipt или тексту модели. |
| Подтвердить устойчивость исправления несколькими свежими прогонами. Предлагаемый выпускной порог — **3 последовательных PASS на каждый критичный сценарий** в disposable stack, затем повтор полного обязательного G3. | Владелец тестирования / release owner | Предыдущие пункты P0 | Три новых последовательных PASS на checkbox и web_table с сохранёнными шагами, временем, verifier outcome и без setup blocks; после этого full-stack G3 без обоих FAIL. Это предлагаемый будущий gate, а не уже выполненный тест. |

P0 закрывает только подтверждённые UI failures. Он не закрывает автоматически необязательные функции, concurrency, capacity или DB migration evidence: их обязательность следует определить в release contract.

### P1 — закрыть, если входит в release contract

| Работа / статус для GO | Владелец по роли | Зависимости | Критерий приёмки |
|---|---|---|---|
| Dialog, file upload и overlay outcome; сейчас BLOCKED/не подтверждены, а overlay test показал receipt `applied` при неактивированной целевой UI-семантике. `applied` может честно означать доставку mouse event, поэтому критерий относится к результату задачи, а не к протоколу receipt. **Обязательно для GO, если заявленная поддержка включает эти действия.** | Владелец browser worker | Явная политика разрешённых действий и безопасные synthetic fixtures | Для dialog/upload есть отдельные поддержанные операции и независимая проверка; при перехваченном клике task/verifier не сообщает достижение целевого результата без фактического эффекта. Неподдерживаемый путь явно блокируется. |
| In-flight cancellation, busy/profile ownership, uncertain dispatch и запрет повторного side effect; сейчас bounded cancel оставляет `RECOVERING`, но in-flight abort не доказан. **Обязательно для GO, если разрешены внешние действия с возможным повторным эффектом или concurrent tasks.** | Владелец control/TaskRunner | Трассируемый dispatch lifecycle; управляемый synthetic endpoint | Проверить cancel до dispatch, во время dispatch и после неизвестного ответа; отсутствие автоматического replay; idempotent/stable status; подтверждённая сверка результата; profile/context/page инварианты при занятости и recovery. |
| Cookie source, live identity/account policy и log redaction. **Обязательно для GO при использовании авторизованной сессии или чувствительных данных.** | Владелец security/privacy | Зафиксированная политика identity/session и test data | Перед действием подтверждаются ожидаемый аккаунт и источник сессии; неверный/неизвестный контекст блокируется; секреты и личные данные отсутствуют в логах и export artifacts. Readiness1 cookie-source block остаётся именно setup failure, не capability PASS. |
| Soak/capacity. Сейчас только короткий Live2 run, поэтому **обязательно лишь при capacity claim или заданной нагрузке в релизном SLO**. | Владелец SRE/performance | Явные concurrency/duration/resource budgets | Длительный bounded run с заранее утверждёнными длительностью, concurrency, лимитами RAM/PID/CPU и критериями деградации; сохранены cgroup метрики и error/latency summaries. Никаких capacity выводов из коротких 6 запросов. |
| Control DB migration/rollback marker. Сейчас marker не проверялся, поэтому **обязательно перед изменением control DB schema**. | Владелец control persistence / release engineering | Версионирование схемы и isolated baseline/candidate окружение | Кандидат с миграцией стартует; rollback/candidate redeploy проверены на совместимом состоянии данных; marker/version явно прочитан и записан в evidence; production state защищён. |

### P2 — расширение уверенности, не текущие доказанные блокеры по умолчанию

| Работа | Владелец по роли | Зависимости | Критерий результата |
|---|---|---|---|
| Расширить synthetic browser dataset: динамические элементы, iframe, shadow DOM, overlay и вариации форм. | Владелец QA automation | P0 deterministic fixtures | Повторяемые regression cases с независимыми assertions; BLOCKED и unsupported cases остаются раздельными от PASS. |
| Добавить наблюдательные метрики и regression CI для ошибок схемы действий, verifier mismatch, task expiration и recovery. | Владелец observability/CI | Согласованные event schema и redaction | В CI доступны агрегаты этапов proposal/action/verification; артефакты санитизированы; regression сравнивается с известными baseline без raw model reasoning. |
| Уточнить публичные сетевые/WAF диагностики и egress границы. Это проверка доступности/совместимости, не попытка обхода защит. | Владелец network/platform | Зафиксированная разрешённая сеть и тестовые публичные endpoints | Сетевые блокировки диагностируются и классифицируются; никакого обхода WAF/CAPTCHA; заявленные egress ограничения отражены в release docs. |
| Рассмотреть расширение smoke с single-worker на дополнительные поддерживаемые сценарии/профили. | Архитектор runtime + QA | P1 busy/profile ownership и capacity при необходимости | Для каждого добавленного профиля/worker есть отдельные resource, ownership и isolation assertions; не заявлять multi-worker readiness до этих проверок. |

### Последовательность принятия решения

1. Сохранить текущий baseline и артефакты; начать с P0 трассировки, не меняя повторно prompt без диагностической гипотезы.
2. Локализовать для checkbox и web_table: выбранная цель, фактическое попадание, receipt, наблюдение, verifier и причина timeout.
3. Исправить конкретную причину, проверить deterministic fixture и повторить сценарий в пределах исходных лимитов.
4. Выполнить предложенный порог 3 последовательных свежих PASS на каждый из двух критичных сценариев и полный G3. Это новый критерий, не свершившийся факт текущего отчёта.
5. До release определить контракт поддерживаемых функций и применить соответствующие P1 gates. Если отмена внешних действий/авторизованные сессии входят в scope, закрыть соответствующие P1 до пилота/GO.
6. Для заявлений capacity или изменений схемы БД завершить соответствующие P1 прогоны. Повторно проверить regression suite и осознанно разобрать 6 SKIPPED тестов.
7. Пересмотреть G0–G7 по новым evidence. Production GO возможен только после закрытия обязательных критериев; ограниченный пилот требует отдельного решения и явно урезанного supported scope.

## Источники и происхождение

Сводные результаты аудита: [RESULTS.md](RESULTS.md) и [план проверки](PLAN.md). Санитизированные артефакты: [полный regression summary](evidence/regression-final-summary.json), [readiness2](evidence/demoqa-readiness2-summary.json), [targeted4](evidence/demoqa-targeted4-summary.json), [control3](evidence/control3-live-summary.json), [task control](evidence/task-control-summary.json), [browser edge audit](evidence/browser-fault-edge-report.json), [runtime/recovery summary](evidence/browser-fault-summary.json), [load metrics](evidence/live2-load-summary.json), [model smoke](evidence/model-smoke-release-summary.json), [rollback](evidence/rollback-summary.json).

Ключевые источники кода: `src/browser_mcp/{app,auth,config,model_client,runtime,storage}.py`, `worker/{app,site_capabilities,native_input}.py`, `ui/{app,desktop,task-status}.js`, `config/fara-system-prompt.txt`, `config/fara-tool-schema.json`. Коммиты исправлений включают `1d0aace` (ограничение body и неизвестный outcome), `953bf8d` (повторный ввод секрета), `43b3dad`/`c895246`/`ac8a7bf` (guidance и формы действий), `b8c319a` (Windows Docker label quoting). Итоговый аудиторский HEAD: **`54ce49f`**; последний полный тестовый source revision: **`b8c319a`**. Идентификаторы браузера, абсолютные model paths, секреты и пользовательские данные в этот отчёт не включались.
