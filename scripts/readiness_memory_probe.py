"""Bounded, diagnostic-only browser memory phase probe for disposable stacks."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.readiness_soak import (
    CONTROL_URL, FIXTURE, MAX_SCREENSHOT_BYTES, WORKER_URL, SoakError,
    validate_environment,
)

PHASES = ("navigate_only", "screenshot_only", "observe_hints")
PHASE_SECONDS = 60
IDLE_SECONDS = 5
OPERATION_INTERVAL_SECONDS = 1 / 7


class Probe:
    def __init__(self, client, key, phase_seconds=PHASE_SECONDS, idle_seconds=IDLE_SECONDS,
                 clock=time.monotonic, sleep=time.sleep, progress=print):
        self.client, self.key = client, key
        self.phase_seconds, self.idle_seconds = phase_seconds, idle_seconds
        self.clock, self.sleep = clock, sleep
        self.progress = progress
        self.headers = {"Authorization": f"Bearer {key}"}

    def request(self, method, path, *, json_body=None):
        try:
            response = self.client.request(method, WORKER_URL + path, headers=self.headers,
                                           json=json_body, timeout=10.0)
        except httpx.HTTPError:
            raise SoakError("request_failed") from None
        if response.status_code != 200:
            raise SoakError("http_status_" + str(response.status_code))
        return response

    def json(self, method, path, body=None):
        try:
            return self.request(method, path, json_body=body).json()
        except ValueError:
            raise SoakError("invalid_json") from None

    def manual_navigate(self, epoch):
        receipt = self.json("POST", "/manual/execute", {
            "control_epoch": epoch,
            "allowed_origins": ["https://readiness-fixture.test"],
            "action": {"kind": "navigate", "url": FIXTURE},
        })
        nxt = receipt.get("control_epoch")
        if receipt.get("outcome") != "applied" or type(nxt) is not int or nxt <= epoch:
            raise SoakError("navigate_receipt_invalid")
        return nxt

    def gate(self, owner):
        response = self.json("POST", "/gate", {"owner": owner})
        value = response.get("control_epoch")
        if type(value) is not int or response.get("control_owner") != owner:
            raise SoakError("gate_receipt_invalid")
        return value

    def setup(self):
        self.json("POST", "/diagnostics/readiness-fixture", {"scenario": "tree"})
        epoch = self.gate("human")
        try:
            self.manual_navigate(epoch)
        finally:
            self.gate("none")

    def idle_status(self):
        state = self.json("GET", "/status")
        if (state.get("context_count") != 1 or state.get("page_count") != 1
                or state.get("control_owner") != "none"
                or state.get("action_in_progress") is not False
                or state.get("import_in_progress") is not False
                or state.get("resource_violation") is not False
                or state.get("recovery_needed") is not False
                or state.get("recovery_state") != "idle"):
            raise SoakError("idle_invariant_failed")
        return state

    def phase(self, name):
        started_at = datetime.now(timezone.utc).isoformat()
        started = self.clock()
        deadline = started + self.phase_seconds
        count = byte_count = 0
        errors = []
        self.progress(json.dumps({"event": "phase_start", "phase": name,
                                  "started_at_utc": started_at}, separators=(",", ":")))
        while self.clock() < deadline:
            operation_started = self.clock()
            try:
                if name == "navigate_only":
                    epoch = self.gate("human")
                    try:
                        self.manual_navigate(epoch)
                    finally:
                        self.gate("none")
                elif name == "screenshot_only":
                    response = self.request("GET", "/screenshot")
                    content = response.content
                    if len(content) > MAX_SCREENSHOT_BYTES or not content.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise SoakError("screenshot_invalid_or_too_large")
                    byte_count += len(content)
                else:
                    observation = self.json("POST", "/observe", {})
                    encoded = observation.get("screenshot_png_b64")
                    if not isinstance(encoded, str) or len(encoded) > ((MAX_SCREENSHOT_BYTES + 2) // 3) * 4:
                        raise SoakError("observation_screenshot_invalid")
                    try:
                        image = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        raise SoakError("observation_screenshot_invalid") from None
                    hints = observation.get("interaction_hints")
                    if (not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > MAX_SCREENSHOT_BYTES
                            or not isinstance(hints, dict) or hints.get("version") != 1
                            or hints.get("coordinate_space") != "normalized_1000"
                            or not isinstance(hints.get("elements"), list) or len(hints["elements"]) > 32
                            or len(json.dumps(hints, separators=(",", ":"), ensure_ascii=False).encode()) > 4096):
                        raise SoakError("observation_invalid")
                    byte_count += len(image)
                count += 1
            except SoakError as exc:
                errors.append(str(exc))
                break
            remaining = deadline - self.clock()
            delay = min(OPERATION_INTERVAL_SECONDS - (self.clock() - operation_started), remaining)
            if delay > 0:
                self.sleep(delay)
        elapsed = round(self.clock() - started, 2)
        result = {"name": name, "started_at_utc": started_at,
                "ended_at_utc": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": elapsed, "operation_count": count,
                "image_bytes": byte_count, "error_codes": errors[:5]}
        self.progress(json.dumps({"event": "phase_end", "phase": name,
                                  "ended_at_utc": result["ended_at_utc"],
                                  "operation_count": count, "error_codes": errors[:5]},
                                 separators=(",", ":")))
        return result

    def run(self):
        started_at = datetime.now(timezone.utc).isoformat()
        started = self.clock()
        phases, errors = [], []
        try:
            self.setup()
            for index, name in enumerate(PHASES):
                phase = self.phase(name)
                phases.append(phase)
                if phase["error_codes"]:
                    errors.extend(phase["error_codes"])
                    break
                self.idle_status()
                if index < len(PHASES) - 1:
                    self.sleep(self.idle_seconds)
            final_status = self.idle_status()
        except SoakError as exc:
            errors.append(str(exc))
            final_status = None
        completed = len(phases) == len(PHASES) and not errors
        return {"schema": "readiness-memory-probe/v1",
                "status": "DIAGNOSTIC" if completed else "FAIL",
                "started_at_utc": started_at, "ended_at_utc": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(self.clock() - started, 2),
                "phases": phases, "error_codes": errors[:10],
                "invariants": {"context_count": 1, "page_count": 1,
                               "control_owner_after_phase": "none" if final_status else "unknown"}}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=WORKER_URL)
    parser.add_argument("--control-url", default=CONTROL_URL)
    parser.add_argument("--key-file", type=Path, default=os.environ.get("WORKER_API_KEY_FILE"))
    parser.add_argument("--duration-seconds", type=int, default=210)
    parser.add_argument("--output", type=Path, default=Path("/app/runtime/control-data/readiness-memory-probe.json"))
    parser.add_argument("--i-confirm-disposable", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.duration_seconds != 210 or args.key_file is None:
        print(json.dumps({"status": "BLOCKED", "error_code": "INVALID_BOUNDED_CONFIGURATION"}))
        return 2
    try:
        key = validate_environment(confirmed=args.i_confirm_disposable, worker_url=args.url,
                                   control_url=args.control_url, key_file=args.key_file)
    except (ValueError, OSError):
        print(json.dumps({"status": "BLOCKED", "error_code": "DISPOSABLE_GUARD_REJECTED"}))
        return 2
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        report = Probe(client, key).run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output)}, separators=(",", ":")))
    return 0 if report["status"] == "DIAGNOSTIC" else 1


if __name__ == "__main__":
    raise SystemExit(main())
