# Локальная очистка артефактов

Скрипт `scripts/cleanup_project_artifacts.ps1` проверяет и удаляет узкий набор игнорируемых локальных файлов из основного checkout и worktree `readiness-2026-10-30`. Скрипт работает из любого из этих двух корней. Docker volumes и развёрнутые сервисы вне его области действия.

## Сначала проверьте план

Без параметров скрипт только показывает план:

```powershell
.\scripts\cleanup_project_artifacts.ps1
```

Эквивалентный явный режим проверки:

```powershell
.\scripts\cleanup_project_artifacts.ps1 -WhatIf
```

В отчёте указаны каждый путь, число файлов и объём в байтах. В каждом checkout рассматриваются только эти каталоги генерируемого кэша:

- `scripts/__pycache__`
- `src/browser_mcp/__pycache__`
- `tests/__pycache__`
- `worker/__pycache__`
- `.pytest_cache`

Кроме них скрипт проверяет каталоги первого уровня `runtime/pytest-*`. Каталог сохраняется, если его имя или относительный путь упомянуты в Markdown/HTML из `docs`, исходниках `scripts` либо `tests` любого из checkout. Если исходник ссылки нельзя прочитать, запуск прекращается. Прочие runtime-данные, включая `demoqa`, `readiness` и `readiness-2026-10-30`, вне области удаления.

Пути, упомянутые в документации, по умолчанию сохраняются. Для двух проверенных старых прогонов предусмотрен отдельный точечный флаг. Он допускает удаление `runtime/pytest-final-release` только если SHA-256 `runtime/readiness-2026-10-30/pytest-final.xml` совпадает в worktree и уже сохранённой копии основного checkout; аналогичная проверка для `runtime/pytest-p0-baseline` использует `pytest-p0-baseline.xml`. Без обоих совпадающих файлов каталоги останутся.

После просмотра списка примените обычную очистку:

```powershell
.\scripts\cleanup_project_artifacts.ps1 -Apply
```

Для очистки только двух проверенных архивных прогонов сначала изучите dry run, затем примените:

```powershell
.\scripts\cleanup_project_artifacts.ps1 -IncludeArchivedPytestRuns
.\scripts\cleanup_project_artifacts.ps1 -IncludeArchivedPytestRuns -TargetedReparseCleanup
.\scripts\cleanup_project_artifacts.ps1 -IncludeArchivedPytestRuns -TargetedReparseCleanup -Apply
```

Удаляются только обычные файлы и пустые обычные каталоги. Обход не следует за reparse points и пропускает каталоги `secrets`, `profiles`, `profile`, `cookies`, `cookie`, `state`, `chrome-profile` и `chrome-profiles`. Если в pytest-каталоге остался такой каталог, reparse point или другой объект, контейнер останется.

## Сохраните readiness evidence до удаления worktree

Перед удалением worktree сохраните выбранные артефакты в основном checkout:

```powershell
.\scripts\cleanup_project_artifacts.ps1 -PreserveEvidence
.\scripts\cleanup_project_artifacts.ps1 -PreserveEvidence -Apply
```

Копируются только обычные файлы `.json`, `.jsonl`, `.png`, `.xml`, `.gif` и `.csv` из трёх путей worktree: `runtime/demoqa`, `runtime/readiness` и `runtime/readiness-2026-10-30`. Reparse points и чувствительные каталоги пропускаются. Скрипт сохраняет относительные пути внутри `runtime` основного checkout. Совпадающие файлы остаются без изменений; при конфликте содержимого скрипт останавливается и ничего не перезаписывает. Сначала изучите dry-run список и разберите все конфликты.

Скрипт не удаляет `debug.log`, tracked-файлы, vendor assets, Git metadata, секреты, профили браузера, пользовательские файлы и production state. Перед удалением он читает Git index, чтобы убедиться, что в цели нет tracked-файлов; состояние Git не меняет. Он не запускает тесты и не вызывает Docker. Если неиспользуемый pytest-каталог остался только из-за reparse links, есть отдельный режим удаления самих ссылок в worktree:

```powershell
.\scripts\cleanup_project_artifacts.ps1 -TargetedReparseCleanup
.\scripts\cleanup_project_artifacts.ps1 -TargetedReparseCleanup -Apply
```

В этом режиме удаляется только объект ссылки, цель не разрешается и не затрагивается. Ссылка должна находиться внутри каталога первого уровня `runtime/pytest-*`, на который нет ссылок из исходников или документации. Reparse points основного checkout и ссылки вне этих временных каталогов остаются на месте.

## Результат очистки 31.10.2026

Очистка выполнена в двух локальных checkout до удаления worktree. Из исходного worktree в основной checkout скопирован 141 файл readiness evidence; SHA-256 исходников и копий совпал. Сохранены deployment before/after, rollback, pytest XML, визуальные отчёты, DemoQA JSONL и общий fingerprint seed.

- 58 целей обычных артефактов: 11 907 файлов, 548 692 206 байт.
- Два проверенных архивных pytest-каталога: 1 647 файлов, 77 607 078 байт; их XML-отчёты проверены по SHA-256.
- Всего удалено 13 554 обычных файлов (626 299 284 байта по длинам файлов) и 2 768 reparse link объектов. Ссылки удалялись без обхода их целей.

Рабочая папка worktree остаётся на месте до завершения объединения этой инструкции и скрипта в основную ветку; после этого её можно удалить.
