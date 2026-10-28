"""Explicit, short-lived catalog and export jobs for the paired Chrome extension."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .domain import DomainError, _normalize_origin


MAX_BACKGROUND = 8
MAX_PENDING = 64
MAX_CATALOGS = 16
MAX_IMPORT_RECEIPTS = 128
MAX_CATALOG_BYTES = 1_000_000
MAX_IMPORT_OUTCOME_BYTES = 24_000
IMPORT_WAIT_LIMIT_S = 30
MAX_TASK_SYNC_ORIGINS = 10
MAX_TASK_SYNC_COOKIES = 500
MAX_TASK_SYNC_BYTES = 1_000_000


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ChromeBroker:
    def __init__(self, data_dir: Path, importer: Any, catalog_ttl_s: int = 120, repository: Any = None):
        self.path = data_dir / "chrome-sources.json"
        self.importer = importer
        self.repository = repository
        self.ttl = catalog_ttl_s
        self.sources: dict[str, dict] = {}
        self.codes: dict[str, float] = {}
        self.pending: dict[str, tuple[str, asyncio.Future]] = {}
        self.catalogs: dict[str, dict] = {}
        self.imports: dict[str, dict] = {}
        self.import_keys: dict[str, tuple[str, str]] = {}
        self.background: set[asyncio.Task] = set()
        if self.path.exists():
            for item in json.loads(self.path.read_text(encoding="utf-8")):
                self.sources[item["source_id"]] = {**item, "socket": None, "last_seen": 0, "inventory": None}

    def spawn(self, coro: Any) -> None:
        try:
            self._require_background_capacity()
        except DomainError:
            close = getattr(coro, "close", None)
            if close:
                close()
            raise
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self._background_done)

    def _require_background_capacity(self) -> None:
        self.background.difference_update([task for task in self.background if task.done()])
        if len(self.background) >= MAX_BACKGROUND:
            raise DomainError("RESOURCE_LIMIT", "Too many Chrome extension operations are already running")

    def _background_done(self, task: asyncio.Task) -> None:
        self.background.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    def _expire_catalogs(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for catalog in self.catalogs.values():
            if catalog["status"] in {"ready", "failed"} and now - catalog["created_at"] > self.ttl:
                catalog["status"] = "expired"
                catalog["cookies"] = []
                catalog["sites"] = []

    def _make_catalog_room(self) -> None:
        self._expire_catalogs()
        while len(self.catalogs) >= MAX_CATALOGS:
            expired = next((key for key, item in self.catalogs.items() if item["status"] == "expired"), None)
            if expired is None:
                raise DomainError("CATALOG_LIMIT", "Too many active cookie catalogs; wait for one to expire")
            del self.catalogs[expired]

    async def close(self) -> None:
        for task in list(self.background):
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        for _, future in list(self.pending.values()):
            if not future.done():
                future.cancel()

    def pairing_code(self) -> dict:
        now = time.monotonic()
        self.codes = {key: value for key, value in self.codes.items() if value > now}
        if len(self.codes) >= 10:
            raise DomainError("RATE_LIMITED", "Слишком много активных кодов сопряжения")
        code = secrets.token_urlsafe(12)
        self.codes[code] = now + 120
        return {"code": code, "expires_in_s": 120}

    def pair(self, code: str, extension_id: str, label: str) -> dict:
        if not re.fullmatch(r"[a-p]{32}", extension_id):
            raise DomainError("PAIRING_INVALID", "Некорректный идентификатор расширения")
        deadline = self.codes.pop(code, 0)
        if deadline <= time.monotonic():
            raise DomainError("PAIRING_INVALID", "Код сопряжения недействителен или истёк")
        if len(self.sources) >= 10:
            raise DomainError("SOURCE_LIMIT", "Достигнут предел подключённых источников")
        source_id, token = secrets.token_urlsafe(16), secrets.token_urlsafe(32)
        self.sources[source_id] = {
            "source_id": source_id, "token_hash": digest(token), "extension_id": extension_id,
            "label": label[:80], "socket": None, "last_seen": 0, "inventory": None,
        }
        durable = [{key: value for key, value in item.items() if key in {
            "source_id", "token_hash", "extension_id", "label"
        }} for item in self.sources.values()]
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(durable), encoding="utf-8")
        temporary.replace(self.path)
        return {"source_id": source_id, "token": token}

    def source(self, source_id: str) -> dict:
        if source_id not in self.sources:
            raise DomainError("SOURCE_NOT_FOUND", "Источник Chrome не найден")
        return self.sources[source_id]

    def authenticate(self, source_id: str, token: str, origin: str) -> dict:
        source = self.source(source_id)
        if origin != f"chrome-extension://{source['extension_id']}" or not secrets.compare_digest(digest(token), source["token_hash"]):
            raise DomainError("UNAUTHORIZED", "Неверные данные источника Chrome")
        return source

    def list_sources(self) -> list[dict]:
        return [{"source_id": s["source_id"], "label": s["label"], "online": bool(s["socket"]) and time.monotonic()-s["last_seen"] < 60,
                 "last_seen": datetime.fromtimestamp(time.time()-(time.monotonic()-s["last_seen"]), UTC).isoformat() if s["last_seen"] else None} for s in self.sources.values()]

    async def ask(self, source_id: str, kind: str, payload: dict) -> dict:
        source = self.source(source_id)
        if not source["socket"]:
            raise DomainError("CHROME_SOURCE_OFFLINE", "Откройте расширение Chrome и подключите источник")
        request_id = secrets.token_urlsafe(16)
        if len(self.pending) >= MAX_PENDING:
            raise DomainError("RESOURCE_LIMIT", "Too many Chrome extension requests are already pending")
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = source_id, future
        try:
            await source["socket"].send_json({"type": "job", "request_id": request_id, "kind": kind, "payload": payload})
            return await asyncio.wait_for(future, timeout=45)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            raise DomainError("CHROME_SOURCE_OFFLINE", "Расширение не ответило вовремя") from exc
        finally:
            self.pending.pop(request_id, None)

    @staticmethod
    def _task_cookie_applies(cookie: dict, origin: str) -> bool:
        hostname = urlsplit(origin).hostname or ""
        domain = cookie["domain"].lstrip(".").encode("idna").decode("ascii").lower()
        host = hostname.encode("idna").decode("ascii").lower()
        return domain == host if cookie["host_only"] else host == domain or host.endswith("." + domain)

    def _task_cookie_records(self, result: Any, origins: list[str]) -> list[dict]:
        if not isinstance(result, dict) or set(result) != {"cookies", "allowed_origins"}:
            raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned an invalid response")
        allowed_origins = result.get("allowed_origins")
        if (not isinstance(allowed_origins, list) or
                any(not isinstance(origin, str) for origin in allowed_origins) or
                len(allowed_origins) != len(origins) or set(allowed_origins) != set(origins)):
            raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export did not match the requested sites")
        records = result.get("cookies")
        if not isinstance(records, list) or len(records) > MAX_TASK_SYNC_COOKIES:
            raise DomainError("COOKIE_SYNC_UNSUPPORTED", "The cookie export exceeds the supported size")
        try:
            encoded_size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned invalid data") from exc
        if encoded_size > MAX_TASK_SYNC_BYTES:
            raise DomainError("COOKIE_SYNC_UNSUPPORTED", "The cookie export exceeds the supported size")

        required = {"source_key", "site_ids", "name", "domain", "path", "store_id", "host_only",
                    "secure", "http_only", "same_site", "expires_at", "partition_key", "unsupported_reason", "value"}
        seen_sources: set[str] = set()
        seen_identities: set[tuple[str, str, str, str, bool]] = set()
        stores: set[str] = set()
        hosts = [(origin, (urlsplit(origin).hostname or "")) for origin in origins]
        validated: list[dict] = []
        for row in records:
            if not isinstance(row, dict) or set(row) != required:
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned invalid cookie metadata")
            source_key, name, domain, path, store_id = (row.get(key) for key in ("source_key", "name", "domain", "path", "store_id"))
            if (not all(isinstance(value, str) and value for value in (source_key, name, domain, path, store_id)) or
                    len(source_key) > 4096 or len(name) > 4096 or len(domain) > 253 or len(path) > 4096 or
                    not path.startswith("/")):
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned invalid cookie metadata")
            if source_key in seen_sources:
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export contained duplicate cookies")
            if any(not isinstance(row.get(key), bool) for key in ("host_only", "secure", "http_only")):
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned invalid cookie metadata")
            if row.get("same_site") not in {"unspecified", "no_restriction", "lax", "strict"}:
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned invalid cookie metadata")
            expires = row.get("expires_at")
            if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires)):
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned invalid cookie metadata")
            if not isinstance(row.get("value"), str) or len(row["value"]) > 16_384:
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned an invalid cookie value")
            if row.get("unsupported_reason") not in (None, "") or row.get("partition_key") not in (None, ""):
                raise DomainError("COOKIE_SYNC_UNSUPPORTED", "The site uses cookies that cannot be transferred safely")
            if row.get("site_ids") != []:
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned unexpected site metadata")
            try:
                normalized_domain = domain.lstrip(".").encode("idna").decode("ascii").lower()
                if not normalized_domain or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in normalized_domain.split(".")):
                    raise UnicodeError
            except UnicodeError as exc:
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export returned an invalid cookie domain") from exc
            normalized = {**row, "domain": ("." if domain.startswith(".") else "") + normalized_domain}
            if not any(self._task_cookie_applies(normalized, origin) for origin, _ in hosts):
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export included a cookie outside the requested sites")
            identity = (store_id, name, normalized_domain, path, row["host_only"])
            if identity in seen_identities:
                raise DomainError("COOKIE_SYNC_FAILED", "Chrome cookie export contained duplicate cookies")
            seen_sources.add(source_key)
            seen_identities.add(identity)
            stores.add(store_id)
            validated.append(normalized)
        if len(stores) > 1:
            raise DomainError("COOKIE_SOURCE_AMBIGUOUS", "Chrome returned cookies from more than one profile store")
        return validated

    async def sync_task_cookies(self, task_id: str, profile_id: str, origins: list[str]) -> dict[str, int]:
        """Export all applicable cookies once, import them through the task-scoped worker gate, and retain only counts."""
        if profile_id != "default":
            raise DomainError("PROFILE_NOT_FOUND", "Cookie sync is supported only for the default profile")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", task_id):
            raise DomainError("COOKIE_SYNC_FAILED", "The task identifier is invalid")
        if not isinstance(origins, list) or not 1 <= len(origins) <= MAX_TASK_SYNC_ORIGINS:
            raise DomainError("COOKIE_SYNC_UNSUPPORTED", "Cookie sync supports one to ten exact HTTPS origins")
        try:
            canonical_origins = [_normalize_origin(origin) for origin in origins]
        except (TypeError, ValueError) as exc:
            raise DomainError("COOKIE_SYNC_FAILED", "Cookie sync requires exact HTTPS origins") from exc
        if len(set(canonical_origins)) != len(canonical_origins):
            raise DomainError("COOKIE_SYNC_FAILED", "Cookie sync origins must be unique")
        if self.repository is None:
            raise DomainError("COOKIE_SYNC_UNAVAILABLE", "Task cookie sync storage is unavailable")

        job_id = f"taskcookie-{task_id}"
        events = self.repository.events(task_id, 0, 1000)
        for event in reversed(events):
            payload = event.get("payload", {})
            if event.get("event_type") == "cookie.sync_completed" and payload.get("job_id") == job_id:
                counts = payload.get("counts")
                if isinstance(counts, dict) and all(isinstance(counts.get(key), int) for key in ("total", "applied", "failed")):
                    return {key: counts[key] for key in ("total", "applied", "failed")}
                raise DomainError("ACTION_OUTCOME_UNKNOWN", "The prior cookie sync receipt is incomplete")

        started = any(event.get("event_type") == "cookie.sync_started" and
                      event.get("payload", {}).get("job_id") == job_id for event in events)
        if not started:
            self.repository.add_event(task_id, "cookie.sync_started", {
                "job_id": job_id, "origins_count": len(canonical_origins),
            })
        exported = None
        cookies: list[dict] = []
        try:
            now = time.monotonic()
            live = [source for source in self.sources.values()
                    if source.get("socket") is not None and now - source.get("last_seen", 0) < 60]
            if not live:
                raise DomainError("COOKIE_SOURCE_UNAVAILABLE", "No paired Chrome cookie source is connected")
            if len(live) != 1:
                raise DomainError("COOKIE_SOURCE_AMBIGUOUS", "Choose one paired Chrome cookie source before starting this task")
            source = live[0]
            try:
                exported = await self.ask(source["source_id"], "task_cookie_export", {"origins": canonical_origins})
            except DomainError as exc:
                code = exc.code
                if code == "UNSUPPORTED_JOB":
                    code = "COOKIE_SYNC_UNSUPPORTED"
                elif code not in {"CHROME_PERMISSION_REQUIRED", "COOKIE_SOURCE_AMBIGUOUS", "COOKIE_SOURCE_UNAVAILABLE", "COOKIE_SYNC_UNSUPPORTED"}:
                    code = "COOKIE_SYNC_FAILED"
                raise DomainError(code, {
                    "CHROME_PERMISSION_REQUIRED": "Grant the requested site permissions in the paired Chrome extension, then retry.",
                    "COOKIE_SOURCE_AMBIGUOUS": "The paired Chrome source could not choose a unique normal profile store.",
                    "COOKIE_SOURCE_UNAVAILABLE": "The paired Chrome source is unavailable.",
                    "COOKIE_SYNC_UNSUPPORTED": "Update the paired Chrome extension to enable automatic cookie sync.",
                    "COOKIE_SYNC_FAILED": "Chrome could not export cookies for this task.",
                }[code]) from exc
            cookies = self._task_cookie_records(exported, canonical_origins)
            if not cookies:
                counts = {"total": 0, "applied": 0, "failed": 0}
            else:
                can_import = getattr(self.importer, "can_import_for_task", None)
                import_for_task = getattr(self.importer, "import_cookies_for_task", None)
                if not callable(can_import) or not callable(import_for_task):
                    raise DomainError("COOKIE_SYNC_UNAVAILABLE", "Task-scoped cookie import is unavailable")
                deadline = time.monotonic() + IMPORT_WAIT_LIMIT_S
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DomainError("IMPORT_CONTROL_BUSY", "The browser remained busy before task cookie sync")
                    try:
                        ready = await asyncio.wait_for(can_import(task_id), timeout=min(5.0, remaining))
                    except TimeoutError:
                        ready = False
                    if ready:
                        break
                    await asyncio.sleep(min(0.25, remaining))
                try:
                    receipt = await asyncio.wait_for(import_for_task(task_id, job_id, cookies), timeout=IMPORT_WAIT_LIMIT_S)
                except TimeoutError as exc:
                    raise DomainError("ACTION_OUTCOME_UNKNOWN", "The cookie sync outcome is unknown") from exc
                if not isinstance(receipt, dict):
                    raise DomainError("ACTION_OUTCOME_UNKNOWN", "The worker returned an invalid cookie sync receipt")
                if receipt.get("outcome") == "unknown":
                    raise DomainError("ACTION_OUTCOME_UNKNOWN", "The cookie sync outcome is unknown")
                total = receipt.get("total", len(cookies))
                applied = receipt.get("applied", receipt.get("copied", 0))
                failed = receipt.get("failed", 0)
                if any(isinstance(value, bool) or not isinstance(value, int) for value in (total, applied, failed)):
                    raise DomainError("ACTION_OUTCOME_UNKNOWN", "The worker returned invalid cookie sync counts")
                if total != len(cookies) or applied + failed != total:
                    raise DomainError("ACTION_OUTCOME_UNKNOWN", "The worker returned incomplete cookie sync counts")
                counts = {"total": total, "applied": applied, "failed": failed}
                if failed or applied != total:
                    raise DomainError("COOKIE_SYNC_FAILED", "One or more task cookies could not be applied")
            self.repository.add_event(task_id, "cookie.sync_completed", {
                "job_id": job_id, "origins_count": len(canonical_origins), "counts": counts,
            })
            return counts
        except Exception as exc:
            approved_codes = {"CHROME_PERMISSION_REQUIRED", "COOKIE_SOURCE_UNAVAILABLE", "COOKIE_SOURCE_AMBIGUOUS",
                              "COOKIE_SYNC_UNSUPPORTED", "COOKIE_SYNC_FAILED", "COOKIE_SYNC_UNAVAILABLE",
                              "ACTION_OUTCOME_UNKNOWN", "IMPORT_CONTROL_BUSY", "PROFILE_NOT_FOUND"}
            safe_code = exc.code if isinstance(exc, DomainError) and exc.code in approved_codes else "COOKIE_SYNC_FAILED"
            self.repository.add_event(task_id, "cookie.sync_failed", {"job_id": job_id, "code": safe_code})
            if isinstance(exc, DomainError):
                raise
            raise DomainError("COOKIE_SYNC_FAILED", "Automatic cookie sync failed") from exc
        finally:
            # Do not retain Chrome cookie values in broker state or receipts, even
            # when validation, import, cancellation, or readback fails.
            for row in cookies:
                if isinstance(row, dict):
                    row.pop("value", None)
            if isinstance(exported, dict) and isinstance(exported.get("cookies"), list):
                for row in exported["cookies"]:
                    if isinstance(row, dict):
                        row.pop("value", None)
            cookies.clear()
            exported = None

    def receive(self, source_id: str, message: dict) -> None:
        pending = self.pending.get(message.get("request_id"))
        if not pending or pending[0] != source_id or pending[1].done():
            return
        if message.get("ok") is not True:
            error = message.get("error") or {}
            pending[1].set_exception(DomainError(str(error.get("code", "EXTENSION_JOB_FAILED"))[:80], str(error.get("message", "Ошибка расширения"))[:240]))
        elif not isinstance(message.get("data"), dict):
            pending[1].set_exception(DomainError("EXTENSION_INVALID", "Некорректный ответ расширения"))
        else:
            pending[1].set_result(message["data"])

    def refresh_sites(self, source_id: str) -> dict:
        source = self.source(source_id)
        if source["inventory"] and source["inventory"].get("status") == "pending":
            return {"request_id": source["inventory"]["discovery_revision"]}
        self._require_background_capacity()
        revision = secrets.token_urlsafe(12)
        source["inventory"] = {"status": "pending", "discovery_revision": revision, "sites": []}
        self.spawn(self._refresh(source_id, revision))
        return {"request_id": revision}

    async def _refresh(self, source_id: str, revision: str) -> None:
        source = self.source(source_id)
        try:
            data = await self.ask(source_id, "open_sites", {})
            sites = []
            seen = set()
            if not isinstance(data.get("sites"), list) or len(data["sites"]) > 500:
                raise DomainError("EXTENSION_INVALID", "Некорректный список сайтов")
            if len(json.dumps(data["sites"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 512_000:
                raise DomainError("EXTENSION_INVALID", "Chrome site inventory is too large")
            for item in data["sites"]:
                origin = _normalize_origin(item["origin"])
                site_id, store_id = str(item["site_id"]), str(item["store_id"])
                if not 0 < len(site_id) <= 4096 or len(store_id) > 128 or site_id in seen:
                    raise DomainError("EXTENSION_INVALID", "Некорректный идентификатор сайта")
                seen.add(site_id)
                sites.append({"site_id": site_id, "origin": origin, "store_id": store_id, "tab_count": max(1, min(int(item["tab_count"]), 10000))})
            if source["inventory"]["discovery_revision"] == revision:
                source["inventory"] = {"status": "ready", "discovery_revision": revision, "sites": sites, "at": time.monotonic()}
        except Exception as exc:
            if source["inventory"]["discovery_revision"] == revision:
                source["inventory"].update(status="failed", error=self.error(exc))

    @staticmethod
    def error(exc: Exception) -> dict:
        if isinstance(exc, DomainError):
            return {"code": exc.code, "message": exc.message}
        return {"code": "EXTENSION_INVALID", "message": "Получены некорректные данные или связь прервана"}

    def sites(self, source_id: str) -> dict:
        return self.source(source_id)["inventory"] or {"status": "empty", "sites": [], "discovery_revision": None}

    def catalog(self, source_id: str, site_ids: list[str], discovery_revision: str) -> dict:
        inventory = self.sites(source_id)
        if inventory.get("status") != "ready" or inventory["discovery_revision"] != discovery_revision:
            raise DomainError("COOKIE_SELECTION_STALE", "Обновите список открытых сайтов")
        if not site_ids or len(site_ids) != len(set(site_ids)) or len(site_ids) > 50:
            raise DomainError("SELECTION_INVALID", "Выберите от 1 до 50 сайтов")
        selected = [site for site in inventory["sites"] if site["site_id"] in site_ids]
        if len(selected) != len(site_ids):
            raise DomainError("SELECTION_INVALID", "В выборе есть неизвестные сайты")
        self._require_background_capacity()
        self._make_catalog_room()
        catalog_id = secrets.token_urlsafe(16)
        self.catalogs[catalog_id] = {"catalog_id": catalog_id, "source_id": source_id, "revision": secrets.token_urlsafe(12),
                                     "discovery_revision": discovery_revision, "status": "pending", "sites": selected, "cookies": [], "created_at": time.monotonic()}
        self.spawn(self._catalog(catalog_id))
        return {"catalog_id": catalog_id}

    @staticmethod
    def applies(record: dict, site: dict) -> bool:
        host = urlsplit(site["origin"]).hostname or ""
        domain = record["domain"].lstrip(".").lower()
        return str(record.get("store_id")) == site["store_id"] and (host == domain or (not record.get("host_only", True) and host.endswith("." + domain)))

    async def _catalog(self, catalog_id: str) -> None:
        catalog = self.catalogs[catalog_id]
        try:
            data = await self.ask(catalog["source_id"], "cookie_catalog", {"sites": catalog["sites"]})
            records = data.get("cookies")
            if not isinstance(records, list) or len(records) > 500:
                raise DomainError("EXTENSION_INVALID", "Каталог cookies слишком велик")
            if len(json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_CATALOG_BYTES:
                raise DomainError("EXTENSION_INVALID", "Cookie catalog metadata is too large")
            seen = set()
            for original in records:
                if "value" in original:
                    raise DomainError("EXTENSION_INVALID", "Каталог не должен содержать значения cookies")
                keys = {"source_key", "name", "domain", "path", "store_id", "host_only", "secure", "http_only", "same_site", "expires_at", "partition_key", "unsupported_reason", "site_ids"}
                if set(original) - keys or not keys.issubset(original):
                    raise DomainError("EXTENSION_INVALID", "Некорректная структура cookie")
                record = dict(original)
                if not all(isinstance(record[x], str) for x in ("name", "domain", "path", "store_id", "source_key")) or not record["path"].startswith("/"):
                    raise DomainError("EXTENSION_INVALID", "Некорректные атрибуты cookie")
                if any(len(record[x]) > 4096 for x in ("name", "domain", "path", "source_key")):
                    raise DomainError("EXTENSION_INVALID", "Слишком длинная запись cookie")
                related = [site["site_id"] for site in catalog["sites"] if self.applies(record, site)]
                if not related or record["source_key"] in seen:
                    raise DomainError("EXTENSION_INVALID", "Cookie вне выбранных сайтов или повторяется")
                seen.add(record["source_key"])
                record["site_ids"] = related
                record["cookie_id"] = secrets.token_urlsafe(16)
                catalog["cookies"].append(record)
            catalog["status"] = "ready"
            catalog["created_at"] = time.monotonic()
        except Exception as exc:
            catalog.update(status="failed", cookies=[], error=self.error(exc))

    def get_catalog(self, catalog_id: str) -> dict:
        self._expire_catalogs()
        catalog = self.catalogs.get(catalog_id)
        if not catalog:
            raise DomainError("CATALOG_NOT_FOUND", "Каталог не найден")
        return {key: ([{k: v for k, v in record.items() if k != "source_key"} for record in value] if key == "cookies" else value)
                for key, value in catalog.items() if key not in {"sites", "created_at"}}

    def import_job(self, profile_id: str, selection: dict) -> dict:
        if profile_id != "default":
            raise DomainError("PROFILE_NOT_FOUND", "В этом выпуске доступен профиль default")
        keys = {"source_id", "discovery_revision", "catalog_id", "catalog_revision", "selected_site_ids", "selected_cookie_ids", "conflict_mode", "idempotency_key"}
        if set(selection) != keys or selection.get("conflict_mode") != "replace_selected_matches":
            raise DomainError("SELECTION_INVALID", "Неверный контракт выбора cookies")
        fingerprint = digest({"profile": profile_id, "selection": selection})
        key = str(selection["idempotency_key"])
        if not 8 <= len(key) <= 128:
            raise DomainError("SELECTION_INVALID", "Некорректный idempotency key")
        if key in self.import_keys:
            old_hash, job_id = self.import_keys[key]
            if old_hash != fingerprint:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого импорта")
            return self.imports[job_id]
        if len(self.import_keys) >= MAX_IMPORT_RECEIPTS:
            raise DomainError("IMPORT_RECEIPT_LIMIT", "Import receipt capacity reached; restart the service before submitting more imports")
        self._require_background_capacity()
        public = self.get_catalog(selection["catalog_id"])
        catalog = self.catalogs[selection["catalog_id"]]
        if public["status"] != "ready" or selection["source_id"] != catalog["source_id"] or selection["catalog_revision"] != catalog["revision"] or selection["discovery_revision"] != catalog["discovery_revision"]:
            raise DomainError("COOKIE_SELECTION_STALE", "Обновите каталог и выбор cookies")
        ids = selection["selected_cookie_ids"]
        sites = selection["selected_site_ids"]
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not isinstance(sites, list) or not sites:
            raise DomainError("SELECTION_INVALID", "Выберите сайты и cookies")
        allowed_sites = {x["site_id"] for x in catalog["sites"]}
        if not set(sites).issubset(allowed_sites):
            raise DomainError("SELECTION_INVALID", "Сайт не принадлежит каталогу")
        records = [record for record in catalog["cookies"] if record["cookie_id"] in ids]
        if len(records) != len(ids) or any(r["unsupported_reason"] or not set(r["site_ids"]) & set(sites) for r in records):
            raise DomainError("SELECTION_INVALID", "Некоторые cookies недоступны для переноса")
        job_id = secrets.token_urlsafe(16)
        job = {"job_id": job_id, "profile_id": profile_id, "status": "validating", "counts": {"selected": len(records)}, "error": None}
        self.imports[job_id] = job
        self.import_keys[key] = fingerprint, job_id
        self.spawn(self._import(job, catalog, records, sites))
        return job

    async def _import(self, job: dict, catalog: dict, records: list[dict], site_ids: list[str]) -> None:
        values = None
        try:
            wait_started = time.monotonic()
            while True:
                remaining = self.ttl - (time.monotonic() - catalog["created_at"])
                if remaining <= 0:
                    raise DomainError("COOKIE_SELECTION_STALE", "Срок списка cookies истёк. Загрузите список заново и повторите выбор.")
                wait_remaining = IMPORT_WAIT_LIMIT_S - (time.monotonic() - wait_started)
                if wait_remaining <= 0:
                    raise DomainError("IMPORT_CONTROL_BUSY", "Браузер остался занят. Верните ручное управление или дождитесь паузы задачи, затем повторите перенос.")
                try:
                    ready = await asyncio.wait_for(self.importer.can_import(), timeout=min(5, remaining, wait_remaining))
                except TimeoutError:
                    ready = False
                if ready:
                    break
                job["status"] = "waiting_pause"
                job["detail"] = "Браузер занят. Если включено ручное управление, нажмите «Вернуть управление». Если выполняется задача, дождитесь её паузы или завершения. Список cookies действует ограниченное время."
                await asyncio.sleep(min(.25, max(0, self.ttl - (time.monotonic() - catalog["created_at"])), max(0, IMPORT_WAIT_LIMIT_S - (time.monotonic() - wait_started))))
            if time.monotonic()-catalog["created_at"] >= self.ttl:
                raise DomainError("COOKIE_SELECTION_STALE", "Срок списка cookies истёк. Загрузите список заново и повторите выбор.")
            job["status"] = "reading_selected"
            job.pop("detail", None)
            selected = [{key: value for key, value in r.items() if key != "cookie_id"} for r in records]
            sites = [x for x in catalog["sites"] if x["site_id"] in site_ids]
            data = await self.ask(catalog["source_id"], "cookie_export", {"sites": sites, "selections": selected})
            values = data.get("cookies")
            if not isinstance(values, list) or len(values) != len(selected):
                raise DomainError("SESSION_IMPORT_REJECTED", "Состав экспорта не совпадает с выбором")
            expected = {x["source_key"]: x for x in selected}
            seen = set()
            for record in values:
                source_key = record.get("source_key")
                original = expected.get(source_key)
                if original is None or source_key in seen or set(record) != set(original) | {"value"} or any(record[k] != original[k] for k in original):
                    raise DomainError("SESSION_IMPORT_REJECTED", "Экспорт содержит изменённую или лишнюю запись")
                if not isinstance(record["value"], str) or len(record["value"]) > 65536:
                    raise DomainError("SESSION_IMPORT_REJECTED", "Значение cookie слишком велико")
                seen.add(source_key)
            job["status"] = "applying"
            receipt = await self.importer.import_cookies(job["job_id"], values)
            if receipt.get("outcome") == "unknown":
                raise DomainError("ACTION_OUTCOME_UNKNOWN", "Исход предыдущего импорта неизвестен; проверьте состояние Camoufox")
            copied = receipt.get("applied", receipt.get("copied", 0))
            failed = receipt.get("failed", 0)
            job["counts"].update(copied=copied, failed=failed)
            job["status"] = "partial" if failed and copied else "failed" if failed else receipt.get("status", "done")
            # Only normalized outcome metadata crosses back into the web UI.
            outcomes = []
            retained_bytes = 0
            rows = receipt.get("cookies", [])
            for source_row in rows[:500] if isinstance(rows, list) else []:
                row = {}
                for key in ("name", "domain", "path", "outcome", "error"):
                    if key in source_row:
                        row[key] = str(source_row[key]).encode("utf-8")[:160].decode("utf-8", errors="ignore")
                row_bytes = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if retained_bytes + row_bytes > MAX_IMPORT_OUTCOME_BYTES:
                    break
                outcomes.append(row)
                retained_bytes += row_bytes
            job["outcomes"] = outcomes
            job["outcomes_truncated"] = len(outcomes) < (len(rows) if isinstance(rows, list) else 0)
            job["detail"] = "Cookies скопированы. Авторизацию проверьте в Camoufox."
        except Exception as exc:
            error = self.error(exc)
            error["code"] = str(error.get("code", "IMPORT_FAILED"))[:80]
            error["message"] = str(error.get("message", "Import failed"))[:512]
            job.update(status="selection_stale" if error["code"] == "COOKIE_SELECTION_STALE" else "failed", error=error)
        finally:
            if isinstance(values, list):
                for record in values:
                    record.pop("value", None)

    def get_import(self, profile_id: str, job_id: str) -> dict:
        job = self.imports.get(job_id)
        if not job or job["profile_id"] != profile_id:
            raise DomainError("IMPORT_NOT_FOUND", "Импорт не найден")
        return job

    def list_imports(self, profile_id: str) -> list[dict]:
        if profile_id != "default":
            raise DomainError("PROFILE_NOT_FOUND", "В этом выпуске доступен профиль default")
        reports = []
        for job in reversed(self.imports.values()):
            if job.get("profile_id") != profile_id:
                continue
            report = {key: job[key] for key in ("job_id", "status", "detail", "outcomes_truncated") if key in job}
            if isinstance(job.get("counts"), dict):
                report["counts"] = {key: job["counts"][key] for key in ("selected", "copied", "failed", "skipped") if key in job["counts"]}
            if job.get("error") is None:
                report["error"] = None
            elif isinstance(job["error"], dict):
                report["error"] = {key: job["error"][key] for key in ("code", "message") if key in job["error"]}
            if isinstance(job.get("outcomes"), list):
                report["outcomes"] = [{key: row[key] for key in ("name", "domain", "path", "outcome", "error") if key in row}
                                      for row in job["outcomes"] if isinstance(row, dict)]
            reports.append(report)
            if len(reports) == 20:
                break
        return reports
