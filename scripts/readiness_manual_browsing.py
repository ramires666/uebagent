"""Prove manual cross-origin navigation stays human-only in a disposable runtime."""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

import httpx

from scripts.readiness_soak import CONTROL_URL, WORKER_URL, SoakError, validate_environment

SCHEMA = "readiness-manual-browsing/v1"
SOURCE_URL = "https://readiness-fixture.test/cross_origin"
SOURCE_ORIGIN = "https://readiness-fixture.test"
TARGET_URL = "https://readiness-target.test/landing"
LINK_POINT = {"x": 720, "y": 285}
TIMEOUT_SECONDS = 10.0


class ProofError(RuntimeError):
    pass


class ManualBrowsingProof:
    def __init__(self, client, key):
        self.client = client
        self.headers = {"Authorization": f"Bearer {key}"}

    def request(self, method, path, body=None, *, expected_status=200):
        try:
            response = self.client.request(method, WORKER_URL + path, headers=self.headers,
                                           json=body, timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError:
            raise ProofError("worker_request_failed") from None
        if expected_status is not None and response.status_code != expected_status:
            raise ProofError(f"unexpected_http_status_{response.status_code}")
        return response

    def json(self, method, path, body=None):
        response = self.request(method, path, body)
        try:
            value = response.json()
        except ValueError:
            raise ProofError("worker_invalid_json") from None
        if not isinstance(value, dict):
            raise ProofError("worker_response_malformed")
        return value

    @staticmethod
    def error_code(response):
        try:
            value = response.json()
        except ValueError:
            raise ProofError("worker_error_response_malformed") from None
        detail = value.get("detail") if isinstance(value, dict) else None
        candidate = detail.get("code") if isinstance(detail, dict) else None
        if candidate not in {"POLICY_DENIED", "CONTROL_NOT_AGENT", "CONTROL_NOT_HUMAN"}:
            raise ProofError("expected_policy_denial_missing")
        return candidate

    def check_status(self, expected_owner):
        state = self.json("GET", "/status")
        if (state.get("browser_started") is not True or state.get("context_count") != 1
                or state.get("page_count") != 1 or state.get("control_owner") != expected_owner
                or state.get("action_in_progress") is not False
                or state.get("import_in_progress") is not False
                or state.get("resource_violation") is not False):
            raise ProofError("single_page_control_invariant_failed")
        page_id = state.get("page_id")
        if not isinstance(page_id, str) or not page_id or len(page_id) > 64:
            raise ProofError("page_identity_missing")
        if expected_owner == "human" and state.get("manual_cross_origin_navigation") is not True:
            raise ProofError("manual_navigation_default_not_enabled")
        if expected_owner != "human" and state.get("manual_cross_origin_navigation") is not False:
            raise ProofError("manual_navigation_policy_not_fenced")
        return page_id

    def gate(self, owner):
        result = self.json("POST", "/gate", {"owner": owner})
        epoch = result.get("control_epoch")
        if type(epoch) is not int or result.get("control_owner") != owner:
            raise ProofError("control_gate_receipt_invalid")
        return epoch

    def manual(self, epoch, action, *, allow_cross_origin_navigation=None, expected_status=200):
        body = {"control_epoch": epoch, "action": action}
        if allow_cross_origin_navigation is not None:
            body["allow_cross_origin_navigation"] = allow_cross_origin_navigation
        return self.request("POST", "/manual/execute", body, expected_status=expected_status)

    def manual_applied(self, epoch, action, *, allow_cross_origin_navigation=None):
        response = self.manual(epoch, action, allow_cross_origin_navigation=allow_cross_origin_navigation)
        if response.status_code != 200:
            self.error_code(response)
            raise ProofError("manual_action_not_applied")
        try:
            result = response.json()
        except ValueError:
            raise ProofError("manual_receipt_invalid") from None
        next_epoch = result.get("control_epoch")
        if (result.get("outcome") != "applied" or type(next_epoch) is not int
                or next_epoch <= epoch or result.get("control_owner") != "human"):
            raise ProofError("manual_receipt_invalid")
        return next_epoch

    def verify_target(self):
        deadline = time.monotonic() + 4.0
        while True:
            result = self.json("POST", "/verify", {"conditions": [{"kind": "text_present", "text": "TARGET_READY"}]})
            checks = result.get("checks")
            if isinstance(checks, list) and len(checks) == 1 and checks[0].get("passed") is True:
                return
            if time.monotonic() >= deadline:
                raise ProofError("target_fixture_verification_failed")
            time.sleep(0.2)

    def run(self):
        initial_page = self.check_status("none")
        installed = self.json("POST", "/diagnostics/readiness-fixture", {"scenario": "cross_origin"})
        if installed.get("installed") is not True or installed.get("scenario") != "cross_origin":
            raise ProofError("fixed_fixture_install_failed")
        results = {"schema": SCHEMA, "status": "FAIL", "scenario": "cross_origin",
                   "default_navigation_enabled": False, "default_click_reached_target": False,
                   "opt_out_error_code": None, "agent_error_code": None,
                   "single_page_preserved": False}
        try:
            epoch = self.gate("human")
            self.check_status("human")
            epoch = self.manual_applied(epoch, {"kind": "navigate", "url": SOURCE_URL})
            epoch = self.manual_applied(epoch, {"kind": "click", **LINK_POINT})
            results["default_navigation_enabled"] = True
            self.verify_target()
            results["default_click_reached_target"] = True

            epoch = self.manual_applied(epoch, {"kind": "navigate", "url": SOURCE_URL})
            rejected = self.manual(epoch, {"kind": "click", **LINK_POINT},
                                   allow_cross_origin_navigation=False, expected_status=None)
            results["opt_out_error_code"] = self.error_code(rejected)
            if results["opt_out_error_code"] != "POLICY_DENIED":
                raise ProofError("manual_opt_out_policy_mismatch")
        finally:
            self.json("POST", "/gate", {"owner": "none"})

        self.gate("none")
        agent_epoch = self.gate("agent")
        try:
            self.check_status("agent")
            observation = self.json("POST", "/observe")
            observation_id = observation.get("observation_id")
            if not isinstance(observation_id, str) or not observation_id or len(observation_id) > 128:
                raise ProofError("agent_observation_missing")
            agent_action = {"action_id": f"manual-scope-proof-{uuid.uuid4().hex}",
                            "control_epoch": agent_epoch, "observation_id": observation_id,
                            "allowed_origins": [SOURCE_ORIGIN], "allow_write": True,
                            "action": {"kind": "click", **LINK_POINT}}
            denied_agent = self.request("POST", "/execute", agent_action, expected_status=None)
            results["agent_error_code"] = self.error_code(denied_agent)
            if results["agent_error_code"] != "POLICY_DENIED":
                raise ProofError("agent_allowlist_was_weakened")
        finally:
            self.json("POST", "/gate", {"owner": "none"})
        final_page = self.check_status("none")
        results["single_page_preserved"] = final_page == initial_page
        if not results["single_page_preserved"]:
            raise ProofError("page_identity_changed")
        results["status"] = "DIAGNOSTIC"
        return results


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=WORKER_URL)
    parser.add_argument("--control-url", default=CONTROL_URL)
    parser.add_argument("--key-file", type=Path, default=os.environ.get("WORKER_API_KEY_FILE"))
    parser.add_argument("--output", type=Path, default=Path("/app/runtime/control-data/readiness-manual-browsing.json"))
    parser.add_argument("--i-confirm-disposable", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.key_file is None:
        print(json.dumps({"status": "BLOCKED", "error_code": "WORKER_KEY_FILE_REQUIRED"}))
        return 2
    try:
        key = validate_environment(confirmed=args.i_confirm_disposable, worker_url=args.url,
                                   control_url=args.control_url, key_file=args.key_file)
    except (ValueError, OSError):
        print(json.dumps({"status": "BLOCKED", "error_code": "DISPOSABLE_GUARD_REJECTED"}))
        return 2
    try:
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            report = ManualBrowsingProof(client, key).run()
    except ProofError as exc:
        report = {"schema": SCHEMA, "status": "FAIL", "error_code": str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output)}, separators=(",", ":")))
    return 0 if report["status"] == "DIAGNOSTIC" else 1


if __name__ == "__main__":
    raise SystemExit(main())
