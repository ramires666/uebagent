"""Live generic-client smoke against localhost llama-server and a synthetic page only."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from browser_mcp.config import Settings  # noqa: E402
from browser_mcp.model_client import ModelError  # noqa: E402
from browser_mcp.runtime import TaskRunner  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


MODEL_BASE = "http://127.0.0.1:8080/v1"
MODEL_ALIAS = "auto"
FIXTURE_URL = "https://model-test.local/"
TARGET = "\u0422\u043e\u0447\u043d\u043e \u0432\u0432\u043e\u0434\u0438\u043c\u0430\u044f \u0444\u0440\u0430\u0437\u0430: \u041d\u0415 \u041e\u0422\u041f\u0420\u0410\u0412\u041b\u042f\u0422\u042c"
GOAL = (
    "On this synthetic page, open the Thinking level menu and choose High. "
    f"Then type exactly this text in the message field: {TARGET}. "
    "Do not press Enter or click Send. Stop when High is selected and the exact text is in the field."
)
MAX_ACTIONS = 8
TOTAL_SECONDS = 120.0
REQUEST_SECONDS = 35.0

FIXTURE_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Model test fixture</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fb;color:#202124;font:16px Arial,sans-serif}
header{height:64px;background:white;border-bottom:1px solid #ddd;display:flex;align-items:center;padding:0 36px;font-weight:bold}
main{width:820px;margin:64px auto;background:white;border:1px solid #ddd;border-radius:14px;padding:30px;min-height:410px}
h1{font-size:24px;margin:0 0 20px}.row{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eee;padding:20px 0}
button,input{font:16px Arial,sans-serif;border:1px solid #aaa;border-radius:8px;background:white;padding:12px 16px}
#menu{position:absolute;background:white;border:1px solid #bbb;border-radius:8px;box-shadow:0 5px 18px #0002;display:none;z-index:3}
#menu button{display:block;border:0;border-radius:0;width:100%;text-align:left}#menu button:hover{background:#eef3ff}
.composer{margin-top:46px;display:flex;gap:12px}#message{flex:1;min-width:0}#send{background:#dfe5ef;color:#344054}
#status{color:#667085;font-size:13px;margin-top:16px}
</style></head><body><header>Synthetic assistant settings</header><main>
<h1>Conversation settings</h1>
<div class="row"><span>Thinking level</span><button id="thinking" aria-haspopup="menu" aria-expanded="false">Low ▾</button></div>
<div id="menu" role="menu"><button role="menuitem" data-level="Low">Low</button><button role="menuitem" data-level="Medium">Medium</button><button role="menuitem" data-level="High">High</button></div>
<div class="composer"><input id="message" placeholder="Type a message" aria-label="Message"><button id="send" type="button">Send</button></div>
<div id="status">No message sent</div></main><script>
const t=document.querySelector('#thinking'),m=document.querySelector('#menu');
t.onclick=()=>{m.style.display=m.style.display==='block'?'none':'block';t.setAttribute('aria-expanded',m.style.display==='block')};
m.querySelectorAll('button').forEach(b=>b.onclick=()=>{t.textContent=b.dataset.level+' ▾';m.style.display='none';t.setAttribute('aria-expanded','false')});
window.__fixtureSubmits=0;document.querySelector('#send').onclick=()=>{window.__fixtureSubmits++;document.querySelector('#status').textContent='Send intercepted'};
document.querySelector('#message').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();window.__fixtureSubmits++;document.querySelector('#status').textContent='Enter intercepted'}});
</script></body></html>"""


def model_runner() -> TaskRunner:
    settings = Settings(
        model_base_url=MODEL_BASE,
        model_alias=MODEL_ALIAS,
        model_protocol="generic",
        model_key_file="",
        model_prompt_file="",
    )
    # The real runtime constructs the production ModelClient from Settings here.
    # The repository and WorkerClient are unused by this local model/synthetic-page smoke.
    return TaskRunner(settings, repository=object())


async def main_async(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = model_runner()
    model = runner.model
    started = time.monotonic()
    health = await asyncio.wait_for(model.health(), timeout=8.0)
    health_result = {
        "ready": health.get("ready") is True,
        "protocol": health.get("protocol"),
        "model_id": health.get("model_id"),
        "vision_status_before_image": health.get("vision_status"),
        "error_code": health.get("error_code"),
    }
    if not health_result["ready"]:
        result = {"success": False, "stop_reason": "model_health_failed", "health": health_result,
                  "actions": [], "wall_seconds": round(time.monotonic() - started, 3)}
        (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    action_log: list[dict[str, Any]] = []
    invalid_outputs = 0
    wrong_clicks = 0
    submit_attempts = 0
    stop_reason = "action_limit"
    final_high = False
    final_exact = False
    vision_status = "unknown"
    model.begin_task("local-generic-smoke")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = await context.new_page()

        async def synthetic_only(route: Any) -> None:
            if route.request.url == FIXTURE_URL:
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FIXTURE_HTML)
            else:
                await route.abort("blockedbyclient")

        await page.route("**/*", synthetic_only)
        await page.goto(FIXTURE_URL, wait_until="load", timeout=5000)
        await page.screenshot(path=str(output_dir / "initial.png"), full_page=False)
        deadline = started + TOTAL_SECONDS

        for step in range(1, MAX_ACTIONS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_reason = "total_deadline"
                break
            screenshot = await page.screenshot(type="png", full_page=False)
            step_started = time.monotonic()
            try:
                action = await asyncio.wait_for(
                    model.next_action(GOAL, screenshot, history=[], current_url=FIXTURE_URL),
                    timeout=min(REQUEST_SECONDS, remaining),
                )
            except asyncio.TimeoutError:
                invalid_outputs += 1
                action_log.append({"step": step, "accepted": False, "error_code": "REQUEST_TIMEOUT",
                                   "latency_seconds": round(time.monotonic() - step_started, 3)})
                stop_reason = "request_timeout"
                break
            except ModelError as exc:
                invalid_outputs += 1
                action_log.append({"step": step, "accepted": False, "error_code": exc.code,
                                   "validation_message": exc.message,
                                   "latency_seconds": round(time.monotonic() - step_started, 3)})
                stop_reason = "model_error"
                break
            except Exception as exc:
                invalid_outputs += 1
                action_log.append({"step": step, "accepted": False, "error_code": type(exc).__name__,
                                   "latency_seconds": round(time.monotonic() - step_started, 3)})
                stop_reason = "client_error"
                break

            latency = round(time.monotonic() - step_started, 3)
            kind = action.get("kind") if isinstance(action, dict) else None
            record: dict[str, Any] = {"step": step, "kind": kind, "latency_seconds": latency}
            if kind == "click":
                x = float(action["x"]) * 1440 / 1000
                y = float(action["y"]) * 900 / 1000
                # The only submit control is intercepted here and never clicked.
                hit_send = await page.evaluate("([x,y]) => Boolean(document.elementFromPoint(x,y)?.closest('#send'))", [x, y])
                if hit_send:
                    submit_attempts += 1
                    record.update({"accepted": False, "blocked": "submit_control"})
                    action_log.append(record)
                    stop_reason = "submit_attempt_blocked"
                    break
                actionable = await page.evaluate("([x,y]) => Boolean(document.elementFromPoint(x,y)?.closest('button,input'))", [x, y])
                await page.mouse.click(x, y, button=action.get("button", "left"), click_count=int(action.get("count", 1)))
                if not actionable:
                    wrong_clicks += 1
                model.record_outcome("applied")
            elif kind == "type_text":
                if action.get("text") != TARGET:
                    record.update({"accepted": False, "blocked": "literal_mismatch"})
                    action_log.append(record)
                    stop_reason = "literal_mismatch"
                    break
                focused = await page.evaluate("document.activeElement === document.querySelector('#message')")
                if not focused:
                    record.update({"accepted": False, "blocked": "editable_not_focused"})
                    action_log.append(record)
                    model.record_action_rejection("editable_not_focused")
                    continue
                await page.keyboard.insert_text(action["text"])
                model.record_verbatim_text_action()
                model.record_outcome("applied")
            elif kind == "press_key":
                key = str(action.get("key", ""))
                if "enter" in {part.casefold() for part in key.split("+")}:
                    submit_attempts += 1
                    record.update({"accepted": False, "blocked": "enter_key"})
                    action_log.append(record)
                    stop_reason = "submit_attempt_blocked"
                    break
                if not key or len(key) > 100:
                    invalid_outputs += 1
                    record.update({"accepted": False, "blocked": "invalid_key"})
                    action_log.append(record)
                    stop_reason = "invalid_action"
                    break
                await page.keyboard.press(key)
                model.record_outcome("applied")
            elif kind == "wait":
                seconds = min(2.0, max(0.0, float(action.get("seconds", 0))))
                await page.wait_for_timeout(int(seconds * 1000))
                model.record_outcome("applied")
            elif kind == "finish":
                record["accepted"] = True
                action_log.append(record)
                stop_reason = "model_finished"
                break
            else:
                record.update({"accepted": False, "blocked": "unsupported_action"})
                action_log.append(record)
                stop_reason = "unsupported_action"
                break

            record["accepted"] = True
            action_log.append(record)
            final_high = (await page.locator("#thinking").inner_text()).split()[0] == "High"
            final_exact = await page.locator("#message").input_value() == TARGET
            if final_high and final_exact:
                stop_reason = "goal_reached"
                break
            if time.monotonic() >= deadline:
                stop_reason = "total_deadline"
                break

        final_high = (await page.locator("#thinking").inner_text()).split()[0] == "High"
        final_exact = await page.locator("#message").input_value() == TARGET
        fixture_submit_count = await page.evaluate("window.__fixtureSubmits || 0")
        await page.screenshot(path=str(output_dir / "final.png"), full_page=False)
        await context.close()
        await browser.close()

    try:
        final_health = await asyncio.wait_for(model.health(), timeout=8.0)
    except Exception:
        final_health = {"vision_status": "unknown"}
    vision_status = str(final_health.get("vision_status", "unknown"))
    result = {
        "success": final_high and final_exact and submit_attempts == 0 and fixture_submit_count == 0,
        "endpoint": MODEL_BASE,
        "model_selector": MODEL_ALIAS,
        "health": health_result,
        "vision_status_after_image_request": vision_status,
        "fixture": FIXTURE_URL,
        "viewport": [1440, 900],
        "thinking_high_selected": final_high,
        "exact_input_match": final_exact,
        "submit_attempts_blocked": submit_attempts,
        "fixture_submit_count": fixture_submit_count,
        "wrong_clicks": wrong_clicks,
        "invalid_outputs": invalid_outputs,
        "steps": len(action_log),
        "max_actions": MAX_ACTIONS,
        "wall_seconds": round(time.monotonic() - started, 3),
        "total_deadline_seconds": TOTAL_SECONDS,
        "stop_reason": stop_reason,
        "actions": action_log,
        "raw_model_output_recorded": False,
        "private_browser_used": False,
        "external_network_allowed": False,
    }
    (output_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "runtime" / "model-smoke"))
    args = parser.parse_args()
    result = asyncio.run(main_async(Path(args.output).resolve()))
    # Print the metric summary only; never print model text/tool arguments or reasoning.
    summary = {key: value for key, value in result.items() if key != "actions"}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
