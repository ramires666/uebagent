"""Fixed 600-cycle memory attribution diagnostic for disposable Camoufox workers."""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.readiness_memory_probe import (
    GC_IDLE_AFTER_SECONDS,
    GC_IDLE_BEFORE_SECONDS,
    OPERATION_INTERVAL_SECONDS,
    Probe,
)
from scripts.readiness_soak import CONTROL_URL, WORKER_URL, SoakError, validate_environment

SCHEMA = "readiness-memory-attribution/v1"
VARIANTS = ("baseline", "no_trace", "no_addons", "neither")
EXPECTED_FLAGS = {
    "baseline": (True, True),
    "no_trace": (False, True),
    "no_addons": (True, False),
    "neither": (False, False),
}
CYCLE_COUNT = 600
WARMUP_SECONDS = 5
SETTLE_SECONDS = 15
CONTRAST_SECONDS = GC_IDLE_BEFORE_SECONDS + GC_IDLE_AFTER_SECONDS
HARD_DEADLINE_SECONDS = 240
PROGRESS_EVERY = 100


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error(exc: Exception) -> str:
    """Map internal errors to a small, fixed set; never serialize exception text."""
    if isinstance(exc, SoakError):
        value = str(exc)
        if value in {"idle_invariant_failed", "variant_mismatch", "configuration_invalid",
                     "identity_missing", "identity_changed", "deadline_exceeded",
                     "configuration_unavailable", "gc_contrast_failed"}:
            return value
        if value.startswith("http_status_"):
            return "http_status_error"
    if isinstance(exc, httpx.TimeoutException):
        return "request_timeout"
    return "diagnostic_operation_failed"


class DeadlineClient:
    """Clamp every Probe request to the remaining fixed workload deadline."""

    def __init__(self, client: httpx.Client, deadline: float, clock):
        self.client, self.deadline, self.clock = client, deadline, clock

    def request(self, method, url, **kwargs):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise httpx.TimeoutException("deadline")
        requested = kwargs.get("timeout", 10.0)
        kwargs["timeout"] = min(float(requested), remaining)
        return self.client.request(method, url, **kwargs)


class Attribution:
    def __init__(self, client: httpx.Client, key: str, variant: str, *, gc_contrast=False,
                 clock=time.monotonic, sleep=time.sleep, progress=print,
                 _cycle_count=CYCLE_COUNT, _operation_interval=OPERATION_INTERVAL_SECONDS,
                 _warmup_seconds=WARMUP_SECONDS, _settle_seconds=SETTLE_SECONDS,
                 _contrast_seconds=CONTRAST_SECONDS,
                 _deadline_seconds=HARD_DEADLINE_SECONDS):
        if variant not in VARIANTS:
            raise ValueError("invalid_variant")
        if type(_cycle_count) is not int or _cycle_count < 2:
            raise ValueError("invalid_internal_cycle_count")
        if gc_contrast and variant != "baseline":
            raise ValueError("gc_contrast_requires_baseline_variant")
        self.client, self.key, self.variant = client, key, variant
        self.gc_contrast_enabled = bool(gc_contrast)
        self.clock, self.sleep, self.progress = clock, sleep, progress
        self.cycle_count = _cycle_count
        self.operation_interval = _operation_interval
        self.warmup_seconds = _warmup_seconds
        self.settle_seconds, self.contrast_seconds = _settle_seconds, _contrast_seconds
        self.deadline_seconds = _deadline_seconds

    def _emit(self, event, **fields):
        self.progress(json.dumps({"event": event, **fields}, separators=(",", ":")))

    def _config(self, probe: Probe) -> dict:
        try:
            payload = probe.json("GET", "/diagnostics/readiness-memory-config")
        except Exception:
            raise SoakError("configuration_unavailable") from None
        if not isinstance(payload, dict):
            raise SoakError("configuration_invalid")
        expected_trace, expected_addons = EXPECTED_FLAGS[self.variant]
        if (payload.get("trace_snapshots_enabled") is not expected_trace
                or payload.get("default_addons_enabled") is not expected_addons
                or payload.get("backend") != "camoufox"):
            raise SoakError("variant_mismatch")
        return {"matches_expected": True}

    @staticmethod
    def _identity(state: dict) -> tuple:
        keys = ("worker_instance_id", "browser_instance_id", "page_id", "recovery_generation")
        values = tuple(state.get(key) for key in keys)
        if (not all(isinstance(value, str) and value for value in values[:3])
                or type(values[3]) is not int or values[3] < 0):
            raise SoakError("identity_missing")
        return values

    def run(self) -> dict:
        started_at = utc_now()
        started = self.clock()
        deadline = started + self.deadline_seconds
        errors: list[str] = []
        samples: list[dict] = []
        configuration_matches = False
        identity_before = identity_after = None
        cycles_completed = 0
        settle_seconds = contrast_seconds = 0
        gc_result = None
        probe = Probe(self.client, self.key, clock=self.clock, sleep=self.sleep,
                      progress=lambda _event: None)
        probe.client = DeadlineClient(self.client, deadline, self.clock)
        try:
            self._emit("progress", variant=self.variant, completed_cycles=0,
                       total_cycles=self.cycle_count, at_utc=utc_now())
            self._config(probe)
            configuration_matches = True
            probe.setup()
            self.sleep(self.warmup_seconds)
            before_status = probe.idle_status()
            identity_before = self._identity(before_status)
            next_start = self.clock()

            for cycle in range(1, self.cycle_count + 1):
                if self.clock() >= deadline:
                    raise SoakError("deadline_exceeded")
                delay = next_start - self.clock()
                if delay > 0:
                    self.sleep(delay)
                if self.clock() >= deadline:
                    raise SoakError("deadline_exceeded")
                image_bytes = probe.click_verify(observe=True)
                cycles_completed = cycle
                samples.append({"cycle": cycle, "ended_at_utc": utc_now(),
                                "image_bytes": image_bytes})
                if cycle % PROGRESS_EVERY == 0 or cycle == self.cycle_count:
                    self._emit("progress", variant=self.variant, completed_cycles=cycle,
                               total_cycles=self.cycle_count, at_utc=utc_now())
                if cycle < self.cycle_count:
                    next_start = max(next_start + self.operation_interval,
                                     self.clock())

            workload_end = utc_now()
            self._emit("workload_end", variant=self.variant, completed_cycles=cycles_completed,
                       at_utc=workload_end)
            settle_start = self.clock()
            self.sleep(self.settle_seconds)
            settle_seconds = round(self.clock() - settle_start, 3)
            settle_end = utc_now()
            self._emit("settle_end", variant=self.variant, at_utc=settle_end)
            contrast_start = utc_now()
            self._emit("contrast_start", variant=self.variant, gc_contrast=self.gc_contrast_enabled,
                       at_utc=contrast_start)
            contrast_clock_start = self.clock()
            if self.gc_contrast_enabled:
                # Existing bounded helper performs 2 s idle, one GC request, then 8 s idle.
                gc_result = probe.gc_contrast()
                if gc_result.get("error_code"):
                    raise SoakError("gc_contrast_failed")
            else:
                # Matched idle control: identical 2+8 s window without an HTTP request.
                self.sleep(GC_IDLE_BEFORE_SECONDS)
                self.sleep(GC_IDLE_AFTER_SECONDS)
            contrast_seconds = round(self.clock() - contrast_clock_start, 3)
            contrast_end = utc_now()
            self._emit("contrast_end", variant=self.variant, gc_contrast=self.gc_contrast_enabled,
                       at_utc=contrast_end)
            after_status = probe.idle_status()
            identity_after = self._identity(after_status)
            if identity_before != identity_after:
                raise SoakError("identity_changed")
        except Exception as exc:
            code = "deadline_exceeded" if self.clock() >= deadline else safe_error(exc)
            errors.append(code)

        status = "DIAGNOSTIC" if not errors and cycles_completed == self.cycle_count and configuration_matches else "FAIL"
        return {
            "schema": SCHEMA,
            "status": status,
            "variant": self.variant,
            "started_at_utc": started_at,
            "ended_at_utc": utc_now(),
            "cycles_expected": self.cycle_count,
            "cycles_completed": cycles_completed,
            "samples": samples,
            "image_sizes": {"count": len(samples),
                            "total_bytes": sum(row["image_bytes"] for row in samples),
                            "max_bytes": max((row["image_bytes"] for row in samples), default=0)},
            "workload_end_utc": locals().get("workload_end"),
            "settle_end_utc": locals().get("settle_end"),
            "contrast_start_utc": locals().get("contrast_start"),
            "contrast_end_utc": locals().get("contrast_end"),
            "settle_seconds": settle_seconds,
            "contrast_seconds": contrast_seconds,
            "gc_contrast": gc_result,
            "gc_control": not self.gc_contrast_enabled,
            "error_codes": errors,
            "invariants": {
                "configuration_matches": configuration_matches,
                "identity_before_present": identity_before is not None,
                "identity_after_present": identity_after is not None,
                "identity_stable": identity_before is not None and identity_before == identity_after,
                "one_context_one_page": bool(after_status.get("context_count") == 1
                                             and after_status.get("page_count") == 1)
                    if identity_after is not None else False,
                "recovery_generation_stable": bool(identity_before is not None
                                                    and identity_after is not None
                                                    and identity_before[3] == identity_after[3]),
            },
            "limits": {"cycle_rate_max_hz": 7, "operation_deadline_seconds": self.deadline_seconds,
                       "warmup_seconds": self.warmup_seconds,
                       "settle_seconds": self.settle_seconds,
                       "contrast_window_seconds": self.contrast_seconds},
        }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=WORKER_URL)
    parser.add_argument("--control-url", default=CONTROL_URL)
    parser.add_argument("--key-file", type=Path, default=os.environ.get("WORKER_API_KEY_FILE"))
    parser.add_argument("--output", type=Path,
                        default=Path("/app/runtime/control-data/readiness-memory-attribution.json"))
    parser.add_argument("--variant", choices=VARIANTS, required=True)
    parser.add_argument("--gc-contrast", action="store_true")
    parser.add_argument("--i-confirm-disposable", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.key_file is None or (args.gc_contrast and args.variant != "baseline"):
        print(json.dumps({"status": "BLOCKED", "error_code": "INVALID_BOUNDED_CONFIGURATION"}))
        return 2
    try:
        key = validate_environment(confirmed=args.i_confirm_disposable, worker_url=args.url,
                                   control_url=args.control_url, key_file=args.key_file)
    except (ValueError, OSError):
        print(json.dumps({"status": "BLOCKED", "error_code": "DISPOSABLE_GUARD_REJECTED"}))
        return 2
    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        report = Attribution(client, key, args.variant, gc_contrast=args.gc_contrast).run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "cycles": report["cycles_completed"],
                      "report": str(args.output)}, separators=(",", ":")))
    return 0 if report["status"] == "DIAGNOSTIC" else 1


if __name__ == "__main__":
    raise SystemExit(main())
