from __future__ import annotations

import base64
import json

import httpx

from scripts import readiness_public_web as web


def test_fixed_site_inventory_and_classification_fail_closed():
    assert [site["name"] for site in web.SITES] == ["demoqa", "amazon", "ebay", "sannysoft"]
    assert web.classify_site(navigation_ok=True, final_origin="https://www.amazon.com",
                             expected_origin="https://www.amazon.com", positive_passed=False,
                             challenge_detected=False) == "UNVERIFIED_DUE_TO_OBSTRUCTION"
    assert web.classify_site(navigation_ok=True, final_origin="https://www.amazon.com",
                             expected_origin="https://www.amazon.com", positive_passed=True,
                             challenge_detected=True) == "CHALLENGE_DETECTED"
    assert web.classify_site(navigation_ok=True, final_origin="https://other.test",
                             expected_origin="https://www.amazon.com", positive_passed=True,
                             challenge_detected=False) == "BLOCKED"
    assert web.classify_site(navigation_ok=False, final_origin=None,
                             expected_origin="https://www.amazon.com", positive_passed=True,
                             challenge_detected=False) == "UNREACHABLE"


def test_run_saves_only_pngs_and_serializes_sanitized_metadata(tmp_path):
    image = b"\x89PNG\r\n\x1a\nfixture"
    encoded = base64.b64encode(image).decode()
    state = {"worker_instance_id": "secret-worker-id", "browser_instance_id": "secret-browser-id",
             "page_id": "secret-page-id", "recovery_generation": 4, "context_count": 1,
             "page_count": 1, "control_owner": "none", "action_in_progress": False,
             "import_in_progress": False, "recovery_needed": False, "recovery_state": "idle"}
    epoch = 0
    current_url = "https://demoqa.com/text-box"
    def handler(request):
        nonlocal epoch, current_url
        path = request.url.path
        if path == "/status":
            return httpx.Response(200, json=state)
        if path == "/gate":
            owner = request.read().decode()
            if '"human"' in owner:
                epoch += 1
                return httpx.Response(200, json={"control_epoch": epoch, "control_owner": "human"})
            return httpx.Response(200, json={"control_epoch": epoch, "control_owner": "none"})
        if path == "/manual/execute":
            payload = json.loads(request.read())
            current_url = payload["action"]["url"]
            epoch += 1
            return httpx.Response(200, json={"outcome": "applied", "control_epoch": epoch,
                                             "control_owner": "human", "url": "https://public.test/?secret=query"})
        if path == "/observe":
            origin = current_url.split("/", 3)[:3]
            return httpx.Response(200, json={**state, "url": "/".join(origin) + "/private?secret=query",
                                             "screenshot_png_b64": encoded})
        if path == "/verify":
            conditions = json.loads(request.read()).get("conditions", [])
            checks = []
            for condition in conditions:
                text = condition.get("text", "")
                if "sannysoft" in current_url:
                    passed = text == "Intoli.com tests"
                else:
                    passed = bool(text) or condition.get("selector") in {"#captcha", "#captchacharacters"}
                checks.append({"passed": passed})
            return httpx.Response(200, json={"checks": checks})
        raise AssertionError(path)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        report = web.PublicWebCheck(client, "TOP_SECRET_KEY", clock=lambda: 0,
                                    sleep=lambda _: None, progress=lambda _: None,
                                    screenshot_dir=tmp_path).run()
    assert report["schema"] == web.SCHEMA
    assert report["status"] == "DIAGNOSTIC"
    assert len(report["sites"]) == 4
    assert [site["status"] for site in report["sites"]] == [
        "CHALLENGE_DETECTED", "CHALLENGE_DETECTED", "CHALLENGE_DETECTED", "PUBLIC_PAGE_AVAILABLE"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["amazon.png", "demoqa.png", "ebay.png", "sannysoft.png"]
    serialized = json.dumps(report)
    for forbidden in ("TOP_SECRET_KEY", "secret-worker-id", "secret-browser-id", "secret-page-id",
                      "private?secret=query", "public.test/private", "screenshot_png_b64"):
        assert forbidden not in serialized


def test_disposable_guard_blocks_before_any_network(tmp_path, monkeypatch, capsys):
    def reject(**kwargs):
        raise ValueError("do not expose details")
    monkeypatch.setattr(web, "validate_environment", reject)
    monkeypatch.setattr(web.httpx, "Client", lambda **kwargs: (_ for _ in ()).throw(AssertionError("network opened")))
    code = web.main(["--key-file", str(tmp_path / "key"), "--output", str(tmp_path / "report.json"),
                     "--i-confirm-disposable"])
    assert code == 2
    assert json.loads(capsys.readouterr().out) == {"status": "BLOCKED", "error_code": "DISPOSABLE_GUARD_REJECTED"}
