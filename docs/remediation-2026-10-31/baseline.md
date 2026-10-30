# Ordinary headed baseline: подготовка

**Статус: PASS · проверка обновлённого runner завершена.** Human-manual baseline отдельно не выполнялся. Production `:8090` и выделенный test viewer `:18081` не использовались.

## Проверенная доступность

Read-only проверка системных мест установки обнаружила:

- Microsoft Edge: `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`.
- Google Chrome: `C:\Program Files\Google\Chrome\Application\chrome.exe`.
- Python Playwright импортируется; `playwright` CLI доступен. В проекте есть optional dependency `playwright==1.51.0`.
- Firefox не найден в стандартных Program Files путях.

Подтверждена возможность автоматизированного сравнения на обычном Edge для этого ограниченного сценария. Настоящая human-manual проверка отдельно **не выполнялась**.

Финальный проверочный запуск `20261030T213322Z-b004dea2` завершился PASS на Edge `156.0.4314.50`: один новый пустой профиль, один context/page, три read-only шага, 5-секундные паузы после home и Elements, `functional_steps_verified=true`, `owned_context_closed=true`, elapsed `12.078 s`. Безопасная копия receipt: [`ordinary-headed-pass-20261030T213322Z-b004dea2.json`](baseline/evidence/ordinary-headed-pass-20261030T213322Z-b004dea2.json). Исходный runtime receipt: `runtime/remediation-2026-10-31/baseline/20261030T213322Z-b004dea2/metadata.json`.

Предшествующая неуспешная попытка `20261030T213138Z-24c6236c` завершила те же три функциональных шага и закрыла owned context, но получила FAIL только на остановке Playwright lifecycle (`playwright_stop_failed`). Минимальная санитизированная запись сохранена в [`ordinary-headed-cleanup-failure-20261030T213138Z-24c6236c.json`](baseline/evidence/ordinary-headed-cleanup-failure-20261030T213138Z-24c6236c.json); исходный metadata receipt не перезаписывался. Исправлен порядок: закрытие owned context до остановки объекта Playwright, после чего финальный run завершился PASS. Profile directories не копировались.

## Предложенный запуск

Runner: [`scripts/ordinary_headed_baseline.py`](../../scripts/ordinary_headed_baseline.py). Он по умолчанию завершится без открытия браузера; публичная навигация требует явного `--run-public-demoqa`.

На разрешённом запуске runner должен:

1. Убедиться, что выбранный Edge binary существует, и создать уникальную пустую папку профиля под `runtime/remediation-2026-10-31/baseline/<run-id>/profile`. Если такой путь уже существует — остановиться, не переиспользовать его.
2. Запустить headed Edge через Playwright persistent context; один browser context и одна page, без чтения/импорта пользовательских профилей, аккаунтов или cookies. Версию брать из запущенного context либо из PE file metadata; не запускать Edge отдельно для запроса версии. Edge окно остаётся видимым для пользователя; runner не подключается к существующим окнам, вкладкам или профилям браузеров пользователя.
3. Выполнить только публичный read-only путь DemoQA home → видимая карточка `Elements` → Elements → browser back, с общим deadline 110 секунд. Проверять точный URL `https://demoqa.com/`, видимый заголовок карточки `Elements`, URL `https://demoqa.com/elements` и видимый body text `Elements`, `Text Box`, `Check Box`, `Web Tables`; после Back проверить исходный URL и карточку. Удерживать каждую страницу не менее 5 секунд до следующего действия, если остаётся общий deadline. Перед каждым действием проверять challenge/blocker маркеры. Любой неожиданный popup в принадлежащем runner контексте закрывается и останавливает сценарий. Не входить и не отправлять формы.
4. Записать JSON receipt в `<run-dir>/metadata.json` и вывести его в stdout: binary path, версия из контекста, созданный profile path и факт его начальной пустоты, способ управления (`automated ordinary headed`, `human_manual=false`), число context/page, итоговые наблюдения и безопасный код ошибки/phase. Не сохранять cookie values, screenshot, полный текст исключения или личные данные.

Окно Edge было видно пользователю; управление выполнялось runner через Playwright, не через human operator. Runner закрыл только собственный контекст/окно и сохранил отдельный тестовый профиль для provenance; существующий пользовательский браузер не затрагивался. Это instrumented UI baseline обычного браузера, не human-manual baseline; Playwright instrumentation может влиять на automation-related browser signals. Прогон подтверждает только заданные видимые действия DemoQA. Он не изолирует причину антибот-решения, не является антибот/WAF verdict и не переносится автоматически на Linux/Docker либо другую сетевую среду.

После review: `python scripts/ordinary_headed_baseline.py --run-public-demoqa`. Без флага `--run-public-demoqa` runner только выводит локальную доступность и не запускает браузер. При разрешённом запуске runner создаёт отдельную папку запуска с профилем и `metadata.json`; screenshot/cookie artifacts не создаются.

## Критерий результата

PASS требует видимого ожидаемого Elements URL/content, возврата к домашней странице, одного context/page, новой пустой изолированной папки профиля и полного закрытия owned context/Playwright. Receipt отдельно фиксирует `functional_steps_verified`, чтобы cleanup error не скрывал успешные функциональные проверки; cleanup error всё равно делает общий status FAIL. Ошибка запуска, неожиданный экран или blocker фиксируются как FAIL/BLOCKED с причиной; не подменять их Camoufox либо пользовательским браузером. Отсутствие отдельного human-manual сравнения — информационное ограничение, не препятствие для ordinary headed baseline.
