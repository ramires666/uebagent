"""Capture a read-only synthetic DemoQA browser frame and bounded font metadata."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("DEMOQA_PROBE_URL", "http://127.0.0.1:18080").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
LABEL = os.environ.get("DEMOQA_RUN_LABEL", "v5diag")
FRAME_PREFIX = os.environ.get("DEMOQA_FRAME_PREFIX", "frame")
COMPOSE = ROOT / "deploy" / "compose.demoqa-test.yaml"
PROJECT = "browser-mcp-demoqa-test"
ORIGIN = "https://demoqa.com"


def desktop_screenshot(client: httpx.Client, target: Path) -> int:
    deadline = time.monotonic() + 10
    last_status = None
    while time.monotonic() < deadline:
        response = client.get(BASE + "/api/v1/desktop/screenshot", timeout=4)
        if response.status_code == 200:
            data = response.content
            if len(data) < 8 or len(data) > 16 * 1024 * 1024 or data[:8] != b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("desktop screenshot was not a bounded PNG")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return len(data)
        last_status = response.status_code
        if response.status_code not in {409, 423, 503}:
            raise RuntimeError(f"desktop screenshot request returned HTTP {response.status_code}")
        time.sleep(0.4)
    raise RuntimeError(f"desktop screenshot remained busy (last HTTP status {last_status})")


def read_font_metadata() -> dict:
    code = (
        "from pathlib import Path;import httpx,json;"
        "key=Path('/run/demoqa-secrets/key').read_text().strip();"
        "r=httpx.get('http://browser:7070/diagnostics/demoqa-fonts',"
        "headers={'Authorization':'Bearer '+key},timeout=6);r.raise_for_status();"
        "print(json.dumps(r.json(),ensure_ascii=True,separators=(',',':')))"
    )
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), "exec", "-T", "control", "python", "-c", code],
        cwd=ROOT, capture_output=True, text=True, timeout=12, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("bounded font metadata request failed inside isolated control")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("font metadata response was not valid JSON") from exc


def set_font_style(variant: str) -> dict:
    if variant not in {"baseline", "arial_normal", "arial_medium", "system_normal", "system_bold", "monospace",
                       "system_thin100", "arial_thin100", "baseline_normal"}:
        raise RuntimeError("invalid font style variant")
    code = (
        "from pathlib import Path;import httpx,json,sys;"
        "key=Path('/run/demoqa-secrets/key').read_text().strip();"
        "r=httpx.post('http://browser:7070/diagnostics/demoqa-font-style',"
        "headers={'Authorization':'Bearer '+key},json={'variant':sys.argv[1]},timeout=6);"
        "r.raise_for_status();print(json.dumps(r.json(),ensure_ascii=True,separators=(',',':')))"
    )
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), "exec", "-T", "control", "python", "-c", code, variant],
        cwd=ROOT, capture_output=True, text=True, timeout=12, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("bounded font-style request failed inside isolated control")
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("font-style response was not valid JSON") from exc


def main() -> int:
    if not FRAME_PREFIX or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in FRAME_PREFIX):
        raise RuntimeError("invalid frame prefix")
    output_dir = ROOT / "runtime" / "demoqa" / LABEL
    first_png = output_dir / f"{FRAME_PREFIX}.png"
    second_png = output_dir / f"{FRAME_PREFIX}-5s.png"
    result_path = ROOT / "runtime" / "demoqa" / f"results-{LABEL}.jsonl"
    rows: list[dict] = []
    timeout = httpx.Timeout(8.0, connect=3.0)
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            session = client.get(BASE + "/api/v1/operator/session")
            session.raise_for_status()
            session_data = session.json()
            if session_data.get("authenticated") is not True or not isinstance(session_data.get("csrf_token"), str):
                raise RuntimeError("test UI operator session is unavailable")
            csrf = session_data["csrf_token"]
            headers = {"x-csrf-token": csrf}
            state = client.get(BASE + "/api/v1/desktop/state")
            state.raise_for_status()
            state_data = state.json()
            if (state_data.get("browser_started") is not True or state_data.get("control_owner") != "none"
                    or state_data.get("action_in_progress") is not False
                    or state_data.get("import_in_progress") is not False
                    or state_data.get("context_count") != 1 or state_data.get("page_count") != 1):
                raise RuntimeError("test browser is not idle and limited to one page")

            takeover = client.post(BASE + "/api/v1/profiles/default/takeover", headers=headers, json={})
            takeover.raise_for_status()
            control_epoch = takeover.json().get("control_epoch")
            if type(control_epoch) is not int:
                raise RuntimeError("test browser did not grant the manual navigation lease")
            nav = client.post(BASE + "/api/v1/desktop/action", headers=headers,
                              json={"control_epoch": control_epoch,
                                    "action": {"kind": "navigate", "url": ORIGIN + "/webtables"}})
            nav.raise_for_status()
            if nav.json().get("outcome") != "applied":
                raise RuntimeError("DemoQA page navigation was not confirmed")
            release = client.post(BASE + "/api/v1/profiles/default/release-control", headers=headers, json={})
            release.raise_for_status()

            allowed_variants = {"baseline", "arial_normal", "arial_medium", "system_normal", "system_bold", "monospace",
                                "system_thin100", "arial_thin100", "baseline_normal"}
            requested = [v for v in os.environ.get("DEMOQA_FONT_VARIANTS", "baseline,arial_normal,arial_medium,system_normal,system_bold,monospace").split(",") if v]
            if not requested or any(v not in allowed_variants for v in requested):
                raise RuntimeError("invalid font matrix variant list")
            restored = False
            try:
                for variant in requested:
                    style = set_font_style(variant)
                    time.sleep(0.3)
                    target = output_dir / f"{variant}.png"
                    size = desktop_screenshot(client, target)
                    row = {"scenario": "font_style_frame", "status": "saved", "variant": variant,
                           "path": str(target.relative_to(ROOT)), "bytes": size, "metadata": style}
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=True), flush=True)
                reset = set_font_style("baseline")
                restored = True
                reset_row = {"scenario": "font_style_restore", "status": "read_only", "metadata": reset}
                rows.append(reset_row)
                print(json.dumps(reset_row, ensure_ascii=True), flush=True)
                if requested == ["baseline"]:
                    time.sleep(4)
                    target = output_dir / "baseline-5s.png"
                    size = desktop_screenshot(client, target)
                    row = {"scenario": "font_style_frame", "status": "saved", "variant": "baseline_5s",
                           "path": str(target.relative_to(ROOT)), "bytes": size, "metadata": reset}
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=True), flush=True)
            finally:
                if not restored:
                    set_font_style("baseline")

            font_metadata = read_font_metadata()
            font_row = {"scenario": "font_probe_metadata", "status": "read_only", "metadata": font_metadata}
            rows.append(font_row)
            print(json.dumps(font_row, ensure_ascii=True), flush=True)

            # Keep the isolated viewer available for review; no model task runs in this probe.
            hold_seconds = int(os.environ.get("DEMOQA_DIAGNOSTIC_HOLD_SECONDS", "10"))
            if not 0 <= hold_seconds <= 120:
                raise RuntimeError("diagnostic hold is outside the bounded range")
            print(json.dumps({"scenario": "font_probe_viewer", "status": "live",
                              "url": "http://localhost:18080/desktop", "hold_seconds": hold_seconds}), flush=True)
            deadline = time.monotonic() + hold_seconds
            while time.monotonic() < deadline:
                time.sleep(1)
            timeout_row = {"scenario": "font_probe_review", "status": "review_timeout", "model_cases_run": False}
            rows.append(timeout_row)
            print(json.dumps(timeout_row, ensure_ascii=True), flush=True)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        failure = {"scenario": "font_probe", "status": "failed", "error_type": type(exc).__name__}
        rows.append(failure)
        print(json.dumps(failure, ensure_ascii=True), flush=True)
        if rows:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            with result_path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(json.dumps(row, ensure_ascii=True) for row in rows) + "\n")
        return 1

    result_path.parent.mkdir(parents=True, exist_ok=True)
    with result_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(json.dumps(row, ensure_ascii=True) for row in rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

