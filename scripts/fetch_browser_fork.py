#!/usr/bin/env python3
"""Fetch the one pinned browser-use-camoufox adapter file and its license."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "config" / "artifacts.lock.json"
DEST_ROOT = ROOT / "vendor" / "browser-use-camoufox"


def run_git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def main() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    artifact = lock["browser_fork"]
    commit = artifact["commit"]
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise SystemExit("Lock file contains an invalid full commit SHA.")

    with tempfile.TemporaryDirectory(prefix="browser-fork-") as temp_name:
        checkout = Path(temp_name) / "source"
        checkout.mkdir()
        run_git("init", str(checkout))
        run_git("remote", "add", "origin", artifact["repository"], cwd=checkout)
        run_git("fetch", "--depth=1", "origin", commit, cwd=checkout)
        run_git("checkout", "--detach", "FETCH_HEAD", cwd=checkout)
        actual_commit = run_git("rev-parse", "HEAD", cwd=checkout)
        if actual_commit != commit:
            raise SystemExit(f"Fetched {actual_commit}; expected {commit}.")

        adapter = checkout / artifact["adapter_path"]
        license_file = checkout / artifact["license_path"]
        if not adapter.is_file() or not license_file.is_file():
            raise SystemExit("Pinned checkout is missing the adapter or license file.")
        digest = hashlib.sha256(adapter.read_bytes()).hexdigest()
        if digest != artifact["adapter_sha256"]:
            raise SystemExit(f"Adapter SHA256 mismatch: got {digest}.")

        adapter_dest = DEST_ROOT / artifact["adapter_path"]
        license_dest = DEST_ROOT / artifact["license_path"]
        adapter_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(adapter, adapter_dest)
        shutil.copyfile(license_file, license_dest)

    print(f"Fetched verified adapter from {commit} ({artifact['adapter_sha256']}).")


if __name__ == "__main__":
    main()
