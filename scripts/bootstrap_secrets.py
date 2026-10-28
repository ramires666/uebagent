#!/usr/bin/env python3
"""Copy local credential files into pre-created Docker named volumes via stdin."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_DIR = ROOT / "secrets"
WORKER_KEY = SECRET_DIR / "worker-api-key.txt"
WORKER_VOLUME = "browser-mcp-worker-credentials"
MODEL_VOLUME = "browser-mcp-model-credentials"

# Secret bytes arrive on stdin and are written without echoing them to stdout/stderr.
VOLUME_WRITER = """import os, pathlib, sys
data = sys.stdin.buffer.read()
if not data.strip():
    raise SystemExit(2)
target = pathlib.Path('/credentials/key')
temporary = pathlib.Path('/credentials/.key-bootstrap')
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'wb') as stream:
    stream.write(data)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, target)
os.chmod(target, 0o444)
"""


def ensure_secret(path: Path) -> bytes:
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        value = (secrets.token_urlsafe(32) + "\n").encode("ascii")
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    value = path.read_bytes()
    if not value.strip():
        raise RuntimeError(f"Credential file is empty: {path.relative_to(ROOT)}")
    return value


def read_existing_model_key(path: str) -> bytes:
    try:
        model_path = Path(path).expanduser().resolve(strict=True)
        if not model_path.is_file():
            raise RuntimeError("The selected model key path is not a regular file.")
        value = model_path.read_bytes()
    except OSError as exc:
        raise RuntimeError("Could not read the explicitly selected model key file.") from exc
    if not value.strip():
        raise RuntimeError("The selected model key file is empty.")
    return value


def populate_volume(volume_name: str, value: bytes) -> None:
    command = [
        "docker", "run", "--rm", "-i", "--network", "none",
        "--mount", f"type=volume,source={volume_name},target=/credentials",
        "python:3.12-slim", "python", "-c", VOLUME_WRITER,
    ]
    try:
        result = subprocess.run(command, input=value, capture_output=True, check=False)
    except OSError as exc:
        raise RuntimeError("Could not start the Docker credential-volume helper.") from exc
    if result.returncode != 0:
        # Do not forward captured diagnostics: helper/container output is deliberately suppressed.
        raise RuntimeError(f"Could not populate Docker credential volume {volume_name!r} (exit {result.returncode}).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare local worker credentials and optional model API key volume.")
    parser.add_argument(
        "--model-key-file",
        help="Existing nonempty model API key file to copy into the optional model credential volume.",
    )
    args = parser.parse_args()

    worker_value = ensure_secret(WORKER_KEY)
    populate_volume(WORKER_VOLUME, worker_value)
    if args.model_key_file:
        model_value = read_existing_model_key(args.model_key_file)
        populate_volume(MODEL_VOLUME, model_value)
        print("Prepared worker credentials and the selected model key volume.")
    else:
        print("Prepared worker credentials. Model API key authentication remains disabled.")


if __name__ == "__main__":
    main()
