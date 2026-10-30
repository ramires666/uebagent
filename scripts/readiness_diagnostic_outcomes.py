"""Run bounded, read-only observations on the allowlisted public diagnostic pages."""
from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

WORKER_URL = "http://browser:7070"
CONTROL_URL = "http://127.0.0.1:8080"
EXPECTED_ORIGIN_ASSIGNMENT = "antibot-2026-10-31-direct"
EXPECTED_ROUTE_GENERATION = 1
PROFILE_ID = "p2"  # Existing profile; this runner never claims to create a fresh profile.
UNVERIFIED = "UNVERIFIED"
CHALLENGE_TEXT = (
    "Verify you are human", "Access denied", "Robot Check", "unusual traffic",
    "Request blocked", "Security Measure", "Just a moment...", "challenge-platform",
)

PAGES = (
    {
        "site": "sannysoft", "url": "https://bot.sannysoft.com/",
        "origin": "https://bot.sannysoft.com",
        "resource_origins": ("https://bot.sannysoft.com", "https://cdnjs.cloudflare.com",
                             "https://cdn.jsdelivr.net"),
        "settle_seconds": 12,
        "labels": ("Intoli.com tests", "WebDriver Advanced"),
        "outcomes": (
            ("webdriver_class", "visible", "#webdriver-result.passed", (True,)),
            ("webdriver_class", "visible", "#webdriver-result.failed", (True,)),
            ("webdriver_text", "text", "#webdriver-result", ("missing (passed)", "present (failed)")),
            ("chrome_class", "visible", "#chrome-result.passed", (True,)),
            ("chrome_class", "visible", "#chrome-result.failed", (True,)),
            ("chrome_text", "text", "#chrome-result", ("missing (failed)", "present (passed)")),
            ("permissions", "text", "#permissions-result", ("prompt", "granted", "denied")),
            ("advanced_class", "visible", "#advanced-webdriver-result.passed", (True,)),
            ("advanced_class", "visible", "#advanced-webdriver-result.failed", (True,)),
            ("advanced_text", "text", "#advanced-webdriver-result", ("passed", "failed")),
            ("plugin_array_class", "visible", "#plugins-type-result.passed", (True,)),
            ("plugin_array_class", "visible", "#plugins-type-result.failed", (True,)),
            ("plugin_array_text", "text", "#plugins-type-result", ("passed", "failed")),
            ("plugins_length_bucket", "visible", '#plugins-length-result:text-matches("^0(?:\\s*\\((?:passed|failed)\\))?$")', (True,)),
            ("plugins_length_nonzero_bucket", "visible", '#plugins-length-result:text-matches("^[1-9][0-9]*(?:\\s*\\((?:passed|failed)\\))?$")', (True,)),
            ("languages_empty", "visible", '#languages-result:empty', (True,)),
            ("languages_present", "visible", '#languages-result:text-matches("^[A-Za-z]{2,3}(-[A-Za-z0-9]+)?(\\s*,\\s*[A-Za-z]{2,3}(-[A-Za-z0-9]+)?)*$")', (True,)),
        ),
    },
    {
        "site": "browserleaks_js", "url": "https://browserleaks.com/javascript",
        "origin": "https://browserleaks.com",
        "resource_origins": ("https://browserleaks.com",),
        "labels": ("JavaScript Detection", "JavaScript Enabled", "Inline Scripts", "Same-Origin Scripts", "Third-Party Scripts"),
        "outcomes": tuple(
            (name, "visible", (f'#js-detect tr:has-text("JavaScript Enabled") td:nth-child(2) .{state}'
                                if selector == "js-enabled" else f"#{selector} .{state}"), (True,))
            for name, selector in (("javascript_enabled", "js-enabled"),
                                   ("inline_scripts", "js-inline"),
                                   ("same_origin_scripts", "js-self"),
                                   ("third_party_scripts", "js-third"))
            for state in ("true", "false")
        ),
    },
    {
        "site": "browserleaks_webgl", "url": "https://browserleaks.com/webgl",
        "origin": "https://browserleaks.com",
        "resource_origins": ("https://browserleaks.com",),
        "labels": ("WebGL Support Detection",),
        "outcomes": tuple(
            (name, "visible", f"#{selector} .{state}", (True,))
            for name, selector in (("webgl_1_support", "gl1-status"),
                                   ("webgl_2_support", "gl2-status"))
            for state in ("true", "false")
        ),
    },
)


def unique_outcome(matches: Iterable[str], allowed: Iterable[str]) -> str:
    """Return a normalized allowed outcome only for one exact candidate match."""
    observed = list(matches)
    valid = {item.strip().casefold(): item for item in allowed}
    found = [valid[item.strip().casefold()] for item in observed
             if item.strip().casefold() in valid]
    return found[0] if len(observed) == 1 and len(found) == 1 else UNVERIFIED


def plugin_length_bucket(zero_match: bool | str, nonzero_match: bool | None = None) -> str:
    if nonzero_match is None and isinstance(zero_match, str):
        raw = zero_match.strip()
        if not raw.isdecimal():
            return UNVERIFIED
        return "zero" if int(raw) == 0 else "nonzero"
    if zero_match == nonzero_match:
        return UNVERIFIED
    return "zero" if zero_match else "nonzero"


def languages_presence(empty_match: bool | str, present_match: bool | None = None) -> str:
    if present_match is None and isinstance(empty_match, str):
        raw = empty_match.strip()
        return "empty" if raw.casefold() in {"", "[]", "none", "null", "undefined"} else "present"
    if empty_match == present_match:
        return UNVERIFIED
    return "empty" if empty_match else "present"


def boolean_class(true_match: bool, false_match: bool) -> str:
    if true_match == false_match:
        return UNVERIFIED
    return "true" if true_match else "false"


def result_class(passed: bool, failed: bool, *, pass_value: str = "passed",
                 fail_value: str = "failed") -> str:
    if passed == failed:
        return UNVERIFIED
    return pass_value if passed else fail_value


def class_text_outcome(class_matches: Iterable[bool], text_matches: Iterable[bool],
                       candidates: Iterable[str], *, pass_value: str = "passed",
                       fail_value: str = "failed") -> str:
    """Require the initialized visible value to agree with its status class."""
    class_state = result_class(*list(class_matches), pass_value=pass_value, fail_value=fail_value)
    text_candidates = list(candidates)
    text_state = unique_outcome([value for value, matched in zip(text_candidates, text_matches) if matched],
                                text_candidates)
    return class_state if class_state == text_state else UNVERIFIED


def support_outcome(value: str | None) -> str:
    """Classify explicit boolean text; reject rows displaying both legend values."""
    if value is None:
        return UNVERIFIED
    normalized = value.strip().casefold()
    if (normalized.startswith("false") or normalized.startswith("✖ false")
            or normalized.startswith("✘ false")) and "true" not in normalized:
        return "false"
    if (normalized.startswith("true") or normalized.startswith("✔ true")
            or normalized.startswith("✓ true")) and "false" not in normalized:
        return "true"
    return UNVERIFIED


def diagnostic_verdict(*, blocked: bool, expected_page_verified: bool,
                       observations: Iterable[str]) -> str:
    if blocked:
        return "BLOCKED"
    values = list(observations)
    if not expected_page_verified or not values or any(v == UNVERIFIED for v in values):
        return "OBSERVATIONS"
    return "DIAGNOSTIC_COMPLETED"


class ProbeError(RuntimeError):
    def __init__(self, step: str, code: str):
        super().__init__(f"{step}: {code}")
        self.step, self.code = step, code


def validate_environment(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("APP_ENV", "").lower() != "test" or env.get("READINESS_DISPOSABLE") != "1":
        raise ProbeError("guard", "disposable_test_markers_required")
    if env.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081":
        raise ProbeError("guard", "test_viewer_origin_required")
    if env.get("BROWSER_WORKER_URL", WORKER_URL) != WORKER_URL:
        raise ProbeError("guard", "test_worker_url_required")
    key_path = Path(env.get("WORKER_API_KEY_FILE", ""))
    if not key_path.is_file():
        raise ProbeError("guard", "worker_key_file_missing")
    return key_path


def read_key(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ProbeError("guard", "invalid_worker_key")
    return value


def check_result_shape(result: dict, expected: int) -> list[bool]:
    checks = result.get("checks")
    if not isinstance(checks, list) or len(checks) != expected or any(not isinstance(x, dict) for x in checks):
        raise ProbeError("verify", "invalid_result_shape")
    return [item.get("passed") is True for item in checks]


def worker_status(client: httpx.Client, headers: dict) -> dict:
    response = client.get(WORKER_URL + "/status", headers=headers)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ProbeError("worker_status", "invalid_shape")
    return value


def assert_worker_idle(status: dict, *, expected_url: str | None = None) -> None:
    if (status.get("browser_started") is not True or status.get("control_owner") != "none"
            or status.get("action_in_progress") is not False or status.get("import_in_progress") is not False
            or status.get("resource_violation") is not False or status.get("recovery_needed") is not False
            or status.get("recovery_state") != "idle" or status.get("context_count") != 1
            or status.get("page_count") != 1 or status.get("width") != 1440 or status.get("height") != 900
            or status.get("proxy_assignment_id") != EXPECTED_ORIGIN_ASSIGNMENT
            or status.get("route_generation") != EXPECTED_ROUTE_GENERATION):
        raise ProbeError("worker_precondition", "test_worker_state_mismatch")
    if expected_url is not None and status.get("url") != expected_url:
        raise ProbeError("worker_precondition", "expected_page_not_open")


def no_other_tasks(client: httpx.Client) -> None:
    session = client.get(CONTROL_URL + "/api/v1/operator/session")
    session.raise_for_status()
    session_data = session.json()
    if session_data.get("local_ui") is not True or session_data.get("authenticated") is not True:
        raise ProbeError("task_precondition", "local_operator_session_unavailable")
    response = client.get(CONTROL_URL + "/api/v1/tasks")
    response.raise_for_status()
    tasks = response.json().get("tasks")
    if not isinstance(tasks, list):
        raise ProbeError("task_precondition", "task_list_invalid")
    active = {"queued", "running", "pause_requested", "paused", "waiting_human", "waiting_approval", "recovering"}
    terminal = {"succeeded", "failed", "cancelled", "expired"}
    statuses = [str(task.get("status", "")).casefold() for task in tasks if isinstance(task, dict)]
    if len(statuses) != len(tasks) or any(status in active or status not in terminal for status in statuses):
        raise ProbeError("task_precondition", "another_task_is_active")


def verify(client: httpx.Client, headers: dict, conditions: list[dict]) -> list[bool]:
    response = client.post(WORKER_URL + "/verify", headers=headers, json={"conditions": conditions})
    response.raise_for_status()
    return check_result_shape(response.json(), len(conditions))


def known_dependency_blocks(status: dict, page: dict) -> dict[str, int]:
    known = {"https://cdnjs.cloudflare.com", "https://cdn.jsdelivr.net"}
    if page["site"] != "sannysoft":
        return {}
    counts: dict[str, int] = {}
    for item in status.get("blocked_resources", []):
        if not isinstance(item, dict) or item.get("resource_type") != "script":
            continue
        host = urlsplit(str(item.get("origin", ""))).hostname
        origin = "https://" + host if host else ""
        if origin in known:
            count = item.get("count", 1)
            counts[host] = counts.get(host, 0) + (count if type(count) is int and 0 <= count <= 10000 else 1)
    return counts


def navigate_once(client: httpx.Client, headers: dict, page: dict, initial_url: str) -> None:
    initial = urlsplit(initial_url)
    if (initial_url != "about:blank" and initial_url not in {entry["url"] for entry in PAGES}
            and (initial.scheme + "://" + initial.netloc) not in ({entry["origin"] for entry in PAGES}
                                                                  | {"https://www.ebay.com"})):
        raise ProbeError("navigation", "initial_origin_not_allowlisted")
    gate = client.post(WORKER_URL + "/gate", headers=headers, json={"owner": "human"})
    gate.raise_for_status()
    epoch = gate.json().get("control_epoch")
    if type(epoch) is not int:
        raise ProbeError("gate", "human_gate_acquire_failed")
    try:
        allowed_origins = list(page.get("resource_origins", (page["origin"],)))
        response = client.post(WORKER_URL + "/manual/execute", headers=headers, json={
            "control_epoch": epoch, "allowed_origins": allowed_origins,
            "allow_cross_origin_navigation": False,
            "action": {"kind": "navigate", "url": page["url"]},
        })
        response.raise_for_status()
        if response.json().get("outcome") != "applied":
            raise ProbeError("navigation", "navigate_not_applied")
        time.sleep(int(page.get("settle_seconds", 5)))
    finally:
        release = client.post(WORKER_URL + "/gate", headers=headers, json={"owner": "none"})
        release.raise_for_status()
        if release.json().get("control_owner") != "none":
            raise ProbeError("gate", "release_failed")


def observe_page(client: httpx.Client, headers: dict, page: dict) -> dict:
    status = worker_status(client, headers)
    assert_worker_idle(status, expected_url=page["url"])
    conditions = ([{"kind": "url_is", "url": page["url"]}]
                  + [{"kind": "text_present", "text": text} for text in page["labels"]]
                  + [{"kind": "text_present", "text": text} for text in CHALLENGE_TEXT])
    flags = verify(client, headers, conditions)
    label_flags = flags[1:1 + len(page["labels"])]
    challenge_flags = flags[1 + len(page["labels"]):]
    challenge = any(challenge_flags)
    page_verified = flags[0] and all(label_flags)
    record = {"site": page["site"], "expected_url": page["url"],
              "observed_url": page["url"] if flags[0] else None, "url_verified": flags[0],
              "resource_origins_allowlisted": list(page.get("resource_origins", (page["origin"],))),
              "required_labels": dict(zip(page["labels"], label_flags)),
              "challenge_indicators": dict(zip(CHALLENGE_TEXT, challenge_flags)),
              "outcomes": {}, "challenge_detected": challenge}
    if challenge:
        record["verdict"] = "BLOCKED"
        return record

    dependency_blocks = known_dependency_blocks(status, page)
    record["initialization"] = {
        "status": "UNVERIFIED" if dependency_blocks else "dependency_requests_unblocked",
        "known_dependency_script_blocks": dependency_blocks,
        "completion_marker_available": False,
    }
    if dependency_blocks:
        unresolved = {
            "webdriver": UNVERIFIED, "chrome": UNVERIFIED, "permissions": UNVERIFIED,
            "advanced_webdriver": UNVERIFIED, "plugin_array": UNVERIFIED,
            "plugins_length_bucket": UNVERIFIED, "languages_presence": UNVERIFIED,
        }
        record["outcomes"] = unresolved
        record["verdict"] = "OBSERVATIONS"
        return record

    values: dict[str, list[bool]] = {}
    for name, kind, selector, candidates in page["outcomes"]:
        conditions = []
        if kind == "text":
            conditions = [{"kind": "element_text_contains", "selector": selector, "text": candidate}
                          for candidate in candidates]
        elif kind == "visible":
            conditions = [{"kind": "element_visible", "selector": selector, "visible": candidate}
                          for candidate in candidates]
        else:
            raise ProbeError("outcome", "unsupported_condition")
        matches = verify(client, headers, conditions)
        values.setdefault(name, []).extend(matches)

    if page["site"] == "sannysoft":
        record["outcomes"]["webdriver"] = class_text_outcome(
            values["webdriver_class"], values["webdriver_text"],
            ("missing (passed)", "present (failed)"),
            pass_value="missing (passed)", fail_value="present (failed)")
        record["outcomes"]["chrome"] = class_text_outcome(
            values["chrome_class"], values["chrome_text"],
            ("missing (failed)", "present (passed)"),
            pass_value="present (passed)", fail_value="missing (failed)")
        record["outcomes"]["permissions"] = unique_outcome(
            [candidate for candidate, matched in zip(("prompt", "granted", "denied"), values["permissions"])
             if matched], ("prompt", "granted", "denied"))
        record["outcomes"]["advanced_webdriver"] = class_text_outcome(
            values["advanced_class"], values["advanced_text"], ("passed", "failed"))
        record["outcomes"]["plugin_array"] = class_text_outcome(
            values["plugin_array_class"], values["plugin_array_text"], ("passed", "failed"))
        record["outcomes"]["plugins_length_bucket"] = plugin_length_bucket(
            values["plugins_length_bucket"][0], values["plugins_length_nonzero_bucket"][0])
        non_language_resolved = all(value != UNVERIFIED for key, value in record["outcomes"].items())
        if values["languages_present"][0]:
            record["outcomes"]["languages_presence"] = "present"
        elif values["languages_empty"][0] and non_language_resolved:
            record["outcomes"]["languages_presence"] = "empty"
        else:
            record["outcomes"]["languages_presence"] = UNVERIFIED
        record["initialization"]["status"] = (
            "inferred_from_resolved_result_cells" if non_language_resolved else UNVERIFIED)
    elif page["site"] == "browserleaks_js":
        for name in ("javascript_enabled", "inline_scripts", "same_origin_scripts", "third_party_scripts"):
            record["outcomes"][name] = boolean_class(*values[name])
    else:
        webgl = boolean_class(*values["webgl_1_support"])
        webgl2 = boolean_class(*values["webgl_2_support"])
        record["outcomes"]["webgl"] = webgl
        record["outcomes"]["webgl2"] = webgl2
        record["outcomes"]["webgl_support_class"] = (
            "webgl_and_webgl2" if (webgl, webgl2) == ("true", "true") else
            "webgl_only" if (webgl, webgl2) == ("true", "false") else
            "no_webgl_support" if (webgl, webgl2) == ("false", "false") else UNVERIFIED)
    record["verdict"] = diagnostic_verdict(
        blocked=False, expected_page_verified=page_verified,
        observations=record["outcomes"].values())
    return record


def run(output: Path, site: str = "all") -> dict:
    key_path = validate_environment()
    headers = {"Authorization": "Bearer " + read_key(key_path)}
    report: dict = {
        "schema": "readiness-diagnostic-observations/v1",
        "profile_id": PROFILE_ID,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_verdict": "OBSERVATIONS",
        "diagnostic_scope": site,
        "runtime": {"viewer_origin": "http://localhost:18081", "worker": "disposable-antibot-test",
                    "viewport": [1440, 900], "profile_reused": True, "cookies_imported": False,
                    "allowed_origins_policy": "exact page origin plus source-confirmed static origins per site",
                    "third_party_resources": "only exact Sannysoft dependencies cdnjs and jsdelivr are allowed; no analytics origin"},
        "pages": [],
        "evidence_scope": "fixed URL, label, challenge, and boolean/bucket predicates only",
        "raw_page_data_saved": False,
    }
    with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
        no_other_tasks(client)
        status = worker_status(client, headers)
        assert_worker_idle(status)
        selected_pages = PAGES if site == "all" else tuple(page for page in PAGES if page["site"] == site)
        for page in selected_pages:
            before = worker_status(client, headers)
            assert_worker_idle(before)
            navigate_once(client, headers, page, before.get("url", ""))
            entry = observe_page(client, headers, page)
            report["pages"].append(entry)
            if entry["verdict"] == "BLOCKED":
                break
    verdicts = [page["verdict"] for page in report["pages"]]
    if "BLOCKED" in verdicts:
        report["overall_verdict"] = "BLOCKED"
    elif verdicts and all(item == "DIAGNOSTIC_COMPLETED" for item in verdicts):
        report["overall_verdict"] = "DIAGNOSTIC_COMPLETED"
    else:
        report["overall_verdict"] = "OBSERVATIONS"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="perform the explicit live read-only diagnostic run")
    parser.add_argument("--i-confirm-disposable", action="store_true")
    parser.add_argument("--site", choices=("all", "sannysoft", "browserleaks_js", "browserleaks_webgl"), default="all")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.run:
        print("No action. Pass --run --i-confirm-disposable to start the bounded diagnostic sequence.")
        return 0
    if not args.i_confirm_disposable:
        raise SystemExit("explicit_disposable_confirmation_required")
    output = args.output or Path("/tmp/antibot-artifacts/diagnostics/run-3.json" if args.site == "sannysoft"
                                 else "/tmp/antibot-artifacts/diagnostics/run-1.json")
    if args.site == "sannysoft" and output.name in {"run-1.json", "run-2.json"}:
        raise SystemExit("sannysoft_corrective_run_must_not_overwrite_history")
    try:
        report = run(output, args.site)
    except ProbeError as exc:
        report = {"schema": "readiness-diagnostic-observations/v1", "overall_verdict": "OBSERVATIONS",
                  "error": {"step": exc.step, "code": exc.code}, "pages": [], "raw_page_data_saved": False}
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 1
    except Exception:
        report = {"schema": "readiness-diagnostic-observations/v1", "overall_verdict": "OBSERVATIONS",
                  "error": {"step": "runtime", "code": "request_or_run_failed"},
                  "pages": [], "raw_page_data_saved": False}
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("overall_verdict") == "DIAGNOSTIC_COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
