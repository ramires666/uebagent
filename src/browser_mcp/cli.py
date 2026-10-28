from __future__ import annotations

import argparse

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Browser MCP control plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    settings = Settings()
    settings.prepare()
    print(f"Operator key file: {settings.data_dir / 'operator-token.txt'}")
    print(f"MCP key file: {settings.data_dir / 'mcp-token.txt'}")
    print(f"Web UI: {settings.public_origin}")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, ws_max_size=settings.max_message_bytes,
                access_log=False, limit_concurrency=32, backlog=64)


if __name__ == "__main__":
    main()
