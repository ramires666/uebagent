# Совместимость browser-use-camoufox и browser-use-undetected

Дата проверки: **2026-10-27**. Проверены локальный зафиксированный исходник WebRobot, официальный wheel и JSON PyPI для `browser-use-undetected`. Пакеты не устанавливались в системное окружение, контейнеры или среду проекта; код инициализации браузера не запускался.

## Вывод

`browser-use-undetected` 0.2.5.10 нельзя считать совместимым с закреплённым `browser-use-camoufox` только на основании их описаний. Undetected требует distribution `browser-use>=0.2.5` и в документации импортирует `Agent` как `browser_use.agent.service.Agent`. Форк WebRobot устанавливается как другая distribution (`browser-use-camoufox`), но намеренно предоставляет тот же импортный пакет `browser_use`. Обычная установка addon может добавить upstream `browser-use` в то же окружение, создав двух владельцев одних и тех же путей импорта. Это ненадёжная схема: порядок установки и состав файлов могут определить, какой код будет импортирован. Фактическое совместное разрешение импортов не проверялось.

Зависимости также не дают подтверждения совместимости: WebRobot закрепляет `pydantic==2.12.5` и `mcp==1.26.0`; приложение этого проекта требует `pydantic==2.13.4` и `mcp==1.27.2`. У undetected зависимости `pydantic>=2.10.6` и `browser-use>=0.2.5`, поэтому её metadata сама по себе не закрепляет ни версию Pydantic, ни совместимый вариант Browser Use. Совместимость API addon с данным форком не установлена.

Практичный текущий вариант — оставить WebRobot Camoufox единственным используемым browser driver проекта. Worker сейчас загружает только отдельный файл adapter из закреплённого исходника через `importlib.util`, а не устанавливает весь пакет `browser_use`. Оценивать `browser-use-undetected` следует отдельно, в изолированном окружении, проверяя его API относительно той же версии форка. Только после проверки можно решить, есть ли функция, которую стоит адаптировать. Это ещё не интеграция addon.

**Требование интеграции `browser-use-undetected` остаётся открытым (G0.6).** Получены версия, wheel, metadata и заявленные зависимости; не доказаны импортная совместимость, поведение addon с WebRobot, пригодность его proxy/CAPTCHA функций для сервиса или необходимость переносить конкретную функцию. Claims о stealth, прокси и CAPTCHA приведены самим проектом и здесь не проверялись.

## Проверенные версии и metadata

| Параметр | WebRobot fork | `browser-use-undetected` |
|---|---|---|
| Точная версия | `0.12.7+webrobot.1` | `0.2.5.10` |
| Источник закрепления | `vendor/browser-use-camoufox`, commit `7293a9b33fac988a1485566d0bdecffeda856c9f` | Официальный wheel с PyPI, загружен 2025-06-02 |
| Distribution name | `browser-use-camoufox` | `browser-use-undetected` |
| Import namespace | `browser_use` | addon предоставляет `browser_use_undetected`; он также импортирует `browser_use` |
| Требование Python | `>=3.11,<4.0` | `>=3.11,<4.0` |
| Browser Use dependency | — | `browser-use>=0.2.5` |
| Остальные прямые зависимости | Включая `pydantic==2.12.5`, `mcp==1.26.0` | `camoufox[geoip]>=0.4.11`, `capsolver>=1.0.0`, `playwright-recaptcha>=0.5.0`, `psutil>=7.0.0`, `pydantic>=2.10.6` |

Наше приложение требует Python `>=3.12,<3.14`, `pydantic==2.13.4` и `mcp==1.27.2`. Установка полного WebRobot distribution вместе с приложением уже потребовала бы разрешить конфликт точных версий; в текущем worker этот distribution не устанавливается целиком. Установка undetected по его опубликованной зависимости, в свою очередь, запрашивает distribution `browser-use`, а не явно закреплённый `browser-use-camoufox`.

Официальная metadata wheel содержит `Requires-Dist: browser-use>=0.2.5` без верхней границы. README addon показывает оба namespace в одном примере: `browser_use_undetected.StealthBrowserSession` и `browser_use.agent.service.Agent`. Поэтому отдельный namespace addon не снимает конфликт базового Browser Use. Сам wheel содержит файлы `browser_use_undetected/`; это не свидетельствует о совместимости с кодом `browser_use` из WebRobot.

PyPI README addon также помечает CAPTCHA solving как не полностью функциональный/проверенный и humanizer как проблемный с combobox/dropdown. Это заявления сопровождающего проекта, а не результаты испытаний в этом сервисе.

## Сохранённые доказательства

В `runtime/compatibility/` сохранены ответ JSON PyPI, исходный wheel и извлечённый файл `.dist-info/METADATA`. Wheel скачан с официального `files.pythonhosted.org`; его локальный SHA-256 проверен и совпал с digest в PyPI JSON.

| Артефакт | SHA-256 |
|---|---|
| `browser_use_undetected-0.2.5.10-py3-none-any.whl` | `220127aeeb3c31c664f67b0984fab1270c5d85d333c3f09e21ee916d5cc7e8fe` |
| `browser-use-undetected-0.2.5.10.METADATA` | `73742c5d041de0690bcf815598323a128dda23729c65d9a0a6b6d2212c0acdc4` |
| `browser-use-undetected-0.2.5.10-pypi.json` | `4e11ad80344ef293b11b031ad2737d62858daff9b68c571854e8bd26c73435ae` |

Сохранённый `browser-use-undetected-0.2.5.10-pypi.json` — официальный JSON endpoint релиза и содержит digest/URL wheel. Сам wheel является первичным источником `METADATA`. Снимок отражает релиз `0.2.5.10`, загруженный 2025-06-02; он не утверждает, что эта версия остаётся последней после даты проверки.

## Ссылки

- [Официальная страница проекта на PyPI](https://pypi.org/project/browser-use-undetected/)
- [Официальный JSON metadata конкретного релиза](https://pypi.org/pypi/browser-use-undetected/0.2.5.10/json)
- [Wheel 0.2.5.10 на PyPI CDN](https://files.pythonhosted.org/packages/b4/9e/b7e1ad4df2498a387b8148c43803a1085919c218d85627dd2988934f5573/browser_use_undetected-0.2.5.10-py3-none-any.whl)
- [GitHub repository addon](https://github.com/BARKEM-JC/browser-use-undetected)
- [Закреплённый fork WebRobot](https://github.com/WebRobot-Ltd/browser-use-camoufox/tree/7293a9b33fac988a1485566d0bdecffeda856c9f)
- [Локальная metadata fork: `vendor/browser-use-camoufox/pyproject.toml`](../vendor/browser-use-camoufox/pyproject.toml)
- [Локальная загрузка adapter: `worker/app.py`](../worker/app.py)


## Дополнительная проверка исходного API (2026-10-27)

Wheel `browser-use-undetected==0.2.5.10` установлен с `--no-deps` в одноразовом контейнере; production не изменялся. Его `stealth_session.py` импортирует `logger` и `_log_pretty_path` из `browser_use.browser.session`, но закреплённый форк `7293a9b33fac988a1485566d0bdecffeda856c9f` не определяет и не импортирует эти имена. Кроме того, `StealthBrowserSession.start()` вызывает отсутствующие в форке методы `setup_playwright`, `setup_browser_via_passed_objects`, `setup_browser_via_wss_url` и `setup_new_browser_context`. Это проверка исходного API, а не совместный запуск пакетов.

По умолчанию addon включает `auto_solve_captchas=True`; импорт не является интеграцией. Геолокация в конфигурации worker явно отключена. Внешние сервисы CAPTCHA и GeoIP не вызывались. Функциональная интеграция addon остаётся открытой; утверждение о незаметности браузера не проверено.
