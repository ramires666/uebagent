"""Stepwise read-only Amazon/eBay guest paths; never submits account or purchase forms."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SITES = {
    "amazon": {
        "origin": "https://www.amazon.com", "home": "https://www.amazon.com/",
        "search": "https://www.amazon.com/s?k=notebook", "term": "notebook",
        "guest_markers": ["Hello, sign in", "Account & Lists", "Sign in"],
        "result_markers": ["notebook", "Results"], "product_path": "/dp/",
    },
    "ebay": {
        "origin": "https://www.ebay.com", "home": "https://www.ebay.com/",
        "search": "https://www.ebay.com/sch/i.html?_nkw=notebook", "term": "notebook",
        "guest_markers": ["Sign in or register"],
        "result_markers": ["notebook", "results"], "product_path": "/itm/",
    },
}
CHALLENGES = ["Verify you are human", "Access denied", "Robot Check",
              "unusual traffic", "Request blocked", "Security Measure",
              "Click the button below to continue shopping"]
WORKER = os.environ.get("BROWSER_WORKER_URL", "http://browser:7070")
KEY = Path(os.environ["WORKER_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
HEADERS = {"Authorization": "Bearer " + KEY}


def check(client: httpx.Client, site: dict, expected_url: str, labels: list[str]):
    conditions = ([{"kind": "url_is", "url": expected_url}]
                  + [{"kind": "text_present", "text": x} for x in labels + CHALLENGES])
    r = client.post(WORKER + "/verify", headers=HEADERS, json={"conditions": conditions})
    r.raise_for_status()
    items = r.json().get("checks")
    if not isinstance(items, list) or len(items) != len(conditions):
        raise RuntimeError("verification_shape_invalid")
    passed = [x.get("passed") is True for x in items if isinstance(x, dict)]
    if len(passed) != len(conditions):
        raise RuntimeError("verification_result_invalid")
    return passed[0], {label: passed[index + 1] for index, label in enumerate(labels)}, any(passed[1 + len(labels):])


def current(client: httpx.Client) -> dict:
    r = client.get(WORKER + "/status", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def navigate(client: httpx.Client, site: dict, epoch: int, url: str) -> int:
    r = client.post(WORKER + "/manual/execute", headers=HEADERS, json={
        "control_epoch": epoch, "allowed_origins": [site["origin"]],
        "allow_cross_origin_navigation": True, "action": {"kind": "navigate", "url": url},
    })
    r.raise_for_status()
    result = r.json()
    if result.get("outcome") != "applied" or type(result.get("control_epoch")) is not int:
        raise RuntimeError("navigation_not_confirmed")
    return result["control_epoch"]


def main() -> int:
    if os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1":
        raise SystemExit("disposable_runtime_markers_required")
    if WORKER != "http://browser:7070" or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081":
        raise SystemExit("disposable_runtime_endpoints_required")
    site_key = os.environ.get("ANTIBOT_GUEST_SITE", "")
    stage = os.environ.get("ANTIBOT_GUEST_STAGE", "home")
    profile_id = os.environ.get("ANTIBOT_PROFILE", "p1")
    if site_key not in SITES or stage not in {"home", "search", "product"} or profile_id not in {"p1", "p2"}:
        raise SystemExit("site_or_stage_not_allowlisted")
    site = SITES[site_key]
    with httpx.Client(timeout=20, trust_env=False) as client:
        before = current(client)
        if (before.get("control_owner") != "none" or before.get("width") != 1440
                or before.get("height") != 900
                or before.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
                or before.get("route_generation") != 1):
            raise SystemExit("guest_flow_precondition_failed")
        observed = client.post(WORKER + "/observe", headers=HEADERS, json={})
        observed.raise_for_status()
        obs = observed.json()
        if obs.get("context_count") != 1 or obs.get("page_count") != 1 or obs.get("control_owner") != "none":
            raise SystemExit("single_context_page_precondition_failed")
        artifact = Path("/tmp/antibot-artifacts") / site_key / profile_id
        artifact.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = artifact / "guest-flow.json"
        saved = json.loads(path.read_text()) if path.exists() else {}
        prior = saved.get("steps", saved) if isinstance(saved, dict) else {}
        if stage == "home":
            start_url = before.get("url")
            start_origin = (urlsplit(start_url).scheme + "://" + urlsplit(start_url).netloc
                            if isinstance(start_url, str) and urlsplit(start_url).scheme else None)
            if start_url not in {None, "about:blank"} and start_origin not in {
                    "https://www.amazon.com", "https://www.ebay.com", "https://demoqa.com",
                    "https://browserleaks.com", "https://coveryourtracks.eff.org", "https://bot.sannysoft.com"}:
                raise SystemExit("guest_home_stage_unrecognized_initial_origin")
            gate = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "human"})
            gate.raise_for_status()
            epoch = gate.json().get("control_epoch")
            if type(epoch) is not int:
                raise SystemExit("human_gate_not_acquired")
            try:
                epoch = navigate(client, site, epoch, site["home"])
                time.sleep(5)
                url_ok, guest_markers, challenge = check(client, site, site["home"], site["guest_markers"])
            finally:
                release = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "none"})
                release.raise_for_status()
                if release.json().get("control_owner") != "none":
                    raise SystemExit("control_gate_release_failed")
            guest_visible = any(guest_markers.values())
            verdict = "BLOCKED" if challenge else ("PARTIAL" if not (url_ok and guest_visible) else "PAGE_VERIFIED")
            prior = {"site": site_key, "profile_id": profile_id, "site_authentication_checked": {
                "fresh_profile": True, "cookies_imported": False, "visible_signed_out_marker": guest_markers,
                "visible_guest_confirmed": guest_visible}, "home_url_verified": url_ok,
                "signed_out_confirmed_before_search": url_ok and guest_visible,
                "search_url": site["search"], "term": site["term"], "search_url_verified": False,
                "result_markers": {}, "product_detail_verified": False,
                "history_back_verified": False, "challenge_detected": challenge,
                "verdict": verdict}
        elif stage == "search":
            if (before.get("url") != site["home"] or not prior.get("site_authentication_checked", {}).get("visible_guest_confirmed")
                    or prior.get("challenge_detected")):
                raise SystemExit("guest_marker_or_home_verification_required")
            gate = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "human"})
            gate.raise_for_status()
            epoch = gate.json().get("control_epoch")
            try:
                epoch = navigate(client, site, epoch, site["search"])
                time.sleep(5)
                search_ok, result_markers, challenge = check(client, site, site["search"], site["result_markers"])
            finally:
                release = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "none"})
                release.raise_for_status()
                if release.json().get("control_owner") != "none":
                    raise SystemExit("control_gate_release_failed")
            prior["search_url_verified"] = search_ok
            prior["result_markers"] = result_markers
            prior["challenge_detected"] = challenge
            prior["verdict"] = "BLOCKED" if challenge else ("PARTIAL" if not (search_ok and all(result_markers.values())) else "SEARCH_VERIFIED")
        else:
            prior.setdefault("signed_out_confirmed_before_search", bool(
                prior.get("home_url_verified")
                and prior.get("site_authentication_checked", {}).get("visible_guest_confirmed")))
            if (before.get("url") != site["search"] or not prior.get("search_url_verified")
                    or prior.get("challenge_detected")):
                raise SystemExit("search_verification_required")
            # The current result card was visually inspected in the one-time screenshot.
            if site_key != "ebay":
                raise SystemExit("product_coordinate_not_confirmed_for_site")
            guest_check = client.post(WORKER + "/verify", headers=HEADERS, json={"conditions": [
                {"kind": "text_present", "text": "Sign in or register"},
                {"kind": "text_present", "text": "Verify you are human"},
                {"kind": "text_present", "text": "Access denied"},
                {"kind": "text_present", "text": "Robot Check"},
                {"kind": "text_present", "text": "unusual traffic"},
            ]})
            guest_check.raise_for_status()
            checks = guest_check.json().get("checks")
            if not isinstance(checks, list) or len(checks) != 5:
                raise SystemExit("guest_marker_verification_shape_invalid")
            flags = [x.get("passed") is True for x in checks if isinstance(x, dict)]
            if len(flags) != 5:
                raise SystemExit("guest_marker_verification_invalid")
            if not flags[0]:
                prior["verdict"] = "PARTIAL"
                prior["product_detail_verified"] = False
                prior["guest_confirmation_after_search"] = False
            elif any(flags[1:]):
                prior["challenge_detected"] = True
                prior["verdict"] = "BLOCKED"
            else:
                product_title = os.environ.get("ANTIBOT_PRODUCT_TITLE", "")
                try:
                    click_x = int(os.environ.get("ANTIBOT_PRODUCT_X", "-1"))
                    click_y = int(os.environ.get("ANTIBOT_PRODUCT_Y", "-1"))
                except ValueError as exc:
                    raise SystemExit("product_coordinate_invalid") from exc
                if not product_title or not 0 <= click_x < 1440 or not 0 <= click_y < 900:
                    raise SystemExit("visual_product_title_and_coordinates_required")
                gate = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "human"})
                gate.raise_for_status()
                epoch = gate.json().get("control_epoch")
                if type(epoch) is not int:
                    raise SystemExit("human_gate_not_acquired")
                try:
                    click = client.post(WORKER + "/manual/execute", headers=HEADERS, json={
                        "control_epoch": epoch, "allowed_origins": [site["origin"]],
                        "allow_cross_origin_navigation": False,
                        "action": {"kind": "click", "x": click_x, "y": click_y},
                    })
                    click.raise_for_status()
                    receipt = click.json()
                    if receipt.get("outcome") != "applied":
                        raise RuntimeError("product_card_click_not_confirmed")
                    time.sleep(5)
                    product_observation = client.post(WORKER + "/observe", headers=HEADERS, json={})
                    product_observation.raise_for_status()
                    product_state = product_observation.json()
                    if (product_state.get("context_count") != 1 or product_state.get("page_count") != 1):
                        raise RuntimeError("single_context_page_invariant_failed_after_product")
                    product_url = current(client).get("url", "")
                    parsed = urlsplit(product_url)
                    product_conditions = [{"kind": "text_present", "text": product_title}] + [
                        {"kind": "text_present", "text": text} for text in CHALLENGES]
                    product_check = client.post(WORKER + "/verify", headers=HEADERS,
                                                json={"conditions": product_conditions})
                    product_check.raise_for_status()
                    product_items = product_check.json().get("checks")
                    if not isinstance(product_items, list) or len(product_items) != len(product_conditions):
                        raise RuntimeError("product_verification_shape_invalid")
                    product_flags = [x.get("passed") is True for x in product_items if isinstance(x, dict)]
                    if len(product_flags) != len(product_conditions):
                        raise RuntimeError("product_verification_invalid")
                    challenge = any(product_flags[1:])
                    path_ok = parsed.scheme == "https" and parsed.hostname == "www.ebay.com" and "/itm/" in parsed.path
                    prior["product_path"] = "/itm/[public id omitted]" if "/itm/" in parsed.path else "other"
                    prior["product_title_verified"] = product_flags[0]
                    prior["guest_confirmation_after_search"] = True
                    if challenge:
                        prior["challenge_detected"] = True
                        prior["verdict"] = "BLOCKED"
                    elif path_ok and product_flags[0]:
                        epoch = receipt["control_epoch"]
                        back = client.post(WORKER + "/manual/execute", headers=HEADERS, json={
                            "control_epoch": epoch, "allowed_origins": [site["origin"]],
                            "action": {"kind": "history_back"},
                        })
                        back.raise_for_status()
                        back_receipt = back.json()
                        if back_receipt.get("outcome") != "applied":
                            raise RuntimeError("history_back_not_confirmed")
                        scroll = client.post(WORKER + "/manual/execute", headers=HEADERS, json={
                            "control_epoch": back_receipt["control_epoch"],
                            "allowed_origins": [site["origin"]],
                            "action": {"kind": "scroll", "dx": 0, "dy": 460},
                        })
                        scroll.raise_for_status()
                        scroll_receipt = scroll.json()
                        time.sleep(3)
                        final_url = current(client).get("url")
                        prior["product_detail_verified"] = True
                        prior["history_back_verified"] = final_url == site["search"]
                        prior["scroll_action_applied"] = scroll_receipt.get("outcome") == "applied"
                        prior["verdict"] = ("PASS" if prior.get("signed_out_confirmed_before_search")
                                             and prior.get("home_url_verified")
                                             and prior.get("search_url_verified")
                                             and all(prior.get("result_markers", {}).values())
                                             and prior.get("product_detail_verified")
                                             and prior.get("product_title_verified")
                                             and prior.get("history_back_verified")
                                             and prior.get("scroll_action_applied")
                                             and not prior.get("challenge_detected") else "PARTIAL")
                    else:
                        prior["verdict"] = "PARTIAL"
                        prior["product_detail_verified"] = False
                finally:
                    release = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "none"})
                    release.raise_for_status()
                    if release.json().get("control_owner") != "none":
                        raise SystemExit("control_gate_release_failed")
        report = {"schema": "antibot-guest-flow/v1", "run": 1,
                  "observed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "runtime": {"browser_backend": before.get("backend"),
                        "application_image": os.environ.get("ANTIBOT_CONTROL_IMAGE", "see profile provenance"),
                        "viewport": [1440, 900],
                              "network_route": "antibot-2026-10-31-direct", "route_generation": 1},
                  "steps": prior,
                  "evidence_scope": "public guest markers and fixed URL/text booleans; no screenshot/cookies/raw page data"}
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
