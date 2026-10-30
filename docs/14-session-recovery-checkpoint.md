# Session recovery checkpoint

Updated: 2026-10-30

This checkpoint preserves the current state so work can resume in a fresh Codex host session without repeating the investigation.

## Verified state

- The control panel is published at `http://localhost:8090`; the local llama.cpp API is on port `8080`.
- The model on port 8080 currently serves Bonsai 27B (`W:\LAMA\Bonsai-demo\models\bonsai2-gguf\27B\Ternary-Bonsai-2-27B-PQ2_0.gguf`). The generic client selects the served ID using alias `auto`.
- On 2026-10-30, control-only deployment completed. The active control container is `browser-mcp-local-control-1` (`b8f7636bc52967316dfdba2de74fefd6b5f66477955b825bcb7a6aade950613c`) on image `sha256:b9009550f18109d1e97d9fd437232413c1c511dfef31cb166c2551ac196548b6`, with tag `browser-mcp-local-control:latest`. The prior image remains available as `browser-mcp-local-control:pre-compact-generic-20261030` (`sha256:c7a8ac64ce637c95d4783269b995c2dcd569620783b6ea885c30f138ce91462d`).
- Extension source is version 0.1.3 and uses the 8090 origin. The running browser container/profile were not recreated or modified; its existing `worker_state` and `browser_profile` volumes remain mounted. Control mounts the existing `control_data` volume and the worker credentials volume read-only. Compose networking remains `service_net` plus `browser_direct_uplink` for browser, and `service_net` plus `host_model_net` for control.
- Post-deployment `/health/ready` returned `ready=true`. `/api/v1/status` reports generic protocol, model ready with the single served Bonsai 27B ID, worker ready, and one source. `/api/v1/desktop/state` reports one page and context, owner `none`, `action_in_progress=false`, and `import_in_progress=false`. It reports the configured 1440×900 viewport.
- Read-only inspection inside the active control container confirmed the installed package import path, `MODEL_ALIAS=auto`, base URL `http://host.docker.internal:8080/v1`, and exact source/UI tree hashes matching the workspace: `src/browser_mcp` `c84f3e73d4e7b675856f4c803cf88083b0f186f4bc5531c1943c10ee031cd8bd`; `ui` `a0e3d01a5851c3be80bd84c3c416bf72cd85b84eb564936c5e2e6e08d0082dfa`.
- Browser container `browser-mcp-local-browser-1` was not restarted: its container ID remains `8b10a262d54abcf38923e7e72499e3cc5ec268a125c559d6afa67b8020c6b69a`, image remains `sha256:e1680f9daac32d492de83754c9e24eaccc284a0e94b55e120ced3d1fef3ca1a7`, and `worker_state` / `browser_profile` volume mounts remain unchanged.
- A local API takeover check returned HTTP 200 and then reported owner `human` at epoch 96. This is a point-in-time observation; do not assume the owner or epoch is still current.
- The last task `9b400...` was waiting for human input after `MODEL_OUTPUT_INVALID`. Its final browser-side effects are unknown. Do not replay, retry, or resubmit that task.
- The user’s private browser page was not used for testing. The synthetic fixture run used no external network and attempted no submission.

## Source verification

- The generic tool schema is now flat at the top level to avoid llama.cpp returning empty arguments for a `oneOf` schema. The production action normalizer remains strict.
- The generic prompt explicitly distinguishes normalized coordinates from screenshot pixels.
- Synthetic Bonsai smoke: High selected, exact test phrase entered, 4 actions, about 17 seconds, zero wrong clicks, invalid outputs, or submit attempts.
- Scoped tests: `tests/test_model_client.py` and `tests/test_desktop_viewer.py` passed, 50 total. The pytest process also printed a Windows temp-directory cleanup `PermissionError` after the successful result.
- Root’s independent final full suite passed: **344 passed, 6 skipped** in 71.22 seconds, using `runtime/pytest-input-generic-final-20261029` as its temporary directory.
- Root’s independent actual Bonsai smoke passed on the synthetic fixture: High selected, exact test phrase entered, 4 actions, 12.989 seconds, zero wrong clicks, invalid outputs, or submit attempts. It used no private browser.

## Resume sequence

1. Docker inspection works under the normal approval review in the restarted host session. Read-only checks are complete; no Docker deployment or image rebuild has been performed.
2. Review the pending full-suite and root smoke results. Fix only a reported failure; otherwise keep the source frozen.
3. After UI and source freeze, prepare a control-only update from the reviewed workspace source and the tagged existing control image using `deploy/Dockerfile.control-overlay`. Preserve both compose configurations, volumes, and current image digests. Keep model API port 8080 and panel/API port 8090.
4. Before deployment, inspect the resolved configuration to confirm generic protocol and model alias `auto`. Apply only the control service with `--no-deps`; do not recreate or restart the browser/worker, change its profile, modify API keys/passwords, or switch the model back to Fara by default.
5. After deployment, verify control health reports generic protocol, discovers the exact served model ID, and reports one online source and one capture. Confirm the source/container revision actually loaded the reviewed generic client and UI.
6. Keep the private browser under human control. Do not touch its page or submit anything as part of deployment checks. A new real task must be freshly authorized by the user; never resume or replay the unknown last task. For that fresh request, verify the requested High setting and exact text before one authorized submit.

## Deployment gate

The control-only deployment completed and its health check passed. This does not establish production readiness or validate the separate RAM test. The generic-model overlay is active, while the browser container and persistent profile volumes remain untouched. Keep the private browser under human control and do not replay the unknown prior task.
## Runtime и готовность на 2026-10-30

- Текущий Docker Desktop 4.91 предоставляет LinuxKit VM через VMM (`UseLibkrun=true`). `docker info --format '{{.MemTotal}}'` показал 8384647168 байт (около 8 GiB). Активного Docker Desktop WSL-дистрибутива нет; лимит 48 GiB из `.wslconfig` к этой VM не применяется. Это измеренный лимит данной машины, не настройка по умолчанию.
- Разделяйте память Windows, лимит Linux VM Docker Desktop и лимит cgroup контейнера. Проверка: `docker info --format '{{.MemTotal}}'` и `docker inspect --format '{{.HostConfig.Memory}}' <container-name>` (оба результата в байтах; `0` у inspect означает отсутствие заданного лимита).
- Production container limit остаётся 2816 MiB. Тест RAM с лимитом browser-контейнера 4 GiB завершился FAIL через 670,56 с (11,18 мин), после 3 446 циклов; peak памяти достиг примерно 100% лимита. API оставался функциональным, но требуемое действие завершилось `MANUAL_ACTION_NOT_APPLIED:navigate`. Отчёт и подробные результаты: [readiness report](readiness-2026-10-30/REPORT.md). Production GO не объявлен.
- После теста исходные production containers восстановлены; postflight показал DemoQA worker/tasks ready с 29 задачами и model preflight ready. Эти проверки не доказывают production readiness. Модель Bonsai/llama.cpp на Windows, API `localhost:8080`, alias `auto`, и headed Camoufox в Linux-контейнере с программным рендерингом остаются без изменений; GPU браузеру не нужен. Следующее сравнение live GC ещё ожидает выполнения; улучшение памяти не подтверждено.
