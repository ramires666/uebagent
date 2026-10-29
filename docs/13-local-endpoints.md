# Local service endpoints

The local control panel and its HTTP/WebSocket API use `http://localhost:8090` on the Windows host. Compose binds this to `127.0.0.1:8090` and forwards it to port `8080` inside the control container. The extension API uses the same origin.

The model endpoint, as seen from the control container, is `http://host.docker.internal:8080/v1`. The default model selector is `auto`; the client/model selector compatibility must be verified before sending a chat completion. The model server is a separate Windows process and is not managed by Docker Compose.

`scripts/start-fara.ps1` now defaults to model port `8080` for future starts. It refuses an occupied port and does not stop an existing process. The panel remains on port `8090`.

The selected `deploy/compose.direct.yaml` overlay remains in effect when applying only the control service. The browser service and its existing profile are not restarted as part of a control-only port change.

Current verification: control `/health/live` on port `8090` returned HTTP 200; from the control container, model `/health` and `/v1/models` on `host.docker.internal:8080` returned HTTP 200. No chat completion or browser task was sent during this check.
