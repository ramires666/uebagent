"""Run bounded synthetic UI edge checks in an isolated Camoufox container.

The runner owns one temporary BrowserRuntime/profile, intercepts the sole
fixture URL, aborts every other page request, and never contacts a live site.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from worker.app import BrowserRuntime, ManualExecuteBody


FIXTURE_URL = "https://example.com/readiness-fixture"
FIXTURE_ORIGIN = "https://example.com"
MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
SYNTHETIC_TEXT = "Audit synthetic input"

FIXTURE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Readiness synthetic fixture</title>
<style>
  body { margin: 0; font: 16px sans-serif; }
  #late-input { position:absolute; left:40px; top:80px; width:300px; height:40px; }
  #target { position:absolute; left:40px; top:200px; width:220px; height:54px; z-index:1; }
  #overlay { position:absolute; left:40px; top:200px; width:220px; height:54px; z-index:2; background:rgba(255,0,0,.25); }
  #shadow-host { position:absolute; left:40px; top:340px; width:220px; height:50px; }
  iframe { position:absolute; left:40px; top:470px; width:300px; height:90px; border:1px solid; }
</style></head>
<body>
  <div id="delay-result">waiting</div>
  <input id="late-input" aria-label="Synthetic delayed field">
  <button id="target">Covered target</button>
  <div id="overlay">Cover</div>
  <div id="overlay-result">0</div><div id="target-result">0</div>
  <readiness-shadow id="shadow-host"></readiness-shadow>
  <div id="shadow-result">waiting</div>
  <iframe id="fixture-frame" title="Synthetic frame"
    srcdoc="<!doctype html><button id='frame-button' style='margin:15px' onclick=&quot;parent.postMessage('frame-clicked','*')&quot;>Frame button</button>"></iframe>
  <div id="frame-result">waiting</div>
  <script>
    window.overlayHits = 0; window.targetHits = 0;
    document.getElementById('overlay').addEventListener('click', () => {
      window.overlayHits++; document.getElementById('overlay-result').textContent = String(window.overlayHits);
    });
    document.getElementById('target').addEventListener('click', () => {
      window.targetHits++; document.getElementById('target-result').textContent = String(window.targetHits);
    });
    document.getElementById('late-input').id = 'temporary-id';
    setTimeout(() => { document.getElementById('delay-result').textContent = 'delay-ready'; }, 120);
    setTimeout(() => { document.getElementById('temporary-id').id = 'resolved-input'; }, 220);
    customElements.define('readiness-shadow', class extends HTMLElement {
      connectedCallback() {
        const root = this.attachShadow({mode:'open'});
        const button = document.createElement('button'); button.textContent = 'Shadow button';
        button.onclick = () => { document.getElementById('shadow-result').textContent = 'shadow-clicked'; };
        root.append(button);
      }
    });
    window.addEventListener('message', event => {
      if (event.data === 'frame-clicked') document.getElementById('frame-result').textContent = 'frame-clicked';
    });
  </script>
</body></html>"""


def _check(status: str, expected: str, actual: object) -> dict:
    return {"status": status, "expected": expected, "actual": actual}


def _center(box: dict | None) -> tuple[float, float]:
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        raise RuntimeError("synthetic target has no visible bounding box")
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


async def run_audit() -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    started_clock = time.monotonic()
    results: dict[str, dict] = {}
    network = {"fixture_requests": 0, "blocked_requests": 0}
    runtime = None
    with tempfile.TemporaryDirectory(prefix="readiness-browser-audit-") as temporary:
        runtime = BrowserRuntime(
            temporary,
            backend="camoufox",
            headless=False,
            egress_ready=True,
            route_generation=1,
            proxy_assignment_id="readiness-synthetic-only",
        )
        try:
            await asyncio.wait_for(runtime.start(), timeout=90)
            page = runtime._page

            async def intercept(route) -> None:
                if route.request.url == FIXTURE_URL:
                    network["fixture_requests"] += 1
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE_HTML)
                else:
                    network["blocked_requests"] += 1
                    await route.abort("blockedbyclient")

            # Page-level routing takes precedence over the runtime context route:
            # only the in-memory fixture is fulfilled; all other requests abort.
            await page.route("**/*", intercept)
            control = runtime.gate("human")
            navigation = await asyncio.wait_for(runtime.execute_manual(ManualExecuteBody(
                control_epoch=control["control_epoch"],
                allowed_origins=[FIXTURE_ORIGIN],
                action={"kind": "navigate", "url": FIXTURE_URL},
            )), timeout=10)
            navigation_ok = (navigation.get("outcome") == "applied"
                             and network["fixture_requests"] == 1
                             and await page.title() == "Readiness synthetic fixture")
            results["fixture_navigation"] = _check(
                "PASS" if navigation_ok else "FAIL",
                "synthetic fixture served through worker navigation; no external request",
                {"outcome": navigation.get("outcome"), "fixture_requests": network["fixture_requests"]},
            )
            epoch = navigation.get("control_epoch", runtime.control_epoch)
            observation = await runtime.observe()
            png = base64.b64decode(observation["screenshot_png_b64"], validate=True)
            if len(png) < 24 or len(png) > MAX_SCREENSHOT_BYTES or not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("worker observation was not a bounded PNG")
            width, height = struct.unpack(">II", png[16:24])
            initial_one_page = observation.get("context_count") == 1 and observation.get("page_count") == 1
            if (width, height) != (1440, 900) or not initial_one_page:
                raise RuntimeError("synthetic observation did not use the expected 1440x900 viewport")
            results["initial_observation"] = _check(
                "PASS" if initial_one_page else "FAIL",
                "one context/page and 1440x900 PNG observation",
                {"width": width, "height": height, "bytes": len(png),
                 "context_count": observation.get("context_count"), "page_count": observation.get("page_count")},
            )

            async def act(action: dict) -> dict:
                nonlocal epoch
                response = await asyncio.wait_for(runtime.execute_manual(ManualExecuteBody(
                    control_epoch=epoch, allowed_origins=[FIXTURE_ORIGIN], action=action,
                )), timeout=10)
                epoch = response.get("control_epoch", runtime.control_epoch)
                return response

            async def click_locator(locator) -> dict:
                # Use a fresh worker observation before translating a visible
                # synthetic target to coordinates for the actual worker action.
                await runtime.observe()
                x, y = _center(await locator.bounding_box())
                return await act({"kind": "click", "x": x, "y": y})

            # Delayed content and dynamic ID are verifier-only checks first.
            await page.wait_for_function(
                "() => document.querySelector('#delay-result')?.textContent === 'delay-ready'",
                timeout=2000,
            )
            delayed = await runtime.verify([{"kind": "element_text_contains", "selector": "#delay-result", "text": "delay-ready"}])
            results["delayed_render"] = _check(
                "PASS" if delayed.get("verified") else "FAIL",
                "delayed element appears and worker verifier confirms it",
                {"verified": delayed.get("verified") is True},
            )
            await page.wait_for_selector("#resolved-input", timeout=2000)
            old_id = await runtime.verify([{"kind": "element_visible", "selector": "#temporary-id", "visible": False}])
            new_id = await runtime.verify([{"kind": "element_visible", "selector": "#resolved-input", "visible": True}])
            results["dynamic_id"] = _check(
                "PASS" if old_id.get("verified") and new_id.get("verified") else "FAIL",
                "old id disappears and replacement id is visible",
                {"old_id_gone": old_id.get("verified") is True, "new_id_visible": new_id.get("verified") is True},
            )

            typed_click = await click_locator(page.locator("#resolved-input"))
            typed = await act({"kind": "type_text", "text": SYNTHETIC_TEXT})
            field = await runtime.verify([{"kind": "field_equals", "selector": "#resolved-input", "value": SYNTHETIC_TEXT}])
            typed_ok = typed_click.get("outcome") == "applied" and typed.get("outcome") == "applied" and field.get("verified") is True
            results["synthetic_type"] = _check(
                "PASS" if typed_ok else "FAIL",
                "real worker click/type actions place exact synthetic text in the replacement field",
                {"click_outcome": typed_click.get("outcome"), "type_outcome": typed.get("outcome"),
                 "verified": field.get("verified") is True, "text_length": len(SYNTHETIC_TEXT)},
            )

            overlay_click = await click_locator(page.locator("#target"))
            overlay_checks = await runtime.verify([
                {"kind": "element_text_contains", "selector": "#overlay-result", "text": "1"},
                {"kind": "element_text_contains", "selector": "#target-result", "text": "0"},
            ])
            overlay_expected = overlay_click.get("outcome") == "applied" and overlay_checks.get("verified") is True
            results["overlay_interception"] = _check(
                "BLOCKED" if overlay_expected else "FAIL",
                "independent verifier detects that overlay intercepted the coordinate click",
                {"worker_receipt": overlay_click.get("outcome"),
                 "overlay_caught_click": overlay_checks.get("verified") is True,
                 "target_activated": False if overlay_expected else None},
            )

            shadow_click = await click_locator(page.locator("readiness-shadow").locator("button"))
            shadow_check = await runtime.verify([
                {"kind": "element_text_contains", "selector": "#shadow-result", "text": "shadow-clicked"},
            ])
            shadow_ok = shadow_click.get("outcome") == "applied" and shadow_check.get("verified") is True
            results["shadow_dom"] = _check(
                "PASS" if shadow_ok else "FAIL",
                "coordinate action reaches open shadow button and verifier sees resulting state",
                {"worker_receipt": shadow_click.get("outcome"), "verified": shadow_check.get("verified") is True},
            )

            frame_button = page.frame_locator("#fixture-frame").locator("#frame-button")
            frame_click = await click_locator(frame_button)
            frame_check = await runtime.verify([
                {"kind": "element_text_contains", "selector": "#frame-result", "text": "frame-clicked"},
            ])
            frame_ok = frame_click.get("outcome") == "applied" and frame_check.get("verified") is True
            results["iframe"] = _check(
                "PASS" if frame_ok else "FAIL",
                "coordinate action reaches synthetic iframe and parent receives verified message",
                {"worker_receipt": frame_click.get("outcome"), "verified": frame_check.get("verified") is True},
            )

            # The current worker action schema has no native dialog mediation or
            # file-upload action. Report these capability boundaries explicitly.
            results["native_dialog"] = _check(
                "BLOCKED", "dialog must be mediated or explicitly unsupported",
                {"reason": "worker exposes no dialog accept/dismiss operation; fixture not triggered"},
            )
            results["file_upload"] = _check(
                "BLOCKED", "file upload must have an explicit bounded capability",
                {"reason": "worker action schema has no file-upload operation; fixture not triggered"},
            )
            results["task_cancel_idempotency"] = _check(
                "BLOCKED", "repeated cancel on an isolated TaskRunner task has one stable terminal outcome",
                {"reason": "this worker-only harness does not instantiate TaskRunner/model or create tasks"},
            )

            final = runtime.status_base()
            health = await runtime.health()
            one_page = (final.get("context_count") == 1 and final.get("page_count") == 1
                        and final.get("resource_violation") is False and health.get("ready") is True)
            results["final_resources"] = _check(
                "PASS" if one_page else "FAIL",
                "one context/page and no resource violation",
                {"context_count": final.get("context_count"), "page_count": final.get("page_count"),
                 "resource_violation": final.get("resource_violation"),
                 "runtime_ready_under_synthetic_route_flag": health.get("ready") is True},
            )
        finally:
            await runtime.close()

    statuses = [entry["status"] for entry in results.values()]
    overall = "FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "PASS"
    return {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "started_at_utc": started_at,
        "duration_seconds": round(time.monotonic() - started_clock, 3),
        "overall_status": overall,
        "scenario_counts": {status.lower(): statuses.count(status) for status in ("PASS", "FAIL", "BLOCKED")},
        "scenarios": results,
        "network": {"policy": "fixture-only fulfill; abort all other page requests", **network},
        "limitations": [
            "This audit uses a synthetic temporary profile and does not test real websites or egress.",
            "Native dialogs and file upload are recorded BLOCKED, not passed.",
            "Queued/in-flight cancellation and browser restart/crash recovery belong to the existing runtime/recovery suites; repeated live cancel idempotency is not verified by this worker-only audit.",
        ],
    }


def _guard() -> bool:
    return (
        Path("/.dockerenv").is_file()
        and os.environ.get("APP_ENV") == "test"
        and os.environ.get("READINESS_DISPOSABLE") == "1"
        and os.environ.get("READINESS_BROWSER_AUDIT") == "1"
        and os.environ.get("READINESS_NETWORK_DISABLED") == "1"
    )


async def _main(output: Path, *, stdout_report: bool = False) -> int:
    report = None
    if not _guard():
        report = {"schema_version": 1, "overall_status": "BLOCKED",
                  "error_code": "ISOLATED_RUNTIME_GUARD"}
    else:
        try:
            report = await run_audit()
        except Exception as exc:
            # Exception type only; never serialize page text, URLs, or runtime values.
            report = {
                "schema_version": 1,
                "run_id": str(uuid.uuid4()),
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "overall_status": "FAIL",
                "error_type": type(exc).__name__,
                "limitations": ["Audit aborted; no exception message or browser content was recorded."],
            }
    report["report_path"] = str(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    output.write_text(serialized + "\n", encoding="utf-8")
    if stdout_report:
        print(serialized)
    else:
        summary = {"overall_status": report["overall_status"], "report": str(output)}
        summary.update(report.get("scenario_counts", {}))
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["overall_status"] == "PASS" else 1 if report["overall_status"] == "FAIL" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("readiness-browser-audit.json"))
    parser.add_argument("--stdout-report", action="store_true",
                        help="print the full sanitized report JSON instead of a compact summary")
    args = parser.parse_args()
    return asyncio.run(_main(args.output, stdout_report=args.stdout_report))


if __name__ == "__main__":
    raise SystemExit(main())
