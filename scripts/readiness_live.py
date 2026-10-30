"""Bounded read-only readiness checks for a disposable Browser MCP stack.

Run from inside the disposable control container or against its loopback
published port 18080. This script never sends browser actions or starts tasks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urlsplit

import httpx


DEFAULT_URL = "http://127.0.0.1:18080"
DEFAULT_ORIGIN = "http://localhost:18080"
MAX_CONCURRENCY = 4
MAX_ITERATIONS = 10
MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_disposable_target(url: str, *, allow_internal: bool = False) -> str:
    parsed = urlsplit(url)
    ports = {18080}
    if allow_internal:
        if os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1":
            raise ValueError("internal target requires APP_ENV=test and READINESS_DISPOSABLE=1")
        ports.add(8080)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.port not in ports:
        raise ValueError("target must be HTTP loopback:18080, or guarded disposable loopback:8080")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("target URL must not contain credentials, path, query, or fragment")
    return url.rstrip("/")


def png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != PNG_SIGNATURE:
        raise ValueError("response is not a PNG image")
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("PNG has invalid dimensions")
    return width, height


def latency_summary(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    def percentile(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, max(0, int((len(ordered) * p + 0.999999)) - 1))] * 1000, 2)
    return {
        "count": len(samples),
        "min_ms": round(ordered[0] * 1000, 2),
        "mean_ms": round(mean(samples) * 1000, 2),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": round(ordered[-1] * 1000, 2),
    }


def _check(passed: bool, *, expected: str, actual: str | int | bool) -> dict:
    return {"status": "PASS" if passed else "FAIL", "expected": expected, "actual": actual}


def _mcp_json(response: httpx.Response) -> dict:
    if "json" in response.headers.get("content-type", ""):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise ValueError("MCP response did not contain a JSON payload")


async def run_live(
    base_url: str,
    *,
    allowed_origin: str = DEFAULT_ORIGIN,
    mcp_token_file: Path | None = None,
    concurrency: int = 2,
    iterations: int = 3,
    allow_internal: bool = False,
) -> dict:
    base_url = validate_disposable_target(base_url, allow_internal=allow_internal)
    if not 1 <= concurrency <= MAX_CONCURRENCY or not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError("concurrency or iterations exceed bounded limits")
    if urlsplit(allowed_origin).scheme != "http" or urlsplit(allowed_origin).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("allowed origin must be a local HTTP origin")
    origin_parts = urlsplit(allowed_origin)
    if origin_parts.path not in {"", "/"} or origin_parts.query or origin_parts.fragment or origin_parts.port != 18080:
        raise ValueError("allowed origin must be a loopback origin on port 18080")

    started = datetime.now(timezone.utc).isoformat()
    report = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "started_at_utc": started,
        "target": {"scheme": "http", "host_class": "loopback", "port": urlsplit(base_url).port},
        "checks": {},
        "browser": {},
        "load": {"concurrency": concurrency, "iterations": iterations},
        "limitations": [
            "No browser actions or task creation are performed.",
            "Delay, dynamic-ID, overlay, iframe, and dialog fixtures are not injected because the current public control API has no safe read-only fixture route.",
        ],
    }
    headers = {"Origin": allowed_origin}

    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(8.0), trust_env=False) as client:
        # Establish local UI session before readiness/state API calls. The value is
        # held only in the in-memory cookie jar and is never written to evidence.
        try:
            await client.get("/api/v1/operator/session", headers=headers)
        except httpx.HTTPError:
            pass
        # Health endpoints are read-only and do not require an operator session.
        for name, path in (("health_live", "/health/live"), ("health_ready", "/health/ready")):
            start = time.perf_counter()
            try:
                response = await client.get(path)
                elapsed = time.perf_counter() - start
                okay = response.status_code == 200
                if name == "health_live":
                    okay = okay and response.json().get("status") == "ok"
                else:
                    okay = okay and response.json().get("ready") is True
                report["checks"][name] = {**_check(okay, expected="HTTP 200 and healthy", actual=response.status_code),
                                          "latency_ms": round(elapsed * 1000, 2)}
            except (httpx.HTTPError, ValueError):
                report["checks"][name] = _check(False, expected="reachable healthy endpoint", actual="request_error")
        report["checks"]["host_accepted"] = _check(
            report["checks"]["health_live"]["status"] == "PASS",
            expected="HTTP 200 from configured loopback Host",
            actual=report["checks"]["health_live"].get("actual"),
        )

        # An explicit bad Host and bad Origin must be rejected before any action.
        try:
            response = await client.get("/health/live", headers={"Host": "readiness-invalid.example"})
            report["checks"]["host_rejected"] = _check(response.status_code == 403, expected=403, actual=response.status_code)
        except httpx.HTTPError:
            report["checks"]["host_rejected"] = _check(False, expected=403, actual="request_error")
        try:
            response = await client.get("/api/v1/desktop/state", headers={"Origin": "https://invalid.example"})
            report["checks"]["origin_rejected"] = _check(response.status_code == 403, expected=403, actual=response.status_code)
        except httpx.HTTPError:
            report["checks"]["origin_rejected"] = _check(False, expected=403, actual="request_error")

        # Local GET creates a throwaway UI session. Only status and token presence
        # are recorded; neither the cookie nor CSRF value enters the report.
        try:
            response = await client.get("/api/v1/operator/session", headers=headers)
            session = response.json()
            csrf = session.get("csrf_token")
            session_ok = response.status_code == 200 and session.get("authenticated") is True and bool(csrf)
            report["checks"]["ui_session_positive"] = _check(session_ok, expected="authenticated session and CSRF token", actual=response.status_code)
        except (httpx.HTTPError, ValueError):
            csrf = None
            report["checks"]["ui_session_positive"] = _check(False, expected="authenticated session", actual="request_error")

        if csrf:
            try:
                response = await client.delete("/api/v1/operator/session", headers=headers)
                report["checks"]["csrf_missing_rejected"] = _check(response.status_code == 403, expected=403, actual=response.status_code)
            except httpx.HTTPError:
                report["checks"]["csrf_missing_rejected"] = _check(False, expected=403, actual="request_error")
            try:
                response = await client.delete("/api/v1/operator/session", headers={**headers, "X-CSRF-Token": "invalid"})
                report["checks"]["csrf_rejected"] = _check(response.status_code == 403, expected=403, actual=response.status_code)
            except httpx.HTTPError:
                report["checks"]["csrf_rejected"] = _check(False, expected=403, actual="request_error")
            try:
                response = await client.delete("/api/v1/operator/session", headers={**headers, "X-CSRF-Token": csrf})
                report["checks"]["csrf_positive"] = _check(response.status_code == 200, expected=200, actual=response.status_code)
            except httpx.HTTPError:
                report["checks"]["csrf_positive"] = _check(False, expected=200, actual="request_error")
        else:
            report["checks"]["csrf_missing_rejected"] = _check(False, expected=403, actual="session_unavailable")
            report["checks"]["csrf_rejected"] = _check(False, expected=403, actual="session_unavailable")
            report["checks"]["csrf_positive"] = _check(False, expected=200, actual="session_unavailable")

        # Establish a fresh local UI session and prove it is not an MCP bearer.
        try:
            await client.get("/api/v1/operator/session", headers=headers)
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            ui_cookie_response = await client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                                                     "Content-Type": "application/json"}, json=payload)
            report["checks"]["ui_cookie_not_mcp_auth"] = _check(ui_cookie_response.status_code == 401, expected=401, actual=ui_cookie_response.status_code)
            invalid_bearer = await client.post("/mcp", headers={"Authorization": "Bearer invalid",
                                                                 "Origin": allowed_origin,
                                                                 "Accept": "application/json, text/event-stream",
                                                                 "Content-Type": "application/json"}, json=payload)
            report["checks"]["mcp_invalid_bearer_rejected"] = _check(invalid_bearer.status_code == 401, expected=401, actual=invalid_bearer.status_code)
            async with httpx.AsyncClient(base_url=base_url, timeout=8.0, trust_env=False) as anonymous:
                no_auth = await anonymous.post("/mcp", headers={"Origin": allowed_origin,
                                                                 "Accept": "application/json, text/event-stream",
                                                                 "Content-Type": "application/json"}, json=payload)
            report["checks"]["mcp_unauth_rejected"] = _check(no_auth.status_code == 401, expected=401, actual=no_auth.status_code)
        except httpx.HTTPError:
            report["checks"]["ui_cookie_not_mcp_auth"] = _check(False, expected=401, actual="request_error")
            report["checks"]["mcp_invalid_bearer_rejected"] = _check(False, expected=401, actual="request_error")
            report["checks"]["mcp_unauth_rejected"] = _check(False, expected=401, actual="request_error")

        token_file = mcp_token_file
        if token_file is None:
            data_dir = os.environ.get("BROWSER_MCP_DATA_DIR")
            candidate = Path(data_dir) / "mcp-token.txt" if data_dir else Path("/app/runtime/control-data/mcp-token.txt")
            token_file = candidate if candidate.is_file() else None
        if token_file is not None and token_file.is_file():
            try:
                token = token_file.read_text(encoding="utf-8").strip()
                if not token:
                    raise ValueError("empty token")
                response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                                                               "Origin": allowed_origin,
                                                               "Accept": "application/json, text/event-stream",
                                                               "Content-Type": "application/json"}, json=payload)
                result = _mcp_json(response) if response.status_code == 200 else {}
                tools = result.get("result", {}).get("tools", []) if isinstance(result, dict) else []
                report["checks"]["mcp_bearer_positive"] = _check(response.status_code == 200 and bool(tools),
                                                                   expected="HTTP 200 with nonempty tools/list", actual=response.status_code)
            except (OSError, ValueError, httpx.HTTPError):
                report["checks"]["mcp_bearer_positive"] = _check(False, expected="valid bearer and tools/list", actual="request_error")
        else:
            report["checks"]["mcp_bearer_positive"] = {"status": "BLOCKED", "expected": "explicit disposable MCP token file",
                                                       "actual": "token_file_unavailable"}

        # Browser state and screenshot are read-only. Do not save or emit image bytes.
        try:
            response = await client.get("/api/v1/desktop/state", headers=headers)
            state = response.json() if response.status_code == 200 else {}
            one_page = (response.status_code == 200 and state.get("browser_started") is True
                        and state.get("context_count") == 1 and state.get("page_count") == 1)
            report["browser"]["state"] = _check(one_page, expected="started with exactly one context and page",
                                                  actual={"http_status": response.status_code,
                                                          "browser_started": state.get("browser_started") is True,
                                                          "context_count": state.get("context_count"),
                                                          "page_count": state.get("page_count")})
            screenshot_status = 0
            image = bytearray()
            async with client.stream("GET", "/api/v1/desktop/screenshot", headers=headers) as screenshot:
                screenshot_status = screenshot.status_code
                if screenshot_status == 200:
                    if int(screenshot.headers.get("content-length", "0") or "0") > MAX_SCREENSHOT_BYTES:
                        raise ValueError("screenshot exceeds bounded response size")
                    async for chunk in screenshot.aiter_bytes():
                        image.extend(chunk)
                        if len(image) > MAX_SCREENSHOT_BYTES:
                            raise ValueError("screenshot exceeds bounded response size")
            width, height = png_dimensions(bytes(image)) if screenshot_status == 200 else (0, 0)
            report["browser"]["observation"] = _check(screenshot_status == 200 and width > 0 and height > 0,
                                                        expected="read-only PNG observation", actual={"http_status": screenshot.status_code,
                                                                                                      "width": width, "height": height,
                                                                                                      "bytes": len(image)})
        except (httpx.HTTPError, ValueError):
            report["browser"]["state"] = _check(False, expected="one-page browser state", actual="request_error")
            report["browser"]["observation"] = _check(False, expected="read-only PNG observation", actual="request_error")

        # Small, bounded read-only API load; no task creation or browser actions.
        semaphore = asyncio.Semaphore(concurrency)
        samples: list[float] = []
        statuses: list[int] = []
        page_shape_ok = True
        async def read_once(path: str) -> None:
            nonlocal page_shape_ok
            async with semaphore:
                start = time.perf_counter()
                try:
                    result = await client.get(path, headers=headers)
                    samples.append(time.perf_counter() - start)
                    statuses.append(result.status_code)
                    if result.status_code == 200 and path.endswith("/desktop/state"):
                        body = result.json()
                        page_shape_ok = page_shape_ok and body.get("context_count") == 1 and body.get("page_count") == 1
                except (httpx.HTTPError, ValueError):
                    statuses.append(0)
        await asyncio.gather(*(read_once(path) for _ in range(iterations)
                               for path in ("/api/v1/desktop/state", "/health/live")))
        load_ok = len(statuses) == iterations * 2 and all(status == 200 for status in statuses) and page_shape_ok
        report["load"].update({
            "request_count": len(statuses),
            "status_counts": {str(code): statuses.count(code) for code in sorted(set(statuses))},
            "latency": latency_summary(samples),
            "status": "PASS" if load_ok else "FAIL",
            "expected": f"{iterations * 2} read-only HTTP 200 responses; one browser page/context",
        })

    statuses = [entry.get("status") for entry in report["checks"].values()]
    statuses += [entry.get("status") for entry in report["browser"].values()]
    statuses.append(report["load"]["status"])
    report["overall_status"] = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("READINESS_SERVICE_URL", DEFAULT_URL))
    parser.add_argument("--origin", default=os.environ.get("READINESS_ALLOWED_ORIGIN", DEFAULT_ORIGIN))
    token_file = os.environ.get("READINESS_MCP_TOKEN_FILE")
    parser.add_argument("--mcp-token-file", type=Path, default=Path(token_file) if token_file else None)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("readiness-live-report.json"))
    parser.add_argument("--allow-disposable-internal", action="store_true",
                        help="allow loopback:8080 only with APP_ENV=test and READINESS_DISPOSABLE=1")
    parser.add_argument("--i-confirm-disposable", action="store_true",
                        help="required guard: target must be disposable loopback:18080 or explicitly guarded test loopback:8080")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    try:
        target = validate_disposable_target(args.url, allow_internal=args.allow_disposable_internal)
    except ValueError:
        print(json.dumps({"overall_status": "BLOCKED", "error_code": "TARGET_REJECTED"}))
        return 2
    if not args.i_confirm_disposable:
        print(json.dumps({"overall_status": "BLOCKED", "error_code": "DISPOSABLE_CONFIRMATION_REQUIRED"}))
        return 2
    try:
        report = await run_live(target, allowed_origin=args.origin, mcp_token_file=args.mcp_token_file,
                                concurrency=args.concurrency, iterations=args.iterations,
                                allow_internal=args.allow_disposable_internal)
    except ValueError:
        print(json.dumps({"overall_status": "BLOCKED", "error_code": "CONFIGURATION_REJECTED"}))
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    statuses = [item.get("status") for item in report["checks"].values()]
    statuses.extend(item.get("status") for item in report["browser"].values())
    statuses.append(report["load"]["status"])
    summary = {status: statuses.count(status) for status in ("PASS", "FAIL", "BLOCKED")}
    print(json.dumps({"overall_status": report["overall_status"], "gate_counts": summary,
                      "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["overall_status"] == "PASS" else 1


def main() -> int:
    return asyncio.run(_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
