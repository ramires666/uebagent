from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from .domain import DomainError


MAX_SESSIONS = 64
MAX_ATTEMPT_PEERS = 1024


@dataclass
class OperatorSession:
    csrf_token: str
    expires_at: float
    local: bool = False
    last_access: float = 0.0


class AuthService:
    def __init__(self, operator_token: str, ttl_s: int = 3600):
        self.operator_token = operator_token
        self.ttl_s = ttl_s
        self.sessions: dict[str, OperatorSession] = {}
        self.attempts: dict[str, list[float]] = {}

    def login(self, token: str, peer: str) -> tuple[str, str]:
        now = time.monotonic()
        peer = str(peer)[:128]
        # Drop expired peer buckets so rotating addresses cannot grow this map
        # indefinitely. Each live bucket contains at most ten timestamps.
        self.attempts = {
            key: recent for key, values in self.attempts.items()
            if (recent := [x for x in values if now - x < 60])
        }
        if peer not in self.attempts and len(self.attempts) >= MAX_ATTEMPT_PEERS:
            raise DomainError("RATE_LIMITED", "Too many active login sources; retry after the rate limit window")
        recent = [x for x in self.attempts.get(peer, []) if now - x < 60]
        self.attempts[peer] = recent
        if len(recent) >= 10:
            raise DomainError("RATE_LIMITED", "Слишком много попыток. Повторите через минуту.")
        recent.append(now)
        if not secrets.compare_digest(token.encode(), self.operator_token.encode()):
            raise DomainError("UNAUTHORIZED", "Неверный ключ оператора")
        return self.create_session()

    def create_session(self, *, local: bool = False) -> tuple[str, str]:
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value.expires_at > now}
        if len(self.sessions) >= MAX_SESSIONS:
            local_sessions = [(getattr(session, "last_access", 0.0), key)
                              for key, session in self.sessions.items() if getattr(session, "local", False)]
            if not local or not local_sessions:
                raise DomainError("SESSION_LIMIT", "Operator session capacity reached; wait for a session to expire")
            # Local UI sessions are anonymous and individually cookie-bound. Evict
            # only the least recently used local session; never displace a
            # token-authenticated session to make room for a local browser.
            _, oldest_key = min(local_sessions)
            del self.sessions[oldest_key]
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.sessions[session_id] = OperatorSession(csrf, now + self.ttl_s, local=local, last_access=now)
        return session_id, csrf

    def check(self, session_id: str, csrf: str = "", mutate: bool = False) -> OperatorSession:
        session = self.sessions.get(session_id)
        if session is None or session.expires_at <= time.monotonic():
            raise DomainError("UNAUTHORIZED", "Войдите с ключом оператора")
        if mutate and not secrets.compare_digest(csrf, session.csrf_token):
            raise DomainError("CSRF_INVALID", "Неверный токен действия")
        session.last_access = time.monotonic()
        return session
