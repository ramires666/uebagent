"""Bounded public guest-page compatibility check for a disposable browser."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from scripts.readiness_soak import CONTROL_URL, MAX_SCREENSHOT_BYTES, WORKER_URL, validate_environment

SCHEMA = "readiness-public-web/v1"
MAX_DURATION_SECONDS = 180
SETTLE_SECONDS = 2
SCREENSHOT_DIR = Path("/app/runtime/control-data/public-web-screenshots")
SITES = (
    {"name": "demoqa", "url": "https://demoqa.com/text-box", "origin": "https://demoqa.com",
     "positive": ({"kind": "element_visible", "selector": "#userName", "visible": True},),
     "challenge": ({"kind": "text_present", "text": "Verify you are human"},
                   {"kind": "element_visible", "selector": "#captcha", "visible": True})},
    {"name": "amazon", "url": "https://www.amazon.com/", "origin": "https://www.amazon.com",
     "positive": ({"kind": "element_visible", "selector": "#nav-logo", "visible": True},),
     "challenge": ({"kind": "text_present", "text": "robot check"},
                   {"kind": "element_visible", "selector": "#captchacharacters", "visible": True})},
    {"name": "ebay", "url": "https://www.ebay.com/", "origin": "https://www.ebay.com",
     "positive": ({"kind": "element_visible", "selector": "#gh-logo", "visible": True},
                  {"kind": "element_visible", "selector": "#gh-ac", "visible": True}),
     "challenge": ({"kind": "text_present", "text": "Security Measure"},
                   {"kind": "element_visible", "selector": "#captcha", "visible": True})},
    {"name": "sannysoft", "url": "https://bot.sannysoft.com/", "origin": "https://bot.sannysoft.com",
     "positive": ({"kind": "text_present", "text": "Intoli.com tests"},),
     "challenge": ({"kind": "text_present", "text": "Verify you are human"},)},
)
ERROR_CODES = {"NAVIGATION_BLOCKED", "NAVIGATION_TIMEOUT", "EGRESS_NOT_READY", "BROWSER_UNAVAILABLE",
               "CONTROL_NOT_HUMAN", "STALE_CONTROL_EPOCH", "ACTION_OUTCOME_UNKNOWN", "ACTION_IN_PROGRESS"}


class PublicWebError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_origin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        return f"https://{host}"
    except ValueError:
        return None


def classify_site(*, navigation_ok: bool, final_origin: str | None, expected_origin: str,
                  positive_passed: bool, challenge_detected: bool) -> str:
    if not navigation_ok:
        return "UNREACHABLE"
    if challenge_detected:
        return "CHALLENGE_DETECTED"
    if final_origin != expected_origin:
        return "BLOCKED" if final_origin is not None else "UNVERIFIED_DUE_TO_OBSTRUCTION"
    if positive_passed:
        return "PUBLIC_PAGE_AVAILABLE"
    return "UNVERIFIED_DUE_TO_OBSTRUCTION"


class PublicWebCheck:
    def __init__(self, client: httpx.Client, key: str, *, clock=time.monotonic, sleep=time.sleep,
                 progress=print, screenshot_dir: Path = SCREENSHOT_DIR, deadline_seconds=MAX_DURATION_SECONDS):
        self.client, self.key, self.clock, self.sleep = client, key, clock, sleep
        self.progress, self.screenshot_dir = progress, screenshot_dir
        self.deadline_seconds = deadline_seconds
        self.started = None
        self.headers = {"Authorization": f"Bearer {key}"}
        self.identity_start = None
        self.epoch = None

    def request(self, method, path, *, body=None, timeout=5.0):
        if self.started is not None and self.clock() - self.started >= self.deadline_seconds:
            raise PublicWebError("DEADLINE_EXCEEDED")
        try:
            response = self.client.request(method, WORKER_URL + path, headers=self.headers,
                                           json=body, timeout=min(timeout, max(.1, self.deadline_seconds - (self.clock() - self.started)) if self.started is not None else timeout))
        except httpx.TimeoutException:
            raise PublicWebError("REQUEST_TIMEOUT") from None
        except httpx.HTTPError:
            raise PublicWebError("REQUEST_FAILED") from None
        if response.status_code != 200:
            code = "WORKER_HTTP_ERROR"
            try:
                payload = response.json()
                detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
                candidate = detail.get("code") if isinstance(detail, dict) else None
                if candidate in ERROR_CODES:
                    code = candidate
            except ValueError:
                pass
            raise PublicWebError(code)
        return response

    def json(self, method, path, body=None, timeout=5.0):
        try:
            payload = self.request(method, path, body=body, timeout=timeout).json()
        except ValueError:
            raise PublicWebError("INVALID_WORKER_RESPONSE") from None
        if not isinstance(payload, dict):
            raise PublicWebError("INVALID_WORKER_RESPONSE")
        return payload

    @staticmethod
    def identity(state):
        names = ("worker_instance_id", "browser_instance_id", "page_id", "recovery_generation")
        if any(state.get(name) is None for name in names):
            raise PublicWebError("IDENTITY_MISSING")
        if (state.get("context_count") != 1 or state.get("page_count") != 1
                or state.get("control_owner") != "none" or state.get("action_in_progress") is not False
                or state.get("import_in_progress") is not False or state.get("recovery_needed") is not False
                or state.get("recovery_state") != "idle"):
            raise PublicWebError("IDLE_INVARIANT_FAILED")
        return tuple(state[name] for name in names)

    def verify(self, conditions):
        result = self.json("POST", "/verify", {"conditions": list(conditions)})
        checks = result.get("checks")
        if not isinstance(checks, list) or len(checks) != len(conditions):
            raise PublicWebError("VERIFY_RESPONSE_INVALID")
        return [item.get("passed") is True for item in checks if isinstance(item, dict)]

    def check_site(self, site):
        result = {"site": site["name"], "origin": site["origin"], "status": None,
                  "navigation_applied": False, "positive_proof": False,
                  "challenge_detected": False, "observed_expected_origin": False,
                  "screenshot": None, "error_code": None}
        gate_acquired = False
        try:
            gate = self.json("POST", "/gate", {"owner": "human"})
            epoch = gate.get("control_epoch")
            if type(epoch) is not int or gate.get("control_owner") != "human":
                raise PublicWebError("GATE_FAILED")
            gate_acquired = True
            receipt = self.json("POST", "/manual/execute", {
                "control_epoch": epoch, "allowed_origins": [site["origin"]],
                "allow_cross_origin_navigation": True,
                "action": {"kind": "navigate", "url": site["url"]},
            }, timeout=15.0)
            epoch = receipt.get("control_epoch")
            if receipt.get("outcome") != "applied" or type(epoch) is not int or receipt.get("control_owner") != "human":
                raise PublicWebError("NAVIGATION_NOT_APPLIED")
            result["navigation_applied"] = True
            self.epoch = epoch
            # Keep the manual public-navigation route exception active while the
            # site loads and the fixed screenshot/proof requests run.
            self.sleep(SETTLE_SECONDS)
            observed = self.json("POST", "/observe", {})
            final_origin = _safe_origin(observed.get("url"))
            encoded = observed.get("screenshot_png_b64")
            if not isinstance(encoded, str) or len(encoded) > ((MAX_SCREENSHOT_BYTES + 2) // 3) * 4:
                raise PublicWebError("SCREENSHOT_INVALID")
            try:
                image = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise PublicWebError("SCREENSHOT_INVALID") from None
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > MAX_SCREENSHOT_BYTES:
                raise PublicWebError("SCREENSHOT_INVALID")
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{site['name']}.png"
            (self.screenshot_dir / filename).write_bytes(image)
            result["screenshot"] = {"file": filename, "bytes": len(image)}
            positive = self.verify(site["positive"])
            challenge = self.verify(site["challenge"])
            result["positive_proof"] = bool(positive) and all(positive)
            result["challenge_detected"] = any(challenge)
            result["status"] = classify_site(navigation_ok=True, final_origin=final_origin,
                                             expected_origin=site["origin"],
                                             positive_passed=result["positive_proof"],
                                             challenge_detected=result["challenge_detected"])
            result["observed_expected_origin"] = final_origin == site["origin"]
        except PublicWebError as exc:
            result["error_code"] = exc.code
            result["status"] = ("UNREACHABLE" if exc.code in {"REQUEST_TIMEOUT", "REQUEST_FAILED", "NAVIGATION_TIMEOUT"}
                                else "BLOCKED" if exc.code in {"NAVIGATION_BLOCKED", "EGRESS_NOT_READY"}
                                else "UNVERIFIED_DUE_TO_OBSTRUCTION")
        finally:
            if gate_acquired:
                try:
                    self.json("POST", "/gate", {"owner": "none"})
                except PublicWebError:
                    result["status"] = "UNVERIFIED_DUE_TO_OBSTRUCTION"
                    result["error_code"] = "GATE_RELEASE_FAILED"
        return result

    def run(self):
        self.started = self.clock()
        started_at = utc_now()
        results = []
        error = None
        try:
            state = self.json("GET", "/status")
            self.identity_start = self.identity(state)
            for site in SITES:
                self.progress(json.dumps({"event": "site_start", "site": site["name"], "at_utc": utc_now()}))
                results.append(self.check_site(site))
                self.progress(json.dumps({"event": "site_end", "site": site["name"],
                                          "status": results[-1]["status"], "at_utc": utc_now()}))
        except PublicWebError as exc:
            error = exc.code
        same = False
        final_state = None
        try:
            final_state = self.json("GET", "/status")
            final = self.identity(final_state)
            same = final == self.identity_start
        except PublicWebError:
            pass
        single_context_page = bool(final_state is not None and final_state.get("context_count") == 1
                                   and final_state.get("page_count") == 1)
        owner_released = bool(final_state is not None and final_state.get("control_owner") == "none")
        report = {"schema": SCHEMA, "status": "DIAGNOSTIC" if len(results) == len(SITES) and same and error is None else "FAIL",
                  "started_at_utc": started_at, "ended_at_utc": utc_now(),
                  "limits": {"site_count": len(SITES), "max_duration_seconds": MAX_DURATION_SECONDS,
                             "settle_seconds_per_site": SETTLE_SECONDS, "accounts_or_auth": False,
                             "captcha_bypass": False, "llm_calls": False},
                  "invariants": {"same_worker_browser_page_generation": same,
                                 "single_context_page": single_context_page,
                                 "control_owner_after_run": "none" if owner_released else "unknown"},
                  "sites": results, "error_code": error}
        return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=WORKER_URL)
    parser.add_argument("--control-url", default=CONTROL_URL)
    parser.add_argument("--key-file", type=Path, default=os.environ.get("WORKER_API_KEY_FILE"))
    parser.add_argument("--output", type=Path, default=Path("/app/runtime/control-data/readiness-public-web.json"))
    parser.add_argument("--i-confirm-disposable", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.key_file is None:
        print(json.dumps({"status": "BLOCKED", "error_code": "INVALID_BOUNDED_CONFIGURATION"}))
        return 2
    try:
        key = validate_environment(confirmed=args.i_confirm_disposable, worker_url=args.url,
                                   control_url=args.control_url, key_file=args.key_file)
    except (ValueError, OSError):
        print(json.dumps({"status": "BLOCKED", "error_code": "DISPOSABLE_GUARD_REJECTED"}))
        return 2
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        report = PublicWebCheck(client, key).run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output)}, separators=(",", ":")))
    return 0 if report["status"] == "DIAGNOSTIC" else 1


if __name__ == "__main__":
    raise SystemExit(main())
