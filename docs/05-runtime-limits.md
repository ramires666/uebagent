# Ограничения ресурсов во время работы

Документ описывает текущую конфигурацию локального MVP. Числа — заданные пределы процессов и очередей, а не гарантия, что произвольный сайт уложится в них.

## Где что выполняется

- Рабочий браузер Camoufox запускается в Docker Desktop на Linux/WSL2. Это браузер, которым управляет worker.
- Окно Chrome в Windows служит только для просмотра. Оно не является рабочим браузером worker.
- Fara запускается отдельным процессом в Windows и использует GPU RTX 3090. Модель не запускается в Docker.
- Для worker предусмотрен один канонический browser context и одна основная страница. Текущий MVP работает с одной страницей; поддержка всплывающих окон для SSO может потребовать отдельной ограниченной политики в будущем.

## Вкладки и захват страницы

Ссылку `<a target="_blank">` при обычном одиночном щелчке слева система распознаёт и открывает в текущей странице. Новые лишние окна закрываются как можно скорее: одновременно допускается до 8 ожидающих закрытий, на каждое отводится 2 секунды. Не закрывшееся окно помещается в карантин; health становится `false`, а gate управления — недействительным.

Основную страницу worker не пересоздаёт автоматически после её закрытия. Для модели допускается один ожидающий захват с ожиданием до секунды; на это время новые захваты для просмотра отклоняются. Запросы снимков не накапливаются в неограниченной очереди.

## Доступ

MCP использует отдельный bearer token. Локальная UI-сессия использует собственную авторизацию; в локальном режиме сессия создаётся автоматически и восстанавливается после перезапуска. Эти два способа входа не заменяют друг друга.

## Ограниченные данные в памяти

- История задачи сохраняет не более 4 последних версий различных фактов и 8 последних применённых действий — всего до 12 записей и не более 36 КиБ на задачу. Повтор факта обновляет его запись и переносит её в конец истории.
- По завершении задачи её история удаляется. Записи завершённых фоновых задач также удаляются.
- Chrome broker допускает до 8 фоновых операций и до 64 ожидающих запросов к расширению. Повторное обновление списка сайтов объединяется, пока запрос ещё выполняется.
- В памяти остаётся не более 16 каталогов cookies. Срок жизни каталога — 120 секунд; при истечении срока удаляются его cookie-метаданные и список сайтов.
- Broker хранит до 128 квитанций импорта и соответствующих ключей идемпотентности за время работы процесса. Ключи не вытесняются: после заполнения новых импортов не будет до перезапуска сервиса. Повтор с сохранённым ключом возвращает прежнюю квитанцию, а другой запрос с тем же ключом отклоняется. Перезапуск очищает эти ключи из памяти, поэтому после перезапуска клиенту следует проверить состояние импорта, прежде чем повторять действие с неизвестным исходом.
- Docker-журнал каждого контейнера ограничен тремя файлами по 10 МиБ.
- ModelClient хранит приватный диалог только в памяти процесса и только для текущей задачи; полное содержимое ответов assistant не записывается в журналы или базу и не выдаётся через API. Сохраняются исходная цель и до 24 сообщений, текст assistant ограничен 24 КиБ. В памяти остаются до трёх последних PNG размером не более 8 МиБ каждый: до 24 МиБ исходных данных плюс накладные расходы кодирования и представления в памяти. Диалог очищается при завершении, отмене, закрытии клиента и начале новой задачи.

## Проверка результата и исправление

Если проверка результата не прошла, допускается ровно одна попытка исправления после этой проверки только чтения. Модели передаётся обратная связь размером до 8 КиБ; атомарная квитанция в SQLite фиксирует бюджет этой попытки, а счётчик шагов сохраняется после перезапуска. При неизвестном исходе действия, запросе подтверждения или ограничении политики автоматическое исправление вслепую не выполняется. Если критерии успеха не заданы, нужна проверка человеком; если повторная проверка не прошла, задача передаётся человеку вместе с результатами проверок.

Перетаскивание поддерживается только для уголка изменения размера `textarea`: до 10 шагов за 1 секунду. На отпускание кнопки мыши отводится 0,4 секунды; при неудаче worker переходит в карантин. Действие `read_page_answer_question` пока не поддерживается.

## Лимиты контейнеров и скриптов

Docker задаёт для браузера предел памяти 2,75 ГиБ, 4 CPU, 512 PID и shared memory 256 МиБ. Общий лимит RAM+swap также равен 2,75 ГиБ: дополнительный swap контейнеру не разрешён. Для control-сервиса заданы 768 МиБ, 2 CPU и 128 PID, также без дополнительного swap. Docker сообщает для Linux VM всего около 3,877 ГиБ памяти, из которых на момент проверки было доступно около 2,49 ГиБ. Это фактический предел среды Docker; значение `memory=48GB` в `.wslconfig` само по себе не отражает объём, который сейчас видит Docker. Лимиты изолируют процессы, но не доказывают отсутствие OOM: тяжёлый сайт всё ещё может исчерпать доступную память и привести к остановке worker.

Ограничение одновременных HTTP-запросов составляет 32 для control-сервиса и 16 для worker.

Скрипт `show_demo` использует только HTTP и не запускает Chrome. Это smoke-тест одного поля, а не полный прогон нескольких сайтов через MCP. Межпроцессная блокировка не даёт параллельно запустить несколько demo-сессий. В памяти остаются не более 64 исходных PNG-кадров суммарным размером до 32 МиБ; для GIF они уменьшаются до 720×450.

Последний полный прогон через MCP (задача `6dcc05d5-4768-419f-bb80-3ca84baa55a8`) запущен и прочитан через MCP со статусом `succeeded`: 20 шагов, 101,44 секунды, три успешные проверки и ноль исправлений. Python.org → Nodejs.org → Selenium: версии и точные значения полей отдельно сверены с источниками; форма не отправлялась. Пройдено 106 тестов; в 43 замерах ресурсов наблюдались один контекст и одна страница без нарушений. Доступны [результат](../runtime/demo/verified-rich-result.json), [запись](../runtime/demo/verified-rich-live.gif) и [замеры ресурсов](../runtime/demo/verified-rich-resources.json). Этот результат относится к указанному прогону.

## Ввод мышью и клавиатурой в ОС

Ввод через PyAutoGUI пока экспериментальный: он не реализован и не гарантирует невидимость действий или отсутствие влияния на пользователя. Если его будут добавлять, план — запускать ввод внутри контейнера через X11, сохранять viewport 1440×900 и отдельно калибровать смещение окна и фокус. Описание API и поведения находится в [официальной документации PyAutoGUI](https://pyautogui.readthedocs.io/en/latest/).


### Browser memory incident note (2026-10-29)

The browser container was OOM-killed twice while limited to 2 GiB. The exact child process and historical peak are unknown; restarting the container reset cgroup peak counters. After restart, one idle browser page peaked at about 749 MiB. The default browser hard memory and RAM-plus-swap limit is now 2,816 MiB (2.75 GiB), leaving roughly 1.13 GiB of the reported 3.877 GiB Docker VM capacity for the control container and VM overhead. This is an interim bounded mitigation based on the observed incident, not evidence of a universal browser memory leak or a guarantee under heavy workloads. For another OOM, inspect `memory.events`, `memory.current`, and `memory.peak` in the browser container. Docker currently reports less memory than the 48 GB configured in `.wslconfig`; changing Docker Desktop/WSL allocation and restarting it requires a separate maintenance window.

### Browser recovery behavior

The worker watches for canonical-page close/crash and browser disconnects, and performs a bounded liveness probe between operations. It can recover only after active action, input, import, observation and screenshot work has finished. Recovery closes the old persistent context before relaunching the same profile, preserves its cookies and SQLite ledgers, and invalidates the previous control lease and observation. The control runtime must reacquire control and observe the page again. Recovery never repeats a browser action or task. If an external dispatch may have happened but its result is unknown, an operator must verify the outcome before resuming. Resource violations, unverified egress and unconfirmed process teardown block recovery. The worker makes at most three attempts in a rolling five-minute window, with 2, 10 and 30 second delays; after three failures it stays blocked until the window expires or the worker is restarted. `browser_instance_id` changes when the worker process starts; `recovery_generation` increments after each in-process browser recovery.
