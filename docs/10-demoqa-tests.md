# DemoQA: сквозные проверки и результат

Живые тесты выполняются последовательно через MCP и TaskRunner/Fara к отдельному Camoufox worker. Условия успеха независимо проверяются worker API. Все введённые данные синтетические. Итоговая таблица объединяет отдельные серийные прогоны после исправлений, а не один общий запуск.

## Текущее покрытие

| Сценарий | Результат и артефакт |
|---|---|
| `text_box` | PASS: exact synthetic `test@example.com`, все восемь проверок полей и вывода; `runtime/demoqa/results-v4.jsonl`. |
| `invalid_email` | PASS: exact `test@example.test` осталось в поле, результат не отправлен, страница добавила `field-error`; `results-v4.jsonl`. HTML validity API при этом оставался `true`, поэтому проверяется фактический класс страницы. |
| `checkbox` | PASS: Desktop выбран; уникальный `[role="checkbox"][aria-label="Select Desktop"]` независимо подтвердил checked/ARIA true и результат `desktop`; 3 клика, `runtime/demoqa/results-v6checkboxfinal2.jsonl`. |
| `radio` | PASS: выбрано Yes, состояние независимо проверено; `runtime/demoqa/results.jsonl`. |
| `double_click` | PASS: сообщение о двойном щелчке независимо проверено; `runtime/demoqa/results.jsonl`. |
| `single_click` | PASS: сообщение о щелчке независимо проверено; `runtime/demoqa/results.jsonl`. |
| `web_table` | PASS: новая нативная table row с exact `test@example.com`; независимо совпали имя, фамилия, возраст, salary и department; 22 шага, `runtime/demoqa/results-v6geckopref.jsonl`. |
| `dynamic` | PASS: кнопка стала видимой и состояние независимо проверено; `runtime/demoqa/results.jsonl`. |
| `notify_then_click` | PASS: одно уведомление и подтверждённый click; `runtime/demoqa/results-v4.jsonl`. |

В сценарии checkbox поддерево Home раскрывается, Desktop остаётся видимым; выбранные дочерние пункты естественны для поведения дерева. Старый селектор `.rct-title` устарел. Текущий DemoQA обозначает цель доступным именем `Select Desktop`.

## Исправление рендеринга system-ui

На production естественный заголовок `Web Tables` теперь читается при исходном CSS weight 100. PNG после rollout: `runtime/demoqa/production-font-fixed.png` (129115 bytes; SHA-256 `b3b5d620a0e49f4bc2d229d26c42ebe374a92ab01af78800d43ebaa385e0db07`). Причина была в macOS системном generic `system-ui` face в Camoufox. Проверенный worker задаёт Firefox family preferences для `system-ui` x-western и x-unicode через `Arial`, не меняя сохранённую identity-конфигурацию. Fingerprint digest остался `958ee3c18bd1870a48fdf4d4bb3f88d93217c54f4844a59cc671ca2521f9b3d3`; системные шрифты целиком не копировались и OS persona не заменялась.

Текст body/sidebar на candidate PNG выглядит жирнее прежнего. Заголовок и текст читаемы; наблюдение сохранено для review в `docs/09-verification.md`. Временные A/B кадры до принятого Gecko-pref исправления — в `runtime/demoqa/v6styles/`, `v6thin/`, `v6alias/`, `v6systemfont/` и `v6sysalias/`; естественный candidate frame — `runtime/demoqa/v6geckopref/baseline.png`.

## Изолированный запуск

Одновременная работа двух Camoufox worker запрещена. `scripts/run-demoqa-tests.ps1` сначала собирает образы, проверяет production на idle, останавливает точный browser container, поднимает отдельный проект с отдельным секретом и состоянием, выполняет сценарии последовательно, удаляет только disposable containers/network в `finally` и восстанавливает production. Проверка idle включает отсутствие queued/running/recovering tasks, ручного владельца, действия или импорта cookies, а также лимит один context/одна page. Production volumes и cookies не подключаются к тестовому стеку.

Из корня проекта:

```powershell
.\scripts\run-demoqa-tests.ps1 -Cases 'text_box,invalid_email,checkbox,web_table,notify_then_click' -ExtendedComplex
```

`-ExtendedComplex` задаёт 180 секунд и максимум 40 шагов для `text_box`, `checkbox` и `web_table`. Для read-only font probe используйте `-ProbeOnly`. После визуальной проверки естественного снимка точечное выполнение только checkbox/table требует явного флага `-RunCasesAfterReadableCandidate` и per-run marker; без marker модельные действия не запускаются. JSONL и снимки располагаются в `runtime/demoqa/`.

## История

Предыдущие ошибочные наблюдения и их результаты сохранены без перезаписи в `results-v2.jsonl`, `results-v3.jsonl`, `results-v4.jsonl` и `results-v6.jsonl`. Первый неудачный checkbox verifier показывал визуально выбранный Desktop, но устаревший мета-селектор не находил состояние; повторный тест подтвердил его точной ARIA-проверкой. Неожиданный текстовый прогон, случайно запущенный ранней версией `-ProbeOnly`, помечен отдельно в v6 и не засчитывается как часть read-only проверки.

Загрузка файлов, browser dialogs, iframe и несколько окон в этом наборе не проверялись и не считаются пройденными.
