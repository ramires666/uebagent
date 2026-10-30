"""Bounded synthetic browser functional and resource soak for disposable stacks."""
from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

WORKER_URL = "http://browser:7070"
CONTROL_URL = "http://localhost:8080"
MAX_DURATION_SECONDS = 1200
API_TIMEOUT_SECONDS = 5.0
ACTION_TIMEOUT_SECONDS = 10.0
MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
FIXTURE = "https://readiness-fixture.test/tree"
WORKER_ERROR_CODES = {
    "ACTION_IN_PROGRESS", "ACTION_OUTCOME_UNKNOWN", "APPROVAL_REQUIRED", "BROWSER_UNAVAILABLE",
    "CONTROL_NOT_HUMAN", "EGRESS_NOT_READY", "INVALID_ACTION", "NAVIGATION_BLOCKED",
    "NAVIGATION_TIMEOUT", "POLICY_DENIED", "STALE_CONTROL_EPOCH",
}


class SoakError(RuntimeError):
    pass


def validate_environment(*, confirmed: bool, worker_url: str, control_url: str,
                         key_file: Path, environ: dict[str, str] | None = None,
                         docker_marker: bool | None = None) -> str:
    env = os.environ if environ is None else environ
    if not confirmed:
        raise ValueError("explicit_disposable_confirmation_required")
    if (env.get("APP_ENV", "").lower() != "test" or env.get("READINESS_DISPOSABLE") != "1"
            or env.get("READINESS_RUNTIME_INTEGRATION") != "1"):
        raise ValueError("disposable_runtime_integration_markers_required")
    if env.get("READINESS_ACTION_TRACE") != "1":
        raise ValueError("readiness_action_trace_required")
    if env.get("BROWSER_OBSERVATION_HINTS") != "1":
        raise ValueError("observation_hints_candidate_flag_required")
    if worker_url != WORKER_URL or env.get("BROWSER_WORKER_URL") != WORKER_URL:
        raise ValueError("exact_disposable_worker_url_required")
    if control_url != CONTROL_URL:
        raise ValueError("exact_disposable_control_url_required")
    in_container = Path("/.dockerenv").is_file() if docker_marker is None else docker_marker
    if not in_container:
        raise ValueError("container_only_execution_required")
    if not key_file.is_file() or str(key_file) != env.get("WORKER_API_KEY_FILE"):
        raise ValueError("worker_key_file_must_match_environment")
    parsed = urlsplit(control_url)
    if parsed.scheme != "http" or parsed.hostname != "localhost" or parsed.port != 8080:
        raise ValueError("invalid_control_target")
    return key_file.read_text(encoding="utf-8").strip()


def latency_summary(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    def pct(p: float) -> float:
        return round(ordered[min(len(ordered)-1, max(0, int(len(ordered)*p + .999999)-1))]*1000, 2)
    return {"count": len(samples), "p50_ms": pct(.5), "p95_ms": pct(.95),
            "max_ms": round(ordered[-1]*1000, 2)}


class Soak:
    def __init__(self, client: httpx.Client, control_client: httpx.Client, key: str,
                 duration: int, progress=print):
        self.client, self.control_client, self.key, self.duration, self.progress = client, control_client, key, duration, progress
        self.headers = {"Authorization": f"Bearer {key}"}
        self.api_latency: list[float] = []
        self.action_latency: list[float] = []
        self.api_failures = 0
        self.action_failures = 0
        self.cycles = 0
        self.invariant_samples = 0
        self.screenshot_max_bytes = 0
        self.hint_observation_count = 0
        self.generation_start = None
        self.generation_end = None
        self.poll_active = 0
        self.poll_max_active = 0
        self.poll_lock = threading.Lock()

    def request(self, method: str, path: str, *, body=None, action=False, expected=200, stage="api"):
        started = time.perf_counter()
        try:
            response = self.client.request(method, WORKER_URL + path, headers=self.headers, json=body,
                                           timeout=ACTION_TIMEOUT_SECONDS if action else API_TIMEOUT_SECONDS)
            if response.status_code != expected:
                code = "HTTP_ERROR"
                try:
                    envelope = response.json()
                    detail = envelope.get("detail", {}) if isinstance(envelope, dict) else {}
                    error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
                    candidate = (detail.get("code") if isinstance(detail, dict) else None) or \
                                (error.get("code") if isinstance(error, dict) else None)
                    if isinstance(candidate, str) and candidate in WORKER_ERROR_CODES:
                        code = candidate
                except ValueError:
                    pass
                raise SoakError(f"{code}:{stage}")
            return response
        except httpx.HTTPError:
            if action: self.action_failures += 1
            else: self.api_failures += 1
            raise SoakError(f"REQUEST_FAILED:{stage}") from None
        except SoakError:
            if action: self.action_failures += 1
            else: self.api_failures += 1
            raise
        finally:
            (self.action_latency if action else self.api_latency).append(time.perf_counter() - started)

    def json(self, method, path, *, body=None, action=False, stage="api"):
        response = self.request(method, path, body=body, action=action, stage=stage)
        try: return response.json()
        except ValueError as exc: raise SoakError("invalid_json") from exc

    def control_json(self, path):
        started = time.perf_counter()
        try:
            response = self.control_client.get(CONTROL_URL + path, timeout=API_TIMEOUT_SECONDS)
            if response.status_code != 200:
                raise SoakError("control_http_status_" + str(response.status_code))
            return response.json()
        except httpx.HTTPError:
            self.api_failures += 1
            raise SoakError("control_request_failed") from None
        except ValueError:
            self.api_failures += 1
            raise SoakError("control_invalid_json") from None
        except SoakError:
            self.api_failures += 1
            raise
        finally:
            self.api_latency.append(time.perf_counter() - started)

    def install_fixture(self):
        self.json("POST", "/diagnostics/readiness-fixture", body={"scenario": "tree"})

    def status(self):
        state = self.json("GET", "/status")
        if (state.get("context_count") != 1 or state.get("page_count") != 1
                or state.get("browser_started") is not True or state.get("control_owner") != "none"
                or state.get("action_in_progress") is not False or state.get("import_in_progress") is not False
                or state.get("resource_violation") is not False or state.get("recovery_needed") is not False
                or state.get("recovery_state") != "idle"):
            raise SoakError("worker_idle_one_context_one_page_invariant_failed")
        generation = state.get("recovery_generation")
        if type(generation) is not int:
            raise SoakError("recovery_generation_missing")
        if self.generation_start is None: self.generation_start = generation
        self.generation_end = generation
        if generation != self.generation_start:
            raise SoakError("worker_recovery_generation_changed")
        self.invariant_samples += 1
        return state

    def action(self, epoch: int, payload: dict) -> int:
        kind = payload.get("kind") if payload.get("kind") in {"navigate", "click"} else "action"
        receipt = self.json("POST", "/manual/execute", body={"control_epoch": epoch,
                           "allowed_origins": ["https://readiness-fixture.test"], "action": payload},
                           action=True, stage=kind)
        if receipt.get("outcome") != "applied":
            raise SoakError(f"MANUAL_ACTION_NOT_APPLIED:{kind}")
        next_epoch = receipt.get("control_epoch")
        if (type(next_epoch) is not int or next_epoch <= epoch
                or receipt.get("control_owner") != "human"):
            raise SoakError(f"MANUAL_ACTION_EPOCH_INVALID:{kind}")
        return next_epoch

    def cycle(self):
        gated = self.json("POST", "/gate", body={"owner": "human"})
        epoch = gated.get("control_epoch")
        if type(epoch) is not int or gated.get("control_owner") != "human":
            raise SoakError("human_gate_not_acquired")
        try:
            epoch = self.action(epoch, {"kind": "navigate", "url": FIXTURE})
            epoch = self.action(epoch, {"kind": "click", "x": 383, "y": 208})
            epoch = self.action(epoch, {"kind": "click", "x": 443, "y": 247})
            result = self.json("POST", "/verify", body={"conditions": [
                {"kind": "text_present", "text": "Selected: Desktop"},
                {"kind": "element_checked", "selector": "#desktop", "checked": True},
            ]})
            checks = result.get("checks", [])
            if len(checks) != 2 or not all(item.get("passed") is True for item in checks):
                raise SoakError("fixture_effect_verification_failed")
        finally:
            self.json("POST", "/gate", body={"owner": "none"})
        self.status()
        self.observe()
        self.cycles += 1

    def observe(self):
        observation = self.json("POST", "/observe")
        if observation.get("width") != 1440 or observation.get("height") != 900:
            raise SoakError("observation_viewport_mismatch")
        encoded = observation.get("screenshot_png_b64")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_SCREENSHOT_BYTES + 2) // 3) * 4:
            raise SoakError("observation_screenshot_invalid_or_too_large")
        try:
            screenshot = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise SoakError("observation_screenshot_invalid") from None
        if (len(screenshot) > MAX_SCREENSHOT_BYTES
                or not screenshot.startswith(b"\x89PNG\r\n\x1a\n")):
            raise SoakError("observation_screenshot_invalid_or_too_large")
        hints = observation.get("interaction_hints")
        if (not isinstance(hints, dict) or hints.get("version") != 1
                or hints.get("coordinate_space") != "normalized_1000"
                or not isinstance(hints.get("elements"), list) or len(hints["elements"]) > 32
                or len(json.dumps(hints, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 4096):
            raise SoakError("interaction_hints_invalid")
        self.screenshot_max_bytes = max(self.screenshot_max_bytes, len(screenshot))
        self.hint_observation_count += 1

    def run(self):
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        next_poll = started
        next_progress = started + 30
        errors: list[str] = []
        # Each poll pair runs worker health and control task-list concurrently.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        futures: set[concurrent.futures.Future] = set()
        max_observed_poll_concurrency = 0
        deadline = started + self.duration
        try:
            self.status()
            session = self.control_json("/api/v1/operator/session")
            if session.get("authenticated") is not True:
                raise SoakError("control_session_not_authenticated")
            self.install_fixture()
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_poll:
                    try:
                        max_observed_poll_concurrency = max(max_observed_poll_concurrency,
                                                            self._harvest_futures(futures))
                    except SoakError as exc:
                        errors.append(str(exc))
                        break
                    if not futures:
                        futures.add(executor.submit(self._tracked_poll, self._worker_health_poll))
                        futures.add(executor.submit(self._tracked_poll, self._control_tasks_poll))
                    next_poll = now + 2
                try:
                    self.cycle()
                except Exception as exc:
                    errors.append(str(exc) if isinstance(exc, SoakError) else "functional_cycle_failed")
                    # Attempt controlled gate release, then immediately record whether idle recovers.
                    try:
                        self.json("POST", "/gate", body={"owner": "none"})
                        self.status()
                    except Exception:
                        errors.append("cleanup_or_idle_invariant_failed")
                    break
                if now >= next_progress:
                    self.progress(json.dumps({"elapsed_seconds": int(now-started), "cycles": self.cycles,
                                               "failures": len(errors)}, separators=(",", ":")))
                    next_progress = now + 30
            # Wait for every dispatched read-only poll, including the final pair.
            if futures:
                done, pending = concurrent.futures.wait(futures, timeout=API_TIMEOUT_SECONDS + 0.5)
                for future in done:
                    try:
                        future.result()
                    except Exception:
                        errors.append("background_poll_failed")
                if pending:
                    errors.append("background_poll_timeout")
                    for future in pending:
                        future.cancel()
        except Exception as exc:
            errors.append(str(exc) if isinstance(exc, SoakError) else "soak_setup_failed")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        elapsed = time.monotonic() - started
        required_cycles = max(1, int(self.duration // 60))
        api_summary = latency_summary(self.api_latency)
        action_summary = latency_summary(self.action_latency)
        latency_ok = (api_summary.get("p95_ms", 0) <= API_TIMEOUT_SECONDS * 1000
                      and action_summary.get("p95_ms", 0) <= ACTION_TIMEOUT_SECONDS * 1000)
        status = ("PASS" if self.cycles >= required_cycles and self.hint_observation_count == self.cycles
                  and not errors and self.api_failures == 0 and self.action_failures == 0 else "FAIL")
        if not latency_ok:
            status = "FAIL"
        return {"schema": "readiness-soak/v1", "status": status,
                "started_at_utc": started_at,
                "duration_requested_seconds": self.duration, "duration_actual_seconds": round(elapsed, 2),
                "cycles_completed": self.cycles, "functional_failures": errors,
                "api_failures": self.api_failures, "action_failures": self.action_failures,
                "latency": {"api": api_summary, "actions": action_summary},
                "pass_criteria": {"minimum_cycles": required_cycles, "api_p95_max_ms": API_TIMEOUT_SECONDS * 1000,
                                  "actions_p95_max_ms": ACTION_TIMEOUT_SECONDS * 1000, "latency_within_bounds": latency_ok,
                                  "zero_functional_or_api_failures": not errors and self.api_failures == 0 and self.action_failures == 0},
                "invariants": {"samples": self.invariant_samples, "context_count": 1, "page_count": 1,
                               "control_owner_after_cycle": "none", "recovery_generation_start": self.generation_start,
                               "recovery_generation_end": self.generation_end,
                               "generation_stable": self.generation_start == self.generation_end},
                "screenshot_max_bytes": self.screenshot_max_bytes,
                "hint_observation_count": self.hint_observation_count,
                "candidate_hints_flag": True,
                "resource_metrics": {"worker_memory": "unavailable_from_worker_API; collect Docker/cgroup externally"},
                "polling": {"configured_pair_size": 2,
                            "max_observed_concurrency": max(max_observed_poll_concurrency, self.poll_max_active)},
                "limits": {"api_timeout_seconds": API_TIMEOUT_SECONDS,
                           "action_timeout_seconds": ACTION_TIMEOUT_SECONDS, "max_duration_seconds": MAX_DURATION_SECONDS}}

    def _tracked_poll(self, operation):
        with self.poll_lock:
            self.poll_active += 1
            self.poll_max_active = max(self.poll_max_active, self.poll_active)
        try:
            return operation()
        finally:
            with self.poll_lock:
                self.poll_active -= 1

    def _worker_health_poll(self):
        response = self.request("GET", "/health")
        data = response.json()
        if data.get("ready") is not True or data.get("recovery_state") != "idle":
            self.api_failures += 1
            raise SoakError("worker_health_not_ready")

    def _control_tasks_poll(self):
        data = self.control_json("/api/v1/tasks")
        if not isinstance(data.get("tasks"), list):
            raise SoakError("control_task_list_invalid")

    def _harvest_futures(self, futures):
        for future in tuple(futures):
            if not future.done():
                continue
            try:
                future.result()
            except Exception:
                futures.discard(future)
                raise SoakError("background_poll_failed") from None
            futures.discard(future)
        return self.poll_max_active


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=WORKER_URL)
    parser.add_argument("--control-url", default=CONTROL_URL)
    parser.add_argument("--key-file", type=Path, default=os.environ.get("WORKER_API_KEY_FILE"))
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path, default=Path("readiness-soak-report.json"))
    parser.add_argument("--i-confirm-disposable", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not 1 <= args.duration_seconds <= MAX_DURATION_SECONDS or args.key_file is None:
        print(json.dumps({"status": "BLOCKED", "error_code": "INVALID_BOUNDED_CONFIGURATION"}))
        return 2
    try:
        key = validate_environment(confirmed=args.i_confirm_disposable, worker_url=args.url,
                                   control_url=args.control_url, key_file=args.key_file)
    except (ValueError, OSError):
        print(json.dumps({"status": "BLOCKED", "error_code": "DISPOSABLE_GUARD_REJECTED"}))
        return 2
    with httpx.Client(trust_env=False, follow_redirects=False) as client, \
            httpx.Client(base_url=CONTROL_URL, trust_env=False, follow_redirects=False) as control_client:
        report = Soak(client, control_client, key, args.duration_seconds).run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "cycles": report["cycles_completed"],
                      "report": str(args.output)}, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
