"""Read-only validation of the model endpoint used by the disposable runner."""
from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit, urlunsplit

import httpx


def _model_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise ValueError("invalid model endpoint")

    # Docker Desktop resolves this host alias inside containers. On Windows the
    # preflight runs on the host, where the same listener is reached via loopback.
    hostname = "127.0.0.1" if parsed.hostname.lower() == "host.docker.internal" else parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "/v1/models", "", ""))


def check_model(base_url: str, alias: str, *, transport: httpx.BaseTransport | None = None) -> bool:
    if not alias or len(alias) > 256:
        return False
    try:
        endpoint = _model_url(base_url)
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=1.0), trust_env=False, transport=transport) as client:
            response = client.get(endpoint)
        if not response.is_success:
            return False
        body = response.json()
        models = body.get("data") if isinstance(body, dict) else None
        if not isinstance(models, list) or not 0 < len(models) <= 128:
            return False
        served: list[str] = []
        for model in models:
            model_id = model.get("id") if isinstance(model, dict) else None
            if (
                not isinstance(model_id, str)
                or not model_id
                or len(model_id) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in model_id)
            ):
                return False
            if model_id not in served:
                served.append(model_id)
        if alias == "auto":
            return len(served) == 1
        return alias in served
    except (httpx.HTTPError, ValueError, TypeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--alias", required=True)
    args = parser.parse_args()
    ready = check_model(args.base_url, args.alias)
    print(json.dumps({"ready": ready}, separators=(",", ":")))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
