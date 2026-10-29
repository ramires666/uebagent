from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("BROWSER_MCP_DATA_DIR", "runtime")))
    public_origin: str = field(default_factory=lambda: os.getenv("BROWSER_MCP_ORIGIN", "http://localhost:8080"))
    model_base_url: str = field(default_factory=lambda: os.getenv("MODEL_BASE_URL", "http://host.docker.internal:8080/v1"))
    model_alias: str = field(default_factory=lambda: os.getenv("MODEL_ALIAS", "auto"))
    model_protocol: str = field(default_factory=lambda: os.getenv("MODEL_PROTOCOL", "generic"))
    model_key_file: str = field(default_factory=lambda: os.getenv("MODEL_API_KEY_FILE", ""))
    model_prompt_file: str = field(default_factory=lambda: (
        os.getenv("MODEL_PROMPT_FILE", "") if os.getenv("MODEL_PROTOCOL", "generic") != "fara"
        else os.getenv("FARA_PROMPT_FILE", "config/fara-system-prompt.txt")
    ))
    worker_url: str = field(default_factory=lambda: os.getenv("BROWSER_WORKER_URL", ""))
    worker_key_file: str = field(default_factory=lambda: os.getenv("WORKER_API_KEY_FILE", ""))
    desktop_url: str = field(default_factory=lambda: os.getenv("BROWSER_DESKTOP_URL", ""))
    ui_dir: Path = field(default_factory=lambda: Path(os.getenv("BROWSER_MCP_UI_DIR", "ui")))
    test_mode: bool = False
    operator_token: str = ""
    operator_auth_mode: str = field(default_factory=lambda: os.getenv("BROWSER_MCP_OPERATOR_AUTH", "token"))
    mcp_token: str = ""
    session_ttl_s: int = 3600
    catalog_ttl_s: int = 120
    max_message_bytes: int = 1_048_576

    def prepare(self) -> None:
        if self.operator_auth_mode not in {"token", "local"}:
            raise ValueError("BROWSER_MCP_OPERATOR_AUTH must be token or local")
        if self.model_protocol not in {"generic", "fara"}:
            raise ValueError("MODEL_PROTOCOL must be generic or fara")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.operator_token = self.operator_token or self._secret("operator-token.txt")
        self.mcp_token = self.mcp_token or self._secret("mcp-token.txt")

    def _secret(self, name: str) -> str:
        path = self.data_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
        return token

    @staticmethod
    def read_secret(path: str) -> str:
        return Path(path).read_text(encoding="utf-8").strip() if path else ""

