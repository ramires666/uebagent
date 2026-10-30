# Подготовка обновления production `:8090`

**Статус: candidate подготовлен к apply; обновление ещё не применялось.** Pre-deploy point-in-time DB checkpoints созданы в существующих persistent volumes. Production контейнеры не пересоздавались, browser UI не открывался, task не запускался. Apply остаётся за главным агентом после review и повторной live-проверки непосредственно перед командой.

## Зафиксированное read-only состояние

Последняя проверка `2026-10-31` получила:

- `http://127.0.0.1:8090/health/live`: `ok`; `/health/ready`: `true`.
- `/api/v1/status`: model protocol `generic`, model/worker `ready=true`, `sources_count=1`, route mode `direct`.
- `/api/v1/desktop/state`: `available=true`, owner `none`, `action_in_progress=false`, `import_in_progress=false`, один context/page, 1440×900.
- Read-only aggregation таблицы tasks по status: `cancelled=14`, `expired=5`, `succeeded=11`; active statuses не обнаружены. Запрос не читает и не печатает task goals/specs.
- Текущие containers были `Up`: `browser-mcp-local-control-1` id `78ba1213b0f215b30e9c782e066109c56820e546a5caaf68aa2eda0a1529a689`; `browser-mcp-local-browser-1` id `4e17b205d1edd4077ab0dce588c4b575f92c30f19c5167d7a0a683a795603fcb`.
- Текущие image IDs: control `sha256:af945993ffc971785cd12c49cefa1269bf29bcd97004374d23b8ad68bfe73aa9`; browser `sha256:24eb3e58f686356401a4cb43ce999eeae31b40fe190085766b22a0fc1cf53f18`. Rollback tags `browser-mcp-readiness-control:candidate` и `browser-mcp-readiness-browser:candidate` разрешаются ровно в эти же IDs.
- Candidate tags существуют локально: control `browser-mcp-antibot-control:prompt-3184fb4` → `sha256:cb2f97ff4e56fa9d9ea5713eafe30ccd076b66032d1bf31ce3186df8bf9c1509`; browser `browser-mcp-remediation-browser:ebay-assets-b437221` → `sha256:b08d4f831b224ad848b64fc03cdcfae5fdad61fc5ab327e4ea51f52445e2f6d4`.
- Compose labels текущих containers указывают файлы `deploy/compose.yaml`, `deploy/compose.direct.yaml`, `.worktrees/readiness-2026-10-30/deploy/compose.readiness-candidate.yaml`; `deploy/compose.model-auth.yaml` в активном наборе отсутствует. Label-путь старого worktree overlay больше не существует. Подготовленное применение использует актуальный source-файл `deploy/compose.readiness-candidate.yaml` вместо отсутствующей копии.
- Control limit: 805306368 bytes (768 MiB); browser limit/swap: 4294967296 bytes (4096 MiB). Volumes: `browser-mcp-local_control_data` → `/app/runtime/control-data`; `browser-mcp-local_worker_state` → `/app/runtime/worker-state`; `browser-mcp-local_browser_profile` → `/app/runtime/worker-state/profiles/default`; external `browser-mcp-worker-credentials` mounted read-only. Сети сохраняют `service_net`, `host_model_net`, `browser_direct_uplink`.

Это точечный snapshot; owner/task state может измениться. Перед apply повторить live gate. Любой `human` owner, незавершённая задача, действие/import в процессе или неожиданное состояние означает **HOLD**. Не прерывать сессию и не снимать human ownership автоматически. `GET /api/v1/operator/session` не использовался: в local-auth режиме endpoint может создать/обновить операторскую сессию; состояние владения проверялось через desktop state.

## Candidate

В [`deploy/compose.readiness-candidate.yaml`](../../deploy/compose.readiness-candidate.yaml) изменены только два `image:` значения:

| Service | Reviewed image | Image ID |
|---|---|---|
| control | `browser-mcp-antibot-control:prompt-3184fb4` | `sha256:cb2f97ff4e56fa9d9ea5713eafe30ccd076b66032d1bf31ce3186df8bf9c1509` |
| browser | `browser-mcp-remediation-browser:ebay-assets-b437221` | `sha256:b08d4f831b224ad848b64fc03cdcfae5fdad61fc5ab327e4ea51f52445e2f6d4` |

Локальные image tags уже существуют, `pull_policy: never`; rebuild/pull не нужны. Control image содержит reviewed prompt `3184fb4`; browser image соответствует source fix `b437221`. Кандидаты прошли заявленные root проверки: EFF functional pass `25.048 s` с явными разрешёнными origins, всеми summary predicates и без блоков; two model tasks, включая DemoQA и eBay, завершились PASS; 46 model-client unit tests PASS; source pytest после worker fix — 556 passed, 8 skipped за 105.86 s (финальный suite run). Это подтверждает перечисленные сценарии, а не глобальную anti-bot совместимость.

Overlay сохраняет base/direct-настройки, project `browser-mcp-local`, localhost port `8090`, generic model route, direct-development mode, `BROWSER_OBSERVATION_HINTS=1`, `BROWSER_MEMORY_MAINTENANCE=1`, browser 4 GiB limit/swap, networks, mounts, profile и control DB volume. `deploy/compose.model-auth.yaml` не добавлять: его нет в активных Compose labels. Не читать, печатать или менять API-key/credential volumes.

Sannysoft run 3 завершил все 7 diagnostic checks, но сохранил Chrome failure; BrowserLeaks P1/P2 diagnostics завершены в пределах policy. Fingerprint/IP не менялись. Mixed signals и Chrome failure сохранены; не маскировать их и не объявлять общий anti-bot/WAF PASS. Локальное обновление сервиса — не публичный release.

## Gate перед обновлением

Эти проверки не открывают браузер и не читают cookies:

```powershell
$ErrorActionPreference = 'Stop'
$live = Invoke-RestMethod 'http://127.0.0.1:8090/health/live' -TimeoutSec 5
$ready = Invoke-RestMethod 'http://127.0.0.1:8090/health/ready' -TimeoutSec 5
$state = Invoke-RestMethod 'http://127.0.0.1:8090/api/v1/desktop/state' -TimeoutSec 8
if ($live.status -ne 'ok' -or -not $ready.ready -or -not $state.available -or
    $state.control_owner -ne 'none' -or $state.action_in_progress -or $state.import_in_progress -or
    $state.context_count -ne 1 -or $state.page_count -ne 1) {
    throw 'Production is not idle/healthy; HOLD without changing ownership or stopping a task.'
}
```

Агрегировать task status без чтения specs:

```powershell
$taskQuery = @'
import sqlite3,json
c=sqlite3.connect("file:/app/runtime/control-data/tasks.sqlite3?mode=ro",uri=True)
print(json.dumps(c.execute("select status,count(*) from tasks group by status order by status").fetchall()))
c.close()
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($taskQuery))
$code = "import base64;exec(base64.b64decode('$encoded'))"
$counts = (docker exec browser-mcp-local-control-1 python -c $code | ConvertFrom-Json)
$active = @($counts | Where-Object { $_[0] -in @('queued','running','pause_requested','paused','waiting_human','waiting_approval','recovering') })
if ($active.Count -gt 0) { throw 'Active production task exists; HOLD without interrupting it.' }
```

Проверить локальные image IDs и Compose resolution, не печатая resolved environment:

```powershell
docker image inspect browser-mcp-antibot-control:prompt-3184fb4 browser-mcp-remediation-browser:ebay-assets-b437221 --format '{{.RepoTags}}|{{.Id}}'
docker image inspect browser-mcp-readiness-control:candidate browser-mcp-readiness-browser:candidate --format '{{.RepoTags}}|{{.Id}}'
docker compose -p browser-mcp-local -f deploy/compose.yaml -f deploy/compose.direct.yaml -f deploy/compose.readiness-candidate.yaml config --quiet
```

Сверить IDs с таблицами выше. Если tag отсутствует, ID не совпадает, Compose validation выдаёт ошибку, live gate не idle/healthy или authorization scope оказался шире указанного — HOLD и сообщить root, не запускать `up`.

## Запланированный apply — не запускался

Пользователь уже авторизовал deployment в текущей задаче. После успешного gate apply выполняется автономно в рамках этой авторизации; отдельный approval-step не нужен. Не менять project/network/profile/DB и не использовать `down`, `down -v`, `--build` или `--force-recreate`.

```powershell
docker compose -p browser-mcp-local -f deploy/compose.yaml -f deploy/compose.direct.yaml -f deploy/compose.readiness-candidate.yaml up -d --no-build --no-deps control browser
```

После команды проверить `docker compose ... ps`, health live/ready, отфильтрованные status fields, Desktop owner/available/1 context/page и сохранность прежних именованных volumes. Не выполнять browser navigation в deployment smoke. Если любой инвариант не проходит, выполнить rollback ниже.

## Point-in-time database checkpoints

Перед apply main создал один control и один worker SQLite checkpoint using [`scripts/remediation_db_checkpoint.py`](../../scripts/remediation_db_checkpoint.py). Скрипт запускался внутри каждого существующего service container, использовал SQLite backup API с `mode=ro` source connection и сохранил raw DB copies только в existing persistent data volumes:

| Role | Fixed source | Checkpoint location |
|---|---|---|
| control | `/app/runtime/control-data/tasks.sqlite3` | `/app/runtime/control-data/remediation-backups/<id>/tasks.sqlite3.checkpoint` |
| worker | `/app/runtime/worker-state/worker.sqlite3` | `/app/runtime/worker-state/remediation-backups/<id>/worker.sqlite3.checkpoint` |

The helper rejects test/debug `APP_ENV`, wrong data root/origin/route metadata, symlinked source paths and non-matching ID (`[a-z0-9][a-z0-9-]{0,63}`). It creates a new exclusive checkpoint directory (0700), database and metadata files (0600); it never overwrites an ID. Control metadata contains integrity status, table/status counts and a SHA-256 digest of sorted task ID/status pairs, without task goals, inputs, IDs or result values. Worker metadata contains integrity status and table counts only. No API keys, credentials, profile files or cookies are read. The script and stdout metadata may pass through host for invocation/inspection; raw database files stay inside their original volumes.

Команды запуска (справочно; сами checkpoint operations уже завершены):

```powershell
$checkpointId = 'remediation-20261031-b437221'
$controlScript = "/tmp/remediation-db-checkpoint-$checkpointId.py"
$workerScript = "/tmp/remediation-db-checkpoint-$checkpointId.py"
docker cp scripts/remediation_db_checkpoint.py "browser-mcp-local-control-1:$controlScript"
docker exec browser-mcp-local-control-1 python $controlScript --role control --checkpoint-id $checkpointId
docker cp scripts/remediation_db_checkpoint.py "browser-mcp-local-browser-1:$workerScript"
docker exec browser-mcp-local-browser-1 python $workerScript --role worker --checkpoint-id $checkpointId
```

Оба запуска завершились `CREATED`, `summary_matches_at_creation=true`, `integrity_check=ok`, directories `0700`, DB/metadata files `0600`. Checkpoint ID: `remediation-20261031-b437221`. Control task counts: 30 tasks (`cancelled=14`, `expired=5`, `succeeded=11`), 671 events, 13 capabilities; worker counts: 159 action ledger rows, 5 capability uses, 13 cookie-import records. Эти числа — структурные counts; содержимое task specs и cookie values не читалось и не записывалось в manifest.

Manifest copies (содержат только safe aggregates) находятся в [control metadata](deployment/before/control-metadata.json) и [worker metadata](deployment/before/worker-metadata.json); matching runtime copies — `runtime/remediation-2026-10-31/deployment/before/`. Raw checkpoint paths остались только в исходных volumes: `/app/runtime/control-data/remediation-backups/remediation-20261031-b437221/tasks.sqlite3.checkpoint` и `/app/runtime/worker-state/remediation-backups/remediation-20261031-b437221/worker.sqlite3.checkpoint`. Эти manifest copies хешированы после копирования; runtime и docs копии каждой совпадают. Profile/fingerprint seed hash в файлы не записывался.

- Control manifest SHA-256 (runtime/docs copies identical): `224BB74AD82CE271ACCD8BCEBF7B5FE6D4C6B353C454C7987B36F9E8BD1887B5`
- Worker manifest SHA-256 (runtime/docs copies identical): `84BBAB982B1C4CEEDCABFFDB2059B99DE4C9CB61F3B04245C3A56D0005B1C2D6`

Checkpoint directories persist with their current volumes through container recreation and image rollback. If either checkpoint operation had failed, deployment would remain on HOLD; existing IDs must never be reused or overwritten.

After successful apply and smoke, copy the helper into each then-current service container and use read-only verification mode with the same ID:

```powershell
docker cp scripts/remediation_db_checkpoint.py "browser-mcp-local-control-1:$controlScript"
docker exec browser-mcp-local-control-1 python $controlScript --role control --checkpoint-id $checkpointId --verify-only
docker cp scripts/remediation_db_checkpoint.py "browser-mcp-local-browser-1:$workerScript"
docker exec browser-mcp-local-browser-1 python $workerScript --role worker --checkpoint-id $checkpointId --verify-only
```

Require `VERIFIED` / `summary_matches=true`; any task status/digest or worker table-count difference is a HOLD for review. These checkpoints provide a point-in-time safety copy and comparison only; they do not prove schema migration compatibility or automatically restore a database. Do not restore/copy data as part of routine rollback unless a separate recovery procedure is reviewed.

## Rollback

Текущие старые image tags и IDs зафиксированы выше. `scripts/readiness_rollback.ps1` и `deploy/compose.readiness-rollback.yaml` — disposable rollback exercise в отдельном project/volumes, а не production rollback runner. Предыдущие isolated results подтверждают profile fingerprint в этом disposable цикле; отдельная control DB migration marker verification не проводилась. В этом candidate нет намеренного изменения DB schema или volume mapping. Production rollback возвращает старые images на те же mounts; volumes не удаляются. Два point-in-time DB checkpoints теперь созданы внутри persistent volumes, но off-volume backup archive или production-specific restore wrapper отсутствует; checkpoints не восстановят данные при независимом повреждении volume. Не копировать raw DB наружу и не выполнять restore автоматически.

Создать временный override для точных старых image tags с прежними runtime limits/hints и вернуть только эти два services:

```powershell
$rollbackOverlay = Join-Path $env:TEMP 'browser-mcp-local-readiness-rollback.yaml'
@'
services:
  control:
    image: browser-mcp-readiness-control:candidate
    pull_policy: never
  browser:
    image: browser-mcp-readiness-browser:candidate
    pull_policy: never
    mem_limit: 4096m
    memswap_limit: 4096m
    environment:
      BROWSER_OBSERVATION_HINTS: "1"
      BROWSER_MEMORY_MAINTENANCE: "1"
'@ | Set-Content -LiteralPath $rollbackOverlay -Encoding UTF8

docker compose -p browser-mcp-local -f deploy/compose.yaml -f deploy/compose.direct.yaml -f $rollbackOverlay config --quiet
docker compose -p browser-mcp-local -f deploy/compose.yaml -f deploy/compose.direct.yaml -f $rollbackOverlay up -d --no-build --no-deps control browser
```

После rollback сверить running `.Image` IDs со старыми IDs из таблицы, live/ready, owner none, action/import idle, один context/page, все четыре mount names. Затем удалить только временный override. Не удалять/очищать profile, worker-state, control DB или credentials volumes. Если old image ID/tag, volume names, health или idle gate отличаются — остановиться и сообщить root до любых дополнительных действий.

## Границы

- Docker inspect, image-tag checks, localhost GET и task aggregate выполнены read-only; SQLite использовал URI `mode=ro`.
- Во время подготовки не выполнялись Docker Compose `up/down`, volume edits, external navigation или browser interaction.
- Кандидат обновляет локальный runtime для известных сценариев; он не сертифицирует WAF, public release или поведение в иной OS/network.
