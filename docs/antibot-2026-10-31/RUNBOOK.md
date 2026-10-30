# Повторный запуск disposable antibot test

Инструкция воспроизводит ограниченный тест на публичных гостевых страницах. Она не подключается к production, не берёт production key и не доказывает универсальный PASS для сайтов/WAF. Нужны Docker Compose v2, локальные образы из compose, модельный API generic на host `:8080` и Windows PowerShell 5. Не публикуйте вывод `docker inspect`, compose config или значения переменных окружения.

## Образы и отдельный профиль

Overlay копирует проверенный `src/browser_mcp` и UI в базовый control image; browser image не меняется. Проверенная базовая метка: `browser-mcp-readiness-control:candidate`.

```powershell
docker image inspect browser-mcp-readiness-control:candidate *> $null
if ($LASTEXITCODE -ne 0) { throw 'Required local candidate image is missing' }
docker build --build-arg BASE_CONTROL_IMAGE=browser-mcp-readiness-control:candidate -f deploy/Dockerfile.control-overlay -t browser-mcp-antibot-control:prompt-3184fb4 .
if ($LASTEXITCODE -ne 0) { throw 'Overlay build failed' }
```

Compose с фиксированным именем использует уже существующий volume `browser-mcp-antibot-2026-10-31-worker-state-p2`; повторное использование не является fresh profile. Для новой серии создайте временную копию compose с новым project name и точным новым volume name:

```powershell
$runName = 'browser-mcp-antibot-rerun-' + [guid]::NewGuid().ToString('N')
$composeTmp = Join-Path $env:TEMP ($runName + '.compose.yaml')
$composeText = Get-Content -LiteralPath scripts/antibot-2026-10-31.compose.yaml -Raw
$composeText = $composeText -replace '(?m)^name: browser-mcp-antibot-2026-10-31\r?$', "name: $runName"
$composeText = $composeText.Replace('name: browser-mcp-antibot-2026-10-31-control-state', "name: $runName-control-state")
$composeText = $composeText.Replace('name: browser-mcp-antibot-2026-10-31-worker-state-p2', "name: $runName-worker-state-p2")
Set-Content -LiteralPath $composeTmp -Value $composeText -Encoding UTF8
```

Остановите только прежний antibot test project, если он держит порт `18081`; команда сохраняет его volume. `down` требует interpolation key из compose, поэтому задайте заведомо неиспользуемую заглушку только на время `down`, затем удалите её. Не останавливайте другие Compose projects:

```powershell
$env:ANTIBOT_WORKER_KEY = 'compose-down-only-placeholder'
try { docker compose -f scripts/antibot-2026-10-31.compose.yaml down }
finally { Remove-Item Env:\ANTIBOT_WORKER_KEY -ErrorAction SilentlyContinue }
```

Создайте новый случайный test-only key в памяти PowerShell. Команда явно задаёт `ANTIBOT_WORKER_KEY` в текущем процессе, поэтому Compose использует свежую переменную, а не значение из `.env`; сам ключ не записывается в файл или вывод. Сервисы записывают его в tmpfs и удаляют переменную при старте. Не копируйте ключ из production:

```powershell
$randomBytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($randomBytes)
$env:ANTIBOT_WORKER_KEY = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
[Array]::Clear($randomBytes, 0, $randomBytes.Length)
$rng.Dispose()
try { docker compose -f $composeTmp up -d --no-build browser control }
finally { Remove-Item Env:\ANTIBOT_WORKER_KEY -ErrorAction SilentlyContinue }
if ($LASTEXITCODE -ne 0) { throw 'Disposable test stack did not start' }
```

Откройте `http://localhost:18081/desktop`. Убедитесь, что открыт test viewer, одна страница/контекст, owner `none`, viewport `1440×900`; runtime guard также проверяет `APP_ENV=test`, `READINESS_DISPOSABLE=1`, worker `browser:7070`, route assignment `antibot-2026-10-31-direct` и generation `1`. Site tasks используют `sync_cookies=false` и свежий worker volume. Не открывайте production UI `:8090`.

## Модельная DemoQA задача

Скопируйте helper в работающий disposable control: скрипты не включены в control image. Команда запускает ограниченную задачу через task API; helper дополнительно откажется стартовать при другой активной задаче.

```powershell
$controlId = docker ps --filter "label=com.docker.compose.project=$runName" --filter 'label=com.docker.compose.service=control' --format '{{.ID}}'
if (-not $controlId) { throw 'Disposable control container is missing' }
docker cp scripts/antibot_model_task.py "${controlId}:/tmp/antibot_model_task.py"
docker exec -e ANTIBOT_PROFILE=p2 $controlId python /tmp/antibot_model_task.py --i-confirm-disposable --output /tmp/antibot-artifacts/demoqa/model-task.json
```

Helper сам ограничивает origin `https://demoqa.com`, task profile `default`, `autonomous`, 12 шагов и 300 секунд. PASS требует независимой проверки домашней страницы и промежуточной Elements страницы, trace `click` → `history_back`, и отсутствие write actions. `BLOCKED` ставится только при наблюдённом challenge; неполный trace/быстрый переход без milestone — `PARTIAL`. Отчёт не содержит screenshot, prompt или сырой model summary.

Здесь `ANTIBOT_PROFILE=p2` — только подпись test evidence; это не `TaskSpec.profile_id` и само по себе не делает worker state fresh. Новый volume определяется уникальным `$runName`, его следует сохранить рядом с отчётом.

Скопируйте только безопасный JSON отчёт вне контейнера:

```powershell
$dest = "runtime/antibot-2026-10-31/model-task/$runName"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
docker cp "${controlId}:/tmp/antibot-artifacts/demoqa/model-task.json" (Join-Path $dest 'model-task.json')
```

## Другие harness сценарии и ограничения

- `scripts/antibot_probe.py` принимает `ANTIBOT_SITE` (`sannysoft`, `browserleaks_js`, `browserleaks_webgl`, `eff`, `demoqa_elements`, `amazon_home`, `ebay_home`), `ANTIBOT_PROFILE=p1|p2`, `ANTIBOT_RUN`; он проверяет страницу после заданной навигации. `scripts/antibot_demoqa_flow.py` принимает `ANTIBOT_PROFILE` и выполняет детерминированный DemoQA home → Elements → back через manual navigation. Эти harness результаты не подтверждают, что модель выполнила переходы.
- `scripts/antibot_guest_flow.py` использует `ANTIBOT_GUEST_SITE=amazon|ebay`, `ANTIBOT_GUEST_STAGE=home|search|product`, `ANTIBOT_PROFILE=p1|p2`. Product шаг требует визуально выбранных `ANTIBOT_PRODUCT_TITLE`, `ANTIBOT_PRODUCT_X`, `ANTIBOT_PRODUCT_Y`; без осмотра текущего screenshot/title не запускайте его. Это пиксельные координаты текущего layout, не селектор.
- `scripts/antibot_eff_finish.py` теперь запускает только корректирующий `ANTIBOT_PROFILE=p2`, `ANTIBOT_EFF_RUN=2`; `ANTIBOT_EFF_WAIT` по умолчанию `45` секунд, допустимо `5..90`. Скрипт проверяет текст CTA/notice, затем делает один manual click по координатам `278,564` с удержанием human gate на bounded wait и verifier; перед любым CTA действием заново визуально подтвердите кнопку и её координаты на текущем screenshot `1440×900`. Если layout изменился — не нажимайте. `ANTIBOT_EFF_POST_DEADLINE_OBSERVE=1` — отдельный режим только для двух read-only наблюдений существующего P2 состояния, без gate, навигации и кликов. Выводы: `/tmp/antibot-artifacts/eff/corrective-gate-hold/metadata.json`, `/tmp/antibot-artifacts/eff/corrective-gate-hold/post-deadline-observation.json`; опубликованные сводки находятся в `evidence/eff/`. Текущий корректирующий результат `PARTIAL`, summary values не сохраняются; post-deadline наблюдение не подтвердило строки результата и осталось `PARTIAL`. Wait `45` — только настройка bounded ожидания этого сценария, не общий timeout или метрика производительности. Исторические P1/P2 прогоны использовали прежнюю версию helper и не воспроизводят текущий run2. В текущей серии повторный CTA запуск не предусмотрен.
- Статусы `PARTIAL`, `BLOCKED`, `UNAVAILABLE` и `SKIPPED` остаются отдельными результатами; не объединяйте их в общий PASS. Ручной headed контроль и модельная задача — отдельные доказательства.

## Остановка и точечная очистка

Выполняйте следующие точечные команды в `finally` блока запуска, чтобы остановить только этот compose project и убрать временные файлы даже при ошибке. `docker ps` находит control по точным Compose labels, не выполняя Compose interpolation после удаления fresh key. Для `down` снова используйте не-секретную заглушку. Не используйте `down -v` и не удаляйте volumes wildcard-командами. Свежий worker volume сохраните для provenance или удалите позже только по точному `$runName-worker-state-p2` после проверки имени.

```powershell
docker exec $controlId rm -f /tmp/antibot_model_task.py /tmp/antibot-artifacts/demoqa/model-task.json
$env:ANTIBOT_WORKER_KEY = 'compose-down-only-placeholder'
try { docker compose -f $composeTmp down }
finally { Remove-Item Env:\ANTIBOT_WORKER_KEY -ErrorAction SilentlyContinue }
Remove-Item -LiteralPath $composeTmp
Remove-Item Env:\ANTIBOT_WORKER_KEY -ErrorAction SilentlyContinue
```
