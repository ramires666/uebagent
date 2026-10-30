"""Automated ordinary headed Edge baseline for public DemoQA navigation.

Default mode checks local prerequisites only. Public navigation requires the explicit
--run-public-demoqa flag. The runner never attaches to or reads a user's browser profile.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

EDGE_PATH = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
BASELINE_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "remediation-2026-10-31" / "baseline"
HOME_URL = "https://demoqa.com/"
ELEMENTS_URL = "https://demoqa.com/elements"
DEADLINE_SECONDS = 110
CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "security check",
)
ELEMENTS_CONTENT_MARKERS = ("Elements", "Text Box", "Check Box", "Web Tables")


class BaselineStop(Exception):
    def __init__(self, status: str, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _remaining_ms(started: float, cap_ms: int) -> int:
    remaining = int((DEADLINE_SECONDS - (time.monotonic() - started)) * 1000)
    if remaining <= 0:
        raise BaselineStop("FAIL", "deadline_exceeded")
    return min(cap_ms, remaining)


def _write_receipt(run_dir: Path, receipt: dict[str, object]) -> None:
    path = run_dir / "metadata.json"
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    receipt["metadata_path"] = str(path)


def _file_version(binary: Path) -> str:
    class FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.c_uint32),
            ("struct_version", ctypes.c_uint32),
            ("file_version_ms", ctypes.c_uint32),
            ("file_version_ls", ctypes.c_uint32),
            ("product_version_ms", ctypes.c_uint32),
            ("product_version_ls", ctypes.c_uint32),
            ("file_flags_mask", ctypes.c_uint32),
            ("file_flags", ctypes.c_uint32),
            ("file_os", ctypes.c_uint32),
            ("file_type", ctypes.c_uint32),
            ("file_subtype", ctypes.c_uint32),
            ("file_date_ms", ctypes.c_uint32),
            ("file_date_ls", ctypes.c_uint32),
        ]

    version_api = ctypes.windll.version
    ignored_handle = ctypes.c_uint32(0)
    size = version_api.GetFileVersionInfoSizeW(str(binary), ctypes.byref(ignored_handle))
    if not size:
        return "unavailable"
    buffer = ctypes.create_string_buffer(size)
    if not version_api.GetFileVersionInfoW(str(binary), 0, size, buffer):
        return "unavailable"
    value = ctypes.c_void_p()
    length = ctypes.c_uint32()
    if not version_api.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
        return "unavailable"
    info = ctypes.cast(value, ctypes.POINTER(FixedFileInfo)).contents
    return ".".join(str(part) for part in (
        info.file_version_ms >> 16,
        info.file_version_ms & 0xFFFF,
        info.file_version_ls >> 16,
        info.file_version_ls & 0xFFFF,
    ))


def run_public_demoqa() -> dict[str, object]:
    if not EDGE_PATH.is_file():
        raise RuntimeError("edge_executable_missing")

    from playwright.sync_api import sync_playwright

    BASELINE_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    run_dir = BASELINE_ROOT / run_id
    run_dir.mkdir()
    profile_dir = run_dir / "profile"
    profile_dir.mkdir()
    if any(profile_dir.iterdir()):
        raise RuntimeError("profile_not_empty")

    receipt: dict[str, object] = {
        "status": "running",
        "run_id": run_id,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "browser": "Microsoft Edge",
        "browser_executable": str(EDGE_PATH),
        "browser_version": "pending_context_launch",
        "profile_path": str(profile_dir),
        "profile_created_by_runner": True,
        "profile_initially_empty": True,
        "profile_source": "new unique empty workspace runtime directory; no existing profile attached",
        "control_mode": "automated ordinary headed",
        "human_manual": False,
        "user_profile_or_cookies_read_or_imported": False,
        "context_count": 1,
        "page_count": 1,
        "functional_steps_verified": False,
        "dwell_seconds_per_page": 5,
        "steps": [],
    }
    started = time.monotonic()
    phase = "launch"
    context = None
    playwright_manager = None
    playwright_instance = None
    popup_seen = False

    try:
        playwright_manager = sync_playwright()
        playwright_instance = playwright_manager.start()
        context = playwright_instance.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=str(EDGE_PATH),
            headless=False,
            viewport={"width": 1440, "height": 900},
            timeout=_remaining_ms(started, 20_000),
            args=["--no-first-run", "--disable-sync"],
        )
        initial_page_count = len(context.pages)
        receipt["page_count_before_setup"] = initial_page_count
        if initial_page_count > 1:
            raise BaselineStop("FAIL", "unexpected_initial_page_count")
        page = context.pages[0] if initial_page_count == 1 else context.new_page()
        receipt["page_count"] = len(context.pages)
        receipt["browser_version"] = context.browser.version if context.browser else _file_version(EDGE_PATH)

        def on_new_page(new_page) -> None:
            nonlocal popup_seen
            if new_page != page:
                popup_seen = True
                try:
                    new_page.close()
                except Exception:
                    pass

        context.on("page", on_new_page)
        page.set_default_timeout(_remaining_ms(started, 12_000))

        def guard() -> None:
            if popup_seen:
                raise BaselineStop("BLOCKED", "unexpected_owned_popup")
            body = page.locator("body").inner_text(timeout=_remaining_ms(started, 2_000)).casefold()
            if any(marker in body for marker in CHALLENGE_MARKERS):
                raise BaselineStop("BLOCKED", "challenge_or_blocker_marker")

        def record_step(step: dict[str, object]) -> None:
            steps = receipt["steps"]
            assert isinstance(steps, list)
            steps.append(step)

        phase = "navigate_home"
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=_remaining_ms(started, 25_000))
        if page.url != HOME_URL:
            raise BaselineStop("FAIL", "unexpected_home_url")
        guard()
        home_elements = page.get_by_role("heading", name="Elements", exact=True)
        home_elements.wait_for(state="visible", timeout=_remaining_ms(started, 12_000))
        record_step({"action": "navigate", "url": page.url, "home_elements_card_visible": True, "read_only": True})
        time.sleep(min(5, _remaining_ms(started, 6_000) / 1000))

        phase = "click_elements"
        guard()
        home_elements.click(timeout=_remaining_ms(started, 12_000))
        page.wait_for_url(ELEMENTS_URL, timeout=_remaining_ms(started, 15_000))
        if popup_seen or len(context.pages) != 1:
            raise BaselineStop("BLOCKED", "unexpected_owned_popup")
        guard()
        body_text = page.locator("body").inner_text(timeout=_remaining_ms(started, 5_000))
        if not all(marker in body_text for marker in ELEMENTS_CONTENT_MARKERS):
            raise BaselineStop("FAIL", "elements_content_incomplete")
        record_step({
            "action": "click",
            "url": page.url,
            "elements_content_markers_present": list(ELEMENTS_CONTENT_MARKERS),
            "read_only": True,
        })
        time.sleep(min(5, _remaining_ms(started, 6_000) / 1000))

        phase = "history_back"
        guard()
        page.go_back(wait_until="domcontentloaded", timeout=_remaining_ms(started, 15_000))
        page.wait_for_url(HOME_URL, timeout=_remaining_ms(started, 12_000))
        if popup_seen or len(context.pages) != 1:
            raise BaselineStop("BLOCKED", "unexpected_owned_popup")
        guard()
        page.get_by_role("heading", name="Elements", exact=True).wait_for(
            state="visible", timeout=_remaining_ms(started, 8_000)
        )
        record_step({"action": "history_back", "url": page.url, "home_elements_card_visible": True, "read_only": True})
        receipt["functional_steps_verified"] = True
        receipt["status"] = "PASS"
    except BaselineStop as exc:
        receipt["status"] = exc.status
        receipt["failure_code"] = exc.code
        receipt["failure_phase"] = phase
    except Exception as exc:
        receipt["status"] = "FAIL"
        receipt["failure_code"] = "runner_error"
        receipt["failure_phase"] = phase
        receipt["error_type"] = type(exc).__name__
    finally:
        if context is not None:
            try:
                receipt["page_count_at_close"] = len(context.pages)
                context.close()
                receipt["owned_context_closed"] = True
            except Exception:
                receipt["owned_context_closed"] = False
                if receipt["status"] == "PASS":
                    receipt["status"] = "FAIL"
                    receipt["failure_code"] = "owned_context_close_failed"
                    receipt["failure_phase"] = "cleanup"
        if playwright_manager is not None:
            try:
                if playwright_instance is not None:
                    playwright_instance.stop()
            except Exception:
                if receipt["status"] == "PASS":
                    receipt["status"] = "FAIL"
                    receipt["failure_code"] = "playwright_stop_failed"
                    receipt["failure_phase"] = "cleanup"
        receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        receipt["metadata_path"] = str(run_dir / "metadata.json")
        _write_receipt(run_dir, receipt)

    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-public-demoqa",
        action="store_true",
        help="Launch a fresh visible Edge profile and navigate the public read-only DemoQA path.",
    )
    args = parser.parse_args()

    if not args.run_public_demoqa:
        print(json.dumps({
            "status": "NOT_RUN",
            "message": "No browser launched. Pass --run-public-demoqa to run the public baseline.",
            "edge_executable_exists": EDGE_PATH.is_file(),
            "edge_executable": str(EDGE_PATH),
            "control_mode_if_run": "automated ordinary headed",
            "human_manual": False,
        }, indent=2))
        return 0

    receipt = run_public_demoqa()
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
