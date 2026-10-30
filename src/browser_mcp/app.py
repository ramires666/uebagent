from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, StrictBool

from .auth import AuthService
from .chrome import ChromeBroker
from .config import Settings
from .domain import DomainError, TaskSpec, _normalize_origin
from .runtime import TaskRunner
from .storage import TaskRepository


class TaskCookieImporter:
    """Keep manual imports unchanged and authorize auto-sync only for its active task."""

    def __init__(self, repository: TaskRepository, worker: Any):
        self.repository = repository
        self.worker = worker

    async def can_import(self) -> bool:
        return await self.worker.can_import()

    async def import_cookies(self, job_id: str, records: list[dict]) -> dict:
        return await self.worker.import_cookies(job_id, records)

    async def can_import_for_task(self, task_id: str) -> bool:
        try:
            record = self.repository.get(task_id)
            if record.spec.profile_id != "default" or record.status.value != "running":
                return False
            active = [item for item in self.repository.list(limit=1000)
                      if item.status.value not in {"cancelled", "expired", "failed", "succeeded"}]
            if len(active) != 1 or active[0].task_id != task_id:
                return False
            state = await self.worker.state()
        except Exception:
            return False
        if (state.get("control_owner") != "none" or state.get("action_in_progress") or
                state.get("import_in_progress") or state.get("browser_started") is not True or
                state.get("context_count") != 1 or state.get("page_count") != 1 or
                state.get("resource_violation") is not False):
            return False
        return True

    async def import_cookies_for_task(self, task_id: str, job_id: str, records: list[dict]) -> dict:
        if not await self.can_import_for_task(task_id):
            raise DomainError("IMPORT_CONTROL_BUSY", "The active task does not own the idle browser cookie import gate")
        return await self.worker.import_cookies(job_id, records)


def create_app(settings: Settings | None = None, worker=None, model=None) -> FastAPI:
    settings = settings or Settings()
    settings.prepare()
    repository = TaskRepository(settings.data_dir / "tasks.sqlite3")
    runner = TaskRunner(settings, repository, worker=worker, model=model)
    importer = TaskCookieImporter(repository, runner.worker)
    broker = ChromeBroker(settings.data_dir, importer, settings.catalog_ttl_s, repository=repository)
    runner.cookie_sync = broker.sync_task_cookies
    auth = AuthService(settings.operator_token, settings.session_ttl_s)
    public = urlsplit(settings.public_origin)
    allowed_origins = {settings.public_origin, f"{public.scheme}://localhost:{public.port or 80}", f"{public.scheme}://127.0.0.1:{public.port or 80}"}
    if settings.test_mode:
        allowed_origins.add("http://testserver")
    mcp = FastMCP("Local Browser MCP", stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                      allowed_hosts=["localhost:*", "127.0.0.1:*", "testserver"], allowed_origins=list(allowed_origins)))

    def redact_user_inputs(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).casefold()
                words = [part for part in re.split(r"[^a-z0-9]+", words) if part]
                sensitive = bool(set(words) & {
                    "password", "passwd", "secret", "token", "cookie", "authorization",
                    "credential", "auth", "otp", "totp", "mfa", "pin", "passcode",
                }) or (("api" in words or "access" in words or "private" in words) and "key" in words) or (
                    "session" in words and "id" in words) or (
                    "verification" in words and bool(set(words) & {"code", "token", "otp"}))
                result[key] = "[REDACTED]" if sensitive else redact_user_inputs(item)
            return result
        if isinstance(value, list):
            return [redact_user_inputs(item) for item in value]
        return value

    def record_response(record) -> dict:
        data = record.model_dump(mode="json")
        # Keep the source values in the repository for task execution and
        # repeat-task editing, but never echo fields marked sensitive by name.
        data["spec"]["user_inputs"] = redact_user_inputs(data["spec"].get("user_inputs", {}))
        return data

    def record_data(task_id: str) -> dict:
        return record_response(repository.get(task_id))

    async def object_body(request: Request) -> dict:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    @mcp.tool()
    async def browser_task_start(goal: str, allowed_origins: list[str], idempotency_key: str,
                                 profile_id: str = "default", success_conditions: list[dict] | None = None,
                                 user_inputs: dict | None = None,
                                 verbatim_text: Annotated[str | None, Field(min_length=1, max_length=100_000)] = None,
                                 limits: dict | None = None,
                                 sync_cookies: StrictBool = True,
                                 execution_mode: Literal['supervised', 'autonomous'] = 'autonomous',
                                 site_capabilities: list[Literal['gemini_prompt_submit']] | None = None,
                                 workflow: Literal['agent', 'gemini_prompt_submit'] = 'agent') -> dict:
        """Start a bounded task; optionally lock one typed payload to exact verbatim_text."""
        spec = TaskSpec(goal=goal, allowed_origins=allowed_origins, idempotency_key=idempotency_key,
                        profile_id=profile_id, success_conditions=success_conditions or [],
                        user_inputs=user_inputs or {}, verbatim_text=verbatim_text,
                        sync_cookies=sync_cookies,
                        execution_mode=execution_mode, site_capabilities=site_capabilities or [], workflow=workflow,
                        **({"limits": limits} if limits else {}))
        return record_response(runner.create(spec))

    @mcp.tool()
    async def browser_task_status(task_id: str) -> dict:
        """Read durable task state, without issuing browser actions."""
        return record_data(task_id)

    @mcp.tool()
    async def browser_task_events(task_id: str, after_event_id: int = 0, limit: int = 100) -> dict:
        """Read sanitized events; cookie values and typed secrets are not returned."""
        return {"events": repository.events(task_id, after_event_id, min(limit, 100))}

    @mcp.tool()
    async def browser_task_result(task_id: str) -> dict:
        """Read verified result or the reason human help is needed."""
        return record_data(task_id)

    @mcp.tool()
    async def browser_task_pause(task_id: str) -> dict:
        return record_response(await runner.control(task_id, "pause"))

    @mcp.tool()
    async def browser_task_resume(task_id: str, expected_version: int | None = None) -> dict:
        return record_response(await runner.control(task_id, "resume", expected_version))

    @mcp.tool()
    async def browser_task_cancel(task_id: str) -> dict:
        return record_response(await runner.control(task_id, "cancel"))

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            await runner.start()
            try:
                yield
            finally:
                await broker.close()
                await runner.close()
                repository.close()

    app = FastAPI(title="Browser MCP local control", lifespan=lifespan)
    app.state.settings, app.state.runner, app.state.broker = settings, runner, broker
    app.state.repository, app.state.mcp = repository, mcp

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        status = 401 if exc.code == "UNAUTHORIZED" else 403 if exc.code in {"CSRF_INVALID", "POLICY_DENIED"} else 404 if exc.code.endswith("NOT_FOUND") else 429 if exc.code == "RATE_LIMITED" else 409
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid_fields(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Проверьте поля запроса",
                                       "fields": [list(error["loc"]) for error in exc.errors()]}}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_json(request: Request, exc: ValueError):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Некорректный JSON или значение"}}, status_code=422)

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.url.hostname not in {"localhost", "127.0.0.1", "testserver" if settings.test_mode else "localhost"}:
            return JSONResponse({"error": {"code": "HOST_DENIED", "message": "Недопустимый Host"}}, status_code=403)
        origin = request.headers.get("origin", "")
        pair_path = request.url.path == "/api/v1/extension/pair"
        extension_origin = origin.startswith("chrome-extension://") and len(origin.split("://")[-1]) == 32
        if origin and origin not in allowed_origins and not (pair_path and extension_origin):
            return JSONResponse({"error": {"code": "ORIGIN_DENIED", "message": "Недопустимый Origin"}}, status_code=403)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Некорректный Content-Length"}}, status_code=400)
        if length < 0:
            return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Некорректный Content-Length"}}, status_code=400)
        if length > settings.max_message_bytes:
            return JSONResponse({"error": {"code": "REQUEST_TOO_LARGE", "message": "Запрос слишком велик"}}, status_code=413)
        renewed_session = None
        try:
            if request.url.path.startswith("/mcp"):
                supplied = request.headers.get("authorization", "")
                if not secrets.compare_digest(supplied, f"Bearer {settings.mcp_token}"):
                    raise DomainError("UNAUTHORIZED", "Нужен отдельный MCP API key")
            elif request.url.path.startswith("/api/v1/"):
                exempt = pair_path or (request.url.path == "/api/v1/operator/session" and
                    (request.method == "POST" or settings.operator_auth_mode == "local" and request.method == "GET"))
                if not exempt:
                    try:
                        auth.check(request.cookies.get("operator_session", ""), request.headers.get("x-csrf-token", ""),
                                   mutate=request.method not in {"GET", "HEAD", "OPTIONS"})
                    except DomainError as exc:
                        if settings.operator_auth_mode == "local" and request.method in {"GET", "HEAD"} and exc.code == "UNAUTHORIZED":
                            renewed_session = auth.create_session(local=True)
                        else:
                            raise
            body = bytearray()
            body_too_large = False
            async for chunk in request.stream():
                if len(body) + len(chunk) > settings.max_message_bytes:
                    body_too_large = True
                    break
                body.extend(chunk)
            if body_too_large:
                response = JSONResponse({"error": {"code": "REQUEST_TOO_LARGE", "message": "Запрос слишком велик"}}, status_code=413)
            else:
                # BaseHTTPMiddleware's downstream request reuses this cached
                # body, so parsing never has to read an unbounded stream again.
                request._body = bytes(body)
                response = await call_next(request)
        except DomainError as exc:
            response = await domain_error(request, exc)
        if renewed_session:
            session_id, csrf = renewed_session
            response.set_cookie("operator_session", session_id, httponly=True, samesite="strict", secure=public.scheme == "https", max_age=settings.session_ttl_s)
            response.headers["X-CSRF-Token"] = csrf
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'"
        if pair_path and extension_origin:
            response.headers["Access-Control-Allow-Origin"] = origin
        return response

    @app.post("/api/v1/operator/session")
    async def login(request: Request):
        body = await object_body(request)
        token = body.get("token", "")
        if not isinstance(token, str) or len(token) > 512:
            raise DomainError("UNAUTHORIZED", "Некорректный ключ оператора")
        session_id, csrf = auth.login(token, request.client.host if request.client else "local")
        response = JSONResponse({"authenticated": True, "csrf_token": csrf, "local_ui": settings.operator_auth_mode == "local"})
        response.set_cookie("operator_session", session_id, httponly=True, samesite="strict", secure=public.scheme == "https", max_age=settings.session_ttl_s)
        return response

    @app.get("/api/v1/operator/session")
    async def session(request: Request):
        try:
            current = auth.check(request.cookies.get("operator_session", ""))
            return {"authenticated": True, "csrf_token": current.csrf_token, "local_ui": settings.operator_auth_mode == "local"}
        except DomainError:
            if settings.operator_auth_mode != "local":
                raise
        session_id, csrf = auth.create_session(local=True)
        response = JSONResponse({"authenticated": True, "csrf_token": csrf, "local_ui": True})
        response.set_cookie("operator_session", session_id, httponly=True, samesite="strict", secure=public.scheme == "https", max_age=settings.session_ttl_s)
        return response

    @app.delete("/api/v1/operator/session")
    async def logout(request: Request):
        auth.sessions.pop(request.cookies.get("operator_session", ""), None)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("operator_session", httponly=True, samesite="strict")
        return response

    @app.get("/health/live")
    async def live():
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/v1/status")
    async def status():
        model_health, worker_health = await asyncio.gather(runner.model.health(), runner.worker.health())
        return {"model": model_health, "worker": worker_health, "profiles": [{"profile_id": "default", "label": "Основной Camoufox"}],
                "sources_count": len(broker.sources), "proxy": {
                    "mode": "direct" if worker_health.get("proxy_assignment_id") == "direct-development" else "external",
                    "detail": "Прямой тестовый маршрут" if worker_health.get("proxy_assignment_id") == "direct-development" else "Внешний маршрут настраивается на хосте"}}

    @app.get("/health/ready")
    async def ready():
        info = await status()
        ok = info["model"].get("ready") and info["worker"].get("ready")
        return JSONResponse({"ready": bool(ok)}, status_code=200 if ok else 503)

    @app.get("/api/v1/tasks")
    async def tasks():
        return {"tasks": [record_response(x) for x in repository.list()]}

    @app.post("/api/v1/tasks")
    async def start_task(spec: TaskSpec):
        return record_response(runner.create(spec))

    @app.get("/api/v1/tasks/{task_id}")
    async def task(task_id: str):
        return record_data(task_id)

    @app.get("/api/v1/tasks/{task_id}/events")
    async def task_events(task_id: str, after_event_id: int = 0):
        return {"events": repository.events(task_id, after_event_id, 100)}

    @app.post("/api/v1/tasks/{task_id}/site-capabilities")
    async def grant_site_capability(task_id: str, request: Request):
        body = await object_body(request)
        if set(body) != {'capability', 'expected_version'} or body.get('capability') != 'gemini_prompt_submit' or isinstance(body.get('expected_version'), bool) or not isinstance(body.get('expected_version'), int):
            raise DomainError('CAPABILITY_INVALID', 'Некорректный запрос разрешения')
        return repository.grant_capability(task_id, body['capability'], body['expected_version'])

    @app.post("/api/v1/tasks/{task_id}/{operation}")
    async def task_control(task_id: str, operation: str, request: Request):
        if operation not in {"pause", "resume", "cancel", "takeover", "release-control", "review-recovery"}:
            raise DomainError("CAPABILITY_UNAVAILABLE", "Неизвестное действие")
        body = await object_body(request)
        return record_response(await runner.control(task_id, operation, body.get("expected_version")))

    @app.post("/api/v1/extension/pairing-codes")
    async def pairing_code():
        return broker.pairing_code()

    @app.post("/api/v1/extension/pair")
    async def pair(request: Request):
        body = await object_body(request)
        extension_id = str(body.get("extension_id", ""))
        if request.headers.get("origin") != f"chrome-extension://{extension_id}":
            raise DomainError("UNAUTHORIZED", "Сопряжение доступно из интерфейса расширения")
        return broker.pair(str(body.get("code", "")), extension_id, str(body.get("label", "Chrome")))

    @app.websocket("/api/v1/extension/channel")
    async def extension_channel(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        host = (ws.url.hostname or "").lower()
        if host not in {"localhost", "127.0.0.1", "testserver" if settings.test_mode else "localhost"} or not origin.startswith("chrome-extension://"):
            await ws.close(code=1008)
            return
        await ws.accept()
        source = None
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=3)
            if len(raw.encode()) > 4096:
                raise DomainError("UNAUTHORIZED", "Некорректная авторизация")
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise DomainError("UNAUTHORIZED", "Некорректная авторизация")
            if message.get("type") != "auth":
                raise DomainError("UNAUTHORIZED", "Нужна авторизация")
            source = broker.authenticate(str(message.get("source_id", "")), str(message.get("token", "")), origin)
            previous = source["socket"]
            source["socket"], source["last_seen"] = ws, time.monotonic()
            if previous and previous is not ws:
                await previous.close(code=1000)
            await ws.send_json({"type": "authenticated"})
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60)
                if len(raw.encode()) > settings.max_message_bytes:
                    await ws.close(code=1009)
                    return
                message = json.loads(raw)
                if not isinstance(message, dict):
                    await ws.close(code=1008)
                    return
                source["last_seen"] = time.monotonic()
                if message.get("type") == "heartbeat":
                    await ws.send_json({"type": "heartbeat_ack"})
                elif message.get("type") == "result":
                    broker.receive(source["source_id"], message)
                else:
                    await ws.close(code=1008)
                    return
        except (DomainError, ValueError, TypeError, WebSocketDisconnect, TimeoutError, RuntimeError):
            try:
                await ws.close(code=1008)
            except RuntimeError:
                pass
        finally:
            if source and source["socket"] is ws:
                source["socket"] = None

    @app.get("/api/v1/chrome/sources")
    async def sources():
        return {"sources": broker.list_sources()}

    @app.post("/api/v1/chrome/sources/{source_id}/open-sites/refresh")
    async def refresh(source_id: str):
        return broker.refresh_sites(source_id)

    @app.get("/api/v1/chrome/sources/{source_id}/open-sites")
    async def open_sites(source_id: str):
        return broker.sites(source_id)

    @app.post("/api/v1/chrome/sources/{source_id}/cookie-catalogs")
    async def catalog(source_id: str, request: Request):
        data = await object_body(request)
        return broker.catalog(source_id, data.get("site_ids", []), data.get("discovery_revision", ""))

    @app.get("/api/v1/chrome/cookie-catalogs/{catalog_id}")
    async def get_catalog(catalog_id: str):
        return broker.get_catalog(catalog_id)

    @app.post("/api/v1/profiles/{profile_id}/cookie-import-jobs")
    async def import_job(profile_id: str, request: Request):
        return broker.import_job(profile_id, await object_body(request))

    @app.get("/api/v1/profiles/{profile_id}/cookie-import-jobs")
    async def list_import_jobs(profile_id: str):
        return {"jobs": broker.list_imports(profile_id)}

    @app.get("/api/v1/profiles/{profile_id}/cookie-import-jobs/{job_id}")
    async def get_import_job(profile_id: str, job_id: str):
        return broker.get_import(profile_id, job_id)

    @app.get("/api/v1/desktop/info")
    async def desktop_info(profile_id: str = "default"):
        health = await runner.worker.health()
        available = bool(health.get("configured") and health.get("browser_started", health.get("ready")))
        return {"available": available, "browser_started": bool(health.get("browser_started", available)),
                "url": "/desktop" if available else None, "detail": health.get("detail", "")}

    @app.get("/api/v1/desktop/state")
    async def desktop_state():
        try:
            state = await runner.worker.state()
            return {"width": 1440, "height": 900, **state, "available": bool(state.get("browser_started", True))}
        except DomainError as exc:
            return {"available": False, "detail": exc.message}

    @app.get("/api/v1/capabilities/gemini-auth/status")
    async def gemini_auth_status():
        unavailable = {"status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}
        try:
            result = await runner.worker.call("GET", "/capabilities/gemini-auth/status")
        except Exception:
            return unavailable
        if not isinstance(result, dict):
            return unavailable
        status = result.get("status")
        reason_code = result.get("reason_code")
        valid_pairs = {
            "signed_in": {"ACCOUNT_CONTROLS_PRESENT"},
            "signed_out": {"SIGN_IN_CONTROL_PRESENT"},
            "unknown": {"PROOF_INCOMPLETE", "PROBE_UNAVAILABLE", "NOT_GEMINI"},
        }
        if (not isinstance(status, str) or not isinstance(reason_code, str) or
                status not in valid_pairs or reason_code not in valid_pairs[status]):
            return unavailable
        return {"status": status, "reason_code": reason_code}

    @app.get("/api/v1/desktop/screenshot")
    async def screenshot():
        return Response(await runner.worker.call("GET", "/screenshot", raw=True), media_type="image/png")

    @app.post("/api/v1/desktop/action")
    async def manual_action(request: Request):
        body = await object_body(request)
        if set(body) != {"control_epoch", "action"} or not isinstance(body["action"], dict):
            raise DomainError("INVALID_ACTION", "Некорректное ручное действие")
        action = body["action"]
        if action.get("kind") == "navigate":
            url = action.get("url")
            if set(action) != {"kind", "url"} or not isinstance(url, str) or not url or len(url) > 4096 or url != url.strip() or any(ord(char) < 32 or ord(char) == 127 for char in url):
                raise DomainError("INVALID_ACTION", "Некорректный адрес перехода")
            try:
                parsed = urlsplit(url)
                origin = _normalize_origin(f"{parsed.scheme}://{parsed.netloc}")
            except ValueError as exc:
                raise DomainError("INVALID_ACTION", "Некорректный адрес перехода") from exc
            body = {**body, "allowed_origins": [origin]}
        return await runner.worker.call("POST", "/manual/execute", body)

    @app.post("/api/v1/profiles/{profile_id}/takeover")
    async def profile_takeover(profile_id: str):
        if profile_id != "default":
            raise DomainError("PROFILE_NOT_FOUND", "Неизвестный профиль")
        active = next((r for r in repository.list() if r.status.value not in {"cancelled", "expired", "failed", "succeeded"}), None)
        if active:
            return record_response(await runner.control(active.task_id, "takeover"))
        return await runner.worker.gate("human")

    @app.post("/api/v1/profiles/{profile_id}/release-control")
    async def profile_release(profile_id: str):
        if profile_id != "default":
            raise DomainError("PROFILE_NOT_FOUND", "Неизвестный профиль")
        return await runner.worker.gate("none")

    app.router.routes.extend(mcp_app.routes)

    @app.get("/desktop", include_in_schema=False)
    async def desktop():
        return FileResponse(settings.ui_dir / "desktop.html")

    if settings.ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_dir, html=True), name="web")
    return app

