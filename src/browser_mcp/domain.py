"""Typed domain objects for browser tasks. This module does not execute browsers."""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class DomainError(Exception):
    """An expected domain or repository error with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    WAITING_APPROVAL = "waiting_approval"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.PAUSE_REQUESTED, TaskStatus.WAITING_HUMAN, TaskStatus.WAITING_APPROVAL, TaskStatus.RECOVERING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.EXPIRED}),
    TaskStatus.PAUSE_REQUESTED: frozenset({TaskStatus.PAUSED, TaskStatus.WAITING_HUMAN, TaskStatus.CANCELLED, TaskStatus.EXPIRED, TaskStatus.RECOVERING}),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.WAITING_HUMAN, TaskStatus.CANCELLED, TaskStatus.EXPIRED, TaskStatus.RECOVERING}),
    TaskStatus.WAITING_HUMAN: frozenset({TaskStatus.PAUSED, TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.EXPIRED, TaskStatus.RECOVERING}),
    TaskStatus.WAITING_APPROVAL: frozenset({TaskStatus.RUNNING, TaskStatus.WAITING_HUMAN, TaskStatus.RECOVERING, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.RECOVERING: frozenset({TaskStatus.PAUSED, TaskStatus.WAITING_HUMAN, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.EXPIRED: frozenset(),
}


class TaskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_s: int = Field(default=900, ge=30, le=3600)
    max_steps: int = Field(default=80, ge=1, le=200)


_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _normalize_origin(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("origin must be a non-empty URL without surrounding whitespace")
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.path or parsed.query or parsed.fragment or "@" in parsed.netloc:
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host:
            raise ValueError
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        else:
            host = host.encode("idna").decode("ascii").lower()
            if not host or any(not label or len(label) > 63 for label in host.split(".")):
                raise ValueError
            if len(host) > 253:
                raise ValueError
            if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError
            if re.fullmatch(r"[0-9.]+", host):
                host = str(ipaddress.IPv4Address(host))
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        suffix = f":{port}" if port is not None and port != 443 else ""
        return f"https://{host}{suffix}"
    except (UnicodeError, ValueError) as exc:
        raise ValueError("origin must be an exact HTTPS origin without credentials, path, query, or fragment") from exc


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = "default"
    sync_cookies: StrictBool = True
    goal: str = Field(min_length=1, max_length=4000)
    goal_language: str = Field(default="ru", min_length=2, max_length=35)
    allowed_origins: list[str] = Field(min_length=1, max_length=50)
    user_inputs: dict[str, Any] = Field(default_factory=dict)
    verbatim_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    secret_refs: list[str] = Field(default_factory=list)
    success_conditions: list[dict[str, Any]] = Field(default_factory=list)
    policy_grant_id: str | None = None
    execution_mode: Literal['supervised', 'autonomous'] = 'autonomous'
    workflow: Literal['agent', 'gemini_prompt_submit'] = 'agent'
    site_capabilities: list[Literal['gemini_prompt_submit']] = Field(default_factory=list, max_length=1)
    limits: TaskLimits = Field(default_factory=TaskLimits)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("profile_id")
    @classmethod
    def safe_profile_id(cls, value: str) -> str:
        if not _PROFILE_RE.fullmatch(value):
            raise ValueError("profile_id must be a simple identifier")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def valid_origins(cls, values: list[str]) -> list[str]:
        normalized = [_normalize_origin(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_origins must be unique after normalization")
        return normalized

    @model_validator(mode='after')
    def valid_site_capabilities(self):
        if self.site_capabilities and 'https://gemini.google.com' not in self.allowed_origins:
            raise ValueError('Gemini capability requires the exact Gemini origin')
        if self.workflow == 'gemini_prompt_submit':
            if self.execution_mode != 'autonomous':
                raise ValueError('Gemini prompt submit workflow requires autonomous execution mode')
            if self.verbatim_text is None or not self.verbatim_text.strip():
                raise ValueError('Gemini prompt submit workflow requires a non-empty verbatim_text payload')
            if self.allowed_origins != ['https://gemini.google.com']:
                raise ValueError('Gemini prompt submit workflow is restricted to the exact Gemini origin')
            if self.site_capabilities != ['gemini_prompt_submit']:
                raise ValueError('Gemini prompt submit workflow requires the gemini_prompt_submit capability')
            if self.success_conditions:
                raise ValueError('Gemini prompt submit workflow confirms submission only and does not support response success conditions')
            if self.user_inputs or self.secret_refs or self.policy_grant_id:
                raise ValueError('Gemini prompt submit workflow accepts only the immutable verbatim_text payload')
        return self


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    principal_id: str
    spec: TaskSpec
    status: TaskStatus
    version: int = Field(ge=1)
    phase: str | None = None
    created_at: str
    updated_at: str
    step_count: int = Field(ge=0)
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
